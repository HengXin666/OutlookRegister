import threading
import unittest
from unittest.mock import mock_open, patch

from outlookregister.core.main import process_single_flow
from outlookregister.proxy.proxy_rotation import ProxyLease


class FakeController:
    def __init__(self, events):
        self.events = events
        self.enable_oauth2 = True
        self.strict_isolation = False
        self.email_suffix = "@outlook.com"
        self.proxy = "http://static-proxy"
        self.results_lock = threading.Lock()
        self.traffic = None
        self.oauth_client_id = "client-id"
        self.hx_email_proxy_url = "http://persistent-hx-proxy"
        self.flow_hx_email = FakeFlowEmail(events)
        self.identity_config = {}

    def set_proxy(self, proxy):
        self.events.append(("set_proxy", proxy))

    def get_proxy(self):
        return "http://static-proxy"

    def get_thread_page(self):
        self.events.append(("registration_page",))
        return "registration-page"

    def outlook_register(self, page, email, password):
        self.events.append(("register", page))
        return True

    def get_oauth_page(self, page, proxy=None):
        self.events.append(("oauth_page", proxy))
        return "oauth-page"

    def clean_up(self, page=None, type="all_browser"):
        self.events.append(("clean_up", page, type))

    def close_thread_browser(self):
        self.events.append(("close_thread_browser",))

    def set_flow_context(self, *args, **kwargs):
        self.events.append(("flow_context", kwargs))

    def clear_flow_context(self):
        self.events.append(("clear_flow_context",))

    def _write_account_checkpoint(self, *args):
        self.events.append(("checkpoint", args[2] if len(args) > 2 else ""))

    def get_recovery_email(self):
        return ""

    def get_flow_hx_email(self):
        return self.flow_hx_email


class FakeFlowEmail:
    def __init__(self, events):
        self.events = events

    def import_outlook_account(self, **kwargs):
        self.events.append(("hx_import", kwargs))
        return {"account_id": 1, "group_id": 2}


class FakeProxyPool:
    def __init__(self, events):
        self.events = events
        self.post_registration_route = "direct"
        self.lease = ProxyLease(
            proxy="http://flow-session-proxy",
            token="token",
            session_id="session-1",
            session_scoped=True,
            exit_ip="203.0.113.30",
        )

    def acquire_proxy(self, country_code=None):
        self.events.append(("acquire_proxy", country_code))
        return self.lease

    def switch_after_registration(self, lease):
        self.events.append(("switch_after_flow",))
        return lease

    def verify_browser_page(self, page, lease):
        self.events.append(("verify_browser_page", page, lease.session_id))

    def release(self, lease):
        self.events.append(("release", lease.session_id))


class MainFlowIsolationTests(unittest.TestCase):
    def test_strict_isolation_rejects_a_flow_without_a_proxy_lease(self):
        events = []
        controller = FakeController(events)
        controller.strict_isolation = True

        result = process_single_flow(controller)

        self.assertFalse(result)
        self.assertNotIn(("registration_page",), events)

    def test_oauth_and_token_exchange_use_the_flow_lease_proxy(self):
        events = []
        controller = FakeController(events)
        proxy_pool = FakeProxyPool(events)

        with patch("outlookregister.core.flow_processor.random_email", return_value="flow-user"), patch(
            "outlookregister.core.flow_processor.generate_strong_password", return_value="password"
        ), patch(
            "outlookregister.core.flow_processor.get_access_token",
            return_value=(False, False, False),
        ) as get_access_token:
            result = process_single_flow(controller, proxy_pool)

        self.assertFalse(result)
        self.assertEqual(
            [event for event in events if event[0] == "oauth_page"],
            [("oauth_page", "http://flow-session-proxy")],
        )
        self.assertEqual(
            get_access_token.call_args.kwargs["proxy"],
            "http://flow-session-proxy",
        )
        flow_context_index = next(
            index for index, event in enumerate(events) if event[0] == "flow_context"
        )
        registration_page_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "registration_page"
        )
        self.assertLess(flow_context_index, registration_page_index)
        self.assertLess(
            events.index(("switch_after_flow",)),
            events.index(("release", "session-1")),
        )

    def test_hx_email_import_receives_the_configured_group_proxy_not_the_flow_proxy(self):
        events = []
        controller = FakeController(events)
        proxy_pool = FakeProxyPool(events)

        with patch("outlookregister.core.flow_processor.random_email", return_value="flow-user"), patch(
            "outlookregister.core.flow_processor.generate_strong_password", return_value="password"
        ), patch(
            "outlookregister.core.flow_processor.get_access_token",
            return_value=("refresh", "access", 123),
        ), patch("builtins.open", mock_open()):
            result = process_single_flow(controller, proxy_pool)

        self.assertTrue(result)
        import_event = next(event for event in events if event[0] == "hx_import")
        # The residential/flow proxy (http://flow-session-proxy) must never leak
        # into the HX-Email group; only the explicit group proxy is forwarded.
        self.assertEqual(
            import_event[1]["proxy_url"],
            "http://persistent-hx-proxy",
        )

    def test_flow_selects_one_country_and_passes_it_to_proxy_and_browser_context(self):
        events = []
        controller = FakeController(events)
        controller.identity_config = {
            "country_selection": "random",
            "country_pool": [
                {
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "America/New_York",
                },
                {
                    "country_code": "GB",
                    "browser_locale": "en-GB",
                    "timezone": "Europe/London",
                },
            ],
        }
        proxy_pool = FakeProxyPool(events)

        with patch("outlookregister.core.flow_processor.select_identity_profile") as select_profile, patch(
            "outlookregister.core.flow_processor.random_email", return_value="flow-user"
        ), patch(
            "outlookregister.core.flow_processor.generate_strong_password", return_value="password"
        ), patch(
            "outlookregister.core.flow_processor.get_access_token", return_value=(False, False, False)
        ):
            select_profile.return_value = {
                "country_code": "GB",
                "browser_locale": "en-GB",
                "timezone": "Europe/London",
            }
            result = process_single_flow(controller, proxy_pool)

        self.assertFalse(result)
        select_profile.assert_called_once_with(controller.identity_config)
        self.assertIn(("acquire_proxy", "GB"), events)
        context = next(event[1] for event in events if event[0] == "flow_context")
        self.assertEqual(context["flow_country_code"], "GB")
        self.assertEqual(context["browser_locale"], "en-GB")
        self.assertEqual(context["browser_timezone"], "Europe/London")


if __name__ == "__main__":
    unittest.main()
