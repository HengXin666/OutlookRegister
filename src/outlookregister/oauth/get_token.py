import json
import base64
import os
import string
import hashlib
import secrets
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from src.outlookregister import PROJECT_ROOT
from src.outlookregister.config.config_store import ConfigStore
class OAuthRecoveryChallengeError(RuntimeError):
    pass


def save_oauth_diagnostic(page, attempt, stage='failed'):
    results_dir = str(PROJECT_ROOT / 'Results')
    base_path = os.path.join(results_dir, f'oauth_{stage}_attempt_{attempt + 1}')
    try:
        page.screenshot(path=f'{base_path}.png', full_page=True)
    except Exception:
        pass
    try:
        body = page.locator('body').inner_text(timeout=3000)
        controls = page.locator('input, button').evaluate_all(
            "els => els.map(el => ({tag: el.tagName, id: el.id, name: el.name, "
            "type: el.type, text: el.innerText || el.value, "
            "ariaLabel: el.getAttribute('aria-label')}))"
        )
        with open(f'{base_path}.txt', 'w', encoding='utf-8') as diagnostic:
            diagnostic.write(f'URL: {page.url}\nCONTROLS: {controls}\n\n{body}')
        print(f'[OAuth2] - 页面诊断已保存: {base_path}.png/.txt')
    except Exception:
        pass

def get_proxy(proxy=None):
    if proxy:
        return {"http": proxy, "https": proxy}
    return {"http": None, "https": None}


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
        config = ConfigStore(PROJECT_ROOT / "config.json").read()
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
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = session.post(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=get_proxy(proxy),
            timeout=30,
        )
        try:
            token_data = response.json()
        except ValueError:
            token_data = {}
    except requests.RequestException as exc:
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

def generate_code_verifier(length=128):
    alphabet = string.ascii_letters + string.digits + '-._~'
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def generate_code_challenge(code_verifier):
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip('=')

def handle_oauth2_form(
    page,
    email,
    password,
    attempt,
    recovery_challenge_handler=None,
):
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

def get_access_token(
    page,
    email,
    password=None,
    proxy=None,
    max_retries=3,
    traffic_recorder=None,
    recovery_challenge_handler=None,
    page_delay_ms=0,
):
    for attempt in range(max_retries):
        result = _try_get_access_token(
            page=page,
            email=email,
            attempt=attempt,
            password=password,
            proxy=proxy,
            traffic_recorder=traffic_recorder,
            recovery_challenge_handler=recovery_challenge_handler,
            page_delay_ms=page_delay_ms,
        )
        if result[0] is not False:
            return result
    return False, False, False

def _try_get_access_token(
    page,
    email,
    attempt,
    password=None,
    proxy=None,
    traffic_recorder=None,
    recovery_challenge_handler=None,
    page_delay_ms=0,
):
    data = ConfigStore(PROJECT_ROOT / 'config.json').read()
    SCOPES = data['oauth2']['Scopes']
    client_id = data['oauth2']['client_id']
    redirect_url = data['oauth2']['redirect_url']
    tenant = data['oauth2'].get('tenant', 'consumers')
    prompt = data['oauth2'].get('prompt', 'consent')
    _email_suffix = data['email_suffix']
    
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_url,
        'scope': ' '.join(SCOPES),
        'response_mode': 'query',
        'prompt': prompt,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256'
    }

    authorize_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"

    captured_url = None

    def on_request(request):
        nonlocal captured_url
        if redirect_url in request.url and 'code=' in request.url:
            captured_url = request.url

    page.on("request", on_request)

    try:
        try:
            page.wait_for_timeout(250)
            page.goto(authorize_url, timeout=30000)
            _wait_for_page(page, page_delay_ms)
        except:
            return False, False, False

        handle_oauth2_form(
            page,
            f"{email}{_email_suffix}",
            password,
            attempt,
            recovery_challenge_handler=recovery_challenge_handler,
        )
        if not captured_url:
            save_oauth_diagnostic(page, attempt, 'after_form')

        max_refreshes = 1
        refresh_count = 0
        refresh_interval = 200 

        for i in range(400):
            try:
                page.wait_for_timeout(100)
            except Exception:
                if captured_url:
                    break
                return False, False, False
            if captured_url:
                break

            if i > 0 and i % refresh_interval == 0:
                if refresh_count >= max_refreshes:
                    save_oauth_diagnostic(page, attempt)
                    return False, False, False
                refresh_count += 1
                try:
                    _wait_for_page(page, page_delay_ms)
                    page.reload(timeout=10000)
                    _wait_for_page(page, page_delay_ms)
                except:
                    pass
        else:
            save_oauth_diagnostic(page, attempt)
            return False, False, False

    finally:
        page.remove_listener("request", on_request)

    if not captured_url or 'code=' not in captured_url:
        return False, False, False

    auth_code = parse_qs(captured_url.split('?')[1])['code'][0]

    try:
        token_payload = {
            'client_id': client_id,
            'code': auth_code,
            'redirect_uri': redirect_url,
            'grant_type': 'authorization_code',
            'code_verifier': code_verifier,
            'scope': ' '.join(SCOPES)
        }
        token_session = requests.Session()
        token_session.trust_env = False
        try:
            _wait_for_page(page, page_delay_ms)
            response = token_session.post(
                f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token',
                data=token_payload,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                proxies=get_proxy(proxy),
                timeout=30,
            )
        finally:
            token_session.close()
        if traffic_recorder is not None:
            response_content = getattr(response, 'content', b'')
            if not isinstance(response_content, bytes):
                response_content = str(getattr(response, 'text', '')).encode('utf-8')
            traffic_recorder.record_http(
                'oauth_token_exchange',
                'oauth_token',
                bytes_sent=len(urlencode(token_payload).encode('utf-8')),
                bytes_received=len(response_content),
                email=f'{email}{_email_suffix}',
            )

        if 'refresh_token' in response.json():
            tokens = response.json()
            return (
                tokens['refresh_token'],
                tokens.get('access_token', ''),
                datetime.now().timestamp() + tokens['expires_in']
            )
    except:
        return False, False, False

    return False, False, False


def _wait_for_page(page, milliseconds):
    """Allow a dashboard page to settle before its next browser/API step."""
    try:
        delay = max(0, int(milliseconds or 0))
    except (TypeError, ValueError):
        delay = 0
    if delay:
        page.wait_for_timeout(delay)
