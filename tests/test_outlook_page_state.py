import unittest

from outlook_page_state import (
    classify_outlook_page,
    is_authenticated,
    is_manual_verification,
)


class FakeLocator:
    def __init__(self, visible=False):
        self.visible = visible

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.visible else 0

    def is_visible(self):
        return self.visible

    def inner_text(self, timeout=None):
        return ""


class BodyLocator(FakeLocator):
    def __init__(self, body):
        super().__init__(visible=True)
        self.body = body

    def inner_text(self, timeout=None):
        return self.body


class FakePage:
    def __init__(self, url, body="", visible_selectors=()):
        self.url = url
        self.body = body
        self.visible_selectors = set(visible_selectors)

    def locator(self, selector):
        if selector == "body":
            return BodyLocator(self.body)
        return FakeLocator(selector in self.visible_selectors)


class OutlookPageStateTests(unittest.TestCase):
    def test_authenticated_url_is_positive_login_evidence(self):
        state = classify_outlook_page(
            FakePage("https://outlook.live.com/mail/0/inbox")
        )

        self.assertEqual(state.name, "logged_in")
        self.assertTrue(is_authenticated(state))

    def test_visible_px_frame_wins_over_mail_url(self):
        state = classify_outlook_page(
            FakePage(
                "https://outlook.live.com/mail/0/",
                visible_selectors=('iframe[src*="hsprotect.net"]',),
            )
        )

        self.assertEqual(state.name, "px_challenge")
        self.assertTrue(is_manual_verification(state))
        self.assertIn("hsprotect", state.evidence)

    def test_dom_login_fields_are_used_when_copy_is_localized(self):
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/login.srf",
                visible_selectors=('input[type="email"]',),
            )
        )

        self.assertEqual(state.name, "email_form")
        self.assertIn("input[type=\"email\"]", state.evidence)

    def test_security_text_requires_manual_verification(self):
        state = classify_outlook_page(
            FakePage("https://login.live.com/login.srf", "Verify you are human")
        )

        self.assertEqual(state.name, "verify_needed")
        self.assertTrue(is_manual_verification(state))


if __name__ == "__main__":
    unittest.main()
