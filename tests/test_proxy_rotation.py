import unittest
from urllib.parse import unquote, urlsplit

from proxy_rotation import ProxyRotationError, RotatingProxyPool


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP status {self.status_code}")


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


class CurrentSessionApi(FakeSession):
    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "route_mode": "residential",
                "session_index": -1,
            })
        return super().request(method, url, **kwargs)


class ExplicitCapacitySession(FakeSession):
    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "route_mode": "residential",
                "session_index": -1,
                "pool_size": 0,
            })
        return super().request(method, url, **kwargs)


class ExitIpSession(FakeSession):
    def __init__(self, exit_ip):
        super().__init__()
        self.exit_ip = exit_ip

    def request(self, method, url, **kwargs):
        if method == "GET":
            self.calls.append((method, url, kwargs))
            return FakeResponse(200, {"ip": self.exit_ip})
        return super().request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class CountrySession(FakeSession):
    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            country_code = (kwargs.get("json") or {}).get("country_code", "")
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "country_code": country_code,
                "route_mode": "residential",
                "session_index": -1,
            })
        return super().request(method, url, **kwargs)


class ReleaseFailureSession(ExitIpSession):
    def request(self, method, url, **kwargs):
        if method == "DELETE":
            self.calls.append((method, url, kwargs))
            return FakeResponse(503, text="temporarily unavailable")
        return super().request(method, url, **kwargs)


class RotatingProxyPoolTests(unittest.TestCase):
    def test_unique_exit_ip_requires_session_scoped_leases(self):
        with self.assertRaisesRegex(ProxyRotationError, "session_scoped"):
            RotatingProxyPool({
                "base_url": "http://127.0.0.1:19090",
                "session_scoped": False,
                "check_proxy": True,
                "enforce_unique_exit_ip": True,
                "tokens": [{
                    "token": "shared-token",
                    "proxy": "http://127.0.0.1:18088",
                }],
            })

    def test_exit_ip_parser_accepts_plain_text_and_rejects_labels(self):
        self.assertEqual(
            RotatingProxyPool._parse_exit_ip(None, "203.0.113.99\n"),
            "203.0.113.99",
        )
        with self.assertRaises(ProxyRotationError):
            RotatingProxyPool._parse_exit_ip(None, "not-an-ip")

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

    def test_residential_post_route_keeps_the_existing_flow_session(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "post_registration_route": "residential",
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = FakeSession()
        pool._session = fake_session

        lease = pool.acquire_proxy()
        returned = pool.switch_after_registration(lease)

        self.assertIs(returned, lease)
        self.assertEqual([call[0] for call in fake_session.calls], ["PUT"])

    def test_country_code_is_sent_and_must_be_echoed_by_proxy_group(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "require_country_echo": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = CountrySession()
        pool._session = fake_session

        lease = pool.acquire_proxy("US")

        self.assertEqual(lease.country_code, "US")
        self.assertEqual(fake_session.calls[0][2]["json"], {"country_code": "US"})

    def test_country_specific_channel_is_not_used_for_another_requested_country(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "require_country_echo": True,
            "tokens": [{
                "token": "gb-token",
                "proxy": "http://127.0.0.1:18088",
                "country_code": "GB",
            }],
        })

        with self.assertRaisesRegex(ProxyRotationError, "没有配置支持国家 US"):
            pool.acquire_proxy("US")

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

    def test_current_session_api_without_legacy_pool_size_is_accepted(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "required_pool_size": 3,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = CurrentSessionApi()
        pool._session = fake_session

        lease = pool.acquire_proxy()

        self.assertTrue(lease.session_scoped)
        self.assertEqual([call[0] for call in fake_session.calls], ["PUT"])
        pool.release(lease)

    def test_explicit_legacy_pool_size_is_still_enforced(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "required_pool_size": 3,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = ExplicitCapacitySession()
        pool._session = fake_session

        with self.assertRaisesRegex(ProxyRotationError, "代理池容量不足"):
            pool.acquire_proxy()

        self.assertEqual(
            [call[0] for call in fake_session.calls],
            ["PUT", "DELETE"],
        )

    def test_duplicate_active_exit_ip_is_rejected(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "check_proxy": True,
            "enforce_unique_exit_ip": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = ExitIpSession("203.0.113.10")
        pool._session = fake_session

        first = pool.acquire_proxy()
        with self.assertRaisesRegex(ProxyRotationError, "出口 IP 重复"):
            pool.acquire_proxy()

        self.assertEqual(first.exit_ip, "203.0.113.10")
        self.assertEqual(
            [call[0] for call in fake_session.calls],
            ["PUT", "GET", "PUT", "GET", "DELETE"],
        )
        pool.release(first)

    def test_unique_exit_ip_is_released_for_next_flow(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "check_proxy": True,
            "enforce_unique_exit_ip": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = ExitIpSession("203.0.113.11")
        pool._session = fake_session

        first = pool.acquire_proxy()
        pool.release(first)
        second = pool.acquire_proxy()

        self.assertEqual(second.exit_ip, "203.0.113.11")
        pool.release(second)

    def test_failed_release_keeps_exit_ip_reserved(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "check_proxy": True,
            "enforce_unique_exit_ip": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = ReleaseFailureSession("203.0.113.12")
        pool._session = fake_session

        lease = pool.acquire_proxy()
        pool.release(lease)

        self.assertIn("203.0.113.12", pool._active_exit_ips)


if __name__ == "__main__":
    unittest.main()
