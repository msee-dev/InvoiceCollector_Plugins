"""Cursor IDE billing invoice collector plugin (Stripe-based).

Auth flow: cursor.com/login redirects through WorkOS AuthKit to
authenticator.cursor.sh. Cursor uses Cloudflare Turnstile CAPTCHAs
that block automated password login.

Strategy:
1. Try password login first
2. If CAPTCHA blocks it, fall back to email magic code flow
3. In headed mode, user enters the code from their email
4. After first success, cookies are saved to skip auth entirely
"""

import pyotp
import structlog
from playwright.async_api import Page

from src.plugin_base import AuthenticationError, StripeProviderPlugin

logger = structlog.get_logger()

_MAGIC_CODE_TIMEOUT = 180_000  # 3 minutes for user to check email and enter code


class CursorPlugin(StripeProviderPlugin):
    """Collects invoices from Cursor via Stripe's billing portal."""

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def login_url(self) -> str:
        return "https://www.cursor.com/login"

    @property
    def billing_portal_url(self) -> str:
        return "https://www.cursor.com/settings"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Cursor. Supports email/password, OAuth (Google/GitHub/Apple), and magic code."""
        try:
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(3000)
            logger.debug("cursor_auth_page", url=page.url)

            # OAuth sign-in (Google, GitHub, Apple — Cursor doesn't support Microsoft)
            login_method = credentials.get("login_method", "email")
            if login_method != "email":
                from src.oauth import handle_oauth_login, detect_supported_methods
                supported = await detect_supported_methods(page)
                if login_method not in supported:
                    raise AuthenticationError(
                        f"Cursor does not support '{login_method}' sign-in. "
                        f"Supported methods: {', '.join(supported)}"
                    )
                await handle_oauth_login(
                    page, credentials,
                    expected_url_pattern="**cursor.com/**",
                )
                await self._wait_for_cursor_redirect(page)
                return

            # Email selector — broad for localized pages
            email_selector = (
                'input[type="email"], '
                'input[name="email"], '
                'input[autocomplete="email"], '
                'input[autocomplete="username"], '
                'input[name="username"], '
                'input[placeholder*="email" i], '
                'input[placeholder*="e-mail" i], '
                'input[placeholder*="mail" i]'
            )

            # Wait for email field (might be behind a CAPTCHA)
            email_visible = await page.query_selector(email_selector)
            if not email_visible:
                is_headless = not await page.evaluate("() => !!window.outerWidth && window.outerWidth > 0")
                if is_headless:
                    raise AuthenticationError(
                        "Cursor login blocked by CAPTCHA. Use debug mode (headed browser)."
                    )
                logger.info("cursor_waiting_for_form", message="Waiting for login form (solve CAPTCHA if shown)...")
                await page.wait_for_selector(email_selector, timeout=120_000)

            # Fill email
            await page.fill(email_selector, credentials["email"])
            logger.debug("cursor_email_filled")

            # Try password login first
            login_success = await self._try_password_login(page, credentials)

            if not login_success:
                # Password login failed (CAPTCHA blocked it) — try magic code
                logger.info("cursor_trying_magic_code", message="Password login failed, trying email code flow...")
                await self._try_magic_code_login(page, credentials)

            # Handle TOTP if needed
            if credentials.get("totp_secret"):
                await self._handle_totp(page, credentials)

            # Wait for redirect to cursor.com
            await self._wait_for_cursor_redirect(page)
            logger.debug("cursor_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Cursor login failed: {exc}") from exc

    async def _try_password_login(self, page: Page, credentials: dict) -> bool:
        """Try email + password login. Returns True if we got past it."""
        submit_selector = (
            'button[type="submit"], '
            'button:has-text("Continue"), '
            'button:has-text("Weiter"), '
            'button:has-text("Anmelden"), '
            'button:has-text("Sign in"), '
            'button:has-text("Log in")'
        )

        # Click submit after email
        try:
            await page.click(submit_selector)
            await page.wait_for_timeout(3000)
        except Exception:
            return False

        # Fill password if a password field appears
        try:
            await page.wait_for_selector('input[type="password"]', timeout=10000)
            await page.fill('input[type="password"]', credentials["password"])
            logger.debug("cursor_password_filled")
            await page.click(submit_selector)
            await page.wait_for_timeout(5000)
        except Exception:
            logger.debug("cursor_no_password_step")
            return False

        # Check if we got through or hit a CAPTCHA error
        page_text = await page.text_content("body") or ""
        error_phrases = ["can't verify", "nicht verifizieren", "try again", "erneut versuchen", "unable to verify"]
        if any(phrase in page_text.lower() for phrase in error_phrases):
            logger.info("cursor_password_blocked", message="CAPTCHA verification error detected")
            return False

        # If we're on cursor.com already, password login worked
        if "cursor.com" in page.url and "authenticator" not in page.url:
            return True

        # If still on auth page, check if it's a CAPTCHA
        captcha_phrases = ["mensch", "human", "verify", "verifizieren", "bestätigen"]
        if any(phrase in page_text.lower() for phrase in captcha_phrases):
            return False

        # Might still be processing — give it a moment
        try:
            await page.wait_for_url("**cursor.com/**", timeout=10000)
            return True
        except Exception:
            return False

    async def _try_magic_code_login(self, page: Page, credentials: dict) -> None:
        """Use the email magic code login flow."""
        is_headless = not await page.evaluate("() => !!window.outerWidth && window.outerWidth > 0")
        if is_headless:
            raise AuthenticationError(
                "Cursor CAPTCHA blocked password login. Use debug mode (headed browser) "
                "to log in with the email code flow."
            )

        # Navigate back to email step if needed
        back_btn = await page.query_selector(
            'button:has-text("Back"), button:has-text("Zurück"), '
            'a:has-text("Back"), a:has-text("Zurück")'
        )
        if back_btn:
            await back_btn.click()
            await page.wait_for_timeout(2000)

        # Re-fill email if cleared
        email_selector = (
            'input[type="email"], input[name="email"], '
            'input[autocomplete="email"], input[placeholder*="mail" i]'
        )
        email_field = await page.query_selector(email_selector)
        if email_field:
            current = await email_field.input_value()
            if not current:
                await page.fill(email_selector, credentials["email"])

        # Click the email code / magic link button
        magic_code_btn = await page.query_selector(
            'button:has-text("Email login code"), '
            'button:has-text("E-Mail-Anmeldecode"), '
            'button:has-text("email code"), '
            'button:has-text("magic"), '
            'button:has-text("Anmeldecode"), '
            'a:has-text("Email login code"), '
            'a:has-text("E-Mail-Anmeldecode")'
        )

        if not magic_code_btn:
            # Try submitting email first to reach the page with the magic code option
            submit_selector = 'button[type="submit"], button:has-text("Weiter"), button:has-text("Continue")'
            try:
                await page.click(submit_selector)
                await page.wait_for_timeout(3000)
                magic_code_btn = await page.query_selector(
                    'button:has-text("Email login code"), '
                    'button:has-text("E-Mail-Anmeldecode"), '
                    'button:has-text("Anmeldecode")'
                )
            except Exception:
                pass

        if not magic_code_btn:
            raise AuthenticationError(
                "Could not find the email code login option. "
                "Please log in manually in the browser window."
            )

        await magic_code_btn.click()
        logger.info("cursor_magic_code_sent", message="Email code sent — check your inbox and enter the code in the browser")
        await page.wait_for_timeout(2000)

        # Wait for the user to enter the code and complete login
        # The page will redirect to cursor.com once the code is accepted
        try:
            await page.wait_for_url("**cursor.com/**", timeout=_MAGIC_CODE_TIMEOUT)
        except Exception:
            raise AuthenticationError(
                "Timed out waiting for email code login. "
                "Check your email for the code and enter it in the browser window."
            )

    async def _handle_totp(self, page: Page, credentials: dict) -> None:
        """Handle TOTP 2FA if prompted."""
        totp_selector = (
            'input[name="totp"], input[name="code"], '
            'input[type="tel"], input[inputmode="numeric"], '
            'input[autocomplete="one-time-code"], '
            'input[name="otp"], input[placeholder*="code" i]'
        )
        try:
            await page.wait_for_selector(totp_selector, timeout=5000)
            totp = pyotp.TOTP(credentials["totp_secret"])
            await page.fill(totp_selector, totp.now())
            logger.debug("cursor_totp_filled")
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)
        except Exception:
            logger.debug("cursor_no_totp_prompt")

    async def _wait_for_cursor_redirect(self, page: Page) -> None:
        """Wait for redirect back to cursor.com after auth."""
        if "cursor.com" in page.url and "authenticator" not in page.url:
            return

        is_headless = not await page.evaluate("() => !!window.outerWidth && window.outerWidth > 0")
        if is_headless:
            raise AuthenticationError(
                "Still on auth page after login. Use debug mode (headed browser)."
            )

        logger.info("cursor_waiting_redirect", message="Waiting for redirect to cursor.com...")
        try:
            await page.wait_for_url("**cursor.com/**", timeout=_MAGIC_CODE_TIMEOUT)
        except Exception:
            raise AuthenticationError(
                "Timed out waiting for redirect to cursor.com. "
                "If a CAPTCHA or code prompt appeared, complete it in the browser."
            )
