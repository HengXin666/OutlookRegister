import unittest
import shutil
from urllib.parse import urlsplit

from managed_mihomo import ManagedMihomo, ManagedMihomoError, build_mihomo_config


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

    def test_rejects_non_websocket_or_unknown_endpoint(self):
        for endpoint in (
            {"protocol": "vless", "transport": "tcp", "uri": "vless://id@example.com:443"},
            {"protocol": "hysteria2", "transport": "ws", "uri": "hysteria2://id@example.com:443"},
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ManagedMihomoError):
                    build_mihomo_config(endpoint, 17891)

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


if __name__ == "__main__":
    unittest.main()
