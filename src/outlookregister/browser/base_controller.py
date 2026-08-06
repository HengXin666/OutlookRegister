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
from src.outlookregister import PROJECT_ROOT
from src.outlookregister.config.config_store import ConfigStore
from src.outlookregister.email.hx_email_client import HXEmailClient, HXEmailError
from src.outlookregister.dashboard.traffic_tracker import TrafficRecorder
from src.outlookregister.browser.base_controller_recovery import _BaseRecovery
from src.outlookregister.browser.base_controller_recovery_challenge import _BaseRecoveryChallenge
from src.outlookregister.browser.base_controller_registration import _BaseRegistration

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


class BaseBrowserController(_BaseRecovery, _BaseRecoveryChallenge, _BaseRegistration, ABC):
    """
    所有浏览器通用的接口和共享逻辑
    """

    def __init__(self):
        data = ConfigStore(PROJECT_ROOT / 'config.json').read()
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
        self.results_dir = str(PROJECT_ROOT / 'Results')
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
