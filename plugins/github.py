"""GitHub Billing invoice collector plugin."""

import os
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
    def org_name(self) -> str:
        """GitHub organization name — set during authenticate() from credentials or env."""
        return self._org or os.getenv("GITHUB_ORG", "")

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to GitHub with username, password, and TOTP."""
        # Capture org from credentials (set by CredentialStore from GITHUB_ORG env)
        self._org = credentials.get("org", "")
        try:
            await page.wait_for_selector('input[name="login"]', timeout=15000)
            await page.fill('input[name="login"]', credentials["email"])
            await page.fill('input[name="password"]', credentials["password"])
            await page.click('input[type="submit"]')
            await page.wait_for_load_state("networkidle")

            # TOTP
            if credentials.get("totp_secret"):
                totp_input = await page.query_selector('input[name="app_otp"]')
                if totp_input:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await page.fill('input[name="app_otp"]', totp.now())
                    # GitHub auto-submits TOTP
                    await page.wait_for_load_state("networkidle")

        except Exception as exc:
            raise AuthenticationError(f"GitHub login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to GitHub billing / payment history."""
        try:
            org = self.org_name
            if org:
                url = f"https://github.com/organizations/{org}/billing/payment-history"
            else:
                url = "https://github.com/settings/billing/payment-history"

            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")
        except Exception as exc:
            raise NavigationError(f"GitHub billing navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse payment history receipts from GitHub."""
        invoices = []

        # GitHub payment history shows a list of receipts
        rows = await page.query_selector_all(
            '.payment-history tr, [data-testid="payment-history-row"], '
            "table tbody tr"
        )

        for row in rows:
            try:
                text = (await row.text_content() or "").strip()
                if not text:
                    continue

                # Extract date and amount from row text
                date_match = re.search(
                    r"(\w+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})", text
                )
                amount_match = re.search(r"(\$[\d,.]+)", text)

                if not date_match:
                    continue

                invoice_date = self._parse_date(date_match.group(1))
                if invoice_date is None:
                    continue

                amount = amount_match.group(1) if amount_match else None
                invoice_id = f"GH-{invoice_date.isoformat()}"

                # Look for receipt/PDF link
                link = await row.query_selector('a[href*="receipt"], a[href*="invoice"]')
                download_url = None
                if link:
                    download_url = await link.get_attribute("href")

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
        """Parse common GitHub date formats."""
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%B %d %Y"):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download a receipt PDF from GitHub."""
        try:
            if invoice.download_url:
                url = invoice.download_url
                if not url.startswith("http"):
                    url = f"https://github.com{url}"
                resp = await page.request.get(url)
                body = await resp.body()
                if len(body) > 0:
                    return body

            # Try clicking a download link on the page
            async with page.expect_download() as download_info:
                await page.click(
                    f'tr:has-text("{invoice.invoice_date.isoformat()}") a, '
                    f'a:has-text("Receipt")'
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
                f"GitHub download failed for {invoice.invoice_id}: {exc}"
            ) from exc
