"""HXEmailClient code-reading and waiting logic tests."""

import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from outlookregister.email.hx_email_client import HXEmailClient
from tests.hx_email_fakes import ConcurrentGroupSession, FakeResponse, FakeSession


class HXEmailClientCodeTests(unittest.TestCase):
    def test_concurrent_group_initialization_reuses_one_group(self):
        session = ConcurrentGroupSession()
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        with ThreadPoolExecutor(max_workers=6) as executor:
            groups = list(executor.map(lambda _: client._ensure_account_group(""), range(6)))

        self.assertEqual({group["id"] for group in groups}, {3})
        self.assertEqual(
            sum(
                method == "POST" and url.endswith("/api/v1/groups")
                for method, url, _kwargs in session.calls
            ),
            1,
        )

    def test_duplicate_group_creation_is_recovered_by_reloading_group(self):
        session = FakeSession([
            FakeResponse([]),
            FakeResponse({"detail": "duplicate group"}, 500),
            FakeResponse([{"id": 3, "name": "OutlookRegister 自动注册"}]),
        ])
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        group = client._ensure_account_group("")

        self.assertEqual(group["id"], 3)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET"])

    def test_wait_for_code_delays_first_mailbox_read_by_three_to_five_seconds(self):
        client = HXEmailClient({"base_url": "http://127.0.0.1:8080", "api_key": "key"})
        events = []

        with (
            patch("outlookregister.email.hx_email_client.random.uniform", return_value=4.25) as random_delay,
            patch("outlookregister.email.hx_email_client.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))),
            patch.object(client, "_read_code", side_effect=lambda mailbox: events.append(("read", mailbox)) or "482913"),
        ):
            code = client.wait_for_code({"email": "backup@example.test"})

        self.assertEqual(code, "482913")
        random_delay.assert_called_once_with(3, 5)
        self.assertEqual(events[0], ("sleep", 4.25))
        self.assertEqual(events[1][0], "read")

    def test_wait_for_code_does_not_reuse_an_excluded_code(self):
        client = HXEmailClient({
            "base_url": "http://127.0.0.1:8080",
            "api_key": "key",
            "poll_interval_seconds": 0,
        })

        with (
            patch("outlookregister.email.hx_email_client.random.uniform", return_value=0),
            patch("outlookregister.email.hx_email_client.time.sleep"),
            patch.object(client, "_read_code", side_effect=["482913", "736251"]),
        ):
            code = client.wait_for_code(
                {"email": "backup@example.test"},
                exclude_codes={"482913"},
            )

        self.assertEqual(code, "736251")

    def test_read_code_selects_newest_timestamp_instead_of_last_array_item(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "codes": [
                            {
                                "message_id": "new",
                                "code": "222222",
                                "received_at": "2026-08-02T00:00:20+00:00",
                            },
                            {
                                "message_id": "old",
                                "code": "111111",
                                "received_at": "2026-08-02T00:00:10+00:00",
                            },
                        ]
                    }
                )
            ]
        )
        client = HXEmailClient(
            {
                "base_url": "http://127.0.0.1:8080/api/v1",
                "api_key": "key",
            },
            session=session,
        )

        self.assertEqual(
            client._read_code(
                {
                    "email": "backup@example.test",
                    "usable_email_id": 7,
                    "mode": "session",
                }
            ),
            "222222",
        )

    def test_read_code_uses_newest_response_position_when_timestamp_is_missing(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "codes": [
                            {"message_id": "old-looking-id", "code": "222222"},
                            {"message_id": "new-looking-id", "code": "111111"},
                        ]
                    }
                )
            ]
        )
        client = HXEmailClient(
            {
                "base_url": "http://127.0.0.1:8080/api/v1",
                "api_key": "key",
            },
            session=session,
        )

        self.assertEqual(
            client._read_code(
                {
                    "email": "backup@example.test",
                    "usable_email_id": 7,
                    "mode": "session",
                }
            ),
            "222222",
        )

    def test_wait_for_code_details_rejects_old_timestamp_and_logs_selected_code(self):
        client = HXEmailClient(
            {
                "base_url": "http://127.0.0.1:8080",
                "api_key": "key",
                "code_timeout_seconds": 30,
            }
        )
        baseline = datetime.now(UTC) - timedelta(seconds=5)
        candidates = client._normalize_code_candidates(
            [
                {
                    "message_id": "old",
                    "code": "111111",
                    "received_at": (baseline - timedelta(seconds=2)).isoformat(),
                },
                {
                    "message_id": "new",
                    "code": "222222",
                    "received_at": (baseline + timedelta(seconds=1)).isoformat(),
                },
            ],
            "session",
            {},
        )
        output = io.StringIO()
        with (
            patch("outlookregister.email.hx_email_client.random.uniform", return_value=0),
            patch("outlookregister.email.hx_email_client.time.sleep"),
            patch.object(client, "_read_code_candidates", return_value=candidates),
            redirect_stdout(output),
        ):
            details = client.wait_for_code_details(
                {"email": "backup@example.test"},
                not_before=baseline,
                known_message_ids={"old"},
            )

        self.assertEqual(details["code"], "222222")
        self.assertEqual(details["message_id"], "new")
        self.assertIn("code=111111", output.getvalue())
        self.assertIn("code=222222", output.getvalue())
        self.assertIn("使用验证码", output.getvalue())

    def test_wait_for_code_details_uses_new_message_id_when_legacy_api_has_no_time(self):
        client = HXEmailClient({"base_url": "http://127.0.0.1:8080", "api_key": "key"})
        baseline = datetime.now(UTC) - timedelta(seconds=1)
        candidates = client._normalize_code_candidates(
            [
                {"message_id": "old", "code": "111111"},
                {"message_id": "new", "code": "222222"},
            ],
            "session",
            {},
        )

        with (
            patch("outlookregister.email.hx_email_client.random.uniform", return_value=0),
            patch("outlookregister.email.hx_email_client.time.sleep"),
            patch.object(client, "_read_code_candidates", return_value=candidates),
        ):
            details = client.wait_for_code_details(
                {"email": "backup@example.test"},
                not_before=baseline,
                known_message_ids={"old"},
            )

        self.assertEqual(details["code"], "222222")
        self.assertIsNone(details["received_at"])
        self.assertIsNotNone(details["observed_at"])

if __name__ == "__main__":
    unittest.main()
