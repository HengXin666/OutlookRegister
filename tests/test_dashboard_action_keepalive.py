"""锁定保活、暂停恢复、日志上限与关停测试。"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from outlookregister.browser.outlook_page_state import OutlookPageState
from outlookregister.dashboard.dashboard_actions import (
    AUTHORIZE,
    KEEPALIVE,
    DashboardActionError,
    DashboardActionRunner,
)


class DashboardActionRunnerTests(unittest.TestCase):
    def test_locked_keepalive_stops_before_any_automatic_challenge_action(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)

    def test_locked_keepalive_without_continue_button_waits_for_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)
        self.assertIn("自动化已停止并保留浏览器", manual_verification.call_args.args[2])

    def test_locked_keepalive_falls_back_to_manual_after_bounded_hold_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)

    def test_keepalive_pause_blocks_at_checkpoint_until_resume(self):
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
            started = threading.Event()
            checkpoint = threading.Event()
            continued = threading.Event()
            observed_options = {}

            def execute(email, action):
                observed_options.update(runner._action_options(email, action))
                runner._mark_browser_open(email, action)
                started.set()
                checkpoint.wait(timeout=2)
                runner._wait_if_paused(email, action)
                continued.set()
                return "done"

            runner._execute_action = execute
            runner.submit(
                "user@outlook.com",
                KEEPALIVE,
                {"auth_mode": "recovery"},
            )
            self.assertTrue(started.wait(timeout=2))

            paused = runner.pause("user@outlook.com", KEEPALIVE)
            with self.assertRaises(DashboardActionError) as overlapping:
                runner.submit("user@outlook.com", AUTHORIZE)
            self.assertEqual(overlapping.exception.status_code, 409)
            checkpoint.set()
            self.assertEqual(paused["status"], "pausing")
            self.assertFalse(continued.wait(timeout=0.1))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                paused_state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if paused_state["status"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(paused_state["status"], "paused")

            resumed = runner.resume("user@outlook.com", KEEPALIVE)
            self.assertEqual(resumed["status"], "running")
            self.assertTrue(continued.wait(timeout=2))
            self.assertGreaterEqual(
                runner._paused_seconds("user@outlook.com", KEEPALIVE),
                0.09,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(observed_options["auth_mode"], "recovery")
        self.assertTrue(
            any("浏览器保持打开" in entry["message"] for entry in state["logs"])
        )

    def test_pause_rejects_non_keepalive_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            with self.assertRaises(DashboardActionError) as raised:
                runner.pause("user@outlook.com", AUTHORIZE)
            runner.shutdown()

        self.assertEqual(raised.exception.status_code, 409)

    def test_action_logs_are_bounded_and_public_errors_redact_transport_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            runner._config = lambda: {
                "proxy": "http://proxy-user:proxy-pass@127.0.0.1:2334",
                "proxy_rotation": {
                    "control_url": "https://proxy.example/ctl/control-secret",
                },
                "recovery_email": {"hx_email": {"api_key": "hx-secret"}},
            }
            for index in range(125):
                runner._set_state(
                    "user@outlook.com",
                    KEEPALIVE,
                    "running",
                    f"log-{index}",
                )
            state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
            detail = runner._public_error(
                "user@outlook.com",
                "http://other-user:other-pass@example.test/path "
                "https://proxy.example/ctl/control-secret?token=query-secret "
                "Bearer bearer-secret hx-secret",
            )
            runner.shutdown()

        self.assertEqual(len(state["logs"]), 100)
        self.assertEqual(state["logs"][0]["message"], "log-25")
        self.assertNotIn("other-user", detail)
        self.assertNotIn("other-pass", detail)
        self.assertNotIn("control-secret", detail)
        self.assertNotIn("query-secret", detail)
        self.assertNotIn("bearer-secret", detail)
        self.assertNotIn("hx-secret", detail)

    def test_shutdown_unblocks_a_paused_keepalive_worker(self):
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
            started = threading.Event()
            checkpoint = threading.Event()

            def execute(email, action):
                started.set()
                checkpoint.wait(timeout=2)
                runner._wait_if_paused(email, action)
                return "unexpected"

            runner._execute_action = execute
            runner.submit("user@outlook.com", KEEPALIVE)
            self.assertTrue(started.wait(timeout=2))
            runner.pause("user@outlook.com", KEEPALIVE)
            checkpoint.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "paused")

            runner.shutdown()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "failed":
                    break
                time.sleep(0.01)

        self.assertEqual(state["status"], "failed")
        self.assertIn("服务正在关闭", state["message"])

if __name__ == "__main__":
    unittest.main()
