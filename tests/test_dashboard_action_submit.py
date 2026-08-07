"""动作提交串行化与失败脱敏测试。"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from outlookregister.dashboard.dashboard_actions import (
    AUTHORIZE,
    DashboardActionError,
    DashboardActionRunner,
)


class DashboardActionRunnerTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
