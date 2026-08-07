"""授权流程与动作提交/失败脱敏测试。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from outlookregister.dashboard.dashboard_actions import (
    OAUTH_PAGE_DELAY_MS,
    SUCCESS_WINDOW_DELAY_MS,
    DashboardActionRunner,
)


class DashboardActionRunnerTests(unittest.TestCase):
    def test_authorize_delegates_recovery_challenge_to_shared_controller_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            recovery_record = {
                "outlook_email": "user@outlook.com",
                "bound": True,
                "recovery_email": "backup@example.test",
                "usable_email_id": 42,
                "mailbox_mode": "session",
            }
            (results / "recovery_email_status.jsonl").write_text(
                json.dumps(recovery_record) + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            controller = MagicMock()
            controller.thread_local = threading.local()
            controller.hx_email = MagicMock()
            controller.get_proxy.return_value = None
            page = MagicMock()
            controller.get_thread_page.return_value = page
            resolved_mailbox = {"usable_email_id": 42, "mode": "session"}
            controller.hx_email.resolve_mailbox.return_value = resolved_mailbox
            controller.confirm_recovery_email_challenge.return_value = True
            runner._config = lambda: {
                "email_suffix": "@outlook.com",
                "oauth2": {"client_id": "client-id"},
            }
            runner._controller = lambda config: controller
            recorder = MagicMock()

            def authorize_with_challenge(*args, **kwargs):
                handler = kwargs["recovery_challenge_handler"]
                self.assertTrue(handler(page))
                return "refresh", "access", 123

            with (
                patch("outlookregister.dashboard.dashboard_actions.TrafficRecorder", return_value=recorder),
                patch(
                    "outlookregister.dashboard.dashboard_actions.get_access_token",
                    side_effect=authorize_with_challenge,
                ),
            ):
                result = runner._authorize("user@outlook.com")
            runner.shutdown()

        self.assertEqual(result, "OAuth 授权已完成")
        page.wait_for_timeout.assert_has_calls(
            [call(OAUTH_PAGE_DELAY_MS), call(SUCCESS_WINDOW_DELAY_MS)]
        )
        controller.clean_up.assert_any_call(page, "done_browser")
        controller.close_thread_browser.assert_called_once_with()
        controller.clean_up.assert_any_call(type="all_browser")
        controller.hx_email.resolve_mailbox.assert_called_once_with(
            "backup@example.test",
            mailbox_hint={
                "email": "backup@example.test",
                "usable_email_id": 42,
                "mode": "session",
            },
        )
        controller.confirm_recovery_email_challenge.assert_called_once_with(
            page,
            controller.hx_email,
            resolved_mailbox,
            "backup@example.test",
        )

    def test_authorize_closes_browser_when_authorization_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            controller = MagicMock()
            controller.thread_local = threading.local()
            controller.hx_email = MagicMock()
            controller.get_proxy.return_value = None
            page = MagicMock()
            controller.get_thread_page.return_value = page
            runner._config = lambda: {
                "email_suffix": "@outlook.com",
                "oauth2": {"client_id": "client-id"},
            }
            runner._controller = lambda config: controller

            with patch(
                "outlookregister.dashboard.dashboard_actions.get_access_token",
                side_effect=RuntimeError("captcha is still loading"),
            ):
                with self.assertRaisesRegex(RuntimeError, "captcha is still loading"):
                    runner._authorize("user@outlook.com")
            runner.shutdown()

        page.wait_for_timeout.assert_called_once_with(OAUTH_PAGE_DELAY_MS)
        controller.clean_up.assert_has_calls(
            [call(page, "done_browser"), call(type="all_browser")]
        )
        controller.close_thread_browser.assert_called_once_with()
        controller.hx_email.close.assert_called_once_with()

    def test_authorize_can_create_a_new_browser_after_a_failed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            controllers = []

            def make_controller(config):
                controller = MagicMock()
                controller.thread_local = threading.local()
                controller.hx_email = MagicMock()
                controller.get_proxy.return_value = None
                controller.get_thread_page.return_value = MagicMock()
                controllers.append(controller)
                return controller

            runner._config = lambda: {
                "email_suffix": "@outlook.com",
                "oauth2": {"client_id": "client-id"},
            }
            runner._controller = make_controller

            with patch(
                "outlookregister.dashboard.dashboard_actions.get_access_token",
                side_effect=[RuntimeError("first attempt"), RuntimeError("second attempt")],
            ):
                for message in ("first attempt", "second attempt"):
                    with self.assertRaisesRegex(RuntimeError, message):
                        runner._authorize("user@outlook.com")
            runner.shutdown()

        self.assertEqual(len(controllers), 2)
        for controller in controllers:
            controller.get_thread_page.assert_called_once_with()
            controller.close_thread_browser.assert_called_once_with()
            controller.clean_up.assert_any_call(type="all_browser")

if __name__ == "__main__":
    unittest.main()
