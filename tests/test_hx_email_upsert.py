"""HX-Email 分组路由与保活 upsert（先查后改，无则新增）测试。"""

import unittest

from outlookregister.email.hx_email_client import HXEmailClient
from tests.hx_email_fakes import FakeResponse, FakeSession

_CONFIG = {
    "base_url": "http://localhost:5173/api/v1",
    "api_key": "Authorization: Bearer token-value",
    "account_group": "共享分组",
    "register_account_group": "注册分组",
    "keepalive_account_group": "保活分组",
}

_REGISTER_GROUP = {"id": 11, "name": "注册分组", "proxy_url": "http://127.0.0.1:2334"}
_KEEPALIVE_GROUP = {"id": 22, "name": "保活分组", "proxy_url": "http://127.0.0.1:2334"}

_ACCOUNT = {"id": 7, "primary_usable_email": {"id": 70}}


def _client(responses, config=None):
    session = FakeSession(responses)
    return HXEmailClient(config or _CONFIG, session=session), session


def _methods(session):
    return [(method, url.rsplit("/api/v1", 1)[-1]) for method, url, _ in session.calls]


def _payload_of(session, method, suffix):
    for call_method, url, kwargs in session.calls:
        if call_method == method and url.endswith(suffix):
            return kwargs.get("json") or {}
    raise AssertionError(f"no {method} call ending in {suffix}")


class GroupRoutingTests(unittest.TestCase):
    def test_each_stage_resolves_its_own_group(self):
        client, _ = _client([])
        self.assertEqual(client.group_name_for_stage("register"), "注册分组")
        self.assertEqual(client.group_name_for_stage("keepalive"), "保活分组")
        self.assertEqual(client.group_name_for_stage(""), "共享分组")

    def test_stage_groups_fall_back_to_the_shared_group(self):
        client, _ = _client([], config={
            "base_url": "http://localhost:5173/api/v1",
            "api_key": "Authorization: Bearer token-value",
            "account_group": "只有共享",
        })
        self.assertEqual(client.group_name_for_stage("register"), "只有共享")
        self.assertEqual(client.group_name_for_stage("keepalive"), "只有共享")


