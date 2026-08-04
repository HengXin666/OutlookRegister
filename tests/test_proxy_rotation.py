import unittest
from urllib.parse import unquote, urlsplit

import requests

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


class AutomaticIdentitySession(FakeSession):
    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "proxy_endpoint": {
                    "type": "http-connect",
                    "server": "remote-proxy.example",
                    "port": 443,
                    "tls": True,
                },
                "country_code": "US",
                "route_mode": "residential",
            })
        if method == "GET":
            self.calls.append((method, url, kwargs))
            proxy = (kwargs.get("proxies") or {}).get("https", "")
            if "__hx_invalid_session__" in proxy:
                return FakeResponse(407, text="Proxy Authentication Required")
            return FakeResponse(200, {
                "success": True,
                "ip": "203.0.113.21",
                "country_code": "US",
                "timezone": {"id": "America/New_York"},
            })
        return super().request(method, url, **kwargs)


class WebSocketOnlySession(FakeSession):
    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "550e8400-e29b-41d4-a716-446655440000",
                "proxy_endpoint": {
                    "type": "vless-ws",
                    "server": "proxy.example.com",
                    "port": 443,
                    "tls": True,
                    "path": "/__hx-proxy__/residential",
                },
                "country_code": "US",
                "route_mode": "residential",
            })
        return super().request(method, url, **kwargs)


class InvalidTokenSession(FakeSession):
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PUT":
            return FakeResponse(404, text="404 page not found")
        return super().request(method, url, **kwargs)


class ListenerCredentialIgnoredSession(AutomaticIdentitySession):
    def request(self, method, url, **kwargs):
        if method == "GET":
            self.calls.append((method, url, kwargs))
            return FakeResponse(200, {
                "success": True,
                "ip": "203.0.113.21",
                "country_code": "US",
                "timezone": {"id": "America/New_York"},
            })
        return super().request(method, url, **kwargs)


class ListenerConnectionFailureSession(FakeSession):
    def request(self, method, url, **kwargs):
        if method == "PUT":
            self.calls.append((method, url, kwargs))
            session_id = url.split("/sessions/", 1)[1]
            return FakeResponse(200, {
                "session_id": session_id,
                "proxy_username": f"hx-session-{session_id}",
                "proxy_password": "secret:@value",
                "proxy_endpoint": {
                    "type": "http-connect",
                    "server": "remote-proxy.example",
                    "port": 443,
                    "tls": True,
                },
                "country_code": "US",
                "route_mode": "residential",
            })
        return super().request(method, url, **kwargs)

    def get(self, url, **kwargs):
        raise requests.exceptions.ProxyError(
            "HTTPConnectionPool(host='remote-proxy.example', port=443): "
            "Max retries exceeded with url: https://ipwho.is/ "
            "(Caused by NewConnectionError('connection refused'))"
        )


class DeclaredNodeSession(FakeSession):
    def __init__(self, *, include_proxy=True, include_ws_endpoint=False, node_count=2):
        super().__init__()
        self.include_proxy = include_proxy
        self.include_ws_endpoint = include_ws_endpoint
        self.node_count = node_count

    def _node(self, index):
        return {
            "index": index,
            "node_name": f"residential-{index}",
            "proxy_url": (
                f"https://node-{index}:secret@proxy.example:{8442 + index}"
                if self.include_proxy
                else None
            ),
            "endpoints": ([{
                "protocol": "vless",
                "transport": "ws",
                "uri": (
                    "vless://550e8400-e29b-41d4-a716-446655440000@"
                    "proxy.example.com:443?security=tls&type=ws&"
                    "host=proxy.example.com&path=%2F__hx-proxy__%2Fresidential"
                ),
                "browser_compatible": False,
            }] if self.include_ws_endpoint else []),
            "country_code": "US",
            "route_mode": "residential",
            "hint": "" if self.include_proxy else "direct listener missing",
        }

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/nodes"):
            return FakeResponse(200, {
                "channel": "residential",
                "nodes": [self._node(index) for index in range(1, self.node_count + 1)],
            })
        if method == "POST" and url.endswith("/next"):
            index = int(url.rsplit("/", 2)[-2])
            return FakeResponse(200, self._node(index))
        if method == "POST" and url.endswith("/route"):
            index = int(url.rsplit("/", 2)[-2])
            node = self._node(index)
            node["route_mode"] = kwargs["json"]["route_mode"]
            return FakeResponse(200, node)
        raise AssertionError(f"unexpected request {method} {url}")

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        proxy = (kwargs.get("proxies") or {}).get("https", "")
        if "__hx_invalid_session__" in proxy:
            return FakeResponse(407, text="Proxy Authentication Required")
        port = urlsplit(proxy).port or 8443
        return FakeResponse(200, {
            "success": True,
            "ip": f"203.0.113.{port - 8422}",
            "country_code": "US",
            "timezone": {"id": "America/New_York"},
        })


