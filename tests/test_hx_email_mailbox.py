"""HXEmailClient mailbox parsing, archiving and account import tests."""

import unittest

from outlookregister.email.hx_email_client import HXEmailClient
from tests.hx_email_fakes import FakeResponse, FakeSession


class HXEmailClientMailboxTests(unittest.TestCase):
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

    def test_resolve_mailbox_finds_an_archived_temp_address(self):
        session = FakeSession([
            FakeResponse({
                "usable_emails": [
                    {
                        "id": 9,
                        "address": "backup@example.test",
                        "kind": "temp",
                        "status": "archived",
                    }
                ]
            }),
        ])
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        mailbox = client.resolve_mailbox("backup@example.test")

        self.assertEqual(
            mailbox,
            {
                "email": "backup@example.test",
                "task_token": "",
                "usable_email_id": 9,
                "mode": "session",
            },
        )
        self.assertEqual(
            session.calls[0][2]["params"]["keyword"],
            "backup@example.test",
        )

    def test_finish_mailbox_archives_a_session_mailbox(self):
        session = FakeSession([FakeResponse({"id": 9, "status": "archived"})])
        client = HXEmailClient(
            {
                "base_url": "http://localhost:5173/api/v1",
                "api_key": "Authorization: Bearer token-value",
            },
            session=session,
        )

        client.finish_mailbox(
            {
                "email": "backup@example.test",
                "usable_email_id": 9,
                "mode": "session",
            },
            True,
        )

        self.assertEqual(session.calls[0][0], "POST")
        self.assertTrue(session.calls[0][1].endswith("/api/v1/temp-mail/9/archive"))

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

        self.assertEqual(
            result,
            {
                "account_id": 5,
                "group_id": 3,
                "usable_email_id": 8,
                "mode": "imported",
            },
        )
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
