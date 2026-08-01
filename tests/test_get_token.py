import unittest

from get_token import get_proxy


class OAuthProxyTests(unittest.TestCase):
    def test_static_proxy_is_used_for_both_protocols(self):
        proxy = "http://127.0.0.1:7890"
        self.assertEqual(get_proxy(proxy), {"http": proxy, "https": proxy})

    def test_missing_proxy_disables_environment_proxy_fallback(self):
        self.assertEqual(get_proxy(), {"http": None, "https": None})


if __name__ == "__main__":
    unittest.main()
