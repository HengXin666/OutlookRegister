import os
import json
import random
import threading
import time
import uuid

import requests
from traffic_tracker import stage_for_hx_email_path


class HXEmailError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class HXEmailClient:
    """Small client for HX-Email recovery mailbox operations."""

    def __init__(self, config, session=None):
        configured_url = str(config.get("base_url", "")).strip().rstrip("/")
        self.base_url = (
            configured_url[:-7]
            if configured_url.lower().endswith("/api/v1")
            else configured_url
        )
        self.prefer_session_api = configured_url.lower().endswith("/api/v1")
        self.api_key = os.getenv("HX_EMAIL_API_KEY", str(config.get("api_key", ""))).strip()
        self.username = os.getenv("HX_EMAIL_USERNAME", str(config.get("username", ""))).strip()
        self.password = os.getenv("HX_EMAIL_PASSWORD", str(config.get("password", ""))).strip()
        self.timeout = float(config.get("request_timeout_seconds", 15))
        self.code_timeout = float(config.get("code_timeout_seconds", 120))
        self.poll_interval = float(config.get("poll_interval_seconds", 3))
        self.caller_id = str(config.get("caller_id", "outlook-register")).strip()
        self.account_group = str(
            config.get("account_group", "OutlookRegister 自动注册")
        ).strip()
        self.account_group_color = str(config.get("account_group_color", "#238636")).strip()
        self.session = session or requests.Session()
        self.access_token = ""
        self._traffic_recorder = None
        self._account_group_lock = threading.Lock()
        self._account_group = None

    def set_traffic_recorder(self, recorder):
        self._traffic_recorder = recorder

    def api_headers(self):
        if not self.api_key:
            return {}
        if ":" in self.api_key:
            name, value = self.api_key.split(":", 1)
            if name.strip() and value.strip():
                return {name.strip(): value.strip()}
        return {"X-API-Key": self.api_key}

    def apply_mailbox(self):
        self._require_config()
        task_id = uuid.uuid4().hex
        if self.api_key and not self.prefer_session_api:
            payload = self._request(
                "POST",
                "/api/external/temp-emails/apply",
                headers=self.api_headers(),
                json={"caller_id": self.caller_id, "task_id": task_id},
            )
            data = self._external_data(payload, "申请临时邮箱")
            return {
                "email": str(data.get("email") or ""),
                "task_token": str(data.get("task_token") or ""),
                "usable_email_id": data.get("usable_email_id"),
                "mode": "external",
            }

        mailbox = self._v1_request(
            "POST",
            "/api/v1/temp-mail/cf/mailboxes",
            json={"address": None, "label": f"Outlook recovery {task_id[:8]}"},
            expected=(200, 201),
        )
        return {
            "email": str(mailbox.get("address") or ""),
            "task_token": "",
            "usable_email_id": mailbox.get("id"),
            "mode": "session",
        }

    def wait_for_code(self, mailbox, exclude_codes=None):
        # Give the newly sent message time to arrive before the first mailbox read.
        time.sleep(random.uniform(3, 5))
        deadline = time.monotonic() + self.code_timeout
        last_error = None
        excluded = {str(code).strip() for code in (exclude_codes or ())}
        while time.monotonic() < deadline:
            try:
                code = self._read_code(mailbox)
                if code and code not in excluded:
                    return code
            except HXEmailError as exc:
                last_error = exc
            time.sleep(self.poll_interval)
        detail = f": {last_error}" if last_error else ""
        raise HXEmailError(f"等待 Microsoft 安全代码超时{detail}")

    def finish_mailbox(self, mailbox, success, detail=""):
        task_token = mailbox.get("task_token")
        if task_token and self.api_key:
            try:
                self._request(
                    "POST",
                    f"/api/external/temp-emails/{task_token}/finish",
                    headers=self.api_headers(),
                    json={"result": "success" if success else "failed", "detail": detail},
                )
            except HXEmailError:
                pass

    def import_outlook_account(
        self,
        email,
        password,
        recovery_email,
        client_id,
        refresh_token,
        proxy_url="",
    ):
        group = self._ensure_account_group(proxy_url)
        imported = self._v1_request(
            "POST",
            "/api/v1/email-accounts/import",
            json={
                "text": f"{email}----{password}----{client_id}----{refresh_token}",
                "provider": "outlook",
                "group_id": group["id"],
                "duplicate_strategy": "overwrite",
                "add_to_pool": False,
            },
            expected=(200, 201),
        )
        if imported.get("failed"):
            raise HXEmailError(f"HX-Email 导入账号失败: {imported.get('errors')}")

        search = self._v1_request(
            "GET",
            "/api/v1/email-accounts/search",
            params={"q": email},
        )
        account = next(
            (
                item for item in search.get("accounts", [])
                if str(item.get("primary_address", "")).lower() == email.lower()
            ),
            None,
        )
        if not account:
            raise HXEmailError("HX-Email 导入后未找到账号")

        remark = (
            f"登录密码: {password}\n"
            f"密保邮箱: {recovery_email or '未触发'}\n"
            "OAuth2: 已授权并导入 refresh_token\n"
            "来源: OutlookRegister 自动注册"
        )
        account_id = account["id"]
        updated = self._v1_request(
            "PUT",
            f"/api/v1/email-accounts/{account_id}",
            json={
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "group_id": group["id"],
                "remark": remark,
                "status": "active",
                "provider": "outlook",
            },
        )
        usable_email = updated.get("primary_usable_email") or {}
        usable_email_id = usable_email.get("id")
        if not usable_email_id:
            raise HXEmailError("HX-Email 账号缺少主可用邮箱 ID")
        try:
            self._v1_request(
                "POST",
                "/api/v1/mail-pool/entries",
                json={"usable_email_id": usable_email_id},
                expected=(200, 201),
            )
        except HXEmailError as exc:
            if exc.status_code != 409:
                raise

        authorization = self._v1_request(
            "POST",
            f"/api/v1/email-accounts/{account_id}/refresh",
        )
        if not authorization.get("success"):
            raise HXEmailError(
                f"HX-Email OAuth2 授权验证失败: {authorization.get('message')}"
            )
        return {
            "account_id": account_id,
            "group_id": group["id"],
            "usable_email_id": usable_email_id,
        }

    def _ensure_account_group(self, proxy_url):
        with self._account_group_lock:
            if self._account_group is not None:
                return self._account_group

            group = self._find_account_group()
            if group is None:
                try:
                    group = self._v1_request(
                        "POST",
                        "/api/v1/groups",
                        json={
                            "name": self.account_group,
                            "color": self.account_group_color,
                            "proxy_url": proxy_url,
                        },
                        expected=(200, 201),
                    )
                except HXEmailError as exc:
                    # HX-Email enforces a unique (user, name) group constraint but
                    # currently exposes a concurrent duplicate as HTTP 500.
                    if exc.status_code not in (409, 500):
                        raise
                    group = self._find_account_group()
                    if group is None:
                        raise

            if not isinstance(group, dict) or not group.get("id"):
                raise HXEmailError("HX-Email 分组响应缺少 ID")
            self._account_group = group
            return group

    def _find_account_group(self):
        groups = self._v1_request("GET", "/api/v1/groups")
        if not isinstance(groups, list):
            raise HXEmailError("HX-Email 分组响应格式无效")
        return next(
            (
                group
                for group in groups
                if isinstance(group, dict)
                and str(group.get("name", "")) == self.account_group
            ),
            None,
        )

        usable_email_id = mailbox.get("usable_email_id")
        if usable_email_id and (
            self.prefer_session_api or self.access_token or (self.username and self.password)
        ):
            try:
                self._v1_request(
                    "POST",
                    f"/api/v1/temp-mail/{usable_email_id}/archive",
                )
            except HXEmailError:
                pass

    def _read_code(self, mailbox):
        email = mailbox["email"]
        if self.api_key and mailbox.get("mode") == "external":
            payload = self._request(
                "GET",
                "/api/external/verification-code",
                headers=self.api_headers(),
                params={
                    "email": email,
                    "from_contains": "Microsoft",
                    "since_minutes": 10,
                    "code_length": 6,
                },
            )
            data = self._external_data(payload, "读取安全代码")
            code = str(data.get("verification_code") or "").strip()
            if code:
                return code

        usable_email_id = mailbox.get("usable_email_id")
        if usable_email_id and (
            self.prefer_session_api or self.username and self.password
        ):
            payload = self._v1_request(
                "GET",
                f"/api/v1/temp-mail/{usable_email_id}/codes",
            )
            codes = payload.get("codes") or []
            if codes:
                return str(codes[-1].get("code") or "").strip()
        return ""

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

    def _external_data(self, payload, action):
        if not payload.get("success"):
            raise HXEmailError(f"HX-Email {action}失败: {payload.get('message') or 'unknown error'}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HXEmailError(f"HX-Email {action}响应缺少 data")
        return data

    def _require_config(self):
        if not self.base_url:
            raise HXEmailError("recovery_email.hx_email.base_url 不能为空")
        if not self.api_key and not (self.username and self.password):
            raise HXEmailError("请配置 HX-Email api_key 或 username/password")
