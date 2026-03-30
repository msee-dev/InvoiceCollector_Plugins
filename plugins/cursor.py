"""Cursor IDE billing invoice collector plugin (Stripe-based).

Auth flow: cursor.com/login redirects through WorkOS AuthKit to
authenticator.cursor.sh where the actual email/password form lives.
After auth, we land back on cursor.com and can reach Stripe billing.

NOTE: Cursor uses a Cloudflare Turnstile CAPTCHA on the login page.
On the first run, use debug/headed mode to solve it manually.
After that, saved cookies will bypass the CAPTCHA on subsequent runs.
"""

import pyotp
import structlog
from playwright.async_api import Page

from src.plugin_base import AuthenticationError, StripeProviderPlugin

logger = structlog.get_logger()

# Max time to wait for user to solve CAPTCHA in headed mode
_CAPTCHA_TIMEOUT = 120_000  # 2 minutes


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
        """Log in to Cursor via WorkOS AuthKit with email/password and optional TOTP.

        Handles Cloudflare Turnstile CAPTCHA: in headed mode, waits for the
        user to solve it. In headless mode, raises a clear error.
        """
        try:
            await page.wait_for_load_state("networkidle")
            logger.debug("cursor_auth_page", url=page.url)

            # Check for Cloudflare Turnstile CAPTCHA
            captcha = await page.query_selector(
                'iframe[src*="challenges.cloudflare.com"], '
                '#cf-turnstile, '
                '.cf-turnstile, '
                '[data-sitekey]'
            )

            # Broad email selector — handles English and localized pages
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

            if captcha:
                is_headless = await page.evaluate("() => !window.outerWidth")
                if is_headless:
                    raise AuthenticationError(
                        "Cursor login has a Cloudflare CAPTCHA. "
                        "Run with debug mode enabled (headed browser) to solve it manually. "
                        "After the first successful login, cookies will be saved to skip it next time."
                    )

                logger.info("cursor_captcha_detected", message="Waiting for user to solve CAPTCHA...")
                try:
                    await page.wait_for_selector(email_selector, timeout=_CAPTCHA_TIMEOUT)
                    logger.info("cursor_captcha_solved")
                except Exception:
                    raise AuthenticationError(
                        "Timed out waiting for CAPTCHA to be solved. "
                        "Please solve the Cloudflare challenge in the browser window."
                    )
            else:
                await page.wait_for_selector(email_selector, timeout=30000)

            # Fill email
            await page.fill(email_selector, credentials["email"])
            logger.debug("cursor_email_filled")

            # Click continue/submit — handles localized button text
            submit_selector = (
                'button[type="submit"], '
                'button:has-text("Continue"), '
                'button:has-text("Weiter"), '
                'button:has-text("Sign in"), '
                'button:has-text("Log in"), '
                'button:has-text("Anmelden"), '
                'button[data-testid="submit"]'
            )
            await page.click(submit_selector)
            await page.wait_for_load_state("networkidle")

            # Password step
            password_selector = 'input[type="password"]'
            try:
                await page.wait_for_selector(password_selector, timeout=15000)
                await page.fill(password_selector, credentials["password"])
                logger.debug("cursor_password_filled")

                await page.click(submit_selector)
                await page.wait_for_load_state("networkidle")
            except Exception:
                logger.debug("cursor_no_separate_password_step")

            # TOTP if configured
            if credentials.get("totp_secret"):
                totp_selector = (
                    'input[name="totp"], input[name="code"], '
                    'input[type="tel"], input[inputmode="numeric"], '
                    'input[autocomplete="one-time-code"], '
                    'input[name="otp"], input[placeholder*="code" i]'
                )
                try:
                    await page.wait_for_selector(totp_selector, timeout=10000)
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await page.fill(totp_selector, totp.now())
                    logger.debug("cursor_totp_filled")
                    await page.click('button[type="submit"]')
                    await page.wait_for_load_state("networkidle")
                except Exception:
                    logger.debug("cursor_no_totp_prompt")

            # Verify we landed on cursor.com
            await page.wait_for_url("**cursor.com/**", timeout=30000)
            logger.debug("cursor_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Cursor login failed: {exc}") from exc
