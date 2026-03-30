"""Google Cloud Console billing invoice collector plugin."""

import re
from datetime import date, datetime

import pyotp
from playwright.async_api import Page

from src.plugin_base import (
    AuthenticationError,
    DownloadError,
    InvoiceInfo,
    NavigationError,
    ProviderPlugin,
    escape_selector_text,
)


class GoogleCloudPlugin(ProviderPlugin):
    """Collects invoices from Google Cloud Console Billing."""

    @property
    def name(self) -> str:
        return "google_cloud"

    @property
    def login_url(self) -> str:
        return "https://accounts.google.com/signin"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Google with email, password, and optional TOTP.

        Google uses a multi-step login flow: email first, then password,
        then optional 2FA challenge.
        """
        try:
            # Email step
            await page.wait_for_selector(
                'input[type="email"], input#identifierId', timeout=15000
            )
            await page.fill(
                'input[type="email"], input#identifierId', credentials["email"]
            )
            await page.click(
                'button#identifierNext, button:has-text("Next")'
            )
            await page.wait_for_load_state("networkidle")

            # Password step
            await page.wait_for_selector(
                'input[type="password"], input[name="Passwd"]', timeout=15000
            )
            await page.fill(
                'input[type="password"], input[name="Passwd"]',
                credentials["password"],
            )
            await page.click(
                'button#passwordNext, button:has-text("Next")'
            )
            await page.wait_for_load_state("networkidle")

            # TOTP if configured
            if credentials.get("totp_secret"):
                totp_input = await page.query_selector(
                    'input[name="totpPin"], input#totpPin, '
                    'input[type="tel"][aria-label*="code"]'
                )
                if totp_input:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await totp_input.fill(totp.now())
                    await page.click(
                        'button#totpNext, button:has-text("Next")'
                    )
                    await page.wait_for_load_state("networkidle")

            # Wait for redirect to Google services
            await page.wait_for_load_state("networkidle")

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Google login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to the Google Cloud billing documents page.

        Uses the billing account ID from credentials if available,
        otherwise navigates to the billing overview which lists accounts.
        """
        try:
            # Navigate to Cloud Console billing transactions/documents
            await page.goto(
                "https://console.cloud.google.com/billing",
                wait_until="domcontentloaded",
            )
            await page.wait_for_load_state("networkidle")

            # Look for and click into "Transactions" or "Documents" tab
            docs_link = await page.query_selector(
                'a[href*="documents"], a[href*="transactions"], '
                'a:has-text("Transactions"), a:has-text("Documents"), '
                '[data-testid*="transaction"], [data-testid*="document"]'
            )
            if docs_link:
                await docs_link.click()
                await page.wait_for_load_state("networkidle")

        except NavigationError:
            raise
        except Exception as exc:
            raise NavigationError(
                f"Google Cloud billing navigation failed: {exc}"
            ) from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse invoices/documents from the GCP billing page."""
        invoices = []

        try:
            await page.wait_for_selector(
                "table tbody tr, [role='row']", timeout=15000
            )
        except Exception:
            return []

        rows = await page.query_selector_all(
            "table tbody tr, [role='row']:not([role='columnheader'])"
        )

        for row in rows:
            try:
                cells = await row.query_selector_all("td, [role='cell']")
                if len(cells) < 3:
                    continue

                # Extract text from cells — GCP typically shows:
                # Document number | Date | Type | Amount | Status
                texts = []
                for cell in cells:
                    texts.append((await cell.text_content() or "").strip())

                # Find date (try each cell)
                invoice_date = None
                for text in texts:
                    invoice_date = self._parse_date(text)
                    if invoice_date:
                        break
                if invoice_date is None:
                    continue

                # Find invoice ID (typically starts with a number or has "INV" pattern)
                invoice_id = None
                for text in texts:
                    if re.match(r"^\d{4,}", text) or "INV" in text.upper():
                        invoice_id = text
                        break
                if not invoice_id:
                    invoice_id = f"GCP-{invoice_date.isoformat()}"

                # Find amount
                amount = None
                for text in texts:
                    if re.search(r"[$€£]\s*[\d,.]+|[\d,.]+\s*(?:USD|EUR)", text):
                        amount = text
                        break

                # Look for PDF download link
                pdf_link = await row.query_selector(
                    'a[href*="pdf"], a[href*="document"], '
                    'a[href*="download"], button[aria-label*="Download"]'
                )
                download_url = None
                if pdf_link:
                    download_url = await pdf_link.get_attribute("href")

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
            except Exception:
                continue

        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
        """Parse date formats used in GCP billing."""
        for fmt in (
            "%b %d, %Y",    # "Mar 01, 2026"
            "%B %d, %Y",    # "March 01, 2026"
            "%Y-%m-%d",     # "2026-03-01"
            "%m/%d/%Y",     # "03/01/2026"
            "%d %b %Y",     # "01 Mar 2026"
        ):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download an invoice PDF from Google Cloud billing."""
        try:
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://console.cloud.google.com{url}"
                resp = await page.request.get(url)
                body = await resp.body()
                if len(body) > 0:
                    return body

            # Fallback: find download link in the invoice row
            safe_id = escape_selector_text(invoice.invoice_id)
            row = await page.query_selector(
                f'tr:has-text("{safe_id}"), [role="row"]:has-text("{safe_id}")'
            )
            if row:
                dl_link = await row.query_selector(
                    'a[href*="pdf"], a[href*="download"], '
                    'button[aria-label*="Download"]'
                )
                if dl_link:
                    href = await dl_link.get_attribute("href")
                    if href:
                        url = href if href.startswith("http") else f"https://console.cloud.google.com{href}"
                        resp = await page.request.get(url)
                        body = await resp.body()
                        if len(body) > 0:
                            return body

                    # Try click-based download
                    async with page.expect_download() as download_info:
                        await dl_link.click()
                    download = await download_info.value
                    path = await download.path()
                    if path is None:
                        raise DownloadError("Download path is None")
                    with open(path, "rb") as f:
                        return f.read()

            raise DownloadError("No download mechanism found")
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Google Cloud download failed for {invoice.invoice_id}: {exc}"
            ) from exc
