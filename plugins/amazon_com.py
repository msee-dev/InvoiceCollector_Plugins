"""Amazon.com order invoice collector plugin.

Downloads invoices from Amazon.com order history by rendering the
invoice HTML page to PDF via Playwright's print-to-PDF capability.
"""

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

BASE_URL = "https://www.amazon.com"
ORDER_HISTORY_URL = f"{BASE_URL}/gp/css/order-history"


class AmazonComPlugin(ProviderPlugin):
    """Collects invoices from Amazon.com order history."""

    @property
    def name(self) -> str:
        return "amazon_com"

    @property
    def login_url(self) -> str:
        return f"{BASE_URL}/ap/signin"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Amazon.com with email, password, and optional TOTP."""
        try:
            # Email step
            await page.wait_for_selector(
                'input[name="email"], #ap_email', timeout=15000
            )
            await page.fill(
                'input[name="email"], #ap_email', credentials["email"]
            )

            # Click continue (some flows split email/password)
            continue_btn = await page.query_selector(
                '#continue, input[id="continue"]'
            )
            if continue_btn:
                await continue_btn.click()
                await page.wait_for_load_state("domcontentloaded")

            # Password step
            await page.wait_for_selector(
                'input[name="password"], #ap_password', timeout=15000
            )
            await page.fill(
                'input[name="password"], #ap_password', credentials["password"]
            )
            await page.click('#signInSubmit, input[type="submit"]')
            await page.wait_for_load_state("networkidle")

            # Handle TOTP 2FA if needed
            if credentials.get("totp_secret"):
                totp_field = await page.query_selector(
                    'input[name="otpCode"], #auth-mfa-otpcode, '
                    'input[id="auth-mfa-otpcode"]'
                )
                if totp_field:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await totp_field.fill(totp.now())
                    submit = await page.query_selector(
                        '#auth-signin-button, input[type="submit"]'
                    )
                    if submit:
                        await submit.click()
                    await page.wait_for_load_state("networkidle")

            # Verify we're logged in (look for account indicator)
            if "/ap/signin" in page.url:
                raise AuthenticationError("Still on sign-in page after login attempt")

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Amazon.com login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to Amazon.com order history."""
        try:
            await page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            # Verify we landed on order history
            if "/ap/signin" in page.url:
                raise NavigationError("Redirected to login — session expired")

        except NavigationError:
            raise
        except Exception as exc:
            raise NavigationError(
                f"Amazon.com order history navigation failed: {exc}"
            ) from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse orders from Amazon.com order history page."""
        invoices = []

        try:
            await page.wait_for_selector(
                '.order-card, .js-order-card, [data-component="order"]',
                timeout=15000,
            )
        except Exception:
            return []

        order_cards = await page.query_selector_all(
            '.order-card, .js-order-card, [data-component="order"]'
        )

        for card in order_cards:
            try:
                invoice = await self._parse_order_card(card)
                if invoice:
                    invoices.append(invoice)
            except Exception:
                continue

        return invoices

    async def _parse_order_card(self, card) -> InvoiceInfo | None:
        """Extract invoice info from a single order card element."""
        # Find order date
        date_el = await card.query_selector(
            '.order-info .a-color-secondary, '
            '[data-testid="order-date"], '
            '.value:first-of-type'
        )
        if not date_el:
            return None

        date_text = (await date_el.text_content() or "").strip()
        # Remove "Order placed" prefix if present
        date_text = re.sub(r"^Order\s+placed\s*", "", date_text, flags=re.IGNORECASE).strip()
        invoice_date = self._parse_amazon_date(date_text)
        if invoice_date is None:
            return None

        # Find order ID
        order_id = None
        order_id_el = await card.query_selector(
            '.order-info .value[dir="ltr"], '
            '[data-testid="order-id"], '
            'bdi'
        )
        if order_id_el:
            order_id = (await order_id_el.text_content() or "").strip()

        if not order_id:
            # Try to extract from any text containing order number pattern
            all_text = await card.text_content() or ""
            match = re.search(r"\b(\d{3}-\d{7}-\d{7})\b", all_text)
            if match:
                order_id = match.group(1)

        if not order_id:
            return None

        # Find order total
        amount = None
        total_el = await card.query_selector(
            '.order-info .a-color-secondary + .value, '
            '[data-testid="order-total"]'
        )
        if total_el:
            amount = (await total_el.text_content() or "").strip()

        # Find invoice link
        download_url = None
        invoice_link = await card.query_selector(
            'a[href*="invoice"], a:has-text("View invoice")'
        )
        if invoice_link:
            href = await invoice_link.get_attribute("href")
            if href and not href.startswith("http"):
                href = f"{BASE_URL}{href}"
            download_url = href

        return InvoiceInfo(
            provider=self.name,
            invoice_id=order_id,
            invoice_date=invoice_date,
            amount=amount,
            currency="USD",
            download_url=download_url,
        )

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download invoice by rendering the invoice page as PDF."""
        try:
            if invoice.download_url:
                # Open invoice in a new tab and print to PDF
                new_page = await page.context.new_page()
                try:
                    await new_page.goto(
                        invoice.download_url, wait_until="networkidle"
                    )
                    pdf_bytes = await new_page.pdf(
                        format="Letter",
                        margin={
                            "top": "0.5in",
                            "right": "0.5in",
                            "bottom": "0.5in",
                            "left": "0.5in",
                        },
                    )
                    if pdf_bytes and len(pdf_bytes) > 0:
                        return pdf_bytes
                finally:
                    await new_page.close()

            # Fallback: find invoice link on the order detail page
            detail_url = f"{BASE_URL}/gp/your-account/order-details?orderID={invoice.invoice_id}"
            await page.goto(detail_url, wait_until="networkidle")

            invoice_link = await page.query_selector(
                'a[href*="invoice"], a:has-text("View invoice")'
            )
            if not invoice_link:
                raise DownloadError(
                    f"No invoice link found for order {invoice.invoice_id}"
                )

            href = await invoice_link.get_attribute("href")
            if href and not href.startswith("http"):
                href = f"{BASE_URL}{href}"

            new_page = await page.context.new_page()
            try:
                await new_page.goto(href, wait_until="networkidle")
                pdf_bytes = await new_page.pdf(
                    format="Letter",
                    margin={
                        "top": "0.5in",
                        "right": "0.5in",
                        "bottom": "0.5in",
                        "left": "0.5in",
                    },
                )
                return pdf_bytes
            finally:
                await new_page.close()

        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Amazon.com download failed for {invoice.invoice_id}: {exc}"
            ) from exc

    @staticmethod
    def _parse_amazon_date(text: str) -> date | None:
        """Parse date formats used on Amazon.com."""
        # "January 15, 2025" or "Jan 15, 2025"
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text.strip(), fmt).date()
            except ValueError:
                continue
        return None
