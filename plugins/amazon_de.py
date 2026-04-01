"""Amazon.de order invoice collector plugin.

Downloads invoices from Amazon.de order history by rendering the
invoice HTML page to PDF via Playwright's print-to-PDF capability.
Handles German-language UI and date formats.
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

BASE_URL = "https://www.amazon.de"
ORDER_HISTORY_URL = f"{BASE_URL}/gp/css/order-history"

# German month names for date parsing
GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


class AmazonDePlugin(ProviderPlugin):
    """Collects invoices from Amazon.de order history."""

    @property
    def name(self) -> str:
        return "amazon_de"

    @property
    def login_url(self) -> str:
        return f"{BASE_URL}/gp/sign-in.html"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Amazon.de with email, password, and optional TOTP."""
        try:
            # Dismiss cookie consent banner if present
            await self._dismiss_cookie_consent(page)

            # Wait for either the email field or a CAPTCHA challenge
            await page.wait_for_selector(
                'input[name="email"], #ap_email, '
                '#captchacharacters, input[name="captchacharacters"], '
                '#auth-captcha-image, img[alt*="captcha" i], '
                '#cvf-page-content, [data-action="cvf"]',
                timeout=15000,
            )

            # Handle CAPTCHA / puzzle challenge
            await self._handle_captcha(page, credentials)

            # Email step — re-check after potential CAPTCHA resolution
            await page.wait_for_selector(
                'input[name="email"], #ap_email', timeout=15000
            )
            await page.fill(
                'input[name="email"], #ap_email', credentials["email"]
            )

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

            if "/ap/signin" in page.url:
                raise AuthenticationError("Still on sign-in page after login attempt")

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Amazon.de login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to Amazon.de order history."""
        try:
            await page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            if "/ap/signin" in page.url:
                raise NavigationError("Redirected to login — session expired")

        except NavigationError:
            raise
        except Exception as exc:
            raise NavigationError(
                f"Amazon.de order history navigation failed: {exc}"
            ) from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse orders from Amazon.de order history page."""
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
        date_el = await card.query_selector(
            '.order-info .a-color-secondary, '
            '[data-testid="order-date"], '
            '.value:first-of-type'
        )
        if not date_el:
            return None

        date_text = (await date_el.text_content() or "").strip()
        # Remove "Bestellung aufgegeben am" prefix if present
        date_text = re.sub(
            r"^Bestellung\s+aufgegeben\s+am\s*",
            "", date_text, flags=re.IGNORECASE
        ).strip()
        invoice_date = self._parse_german_date(date_text)
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
            'a[href*="invoice"], a:has-text("Rechnung"), '
            'a:has-text("View invoice")'
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
            currency="EUR",
            download_url=download_url,
        )

    async def download_invoice(self, page: Page, invoice: InvoiceInfo) -> bytes:
        """Download invoice by rendering the invoice page as PDF."""
        try:
            if invoice.download_url:
                new_page = await page.context.new_page()
                try:
                    await new_page.goto(
                        invoice.download_url, wait_until="networkidle"
                    )
                    pdf_bytes = await new_page.pdf(
                        format="A4",
                        margin={
                            "top": "1cm",
                            "right": "1cm",
                            "bottom": "1cm",
                            "left": "1cm",
                        },
                    )
                    if pdf_bytes and len(pdf_bytes) > 0:
                        return pdf_bytes
                finally:
                    await new_page.close()

            # Fallback: navigate to order detail page
            detail_url = f"{BASE_URL}/gp/your-account/order-details?orderID={invoice.invoice_id}"
            await page.goto(detail_url, wait_until="networkidle")

            invoice_link = await page.query_selector(
                'a[href*="invoice"], a:has-text("Rechnung"), '
                'a:has-text("View invoice")'
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
                    format="A4",
                    margin={
                        "top": "1cm",
                        "right": "1cm",
                        "bottom": "1cm",
                        "left": "1cm",
                    },
                )
                return pdf_bytes
            finally:
                await new_page.close()

        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Amazon.de download failed for {invoice.invoice_id}: {exc}"
            ) from exc

    @staticmethod
    async def _handle_captcha(page: Page, credentials: dict) -> None:
        """Detect and handle CAPTCHA / puzzle challenges on Amazon."""
        captcha_selectors = (
            '#captchacharacters, input[name="captchacharacters"], '
            '#auth-captcha-image, img[alt*="captcha" i], '
            '#cvf-page-content, [data-action="cvf"]'
        )
        captcha = await page.query_selector(captcha_selectors)
        if not captcha:
            return

        # Check if we have a headed browser where user can solve it
        body_text = await page.text_content("body") or ""
        logger.warning(
            "captcha_detected",
            hint="Amazon is showing a CAPTCHA/puzzle challenge",
        )

        # Wait up to 120s for user to solve the CAPTCHA in headed mode
        # In headless mode this will timeout and raise an error
        try:
            await page.wait_for_selector(
                'input[name="email"], #ap_email',
                timeout=120000,
            )
            logger.info("captcha_resolved", hint="User solved CAPTCHA")
        except Exception:
            raise AuthenticationError(
                "Amazon.de blocked by CAPTCHA/puzzle challenge — "
                "run with debug mode (headed browser) to solve it manually"
            )

    @staticmethod
    async def _dismiss_cookie_consent(page: Page) -> None:
        """Dismiss the Amazon cookie consent banner if present."""
        try:
            consent_btn = await page.query_selector(
                '#sp-cc-accept, '
                'input[name="accept"], '
                '[data-action="sp-cc"][data-action-type="ACCEPT"], '
                'button:has-text("Alle akzeptieren"), '
                'button:has-text("Accept all"), '
                'button:has-text("Accepter tout")'
            )
            if consent_btn:
                await consent_btn.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass  # Not critical — proceed with login

    @staticmethod
    def _parse_german_date(text: str) -> date | None:
        """Parse date formats used on Amazon.de.

        Handles:
        - "15. Januar 2025"
        - "15. Jan. 2025"
        - "15.01.2025"
        - Standard ISO format
        """
        text = text.strip().rstrip(".")

        # "15. Januar 2025" or "15. Jan 2025"
        match = re.match(
            r"(\d{1,2})\.\s*(\w+)\.?\s+(\d{4})", text
        )
        if match:
            day = int(match.group(1))
            month_str = match.group(2).lower()
            year = int(match.group(3))
            month = GERMAN_MONTHS.get(month_str)
            if month:
                try:
                    return date(year, month, day)
                except ValueError:
                    pass

        # "15.01.2025" (DD.MM.YYYY)
        match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if match:
            try:
                return date(
                    int(match.group(3)),
                    int(match.group(2)),
                    int(match.group(1)),
                )
            except ValueError:
                pass

        # ISO format fallback
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            pass

        return None
