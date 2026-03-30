"""Google Workspace/Services billing invoice collector.

Covers: YouTube Premium, Gemini, Google One, Google Workspace,
Google Domains, and other Google services billed through pay.google.com.

Auth: Google account login (same as google_cloud but different billing page).
Billing page: https://pay.google.com/gp/w/u/0/home/activity

Requires stealth mode: Google actively blocks automated browsers from their
sign-in flow. The orchestrator applies playwright-stealth patches when
requires_stealth is True.
"""

import random
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

# Google sign-in block page indicators (language-agnostic)
_BLOCK_INDICATORS = [
    "accounts.google.com/v3/signin/rejected",
    "accounts.google.com/signin/v2/deniedsignin",
    "identifier?dsh=",
]


async def _human_delay(page: Page, min_ms: int = 300, max_ms: int = 1200) -> None:
    """Wait a random human-like interval between actions."""
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def _detect_sign_in_block(page: Page) -> bool:
    """Check if Google blocked the sign-in as 'insecure browser'."""
    url = page.url
    if any(indicator in url for indicator in _BLOCK_INDICATORS):
        return True
    # Check page content for the block message (multilanguage)
    body = await page.text_content("body") or ""
    block_phrases = [
        "this browser or app may not be secure",
        "dieser browser oder diese app ist möglicherweise nicht sicher",
        "anmeldung nicht möglich",
        "couldn't sign you in",
        "ce navigateur ou cette application n'est peut-être pas sécurisé",
    ]
    body_lower = body.lower()
    return any(phrase in body_lower for phrase in block_phrases)


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
        return ["email"]

    @property
    def requires_stealth(self) -> bool:
        return True

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Google account with human-like timing."""
        try:
            # Email step
            email_sel = 'input[type="email"], input[name="identifier"], #identifierId'
            await page.wait_for_selector(email_sel, timeout=15000)
            await _human_delay(page, 500, 1000)
            await page.fill(email_sel, credentials["email"])
            await _human_delay(page, 300, 800)

            # Click Next (multilanguage)
            await page.click(
                '#identifierNext, '
                'button:has-text("Next"), '
                'button:has-text("Weiter"), '
                'button:has-text("Suivant"), '
                'button:has-text("Siguiente"), '
                'button:has-text("Avanti")'
            )
            await _human_delay(page, 2000, 4000)

            # Check for sign-in block after email step
            if await _detect_sign_in_block(page):
                raise AuthenticationError(
                    "Google blocked automated sign-in. "
                    "Try running with --headed to complete login manually, "
                    "then subsequent runs can reuse the saved session."
                )

            # Handle passkey/security key challenge — click "Try another way"
            # to fall back to password entry
            if "challenge" in page.url:
                logger.debug("google_passkey_challenge", url=page.url)

                # Click "Choose another option" / "Andere Option wählen"
                other_btn = await page.query_selector(
                    'button:has-text("Andere Option"), '
                    'button:has-text("Try another way"), '
                    'button:has-text("Essayer autrement"), '
                    'button:has-text("Otra opción"), '
                    '[data-action="selectChallenge"]'
                )
                if other_btn:
                    await other_btn.click()
                    await _human_delay(page, 2000, 3000)

                # Select "Enter your password" (data-challengetype="1")
                pw_option = await page.query_selector(
                    '[data-challengetype="1"], '
                    'li:has-text("Passwort eingeben"), '
                    'li:has-text("Enter your password"), '
                    'li:has-text("Saisissez votre mot de passe"), '
                    'li:has-text("Introduce tu contraseña")'
                )
                if pw_option:
                    await pw_option.click()
                    await _human_delay(page, 2000, 3000)

            # Password step
            pw_sel = 'input[type="password"], input[name="Passwd"]'
            await page.wait_for_selector(pw_sel, timeout=15000)
            await _human_delay(page, 500, 1000)
            await page.fill(pw_sel, credentials["password"])
            await _human_delay(page, 300, 800)

            await page.click(
                '#passwordNext, '
                'button:has-text("Next"), '
                'button:has-text("Weiter"), '
                'button:has-text("Suivant"), '
                'button:has-text("Siguiente"), '
                'button:has-text("Avanti")'
            )
            await _human_delay(page, 3000, 5000)

            # Check for sign-in block after password step
            if await _detect_sign_in_block(page):
                raise AuthenticationError(
                    "Google blocked automated sign-in after password entry. "
                    "Try running with --headed to complete login manually."
                )

            # Handle 2FA challenge if prompted after password
            await self._handle_2fa_challenge(page, credentials)

            logger.debug("google_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Google login failed: {exc}") from exc

    async def _handle_2fa_challenge(self, page: Page, credentials: dict) -> None:
        """Handle Google 2FA challenge after password entry.

        Supports: TOTP (Google Authenticator), phone prompt, SMS code.
        On challenge selection page, picks TOTP if available, otherwise
        prompts the user via _totp_callback for whatever code Google asks.
        """
        if "challenge" not in page.url:
            return

        code_input_sel = (
            'input[name="totpPin"], input#totpPin, '
            'input[type="tel"][aria-label], '
            'input[inputmode="numeric"], input[autocomplete="one-time-code"]'
        )
        selected_method = None

        # If on a passkey 2FA page, click "Choose another option" first
        if "challenge/pk" in page.url or "challenge/ipp" in page.url:
            logger.debug("google_2fa_passkey_page", url=page.url)
            other_btn = await page.query_selector(
                'button:has-text("Andere Option"), '
                'button:has-text("Try another way"), '
                'button:has-text("Essayer autrement"), '
                'a:has-text("Andere Option"), '
                'a:has-text("Try another way")'
            )
            if other_btn:
                await other_btn.click()
                await _human_delay(page, 2000, 3000)

        # If on challenge selection page, pick an automatable method
        if "challenge/selection" in page.url:
            logger.debug("google_2fa_selection", url=page.url)

            # Try methods in preference order:
            # ct=6  Google Authenticator / TOTP (auto-fill from secret)
            # ct=9  SMS code (prompt user)
            # ct=13 One-time security code (prompt user)
            # ct=39 Phone tap (wait for approval, no code needed)
            for sel, method in [
                ('[data-challengetype="6"]', "totp"),
                ('[data-challengetype="9"]', "sms"),
                ('[data-challengetype="13"]', "otp"),
                ('[data-challengetype="39"]', "phone_tap"),
            ]:
                option = await page.query_selector(sel)
                if option and await option.is_visible():
                    logger.debug("google_2fa_selected", method=method)
                    await option.click()
                    await _human_delay(page, 2000, 3000)
                    selected_method = method
                    break

        # Also detect method from the challenge URL
        if "challenge/totp" in page.url:
            selected_method = "totp"

        # Wait for a code input field to appear
        try:
            await page.wait_for_selector(code_input_sel, timeout=10000)
        except Exception:
            # No code input — might be phone tap or unsupported
            if selected_method == "phone_tap" or "challenge" in page.url:
                logger.info(
                    "google_2fa_waiting",
                    message="Waiting for 2FA approval (phone tap or code)...",
                )
                try:
                    await page.wait_for_selector(code_input_sel, timeout=30000)
                except Exception:
                    if "challenge" not in page.url:
                        return  # Got through via phone tap
                    logger.debug("google_2fa_no_input")
                    return
            else:
                return

        # Fill the code
        code_input = await page.query_selector(code_input_sel)
        if not code_input:
            return

        # Only auto-fill TOTP secret for Google Authenticator challenges
        if credentials.get("totp_secret") and selected_method == "totp":
            import pyotp
            secret = credentials["totp_secret"].replace(" ", "").replace("-", "").strip().upper()
            code = pyotp.TOTP(secret).now()
            logger.debug("google_totp_auto")
        elif credentials.get("_totp_callback"):
            logger.info("google_2fa_code_prompt", message="Requesting 2FA code from web UI...")
            code = await credentials["_totp_callback"]()
        else:
            raise AuthenticationError(
                "Google requires a 2FA code but no callback configured. "
                "Use the web UI, or add Google Authenticator to your Google account "
                "for automatic TOTP."
            )

        await _human_delay(page, 500, 1000)
        await page.fill(code_input_sel, code)
        await _human_delay(page, 300, 800)
        await page.click(
            '#totpNext, '
            'button:has-text("Next"), '
            'button:has-text("Weiter"), '
            'button:has-text("Suivant")'
        )
        await _human_delay(page, 2000, 4000)

    def _get_payments_frame(self, page: Page):
        """Find the payments.google.com iframe that contains transaction data."""
        for frame in page.frames:
            if "payments.google.com" in frame.url and "timelineview" in frame.url:
                return frame
        return None

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to Google Pay activity/transactions page."""
        try:
            await page.goto(
                "https://pay.google.com/gp/w/u/0/home/activity",
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(8000)

            # Verify the payments iframe loaded
            frame = self._get_payments_frame(page)
            if not frame:
                # Retry navigation
                await page.goto(
                    "https://pay.google.com/gp/w/u/0/home/activity",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(8000)
                frame = self._get_payments_frame(page)

            if not frame:
                raise NavigationError("Google Pay payments iframe not found")

            logger.debug("google_billing_page", url=page.url, iframe=frame.url[:80])
        except NavigationError:
            raise
        except Exception as exc:
            raise NavigationError(f"Google Pay navigation failed: {exc}") from exc

    async def get_invoice_list(self, page: Page) -> list[InvoiceInfo]:
        """Parse transactions from the Google Pay activity iframe."""
        invoices = []

        frame = self._get_payments_frame(page)
        if not frame:
            logger.warning("google_no_payments_iframe")
            return []

        # Wait for transaction rows to render inside the iframe
        row_sel = 'tr.b3id-widget-table-data-row.clickable, tr[role="row"][class*="clickable"]'
        try:
            await frame.wait_for_selector(row_sel, timeout=15000)
        except Exception:
            logger.warning("google_no_transaction_rows")
            return []

        rows = await frame.query_selector_all(row_sel)
        if not rows:
            logger.warning("google_no_transaction_rows")
            return []

        logger.debug("google_rows_found", count=len(rows))

        for idx, row in enumerate(rows):
            try:
                text = (await row.text_content() or "").strip()
                if not text or len(text) < 10:
                    continue

                # Extract date — formats: "Mar 29", "Mar 2, 2025", "Dec 2, 2024"
                date_match = re.search(
                    r"(\w{3}\s+\d{1,2},?\s+\d{4}|\w{3}\s+\d{1,2})",
                    text,
                )
                if not date_match:
                    continue

                date_str = date_match.group(1)
                invoice_date = self._parse_date(date_str)
                if not invoice_date:
                    continue

                # Extract amount (€, $, £)
                amount_match = re.search(
                    r'[-]?\s*[€$£]\s*[\d,.]+|[\d,.]+\s*(?:USD|EUR|GBP|CHF)',
                    text,
                )
                amount = amount_match.group(0).strip() if amount_match else None

                # Extract product name from "Service · Product Description"
                # Text looks like: "YouTubeMar 2 · YouTube Premium-€23.99"
                product_match = re.search(r'·\s*(.+?)(?:\s*[-]?\s*[€$£])', text)
                if product_match:
                    product = product_match.group(1).strip()
                else:
                    # Fallback: use the service name (first word)
                    service_match = re.match(r'([A-Za-z][\w\s]+?)(?:\s*[A-Z][a-z]{2}\s+\d)', text)
                    product = service_match.group(1).strip() if service_match else "Google"

                # Build filename from product: Product-YYYY-MM
                safe_product = re.sub(r'[^A-Za-z0-9]+', '_', product).strip('_')
                month_str = invoice_date.strftime("%Y-%m")
                filename_base = f"{safe_product}-{month_str}"

                # Temporary invoice_id (will be replaced with real transaction ID during download)
                invoice_id = filename_base

                # Encode row index and product in download_url for use during download
                invoices.append(
                    InvoiceInfo(
                        provider=self.name,
                        invoice_id=invoice_id,
                        invoice_date=invoice_date,
                        amount=amount,
                        download_url=f"{idx}|{filename_base}",
                    )
                )
            except Exception:
                continue

        logger.info("google_invoices_parsed", count=len(invoices))
        return invoices

    @staticmethod
    def _parse_date(text: str) -> date | None:
        """Parse various date formats from Google Pay."""
        text = text.strip().rstrip(",")
        # If no year, assume current year
        if not re.search(r'\d{4}', text):
            text = f"{text}, {date.today().year}"
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
        """Download a tax invoice from Google Pay by clicking the transaction row."""
        try:
            frame = self._get_payments_frame(page)
            if not frame:
                raise DownloadError("Google Pay payments iframe not found")

            # Parse row index and filename from download_url ("idx|filename_base")
            parts = (invoice.download_url or "").split("|", 1)
            row_idx = int(parts[0]) if parts[0].isdigit() else -1
            filename_base = parts[1] if len(parts) > 1 else invoice.invoice_id

            row_sel = 'tr.b3id-widget-table-data-row.clickable, tr[role="row"][class*="clickable"]'
            rows = await frame.query_selector_all(row_sel)

            if row_idx < 0 or row_idx >= len(rows):
                raise DownloadError(f"Transaction row {row_idx} not found")

            # Close any open detail panel first (prevents overlay interception)
            close_btn = await frame.query_selector(
                '[aria-label="Close"], [aria-label="Schließen"], '
                'button:has-text("close"), div.b3id-close-button, '
                '[data-tooltip="Close"], [data-tooltip="Schließen"]'
            )
            if close_btn:
                try:
                    await close_btn.click()
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass

            # Click the transaction row to open the detail panel
            await rows[row_idx].click()
            await page.wait_for_timeout(4000)

            # Extract the real transaction ID from the detail panel
            detail_text = await frame.evaluate('() => document.body.innerText')
            tx_match = re.search(
                r'(?:TRANSACTION ID|Transaktions-ID|ID de transaction)\s*\n?\s*([A-Z0-9][\w.-]+)',
                detail_text,
            )
            if tx_match:
                invoice.invoice_id = tx_match.group(1)
                logger.debug("google_transaction_id", id=invoice.invoice_id)

            # Use product name for the filename (stored in download_url)
            invoice.download_url = filename_base

            # The "Download tax invoice" button is a div[role="button"], not an <a>
            download_sel = (
                '[role="button"]:has-text("Download tax invoice"), '
                '[role="button"]:has-text("Download"), '
                '[role="button"]:has-text("Steuerrechnung"), '
                '[role="button"]:has-text("Rechnung herunterladen"), '
                '[role="button"]:has-text("Télécharger la facture"), '
                'a:has-text("Download tax invoice"), '
                'a:has-text("Steuerrechnung"), '
                'a[href*="invoice"], a[href*="receipt"]'
            )
            download_btn = await frame.query_selector(download_sel)

            if not download_btn:
                raise DownloadError(
                    f"No download button found for {invoice.invoice_id}. "
                    "This transaction may not have a tax invoice."
                )

            # Click the download button and intercept the file download
            async with page.expect_download(timeout=15000) as download_info:
                await download_btn.click()
            download = await download_info.value
            path = await download.path()
            if path:
                with open(path, "rb") as f:
                    return f.read()

            raise DownloadError(f"Download failed for {invoice.invoice_id}")
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError(
                f"Google Pay download failed for {invoice.invoice_id}: {exc}"
            ) from exc
