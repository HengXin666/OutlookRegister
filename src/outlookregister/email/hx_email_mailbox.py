"""Mailbox lifecycle mixin for ``HXEmailClient``.

Creating (:meth:`apply_mailbox`), archiving (:meth:`finish_mailbox`) and
resolving (:meth:`resolve_mailbox`) a recovery mailbox. The actual HTTP
traffic is performed via the transport mixin's ``_request``/``_v1_request``.
"""

import uuid

from outlookregister.email.hx_email_base import HXEmailError


class _HXEmailMailbox:
    """External + session mailbox management for recovery flows."""

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