class KeepaliveUpsertTests(unittest.TestCase):
    def test_existing_account_in_the_group_is_updated_without_importing(self):
        client, session = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            FakeResponse({"accounts": [
                {"id": 7, "primary_address": "a@outlook.com", "group_id": 22},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"id": 700}, 201),
            FakeResponse({"success": True}),
        ])

        result = client.upsert_outlook_account(
            "a@outlook.com", "pw", "r@x.com", "cid", "rt", stage="keepalive"
        )

        self.assertEqual(result["mode"], "updated")
        self.assertEqual(result["group_id"], 22)
        self.assertEqual(result["account_id"], 7)
        self.assertNotIn(
            ("POST", "/email-accounts/import"),
            _methods(session),
            "an account already in the group must not be imported again",
        )
        self.assertEqual(
            _methods(session),
            [
                ("GET", "/groups"),
                ("GET", "/email-accounts/search"),
                ("PUT", "/email-accounts/7"),
                ("POST", "/mail-pool/entries"),
                ("POST", "/email-accounts/7/refresh"),
            ],
        )

    def test_account_missing_from_the_group_is_imported_then_updated(self):
        client, session = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            FakeResponse({"accounts": []}),
            FakeResponse({"imported": 1}, 201),
            FakeResponse({"accounts": [
                {"id": 9, "primary_address": "b@outlook.com", "group_id": 22},
            ]}),
            FakeResponse({"id": 9, "primary_usable_email": {"id": 90}}),
            FakeResponse({"id": 900}, 201),
            FakeResponse({"success": True}),
        ])

        result = client.upsert_outlook_account(
            "b@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        self.assertEqual(result["mode"], "imported")
        self.assertEqual(result["account_id"], 9)
        self.assertIn(("POST", "/email-accounts/import"), _methods(session))
        self.assertEqual(_payload_of(session, "POST", "/import")["group_id"], 22)

    def test_same_address_in_another_group_is_treated_as_new(self):
        client, session = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            # Present in group 99, not in the keepalive group 22.
            FakeResponse({"accounts": [
                {"id": 5, "primary_address": "c@outlook.com", "group_id": 99},
            ]}),
            FakeResponse({"imported": 1}, 201),
            FakeResponse({"accounts": [
                {"id": 5, "primary_address": "c@outlook.com", "group_id": 22},
            ]}),
            FakeResponse({"id": 5, "primary_usable_email": {"id": 50}}),
            FakeResponse({"id": 500}, 201),
            FakeResponse({"success": True}),
        ])

        result = client.upsert_outlook_account(
            "c@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        self.assertEqual(result["mode"], "imported")
        self.assertIn(("POST", "/email-accounts/import"), _methods(session))

    def test_update_moves_the_account_into_the_keepalive_group(self):
        client, session = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            FakeResponse({"accounts": [
                {"id": 7, "primary_address": "a@outlook.com", "group_id": 22},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"id": 700}, 201),
            FakeResponse({"success": True}),
        ])

        client.upsert_outlook_account(
            "a@outlook.com", "pw", "r@x.com", "cid", "rt", stage="keepalive"
        )

        payload = _payload_of(session, "PUT", "/email-accounts/7")
        self.assertEqual(payload["group_id"], 22)
        self.assertEqual(payload["refresh_token"], "rt")
        self.assertEqual(payload["status"], "active")
        self.assertIn("自动保活", payload["remark"])
        self.assertIn("已更新", payload["remark"])

    def test_already_pooled_usable_email_is_not_an_error(self):
        client, _ = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            FakeResponse({"accounts": [
                {"id": 7, "primary_address": "a@outlook.com", "group_id": 22},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"detail": "already in pool"}, 409),
            FakeResponse({"success": True}),
        ])

        result = client.upsert_outlook_account(
            "a@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        self.assertEqual(result["usable_email_id"], 70)

    def test_empty_proxy_falls_back_to_group_proxy_default_when_creating_group(self):
        client, session = _client([
            FakeResponse([]),
            FakeResponse({"id": 23, "name": "保活分组", "proxy_url": "http://127.0.0.1:2334"}, 201),
            FakeResponse({"accounts": [
                {"id": 8, "primary_address": "a@outlook.com", "group_id": 23},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"id": 700}, 201),
            FakeResponse({"success": True}),
        ])

        client.upsert_outlook_account(
            "a@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        group_payload = _payload_of(session, "POST", "/groups")
        self.assertEqual(group_payload["proxy_url"], "http://127.0.0.1:2334")

    def test_existing_group_with_stale_proxy_is_self_healed(self):
        client, session = _client([
            FakeResponse([{"id": 22, "name": "保活分组", "proxy_url": "http://residential.example:9999"}]),
            FakeResponse({"id": 22, "name": "保活分组", "proxy_url": "http://127.0.0.1:2334"}),
            FakeResponse({"accounts": [
                {"id": 7, "primary_address": "a@outlook.com", "group_id": 22},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"id": 700}, 201),
            FakeResponse({"success": True}),
        ])

        result = client.upsert_outlook_account(
            "a@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        self.assertEqual(result["mode"], "updated")
        self.assertIn(("PUT", "/groups/22"), _methods(session))
        self.assertEqual(
            _payload_of(session, "PUT", "/groups/22")["proxy_url"],
            "http://127.0.0.1:2334",
        )

    def test_existing_group_with_matching_proxy_is_not_rewritten(self):
        client, session = _client([
            FakeResponse([_KEEPALIVE_GROUP]),
            FakeResponse({"accounts": [
                {"id": 7, "primary_address": "a@outlook.com", "group_id": 22},
            ]}),
            FakeResponse(_ACCOUNT),
            FakeResponse({"id": 700}, 201),
            FakeResponse({"success": True}),
        ])

        client.upsert_outlook_account(
            "a@outlook.com", "pw", "", "cid", "rt", stage="keepalive"
        )

        self.assertNotIn(("PUT", "/groups/22"), _methods(session))


class RegistrationImportTests(unittest.TestCase):
    def test_registration_always_imports_into_the_register_group(self):
        client, session = _client([
            FakeResponse([_REGISTER_GROUP]),
            FakeResponse({"imported": 1}, 201),
            FakeResponse({"accounts": [
                {"id": 3, "primary_address": "d@outlook.com", "group_id": 11},
            ]}),
            FakeResponse({"id": 3, "primary_usable_email": {"id": 30}}),
            FakeResponse({"id": 300}, 201),
            FakeResponse({"success": True}),
        ])

        result = client.import_outlook_account(
            "d@outlook.com", "pw", "", "cid", "rt", stage="register"
        )

        self.assertEqual(result["mode"], "imported")
        self.assertEqual(result["group_id"], 11)
        # No pre-search: registration skips the reuse lookup entirely.
        self.assertEqual(_methods(session)[:2], [
            ("GET", "/groups"),
            ("POST", "/email-accounts/import"),
        ])
        self.assertIn("自动注册", _payload_of(session, "PUT", "/email-accounts/3")["remark"])


if __name__ == "__main__":
    unittest.main()
