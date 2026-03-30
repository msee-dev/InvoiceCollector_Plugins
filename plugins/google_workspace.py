"""Google Workspace/Services billing invoice collector.

Covers: YouTube Premium, Gemini, Google One, Google Workspace,
Google Domains, and other Google services billed through pay.google.com.

Auth: Google account login (same as google_cloud but different billing page).
Billing page: https://pay.google.com/gp/w/u/0/home/activity
"""

import re
from datetime import date, datetime

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


class GoogleWorkspacePlugin(ProviderPlugin):
    """Collects invoices from Google Pay / Google services billing."""

    @property
    def name(self) -> str:
        return "google_services"

    @property
    def login_url(self) -> str:
        return "https://accounts.google.com/signin"

    @property
    def supported_login_methods(self) -> list[str]:
        return ["email"]  # Google's own login page

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Google account."""
        try:
            # Email step
            email_sel = 'input[type="email"], input[name="identifier"], #identifierId'
            await page.wait_for_selector(email_sel, timeout=15000)
            await page.fill(email_sel, credentials["email"])

            # Click Next (multilanguage)
            await page.click(
                '#identifierNext, '
                'button:has-text("Next"), '
                'button:has-text("Weiter"), '
                'button:has-text("Suivant")'
            )
            await page.wait_for_timeout(3000)

            # Password step
            pw_sel = 'input[type="password"], input[name="Passwd"]'
            await page.wait_for_selector(pw_sel, timeout=15000)
            await page.fill(pw_sel, credentials["password"])

            await page.click(
                '#passwordNext, '
                'button:has-text("Next"), '
                'button:has-text("Weiter"), '
                'button:has-text("Suivant")'
            )
            await page.wait_for_timeout(5000)

            # TOTP if configured
            if credentials.get("totp_secret"):
                import pyotp
                totp_sel = (
                    'input[name="totpPin"], input[type="tel"], '
                    'input[inputmode="numeric"], input[autocomplete="one-time-code"]'
                )
                try:
                    await page.wait_for_selector(totp_sel, timeout=10000)
                    code = pyotp.TOTP(credentials["totp_secret"]).now()
                    await page.fill(totp_sel, code)
                    await page.click(
                        '#totpNext, button:has-text("Next"), button:has-text("Weiter")'
                    )
                    await page.wait_for_timeout(3000)
                except Exception:
                    logger.debug("google_no_totp_prompt")
            elif credentials.get("_totp_callback"):
                # Check if 2FA is prompted
                totp_sel = 'input[name="totpPin"], input[type="tel"], input[inputmode="numeric"]'
                totp_input = await page.query_selector(totp_sel)
                if totp_input:
                    code = await credentials["_totp_callback"]()
                    await page.fill(totp_sel, code)
                    await page.click(
                        '#totpNext, button:has-text("Next"), button:has-text("Weiter")'
                    )
                    await page.wait_for_timeout(3000)

            logger.debug("google_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Google login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to Google Pay activity/transactions page."""
        try:
            await page.goto(
                "https://pay.google.com/gp/w/u/0/home/activity",
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)

            # If redirected to subscriptions, navigate to activity
            if "activity" not in page.url:
                await page.goto(
                    "https://pay.google.com/gp/w/u/0/home/activity",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(5000)

            logger.debug("google_billing_page", url=page.url)
        except Exception as exc:
            raise NavigationError(f"Google Pay navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse transactions from Google Pay activity page."""
        invoices = []

        # Google Pay uses various selectors for transaction rows
        row_selectors = [
            '[data-was-visible] [role="listitem"]',
            '[class*="transaction"]',
            '[class*="activity"] [role="listitem"]',
            'div[data-order-id]',
        ]

        rows = []
        for sel in row_selectors:
            rows = await page.query_selector_all(sel)
            if rows:
                logger.debug("google_rows_found", selector=sel, count=len(rows))
                break

        if not rows:
            # Try AI fallback if enabled
            logger.warning("google_no_transaction_rows")
            return []

        for row in rows:
            try:
                text = (await row.text_content() or "").strip()
                if not text or len(text) < 10:
                    continue

                # Extract date
                date_match = re.search(
                    r"(\d{1,2}\.\s*\w+\s*\d{4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
                    text,
                )
                if not date_match:
                    continue

                invoice_date = self._parse_date(date_match.group(1))
                if not invoice_date:
                    continue

                # Extract amount
                amount_match = re.search(
                    r'([$€£]\s*[\d,.]+|[\d,.]+\s*(?:USD|EUR|GBP|CHF))',
                    text,
                )
                amount = amount_match.group(1) if amount_match else None

                # Extract order ID if available
                order_id = await row.get_attribute("data-order-id") or ""
                if not order_id:
                    order_id_match = re.search(r'(GPA\.\d[\d-]+|\d{10,})', text)
                    order_id = order_id_match.group(1) if order_id_match else ""

                invoice_id = order_id or f"GOOG-{invoice_date.isoformat()}"

                # Look for receipt/download link
                download_url = None
                link = await row.query_selector('a[href*="receipt"], a[href*="invoice"], a[href*="order"]')
                if link:
                    download_url = await link.get_attribute("href")

                invoices.append(
                    InvoiceInfo(
                        provider=self.name,
                        invoice_id=invoice_id,
                        invoice_date=invoice_date,
                        amount=amount,
                        download_url=download_url,
                    )
                )
            except Exception:
                continue

        logger.info("google_invoices_parsed", count=len(invoices))
        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
        """Parse various date formats from Google Pay."""
        for fmt in (
            "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d",
            "%d. %B %Y", "%d. %b %Y", "%d %b %Y",
            "%m/%d/%Y", "%d/%m/%Y",
        ):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download a receipt/invoice from Google Pay."""
        try:
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://pay.google.com{url}"

                resp = await page.request.get(url)
                body = await resp.body()
                if body and body[:5].startswith(b"%PDF-"):
                    return body

            # Fallback: navigate to order detail and look for download
            raise DownloadError(
                f"No PDF download available for {invoice.invoice_id}. "
                "Google Pay may require manual receipt download."
            )
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Google Pay download failed for {invoice.invoice_id}: {exc}"
            ) from exc
