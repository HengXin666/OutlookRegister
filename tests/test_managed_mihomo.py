import shutil
import socket
import unittest
from urllib.parse import urlsplit

from outlookregister.proxy.managed_mihomo import (
    ManagedMihomo,
    ManagedMihomoError,
    _available_loopback_port,
    build_mihomo_config,
)


class ManagedMihomoConfigTests(unittest.TestCase):
    def test_builds_supported_websocket_protocols(self):
        endpoints = {
            "vless": (
                "vless://550e8400-e29b-41d4-a716-446655440000@proxy.example.com:443"
                "?security=tls&type=ws&host=proxy.example.com&path=%2Fedge%2Fvless"
            ),
            "trojan": (
                "trojan://secret@proxy.example.com:443"
                "?security=tls&type=ws&host=proxy.example.com&path=%2Fedge%2Ftrojan"
            ),
            "vmess": (
                "vmess://eyJ2IjoiMiIsInBzIjoibm9kZSIsImFkZCI6InByb3h5LmV4YW1wbGUuY29tIiw"
                "icG9ydCI6IjQ0MyIsImlkIjoiNTUwZTg0MDAtZTI5Yi00MWQ0LWE3MTYtNDQ2NjU1NDQ"
                "wMDAwIiwiYWlkIjoiMCIsIm5ldCI6IndzIiwiaG9zdCI6InByb3h5LmV4YW1wbGUuY2"
                "9tIiwicGF0aCI6Ii9lZGdlL3ZtZXNzIiwidGxzIjoidGxzIiwic25pIjoicHJveHkuZX"
                "hhbXBsZS5jb20ifQ"
            ),
        }

        for protocol, uri in endpoints.items():
            with self.subTest(protocol=protocol):
                config = build_mihomo_config({
                    "protocol": protocol,
                    "transport": "ws",
                    "uri": uri,
                }, 17891)
                self.assertEqual(config["listeners"][0]["listen"], "127.0.0.1")
                self.assertEqual(config["listeners"][0]["port"], 17891)
                self.assertEqual(config["proxies"][0]["type"], protocol)
                self.assertEqual(config["proxies"][0]["network"], "ws")
                self.assertTrue(config["proxies"][0]["tls"])
                self.assertEqual(config["log-level"], "warning")
                self.assertEqual(config["dns"]["enhanced-mode"], "redir-host")
                # 住宅数据面解析优先系统 DNS，DoH 仅作回退（DoH 在部分网络不可达）。
                self.assertEqual(
                    config["dns"]["proxy-server-nameserver"],
                    [
                        "system",
                        "https://1.1.1.1/dns-query",
                        "https://8.8.8.8/dns-query",
                    ],
                )
                self.assertEqual(
                    config["dns"]["nameserver"],
                    [
                        "system",
                        "https://1.1.1.1/dns-query",
                        "https://8.8.8.8/dns-query",
                    ],
                )

    def test_rejects_non_websocket_or_unknown_endpoint(self):
        for endpoint in (
            {"protocol": "vless", "transport": "tcp", "uri": "vless://id@example.com:443"},
            {"protocol": "hysteria2", "transport": "ws", "uri": "hysteria2://id@example.com:443"},
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ManagedMihomoError):
                    build_mihomo_config(endpoint, 17891)

    def test_builds_direct_residential_http_and_socks_endpoints(self):
        for protocol, tls in (("http", False), ("http", True), ("socks5", False)):
            with self.subTest(protocol=protocol, tls=tls):
                config = build_mihomo_config({
                    "protocol": protocol,
                    "transport": "tcp",
                    "server": "11.22.33.44",
                    "port": 8000,
                    "username": "node-user",
                    "password": "node-password",
                    "tls": tls,
                }, 2334)
                proxy = config["proxies"][0]
                self.assertEqual(proxy["type"], protocol)
                self.assertEqual(proxy["server"], "11.22.33.44")
                self.assertEqual(proxy["port"], 8000)
                self.assertTrue(proxy.get("username") and proxy.get("password"))
                self.assertEqual(proxy.get("tls", False), tls)

    def test_preferred_loopback_port_falls_back_when_already_bound(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            preferred = int(occupied.getsockname()[1])
            selected = _available_loopback_port(preferred)
        self.assertNotEqual(selected, preferred)
        self.assertGreater(selected, 0)

    @unittest.skipUnless(shutil.which("mihomo"), "mihomo is not installed")
    def test_real_mihomo_starts_on_loopback_and_stops(self):
        manager = ManagedMihomo(start_timeout=5)
        try:
            proxy_url = manager.start(1, {
                "protocol": "vless",
                "transport": "ws",
                "uri": (
                    "vless://550e8400-e29b-41d4-a716-446655440000@proxy.example.com:443"
                    "?security=tls&type=ws&host=proxy.example.com&path=%2Fedge%2Fvless"
                ),
            })
            parsed = urlsplit(proxy_url)
            self.assertEqual(parsed.scheme, "http")
            self.assertEqual(parsed.hostname, "127.0.0.1")
            self.assertGreater(parsed.port or 0, 0)
        finally:
            manager.close()

    @unittest.skipUnless(shutil.which("mihomo"), "mihomo is not installed")
    def test_real_mihomo_accepts_extracted_residential_endpoint(self):
        manager = ManagedMihomo(start_timeout=5)
        try:
            proxy_url = manager.start(1, {
                "protocol": "http",
                "transport": "tcp",
                "server": "203.0.113.10",
                "port": 8000,
                "username": "node-user",
                "password": "node-password",
                "tls": False,
            })
            parsed = urlsplit(proxy_url)
            self.assertEqual(parsed.scheme, "http")
            self.assertEqual(parsed.hostname, "127.0.0.1")
            self.assertGreater(parsed.port or 0, 0)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
