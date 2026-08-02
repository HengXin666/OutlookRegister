import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from dashboard_actions import (
    AUTHORIZE,
    AccountArtifactStore,
    DashboardActionError,
    DashboardActionRunner,
    OAUTH_PAGE_DELAY_MS,
    SUCCESS_WINDOW_DELAY_MS,
)


class AccountArtifactStoreTests(unittest.TestCase):
    def test_resolves_latest_credentials_token_and_bound_recovery_email(self):
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            checkpoints = [
                {
                    "outlook_email": "User@outlook.com",
                    "password": "old-password",
                },
                {"invalid": True},
                {
                    "outlook_email": "user@outlook.com",
                    "password": "new-password",
                },
            ]
            (results / "account_checkpoints.jsonl").write_text(
                "\n".join(json.dumps(item) for item in checkpoints) + "\n",
                encoding="utf-8",
            )
            (results / "outlook_token.txt").write_text(
                "user@outlook.com---new-password---refresh---access---123\n",
                encoding="utf-8",
            )
            (results / "recovery_email_status.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "bound": True,
                        "recovery_email": "recovery@example.com",
                        "usable_email_id": 42,
                        "mailbox_mode": "session",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store = AccountArtifactStore(results)

            self.assertEqual(
                store.credentials("USER@outlook.com"),
                ("user@outlook.com", "new-password"),
            )
            self.assertEqual(
                store.oauth_token("USER@outlook.com")["refresh_token"],
                "refresh",
            )
            self.assertEqual(
                store.recovery_email("USER@outlook.com"),
                "recovery@example.com",
            )
            self.assertEqual(
                store.recovery_mailbox("USER@outlook.com")["usable_email_id"],
                42,
            )

    def test_missing_credentials_are_reported_as_not_found(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountArtifactStore(directory)
            with self.assertRaises(DashboardActionError) as raised:
                store.credentials("missing@outlook.com")

        self.assertEqual(raised.exception.status_code, 404)


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
                patch("dashboard_actions.TrafficRecorder", return_value=recorder),
                patch(
                    "dashboard_actions.get_access_token",
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
                "dashboard_actions.get_access_token",
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
                "dashboard_actions.get_access_token",
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

    def test_submit_prevents_overlapping_actions_and_reports_completion(self):
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
            started = threading.Event()
            release = threading.Event()
            runner = DashboardActionRunner(root, results, max_workers=1)

            def execute(email, action):
                started.set()
                release.wait(timeout=2)
                return "done"

            runner._execute_action = execute
            queued = runner.submit("user@outlook.com", AUTHORIZE)
            self.assertEqual(queued["status"], "queued")
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaises(DashboardActionError) as raised:
                runner.submit("user@outlook.com", AUTHORIZE)
            self.assertEqual(raised.exception.status_code, 409)

            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][AUTHORIZE]
                if state["status"] == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["message"], "done")
        self.assertNotIn("private-password", json.dumps(state))

    def test_submit_allows_retry_after_a_failed_action(self):
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
            attempts = []

            def execute(email, action):
                attempts.append((email, action))
                if len(attempts) == 1:
                    raise RuntimeError("first browser attempt failed")
                return "retry succeeded"

            runner._execute_action = execute
            runner.submit("user@outlook.com", AUTHORIZE)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][AUTHORIZE]
                if state["status"] == "failed":
                    break
                time.sleep(0.01)

            runner.submit("user@outlook.com", AUTHORIZE)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][AUTHORIZE]
                if state["status"] == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["message"], "retry succeeded")
        self.assertEqual(len(attempts), 2)

    def test_failed_action_redacts_known_credentials_from_status(self):
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
            runner._execute_action = lambda email, action: (_ for _ in ()).throw(
                RuntimeError("login private-password rejected")
            )
            runner.submit("user@outlook.com", AUTHORIZE)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][AUTHORIZE]
                if state["status"] == "failed":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["message"], "login [redacted] rejected")


if __name__ == "__main__":
    unittest.main()
