"""Evidence-based Outlook login page classification for the dashboard flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OutlookPageState:
    """A small, non-secret snapshot used by the synchronous browser flow."""

    name: str
    evidence: str
    url_path: str = ""


def _page_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _page_body(page: Any) -> str:
    try:
        return " ".join(page.locator("body").inner_text(timeout=2000).split()).casefold()
    except Exception:
        return ""


def _visible(page: Any, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        return int(locator.count()) > 0 and bool(locator.is_visible())
    except Exception:
        return False


def _first_visible(page: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        if _visible(page, selector):
            return selector
    return ""


def _url_path(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return parsed.path or parsed.netloc
    except Exception:
        return ""


def classify_outlook_page(page: Any) -> OutlookPageState:
    """Classify by stable URL/DOM evidence first and localized text second.

    Microsoft changes copy by locale and periodically changes the surrounding
    markup. The classifier therefore reports the evidence source so callers can
    pause for a human only when an actual challenge is visible.
    """

    url = _page_url(page)
    lowered_url = url.casefold()
    body = _page_body(page)
    path = _url_path(url)

    if "/fido/" in lowered_url or any(
        marker in body
        for marker in (
            "setting up your passkey",
            "create a passkey",
            "security key",
            "passkey",
            "通行密钥",
            "密钥",
            "パスキー",
        )
    ):
        return OutlookPageState("fido_setup", "fido:url-or-text", path)

    px_selector = _first_visible(
        page,
        (
            "#px-captcha",
            'iframe[src*="hsprotect.net"]',
            "iframe#enforcementFrame",
            'iframe[title*="验证"]',
            'iframe[title*="challenge" i]',
        ),
    )
    if px_selector:
        return OutlookPageState("px_challenge", f"dom:{px_selector}", path)
    if any(
        marker in body
        for marker in (
            "let's prove you're human",
            "press and hold",
            "prove you're not a robot",
            "按住",
            "长按",
            "長按",
            "按压验证",
        )
    ):
        return OutlookPageState("px_challenge", "text:human-challenge", path)

    code_selector = _first_visible(
        page,
        (
            'input[name="otc"]',
            "#otc",
            "#proof-confirmation-code-input",
            "#otc-confirmation-input",
            "#iOttText",
            'input[name="ProofConfirmationCode"]',
            'input[name="VerificationCode"]',
            'input[name="ProofConfirmation"]',
            'input[autocomplete="one-time-code"]',
            'input[id^="codeEntry-"]',
            'input[inputmode="numeric"]',
        ),
    )
    if code_selector:
        return OutlookPageState("sms_verify", f"dom:{code_selector}", path)

    recovery_email_markers = (
        "recovery email",
        "alternate email",
        "enter an email address",
        "enter your recovery email",
        "enter an alternate email",
        "add an email address",
        "protect your account",
        "security info",
        "辅助邮箱",
        "恢复邮箱",
        "备用邮箱",
        "添加电子邮件",
        "復原電子郵件",
        "備用電子郵件",
        "回復用メール",
        "복구 이메일",
        "让我们来保护你的帐户",
        "保护你的帐户",
        "協助我們保護您的帳戶",
    )
    recovery_surface = "/proofs/" in lowered_url or any(
        marker in body for marker in recovery_email_markers
    )
    if recovery_surface:
        recovery_email_selector = _first_visible(
            page,
            (
                "#proof-confirmation-email-input",
                'input[name="proofConfirmationEmail"]',
                'input[name="ProofConfirmationEmail"]',
                'input[data-testid="proof-confirmation-email-input"]',
                'input[autocomplete="email"]',
                'input[type="email"]',
            ),
        )
        if recovery_email_selector:
            return OutlookPageState(
                "recovery_email_form",
                f"dom:{recovery_email_selector}",
                path,
            )
        if any(marker in body for marker in recovery_email_markers):
            return OutlookPageState("recovery_email_form", "text:recovery-email", path)

    if any(
        marker in body
        for marker in (
            "enter the code",
            "we texted",
            "we sent",
            "verification code",
            "验证码",
            "短信",
            "コード",
            "code de vérification",
            "código de verificación",
        )
    ):
        return OutlookPageState("sms_verify", "text:verification-code", path)

    if any(
        marker in body
        for marker in (
            "your account has been locked",
            "we've locked",
            "locked for your protection",
            "帐户已锁定",
            "帳戶已鎖定",
            "account è stato bloccato",
            "cuenta ha sido bloqueada",
            "konto wurde gesperrt",
        )
    ):
        return OutlookPageState("locked", "text:account-locked", path)

    if any(
        marker in body
        for marker in (
            "verify your identity",
            "unusual activity",
            "security challenge",
            "异常活动",
            "本人確認",
            "需要验证",
            "verify you are human",
        )
    ):
        return OutlookPageState("verify_needed", "text:security-challenge", path)

    if (
        ("account.microsoft.com" in lowered_url and "login" not in lowered_url)
        or "account.live.com/proofs" in lowered_url
        or ("outlook.live.com/mail/" in lowered_url and "login" not in lowered_url)
    ):
        return OutlookPageState("logged_in", "url:authenticated", path)
    if _first_visible(
        page,
        (
            '[aria-label="新邮件"]',
            '[aria-label="New mail"]',
            '[aria-label="New message"]',
            '[aria-label="新郵件"]',
        ),
    ):
        return OutlookPageState("logged_in", "dom:mail-control", path)
    if any(marker in body for marker in ("new mail", "new message", "收件箱", "inbox")):
        return OutlookPageState("logged_in", "text:mail-control", path)

    if "chrome-error://" in lowered_url:
        return OutlookPageState("net_error", "url:chrome-error", path)
    if any(marker in body for marker in ("something went wrong", "出错了", "問題が発生")):
        return OutlookPageState("error_page", "text:generic-error", path)

    password_selector = _first_visible(
        page,
        ('input[type="password"]', 'input[name="passwd"]', "#passwordEntry"),
    )
    if password_selector:
        return OutlookPageState("login_form", f"dom:{password_selector}", path)
    if any(
        marker in body
        for marker in (
            "enter your password",
            "输入密码",
            "パスワードを入力",
            "entrez votre mot de passe",
            "introduce tu contraseña",
            "kennwort eingeben",
        )
    ):
        return OutlookPageState("login_form", "text:password-form", path)

    email_selector = _first_visible(
        page,
        ('input[type="email"]', 'input[name="loginfmt"]', "#usernameEntry", "#i0116"),
    )
    if email_selector:
        return OutlookPageState("email_form", f"dom:{email_selector}", path)
    if any(
        marker in body
        for marker in (
            "email or phone",
            "sign in",
            "enter your email",
            "电子邮件或电话",
            "メールまたは電話",
            "サインイン",
            "e-mail ou téléphone",
            "correo o teléfono",
            "e-mail oder telefon",
        )
    ):
        return OutlookPageState("email_form", "text:email-form", path)

    return OutlookPageState("unknown", "none", path)


def is_manual_verification(state: OutlookPageState) -> bool:
    return state.name in {"px_challenge", "verify_needed"}


def is_authenticated(state: OutlookPageState) -> bool:
    return state.name == "logged_in"
