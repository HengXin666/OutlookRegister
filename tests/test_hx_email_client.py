import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from hx_email_client import HXEmailClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ConcurrentGroupSession:
    def __init__(self):
        self.calls = []
        self.group = None
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/api/v1/groups"):
            # Make overlapping GET-before-POST windows likely without making the
            # test depend on a particular thread scheduling order.
            import time

            time.sleep(0.01)
            with self.lock:
                groups = [self.group] if self.group is not None else []
            return FakeResponse(groups)
        if method == "POST" and url.endswith("/api/v1/groups"):
            with self.lock:
                if self.group is not None:
                    return FakeResponse({"detail": "duplicate group"}, 500)
                self.group = {
                    "id": 3,
                    "name": "OutlookRegister 自动注册",
                }
                return FakeResponse(self.group, 201)
        raise AssertionError(f"unexpected request {method} {url}")


class HXEmailClientTests(unittest.TestCase):
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
            patch("hx_email_client.random.uniform", return_value=4.25) as random_delay,
            patch("hx_email_client.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))),
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
            patch("hx_email_client.random.uniform", return_value=0),
            patch("hx_email_client.time.sleep"),
            patch.object(client, "_read_code", side_effect=["482913", "736251"]),
        ):
            code = client.wait_for_code(
                {"email": "backup@example.test"},
                exclude_codes={"482913"},
            )

        self.assertEqual(code, "736251")

    def test_external_mailbox_uses_api_key_and_reads_verification_code(self):
        session = FakeSession([
            FakeResponse({
                "success": True,
                "data": {
                    "email": "backup@example.test",
                    "task_token": "task",
                    "usable_email_id": 7,
                },
            }),
            FakeResponse({"success": True, "data": {"verification_code": "482913"}}),
        ])
        client = HXEmailClient(
            {"base_url": "http://127.0.0.1:8080", "api_key": "key"},
            session=session,
        )

        mailbox = client.apply_mailbox()
        code = client._read_code(mailbox)

        self.assertEqual(mailbox["email"], "backup@example.test")
        self.assertEqual(code, "482913")
        self.assertEqual(session.calls[0][2]["headers"], {"X-API-Key": "key"})
        self.assertEqual(session.calls[1][2]["params"]["code_length"], 6)

    def test_session_fallback_creates_and_reads_temp_mailbox(self):
        session = FakeSession([
            FakeResponse({"access_token": "bearer"}),
            FakeResponse({"id": 9, "address": "backup@example.test"}, 201),
            FakeResponse({"codes": [{"message_id": "one", "code": "736251"}]}),
        ])
        client = HXEmailClient(
            {
                "base_url": "http://127.0.0.1:8080",
                "username": "admin",
                "password": "secret",
            },
            session=session,
        )

        mailbox = client.apply_mailbox()
        code = client._read_code(mailbox)

        self.assertEqual(mailbox["usable_email_id"], 9)
        self.assertEqual(code, "736251")
        self.assertEqual(
            session.calls[2][2]["headers"],
            {"Authorization": "Bearer bearer"},
        )

    def test_prefixed_base_url_and_full_authorization_header_are_normalized(self):
        client = HXEmailClient({
            "base_url": "http://localhost:5173/api/v1",
            "api_key": "Authorization: Bearer token-value",
        })

        self.assertEqual(client.base_url, "http://localhost:5173")
        self.assertTrue(client.prefer_session_api)
        self.assertEqual(
            client.api_headers(),
            {"Authorization": "Bearer token-value"},
        )

    def test_prefixed_url_uses_bearer_with_v1_temp_mail_api(self):
        session = FakeSession([
            FakeResponse({"id": 11, "address": "backup@example.test"}, 201),
        ])
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        mailbox = client.apply_mailbox()

        self.assertEqual(mailbox["mode"], "session")
        self.assertEqual(
            session.calls[0][1],
            "http://localhost:5173/api/v1/temp-mail/cf/mailboxes",
        )
        self.assertEqual(
            session.calls[0][2]["headers"],
            {"Authorization": "Bearer token-value"},
        )

    def test_import_outlook_account_creates_group_pool_entry_and_verifies_oauth(self):
        session = FakeSession([
            FakeResponse([]),
            FakeResponse({"id": 3, "name": "OutlookRegister 自动注册"}, 201),
            FakeResponse({"imported": 1, "failed": 0}, 201),
            FakeResponse({
                "accounts": [{"id": 5, "primary_address": "user@outlook.com"}],
            }),
            FakeResponse({
                "id": 5,
                "primary_usable_email": {"id": 8},
            }),
            FakeResponse({"id": 10}, 201),
            FakeResponse({"success": True, "message": "Token refreshed"}),
        ])
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        result = client.import_outlook_account(
            email="user@outlook.com",
            password="account-password",
            recovery_email="recovery@example.test",
            client_id="client-id",
            refresh_token="refresh-token",
            proxy_url="http://127.0.0.1:7890",
        )

        self.assertEqual(result, {"account_id": 5, "group_id": 3, "usable_email_id": 8})
        group_payload = session.calls[1][2]["json"]
        self.assertEqual(group_payload["proxy_url"], "http://127.0.0.1:7890")
        update_payload = session.calls[4][2]["json"]
        self.assertIn("登录密码: account-password", update_payload["remark"])
        self.assertIn("密保邮箱: recovery@example.test", update_payload["remark"])
        self.assertEqual(update_payload["refresh_token"], "refresh-token")
        self.assertEqual(
            session.calls[5][1],
            "http://localhost:5173/api/v1/mail-pool/entries",
        )
        self.assertTrue(session.calls[6][1].endswith("/email-accounts/5/refresh"))


if __name__ == "__main__":
    unittest.main()
