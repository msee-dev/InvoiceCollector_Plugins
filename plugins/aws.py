"""AWS Billing Console invoice collector plugin."""

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
)


class AWSPlugin(ProviderPlugin):
    """Collects invoices from the AWS Billing Console."""

    @property
    def name(self) -> str:
        return "aws"

    @property
    def login_url(self) -> str:
        return "https://console.aws.amazon.com/billing/"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to AWS Console with email, password, and optional TOTP."""
        try:
            # Root user email
            await page.wait_for_selector('input[id="resolving_input"]', timeout=15000)
            await page.fill('input[id="resolving_input"]', credentials["email"])
            await page.click('button[id="next_button"]')

            # Password
            await page.wait_for_selector('input[id="password"]', timeout=15000)
            await page.fill('input[id="password"]', credentials["password"])
            await page.click('button[id="signin_button"]')

            # TOTP if configured
            if credentials.get("totp_secret"):
                await page.wait_for_selector('input[id="mfaCode"]', timeout=15000)
                totp = pyotp.TOTP(credentials["totp_secret"])
                await page.fill('input[id="mfaCode"]', totp.now())
                await page.click('button[id="submitMfa_button"]')

            # Wait for console to load
            await page.wait_for_load_state("networkidle")

        except Exception as exc:
            raise AuthenticationError(f"AWS login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to the AWS Bills / Invoices page."""
        try:
            await page.goto(
                "https://us-east-1.console.aws.amazon.com/billing/home#/bills",
                wait_until="domcontentloaded",
            )
            await page.wait_for_load_state("networkidle")
        except Exception as exc:
            raise NavigationError(f"AWS invoices navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse invoices from the AWS Bills page."""
        invoices = []

        try:
            await page.wait_for_selector(
                '[data-testid="bill-summary-row"], #content table tbody tr',
                timeout=15000,
            )
        except Exception:
            return []

        # AWS shows bills by month — look for bill summary rows
        rows = await page.query_selector_all(
            '[data-testid="bill-summary-row"], #content table tbody tr'
        )

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue

                text = await cells[0].text_content()
                if not text:
                    continue
                text = text.strip()

                # Try to parse date (AWS shows months like "March 2026")
                invoice_date = self._parse_aws_date(text)
                if invoice_date is None:
                    continue

                amount_text = (await cells[-1].text_content() or "").strip()

                # Generate an invoice ID from the date
                invoice_id = f"AWS-{invoice_date.isoformat()}"

                # Look for PDF download link within this row
                pdf_link = await row.query_selector(
                    'a[href*="invoice"], a[href*="pdf"], a[href*="download"]'
                )
                download_url = None
                if pdf_link:
                    download_url = await pdf_link.get_attribute("href")

                invoices.append(
                    InvoiceInfo(
                        provider=self.name,
                        invoice_id=invoice_id,
                        invoice_date=invoice_date,
                        amount=amount_text,
                        currency="USD",
                        download_url=download_url,
                    )
                )
            except Exception:
                continue

        return invoices

    @staticmethod
    def _parse_aws_date(text: str) -> date | None:
        """Try to parse a date string from AWS billing page."""
        # Try "March 2026" format
        try:
            dt = datetime.strptime(text.strip(), "%B %Y")
            return dt.date().replace(day=1)
        except ValueError:
            pass

        # Try ISO-like format
        match = re.search(r"(\d{4})[/-](\d{1,2})", text)
        if match:
            return date(int(match.group(1)), int(match.group(2)), 1)

        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download an invoice PDF from AWS."""
        try:
            # Use the download URL captured during invoice listing
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://us-east-1.console.aws.amazon.com{url}"
                resp = await page.request.get(url)
                body = await resp.body()
                if len(body) > 0:
                    return body

            # Fallback: find download link scoped to the invoice row
            row_selector = (
                f'tr:has-text("{invoice.invoice_date.strftime("%B %Y")}"), '
                f'tr:has-text("{invoice.invoice_id}")'
            )
            row = await page.query_selector(row_selector)
            if row:
                download_link = await row.query_selector(
                    'a[href*="invoice"], a[href*="pdf"], '
                    'button:has-text("Download")'
                )
                if download_link:
                    async with page.expect_download() as download_info:
                        await download_link.click()
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
            raise DownloadError(f"AWS download failed for {invoice.invoice_id}: {exc}") from exc
