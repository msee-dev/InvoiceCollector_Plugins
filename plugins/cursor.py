"""Cursor IDE billing invoice collector plugin (Stripe-based).

Auth flow: cursor.com/login redirects through WorkOS AuthKit to
authenticator.cursor.sh where the actual email/password form lives.
After auth, we land back on cursor.com and can reach Stripe billing.
"""

import pyotp
import structlog
from playwright.async_api import Page

from src.plugin_base import AuthenticationError, StripeProviderPlugin

logger = structlog.get_logger()


class CursorPlugin(StripeProviderPlugin):
    """Collects invoices from Cursor via Stripe's billing portal."""

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def login_url(self) -> str:
        # Use cursor.com/login which bootstraps the full WorkOS auth flow
        return "https://www.cursor.com/login"

    @property
    def billing_portal_url(self) -> str:
        return "https://www.cursor.com/settings"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Cursor via WorkOS AuthKit with email/password and optional TOTP."""
        try:
            # The login URL redirects through WorkOS to authenticator.cursor.sh
            # Wait for the auth page to fully load (may take a few redirects)
            await page.wait_for_load_state("networkidle")
            logger.debug("cursor_auth_page", url=page.url)

            # Wait for email input — WorkOS AuthKit uses various input patterns
            email_selector = (
                'input[type="email"], '
                'input[name="email"], '
                'input[autocomplete="email"], '
                'input[autocomplete="username"], '
                'input[name="username"], '
                'input[placeholder*="email" i], '
                'input[placeholder*="Email" i]'
            )
            await page.wait_for_selector(email_selector, timeout=30000)
            await page.fill(email_selector, credentials["email"])
            logger.debug("cursor_email_filled")

            # Click continue/submit
            await page.click(
                'button[type="submit"], '
                'button:has-text("Continue"), '
                'button:has-text("Sign in"), '
                'button:has-text("Log in"), '
                'button[data-testid="submit"]'
            )
            await page.wait_for_load_state("networkidle")

            # Password step — may be on same page or a new page
            password_selector = 'input[type="password"]'
            try:
                await page.wait_for_selector(password_selector, timeout=15000)
                await page.fill(password_selector, credentials["password"])
                logger.debug("cursor_password_filled")

                await page.click(
                    'button[type="submit"], '
                    'button:has-text("Continue"), '
                    'button:has-text("Sign in"), '
                    'button:has-text("Log in")'
                )
                await page.wait_for_load_state("networkidle")
            except Exception:
                # Some flows combine email+password on one page
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
                    # TOTP not prompted — might not be required
                    logger.debug("cursor_no_totp_prompt")

            # Verify we landed on cursor.com (allow time for final redirect)
            await page.wait_for_url("**cursor.com/**", timeout=30000)
            logger.debug("cursor_auth_complete", url=page.url)

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Cursor login failed: {exc}") from exc
