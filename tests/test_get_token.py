import unittest
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

from get_token import (
    OAuthRecoveryChallengeError,
    _try_get_access_token,
    get_proxy,
    get_access_token,
    handle_oauth2_form,
)


class FakeLocator:
    def __init__(self, page, role=None):
        self.page = page
        self.role = role

    @property
    def first(self):
        return self

    def count(self):
        return int(self.role is not None)

    def is_visible(self):
        return self.role is not None

    def fill(self, value, timeout=None):
        self.page.fills.append((self.role, value))

    def click(self, timeout=None):
        if self.role == "submit":
            self.page.state = (
                "proof_code" if self.page.state == "proof_email" else "consent"
            )
        elif self.role == "consent":
            self.page.state = "done"


class FakeKeyboard:
    def press(self, key):
        raise AssertionError(f"unexpected keyboard submit: {key}")


class FakeOAuthRecoveryPage:
    def __init__(self):
        self.state = "proof_email"
        self.fills = []
        self.keyboard = FakeKeyboard()

    def locator(self, selector):
        role = None
        if self.state == "proof_email" and selector == "#proof-confirmation-email-input":
            role = "proof_email"
        elif self.state == "proof_code" and selector == "#proof-confirmation-code-input":
            role = "proof_code"
        elif self.state in {"proof_email", "proof_code"} and selector == 'button[type="submit"]':
            role = "submit"
        elif self.state == "consent" and selector == '[data-testid="appConsentPrimaryButton"]':
            role = "consent"
        return FakeLocator(self, role)

    def wait_for_timeout(self, milliseconds):
        pass


class OAuthProxyTests(unittest.TestCase):
    def test_static_proxy_is_used_for_both_protocols(self):
        proxy = "http://127.0.0.1:7890"
        self.assertEqual(get_proxy(proxy), {"http": proxy, "https": proxy})

    def test_missing_proxy_disables_environment_proxy_fallback(self):
        self.assertEqual(get_proxy(), {"http": None, "https": None})

    @patch("get_token._try_get_access_token", return_value=("refresh", "access", 123))
    def test_page_delay_is_forwarded_to_each_token_attempt(self, try_get_access_token):
        recovery_handler = object()

        result = get_access_token(
            object(),
            "user",
            recovery_challenge_handler=recovery_handler,
            page_delay_ms=1500,
        )

        self.assertEqual(result, ("refresh", "access", 123))
        self.assertEqual(
            try_get_access_token.call_args.kwargs["page_delay_ms"],
            1500,
        )
        self.assertIs(
            try_get_access_token.call_args.kwargs["recovery_challenge_handler"],
            recovery_handler,
        )

    def test_page_delay_is_applied_after_navigation_and_before_token_request(self):
        page = MagicMock()
        page.on.side_effect = lambda event, callback: callback(
            SimpleNamespace(
                url="http://localhost:8000/token-tool/callback?code=auth-code"
            )
        )
        response = MagicMock()
        response.content = b"token-response"
        response.json.return_value = {
            "refresh_token": "refresh",
            "access_token": "access",
            "expires_in": 3600,
        }
        session = MagicMock()
        session.post.return_value = response

        config = {
            "email_suffix": "@outlook.com",
            "oauth2": {
                "Scopes": ["offline_access"],
                "client_id": "client-id",
                "redirect_url": "http://localhost:8000/token-tool/callback",
            },
        }
        with patch("builtins.open", mock_open(read_data=json.dumps(config))), patch(
            "get_token.handle_oauth2_form"
        ), patch(
            "get_token.requests.Session", return_value=session
        ):
            result = _try_get_access_token(
                page,
                "user",
                attempt=0,
                page_delay_ms=1500,
            )

        self.assertEqual(result[:2], ("refresh", "access"))
        delays = [call.args[0] for call in page.wait_for_timeout.call_args_list]
        self.assertGreaterEqual(delays.count(1500), 2)


class OAuthRecoveryFormTests(unittest.TestCase):
    def test_recovery_challenge_is_delegated_to_shared_handler(self):
        page = FakeOAuthRecoveryPage()
        handled_pages = []

        def handle_recovery_challenge(challenge_page):
            handled_pages.append(challenge_page)
            challenge_page.state = "consent"
            return True

        handle_oauth2_form(
            page,
            "user@outlook.com",
            "password",
            attempt=0,
            recovery_challenge_handler=handle_recovery_challenge,
        )

        self.assertEqual(handled_pages, [page])
        self.assertEqual(page.fills, [])
        self.assertEqual(page.state, "done")

    def test_recovery_challenge_without_saved_email_fails_without_submitting(self):
        page = FakeOAuthRecoveryPage()
        with patch("get_token.save_oauth_diagnostic"):
            with self.assertRaises(OAuthRecoveryChallengeError):
                handle_oauth2_form(
                    page,
                    "user@outlook.com",
                    "password",
                    attempt=0,
                )

        self.assertEqual(page.fills, [])
        self.assertEqual(page.state, "proof_email")


if __name__ == "__main__":
    unittest.main()
