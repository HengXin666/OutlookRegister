import json
import tempfile
import unittest
from pathlib import Path

from outlookregister.config.config_store import (
    CONFIGURED_VALUE,
    ConfigStore,
    validate_config,
)


class ConfigStoreTests(unittest.TestCase):
    def test_public_update_preserves_redacted_nested_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "choose_browser": "patchright",
                        "email_suffix": "@outlook.com",
                        "bot_protection_wait": 1,
                        "max_captcha_retries": 1,
                        "concurrent_flows": 1,
                        "max_tasks": 1,
                        "proxy_rotation": {
                            "base_url": "https://proxy.example/rot/channel-secret",
                            "tokens": [
                                {
                                    "token": "channel-secret",
                                    "proxy": "http://user:pass@127.0.0.1:18088",
                                }
                            ]
                        },
                        "recovery_email": {
                            "hx_email": {"api_key": "hx-secret"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = ConfigStore(path)

            public = store.public()["config"]
            self.assertEqual(
                public["proxy_rotation"]["tokens"][0]["token"],
                CONFIGURED_VALUE,
            )
            self.assertEqual(
                public["proxy_rotation"]["tokens"][0]["proxy"],
                CONFIGURED_VALUE,
            )
            self.assertEqual(
                public["proxy_rotation"]["base_url"],
                "https://proxy.example",
            )
            self.assertEqual(
                public["recovery_email"]["hx_email"]["api_key"],
                CONFIGURED_VALUE,
            )

            store.update({
                "proxy_rotation": {
                    "base_url": public["proxy_rotation"]["base_url"],
                    "tokens": public["proxy_rotation"]["tokens"],
                }
            })
            stored = store.read()
            self.assertEqual(
                stored["proxy_rotation"]["base_url"],
                "https://proxy.example",
            )
            self.assertEqual(
                stored["proxy_rotation"]["tokens"][0]["token"],
                "channel-secret",
            )
            self.assertEqual(
                stored["proxy_rotation"]["tokens"][0]["proxy"],
                "http://user:pass@127.0.0.1:18088",
            )

    def test_dynamic_runtime_requires_country_and_country_echo(self):
        config = {
            "choose_browser": "patchright",
            "email_suffix": "@outlook.com",
            "bot_protection_wait": 1,
            "max_captcha_retries": 1,
            "concurrent_flows": 1,
            "max_tasks": 1,
            "strict_isolation": True,
            "prevent_direct_network_leaks": True,
            "identity": {"require_dynamic_residential_ip": True},
            "proxy_rotation": {
                "enabled": True,
                "session_scoped": True,
                "check_proxy": True,
                "enforce_unique_exit_ip": True,
                "verify_browser_exit_ip": True,
                "require_country_echo": False,
                "post_registration_route": "residential",
                "tokens": [{"token": "channel", "proxy": "http://127.0.0.1:18088"}],
            },
        }
        errors = validate_config(config, for_run=True)
        self.assertTrue(any("identity.country_code" in error for error in errors))
        self.assertTrue(any("require_country_echo" in error for error in errors))

    def test_dynamic_runtime_accepts_a_single_automatic_rotation_url(self):
        config = {
            "choose_browser": "patchright",
            "email_suffix": "@outlook.com",
            "bot_protection_wait": 1,
            "max_captcha_retries": 1,
            "concurrent_flows": 1,
            "max_tasks": 1,
            "strict_isolation": True,
            "prevent_direct_network_leaks": True,
            "identity": {"require_dynamic_residential_ip": True},
            "proxy_rotation": {
                "rotation_url": "https://proxy.example/rot/automatic-token",
                "listener": "http://127.0.0.1:7890",
            },
        }

        errors = validate_config(config, for_run=True)

        self.assertEqual(errors, [])
        self.assertFalse(any("identity.country_code" in error for error in errors))

    def test_dynamic_runtime_accepts_a_residential_control_url(self):
        config = {
            "choose_browser": "patchright",
            "email_suffix": "@outlook.com",
            "bot_protection_wait": 1,
            "max_captcha_retries": 1,
            "concurrent_flows": 2,
            "max_tasks": 2,
            "strict_isolation": True,
            "prevent_direct_network_leaks": True,
            "identity": {"require_dynamic_residential_ip": True},
            "proxy_rotation": {
                "control_url": "https://proxy.example/ctl/automatic-control-token",
            },
        }

        self.assertEqual(validate_config(config, for_run=True), [])

    def test_residential_control_url_is_redacted_from_public_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({
                    "choose_browser": "patchright",
                    "email_suffix": "@outlook.com",
                    "proxy_rotation": {
                        "control_url": "https://proxy.example/ctl/automatic-control-token",
                    },
                }),
                encoding="utf-8",
            )

            public = ConfigStore(path).public()["config"]

        self.assertEqual(
            public["proxy_rotation"]["control_url"],
            CONFIGURED_VALUE,
        )

    def test_automatic_rotation_url_rejects_a_loopback_control_plane(self):
        config = {
            "choose_browser": "patchright",
            "email_suffix": "@outlook.com",
            "bot_protection_wait": 1,
            "max_captcha_retries": 1,
            "concurrent_flows": 1,
            "max_tasks": 1,
            "identity": {"require_dynamic_residential_ip": True},
            "prevent_direct_network_leaks": True,
            "proxy_rotation": {
                "rotation_url": "https://127.0.0.1/rot/automatic-token",
            },
        }

        errors = validate_config(config, for_run=True)

        self.assertTrue(any("远程控制面不能使用回环地址" in error for error in errors))

    def test_automatic_rotation_url_is_redacted_from_public_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({
                    "choose_browser": "patchright",
                    "email_suffix": "@outlook.com",
                    "proxy_rotation": {
                        "rotation_url": "https://proxy.example/rot/automatic-token",
                    },
                }),
                encoding="utf-8",
            )

            public = ConfigStore(path).public()["config"]

        self.assertEqual(
            public["proxy_rotation"]["rotation_url"],
            CONFIGURED_VALUE,
        )

    def test_dynamic_runtime_accepts_a_multi_country_pool(self):
        config = {
            "choose_browser": "patchright",
            "email_suffix": "@outlook.com",
            "bot_protection_wait": 1,
            "max_captcha_retries": 1,
            "concurrent_flows": 1,
            "max_tasks": 1,
            "strict_isolation": True,
            "prevent_direct_network_leaks": True,
            "identity": {
                "country_selection": "random",
                "country_pool": [
                    {
                        "country_code": "US",
                        "browser_locale": "en-US",
                        "timezone": "America/New_York",
                    },
                    {
                        "country_code": "DE",
                        "browser_locale": "de-DE",
                        "timezone": "Europe/Berlin",
                    },
                ],
                "require_dynamic_residential_ip": True,
            },
            "proxy_rotation": {
                "base_url": "http://127.0.0.1:19090",
                "enabled": True,
                "session_scoped": True,
                "check_proxy": True,
                "enforce_unique_exit_ip": True,
                "verify_browser_exit_ip": True,
                "require_country_echo": True,
                "post_registration_route": "residential",
                "tokens": [{"token": "channel", "proxy": "http://127.0.0.1:18088"}],
            },
        }

        self.assertEqual(validate_config(config, for_run=True), [])

    def test_rotation_config_rejects_unsafe_control_plane_url(self):
        config = {
            "proxy_rotation": {
                "base_url": "http://proxy.example/rot/channel",
                "tokens": [{
                    "token": "channel",
                    "proxy": "http://127.0.0.1:18088",
                }],
            }
        }

        errors = validate_config(config)

        self.assertTrue(any("必须使用 https" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
