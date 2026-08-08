import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from outlookregister.browser.base_controller import BaseBrowserController


class FakePage:
    def __init__(self, context):
        self.context = context


class FakeContext:
    def __init__(self, cookies=None):
        self.cookies_value = cookies or [{"name": "session", "value": "flow-cookie"}]
        self.added_cookies = []
        self.closed = False

    def cookies(self):
        return self.cookies_value

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def new_page(self):
        return FakePage(self)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.context = None
        self.closed = False

    def new_context(self):
        self.context = FakeContext()
        return self.context

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class StubController(BaseBrowserController):
    def launch_browser(self, proxy=None, playwright=None):
        self.launch_calls.append((proxy, playwright))
        return playwright or object(), FakeBrowser()

    def handle_captcha(self, page):
        raise NotImplementedError

    def clean_up(self, page=None, type="all_browser"):
        raise NotImplementedError

    def get_thread_page(self):
        raise NotImplementedError


class FakeFlowEmailClient:
    def __init__(self, config):
        self.config = config
        self.recorder = None
        self.closed = False

    def set_traffic_recorder(self, recorder):
        self.recorder = recorder

    def close(self):
        self.closed = True


def make_controller():
    controller = object.__new__(StubController)
    controller.thread_local = threading.local()
    controller.cleanup_lock = threading.Lock()
    controller.active_resources = []
    controller.oauth_browsers = {}
    controller.launch_calls = []
    controller.get_proxy = lambda: "http://static-proxy"
    return controller


class FlowIsolationTests(unittest.TestCase):
    def test_oauth_browser_receives_the_flow_proxy(self):
        controller = make_controller()
        shared_playwright = FakePlaywright()
        controller.thread_local.playwright = shared_playwright
        source_context = FakeContext([{"name": "source", "value": "cookie"}])
        source_page = FakePage(source_context)

        oauth_page = controller.get_oauth_page(
            source_page,
            proxy="http://flow-session-proxy",
        )

        self.assertEqual(
            controller.launch_calls[0][0],
            "http://flow-session-proxy",
        )
        self.assertIs(controller.launch_calls[0][1], shared_playwright)
        self.assertEqual(oauth_page.context.added_cookies, source_context.cookies_value)
        controller.close_page_context(oauth_page)
        self.assertTrue(oauth_page.context.closed)
        self.assertFalse(shared_playwright.stopped)

    def test_oauth_browser_stops_a_runtime_it_created(self):
        controller = make_controller()
        owned_playwright = FakePlaywright()
        controller.launch_browser = lambda proxy=None, playwright=None: (
            owned_playwright,
            FakeBrowser(),
        )
        oauth_page = controller.get_oauth_page(FakePage(FakeContext()))

        controller.close_page_context(oauth_page)

        self.assertTrue(owned_playwright.stopped)

    def test_flow_hx_email_clients_and_groups_are_distinct(self):
        controller = object.__new__(StubController)
        controller.thread_local = threading.local()
        controller.recovery_email_config = {
            "hx_email": {"account_group": "OutlookRegister"},
        }
        controller.isolate_hx_email_group = True
        controller.traffic = object()
        controller.hx_email = FakeFlowEmailClient({})
        created = []

        def create_client(config):
            client = FakeFlowEmailClient(config)
            created.append(client)
            return client

        with patch(
            "outlookregister.browser.base_controller.HXEmailClient",
            side_effect=create_client,
        ):
            controller.set_flow_context("flow-a")
            first = controller.get_flow_hx_email()
            controller.clear_flow_context()
            controller.set_flow_context("flow-b")
            second = controller.get_flow_hx_email()

        self.assertIsNot(first, second)
        # Only the registration group is flow-suffixed; keepalive needs a stable
        # group so it can find and update accounts it imported earlier.
        self.assertEqual(
            first.config["register_account_group"], "OutlookRegister [flow-a]"
        )
        self.assertEqual(
            second.config["register_account_group"], "OutlookRegister [flow-b]"
        )
        self.assertEqual(first.config["account_group"], "OutlookRegister")
        self.assertTrue(first.closed)

    def test_clearing_thread_proxy_removes_flow_override(self):
        controller = object.__new__(StubController)
        controller.thread_local = threading.local()
        controller.proxy = "http://static-proxy"

        controller.set_proxy("http://flow-proxy")
        self.assertEqual(controller.get_proxy(), "http://flow-proxy")
        controller.set_proxy(None)

        self.assertFalse(hasattr(controller.thread_local, "proxy"))
        self.assertEqual(controller.get_proxy(), "http://static-proxy")

    def test_captcha_attempt_log_is_scoped_without_credentials(self):
        controller = object.__new__(StubController)
        controller.thread_local = threading.local()
        controller.results_lock = threading.Lock()
        controller.results_dir = None
        with tempfile.TemporaryDirectory() as directory:
            controller.results_dir = directory
            controller.thread_local.flow_id = "flow-a"
            controller.thread_local.proxy_session_id = "session-a"
            controller.thread_local.proxy_exit_ip = "203.0.113.40"
            controller.thread_local.worker_id = "worker-a"
            controller.record_captcha_attempt()
            controller.record_captcha_attempt()

            records = [
                json.loads(line)
                for line in Path(directory, "captcha_attempts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual([record["attempt"] for record in records], [1, 2])
        self.assertEqual({record["flow_id"] for record in records}, {"flow-a"})
        self.assertNotIn("password", records[0])


if __name__ == "__main__":
    unittest.main()
