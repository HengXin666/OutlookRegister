"""控制面租约与数据端点解析测试。"""

import socket
import threading
import unittest
from urllib.parse import urlsplit

import requests

from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from tests.proxy_rotation_fakes import FakeResponse
from tests.proxy_rotation_node_fakes import DeclaredNodeSession


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
        identity_calls = [
            call for call in pool._session.calls
            if call[0] == "GET" and call[1] == pool.identity_endpoint
        ]
        self.assertEqual(len(identity_calls), 1)
        pool.release(lease)
        self.assertEqual(local_data_plane.stopped, [1])

    def test_control_url_prefers_extracted_residential_endpoint_via_local_mihomo(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
            "max_rotate_retries": 0,
        })
        pool._session = DeclaredNodeSession(
            include_proxy=True,
            include_ws_endpoint=True,
            include_residential_endpoint=True,
            node_count=1,
        )

        class FakeLocalDataPlane:
            def __init__(self):
                self.started = []

            def start(self, node_index, endpoint):
                self.started.append((node_index, endpoint))
                return "http://127.0.0.1:2334"

            def stop(self, node_index):
                return None

        local_data_plane = FakeLocalDataPlane()
        pool._local_data_plane = local_data_plane

        lease = pool.acquire_proxy()

        self.assertEqual(lease.proxy, "http://127.0.0.1:2334")
        self.assertEqual(len(local_data_plane.started), 1)
        endpoint = local_data_plane.started[0][1]
        self.assertEqual(endpoint["protocol"], "http")
        self.assertEqual(endpoint["server"], "203.0.113.1")
        self.assertEqual(endpoint["port"], 8001)
        self.assertEqual(endpoint["transport"], "tcp")
        self.assertTrue(endpoint.get("username") and endpoint.get("password"))

    def test_control_url_leases_are_shared_across_pool_instances(self):
        control_url = "https://proxy.example/ctl/multi-pool-control-token"

        class IdentitySession(DeclaredNodeSession):
            def __init__(self, exit_ip):
                super().__init__(
                    include_proxy=False,
                    include_residential_endpoint=True,
                    node_count=2,
                )
                self.exit_ip = exit_ip

            def get(self, url, **kwargs):
                self.calls.append(("GET", url, kwargs))
                return FakeResponse(200, {
                    "success": True,
                    "ip": self.exit_ip,
                    "country_code": "US",
                    "timezone": {"id": "America/New_York"},
                })

        class BoundLocalDataPlane:
            def __init__(self):
                self.listeners = {}

            def start(self, node_index, endpoint):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    listener.bind(("127.0.0.1", 2334))
                except OSError:
                    listener.bind(("127.0.0.1", 0))
                listener.listen()
                self.listeners[node_index] = listener
                return f"http://127.0.0.1:{listener.getsockname()[1]}"

            def stop(self, node_index):
                listener = self.listeners.pop(node_index, None)
                if listener is not None:
                    listener.close()

        pools = []
        for suffix in (31, 32, 33):
            pool = RotatingProxyPool({
                "control_url": control_url,
                "max_rotate_retries": 0,
            })
            pool._session = IdentitySession(f"203.0.113.{suffix}")
            pool._local_data_plane = BoundLocalDataPlane()
            pools.append(pool)

        barrier = threading.Barrier(3)
        acquired = [None, None]
        failures = []

        def acquire(position):
            try:
                barrier.wait()
                acquired[position] = pools[position].acquire_proxy()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=acquire, args=(position,)) for position in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        first, second = acquired
        self.assertEqual({first.node_index, second.node_index}, {1, 2})
        self.assertNotEqual(urlsplit(first.proxy).port, urlsplit(second.proxy).port)
        with self.assertRaisesRegex(ProxyRotationError, "节点池已全部占用"):
            pools[2].acquire_proxy()

        pools[0].release(first)
        replacement = pools[2].acquire_proxy()
        self.assertEqual(replacement.node_index, first.node_index)
        pools[1].release(second)
        pools[2].release(replacement)

    def test_control_url_preserves_probe_error_from_running_local_mihomo(self):
        pool = RotatingProxyPool({
            "control_url": "https://proxy.example/ctl/automatic-control-token",
            "max_rotate_retries": 0,
        })

        class TimeoutSession(DeclaredNodeSession):
            def get(self, url, **kwargs):
                raise requests.exceptions.Timeout("identity probe timed out")

        class FakeLocalDataPlane:
            def start(self, node_index, endpoint):
                return "http://127.0.0.1:8443"

            def is_active(self, node_index):
                return True

            def failure_detail(self, node_index):
                return "connect error: residential gateway timed out"

            def stop(self, node_index):
                return None

        pool._session = TimeoutSession(
            include_proxy=False,
            include_ws_endpoint=True,
            node_count=1,
        )
        pool._local_data_plane = FakeLocalDataPlane()

        with self.assertRaises(ProxyRotationError) as raised:
            pool.acquire_proxy()

        message = str(raised.exception)
        self.assertIn("HX-ProxyGroup Listener 请求超时", message)
        self.assertIn("本机 Mihomo: connect error: residential gateway timed out", message)
        self.assertNotIn("无法连接 HX WebSocket 数据面", message)

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


if __name__ == "__main__":
    unittest.main()
