"""Hetzner Cloud Console invoice collector plugin.

Hetzner uses a Cloudflare-style proof-of-work challenge (/_ray/pow)
before showing the login form. The plugin waits for the challenge
to complete before attempting login.
"""

import structlog
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

logger = structlog.get_logger()


class HetznerPlugin(ProviderPlugin):
    """Collects invoices from Hetzner Cloud Console."""

    @property
    def name(self) -> str:
        return "hetzner"

    @property
    def login_url(self) -> str:
        return "https://accounts.hetzner.com/login"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Hetzner with username, password, and optional TOTP.

        Handles the Cloudflare proof-of-work challenge that appears
        before the login form.
        """
        try:
            # Wait for page to load — may go through /_ray/pow challenge first
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(3000)

            # Wait for login form — may be behind a PoW challenge that
            # takes 60+ seconds and causes page navigations
            logger.info("hetzner_waiting_login_form", url=page.url)
            try:
                await page.wait_for_selector('input[name="_username"]', timeout=90000)
            except Exception:
                # PoW might have passed but didn't redirect — try navigating manually
                logger.info("hetzner_manual_nav_after_pow", url=page.url)
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
                try:
                    await page.wait_for_selector('input[name="_username"]', timeout=90000)
                except Exception:
                    raise AuthenticationError(
                        f"Hetzner login form not found. Still on: {page.url}"
                    )

            # Fill login form
            await page.fill('input[name="_username"]', credentials["email"])
            await page.fill('input[name="_password"]', credentials["password"])
            logger.debug("hetzner_credentials_filled")

            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

            # TOTP if needed
            if credentials.get("totp_secret"):
                totp_input = await page.query_selector('input[name="_totp"]')
                if totp_input:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await page.fill('input[name="_totp"]', totp.now())
                    await page.click('button[type="submit"]')
                    await page.wait_for_timeout(3000)
            elif credentials.get("_totp_callback"):
                totp_input = await page.query_selector('input[name="_totp"]')
                if totp_input:
                    logger.info("hetzner_totp_prompt", message="Requesting TOTP from web UI...")
                    code = await credentials["_totp_callback"]()
                    await page.fill('input[name="_totp"]', code)
                    await page.click('button[type="submit"]')
                    await page.wait_for_timeout(3000)

            logger.debug("hetzner_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Hetzner login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to the Hetzner invoices page."""
        try:
            await page.goto(
                "https://console.hetzner.cloud/billing/invoices",
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)
            logger.debug("hetzner_invoices_page", url=page.url)
        except Exception as exc:
            raise NavigationError(f"Hetzner invoices navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse invoices from the Hetzner billing page."""
        invoices = []

        try:
            await page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception:
            logger.warning("hetzner_no_invoice_table")
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

                invoice_date = self._parse_date(date_str)
                if invoice_date is None:
                    continue

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

        logger.info("hetzner_invoices_parsed", count=len(invoices))
        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
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
                body = await resp.body()
                if body and body[:5].startswith(b"%PDF-"):
                    return body

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
