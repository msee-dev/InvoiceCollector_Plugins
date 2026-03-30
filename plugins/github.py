"""GitHub Billing invoice collector plugin."""

import os
import re
from datetime import date, datetime

import pyotp
import structlog
from playwright.async_api import Page

from src.plugin_base import (
    AuthenticationError,
    DownloadError,
    InvoiceInfo,
    NavigationError,
    ProviderPlugin,
)

logger = structlog.get_logger()


class GitHubPlugin(ProviderPlugin):
    """Collects payment receipts from GitHub Billing."""

    def __init__(self) -> None:
        self._org: str = ""

    @property
    def name(self) -> str:
        return "github"

    @property
    def login_url(self) -> str:
        return "https://github.com/login"

    @property
    def supported_login_methods(self) -> list[str]:
        return ["email", "google", "apple"]

    @property
    def org_name(self) -> str:
        """GitHub organization name — set during authenticate() from credentials or env."""
        return self._org or os.getenv("GITHUB_ORG", "")

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to GitHub. Supports email/password, Google, and Apple sign-in."""
        self._org = credentials.get("org", "")

        try:
            login_method = credentials.get("login_method", "email")

            if login_method != "email":
                from src.oauth import handle_oauth_login
                await handle_oauth_login(
                    page, credentials,
                    expected_url_pattern="**github.com/**",
                )
                return

            # Standard email + password login
            await page.wait_for_selector('input[name="login"]', timeout=15000)
            await page.fill('input[name="login"]', credentials["email"])
            await page.fill('input[name="password"]', credentials["password"])
            await page.click('input[type="submit"]')
            await page.wait_for_timeout(3000)

            # Check if we landed on a 2FA / TOTP page
            totp_input = await page.query_selector(
                'input[name="app_otp"], input[id="app_totp"], '
                'input[autocomplete="one-time-code"]'
            )

            if totp_input:
                if credentials.get("totp_secret"):
                    # Auto-fill from configured secret
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await page.fill('input[name="app_otp"]', totp.now())
                    await page.wait_for_timeout(3000)
                    logger.debug("github_totp_filled")
                elif credentials.get("_totp_callback"):
                    # Request code from web UI
                    logger.info("github_2fa_prompt", message="Requesting TOTP code from web UI...")
                    totp_callback = credentials["_totp_callback"]
                    code = await totp_callback()
                    await page.fill('input[name="app_otp"]', code)
                    await page.wait_for_timeout(3000)
                    logger.debug("github_totp_filled_from_ui")
                else:
                    # No TOTP secret and no callback — wait for manual entry in headed mode
                    is_headless = not await page.evaluate("() => !!window.outerWidth && window.outerWidth > 0")
                    if is_headless:
                        raise AuthenticationError(
                            "GitHub requires 2FA but no TOTP secret is configured. "
                            "Set the TOTP secret in credentials, or run from the web UI."
                        )
                    logger.info("github_2fa_waiting", message="Waiting for user to enter 2FA code...")
                    await page.wait_for_function(
                        "() => !document.querySelector('input[name=\"app_otp\"]')",
                        timeout=120_000,
                    )
                    await page.wait_for_timeout(3000)

            # Check for device verification prompt
            page_text = (await page.text_content("body") or "").lower()
            if "device verification" in page_text or "geräteverifizierung" in page_text:
                is_headless = not await page.evaluate("() => !!window.outerWidth && window.outerWidth > 0")
                if is_headless:
                    raise AuthenticationError(
                        "GitHub requires device verification. Use debug mode (headed browser)."
                    )
                logger.info("github_device_verify", message="Waiting for device verification...")
                await page.wait_for_function(
                    "() => !window.location.pathname.includes('sessions')",
                    timeout=120_000,
                )

            # Verify we're past auth
            if "sessions" in page.url or "login" in page.url:
                raise AuthenticationError(
                    f"GitHub login incomplete — still on {page.url}. "
                    "Check credentials, 2FA, or use debug mode."
                )

            logger.debug("github_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"GitHub login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to GitHub billing / payment history."""
        try:
            org = self.org_name
            if org:
                url = f"https://github.com/organizations/{org}/billing/history"
            else:
                url = "https://github.com/account/billing/history"

            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            logger.debug("github_billing_page", url=page.url)
        except Exception as exc:
            raise NavigationError(f"GitHub billing navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse payment history from GitHub.

        GitHub uses li.Box-row elements (not a <table>):
        - .date time → invoice date
        - .id code span → transaction ID
        - .amount → dollar amount
        - a[href*=receipt], a[href*=invoice] → download links
        """
        invoices = []

        # Wait for payment history rows to render
        try:
            await page.wait_for_selector("li.Box-row", timeout=15000)
        except Exception:
            logger.warning("github_no_invoice_rows")
            return []

        rows = await page.query_selector_all("li.Box-row")
        logger.debug("github_rows_found", count=len(rows))

        for row in rows:
            try:
                # Date
                time_el = await row.query_selector(".date time, time")
                if not time_el:
                    continue
                date_text = (await time_el.text_content() or "").strip()
                invoice_date = self._parse_date(date_text)
                if invoice_date is None:
                    continue

                # ID — use the specific span to avoid tooltip text
                id_el = await row.query_selector('span[id^="short-transaction-id"]')
                if not id_el:
                    id_el = await row.query_selector(".id code span, .id code, .id")
                invoice_id = (await id_el.text_content() or "").strip() if id_el else ""
                if not invoice_id:
                    invoice_id = f"GH-{invoice_date.isoformat()}"

                # Amount
                amount_el = await row.query_selector(".amount")
                amount = (await amount_el.text_content() or "").strip() if amount_el else None

                # Download URL — prefer invoice, fall back to receipt (for refunds)
                download_url = None

                # 1. Invoice: <invoice-download data-url="...">
                inv_dl = await row.query_selector("invoice-download[data-url]")
                if inv_dl:
                    download_url = await inv_dl.get_attribute("data-url")

                # 2. Receipt PDF (refunds only have receipts, not invoices)
                if not download_url:
                    pdf_link = await row.query_selector('a[href$=".pdf"]')
                    if pdf_link:
                        download_url = await pdf_link.get_attribute("href")

                # 3. Receipt HTML → append .pdf
                if not download_url:
                    receipt_link = await row.query_selector('a[href*="/receipt/"]')
                    if receipt_link:
                        href = await receipt_link.get_attribute("href") or ""
                        if not href.endswith(".pdf"):
                            href += ".pdf"
                        download_url = href

                if not download_url:
                    logger.warning("github_no_download_link", id=invoice_id)
                    continue

                invoices.append(
                    InvoiceInfo(
                        provider=self.name,
                        invoice_id=invoice_id,
                        invoice_date=invoice_date,
                        amount=amount,
                        currency="USD",
                        download_url=download_url,
                    )
                )
                logger.debug("github_invoice_found", id=invoice_id, date=str(invoice_date), amount=amount)
            except Exception:
                continue

        logger.info("github_invoices_parsed", count=len(invoices))
        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
        """Parse common GitHub date formats."""
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%B %d %Y"):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download a receipt/invoice PDF from GitHub."""
        try:
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://github.com{url}"

                logger.debug("github_downloading", id=invoice.invoice_id, url=url)
                resp = await page.request.get(url)
                body = await resp.body()
                if body and body[:5].startswith(b"%PDF-"):
                    return body

                # Invoice download URL may return a ZIP or redirect — try receipt PDF
                logger.debug("github_not_pdf", id=invoice.invoice_id, size=len(body), header=body[:30])

            # Fallback: find receipt PDF link from the page
            row = await page.query_selector(f'li.Box-row:has-text("{invoice.invoice_id}")')
            if row:
                # Try PDF link first
                pdf_link = await row.query_selector('a[href$=".pdf"]')
                if pdf_link:
                    href = await pdf_link.get_attribute("href") or ""
                    url = href if href.startswith("http") else f"https://github.com{href}"
                    logger.debug("github_trying_receipt_pdf", id=invoice.invoice_id, url=url)
                    resp = await page.request.get(url)
                    body = await resp.body()
                    if body and body[:5].startswith(b"%PDF-"):
                        return body

                # Try all links in the row
                links = await row.query_selector_all("a[href]")
                for link in links:
                    href = await link.get_attribute("href") or ""
                    if not href or href == "#":
                        continue
                    dl_url = href if href.startswith("http") else f"https://github.com{href}"
                    if ".pdf" in dl_url or "/receipt/" in dl_url:
                        resp = await page.request.get(dl_url)
                        body = await resp.body()
                        if body and body[:5].startswith(b"%PDF-"):
                            return body

            raise DownloadError(f"No PDF download found for {invoice.invoice_id}")
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"GitHub download failed for {invoice.invoice_id}: {exc}"
            ) from exc
