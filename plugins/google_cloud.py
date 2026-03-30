"""Google Cloud Console billing invoice collector plugin.

Requires stealth mode: Google actively blocks automated browsers from their
sign-in flow. The orchestrator applies playwright-stealth patches when
requires_stealth is True.
"""

import random
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


class GoogleCloudPlugin(ProviderPlugin):
    """Collects invoices from Google Cloud Console Billing."""

    @property
    def name(self) -> str:
        return "google_cloud"

    @property
    def login_url(self) -> str:
        return "https://accounts.google.com/signin"

    @property
    def requires_stealth(self) -> bool:
        return True

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Google with email, password, and optional TOTP.

        Uses human-like delays between actions to reduce bot detection risk.
        """
        try:
            # Email step
            await page.wait_for_selector(
                'input[type="email"], input#identifierId', timeout=15000
            )
            await _human_delay(page, 500, 1000)
            await page.fill(
                'input[type="email"], input#identifierId', credentials["email"]
            )
            await _human_delay(page, 300, 800)
            await page.click(
                'button#identifierNext, '
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
            if "challenge" in page.url:
                other_btn = await page.query_selector(
                    'button:has-text("Andere Option"), '
                    'button:has-text("Try another way"), '
                    'button:has-text("Essayer autrement"), '
                    '[data-action="selectChallenge"]'
                )
                if other_btn:
                    await other_btn.click()
                    await _human_delay(page, 2000, 3000)

                pw_option = await page.query_selector(
                    '[data-challengetype="1"], '
                    'li:has-text("Passwort eingeben"), '
                    'li:has-text("Enter your password"), '
                    'li:has-text("Saisissez votre mot de passe")'
                )
                if pw_option:
                    await pw_option.click()
                    await _human_delay(page, 2000, 3000)

            # Password step
            await page.wait_for_selector(
                'input[type="password"], input[name="Passwd"]', timeout=15000
            )
            await _human_delay(page, 500, 1000)
            await page.fill(
                'input[type="password"], input[name="Passwd"]',
                credentials["password"],
            )
            await _human_delay(page, 300, 800)
            await page.click(
                'button#passwordNext, '
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

            # Handle TOTP / 2FA challenge if prompted
            totp_sel = (
                'input[name="totpPin"], input#totpPin, '
                'input[type="tel"][aria-label], '
                'input[inputmode="numeric"], input[autocomplete="one-time-code"]'
            )

            if "challenge" in page.url:
                # If on passkey 2FA page, click "Choose another option" first
                if "challenge/pk" in page.url or "challenge/ipp" in page.url:
                    other_btn = await page.query_selector(
                        'button:has-text("Andere Option"), '
                        'button:has-text("Try another way"), '
                        'a:has-text("Andere Option"), '
                        'a:has-text("Try another way")'
                    )
                    if other_btn:
                        await other_btn.click()
                        await _human_delay(page, 2000, 3000)

                if "challenge/selection" in page.url:
                    totp_option = await page.query_selector(
                        '[data-challengetype="6"], '
                        'li:has-text("Google Authenticator"), '
                        'li:has-text("Authenticator"), '
                        'li:has-text("Bestätigung in zwei Schritten")'
                    )
                    if totp_option:
                        await totp_option.click()
                        await _human_delay(page, 2000, 3000)

                try:
                    await page.wait_for_selector(totp_sel, timeout=10000)
                except Exception:
                    pass

            totp_input = await page.query_selector(totp_sel)
            if totp_input:
                if credentials.get("totp_secret"):
                    secret = credentials["totp_secret"].replace(" ", "").replace("-", "").strip().upper()
                    code = pyotp.TOTP(secret).now()
                elif credentials.get("_totp_callback"):
                    code = await credentials["_totp_callback"]()
                else:
                    raise AuthenticationError(
                        "Google requires 2FA but no TOTP secret or callback configured. "
                        "Set GOOGLE_CLOUD_TOTP_SECRET in .env or use the web UI."
                    )

                await _human_delay(page, 500, 1000)
                await totp_input.fill(code)
                await _human_delay(page, 300, 800)
                await page.click(
                    'button#totpNext, '
                    'button:has-text("Next"), '
                    'button:has-text("Weiter"), '
                    'button:has-text("Suivant")'
                )
                await _human_delay(page, 2000, 4000)

            # Wait for redirect to Google services
            await page.wait_for_load_state("domcontentloaded")

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Google login failed: {exc}") from exc

    async def navigate_to_invoices(self, page: Page) -> None:
        """Navigate to the Google Cloud billing documents page."""
        try:
            await page.goto(
                "https://console.cloud.google.com/billing",
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)

            # Look for and click into "Transactions" or "Documents" tab
            docs_link = await page.query_selector(
                'a[href*="documents"], a[href*="transactions"], '
                'a:has-text("Transactions"), a:has-text("Documents"), '
                '[data-testid*="transaction"], [data-testid*="document"]'
            )
            if docs_link:
                await docs_link.click()
                await page.wait_for_timeout(3000)

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

                # Find invoice ID
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
            "%b %d, %Y",
            "%B %d, %Y",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%d %b %Y",
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
