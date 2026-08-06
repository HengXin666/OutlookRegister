import os
import time
import json
from datetime import datetime, timezone


class _BaseRecovery:
    """Microsoft 验证回复邮箱验证流程的混合方法。"""

    def _visible_first(self, page, selectors):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                pass
        return None

    def _recovery_page_visible(self, page):
        try:
            url = (page.url or '').lower()
            body = ' '.join(page.locator('body').inner_text(timeout=3000).split()).lower()
        except Exception:
            return False
        markers = (
            'protect your account', 'recovery email', 'alternate email',
            'add an email address', 'security info',
            'adresse e-mail de récupération', 'correo de recuperación',
            'wiederherstellungs-e-mail', 'email de recuperação',
            'email di recupero', 'herstel-e-mailadres',
            '辅助邮箱', '恢复邮箱', '备用邮箱', '添加电子邮件',
            '让我们来保护你的帐户', '保护你的帐户',
            '協助我們保護您的帳戶', '復原電子郵件', '備用電子郵件',
            '回復用メール', '복구 이메일',
        )
        return '/proofs/add' in url or any(marker in body for marker in markers)

    def _recovery_code_input(self, page):
        return self._visible_first(page, (
            '#proof-confirmation-code-input', '#otc-confirmation-input',
            'input[id^="codeEntry-"]', '#iOttText',
            'input[autocomplete="one-time-code"]',
            'input[name="otc"]', 'input[name="VerificationCode"]',
            'input[name="ProofConfirmationCode"]',
            'input[name="ProofConfirmation"]', 'input[name="code"]',
            'input[inputmode="numeric"]', 'input[aria-label*="code" i]',
            'input[aria-label*="代码"]', 'input[placeholder*="code" i]',
            'input[placeholder*="代码"]',
        ))

    def _recovery_email_input(self, page):
        return self._visible_first(page, (
            '#proof-confirmation-email-input',
            'input[name="proofConfirmationEmail"]',
            'input[name="ProofConfirmationEmail"]',
            'input[data-testid="proof-confirmation-email-input"]',
            'input[autocomplete="email"]',
            'input[name="EmailAddress"]',
            'input[name="proof"]',
            'input[type="email"]',
            'input[placeholder*="example.com" i]',
        ))

    def _fill_recovery_email(self, page, recovery_email, email_input=None):
        email_input = email_input or self._recovery_email_input(page)
        if email_input is None:
            return False
        try:
            email_input.fill(recovery_email, timeout=8000)
        except Exception:
            try:
                email_input.fill(recovery_email)
            except Exception:
                pass
        try:
            value = str(email_input.input_value(timeout=1000) or '').strip()
        except Exception:
            value = ''
        if value.casefold() != str(recovery_email).strip().casefold():
            try:
                self.smooth_click(page, email_input)
                page.keyboard.press('Control+A')
                page.keyboard.type(recovery_email, delay=40)
            except Exception:
                return False
        return True

    def _wait_for_recovery_page(self, page, timeout_seconds=5):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._recovery_page_visible(page):
                return True
            if self._visible_first(page, (
                '[aria-label="新邮件"]', '[aria-label="New mail"]',
                '[aria-label="新郵件"]',
            )) is not None:
                return False
            page.wait_for_timeout(500)
        return self._recovery_page_visible(page)

    def _recovery_error(self, page):
        try:
            body = ' '.join(page.locator('body').inner_text(timeout=3000).split()).lower()
        except Exception:
            return ''
        markers = (
            "that code didn't work", 'code is incorrect', 'incorrect code',
            'invalid code', 'code has expired', 'code expired',
            'enter the code again',
            '该代码不起作用', '代码不正确', '验证码不正确', '验证码错误',
            '代码已过期', '验证码已过期', '请重新输入',
            '此代碼無效', '驗證碼不正確', '驗證碼錯誤', '代碼已過期',
        )
        return next((marker for marker in markers if marker in body), '')

    def _wait_for_recovery_confirmation(self, page, timeout_seconds=20):
        """Return only after Microsoft clearly accepts or rejects the submitted code."""
        deadline = time.time() + timeout_seconds
        departed_since = None
        while time.time() < deadline:
            error = self._recovery_error(page)
            if error:
                return False, f'Microsoft 提示安全代码错误（{error}）'

            code_input = self._recovery_code_input(page)
            email_input = self._recovery_email_input(page)
            try:
                current_url = (page.url or '').casefold()
            except Exception:
                current_url = ''
            success_surface = (
                '/mail/' in current_url
                or self._visible_first(page, (
                    '[aria-label="新邮件"]',
                    '[aria-label="New mail"]',
                    '[aria-label="New message"]',
                )) is not None
            )
            still_on_proof = '/proofs/' in current_url and not success_surface
            if code_input is not None or email_input is not None or still_on_proof:
                departed_since = None
            else:
                # Require a stable departure so a transient re-render is not treated as success.
                departed_since = departed_since or time.time()
                if time.time() - departed_since >= 1:
                    return True, ''
            page.wait_for_timeout(500)
        return False, 'Microsoft 未确认备用邮箱安全代码，验证码页面仍未正常离开'

    def _resend_recovery_code(self, page):
        resend = self._visible_first(page, (
            'button:has-text("Resend code")', 'button:has-text("Send a new code")',
            'button:has-text("重新发送代码")', 'button:has-text("发送新代码")',
            'button:has-text("重寄代碼")', 'a:has-text("Resend code")',
            'a:has-text("重新发送代码")',
        ))
        if resend is None:
            return False
        requested_at = datetime.now(timezone.utc)
        self.smooth_click(page, resend)
        deadline = time.time() + 5
        while time.time() < deadline:
            if not self._recovery_error(page):
                return requested_at
            page.wait_for_timeout(500)
        return False

    def _set_recovery_result(
        self,
        bound=False,
        recovery_email='',
        reason='not_requested',
        detail='',
        usable_email_id=None,
        mailbox_mode='',
    ):
        self.thread_local.recovery_result = {
            'bound': bool(bound),
            'recovery_email': recovery_email,
            'reason': reason,
            'detail': detail,
            'usable_email_id': usable_email_id,
            'mailbox_mode': mailbox_mode,
        }

    def _write_recovery_result(self, outlook_email):
        result = getattr(self.thread_local, 'recovery_result', None) or {
            'bound': False,
            'recovery_email': '',
            'reason': 'not_requested',
            'detail': '',
        }
        record = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'outlook_email': outlook_email,
            'flow_id': getattr(self.thread_local, 'flow_id', ''),
            'proxy_session_id': getattr(self.thread_local, 'proxy_session_id', ''),
            'proxy_exit_ip': getattr(self.thread_local, 'proxy_exit_ip', ''),
            'identity_country_code': getattr(self.thread_local, 'flow_country_code', ''),
            'proxy_country_code': getattr(self.thread_local, 'proxy_country_code', ''),
            'browser_locale': getattr(self.thread_local, 'browser_locale', ''),
            'browser_timezone': getattr(self.thread_local, 'browser_timezone', ''),
            'worker_id': getattr(self.thread_local, 'worker_id', ''),
            'captcha_attempts': getattr(self.thread_local, 'captcha_attempts', 0),
            **result,
        }
        path = os.path.join(self.results_dir, 'recovery_email_status.jsonl')
        with self.results_lock:
            with open(path, 'a', encoding='utf-8') as status_file:
                status_file.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _write_account_checkpoint(self, outlook_email, password, stage, detail=''):
        record = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'outlook_email': outlook_email,
            'password': password,
            'stage': stage,
            'detail': detail,
            'flow_id': getattr(self.thread_local, 'flow_id', ''),
            'proxy_session_id': getattr(self.thread_local, 'proxy_session_id', ''),
            'proxy_exit_ip': getattr(self.thread_local, 'proxy_exit_ip', ''),
            'identity_country_code': getattr(self.thread_local, 'flow_country_code', ''),
            'proxy_country_code': getattr(self.thread_local, 'proxy_country_code', ''),
            'browser_locale': getattr(self.thread_local, 'browser_locale', ''),
            'browser_timezone': getattr(self.thread_local, 'browser_timezone', ''),
            'worker_id': getattr(self.thread_local, 'worker_id', ''),
            'captcha_attempts': getattr(self.thread_local, 'captcha_attempts', 0),
        }
        path = os.path.join(self.results_dir, 'account_checkpoints.jsonl')
        with self.results_lock:
            with open(path, 'a', encoding='utf-8') as checkpoint_file:
                checkpoint_file.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _save_registered_credentials(self, outlook_email, password, evidence):
        """Persist a created account once; later recovery/OAuth failures never remove it."""
        if getattr(self.thread_local, 'credentials_saved', False):
            return False
        filename = os.path.join(
            self.results_dir,
            'logged_email.txt' if self.enable_oauth2 else 'unlogged_email.txt',
        )
        with self.results_lock:
            with open(filename, 'a', encoding='utf-8') as credentials_file:
                credentials_file.write(f'{outlook_email}: {password}\n')
        self.thread_local.credentials_saved = True
        self._write_account_checkpoint(
            outlook_email,
            password,
            'registered',
            evidence,
        )
        print(f'[Saved: Account Credentials] - {outlook_email}: {password}')
        return True

    def _account_created_visible(self, page):
        """Use post-signup controls and URLs as evidence that Microsoft created the account."""
        if self._recovery_page_visible(page):
            return True
        if self._visible_first(page, (
            '[aria-label="新邮件"]', '[aria-label="New mail"]',
            '[aria-label="新郵件"]',
        )) is not None:
            return True
        if self._visible_first(page, (
            '#iShowSkip', '#idBtn_Skip', '#skipBtn',
            'button:has-text("暂时跳过")', 'button:has-text("Skip for now")',
            'button:has-text("Not now")', 'button:has-text("稍后再说")',
            'button:has-text("暫時略過")', 'a:has-text("Skip for now")',
        )) is not None:
            return True
        try:
            url = (page.url or '').lower()
        except Exception:
            url = ''
        return (
            '/proofs/add' in url
            or ('outlook.live.com/mail/' in url and 'prompt=create_account' not in url)
        )

    def _wait_for_account_created(self, page, timeout_seconds=8):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self._account_created_visible(page):
                return True
            page.wait_for_timeout(500)
        return self._account_created_visible(page)

