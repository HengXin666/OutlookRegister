import unittest

from controllers.base_controller import build_browser_proxy_settings


class BrowserProxySettingsTests(unittest.TestCase):
    def test_session_credentials_are_split_from_proxy_server(self):
        settings = build_browser_proxy_settings(
            "http://hx-session-user:secret%3A%40value@127.0.0.1:18088"
        )

        self.assertEqual(settings["server"], "http://127.0.0.1:18088")
        self.assertEqual(settings["username"], "hx-session-user")
        self.assertEqual(settings["password"], "secret:@value")
        self.assertEqual(settings["bypass"], "localhost,127.0.0.1,[::1]")

    def test_empty_proxy_disables_browser_proxy(self):
        self.assertIsNone(build_browser_proxy_settings(""))


if __name__ == "__main__":
    unittest.main()
