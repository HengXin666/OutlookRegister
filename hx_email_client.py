import os
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
        try:
            self.code_timestamp_skew_seconds = max(
                0.0, float(config.get("code_timestamp_skew_seconds", 15))
            )
        except (TypeError, ValueError):
            self.code_timestamp_skew_seconds = 15.0
        try:
            configured_max_age = float(
                config.get("code_max_age_seconds", max(self.code_timeout, 300.0))
            )
        except (TypeError, ValueError):
            configured_max_age = max(self.code_timeout, 300.0)
        self.code_max_age_seconds = max(1.0, configured_max_age)
        self.caller_id = str(config.get("caller_id", "outlook-register")).strip()
        self.account_group = str(
            config.get("account_group", "OutlookRegister 自动注册")
        ).strip()
        self.account_group_color = str(config.get("account_group_color", "#238636")).strip()
        self.session = session or requests.Session()
        try:
            self.session.trust_env = False
        except Exception:
            pass
        self.access_token = ""
        self._traffic_recorder = None
        self._account_group_lock = threading.Lock()
        self._account_group = None

    def set_traffic_recorder(self, recorder):
        self._traffic_recorder = recorder

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

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

    def wait_for_code(self, mailbox, exclude_codes=None, not_before=None):
        """Wait for a code while retaining the legacy string-only return value.

        The recovery flow passes ``not_before`` and uses the metadata-aware
        implementation below. Calls without a baseline keep the old API
        contract for integrations that only need a code string.
        """
        if not_before is not None:
            return self.wait_for_code_details(
                mailbox,
                exclude_codes=exclude_codes,
                not_before=not_before,
            )["code"]

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

    def wait_for_code_details(
        self,
        mailbox,
        exclude_codes=None,
        not_before=None,
        known_message_ids=None,
        known_codes=None,
    ):
        """Wait for a newly received six-digit code and return its metadata.

        ``not_before`` is the instant Microsoft was asked to send the code. A
        mailbox may contain older messages, so a code without a usable message
        timestamp is accepted only when its message ID was absent from the
        pre-send snapshot. This fallback is needed for older HX-Email servers
        whose ``/codes`` response exposes IDs but not provider timestamps.
        """
        baseline = self._coerce_datetime(not_before)
        if baseline is None:
            raise HXEmailError("等待验证码时缺少有效的发送时间基线")

        # Give the newly sent message time to arrive before the first mailbox read.
        time.sleep(random.uniform(3, 5))
        deadline = time.monotonic() + self.code_timeout
        excluded = {str(code).strip() for code in (exclude_codes or ())}
        known_ids = (
            {str(value).strip() for value in known_message_ids if str(value).strip()}
            if known_message_ids is not None
            else None
        )
        known_code_values = (
            {str(value).strip() for value in known_codes if str(value).strip()}
            if known_codes is not None
            else None
        )
        observed = set()
        rejected = set()
        last_reason = ""

        while time.monotonic() < deadline:
            try:
                candidates = self._read_code_candidates(mailbox)
            except HXEmailError as exc:
                last_reason = str(exc)
                time.sleep(self.poll_interval)
                continue

            accepted = []
            now = self._utc_now()
            for candidate in candidates:
                candidate = dict(candidate)
                candidate.setdefault("observed_at", now.isoformat())
                observation_key = (
                    candidate.get("message_id", ""),
                    candidate.get("code", ""),
                    candidate.get("received_at", ""),
                )
                if observation_key not in observed:
                    observed.add(observation_key)
                    self._log_code_event("获取到验证码", candidate, mailbox=mailbox)

                code = str(candidate.get("code") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    rejection_key = (*observation_key, "format")
                    if rejection_key not in rejected:
                        rejected.add(rejection_key)
                        self._log_code_event(
                            "丢弃验证码",
                            candidate,
                            "格式不是六位数字",
                            mailbox=mailbox,
                        )
                    last_reason = "HX-Email 返回了无效的安全代码格式"
                    continue
                if code in excluded:
                    rejection_key = (*observation_key, "excluded")
                    if rejection_key not in rejected:
                        rejected.add(rejection_key)
                        self._log_code_event(
                            "忽略验证码",
                            candidate,
                            "该验证码已经尝试过",
                            mailbox=mailbox,
                        )
                    last_reason = "HX-Email 返回了已经尝试过的安全代码"
                    continue

                valid, reason = self._validate_code_timestamp(
                    candidate,
                    baseline,
                    now,
                    known_ids,
                    known_code_values,
                )
                if not valid:
                    rejection_key = (*observation_key, reason)
                    if rejection_key not in rejected:
                        rejected.add(rejection_key)
                        self._log_code_event(
                            "丢弃验证码",
                            candidate,
                            reason,
                            mailbox=mailbox,
                        )
                    last_reason = reason
                    continue
                accepted.append(candidate)

            if accepted:
                selected = max(accepted, key=self._candidate_sort_key)
                self._log_code_event(
                    "使用验证码",
                    selected,
                    f"发送基线={baseline.isoformat()}",
                    mailbox=mailbox,
                )
                return {
                    key: value
                    for key, value in selected.items()
                    if not key.startswith("_")
                }
            time.sleep(self.poll_interval)

        detail = f"；最近原因={last_reason}" if last_reason else ""
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

    def resolve_mailbox(self, email, mailbox_hint=None):
        """Resolve a previously created temp mailbox so later codes remain readable."""
        normalized_email = str(email or "").strip()
        if not normalized_email:
            raise HXEmailError("密保邮箱地址不能为空")
        hint = dict(mailbox_hint or {})
        if hint.get("usable_email_id"):
            return {
                "email": normalized_email,
                "task_token": str(hint.get("task_token") or ""),
                "usable_email_id": hint["usable_email_id"],
                "mode": str(hint.get("mode") or "session"),
            }
        if self.api_key and not self.prefer_session_api:
            return {
                "email": normalized_email,
                "task_token": "",
                "usable_email_id": None,
                "mode": "external",
            }

        payload = self._v1_request(
            "GET",
            "/api/v1/workbench/usable-emails",
            params={
                "kind": "temp",
                "keyword": normalized_email,
                "page": 1,
                "page_size": 200,
            },
        )
        usable_emails = payload.get("usable_emails") or []
        matched = next(
            (
                item
                for item in usable_emails
                if str(item.get("address") or "").strip().casefold()
                == normalized_email.casefold()
            ),
            None,
        )
        if not matched or not matched.get("id"):
            raise HXEmailError(f"HX-Email 中未找到密保邮箱 {normalized_email}")
        return {
            "email": normalized_email,
            "task_token": "",
            "usable_email_id": matched["id"],
            "mode": "session",
        }

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

    def _read_code(self, mailbox):
        """Return the newest valid code for legacy callers."""
        candidates = self._read_code_candidates(mailbox)
        valid = [
            candidate
            for candidate in candidates
            if re.fullmatch(r"\d{6}", str(candidate.get("code") or "").strip())
        ]
        if not valid:
            return ""
        selected = max(valid, key=self._candidate_sort_key)
        return str(selected["code"]).strip()

    def _read_code_candidates(self, mailbox):
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
            candidates = self._candidate_items(data)
            if candidates:
                return self._normalize_code_candidates(candidates, "external", data)

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
                return self._normalize_code_candidates(codes, "session", payload)
        return []

    def code_message_ids(self, mailbox):
        """Return the message IDs visible before a verification request."""
        return {
            str(candidate.get("message_id") or "").strip()
            for candidate in self._read_code_candidates(mailbox)
            if str(candidate.get("message_id") or "").strip()
        }

    def code_snapshot(self, mailbox):
        """Return code candidates visible at one point in time."""
        return [dict(candidate) for candidate in self._read_code_candidates(mailbox)]

    @classmethod
    def _candidate_items(cls, data):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("codes", "messages", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(
            key in data
            for key in (
                "code",
                "verification_code",
                "verificationCode",
                "otp",
            )
        ):
            return [data]
        return []

    @classmethod
    def _normalize_code_candidates(cls, items, source, envelope):
        fallback_timestamp = cls._timestamp_from_item(envelope)
        candidates = []
        for position, item in enumerate(items):
            if isinstance(item, str):
                item = {"code": item}
            if not isinstance(item, dict):
                continue
            containers = [item]
            for nested_key in ("message", "email", "mail"):
                nested = item.get(nested_key)
                if isinstance(nested, dict):
                    containers.append(nested)
            code = ""
            message_id = ""
            received_at_value = None
            for container in containers:
                if not code:
                    for key in (
                        "code",
                        "verification_code",
                        "verificationCode",
                        "otp",
                    ):
                        value = container.get(key)
                        if value not in (None, ""):
                            code = str(value).strip()
                            break
                if not message_id:
                    for key in (
                        "message_id",
                        "messageId",
                        "email_id",
                        "emailId",
                        "matched_email_id",
                        "matchedEmailId",
                        "uid",
                        "id",
                    ):
                        value = container.get(key)
                        if value not in (None, ""):
                            message_id = str(value).strip()
                            break
                if received_at_value is None:
                    received_at_value = cls._timestamp_from_item(container)
            received_at = cls._coerce_datetime(received_at_value)
            if received_at is None:
                received_at = fallback_timestamp
            if not code:
                continue
            candidates.append(
                {
                    "code": code,
                    "received_at": cls._format_timestamp(received_at),
                    "message_id": message_id,
                    "source": source,
                    "_received_at": received_at,
                    # Older HX-Email servers omit mail timestamps. Their
                    # response is newest-first, so retain that order.
                    "_position": position,
                }
            )
        return candidates

    def _candidate_sort_key(self, candidate):
        received_at = self._coerce_datetime(
            candidate.get("_received_at") or candidate.get("received_at")
        )
        try:
            position = int(candidate.get("_position", 0))
        except (TypeError, ValueError):
            position = 0
        if received_at is not None:
            return (1, received_at, -position)
        return (0, datetime.min.replace(tzinfo=timezone.utc), -position)

    @classmethod
    def _timestamp_from_item(cls, item):
        if not isinstance(item, dict):
            return None
        for key in (
            "received_at",
            "receivedAt",
            "received_time",
            "receivedTime",
            "receivedDateTime",
            "created_at",
            "createdAt",
            "sent_at",
            "sentAt",
            "timestamp",
            "email_date",
            "message_date",
            "date",
            "created",
        ):
            value = item.get(key)
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def _coerce_datetime(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 100_000_000_000:
                numeric /= 1000
            try:
                parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(text)
                except (TypeError, ValueError, OverflowError):
                    return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_timestamp(value):
        return value.isoformat() if value is not None else None

    @staticmethod
    def _utc_now():
        return datetime.now(timezone.utc)

    def _validate_code_timestamp(
        self,
        candidate,
        baseline,
        now,
        known_message_ids=None,
        known_codes=None,
    ):
        received_at = candidate.get("_received_at")
        if received_at is None:
            message_id = str(candidate.get("message_id") or "").strip()
            code = str(candidate.get("code") or "").strip()
            if known_message_ids is not None and message_id:
                if message_id not in known_message_ids:
                    return True, ""
                if known_codes is not None and code not in known_codes:
                    return True, ""
            return False, "验证码缺少邮件接收时间，无法确认是本次发送"
        if received_at < baseline - self._timestamp_delta():
            return False, f"验证码时间早于本次发送基线（{candidate.get('received_at')}）"
        if received_at > now + self._timestamp_delta():
            return False, f"验证码时间晚于当前时间（{candidate.get('received_at')}）"
        age = (now - received_at).total_seconds()
        if age > self.code_max_age_seconds:
            return False, f"验证码已超过允许时效（{candidate.get('received_at')}）"
        return True, ""

    def _timestamp_delta(self):
        return timedelta(seconds=self.code_timestamp_skew_seconds)

    @staticmethod
    def _log_code_event(event, candidate, detail="", mailbox=None):
        code = str(candidate.get("code") or "<empty>")
        received_at = candidate.get("received_at") or "unknown"
        observed_at = candidate.get("observed_at") or "unknown"
        message_id = str(candidate.get("message_id") or "unknown")
        source = str(candidate.get("source") or "unknown")
        mailbox_email = str((mailbox or {}).get("email") or "unknown")
        suffix = f"; {detail}" if detail else ""
        print(
            f"[Recovery Code] {event}: code={code}; "
            f"received_at={received_at}; observed_at={observed_at}; "
            f"message_id={message_id}; "
            f"source={source}; mailbox={mailbox_email}{suffix}",
            flush=True,
        )

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
