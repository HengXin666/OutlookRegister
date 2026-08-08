"""HXEmailClient 测试共享的 HTTP 会话替身。"""

import threading
import time


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ConcurrentGroupSession:
    def __init__(self):
        self.calls = []
        self.group = None
        self.lock = threading.Lock()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET" and url.endswith("/api/v1/groups"):
            # Make overlapping GET-before-POST windows likely without making the
            # test depend on a particular thread scheduling order.
            time.sleep(0.01)
            with self.lock:
                groups = [self.group] if self.group is not None else []
            return FakeResponse(groups)
        if method == "POST" and url.endswith("/api/v1/groups"):
            with self.lock:
                if self.group is not None:
                    return FakeResponse({"detail": "duplicate group"}, 500)
                self.group = {
                    "id": 3,
                    "name": "OutlookRegister 自动注册",
                    "proxy_url": "http://127.0.0.1:2334",
                }
                return FakeResponse(self.group, 201)
        raise AssertionError(f"unexpected request {method} {url}")
