"""账号锁定页/按压验证的选择器与多语言 aria-label 关键字。

保活的浏览器语言由出口 IP 决定，不一定是中文，所以控件一律按 URL 与 aria-label
关键字匹配，不依赖单一语言的 title/文案。关键字同时用于：挑战帧内容识别、
无障碍入口识别、按压/再按按钮识别；新增语言时只需在这里补关键词。
"""

from __future__ import annotations

# 锁定页与恢复页的主按钮。Fluent UI 的 data-testid 跨语言稳定，其余选择器覆盖
# 旧版 login.live.com 页面和常见语言的按钮文案。
UNLOCK_CONTINUE_SELECTORS: tuple[str, ...] = (
    '[data-testid="primaryButton"]',
    "#idSIButton9",
    "#iNext",
    'input[type="submit"]',
    'button[type="submit"]',
    'button:has-text("Next")',
    'button:has-text("Continue")',
    'button:has-text("Sign in")',
    'button:has-text("继续")',
    'button:has-text("下一步")',
    'button:has-text("繼續")',
    'button:has-text("次へ")',
    'button:has-text("開始")',
    'button:has-text("はじめる")',
)

# 挑战 iframe 的 URL 特征（HUMAN/PerimeterX 与 Microsoft 的强制执行帧）。
CHALLENGE_FRAME_MARKERS: tuple[str, ...] = (
    "hsprotect.net",
    "perimeterx",
    "px-captcha",
    "captcha",
    "enforcement",
    "fpt.live.com",
)

# 无障碍挑战入口：点击后按住动作会被替换成一次普通点击。
ACCESSIBILITY_LABEL_MARKERS: tuple[str, ...] = (
    "可访问性挑战",
    "可訪問性挑戰",
    "accessibility challenge",
    "accessibility",
    "accessible",
    "アクセシビリティ",
    "アクセス可能",
    "접근성",
    "barrierefrei",
    "accessibilité",
    "accesibilidad",
    "acessibilidade",
    "доступность",
    "dostępność",
    "toegankelijkheid",
)

# 无障碍入口点击后出现的“请再按一次/点击一次”按钮。
PRESS_AGAIN_LABEL_MARKERS: tuple[str, ...] = (
    "再次按下",
    "再按一次",
    "请再按一次",
    "点击一次",
    "按一次",
    "press again",
    "click once",
    "press once",
    "press the button",
    "click the button",
    "click here",
    "もう一度",
    "もういちど",
    "押してください",
    "クリック",
    "押して",
    "다시 누르",
    "다시 클릭",
    "presiona de nuevo",
    "pulsa de nuevo",
    "appuyez à nouveau",
    "cliquez",
    "erneut drücken",
    "klicken",
)

# 真实按压目标（无障碍入口不存在时使用）。
PRESS_HOLD_LABEL_MARKERS: tuple[str, ...] = (
    "按住",
    "长按",
    "長按",
    "press and hold",
    "press & hold",
    "hold the button",
    "hold to continue",
    "長押し",
    "押し続け",
    "누르고 있",
    "길게 누르",
    "mantén pulsado",
    "mantenga pulsado",
    "maintenez",
    "gedrückt halten",
    "halten",
    "premi e tieni",
    "вдерживайте",
    "притримайте",
)

PRESS_HOLD_SELECTORS: tuple[str, ...] = (
    "#px-captcha",
    "#px-captcha-wrapper",
    '[role="button"]',
    "button",
    "a[role='button']",
)
