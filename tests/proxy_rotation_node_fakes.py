"""ProxyRotation 测试用的监听器与声明式节点假会话。"""

from urllib.parse import urlsplit

import requests

from tests.proxy_rotation_fakes import FakeResponse, FakeSession


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
    def __init__(
        self,
        *,
        include_proxy=True,
        include_ws_endpoint=False,
        include_residential_endpoint=False,
        node_count=2,
    ):
        super().__init__()
        self.include_proxy = include_proxy
        self.include_ws_endpoint = include_ws_endpoint
        self.include_residential_endpoint = include_residential_endpoint
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
            "residential_endpoint": ({
                "protocol": "http",
                "server": f"203.0.113.{index}",
                "port": 8000 + index,
                "username": f"node-{index}",
                "password": "secret",
                "tls": False,
            } if self.include_residential_endpoint else None),
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
        suffix = port - 8422
        if suffix < 1 or suffix > 254:
            suffix = 21
        return FakeResponse(200, {
            "success": True,
            "ip": f"203.0.113.{suffix}",
            "country_code": "US",
            "timezone": {"id": "America/New_York"},
        })
