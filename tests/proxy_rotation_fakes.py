"""ProxyRotation 测试共用的基础假会话与响应对象。"""


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
