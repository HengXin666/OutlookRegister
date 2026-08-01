import unittest
from urllib.parse import unquote, urlsplit

from proxy_rotation import RotatingProxyPool


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PUT":
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "route_mode": "residential",
                "session_index": 1,
                "pool_size": 8,
            })
        if method == "POST":
            return FakeResponse(200, {"route_mode": kwargs["json"]["route_mode"]})
        if method == "DELETE":
            return FakeResponse(204)
        raise AssertionError(f"unexpected request {method} {url}")


class MalformedSession(FakeSession):
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PUT":
            return FakeResponse(200, {"proxy_username": "missing-password"})
        if method == "DELETE":
            return FakeResponse(204)
        raise AssertionError(f"unexpected request {method} {url}")


class RotatingProxyPoolTests(unittest.TestCase):
    def test_one_token_creates_independent_window_sessions_and_switches_direct(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "post_registration_route": "upstream",
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = FakeSession()
        pool._session = fake_session

        first = pool.acquire_proxy()
        second = pool.acquire_proxy()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(first.token, second.token)
        first_proxy = urlsplit(first.proxy)
        second_proxy = urlsplit(second.proxy)
        self.assertNotEqual(unquote(first_proxy.username), unquote(second_proxy.username))
        self.assertEqual(unquote(first_proxy.password), "secret:@value")
        self.assertEqual(first_proxy.port, 18088)

        pool.switch_after_registration(first)
        pool.release(first)

        methods = [call[0] for call in fake_session.calls]
        self.assertEqual(methods, ["PUT", "PUT", "POST", "DELETE"])
        self.assertEqual(
            fake_session.calls[2][2]["json"],
            {"route_mode": "upstream"},
        )

    def test_malformed_create_response_releases_allocated_server_session(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = MalformedSession()
        pool._session = fake_session

        with self.assertRaisesRegex(Exception, "缺少代理账号或密码"):
            pool.acquire_proxy()

        self.assertEqual([call[0] for call in fake_session.calls], ["PUT", "DELETE"])


if __name__ == "__main__":
    unittest.main()
