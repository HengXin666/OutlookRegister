"""Outlook OAuth2 token acquisition entry module.

The lighter helpers (PKCE, proxy, diagnostics) and the public entry points
``get_access_token`` / ``_try_get_access_token`` live here. The two heavier
OAuth2 routines (``refresh_oauth_token`` and ``handle_oauth2_form``) live in
``oauth_flow`` and are re-exported below so the historical public/private names
keep resolving from ``src.outlookregister.oauth.get_token`` -- which is what
tests and the legacy ``get_token`` shim import.
"""

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
from src.outlookregister.oauth.oauth_flow import (
    handle_oauth2_form,
    refresh_oauth_token,
)


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


def generate_code_verifier(length=128):
    alphabet = string.ascii_letters + string.digits + '-._~'
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def generate_code_challenge(code_verifier):
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip('=')


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
