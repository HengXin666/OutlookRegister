import os
import time
import json
import random
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from faker import Faker
from abc import ABC, abstractmethod
from config_store import ConfigStore
from hx_email_client import HXEmailClient, HXEmailError
from traffic_tracker import TrafficRecorder


def build_browser_proxy_settings(proxy):
    """Convert a proxy URL into Playwright's server/credential fields."""
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if not parsed.scheme or not parsed.hostname:
        return {"server": proxy, "bypass": "localhost,127.0.0.1,[::1]"}
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    settings = {
        "server": urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)),
        "bypass": "localhost,127.0.0.1,[::1]",
    }
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        settings["password"] = unquote(parsed.password)
    return settings


class BaseBrowserController(ABC):
    """
    所有浏览器通用的接口和共享逻辑
    """

    def __init__(self):
        data = ConfigStore(Path(__file__).resolve().parent.parent / 'config.json').read()
        self.wait_time = data['bot_protection_wait'] * 1000
        self.max_captcha_retries = data['max_captcha_retries']
        self.enable_oauth2 = data["oauth2"]['enable_oauth2']
        self.proxy = data['proxy']
        self.debug = bool(data.get('debug', False))
        if self.debug:
            self.strict_isolation = False
        self.strict_isolation = bool(data.get('strict_isolation', True)) and not self.debug

        self.isolate_hx_email_group = bool(
            data.get('isolate_hx_email_group', self.strict_isolation)
        )
        self.prevent_direct_network_leaks = bool(
            data.get('prevent_direct_network_leaks', True)
        )
        identity = data.get('identity') or {}
        self.identity_config = dict(identity)
        self.country_code = str(identity.get('country_code') or '').strip()
        self.browser_locale = str(
            identity.get('browser_locale')
            or identity.get('locale')
            or 'en-US'
        ).strip()
        self.browser_timezone = str(identity.get('timezone') or '').strip()
        self.require_dynamic_residential_ip = bool(
            identity.get(
                'require_dynamic_residential_ip',
                self.strict_isolation,
            )
        )
        self.email_suffix = data['email_suffix']
        self.results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Results')
        os.makedirs(self.results_dir, exist_ok=True)
        self.recovery_email_config = data.get('recovery_email') or {}
        self.recovery_email_enabled = bool(self.recovery_email_config.get('enabled', False))
        self.hx_email_proxy_url = str(
            (self.recovery_email_config.get('hx_email') or {}).get('proxy_url', '')
        ).strip()
        self.recovery_code_attempts = max(
            1, int(self.recovery_email_config.get('max_code_attempts', 2))
        )
        self.hx_email = HXEmailClient(self.recovery_email_config.get('hx_email') or {})
        self.traffic = TrafficRecorder(self.results_dir)
        self.hx_email.set_traffic_recorder(self.traffic)
        self.oauth_client_id = data['oauth2']['client_id']

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.active_resources = []  # 记录资源以便关闭
        self.oauth_browsers = {}

    def set_proxy(self, proxy):
        """设置当前线程使用的代理地址(支持每个注册流程使用不同的住宅代理)。"""
        normalized = str(proxy or '').strip()
        if normalized:
            self.thread_local.proxy = normalized
        elif hasattr(self.thread_local, 'proxy'):
            delattr(self.thread_local, 'proxy')

    def set_flow_context(
        self,
        flow_id,
        proxy_session_id="",
        proxy_exit_ip="",
        proxy_country_code="",
        worker_id="",
        browser_locale="",
        browser_timezone="",
        flow_country_code="",
    ):
        self.thread_local.flow_id = str(flow_id or "")
        self.thread_local.proxy_session_id = str(proxy_session_id or "")
        self.thread_local.proxy_exit_ip = str(proxy_exit_ip or "")
        self.thread_local.flow_country_code = str(
            flow_country_code or proxy_country_code or getattr(self, "country_code", "") or ""
        ).strip()
        self.thread_local.proxy_country_code = str(
            proxy_country_code or self.thread_local.flow_country_code or ""
        ).strip()
        self.thread_local.worker_id = str(worker_id or "")
        self.thread_local.browser_locale = str(
            browser_locale or getattr(self, "browser_locale", "") or ""
        ).strip()
        self.thread_local.browser_timezone = str(
            browser_timezone or getattr(self, "browser_timezone", "") or ""
        ).strip()

        previous_client = getattr(self.thread_local, 'hx_email', None)
        if previous_client is not None:
            try:
                previous_client.close()
            except Exception:
                pass

        hx_email_config = dict(self.recovery_email_config.get('hx_email') or {})
        if self.isolate_hx_email_group:
            base_group = str(
                hx_email_config.get('account_group', 'OutlookRegister 自动注册')
            ).strip()
            hx_email_config['account_group'] = f'{base_group} [{self.thread_local.flow_id}]'
        flow_client = HXEmailClient(hx_email_config)
        flow_client.set_traffic_recorder(getattr(self, 'traffic', None))
        self.thread_local.hx_email = flow_client

    def get_flow_hx_email(self):
        return getattr(self.thread_local, 'hx_email', self.hx_email)

    def clear_flow_context(self):
        for attribute in (
            "flow_id",
            "proxy_session_id",
            "proxy_exit_ip",
            "flow_country_code",
            "proxy_country_code",
            "worker_id",
            "browser_locale",
            "browser_timezone",
            "captcha_attempts",
            "proxy",
            "last_pos",
            "recovery_email",
            "recovery_mailbox",
            "credentials_saved",
            "recovery_result",
        ):
            if hasattr(self.thread_local, attribute):
                delattr(self.thread_local, attribute)
        flow_client = getattr(self.thread_local, 'hx_email', None)
        if flow_client is not None:
            try:
                flow_client.close()
            except Exception:
                pass
            delattr(self.thread_local, 'hx_email')

    def record_captcha_attempt(self):
        attempts = getattr(self.thread_local, 'captcha_attempts', 0) + 1
        self.thread_local.captcha_attempts = attempts
        traffic = getattr(self, 'traffic', None)
        if traffic is not None:
            traffic.set_captcha_attempts(attempts)
        record = {
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'flow_id': getattr(self.thread_local, 'flow_id', ''),
            'proxy_session_id': getattr(self.thread_local, 'proxy_session_id', ''),
            'proxy_exit_ip': getattr(self.thread_local, 'proxy_exit_ip', ''),
            'identity_country_code': getattr(self.thread_local, 'flow_country_code', ''),
            'proxy_country_code': getattr(self.thread_local, 'proxy_country_code', ''),
            'browser_locale': getattr(self.thread_local, 'browser_locale', ''),
            'browser_timezone': getattr(self.thread_local, 'browser_timezone', ''),
            'worker_id': getattr(self.thread_local, 'worker_id', ''),
            'attempt': attempts,
        }
        path = os.path.join(self.results_dir, 'captcha_attempts.jsonl')
        try:
            with self.results_lock:
                with open(path, 'a', encoding='utf-8') as attempts_file:
                    attempts_file.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError as exc:
            print(f'[Captcha] 尝试记录失败: {exc}')
        return attempts

    def get_proxy(self):
        """Return the flow lease, or no route when dynamic mode is required."""
        flow_proxy = getattr(self.thread_local, 'proxy', None)
        if flow_proxy:
            return flow_proxy
        if getattr(self, "require_dynamic_residential_ip", False) and not getattr(self, "debug", False):
            return None
        return getattr(self, "proxy", None)

    def browser_context_options(self):
        options = {}
        browser_locale = str(
            getattr(
                self.thread_local,
                'browser_locale',
                getattr(self, 'browser_locale', ''),
            )
            or ''
        ).strip()
        browser_timezone = str(
            getattr(
                self.thread_local,
                'browser_timezone',
                getattr(self, 'browser_timezone', ''),
            )
            or ''
        ).strip()
        if browser_locale:
            options['locale'] = browser_locale
        if browser_timezone:
            options['timezone_id'] = browser_timezone
        return options

    def new_browser_context(self, browser):
        options = self.browser_context_options()
        return browser.new_context(**options) if options else browser.new_context()

    def browser_launch_args(self):
        browser_locale = str(
            getattr(
                self.thread_local,
                'browser_locale',
                getattr(self, 'browser_locale', ''),
            )
            or ''
        ).strip()
        args = [f'--lang={browser_locale}'] if browser_locale else []
        if self.prevent_direct_network_leaks and not getattr(self, "debug", False):
            args.extend(
                (
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                    '--disable-quic',
                )
            )
        return args

    def get_last_pos(self):
        """获取当前线程的上一次鼠标位置 (x, y)"""
        return getattr(self.thread_local, 'last_pos', None)

    def set_last_pos(self, x, y):
        """设置当前线程的鼠标位置 (x, y)"""
        self.thread_local.last_pos = (float(x), float(y))

    def reset_last_pos(self):
        """重置当前线程的坐标历史"""
        if hasattr(self.thread_local, 'last_pos'):
            del self.thread_local.last_pos

    def wait_random_ratio(self, page, min_ratio, delta=0.02):

        actual_ratio = random.uniform(min_ratio, min_ratio + delta)
        page.wait_for_timeout(actual_ratio * self.wait_time)

    def smooth_move_to(self, page, target_x, target_y, steps=None):
        """从上一次坐标滑动到目标坐标"""
        last_pos = self.get_last_pos()
        if not last_pos:
            last_pos = (random.uniform(150, 450), random.uniform(100, 350))
            try:
                page.mouse.move(last_pos[0], last_pos[1])
            except Exception:
                pass

        if steps is None:
            steps = random.randint(6, 14)

        try:
            page.mouse.move(target_x, target_y, steps=steps)
        except Exception:
            pass

        self.set_last_pos(target_x, target_y)

    def smooth_click(self, page, locator, offset_range=5, click_delay_range=(60, 160)):
        """点击方法"""
        try:
            box = locator.bounding_box()
            if not box:
                locator.click()
                return False

            tx = box['x'] + box['width'] / 2 + random.uniform(-offset_range, offset_range)
            ty = box['y'] + box['height'] / 2 + random.uniform(-offset_range, offset_range)

            self.smooth_move_to(page, tx, ty)

            pause_ms = random.randint(click_delay_range[0], click_delay_range[1])
            page.wait_for_timeout(pause_ms)

            page.mouse.click(tx, ty)
            self.set_last_pos(tx, ty)
            return True
        except Exception:
            try:
                locator.click()
            except Exception:
                pass
            return False

    def smooth_type(self, page, locator, text, click_first=True):
        """输入方法"""
        if click_first:
            self.smooth_click(page, locator)

        for char in text:
            try:
                locator.type(char, delay=random.randint(40, 110))
            except Exception:
                break

    @abstractmethod
    def launch_browser(self, proxy=None, playwright=None):
        """
        获取浏览器实例,返回playwright_instance, browser_instance
        """
        pass

    @abstractmethod
    def handle_captcha(self, page):
        """
        验证码处理流程
        """
        pass

    @abstractmethod 
    def clean_up(self, page=None, type="all_browser"):
        """
        清理自己创建的内容
        一个是单进程结束后关闭进程，另一个是程序结束后清除所有内容
        """
        pass

    @abstractmethod
    def get_thread_page(self):
        """
        返回页面
        """

    def get_thread_browser(self):
        """
        通用逻辑:获取不同进程的浏览器
        """
        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                return False

            self.thread_local.playwright = p
            self.thread_local.browser = b

            with self.cleanup_lock:
                self.active_resources.append((p, b))

        return self.thread_local.browser

    def get_oauth_page(self, source_page, proxy=None):
        """Copy the signed-in session while preserving the current flow proxy."""
        selected_proxy = self.get_proxy() if proxy is None else proxy
        shared_playwright = getattr(self.thread_local, 'playwright', None)
        p, browser = self.launch_browser(
            proxy=selected_proxy,
            playwright=shared_playwright,
        )
        if not p:
            return False
        owned_playwright = None if shared_playwright is not None else p
        with self.cleanup_lock:
            self.active_resources.append((owned_playwright, browser))
        context = None
        try:
            context = self.new_browser_context(browser)
            cookies = source_page.context.cookies()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            with self.cleanup_lock:
                self.oauth_browsers[id(page)] = (owned_playwright, browser)
            return page
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
            if owned_playwright is not None:
                try:
                    owned_playwright.stop()
                except Exception:
                    pass
            with self.cleanup_lock:
                self.active_resources = [
                    (resource_p, b)
                    for resource_p, b in self.active_resources
                    if b is not browser
                ]
            raise

    def close_page_context(self, page):
        """Close a page context and its dedicated OAuth browser, if any."""
        if page is None:
            return
        try:
            context = page.context
        except Exception:
            context = None
        with self.cleanup_lock:
            resource = self.oauth_browsers.pop(id(page), None)
        if isinstance(resource, tuple):
            playwright, browser = resource
        else:
            playwright, browser = None, resource
        traffic = getattr(self, 'traffic', None)
        if traffic is not None:
            try:
                traffic.detach_page(page)
            except Exception:
                pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
                with self.cleanup_lock:
                    self.active_resources = [
                        (p, b)
                        for p, b in self.active_resources
                        if b is not browser
                    ]
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def close_all_resources(self):
        with self.cleanup_lock:
            resources = list(self.active_resources)
            self.active_resources.clear()
            self.oauth_browsers.clear()
        for playwright, browser in resources:
            try:
                browser.close()
            except Exception:
                pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def close_thread_browser(self):
        """Close the registration browser so a rotated proxy applies next time."""
        browser = getattr(self.thread_local, 'browser', None)
        playwright = getattr(self.thread_local, 'playwright', None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
            with self.cleanup_lock:
                self.active_resources = [
                    (p, b) for p, b in self.active_resources if b is not browser
                ]
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        for attribute in ('browser', 'playwright'):
            if hasattr(self.thread_local, attribute):
                delattr(self.thread_local, attribute)

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

    def outlook_register(self, page, email, password):
        """
        通用逻辑:注册邮箱
        """

        self.reset_last_pos()
        self.thread_local.recovery_email = ''
        self.thread_local.credentials_saved = False
        self.thread_local.captcha_attempts = 0
        self._set_recovery_result()
        outlook_email = f'{email}{self.email_suffix}'
        self._write_account_checkpoint(
            outlook_email,
            password,
            'generated',
            '账号密码已生成，尚未确认 Microsoft 注册结果',
        )
        fake = Faker()

        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1960, 2005))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=20000, wait_until="domcontentloaded")
            consent_btn = page.get_by_text('同意并继续')
            consent_btn.wait_for(timeout=30000)
            start_time = time.time()
            self.wait_random_ratio(page, 0.06)
            self.smooth_click(page, consent_btn)
        except Exception as exc:
            self._write_account_checkpoint(
                outlook_email,
                password,
                'navigation_failed',
                str(exc),
            )
            print("[Error: IP] - IP质量不佳，无法进入注册界面。")
            return False

        try:
            if self.email_suffix == "@hotmail.com":
                self.wait_random_ratio(page, 0.06)
                domain_btn = page.get_by_text("@outlook.com")
                self.smooth_click(page, domain_btn)
                option_btn = page.locator(f'[role="option"]:text-is("@hotmail.com")')
                self.smooth_click(page, option_btn)


            email_input = page.locator('[aria-label="新建电子邮件"]')
            self.smooth_type(page, email_input, email)

            primary_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.04)

            pwd_input = page.locator('[type="password"]')
            self.smooth_type(page, pwd_input, password)
            self.wait_random_ratio(page, 0.03)
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.03)

            if page.get_by_text("请重试。如果仍然不起作用，请稍后再试。").count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'registration_rejected',
                    'Microsoft 提示请稍后重试',
                )
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            year_input = page.locator('[name="BirthYear"]')
            if year_input.count() > 0:
                self.smooth_click(page, year_input)
                year_input.fill(year)

            month_btn = page.locator('[name="BirthMonth"]')
            self.smooth_click(page, month_btn)
            self.wait_random_ratio(page, 0.03)
            m_opt = page.locator(f'[role="option"]:text-is("{month}月")')
            self.smooth_click(page, m_opt)

            self.wait_random_ratio(page, 0.03)
            day_btn = page.locator('[name="BirthDay"]')
            self.smooth_click(page, day_btn)
            self.wait_random_ratio(page, 0.03)

            d_opt = page.locator(f'[role="option"]:text-is("{day}日")')
            if d_opt.count() > 0:
                try:
                    d_opt.scroll_into_view_if_needed()
                except Exception:
                    pass
            self.smooth_click(page, d_opt)

            self.smooth_click(page, primary_btn)

            lname_input = page.locator('#lastNameInput')
            lname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, lname_input, lastname)

            self.wait_random_ratio(page, 0.02)
            fname_input = page.locator('#firstNameInput')
            fname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, fname_input, firstname)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            self.smooth_click(page, primary_btn)
            self._write_account_checkpoint(
                outlook_email,
                password,
                'profile_submitted',
                '姓名资料已提交，等待按压验证及 Microsoft 创建结果',
            )
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'registration_rejected',
                    'Microsoft 提示异常活动或站点维护',
                )
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'captcha_unsupported',
                    '出现 FunCaptcha，当前流程仅支持按压验证码',
                )
                print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            captcha_error = ''
            try:
                captcha_result = self.handle_captcha(page)
            except Exception as exc:
                captcha_result = False
                captcha_error = str(exc)

            if not captcha_result:
                if not self._wait_for_account_created(page):
                    detail = captcha_error or '按压次数用尽，且未发现账号创建成功的页面证据'
                    self._write_account_checkpoint(
                        outlook_email,
                        password,
                        'registration_unconfirmed',
                        detail,
                    )
                    print(f'[Error: Captcha] - {detail}')
                    return False
                print(
                    '[Warning: Captcha Result] - 按压处理返回失败，但页面已进入注册后阶段，'
                    '按账号注册成功继续处理。'
                )
                creation_evidence = 'captcha_returned_false_but_post_signup_page_visible'
            else:
                creation_evidence = 'captcha_handler_completed'

            self._save_registered_credentials(
                outlook_email,
                password,
                creation_evidence,
            )

            page.wait_for_timeout(1000)
            if not self.handle_recovery_email(page):
                recovery_detail = getattr(
                    self.thread_local, 'recovery_result', {}
                ).get('detail', '')
                self._write_account_checkpoint(
                    outlook_email,
                    password,
                    'recovery_failed',
                    recovery_detail,
                )
                self._write_recovery_result(outlook_email)
                return False

        except Exception as exc:
            if (
                not self.thread_local.credentials_saved
                and self._account_created_visible(page)
            ):
                self._save_registered_credentials(
                    outlook_email,
                    password,
                    'exception_but_post_signup_page_visible',
                )
            stage = (
                'post_registration_failed'
                if self.thread_local.credentials_saved
                else 'registration_unconfirmed'
            )
            self._write_account_checkpoint(outlook_email, password, stage, str(exc))
            print(f'[Error: Registration Flow] - {exc}')
            return False

        # Idempotent fallback for future controller implementations that reach here directly.
        self._save_registered_credentials(
            outlook_email,
            password,
            'registration_flow_completed',
        )
        print(f'[Success: Email Registration] - {outlook_email}: {password}')

        if not self.enable_oauth2:
            self._write_recovery_result(outlook_email)
            return True

        start_skip_time = time.time()
        while time.time() - start_skip_time < 20:
            try:
                if self._recovery_page_visible(page):
                    if not self.handle_recovery_email(page):
                        recovery_detail = getattr(
                            self.thread_local, 'recovery_result', {}
                        ).get('detail', '')
                        self._write_account_checkpoint(
                            outlook_email,
                            password,
                            'recovery_failed',
                            recovery_detail,
                        )
                        self._write_recovery_result(outlook_email)
                        return False
                    continue
                btn_skip = page.get_by_text("暂时跳过")
                if btn_skip.count() > 0 and btn_skip.is_visible():
                    self.smooth_click(page, btn_skip)
                    page.wait_for_timeout(random.randint(1000, 1500))
                else:
                    btn_skip.wait_for(timeout=7000)
            except Exception:
                break

        try:
            page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
            return True
        except Exception:
            print('[Error: Timeout] - 邮箱未初始化，无法正常收件。')
            return False
        finally:
            self._write_recovery_result(outlook_email)
