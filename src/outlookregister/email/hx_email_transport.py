"""HTTP transport mixin for ``HXEmailClient``.

Owns login, the v1 retry wrapper and the low-level request that records
traffic to the dashboard recorder.
"""

import json

import requests

from outlookregister.dashboard.traffic_tracker import stage_for_hx_email_path
from outlookregister.email.hx_email_base import HXEmailError


class _HXEmailTransport:
    """HTTP request helpers shared by every higher-level mixin."""

    def _login(self):
        if not self.username or not self.password:
            raise HXEmailError("HX-Email 外部接口暂时无法读取临时邮箱，请配置 username/password 兼容读取")
        payload = self._request(
            "POST",
            "/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
        )
        self.access_token = str(payload.get("access_token") or "")
        if not self.access_token:
            raise HXEmailError("HX-Email 登录响应缺少 access_token")
        return self.access_token

    def _v1_request(self, method, path, **kwargs):
        headers = self.api_headers() if self.prefer_session_api and self.api_key else {}
        if not headers:
            token = self.access_token or self._login()
            headers = {"Authorization": f"Bearer {token}"}
        try:
            return self._request(method, path, headers=headers, **kwargs)
        except HXEmailError as exc:
            if exc.status_code not in (401, 403) or not (self.username and self.password):
                raise
        token = self._login()
        return self._request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )

    def _request(self, method, path, expected=(200,), **kwargs):
        request_bytes = self._request_body_size(kwargs)
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise HXEmailError(f"HX-Email 请求失败: {exc}") from exc
        if self._traffic_recorder is not None:
            self._traffic_recorder.record_http(
                stage_for_hx_email_path(path),
                "hx_email_api",
                bytes_sent=request_bytes,
                bytes_received=self._response_size(response),
            )
        if response.status_code not in expected:
            raise HXEmailError(
                f"HX-Email HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise HXEmailError("HX-Email 返回了无效 JSON") from exc

    @staticmethod
    def _request_body_size(kwargs):
        if kwargs.get("data") is not None:
            data = kwargs["data"]
            if isinstance(data, bytes):
                return len(data)
            return len(str(data).encode("utf-8"))
        if kwargs.get("json") is not None:
            try:
                return len(json.dumps(kwargs["json"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            except (TypeError, ValueError):
                return 0
        return 0

    @staticmethod
    def _response_size(response):
        content = getattr(response, "content", None)
        if isinstance(content, bytes):
            return len(content)
        text = getattr(response, "text", "")
        return len(str(text).encode("utf-8"))
