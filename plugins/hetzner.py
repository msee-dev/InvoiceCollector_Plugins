"""Hetzner Cloud Console invoice collector plugin."""

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


class HetznerPlugin(ProviderPlugin):
    """Collects invoices from Hetzner Cloud Console."""

    @property
    def name(self) -> str:
        return "hetzner"

    @property
    def login_url(self) -> str:
        return "https://accounts.hetzner.com/login"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Hetzner with email, password, and optional TOTP."""
        try:
            await page.wait_for_selector('input[name="_username"]', timeout=15000)
            await page.fill('input[name="_username"]', credentials["email"])
            await page.fill('input[name="_password"]', credentials["password"])
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")

            # TOTP if needed
            if credentials.get("totp_secret"):
                totp_input = await page.query_selector('input[name="_totp"]')
                if totp_input:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await page.fill('input[name="_totp"]', totp.now())
                    await page.click('button[type="submit"]')
                    await page.wait_for_load_state("networkidle")

        except Exception as exc:
            raise AuthenticationError(f"Hetzner login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to the Hetzner invoices page."""
        try:
            await page.goto(
                "https://console.hetzner.cloud/billing/invoices",
                wait_until="domcontentloaded",
            )
            await page.wait_for_load_state("networkidle")
        except Exception as exc:
            raise NavigationError(f"Hetzner invoices navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse invoices from the Hetzner billing page."""
        invoices = []

        # Wait for invoice table to load
        try:
            await page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            return []
        rows = await page.query_selector_all("table tbody tr")

        for row in rows:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                invoice_id = (await cells[0].text_content() or "").strip()
                date_str = (await cells[1].text_content() or "").strip()
                amount = (await cells[2].text_content() or "").strip()

                if not invoice_id:
                    continue

                # Parse date — Hetzner typically shows DD.MM.YYYY
                invoice_date = self._parse_date(date_str)
                if invoice_date is None:
                    continue

                # Check for PDF download link
                pdf_link = await row.query_selector('a[href*=".pdf"], a[href*="download"]')
                download_url = None
                if pdf_link:
                    download_url = await pdf_link.get_attribute("href")

                invoices.append(
                    InvoiceInfo(
                        provider=self.name,
                        invoice_id=invoice_id,
                        invoice_date=invoice_date,
                        amount=amount,
                        currency="EUR",
                        download_url=download_url,
                    )
                )
            except Exception:
                continue

        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
        """Try parsing common Hetzner date formats."""
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download an invoice PDF from Hetzner."""
        try:
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://console.hetzner.cloud{url}"
                resp = await page.request.get(url)
                return await resp.body()

            # Fallback: find download button in the invoice row
            safe_id = escape_selector_text(invoice.invoice_id)
            async with page.expect_download() as download_info:
                await page.click(
                    f'tr:has-text("{safe_id}") a[href*="download"], '
                    f'tr:has-text("{safe_id}") button:has-text("PDF")'
                )
            download = await download_info.value
            path = await download.path()
            if path is None:
                raise DownloadError("Download path is None")
            with open(path, "rb") as f:
                return f.read()
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Hetzner download failed for {invoice.invoice_id}: {exc}"
            ) from exc
