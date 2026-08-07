"""AccountArtifactStore 与保活恢复/断点续跑测试。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from outlookregister.browser.outlook_page_state import OutlookPageState
from outlookregister.dashboard.dashboard_actions import (
    KEEPALIVE,
    AccountArtifactStore,
    DashboardActionError,
    DashboardActionRunner,
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
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
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

if __name__ == "__main__":
    unittest.main()
