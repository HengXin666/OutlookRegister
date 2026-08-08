import unittest

from outlookregister.browser.outlook_page_state import (
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

    def test_recovery_email_form_is_not_treated_as_account_login(self):
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/proofs/add",
                "Enter an alternate email address",
                visible_selectors=("#proof-confirmation-email-input",),
            )
        )

        self.assertEqual(state.name, "recovery_email_form")
        self.assertIn("proof-confirmation-email-input", state.evidence)

    def test_recovery_code_selector_is_classified_as_security_code(self):
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/proofs/add",
                visible_selectors=("#proof-confirmation-code-input",),
            )
        )

        self.assertEqual(state.name, "sms_verify")
        self.assertIn("proof-confirmation-code-input", state.evidence)

    def test_security_text_requires_manual_verification(self):
        state = classify_outlook_page(
            FakePage("https://login.live.com/login.srf", "Verify you are human")
        )

        self.assertEqual(state.name, "verify_needed")
        self.assertTrue(is_manual_verification(state))

    def test_stay_signed_in_prompt_is_kmsi_not_email_form(self):
        # "Microsoft account requires JavaScript to sign in" 里含 "sign in"，
        # 之前会被误判成 email_form；KMSI 确认页必须单独识别为 kmsi。
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/login.srf?id=292841",
                "Microsoft account requires JavaScript to sign in. "
                "Stay signed in? user@outlook.com "
                "Skip having to sign in every time. Yes No",
                visible_selectors=(
                    'button[data-testid="primaryButton"]',
                    'button[data-testid="secondaryButton"]',
                ),
            )
        )

        self.assertEqual(state.name, "kmsi")
        self.assertIn("stay-signed-in", state.evidence)
        self.assertFalse(is_manual_verification(state))

    def test_stay_signed_in_localized_copy_is_kmsi(self):
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/login.srf",
                "保持登录？ 不再显示此内容 是 否",
            )
        )

        self.assertEqual(state.name, "kmsi")

    def test_japanese_fluent_kmsi_is_not_email_form(self):
        # 新 Fluent UI 的日文 KMSI 页标题是「サインインの状態を維持しますか?」，
        # 正文含「サインイン」；之前会被 email_form 的「サインイン」文本标记抢先，
        # 导致 KMSI 页被当成邮箱输入页卡住交给人工。
        state = classify_outlook_page(
            FakePage(
                "https://login.live.com/ppsecure/post.srf",
                "ko2lmqkohmrkut@outlook.com "
                "サインインの状態を維持しますか? "
                "毎回サインインする必要がないようにします。詳細情報 はい いいえ",
                visible_selectors=(
                    'button[data-testid="primaryButton"]',
                    'button[data-testid="secondaryButton"]',
                ),
            )
        )

        self.assertEqual(state.name, "kmsi")
        self.assertFalse(is_manual_verification(state))

    def test_locked_account_copy_is_classified_before_login_forms(self):
        state = classify_outlook_page(
            FakePage(
                "https://account.live.com/identity/confirm",
                "Your account has been locked\n"
                "We detected activity that goes against the Microsoft Services Agreement "
                "and have locked your account. You'll just need to show you're human in the next step.",
                visible_selectors=('button[type="submit"]',),
            )
        )

        self.assertEqual(state.name, "locked")
        self.assertEqual(state.evidence, "text:account-locked")
        self.assertFalse(is_manual_verification(state))


if __name__ == "__main__":
    unittest.main()
