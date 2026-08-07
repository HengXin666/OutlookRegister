"""监听器分类与轮换令牌校验测试。"""

import unittest
from urllib.parse import unquote, urlsplit

from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from tests.proxy_rotation_fakes import (
    CountrySession,
    CurrentSessionApi,
    FakeSession,
    MalformedSession,
)
from tests.proxy_rotation_node_fakes import (
    InvalidTokenSession,
    ListenerConnectionFailureSession,
    ListenerCredentialIgnoredSession,
)


class RotatingProxyPoolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
