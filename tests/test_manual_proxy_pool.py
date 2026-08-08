"""手动代理列表池：逐行消费、落盘、跳过失效行与出口去重测试。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from outlookregister.proxy.manual_proxy_pool import ManualProxyPool
from outlookregister.proxy.proxy_pool_types import ProxyRotationError

_LINES = [
    "http://u1:p1@1.1.1.1:8000",
    "http://u2:p2@2.2.2.2:8000",
    "http://u3:p3@3.3.3.3:8000",
]


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = json.dumps(self.payload)

    def json(self):
        return self.payload


class _IdentitySession:
    """Answer the identity probe per proxy host, like ipwho.is would."""

    def __init__(self, dead_hosts=(), exit_ip_for=None):
        self.dead_hosts = set(dead_hosts)
        self.exit_ip_for = exit_ip_for or {}
        self.probed = []
        self.trust_env = True

    def get(self, url, **kwargs):
        proxy = (kwargs.get("proxies") or {}).get("https", "")
        host = proxy.split("@")[-1].split(":")[0]
        self.probed.append(host)
        if host in self.dead_hosts:
            return _Response(502)
        return _Response(200, {
            "ip": self.exit_ip_for.get(host, host),
            "country_code": "US",
            "timezone": {"id": "America/New_York"},
        })

    def close(self):
        pass


def _write_config(directory, pending, used=()):
    base = json.loads(Path("config.json.example").read_text(encoding="utf-8"))
    base["proxy_source"] = "manual"
    base["manual_proxy_pool"] = {"pending": list(pending), "used": list(used)}
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def _pool(path, session, **overrides):
    config = json.loads(path.read_text(encoding="utf-8"))
    config.setdefault("proxy_rotation", {}).update(overrides)
    pool = ManualProxyPool(config, config_path=path)
    pool._session = session
    return pool


def _state(path):
    return json.loads(path.read_text(encoding="utf-8"))["manual_proxy_pool"]


class ManualProxyPoolTests(unittest.TestCase):
    def test_lines_are_handed_out_in_order_and_persisted_as_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession())

            first = pool.acquire_proxy()
            self.assertEqual(first.proxy, _LINES[0])
            self.assertEqual(_state(path)["pending"], _LINES[1:])
            self.assertEqual(_state(path)["used"], _LINES[:1])

            second = pool.acquire_proxy()
            self.assertEqual(second.proxy, _LINES[1])
            self.assertEqual(_state(path)["pending"], _LINES[2:])
            self.assertEqual(_state(path)["used"], _LINES[:2])

    def test_lease_is_session_scoped_with_a_confirmed_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession())

            lease = pool.acquire_proxy()

            self.assertTrue(lease.session_scoped)
            self.assertEqual(lease.token, "manual")
            self.assertEqual(lease.exit_ip, "1.1.1.1")
            self.assertEqual(
                pool.identity_profile_for_lease(lease),
                {
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "America/New_York",
                },
            )

    def test_a_dead_line_is_still_consumed_and_the_next_line_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession(dead_hosts={"1.1.1.1"}))

            lease = pool.acquire_proxy()

            self.assertEqual(lease.proxy, _LINES[1])
            # The dead line is burnt too: every line is offered exactly once.
            self.assertEqual(_state(path)["used"], _LINES[:2])
            self.assertEqual(_state(path)["pending"], _LINES[2:])

    def test_release_does_not_return_the_line_to_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession())

            lease = pool.acquire_proxy()
            pool.release(lease)

            self.assertEqual(_state(path)["pending"], _LINES[1:])
            self.assertEqual(_state(path)["used"], _LINES[:1])

    def test_a_line_sharing_an_active_exit_ip_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            session = _IdentitySession(
                exit_ip_for={"1.1.1.1": "203.0.113.7", "2.2.2.2": "203.0.113.7"}
            )
            pool = _pool(path, session)

            first = pool.acquire_proxy()
            second = pool.acquire_proxy()

            self.assertEqual(first.exit_ip, "203.0.113.7")
            # Line 2 duplicates the active exit IP, so line 3 is served instead.
            self.assertEqual(second.proxy, _LINES[2])
            self.assertEqual(second.exit_ip, "3.3.3.3")
            self.assertEqual(_state(path)["pending"], [])

    def test_releasing_a_lease_frees_its_exit_ip_for_a_later_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES[:2])
            session = _IdentitySession(
                exit_ip_for={"1.1.1.1": "203.0.113.7", "2.2.2.2": "203.0.113.7"}
            )
            pool = _pool(path, session)

            first = pool.acquire_proxy()
            pool.release(first)
            second = pool.acquire_proxy()

            self.assertEqual(second.proxy, _LINES[1])
            self.assertEqual(second.exit_ip, "203.0.113.7")

    def test_exhausted_list_raises_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES[:1])
            pool = _pool(path, _IdentitySession())

            pool.acquire_proxy()
            with self.assertRaisesRegex(ProxyRotationError, "手动代理列表已用尽"):
                pool.acquire_proxy()

    def test_concurrent_acquires_never_hand_out_the_same_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession())
            leases = []
            errors = []
            barrier = threading.Barrier(3)

            def worker():
                barrier.wait()
                try:
                    leases.append(pool.acquire_proxy().proxy)
                except ProxyRotationError as exc:
                    errors.append(str(exc))

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(sorted(leases), sorted(_LINES))
            self.assertEqual(_state(path)["pending"], [])
            self.assertEqual(sorted(_state(path)["used"]), sorted(_LINES))

    def test_check_connection_probes_without_consuming(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, _LINES)
            pool = _pool(path, _IdentitySession())

            result = pool.check_connection()

            self.assertEqual(result["exit_ip"], "1.1.1.1")
            self.assertEqual(result["pending"], 3)
            # The label must not leak the proxy credentials.
            self.assertNotIn("u1", result["proxy"])
            self.assertNotIn("p1", result["proxy"])
            self.assertEqual(_state(path)["pending"], _LINES)
            self.assertEqual(_state(path)["used"], [])

    def test_empty_pending_list_is_rejected_at_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, [])
            config = json.loads(path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ProxyRotationError, "至少需要一行"):
                ManualProxyPool(config, config_path=path)


if __name__ == "__main__":
    unittest.main()
