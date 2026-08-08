"""手动代理来源的配置解析与校验测试。"""

import unittest

from outlookregister.config.config_store import validate_config
from outlookregister.config.proxy_rotation_config import (
    parse_manual_proxy_lines,
    parse_proxy_source,
)

_BASE = {
    "choose_browser": "patchright",
    "email_suffix": "@outlook.com",
    "bot_protection_wait": 1,
    "max_captcha_retries": 1,
    "concurrent_flows": 1,
    "max_tasks": 1,
    "prevent_direct_network_leaks": True,
    "identity": {
        "country_selection": "proxy",
        "require_dynamic_residential_ip": True,
    },
}


def _config(**overrides):
    return {**_BASE, **overrides}


class ParseManualProxyLinesTests(unittest.TestCase):
    def test_drops_blank_lines_and_comments_and_keeps_order(self):
        entries = parse_manual_proxy_lines(
            "http://u:p@1.1.1.1:8000\n\n  # 注释\nhttp://u:p@2.2.2.2:8000\n"
        )
        self.assertEqual(
            entries,
            ["http://u:p@1.1.1.1:8000", "http://u:p@2.2.2.2:8000"],
        )

    def test_collapses_duplicates_preserving_first_seen_order(self):
        entries = parse_manual_proxy_lines(
            ["http://u:p@2.2.2.2:8000", "http://u:p@1.1.1.1:8000", "http://u:p@2.2.2.2:8000"]
        )
        self.assertEqual(
            entries,
            ["http://u:p@2.2.2.2:8000", "http://u:p@1.1.1.1:8000"],
        )

    def test_accepts_socks5_and_rejects_malformed_entries(self):
        self.assertEqual(
            parse_manual_proxy_lines("socks5://u:p@1.1.1.1:1080"),
            ["socks5://u:p@1.1.1.1:1080"],
        )
        for bad in ("not-a-url", "ftp://1.1.1.1:21", "http://1.1.1.1:8000/path"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_manual_proxy_lines(bad)

    def test_reports_the_offending_line_number(self):
        with self.assertRaises(ValueError) as raised:
            parse_manual_proxy_lines("http://u:p@1.1.1.1:8000\nnot-a-url")
        self.assertIn("第 2 行", str(raised.exception))


class ProxySourceValidationTests(unittest.TestCase):
    def test_defaults_to_residential_and_rejects_unknown_sources(self):
        self.assertEqual(parse_proxy_source(""), "residential")
        with self.assertRaises(ValueError):
            parse_proxy_source("carrier-pigeon")

    def test_manual_mode_does_not_require_a_control_url(self):
        errors = validate_config(
            _config(
                proxy_source="manual",
                manual_proxy_pool={"pending": ["http://u:p@1.1.1.1:8000"], "used": []},
            ),
            for_run=True,
        )
        self.assertEqual(errors, [])

    def test_manual_mode_requires_at_least_one_pending_proxy(self):
        errors = validate_config(
            _config(proxy_source="manual", manual_proxy_pool={"pending": [], "used": []}),
            for_run=True,
        )
        self.assertIn("manual_proxy_pool.pending 至少需要一行可用代理", errors)

    def test_manual_mode_rejects_a_top_level_static_proxy(self):
        errors = validate_config(
            _config(
                proxy_source="manual",
                proxy="http://u:p@9.9.9.9:8000",
                manual_proxy_pool={"pending": ["http://u:p@1.1.1.1:8000"], "used": []},
            ),
            for_run=True,
        )
        self.assertIn("手动代理列表模式禁止使用顶层静态 proxy", errors)

    def test_residential_mode_still_requires_hx_proxygroup_configuration(self):
        # The same config that is valid for a manual list must stay invalid for
        # the residential source, which has no control plane configured here.
        errors = validate_config(_config(proxy_source="residential"), for_run=True)
        self.assertTrue(any("proxy_rotation" in item for item in errors), errors)

    def test_residential_mode_keeps_enforcing_its_own_runtime_rules(self):
        # Regression guard: a residential config must not fall through to the
        # manual branch, which would silently drop these requirements.
        errors = validate_config(
            _config(
                proxy_source="residential",
                proxy="http://u:p@9.9.9.9:8000",
                proxy_rotation={
                    "control_url": "https://hx.example/ctl/control-token",
                    "post_registration_route": "direct",
                },
            ),
            for_run=True,
        )
        self.assertIn("动态住宅 IP 模式禁止使用顶层静态 proxy", errors)
        self.assertIn("动态住宅 IP 模式禁止切换到 direct 或 upstream", errors)

    def test_group_names_must_not_be_blank(self):
        errors = validate_config(
            _config(
                proxy_source="manual",
                manual_proxy_pool={"pending": ["http://u:p@1.1.1.1:8000"], "used": []},
                recovery_email={"hx_email": {"keepalive_account_group": "   "}},
            ),
            for_run=True,
        )
        self.assertIn(
            "recovery_email.hx_email.keepalive_account_group 不能是空白字符串",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
