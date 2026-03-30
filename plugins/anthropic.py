"""Anthropic Console billing invoice collector plugin (Stripe-based)."""

import pyotp
from playwright.async_api import Page

from src.plugin_base import AuthenticationError, StripeProviderPlugin


class AnthropicPlugin(StripeProviderPlugin):
    """Collects invoices from Anthropic Console via Stripe's billing portal.

    Auth flow: authenticate on console.anthropic.com, then navigate
    to the billing/plans page which links to Stripe's billing portal.
    """

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def login_url(self) -> str:
        return "https://console.anthropic.com/login"

    @property
    def billing_portal_url(self) -> str:
        return "https://console.anthropic.com/settings/billing"

    async def authenticate(self, page: Page, credentials: dict) -> None:
        """Log in to Anthropic Console with email/password and optional TOTP.

        Anthropic uses a standard email + password form, followed by
        optional 2FA verification.
        """
        try:
            # Email step — Anthropic may show email first or combined form
            await page.wait_for_selector(
                'input[type="email"], input[name="email"], '
                'input[placeholder*="email" i]',
                timeout=15000,
            )
            await page.fill(
                'input[type="email"], input[name="email"], '
                'input[placeholder*="email" i]',
                credentials["email"],
            )

            # Check if password is on the same page or needs a "Continue" click
            password_input = await page.query_selector(
                'input[type="password"]'
            )
            if not password_input:
                await page.click(
                    'button[type="submit"], button:has-text("Continue"), '
                    'button:has-text("Next")'
                )
                await page.wait_for_load_state("networkidle")
                await page.wait_for_selector(
                    'input[type="password"]', timeout=15000
                )

            await page.fill('input[type="password"]', credentials["password"])
            await page.click(
                'button[type="submit"], button:has-text("Sign in"), '
                'button:has-text("Log in"), button:has-text("Continue")'
            )
            await page.wait_for_load_state("networkidle")

            # TOTP if configured
            if credentials.get("totp_secret"):
                totp_input = await page.query_selector(
                    'input[name="totp"], input[name="code"], '
                    'input[inputmode="numeric"], input[type="tel"]'
                )
                if totp_input:
                    totp = pyotp.TOTP(credentials["totp_secret"])
                    await totp_input.fill(totp.now())
                    await page.click('button[type="submit"]')
                    await page.wait_for_load_state("networkidle")

            # Verify we're on the console
            await page.wait_for_url(
                "**/console.anthropic.com/**", timeout=15000
            )

        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError(f"Anthropic login failed: {exc}") from exc
