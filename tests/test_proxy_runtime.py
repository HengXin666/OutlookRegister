import base64
import json
import unittest
from urllib.parse import quote

from proxy_runtime import (
    ProxyRuntimeError,
    advanced_proxy_from_uri,
    mihomo_config,
    parse_control_node,
    select_endpoint,
)


class ProxyRuntimeTests(unittest.TestCase):
    def test_vless_ws_uri_becomes_mihomo_proxy(self):
        uri = (
            "vless://00000000-0000-4000-8000-000000000001@proxy.example.com:443"
            "?encryption=none&security=tls&type=ws&sni=proxy.example.com"
            f"&host=proxy.example.com&path={quote('/__hx-proxy__/residential')}#node"
        )
        proxy = advanced_proxy_from_uri(uri)
        self.assertEqual(proxy["type"], "vless")
        self.assertEqual(proxy["server"], "proxy.example.com")
        self.assertEqual(proxy["port"], 443)
        self.assertEqual(proxy["network"], "ws")
        self.assertEqual(proxy["ws-opts"]["path"], "/__hx-proxy__/residential")
        self.assertEqual(proxy["ws-opts"]["headers"]["Host"], "proxy.example.com")
        self.assertTrue(proxy["tls"])

    def test_vmess_ws_uri_becomes_mihomo_proxy(self):
        payload = {
            "v": "2",
            "ps": "node",
            "add": "proxy.example.com",
            "port": "443",
            "id": "00000000-0000-4000-8000-000000000002",
            "aid": "0",
            "scy": "auto",
            "net": "ws",
            "host": "proxy.example.com",
            "path": "/ws",
            "tls": "tls",
            "sni": "proxy.example.com",
        }
        encoded = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        proxy = advanced_proxy_from_uri(f"vmess://{encoded}")
        self.assertEqual(proxy["type"], "vmess")
        self.assertEqual(proxy["alterId"], 0)
        self.assertEqual(proxy["ws-opts"]["path"], "/ws")

    def test_mihomo_config_is_loopback_only(self):
        config = mihomo_config(
            "trojan://secret@proxy.example.com:443?security=tls&type=ws"
            "&host=proxy.example.com&path=%2Fws&sni=proxy.example.com",
            23456,
        )
        self.assertEqual(config["mixed-port"], 23456)
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["allow-lan"])
        self.assertEqual(config["rules"], ["MATCH,HX-UPSTREAM"])

    def test_control_node_selects_preferred_vless_ws_endpoint(self):
        node = {
            "index": 1,
            "node_name": "residential-01",
            "proxy_url": None,
            "endpoints": [
                {
                    "protocol": "vless",
                    "transport": "ws",
                    "uri": (
                        "vless://00000000-0000-4000-8000-000000000001@proxy.example.com:443"
                        "?security=tls&type=ws&host=proxy.example.com&path=%2Fws&sni=proxy.example.com"
                    ),
                    "browser_compatible": False,
                }
            ],
        }
        parsed = parse_control_node(node)
        endpoint = select_endpoint(parsed, ("vless",))
        self.assertEqual(parsed.index, 1)
        self.assertEqual(endpoint.protocol, "vless")
        self.assertEqual(endpoint.transport, "ws")
        self.assertFalse(endpoint.browser_compatible)

    def test_control_node_accepts_legacy_browser_proxy_response(self):
        node = {"index": 1, "node_name": "legacy", "proxy_url": "http://user:pass@127.0.0.1:8080#legacy"}
        endpoint = select_endpoint(parse_control_node(node), ("http",))
        self.assertEqual(endpoint.uri, "http://user:pass@127.0.0.1:8080#legacy")
        self.assertTrue(endpoint.browser_compatible)

    def test_vless_endpoint_requires_tls_on_port_443(self):
        node = parse_control_node({
            "index": 1,
            "endpoints": [{
                "protocol": "vless",
                "transport": "ws",
                "uri": (
                    "vless://00000000-0000-4000-8000-000000000001@proxy.example.com:80"
                    "?security=none&type=ws&host=proxy.example.com&path=%2Fws"
                ),
            }],
        })
        with self.assertRaisesRegex(ProxyRuntimeError, "no preferred endpoint"):
            select_endpoint(node, ("vless",))

    def test_control_node_rejects_missing_supported_endpoint(self):
        with self.assertRaisesRegex(ProxyRuntimeError, "no supported endpoint"):
            parse_control_node({"index": 1, "node_name": "empty", "endpoints": []})


if __name__ == "__main__":
    unittest.main()
