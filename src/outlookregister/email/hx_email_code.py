"""Verification code candidate parsing and validation mixin.

Reads verification codes from the mailbox, normalizes the raw provider
response into a stable candidate shape and validates candidate freshness
against the send-time baseline. None of these helpers depend on the
``time.sleep``/``random.uniform`` module globals that callers patch, so they
live safely outside the composite client module.
"""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from src.outlookregister.email.hx_email_base import HXEmailError


class _HXEmailCode:
    """Code reading, candidate normalization and timestamp validation."""

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
