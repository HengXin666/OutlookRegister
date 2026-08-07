"""Composite HX-Email recovery mailbox client.

The client is assembled from focused mixin modules (transport, import,
mailbox, code) plus the base configuration mixin. ``wait_for_code`` and
``wait_for_code_details`` remain here because integration tests patch the
module-level ``time.sleep``/``random.uniform`` globals through this module's
name (the top-level shim redirects ``hx_email_client`` to this module).
"""

import random
import re
import time

# NOTE: ``stage_for_hx_email_path`` is imported by the transport mixin. It is
# kept importable here too for backwards-compatible callers and tests that may
# reference ``hx_email_client.stage_for_hx_email_path``.
from outlookregister.email.hx_email_base import HXEmailError, _HXEmailBase
from outlookregister.email.hx_email_code import _HXEmailCode
from outlookregister.email.hx_email_import import _HXEmailImport
from outlookregister.email.hx_email_mailbox import _HXEmailMailbox
from outlookregister.email.hx_email_transport import _HXEmailTransport


class HXEmailClient(_HXEmailTransport, _HXEmailImport, _HXEmailMailbox, _HXEmailCode, _HXEmailBase):
    """Small client for HX-Email recovery mailbox operations.

    Public mailbox/code/import methods are provided by the mixins above; only
    the polling entry points that depend on patchable module globals live in
    this file.
    """

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
