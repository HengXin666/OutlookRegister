import json
import tempfile
import unittest
from pathlib import Path

from outlookregister.dashboard.dashboard_server import (
    DashboardStore,
    _interactive_proxy_config,
)
from outlookregister.dashboard.traffic_tracker import (
    TrafficRecorder,
    stage_for_hx_email_path,
)


class DashboardStoreTests(unittest.TestCase):
    def test_interactive_proxy_check_has_a_bounded_retry_budget(self):
        runtime_config = {
            "control_url": "https://proxy.example/ctl/control-token",
            "timeout_seconds": 30,
            "max_rotate_retries": 6,
        }

        interactive = _interactive_proxy_config(runtime_config)

        self.assertEqual(interactive["timeout_seconds"], 10)
        self.assertEqual(interactive["max_rotate_retries"], 0)
        self.assertEqual(runtime_config["max_rotate_retries"], 6)

    def test_snapshot_merges_four_milestones_and_traffic_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            checkpoints = [
                {
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "outlook_email": "user@outlook.com",
                    "password": "do-not-return",
                    "stage": "generated",
                    "detail": "generated",
                },
                {
                    "timestamp": "2026-08-01T00:00:05+00:00",
                    "outlook_email": "user@outlook.com",
                    "password": "do-not-return",
                    "stage": "registered",
                    "detail": "registered",
                },
                {
                    "timestamp": "2026-08-01T00:00:20+00:00",
                    "outlook_email": "user@outlook.com",
                    "password": "do-not-return",
                    "stage": "oauth_success",
                    "detail": "refresh-token-secret",
                },
                {
                    "timestamp": "2026-08-01T00:00:25+00:00",
                    "outlook_email": "user@outlook.com",
                    "password": "do-not-return",
                    "stage": "hx_email_imported",
                    "detail": "account_id=1",
                },
            ]
            recovery = {
                "timestamp": "2026-08-01T00:00:10+00:00",
                "outlook_email": "user@outlook.com",
                "bound": True,
                "recovery_email": "temporary@example.com",
                "reason": "verified",
                "detail": "verified",
            }
            traffic = [
                {
                    "timestamp": "2026-08-01T00:00:26+00:00",
                    "outlook_email": "user@outlook.com",
                    "stage": "residential_registration",
                    "source": "residential_browser",
                    "bytes": 100,
                    "identity_country_code": "DE",
                },
                {
                    "timestamp": "2026-08-01T00:00:26+00:00",
                    "outlook_email": "user@outlook.com",
                    "stage": "oauth_token_exchange",
                    "source": "oauth_token",
                    "bytes_received": 200,
                },
            ]
            (results / "account_checkpoints.jsonl").write_text(
                "\n".join(json.dumps(item) for item in checkpoints) + "\n",
                encoding="utf-8",
            )
            (results / "recovery_email_status.jsonl").write_text(
                json.dumps(recovery) + "\n", encoding="utf-8"
            )
            (results / "traffic_usage.jsonl").write_text(
                "\n".join(json.dumps(item) for item in traffic) + "\n",
                encoding="utf-8",
            )

            snapshot = DashboardStore(results).snapshot()

        self.assertEqual(snapshot["summary"]["total"], 1)
        self.assertEqual(snapshot["summary"]["fully_complete"], 1)
        self.assertEqual(snapshot["summary"]["registered"], 1)
        self.assertEqual(snapshot["summary"]["recovery_bound"], 1)
        self.assertEqual(snapshot["summary"]["oauth_authorized"], 1)
        self.assertEqual(snapshot["summary"]["hx_email_imported"], 1)
        self.assertEqual(snapshot["summary"]["average_duration_seconds"], 25.0)
        self.assertEqual(snapshot["stages"][0]["average_seconds"], 5.0)
        self.assertEqual(snapshot["traffic"]["total_bytes"], 300)
        self.assertEqual(snapshot["accounts"][0]["traffic"]["total_bytes"], 300)
        self.assertEqual(snapshot["accounts"][0]["identity_countries"], ["DE"])
        self.assertTrue(snapshot["accounts"][0]["recovery"]["bound"])
        self.assertEqual(
            snapshot["accounts"][0]["recovery"]["email"],
            "temporary@example.com",
        )
        self.assertEqual(
            snapshot["accounts"][0]["recovery_events"][0]["recovery_email"],
            "temporary@example.com",
        )

        account_json = json.dumps(snapshot["accounts"][0], ensure_ascii=False)
        self.assertNotIn("do-not-return", account_json)
        self.assertNotIn("refresh-token-secret", account_json)


class TrafficRecorderTests(unittest.TestCase):
    def test_hx_email_paths_keep_recovery_and_api_traffic_separate(self):
        self.assertEqual(
            stage_for_hx_email_path("/api/v1/temp-mail/42/codes"),
            "recovery_email",
        )
        self.assertEqual(
            stage_for_hx_email_path("/api/v1/auth/login"),
            "hx_email_api",
        )

    def test_task_buckets_are_flushed_as_stage_records(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = TrafficRecorder(directory)
            recorder.start_task(
                "user@outlook.com",
                flow_id="flow-123",
                proxy_session_id="session-123",
                proxy_exit_ip="203.0.113.20",
            )
            with recorder.stage("residential_registration", "residential_browser"):
                recorder.record(bytes_received=128)
            recorder.record_http(
                "oauth_token_exchange",
                "oauth_token",
                bytes_sent=32,
                bytes_received=64,
            )
            recorder.finish_task()

            records = [
                json.loads(line)
                for line in Path(directory, "traffic_usage.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(len(records), 2)
        self.assertEqual(
            {(record["stage"], record["bytes"]) for record in records},
            {
                ("residential_registration", 128),
                ("oauth_token_exchange", 96),
            },
        )
        self.assertEqual({record["flow_id"] for record in records}, {"flow-123"})
        self.assertEqual(
            {record["proxy_session_id"] for record in records},
            {"session-123"},
        )
        self.assertEqual(
            {record["proxy_exit_ip"] for record in records},
            {"203.0.113.20"},
        )


if __name__ == "__main__":
    unittest.main()
