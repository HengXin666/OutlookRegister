import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from outlookregister.browser.base_controller import BaseBrowserController


class StubController(BaseBrowserController):
    def launch_browser(self, proxy=None, playwright=None):
        raise NotImplementedError

    def handle_captcha(self, page):
        raise NotImplementedError

    def clean_up(self, page=None, type="all_browser"):
        raise NotImplementedError

    def get_thread_page(self):
        raise NotImplementedError


def make_controller(attempts=2):
    controller = object.__new__(StubController)
    controller.thread_local = threading.local()
    controller.results_lock = threading.Lock()
    controller.recovery_email_enabled = True
    controller.recovery_code_attempts = attempts
    controller.enable_oauth2 = True
    controller.hx_email = MagicMock()
    controller.smooth_click = MagicMock()
    controller._save_recovery_diagnostic = MagicMock()
    return controller


class RecoveryEmailFlowTests(unittest.TestCase):
    def test_common_microsoft_error_message_is_detected(self):
        controller = make_controller()
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "That code didn't work. Check the code and try again."
        )

        self.assertEqual(
            controller._recovery_error(page),
            "that code didn't work",
        )

    def test_rejected_code_is_not_marked_as_bound(self):
        controller = make_controller(attempts=1)
        page = MagicMock()
        email_input = MagicMock()
        email_input.input_value.return_value = "backup@example.test"
        code_input = MagicMock()
        submit = MagicMock()
        controller.hx_email.apply_mailbox.return_value = {
            "email": "backup@example.test",
            "usable_email_id": 7,
            "mode": "session",
        }
        controller.hx_email.wait_for_code.return_value = "482913"

        with (
            patch.object(controller, "_recovery_page_visible", return_value=True),
            patch.object(
                controller,
                "_visible_first",
                side_effect=[email_input, submit, submit],
            ),
            patch.object(controller, "_recovery_code_input", return_value=code_input),
            patch.object(controller, "_fill_recovery_code", return_value=False),
            patch.object(
                controller,
                "_wait_for_recovery_confirmation",
                return_value=(False, "Microsoft 提示安全代码错误"),
            ),
        ):
            result = controller.handle_recovery_email(page)

        self.assertFalse(result)
        self.assertFalse(controller.thread_local.recovery_result["bound"])
        self.assertEqual(
            controller.thread_local.recovery_result["reason"],
            "verification_failed",
        )
        email_input.fill.assert_called_once_with(
            "backup@example.test",
            timeout=8000,
        )
        controller.hx_email.finish_mailbox.assert_called_once_with(
            {
                "email": "backup@example.test",
                "usable_email_id": 7,
                "mode": "session",
            },
            False,
            "Microsoft 提示安全代码错误",
        )

    def test_retry_requests_a_new_code_and_excludes_the_rejected_one(self):
        controller = make_controller(attempts=2)
        page = MagicMock()
        controller.hx_email.apply_mailbox.return_value = {
            "email": "backup@example.test",
            "usable_email_id": 7,
            "mode": "session",
        }
        controller.hx_email.wait_for_code.side_effect = ["482913", "736251"]

        with (
            patch.object(controller, "_recovery_page_visible", return_value=True),
            patch.object(controller, "_visible_first", return_value=MagicMock()),
            patch.object(controller, "_recovery_code_input", return_value=MagicMock()),
            patch.object(controller, "_fill_recovery_code", return_value=False),
            patch.object(
                controller,
                "_wait_for_recovery_confirmation",
                side_effect=[(False, "错误代码"), (True, "")],
            ),
            patch.object(controller, "_resend_recovery_code", return_value=True),
        ):
            result = controller.handle_recovery_email(page)

        self.assertTrue(result)
        self.assertTrue(controller.thread_local.recovery_result["bound"])
        self.assertEqual(
            controller.hx_email.wait_for_code.call_args_list,
            [
                call(
                    {
                        "email": "backup@example.test",
                        "usable_email_id": 7,
                        "mode": "session",
                    },
                    set(),
                ),
                call(
                    {
                        "email": "backup@example.test",
                        "usable_email_id": 7,
                        "mode": "session",
                    },
                    {"482913"},
                ),
            ],
        )

    def test_segmented_code_uses_delayed_keyboard_input_and_shared_confirmation(self):
        controller = make_controller(attempts=1)
        page = MagicMock()
        inputs = [MagicMock() for _ in range(6)]
        segmented = MagicMock()
        segmented.count.return_value = len(inputs)
        segmented.nth.side_effect = lambda index: inputs[index]
        page.locator.return_value = segmented
        mailbox = {
            "email": "backup@example.test",
            "usable_email_id": 7,
            "mode": "session",
        }
        controller.hx_email.wait_for_code.return_value = "482913"

        with (
            patch.object(controller, "_visible_first", return_value=None),
            patch.object(controller, "_recovery_code_input", return_value=inputs[0]),
            patch.object(
                controller,
                "_wait_for_recovery_confirmation",
                return_value=(True, ""),
            ) as wait_for_confirmation,
        ):
            result = controller.confirm_recovery_email_challenge(
                page,
                controller.hx_email,
                mailbox,
                "backup@example.test",
            )

        self.assertTrue(result)
        page.keyboard.type.assert_called_once_with("482913", delay=120)
        controller.smooth_click.assert_called_once_with(page, inputs[0])
        for input_box in inputs:
            input_box.fill.assert_called_once_with("")
        wait_for_confirmation.assert_called_once_with(page)

    def test_status_file_marks_bound_and_unbound_accounts(self):
        controller = make_controller()
        with tempfile.TemporaryDirectory() as directory:
            controller.results_dir = directory
            controller._set_recovery_result(
                bound=True,
                recovery_email="backup@example.test",
                reason="verified",
                usable_email_id=7,
                mailbox_mode="session",
            )
            controller._write_recovery_result("bound@outlook.com")
            controller._set_recovery_result(
                reason="verification_failed",
                detail="验证码错误",
            )
            controller._write_recovery_result("unbound@outlook.com")

            lines = Path(directory, "recovery_email_status.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            records = [json.loads(line) for line in lines]

        self.assertTrue(records[0]["bound"])
        self.assertEqual(records[0]["recovery_email"], "backup@example.test")
        self.assertEqual(records[0]["usable_email_id"], 7)
        self.assertFalse(records[1]["bound"])
        self.assertEqual(records[1]["reason"], "verification_failed")

    def test_registered_credentials_are_saved_once_before_later_failures(self):
        controller = make_controller()
        controller.thread_local.credentials_saved = False
        with tempfile.TemporaryDirectory() as directory:
            controller.results_dir = directory

            first = controller._save_registered_credentials(
                "saved@outlook.com",
                "account-password",
                "post_signup_page_visible",
            )
            second = controller._save_registered_credentials(
                "saved@outlook.com",
                "account-password",
                "later_recovery_step",
            )

            credentials = Path(directory, "logged_email.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            checkpoints = [
                json.loads(line)
                for line in Path(directory, "account_checkpoints.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            credentials,
            ["saved@outlook.com: account-password"],
        )
        self.assertEqual([item["stage"] for item in checkpoints], ["registered"])


if __name__ == "__main__":
    unittest.main()
