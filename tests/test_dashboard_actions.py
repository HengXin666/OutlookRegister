import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from dashboard_actions import (
    AUTHORIZE,
    KEEPALIVE,
    AccountArtifactStore,
    DashboardActionError,
    DashboardActionRunner,
    OAUTH_PAGE_DELAY_MS,
    SUCCESS_WINDOW_DELAY_MS,
)
from outlook_page_state import OutlookPageState


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
    def test_keepalive_recovery_email_page_uses_recovery_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            runner._states["user@outlook.com"] = {
                KEEPALIVE: {
                    "email": "user@outlook.com",
                    "action": KEEPALIVE,
                    "status": "running",
                    "step": "email_login",
                    "_resume_step": "email_login",
                    "logs": [],
                    "steps": {
                        "login": "completed",
                        "email_login": "running",
                        "email_code": "pending",
                        "manual_challenge": "pending",
                        "oauth": "pending",
                        "hx_email": "pending",
                    },
                }
            }
            page = MagicMock()
            controller = MagicMock()
            recovery_handler = MagicMock(return_value=True)
            states = [
                OutlookPageState(
                    "recovery_email_form",
                    "dom:#proof-confirmation-email-input",
                    "/proofs/add",
                ),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_ensure_outlook_step_page") as ensure_page,
                patch.object(runner, "_set_progress", wraps=runner._set_progress) as progress,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "backup@example.test",
                    recovery_handler,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        recovery_handler.assert_called_once_with(page)
        ensure_page.assert_called_once_with(page, "email_login", fresh=False)
        self.assertEqual(progress.call_args_list[0].args[2], "email_login")

    def test_resume_keeps_selected_step_and_rejects_future_step(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            state = {
                "email": "user@outlook.com",
                "action": KEEPALIVE,
                "status": "manual_verification_required",
                "step": "manual_challenge",
                "logs": [],
                "steps": {
                    "login": "completed",
                    "email_login": "completed",
                    "email_code": "completed",
                    "manual_challenge": "paused",
                    "oauth": "pending",
                    "hx_email": "pending",
                },
            }
            runner._states["user@outlook.com"] = {KEEPALIVE: state}
            event = threading.Event()
            runner._verification_events[("user@outlook.com", KEEPALIVE)] = event

            selected = runner.resume(
                "user@outlook.com",
                KEEPALIVE,
                "email_login",
            )
            with self.assertRaises(DashboardActionError) as raised:
                runner.resume("user@outlook.com", KEEPALIVE, "oauth")
            runner.shutdown()

        self.assertEqual(selected["step"], "email_login")
        self.assertEqual(selected["status"], "manual_verification_required")
        self.assertTrue(event.is_set())
        self.assertEqual(raised.exception.status_code, 422)

    def test_checkpoint_context_keeps_flow_identity_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            runner = DashboardActionRunner(root, results, max_workers=1)
            runner._set_checkpoint_context(
                "flow-a",
                "worker-a",
                SimpleNamespace(
                    session_id="session-a",
                    exit_ip="203.0.113.10",
                    country_code="US",
                ),
                {
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "America/New_York",
                },
            )
            runner._append_checkpoint(
                "user@outlook.com",
                "private-password",
                "keepalive_started",
                "started",
            )
            runner._clear_checkpoint_context()
            runner.shutdown()

            record = json.loads(
                (results / "account_checkpoints.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )

        self.assertEqual(record["flow_id"], "flow-a")
        self.assertEqual(record["worker_id"], "worker-a")
        self.assertEqual(record["proxy_session_id"], "session-a")
        self.assertEqual(record["proxy_exit_ip"], "203.0.113.10")
        self.assertEqual(record["identity_country_code"], "US")
        self.assertEqual(record["browser_locale"], "en-US")
        self.assertEqual(record["browser_timezone"], "America/New_York")

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
                patch("dashboard_actions.classify_outlook_page", side_effect=states),
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
                patch("dashboard_actions.classify_outlook_page", side_effect=states),
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
                patch("dashboard_actions.classify_outlook_page", side_effect=states),
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
