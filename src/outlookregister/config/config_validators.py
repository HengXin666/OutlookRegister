"""Internal validation blocks backing ``validate_config``.

Private ``_validate_*`` helpers extracted from ``config_validation`` to keep
that module concise; not re-exported from ``config_store``.
"""

from __future__ import annotations

from typing import Any

import outlookregister.config.config_validation as _cv
from outlookregister.config.identity_profiles import (
    identity_profiles,
    is_valid_browser_locale,
    is_valid_country_code,
    is_valid_timezone,
)
from outlookregister.config.proxy_rotation_config import (
    parse_control_plane_url,
    parse_remote_control_plane_url,
    parse_remote_residential_control_url,
    validate_proxy_endpoint,
    validate_rotation_token,
)


def _validate_basic_fields(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    browser = str(config.get("choose_browser") or "").strip().casefold()
    if browser not in {"patchright", "playwright"}:
        errors.append("choose_browser 必须是 patchright 或 playwright")

    suffix = str(config.get("email_suffix") or "").strip().casefold()
    if suffix not in {"@outlook.com", "@hotmail.com"}:
        errors.append("email_suffix 只能是 @outlook.com 或 @hotmail.com")

    for name, minimum, maximum in (
        ("bot_protection_wait", 0, 3600),
        ("max_captcha_retries", 0, 100),
        ("concurrent_flows", 1, 64),
        ("max_tasks", 1, 10000),
    ):
        try:
            _cv._number(config.get(name), name, minimum, maximum)
        except _cv.ConfigError as exc:
            errors.append(str(exc))
    return errors


def _validate_proxy_rotation(
    config: dict[str, Any],
) -> tuple[list[str], Any, bool, str, str, str, dict[str, Any]]:
    """Validate proxy_rotation; return errors plus state needed downstream."""
    errors: list[str] = []
    proxy_rotation = config.get("proxy_rotation") or {}
    if not isinstance(proxy_rotation, dict):
        errors.append("proxy_rotation 必须是对象")
        proxy_rotation = {}
    rotation_endpoint = None
    control_url = str(proxy_rotation.get("control_url") or "").strip()
    rotation_url = str(proxy_rotation.get("rotation_url") or "").strip()
    auto_control = bool(control_url)
    auto_rotation = bool(rotation_url)
    auto_identity = auto_control or auto_rotation
    if auto_control and auto_rotation:
        errors.append("proxy_rotation.control_url 与 rotation_url 不能同时配置")
    base_url = str(proxy_rotation.get("base_url") or "").strip()
    control_plane_value = control_url or rotation_url or base_url
    if control_plane_value:
        try:
            if auto_control:
                rotation_endpoint = parse_remote_residential_control_url(control_plane_value)
            elif auto_rotation:
                rotation_endpoint = parse_remote_control_plane_url(control_plane_value)
            else:
                rotation_endpoint = parse_control_plane_url(control_plane_value)
        except ValueError as exc:
            errors.append(str(exc))
    if auto_rotation and rotation_endpoint and not rotation_endpoint.embedded_token:
        errors.append("proxy_rotation.rotation_url 必须是完整 /rot/<token> URL")

    if not auto_identity:
        configured_rotation_tokens: list[str] = []
        raw_rotation_tokens = proxy_rotation.get("tokens")
        if raw_rotation_tokens is not None:
            if not isinstance(raw_rotation_tokens, list):
                errors.append("proxy_rotation.tokens 必须是数组")
            else:
                for index, entry in enumerate(raw_rotation_tokens):
                    if not isinstance(entry, dict):
                        errors.append(f"proxy_rotation.tokens[{index}] 必须是对象")
                        continue
                    token = str(entry.get("token") or "").strip()
                    proxy = str(entry.get("proxy") or "").strip()
                    if not token and not proxy:
                        continue
                    if not token:
                        errors.append(f"proxy_rotation.tokens[{index}].token 不能为空")
                    else:
                        try:
                            configured_rotation_tokens.append(
                                validate_rotation_token(token)
                            )
                        except ValueError as exc:
                            errors.append(f"proxy_rotation.tokens[{index}].token: {exc}")
                    if not proxy:
                        errors.append(f"proxy_rotation.tokens[{index}].proxy 不能为空")
                    else:
                        try:
                            validate_proxy_endpoint(proxy)
                        except ValueError as exc:
                            errors.append(f"proxy_rotation.tokens[{index}].proxy: {exc}")

        if rotation_endpoint and rotation_endpoint.embedded_token:
            if (
                len(configured_rotation_tokens) != 1
                or configured_rotation_tokens[0] != rotation_endpoint.embedded_token
            ):
                errors.append("完整 /rot/<token> URL 中的 token 必须与唯一渠道 token 一致")
    return (
        errors,
        rotation_endpoint,
        auto_identity,
        control_url,
        rotation_url,
        base_url,
        proxy_rotation,
    )


def _validate_identity(
    config: dict[str, Any],
    auto_identity: bool,
) -> tuple[list[str], dict[str, Any], bool]:
    """Validate identity/keepalive; return identity and require_dynamic flag."""
    errors: list[str] = []
    identity = config.get("identity") or {}
    if not isinstance(identity, dict):
        errors.append("identity 必须是对象")
        identity = {}
    keepalive = config.get("keepalive") or {}
    if not isinstance(keepalive, dict):
        errors.append("keepalive 必须是对象")
        keepalive = {}
    for name, minimum, maximum in (
        ("keepalive.login_timeout_seconds", 30, 900),
        ("keepalive.manual_verification_timeout_seconds", 1, 3600),
    ):
        raw_name = name.split(".", 1)[1]
        if raw_name in keepalive:
            try:
                _cv._number(keepalive.get(raw_name), name, minimum, maximum)
            except _cv.ConfigError as exc:
                errors.append(str(exc))
    for name in ("verify_existing_oauth_token", "auto_import_hx_email"):
        if name in keepalive and not isinstance(keepalive.get(name), bool):
            errors.append(f"keepalive.{name} 必须是布尔值")
    country_code = str(identity.get("country_code") or "").strip()
    if not auto_identity and country_code and not is_valid_country_code(country_code):
        errors.append("identity.country_code 只能包含 2-16 个字母、数字或连字符")

    country_selection = str(identity.get("country_selection") or "random").strip().casefold()
    if country_selection not in {"random", "proxy"} or (
        country_selection == "proxy" and not auto_identity
    ):
        errors.append("identity.country_selection 目前只能是 random 或 proxy")

    legacy_locale = str(
        identity.get("browser_locale") or identity.get("locale") or ""
    ).strip()
    if not auto_identity and legacy_locale and not is_valid_browser_locale(legacy_locale):
        errors.append("identity.browser_locale 不是有效的浏览器语言标签")
    legacy_timezone = str(identity.get("timezone") or "").strip()
    if not auto_identity and legacy_timezone and not is_valid_timezone(legacy_timezone):
        errors.append("identity.timezone 不是有效的 IANA 时区")

    if not auto_identity and "country_pool" in identity:
        country_pool = identity.get("country_pool")
        if not isinstance(country_pool, list):
            errors.append("identity.country_pool 必须是数组")
        elif not country_pool:
            errors.append("identity.country_pool 至少需要一个国家")
        else:
            for index, profile in enumerate(country_pool):
                prefix = f"identity.country_pool[{index}]"
                if not isinstance(profile, dict):
                    errors.append(f"{prefix} 必须是对象")
                    continue
                profile_country = str(profile.get("country_code") or "").strip()
                if not profile_country:
                    errors.append(f"{prefix}.country_code 不能为空")
                elif not is_valid_country_code(profile_country):
                    errors.append(
                        f"{prefix}.country_code 只能包含 2-16 个字母、数字或连字符"
                    )
                profile_locale = str(
                    profile.get("browser_locale") or profile.get("locale") or ""
                ).strip()
                if profile_locale and not is_valid_browser_locale(profile_locale):
                    errors.append(f"{prefix}.browser_locale 不是有效的浏览器语言标签")
                profile_timezone = str(
                    profile.get("timezone") or profile.get("browser_timezone") or ""
                ).strip()
                if profile_timezone and not is_valid_timezone(profile_timezone):
                    errors.append(f"{prefix}.timezone 不是有效的 IANA 时区")
    elif not auto_identity and "country_codes" in identity:
        country_codes = identity.get("country_codes")
        if not isinstance(country_codes, list):
            errors.append("identity.country_codes 必须是数组")
        elif not country_codes:
            errors.append("identity.country_codes 至少需要一个国家")
        else:
            for index, value in enumerate(country_codes):
                if not is_valid_country_code(value):
                    errors.append(
                        f"identity.country_codes[{index}] 必须是有效的国家代码"
                    )

    require_dynamic = bool(
        identity.get(
            "require_dynamic_residential_ip",
            config.get("strict_isolation", True),
        )
    )
    return errors, identity, require_dynamic


def _validate_runtime_dynamic(
    config: dict[str, Any],
    identity: dict[str, Any],
    proxy_rotation: dict[str, Any],
    auto_identity: bool,
    require_dynamic: bool,
    control_url: str,
    rotation_url: str,
    base_url: str,
    for_run: bool,
) -> list[str]:
    errors: list[str] = []
    if not (for_run and require_dynamic and not config.get("debug", False)):
        return errors

    if auto_identity:
        # A full /ctl/<token> URL is the complete user-facing input. The
        # runtime discovers the HTTP/SOCKS data-plane endpoint from the
        # declared node list and never infers a Listener address.
        if str(config.get("proxy") or "").strip():
            errors.append("动态住宅 IP 模式禁止使用顶层静态 proxy")
        route = str(
            proxy_rotation.get("post_registration_route") or ""
        ).strip().casefold()
        if route not in {"residential", ""}:
            errors.append("动态住宅 IP 模式禁止切换到 direct 或 upstream")
        if config.get("prevent_direct_network_leaks", True) is not True:
            errors.append("prevent_direct_network_leaks=true 是动态住宅 IP 运行的必需项")
        if not control_url and not rotation_url:
            errors.append("proxy_rotation.control_url 不能为空")
    else:
        has_country = any(
            str(profile.country_code or "").strip()
            for profile in identity_profiles(identity)
        )
        if not has_country:
            if "country_pool" in identity:
                country_name = "identity.country_pool"
            elif "country_codes" in identity:
                country_name = "identity.country_codes"
            else:
                country_name = "identity.country_code"
            errors.append(f"{country_name} 是动态住宅 IP 运行的必填项")
        required_flags = (
            "enabled",
            "session_scoped",
            "check_proxy",
            "enforce_unique_exit_ip",
            "verify_browser_exit_ip",
            "require_country_echo",
        )
        for flag in required_flags:
            if proxy_rotation.get(flag) is not True:
                errors.append(f"proxy_rotation.{flag}=true 是动态住宅 IP 运行的必需项")
        if str(config.get("proxy") or "").strip():
            errors.append("动态住宅 IP 模式禁止使用顶层静态 proxy")
        route = str(proxy_rotation.get("post_registration_route") or "").strip().casefold()
        if route not in {"residential", ""}:
            errors.append("动态住宅 IP 模式禁止切换到 direct 或 upstream")
        tokens = proxy_rotation.get("tokens") or []
        if not isinstance(tokens, list) or not any(
            isinstance(entry, dict)
            and str(entry.get("token") or "").strip()
            and str(entry.get("proxy") or "").strip()
            for entry in tokens
        ):
            errors.append("proxy_rotation.tokens 至少需要一个已配置的 HX-ProxyGroup 渠道")
        if config.get("prevent_direct_network_leaks") is not True:
            errors.append("prevent_direct_network_leaks=true 是动态住宅 IP 运行的必需项")
        if not base_url:
            errors.append("proxy_rotation.base_url 不能为空")
    return errors
