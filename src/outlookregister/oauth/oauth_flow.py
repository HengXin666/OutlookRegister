"""Long-running OAuth2 flow helpers split out of ``get_token``.

This module owns the two heavier OAuth2 routines:

* :func:`refresh_oauth_token` – refresh-token probe used by the keepalive flow.
* :func:`handle_oauth2_form` – drive the interactive Microsoft login page.

Names that tests patch on the ``get_token`` module (``requests``,
``ConfigStore`` …) are resolved through the ``get_token`` module at call
time so those patches keep taking effect, even though the function bodies
now physically live here.
"""

from datetime import datetime
from urllib.parse import urlencode

from outlookregister.oauth import get_token as _get_token


def refresh_oauth_token(
    refresh_token,
    *,
    client_id=None,
    tenant=None,
    scopes=None,
    proxy=None,
    traffic_recorder=None,
    email="",
):
    """Probe and refresh an existing OAuth token through the current flow proxy.

    A non-empty local refresh-token file is not proof that Microsoft still
    accepts the token. This request is the reliable ``missing/expired/invalid``
    decision point used by the keepalive workflow. ``trust_env`` is disabled so
    a system proxy cannot silently replace the HX-ProxyGroup session.
    """

    try:
        config = _get_token.ConfigStore(_get_token.PROJECT_ROOT / "config.json").read()
    except Exception as exc:
        return {
            "ok": False,
            "error": "config_error",
            "error_description": str(exc),
        }
    oauth_config = config.get("oauth2") or {}
    selected_client_id = str(client_id or oauth_config.get("client_id") or "").strip()
    selected_tenant = str(tenant or oauth_config.get("tenant") or "consumers").strip()
    configured_scopes = scopes if scopes is not None else oauth_config.get("Scopes") or []
    if isinstance(configured_scopes, str):
        scope_value = configured_scopes.strip()
    else:
        scope_value = " ".join(str(item).strip() for item in configured_scopes if str(item).strip())
    if not selected_client_id:
        return {
            "ok": False,
            "error": "missing_client_id",
            "error_description": "oauth2.client_id 尚未配置",
        }
    if not str(refresh_token or "").strip():
        return {
            "ok": False,
            "error": "missing_refresh_token",
            "error_description": "refresh token 为空",
        }

    payload = {
        "client_id": selected_client_id,
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token).strip(),
    }
    if scope_value:
        payload["scope"] = scope_value
    endpoint = f"https://login.microsoftonline.com/{selected_tenant}/oauth2/v2.0/token"
    session = _get_token.requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.post(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=_get_token.get_proxy(proxy),
            timeout=30,
        )
        try:
            token_data = response.json()
        except ValueError:
            token_data = {}
    except _get_token.requests.RequestException as exc:
        token_data = {
            "error": "network_error",
            "error_description": str(exc),
        }
    finally:
        session.close()

    if traffic_recorder is not None and response is not None:
        response_content = getattr(response, "content", b"")
        if not isinstance(response_content, bytes):
            response_content = str(getattr(response, "text", "")).encode("utf-8")
        traffic_recorder.record_http(
            "oauth_token_refresh_probe",
            "oauth_token",
            bytes_sent=len(urlencode(payload).encode("utf-8")),
            bytes_received=len(response_content),
            email=email,
        )

    access_token = str(token_data.get("access_token") or "").strip()
    if response is not None and response.status_code < 400 and access_token:
        try:
            expires_in = max(1, int(token_data.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        return {
            "ok": True,
            "refresh_token": str(token_data.get("refresh_token") or refresh_token).strip(),
            "access_token": access_token,
            "expires_at": str(datetime.now().timestamp() + expires_in),
            "client_id": selected_client_id,
        }

    return {
        "ok": False,
        "error": str(token_data.get("error") or f"http_{getattr(response, 'status_code', 0)}"),
        "error_description": str(
            token_data.get("error_description")
            or token_data.get("error")
            or "OAuth refresh token 探针失败"
        ),
    }


def handle_oauth2_form(
    page,
    email,
    password,
    attempt,
    recovery_challenge_handler=None,
):
    OAuthRecoveryChallengeError = _get_token.OAuthRecoveryChallengeError
    save_oauth_diagnostic = _get_token.save_oauth_diagnostic

    def visible_first(selectors):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:
                pass
        return None

    def submit_visible_form():
        for selector in ('#idSIButton9', 'button[type="submit"]', 'input[type="submit"]'):
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=5000)
                    return True
            except Exception:
                pass
        try:
            page.keyboard.press('Enter')
            return True
        except Exception:
            return False

    deadline = datetime.now().timestamp() + (25 if attempt == 0 else 8)
    while datetime.now().timestamp() < deadline:
        acted = False
        proof_email_input = visible_first((
            '#proof-confirmation-email-input',
            'input[name="proofConfirmationEmail"]',
            'input[name="ProofConfirmationEmail"]',
            'input[data-testid="proof-confirmation-email-input"]',
        ))
        if proof_email_input is None:
            try:
                proof_url = str(page.url or '').casefold()
                proof_body = ' '.join(
                    page.locator('body').inner_text(timeout=1000).split()
                ).casefold()
            except Exception:
                proof_url = ''
                proof_body = ''
            if '/proofs/' in proof_url or any(
                marker in proof_body
                for marker in (
                    'recovery email',
                    'alternate email',
                    '备用邮箱',
                    '恢复邮箱',
                )
            ):
                proof_email_input = visible_first((
                    'input[autocomplete="email"]',
                    'input[type="email"]',
                    'input[name="EmailAddress"]',
                    'input[name="proof"]',
                ))
        proof_code_input = visible_first((
            'input[id^="codeEntry-"]',
            '#proof-confirmation-code-input',
            '#otc-confirmation-input',
            '#iOttText',
            'input[name="otc"]',
            'input[name="ProofConfirmationCode"]',
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
        ))
        if proof_email_input is not None or proof_code_input is not None:
            if recovery_challenge_handler is None:
                stage = (
                    'recovery_email_required'
                    if proof_email_input is not None
                    else 'recovery_code_required'
                )
                save_oauth_diagnostic(page, attempt, stage)
                raise OAuthRecoveryChallengeError(
                    'Microsoft 要求密保邮箱验证，但当前授权流程没有可用的验证处理器'
                )
            if not recovery_challenge_handler(page):
                raise OAuthRecoveryChallengeError('Microsoft 密保邮箱验证未完成')
            page.wait_for_timeout(500)
            continue

        try:
            email_input = page.locator(
                '#usernameEntry, [name="loginfmt"], input[type="email"]'
            ).first
            if email_input.count() and email_input.is_visible():
                email_input.fill(email, timeout=5000)
                acted = submit_visible_form()
        except Exception:
            pass

        if password:
            try:
                password_input = page.locator('input[type="password"]').first
                if password_input.count() and password_input.is_visible():
                    password_input.fill(password, timeout=5000)
                    acted = submit_visible_form()
            except Exception:
                pass

        try:
            consent_btn = page.locator('[data-testid="appConsentPrimaryButton"]').first
            if consent_btn.count() and consent_btn.is_visible():
                consent_btn.click(timeout=5000)
                return
        except Exception:
            pass

        try:
            kmsi = page.locator('#idSIButton9').first
            if not acted and kmsi.count() and kmsi.is_visible():
                kmsi.click(timeout=5000)
                acted = True
        except Exception:
            pass
        page.wait_for_timeout(500 if acted else 250)
