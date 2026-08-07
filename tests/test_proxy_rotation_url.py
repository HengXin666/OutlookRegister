"""轮换 URL 校验与自动身份派生测试。"""

import unittest
from urllib.parse import urlsplit

from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from tests.proxy_rotation_node_fakes import (
    AutomaticIdentitySession,
    WebSocketOnlySession,
)


class RotatingProxyPoolTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
