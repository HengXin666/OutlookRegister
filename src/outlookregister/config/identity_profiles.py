"""Country and browser identity profiles selected once per flow."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class IdentityProfile:
    country_code: str = ""
    browser_locale: str = "en-US"
    timezone: str = ""


_COUNTRY_DEFAULTS: dict[str, IdentityProfile] = {
    "AT": IdentityProfile("AT", "de-AT", "Europe/Vienna"),
    "AU": IdentityProfile("AU", "en-AU", "Australia/Sydney"),
    "BE": IdentityProfile("BE", "nl-BE", "Europe/Brussels"),
    "BR": IdentityProfile("BR", "pt-BR", "America/Sao_Paulo"),
    "CA": IdentityProfile("CA", "en-CA", "America/Toronto"),
    "CH": IdentityProfile("CH", "de-CH", "Europe/Zurich"),
    "CN": IdentityProfile("CN", "zh-CN", "Asia/Shanghai"),
    "DK": IdentityProfile("DK", "da-DK", "Europe/Copenhagen"),
    "DE": IdentityProfile("DE", "de-DE", "Europe/Berlin"),
    "ES": IdentityProfile("ES", "es-ES", "Europe/Madrid"),
    "FI": IdentityProfile("FI", "fi-FI", "Europe/Helsinki"),
    "FR": IdentityProfile("FR", "fr-FR", "Europe/Paris"),
    "GB": IdentityProfile("GB", "en-GB", "Europe/London"),
    "HK": IdentityProfile("HK", "zh-HK", "Asia/Hong_Kong"),
    "IE": IdentityProfile("IE", "en-IE", "Europe/Dublin"),
    "ID": IdentityProfile("ID", "id-ID", "Asia/Jakarta"),
    "IN": IdentityProfile("IN", "en-IN", "Asia/Kolkata"),
    "IL": IdentityProfile("IL", "he-IL", "Asia/Jerusalem"),
    "IT": IdentityProfile("IT", "it-IT", "Europe/Rome"),
    "JP": IdentityProfile("JP", "ja-JP", "Asia/Tokyo"),
    "KR": IdentityProfile("KR", "ko-KR", "Asia/Seoul"),
    "MX": IdentityProfile("MX", "es-MX", "America/Mexico_City"),
    "NL": IdentityProfile("NL", "nl-NL", "Europe/Amsterdam"),
    "NO": IdentityProfile("NO", "nb-NO", "Europe/Oslo"),
    "NZ": IdentityProfile("NZ", "en-NZ", "Pacific/Auckland"),
    "PL": IdentityProfile("PL", "pl-PL", "Europe/Warsaw"),
    "PT": IdentityProfile("PT", "pt-PT", "Europe/Lisbon"),
    "RU": IdentityProfile("RU", "ru-RU", "Europe/Moscow"),
    "SE": IdentityProfile("SE", "sv-SE", "Europe/Stockholm"),
    "SG": IdentityProfile("SG", "en-SG", "Asia/Singapore"),
    "TH": IdentityProfile("TH", "th-TH", "Asia/Bangkok"),
    "TR": IdentityProfile("TR", "tr-TR", "Europe/Istanbul"),
    "TW": IdentityProfile("TW", "zh-TW", "Asia/Taipei"),
    "UA": IdentityProfile("UA", "uk-UA", "Europe/Kyiv"),
    "US": IdentityProfile("US", "en-US", "America/New_York"),
    "ZA": IdentityProfile("ZA", "en-ZA", "Africa/Johannesburg"),
}

_COUNTRY_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,16}(?:-[A-Za-z0-9]{1,16})?$")
_LOCALE_RE = re.compile(
    r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}$"
)
_TIMEZONE_FALLBACK_RE = re.compile(r"^(?:UTC|[A-Za-z0-9_.+-]+/[A-Za-z0-9_.+-]+)$")


def normalize_country_code(value: Any) -> str:
    return str(value or "").strip().upper()


def is_valid_country_code(value: Any) -> bool:
    return bool(_COUNTRY_CODE_RE.fullmatch(normalize_country_code(value)))


def is_valid_browser_locale(value: Any) -> bool:
    normalized = str(value or "").strip()
    return not normalized or bool(_LOCALE_RE.fullmatch(normalized))


def is_valid_timezone(value: Any) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return True
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return bool(_TIMEZONE_FALLBACK_RE.fullmatch(normalized))
    return True


def _profile_from_mapping(value: Mapping[str, Any]) -> IdentityProfile:
    country_code = normalize_country_code(value.get("country_code"))
    defaults = _COUNTRY_DEFAULTS.get(country_code, IdentityProfile(country_code))
    locale = str(
        value.get("browser_locale")
        or value.get("locale")
        or defaults.browser_locale
        or "en-US"
    ).strip()
    timezone = str(
        value.get("timezone")
        or value.get("browser_timezone")
        or defaults.timezone
        or "UTC"
    ).strip()
    return IdentityProfile(country_code, locale, timezone)


def identity_profiles(identity: Mapping[str, Any] | None) -> list[IdentityProfile]:
    """Resolve the configured pool, with compatibility for older single-value config."""
    value = identity if isinstance(identity, Mapping) else {}

    if "country_pool" in value:
        raw_pool = value.get("country_pool")
        if not isinstance(raw_pool, list):
            return []
        return [
            _profile_from_mapping(item)
            if isinstance(item, Mapping)
            else IdentityProfile()
            for item in raw_pool
        ]

    if "country_codes" in value:
        raw_codes = value.get("country_codes")
        if not isinstance(raw_codes, list):
            return []
        return [
            _profile_from_mapping({"country_code": item})
            for item in raw_codes
        ]

    return [_profile_from_mapping(value)]


def select_identity_profile(identity: Mapping[str, Any] | None) -> dict[str, str]:
    """Select exactly one profile for a flow using a cryptographically strong choice."""
    profiles = identity_profiles(identity)
    selected = secrets.choice(profiles) if profiles else IdentityProfile()
    return {key: str(item) for key, item in asdict(selected).items()}