class RotatingProxyPoolTests(unittest.TestCase):
    def test_control_url_leases_declared_nodes_without_server_session_deletes(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
            "required_pool_size": 2,
        })
        fake_session = DeclaredNodeSession()
        pool._session = fake_session

        first = pool.acquire_proxy()
        second = pool.acquire_proxy()

        self.assertEqual((first.node_index, second.node_index), (1, 2))
        self.assertEqual(first.country_code, "US")
        self.assertEqual(urlsplit(first.proxy).username, "node-1")
        with self.assertRaisesRegex(ProxyRotationError, "节点池已全部占用"):
            pool.acquire_proxy()

        pool.release(first)
        replacement = pool.acquire_proxy()
        self.assertEqual(replacement.node_index, 1)
        self.assertNotIn("DELETE", [call[0] for call in fake_session.calls])
        self.assertEqual(
            [call[1] for call in fake_session.calls if call[0] == "POST"],
            [
                "https://proxy.example/ctl/automatic-control-token/nodes/1/next",
                "https://proxy.example/ctl/automatic-control-token/nodes/2/next",
                "https://proxy.example/ctl/automatic-control-token/nodes/1/next",
            ],
        )

    def test_control_url_lands_websocket_endpoint_through_local_mihomo(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
            "max_rotate_retries": 0,
        })
        pool._session = DeclaredNodeSession(
            include_proxy=False,
            include_ws_endpoint=True,
            node_count=1,
        )

        class FakeLocalDataPlane:
            def __init__(self):
                self.started = []
                self.stopped = []

            def start(self, node_index, endpoint):
                self.started.append((node_index, endpoint))
                return "http://127.0.0.1:8443"

            def stop(self, node_index):
                self.stopped.append(node_index)

        local_data_plane = FakeLocalDataPlane()
        pool._local_data_plane = local_data_plane

        lease = pool.acquire_proxy()

        self.assertEqual(lease.proxy, "http://127.0.0.1:8443")
        self.assertEqual(local_data_plane.started[0][0], 1)
        self.assertEqual(local_data_plane.started[0][1]["protocol"], "vless")
        pool.release(lease)
        self.assertEqual(local_data_plane.stopped, [1])

    def test_control_url_requires_a_supported_data_endpoint(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
            "max_rotate_retries": 0,
        })
        pool._session = DeclaredNodeSession(include_proxy=False, node_count=1)

        with self.assertRaisesRegex(
            ProxyRotationError,
            "可用的数据端点",
        ):
            pool.acquire_proxy()

    def test_control_url_route_change_targets_the_leased_node(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
        })
        fake_session = DeclaredNodeSession(node_count=1)
        pool._session = fake_session
        lease = pool.acquire_proxy()

        updated = pool.switch_to_direct(lease)

        self.assertEqual(updated.node_index, 1)
        route_calls = [call for call in fake_session.calls if call[1].endswith("/route")]
        self.assertEqual(len(route_calls), 1)
        self.assertEqual(route_calls[0][2]["json"], {"route_mode": "direct"})

    def test_control_url_rejects_a_loopback_control_plane(self):
        with self.assertRaisesRegex(ProxyRotationError, "远程控制面不能使用回环地址"):
            RotatingProxyPool({
                "control_url": "https://127.0.0.1/ctl/automatic-control-token",
            })

    def test_automatic_rotation_url_derives_identity_without_sending_country(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
        })
        fake_session = AutomaticIdentitySession()
        pool._session = fake_session

        lease = pool.acquire_proxy()

        self.assertEqual(lease.exit_ip, "203.0.113.21")
        self.assertEqual(lease.country_code, "US")
        self.assertEqual(lease.browser_locale, "en-US")
        self.assertEqual(lease.timezone, "America/New_York")
        self.assertIsNone(fake_session.calls[0][2]["json"])
        self.assertIn("/rot/automatic-token/sessions/", fake_session.calls[0][1])
        self.assertEqual(urlsplit(lease.proxy).hostname, "remote-proxy.example")
        self.assertEqual(urlsplit(lease.proxy).port, 443)
        pool.release(lease)

    def test_automatic_rotation_url_rejects_a_loopback_control_plane(self):
        with self.assertRaisesRegex(ProxyRotationError, "远程控制面不能使用回环地址"):
            RotatingProxyPool({
                "rotation_url": "https://127.0.0.1/rot/automatic-token",
            })

    def test_automatic_rotation_rejects_a_loopback_data_endpoint(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
        })

        with self.assertRaisesRegex(ProxyRotationError, "远程数据面不能使用回环地址"):
            pool._proxy_from_session_payload(
                {
                    "proxy_endpoint": {
                        "type": "http-connect",
                        "server": "127.0.0.1",
                        "port": 7890,
                    }
                },
                {},
                "user",
                "password",
            )

    def test_automatic_rotation_ignores_legacy_listener_setting(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
            "listener": "http://192.0.2.10:7890",
        })
        self.assertEqual(pool.listener, "")

    def test_automatic_rotation_rejects_ws_without_browser_data_endpoint(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
        })
        fake_session = WebSocketOnlySession()
        pool._session = fake_session

        with self.assertRaisesRegex(ProxyRotationError, "仅返回 WebSocket"):
            pool.acquire_proxy()

        self.assertEqual([call[0] for call in fake_session.calls], ["PUT", "DELETE"])

    def test_invalid_rotation_token_is_reported_as_expired(self):
        pool = RotatingProxyPool({
            "base_url": "http://127.0.0.1:19090",
            "session_scoped": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = InvalidTokenSession()
        pool._session = fake_session

        with self.assertRaisesRegex(ProxyRotationError, "Token 无效或已失效"):
            pool.acquire_proxy()

        self.assertEqual([call[0] for call in fake_session.calls], ["PUT"])

    def test_listener_connection_failure_identifies_missing_listener(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
            "listener": "http://127.0.0.1:7890",
        })
        fake_session = ListenerConnectionFailureSession()
        pool._session = fake_session

        with self.assertRaisesRegex(
            ProxyRotationError,
            "Listener 未监听或连接被拒绝: https://remote-proxy.example:443",
        ):
            pool.acquire_proxy()

        self.assertEqual([call[0] for call in fake_session.calls], ["PUT", "DELETE"])

    def test_listener_authentication_failure_is_classified(self):
        message = RotatingProxyPool._listener_error(
            "HTTP 407 Proxy Authentication Required",
            "http://127.0.0.1:7890",
        )

        self.assertEqual(
            message,
            "HX-ProxyGroup Listener 认证失败: http://127.0.0.1:7890",
        )

    def test_automatic_rotation_rejects_a_listener_that_ignores_session_credentials(self):
        pool = RotatingProxyPool({
            "rotation_url": "https://proxy.example/rot/automatic-token",
            "listener": "http://127.0.0.1:7890",
        })
        fake_session = ListenerCredentialIgnoredSession()
        pool._session = fake_session

        with self.assertRaisesRegex(ProxyRotationError, "未强制校验会话认证"):
            pool.acquire_proxy()

        self.assertEqual(
            [call[0] for call in fake_session.calls],
            ["PUT", "GET", "GET", "DELETE"],
        )

    def test_pasted_rotation_url_is_normalized_without_duplicate_path(self):
        pool = RotatingProxyPool({
            "base_url": "https://proxy.example/rot/shared-token",
            "session_scoped": True,
            "tokens": [{
                "token": "shared-token",
                "proxy": "http://127.0.0.1:18088",
            }],
        })
        fake_session = FakeSession()
        pool._session = fake_session

        lease = pool.acquire_proxy()

        request_url = fake_session.calls[0][1]
        self.assertIn("/rot/shared-token/sessions/", request_url)
        self.assertNotIn("/rot/shared-token/rot/", request_url)
        self.assertFalse(fake_session.calls[0][2]["allow_redirects"])
        pool.release(lease)

    def test_remote_http_control_plane_is_rejected(self):
        with self.assertRaisesRegex(ProxyRotationError, "必须使用 https"):
            RotatingProxyPool({
                "base_url": "http://proxy.example",
                "tokens": [{
                    "token": "shared-token",
                    "proxy": "http://127.0.0.1:18088",
                }],
            })

    def test_pasted_rotation_url_must_match_the_only_token(self):
        with self.assertRaisesRegex(ProxyRotationError, "token 必须与唯一渠道 token 一致"):
            RotatingProxyPool({
                "base_url": "https://proxy.example/rot/other-token",
                "tokens": [{
                    "token": "shared-token",
                    "proxy": "http://127.0.0.1:18088",
                }],
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
