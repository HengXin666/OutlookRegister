import os
import time
import re
from datetime import datetime, timezone

from src.outlookregister.email.hx_email_client import HXEmailClient, HXEmailError


class _BaseRecoveryChallenge:
    """Microsoft 备用邮箱验证挑战的混合方法。"""

    def _save_recovery_diagnostic(self, page, name):
        stamp = int(time.time())
        base_path = os.path.join(self.results_dir, f'{name}_{stamp}')
        try:
            page.screenshot(path=f'{base_path}.png', full_page=True)
        except Exception:
            pass
        try:
            body = page.locator('body').inner_text(timeout=3000)
            inputs = page.locator('input').evaluate_all(
                "els => els.map(el => ({id: el.id, name: el.name, type: el.type, "
                "placeholder: el.placeholder, ariaLabel: el.getAttribute('aria-label')}))"
            )
            with open(f'{base_path}.txt', 'w', encoding='utf-8') as diagnostic:
                diagnostic.write(f'URL: {page.url}\nINPUTS: {inputs}\n\n{body}')
            print(f'[Recovery Email] - 页面诊断已保存: {base_path}.png/.txt')
        except Exception:
            pass

    def _fill_recovery_code(self, page, code):
        """Fill either Microsoft's single OTP field or its segmented six-field UI."""
        try:
            segmented = page.locator('input[id^="codeEntry-"]')
            visible_inputs = [
                segmented.nth(index)
                for index in range(segmented.count())
                if segmented.nth(index).is_visible()
            ]
        except Exception:
            visible_inputs = []
        if len(visible_inputs) >= len(code):
            for input_box in visible_inputs:
                try:
                    input_box.fill('')
                except Exception:
                    pass
            self.smooth_click(page, visible_inputs[0])
            page.keyboard.type(code, delay=120)
            return True

        code_input = self._recovery_code_input(page)
        if code_input is None:
            raise HXEmailError('Microsoft 安全代码输入框已消失，但验证尚未确认')
        code_input.fill(code)
        return False

    def confirm_recovery_email_challenge(
        self,
        page,
        hx_email,
        mailbox,
        recovery_email,
    ):
        """Confirm an existing recovery address using the shared Microsoft proof flow."""
        code_requested_at = datetime.now(timezone.utc)
        known_message_ids = None
        known_codes = None
        if isinstance(hx_email, HXEmailClient):
            try:
                known_candidates = hx_email.code_snapshot(mailbox)
                known_message_ids = {
                    str(candidate.get('message_id') or '').strip()
                    for candidate in known_candidates
                    if str(candidate.get('message_id') or '').strip()
                }
                known_codes = {
                    str(candidate.get('code') or '').strip()
                    for candidate in known_candidates
                    if str(candidate.get('code') or '').strip()
                }
                print(
                    f'[Recovery Code] 发送前已有验证码消息: '
                    f'{len(known_message_ids)} 条',
                    flush=True,
                )
            except HXEmailError as exc:
                print(
                    f'[Recovery Code] 无法建立发送前消息基线，将拒绝无时间戳的旧验证码: {exc}',
                    flush=True,
                )
        email_input = self._recovery_email_input(page)
        if email_input is not None:
            if not self._fill_recovery_email(page, recovery_email, email_input):
                raise HXEmailError('无法填写 Microsoft 密保邮箱输入框')
            submit = self._visible_first(page, (
                '[data-testid="primaryButton"]', '#idSIButton9', '#iNext',
                'button[type="submit"]', 'input[type="submit"]',
            ))
            if submit is None:
                raise HXEmailError('未找到 Microsoft 备用邮箱提交按钮')
            self.smooth_click(page, submit)

        code_input = None
        deadline = time.time() + 30
        while time.time() < deadline and code_input is None:
            code_input = self._recovery_code_input(page)
            if code_input is None:
                page.wait_for_timeout(500)
        if code_input is None:
            self._save_recovery_diagnostic(page, 'recovery_email_submit_failed')
            raise HXEmailError('Microsoft 未进入备用邮箱安全代码页面')

        print(
            f'[Recovery Code] 等待新验证码: mailbox={recovery_email}; '
            f'发送基线={code_requested_at.isoformat()}',
            flush=True,
        )
        used_codes = set()
        for attempt in range(1, self.recovery_code_attempts + 1):
            if isinstance(hx_email, HXEmailClient):
                code_details = hx_email.wait_for_code_details(
                    mailbox,
                    set(used_codes),
                    not_before=code_requested_at,
                    known_message_ids=known_message_ids,
                    known_codes=known_codes,
                )
                code = str(code_details.get('code') or '').strip()
                message_id = str(code_details.get('message_id') or '').strip()
                if message_id:
                    if known_message_ids is None:
                        known_message_ids = set()
                    known_message_ids.add(message_id)
                if known_codes is None:
                    known_codes = set()
                known_codes.add(code)
            else:
                # Keep lightweight test doubles and third-party compatible
                # clients working while the built-in client enforces timestamps.
                code = str(hx_email.wait_for_code(mailbox, set(used_codes))).strip()
                code_details = {
                    'code': code,
                    'received_at': None,
                    'message_id': '',
                }
            if not re.fullmatch(r'\d{6}', code):
                raise HXEmailError(f'HX-Email 返回的安全代码格式无效: {code!r}')
            used_codes.add(code)
            if not isinstance(hx_email, HXEmailClient):
                print(
                    f'[Recovery Code] 使用验证码: code={code}; '
                    f'received_at={code_details.get("received_at") or "unknown"}; '
                    f'message_id={code_details.get("message_id") or "unknown"}; '
                    f'mailbox={recovery_email}',
                    flush=True,
                )

            segmented = self._fill_recovery_code(page, code)
            submit = self._visible_first(page, (
                '[data-testid="primaryButton"]', '#idSIButton9', '#iNext',
                'button[type="submit"]', 'input[type="submit"]',
            ))
            if submit is not None:
                self.smooth_click(page, submit)
            elif not segmented:
                raise HXEmailError('未找到 Microsoft 安全代码确认按钮')
            page.wait_for_timeout(750)

            accepted, verification_detail = self._wait_for_recovery_confirmation(page)
            if accepted:
                return True
            if attempt >= self.recovery_code_attempts:
                self._save_recovery_diagnostic(page, 'recovery_email_code_rejected')
                raise HXEmailError(verification_detail)
            if isinstance(hx_email, HXEmailClient):
                try:
                    known_candidates = hx_email.code_snapshot(mailbox)
                    known_message_ids = {
                        str(candidate.get('message_id') or '').strip()
                        for candidate in known_candidates
                        if str(candidate.get('message_id') or '').strip()
                    }
                    known_codes = {
                        str(candidate.get('code') or '').strip()
                        for candidate in known_candidates
                        if str(candidate.get('code') or '').strip()
                    }
                except HXEmailError as exc:
                    known_message_ids = None
                    known_codes = None
                    print(
                        f'[Recovery Code] 无法建立重发前消息基线，将拒绝无时间戳的旧验证码: {exc}',
                        flush=True,
                    )
            resend_result = self._resend_recovery_code(page)
            if not resend_result:
                self._save_recovery_diagnostic(page, 'recovery_email_code_rejected')
                raise HXEmailError(verification_detail)
            code_requested_at = (
                resend_result
                if isinstance(resend_result, datetime)
                else datetime.now(timezone.utc)
            )
            print(
                f'[Recovery Email] - 第 {attempt} 个安全代码未通过，'
                f'已请求新代码（发送基线={code_requested_at.isoformat()}），'
                '旧代码不会再次使用。'
            )
        return False

    def handle_recovery_email(self, page):
        """Enroll an HX-Email temp address when Microsoft requires a recovery email."""
        self._set_traffic_page_stage(page, 'recovery_email', 'recovery_browser')
        if not self._wait_for_recovery_page(page):
            self._set_recovery_result()
            self._set_traffic_page_stage(page, 'residential_registration', 'residential_browser')
            return True
        if not self.recovery_email_enabled:
            self._set_recovery_result(reason='disabled', detail='recovery_email 未启用')
            print('[Error: Recovery Email] - Microsoft 要求备用邮箱，但 recovery_email 未启用。')
            self._set_traffic_page_stage(page, 'residential_registration', 'residential_browser')
            return False

        self._set_recovery_result(
            reason='binding_failed',
            detail='已检测到密保邮箱页面，但尚未完成绑定',
        )
        mailbox = None
        success = False
        detail = ''
        try:
            hx_email = self.get_flow_hx_email()
            mailbox = hx_email.apply_mailbox()
            recovery_email = mailbox.get('email')
            if not recovery_email:
                raise HXEmailError('HX-Email 未返回临时邮箱地址')
            self._set_recovery_result(
                recovery_email=recovery_email,
                reason='verification_failed',
                detail='尚未通过 Microsoft 验证',
                usable_email_id=mailbox.get('usable_email_id'),
                mailbox_mode=mailbox.get('mode', ''),
            )
            self.thread_local.recovery_mailbox = dict(mailbox)

            self.confirm_recovery_email_challenge(
                page,
                hx_email,
                mailbox,
                recovery_email,
            )

            success = True
            self.thread_local.recovery_email = recovery_email
            self._set_recovery_result(
                bound=True,
                recovery_email=recovery_email,
                reason='verified',
                usable_email_id=mailbox.get('usable_email_id'),
                mailbox_mode=mailbox.get('mode', ''),
            )
            print(f'[Success: Recovery Email] - {recovery_email}')
            return True
        except Exception as exc:
            detail = str(exc)
            current = getattr(self.thread_local, 'recovery_result', {})
            self._set_recovery_result(
                recovery_email=current.get('recovery_email', ''),
                reason=current.get('reason', 'verification_failed'),
                detail=detail,
                usable_email_id=current.get('usable_email_id'),
                mailbox_mode=current.get('mailbox_mode', ''),
            )
            print(f'[Error: Recovery Email] - {detail}')
            return False
        finally:
            if mailbox:
                self.get_flow_hx_email().finish_mailbox(mailbox, success, detail)
            self._set_traffic_page_stage(page, 'residential_registration', 'residential_browser')

    def _set_traffic_page_stage(self, page, stage, source):
        traffic = getattr(self, 'traffic', None)
        if traffic is not None:
            traffic.set_page_stage(page, stage, source)

    def get_recovery_email(self):
        return getattr(self.thread_local, 'recovery_email', '')

    def get_recovery_mailbox(self):
        return getattr(self.thread_local, 'recovery_mailbox', None)
