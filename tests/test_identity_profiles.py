import unittest
from unittest.mock import patch

from identity_profiles import (
    identity_profiles,
    is_valid_browser_locale,
    is_valid_timezone,
    select_identity_profile,
)


class IdentityProfileTests(unittest.TestCase):
    def test_random_selection_returns_one_complete_profile(self):
        identity = {
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
        }
        with patch(
            "identity_profiles.secrets.choice",
            side_effect=lambda profiles: profiles[1],
        ) as choice:
            selected = select_identity_profile(identity)

        choice.assert_called_once()
        self.assertEqual(
            selected,
            {
                "country_code": "DE",
                "browser_locale": "de-DE",
                "timezone": "Europe/Berlin",
            },
        )

    def test_legacy_single_profile_and_country_codes_are_supported(self):
        legacy = identity_profiles(
            {
                "country_code": "GB",
                "browser_locale": "en-GB",
                "timezone": "Europe/London",
            }
        )
        codes = identity_profiles({"country_codes": ["JP", "SG"]})

        self.assertEqual(legacy[0].country_code, "GB")
        self.assertEqual(legacy[0].browser_locale, "en-GB")
        self.assertEqual([profile.country_code for profile in codes], ["JP", "SG"])
        self.assertEqual(codes[0].browser_locale, "ja-JP")
        self.assertEqual(codes[1].timezone, "Asia/Singapore")

    def test_browser_locale_and_timezone_helpers_accept_expected_values(self):
        self.assertTrue(is_valid_browser_locale("pt-BR"))
        self.assertFalse(is_valid_browser_locale("not a locale"))
        self.assertTrue(is_valid_timezone("America/Sao_Paulo"))
        self.assertTrue(is_valid_timezone("UTC"))
        self.assertFalse(is_valid_timezone("not-a-timezone"))


if __name__ == "__main__":
    unittest.main()
