import unittest
from unittest.mock import patch
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


class ReleaseFailureSession(ExitIpSession):
    def request(self, method, url, **kwargs):
        if method == "DELETE":
            self.calls.append((method, url, kwargs))
            return FakeResponse(503, text="temporarily unavailable")
        return super().request(method, url, **kwargs)


class DeclaredNodeSession(FakeSession):
    node = {
        "index": 1,
        "node_name": "residential-01",
        "proxy_url": None,
        "route_mode": "residential",
        "endpoints": [{
            "protocol": "vless",
            "transport": "ws",
            "uri": (
                "vless://00000000-0000-4000-8000-000000000001@proxy.example.com:443"
                "?security=tls&type=ws&host=proxy.example.com&path=%2Fws&sni=proxy.example.com"
            ),
            "browser_compatible": False,
        }],
    }

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/nodes"):
            return FakeResponse(200, {"nodes": [self.node]})
        if method == "POST" and url.endswith("/nodes/1/next"):
            return FakeResponse(200, self.node)
        if method == "POST" and url.endswith("/nodes/1/route"):
            return FakeResponse(200, {"route_mode": kwargs["json"]["route_mode"]})
        raise AssertionError(f"unexpected request {method} {url}")


class FakeMihomoRuntime:
    instances = []

    def __init__(self, binary, uri, startup_timeout):
        self.binary = binary
        self.uri = uri
        self.startup_timeout = startup_timeout
        self.proxy_url = "http://127.0.0.1:32123"
        self.started = False
        self.closed = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class RotatingProxyPoolTests(unittest.TestCase):
    def test_declared_vless_node_uses_local_mihomo_and_returns_to_pool(self):
        session = DeclaredNodeSession()
        FakeMihomoRuntime.instances.clear()
        config = {
            "control_url": "https://proxy.example.com/ctl/control-token",
            "protocol_preference": ["vless"],
            "required_pool_size": 1,
            "session_scoped": True,
            "check_proxy": False,
            "enforce_unique_exit_ip": False,
            "post_registration_route": "direct",
        }
        with patch("proxy_rotation.requests.Session", return_value=session), patch(
            "proxy_rotation.ManagedMihomo", FakeMihomoRuntime
        ):
            pool = RotatingProxyPool(config)
            lease = pool.acquire_proxy()
            self.assertEqual(pool.capacity, 1)
            self.assertEqual(lease.proxy, "http://127.0.0.1:32123")
            self.assertEqual(lease.node_index, 1)
            self.assertEqual(lease.session_id, "node-1")
            self.assertTrue(FakeMihomoRuntime.instances[0].started)

            pool.switch_after_registration(lease)
            pool.release(lease)

        self.assertTrue(FakeMihomoRuntime.instances[0].closed)
        self.assertEqual(len(pool._available_nodes), 1)
        self.assertEqual(
            [(method, url.rsplit("/", 1)[-1]) for method, url, _ in session.calls],
            [("GET", "nodes"), ("POST", "next"), ("POST", "route")],
        )

    def test_declared_mode_rejects_insufficient_nodes(self):
        session = DeclaredNodeSession()
        with patch("proxy_rotation.requests.Session", return_value=session):
            with self.assertRaisesRegex(ProxyRotationError, "required=2"):
                RotatingProxyPool({
                    "control_url": "https://proxy.example.com/ctl/control-token",
                    "required_pool_size": 2,
                    "session_scoped": True,
                    "check_proxy": False,
                    "enforce_unique_exit_ip": False,
                })

    def test_declared_mode_rejects_public_plaintext_control_url(self):
        with self.assertRaisesRegex(ProxyRotationError, "必须使用 HTTPS"):
            RotatingProxyPool({
                "control_url": "http://proxy.example.com/ctl/control-token",
                "required_pool_size": 1,
                "session_scoped": True,
                "check_proxy": False,
                "enforce_unique_exit_ip": False,
            })

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
