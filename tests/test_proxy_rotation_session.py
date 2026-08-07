"""会话隔离、国家通道与出口 IP 唯一性测试。"""

import unittest

from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from tests.proxy_rotation_fakes import (
    ExitIpSession,
    ExplicitCapacitySession,
    ReleaseFailureSession,
)


class RotatingProxyPoolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
