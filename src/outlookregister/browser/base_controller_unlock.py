"""账号锁定页（account.live.com/Abuse）的按压验证自动处理。

保活登录会遇到「账号已锁定 → 按压验证 → 恢复页面」三连页面。注册流程的
``handle_captcha`` 已经证明 HUMAN/PerimeterX 的无障碍挑战按钮可以稳定通过，
这里沿用同一解法（无障碍入口 + 再次按压），并在无障碍按钮缺失时回退到真实的
mouse down/hold/up。

按压必须“尽快开始”：挑战 iframe 由 hsprotect 动态创建（多一层 about:blank），
页面分类看到挑战文本时按钮可能还没渲染。因此这里先短轮询可操作目标，一旦出现
立即执行按压，而不是等整页稳定。

选择器与多语言关键字见 ``base_controller_unlock_markers``。
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

from outlookregister.browser.base_controller_unlock_markers import (
    ACCESSIBILITY_LABEL_MARKERS,
    CHALLENGE_FRAME_MARKERS,
    PRESS_AGAIN_LABEL_MARKERS,
    PRESS_HOLD_LABEL_MARKERS,
    PRESS_HOLD_SELECTORS,
    UNLOCK_CONTINUE_SELECTORS,
)
from outlookregister.browser.outlook_page_state import classify_outlook_page

__all__ = ["UNLOCK_CONTINUE_SELECTORS", "_BaseUnlockChallenge"]

# 这些状态说明挑战还在或页面还在过渡，不能当作“已经通过”。
UNLOCK_BLOCKED_STATES = frozenset({"px_challenge", "locked", "verify_needed"})


class _BaseUnlockChallenge:
    """账号锁定页的解锁挑战处理；只用于保活路径，不改变注册流程。"""

    def click_unlock_continue(self, page: Any) -> bool:
        """点击锁定页/恢复页的主按钮，成功点击返回 True。"""
        for selector in UNLOCK_CONTINUE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if int(locator.count()) <= 0 or not locator.is_visible():
                    continue
            except Exception:
                continue
            if self.smooth_click(page, locator):
                return True
            try:
                locator.click(timeout=8000)
                return True
            except Exception:
                continue
        return False

    def unlock_challenge_visible(self, page: Any) -> bool:
        """页面上是否仍有按压验证。"""
        try:
            return classify_outlook_page(page).name == "px_challenge"
        except Exception:
            return False

    def _page_still_blocked(self, page: Any) -> bool:
        """页面是否仍停留在锁定/挑战/需要验证的状态。"""
        try:
            return classify_outlook_page(page).name in UNLOCK_BLOCKED_STATES
        except Exception:
            return False

    def _challenge_absent_for(self, page: Any, checks: int, interval_ms: int) -> bool:
        """连续多次判定挑战不在（且页面已离开锁定/挑战状态）才算真的过了。

        挑战 iframe 在重绘时会短暂 detach，只看一次会把重绘误判成通过，导致后续
        尝试被跳过；反过来，挑战还没加载出来时也不能把“暂时看不见”当成通过。
        """
        for index in range(max(1, checks)):
            if self._page_still_blocked(page) or self.unlock_challenge_visible(page):
                return False
            if index < checks - 1:
                page.wait_for_timeout(interval_ms)
        return True

    def _wait_for_gesture_target(
        self,
        page: Any,
        timeout_seconds: float = 8.0,
    ) -> bool:
        """短轮询等可操作的按压目标出现；页面已离开挑战则提前返回 False。"""
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            scopes = self._unlock_challenge_scopes(page)
            if scopes and self._scopes_have_target(scopes):
                return True
            if not self.unlock_challenge_visible(page) and not self._page_still_blocked(page):
                return False
            page.wait_for_timeout(250)
        return True

    def _scopes_have_target(self, scopes: list[Any]) -> bool:
        for scope in scopes:
            if self._labeled_target(scope, ACCESSIBILITY_LABEL_MARKERS) is not None:
                return True
            if self._labeled_target(scope, PRESS_HOLD_LABEL_MARKERS) is not None:
                return True
            if self._first_visible_in_scope(scope, PRESS_HOLD_SELECTORS) is not None:
                return True
        return False

    def solve_unlock_challenge(
        self,
        page: Any,
        max_attempts: int = 2,
        settle_seconds: float = 12.0,
    ) -> bool:
        """有界地自动完成账号锁定页的按压验证，通过返回 True。"""
        attempts = max(1, int(max_attempts))
        # 挑战帧可能晚于页面分类出现；先短轮询等目标，出现就立即开始按压。
        self._wait_for_gesture_target(page, timeout_seconds=8.0)
        for attempt in range(1, attempts + 1):
            self.record_captcha_attempt()
            scopes = self._unlock_challenge_scopes(page)
            if not scopes:
                page.wait_for_timeout(random.randint(300, 500))
                if self._challenge_absent_for(page, 3, 600):
                    return True
                continue

            gesture = self._run_unlock_gesture(page, scopes)
            if not gesture:
                print(
                    f"[Keepalive Unlock] 第 {attempt} 次尝试没有找到可操作的按压目标，"
                    "等待挑战控件渲染后重试。",
                    flush=True,
                )
                page.wait_for_timeout(random.randint(400, 700))
                continue

            print(f"[Keepalive Unlock] 第 {attempt} 次尝试已执行 {gesture}。", flush=True)
            if self._wait_for_challenge_cleared(page, settle_seconds):
                print(f"[Keepalive Unlock] 第 {attempt} 次尝试通过。", flush=True)
                return True
            print(f"[Keepalive Unlock] 第 {attempt} 次尝试后挑战仍在。", flush=True)
            page.wait_for_timeout(random.randint(600, 900))

        self._log_challenge_labels(self._unlock_challenge_scopes(page))
        self._save_unlock_diagnostic(page, "keepalive_unlock_failed")
        return False

    @staticmethod
    def _log_challenge_labels(scopes: list[Any]) -> None:
        """打印挑战帧里出现的 aria-label，便于补齐本地化关键字。"""
        for index, scope in enumerate(scopes):
            try:
                labels = scope.locator("[aria-label]").evaluate_all(
                    "els => els.map(el => el.getAttribute('aria-label') || '')"
                )
            except Exception:
                continue
            if not isinstance(labels, list) or not labels:
                continue
            visible = [str(label) for label in labels[:30] if str(label or "").strip()]
            print(
                f"[Keepalive Unlock] 挑战帧 {index} 的 aria-label: {visible}",
                flush=True,
            )

    def _run_unlock_gesture(self, page: Any, scopes: list[Any]) -> str:
        """优先走无障碍挑战按钮（一次点击+再次点击），缺失时回退到真实按住。"""
        for scope in scopes:
            accessibility = self._labeled_target(scope, ACCESSIBILITY_LABEL_MARKERS)
            if accessibility is None:
                continue
            page.wait_for_timeout(random.randint(200, 350))
            self.smooth_click(page, accessibility)
            press_again = self._wait_for_press_again(page)
            if press_again is not None:
                page.wait_for_timeout(random.randint(250, 450))
                self.smooth_click(page, press_again)
                return "accessibility+press_again"
            # 无障碍已生效后主按钮变成单击模式：直接点一次主按钮。
            for target_scope in self._unlock_challenge_scopes(page):
                click_target = self._labeled_target(
                    target_scope, PRESS_HOLD_LABEL_MARKERS
                )
                if click_target is not None:
                    page.wait_for_timeout(random.randint(250, 450))
                    self.smooth_click(page, click_target)
                    return "accessibility+click_once"
            return "accessibility"

        for scope in scopes:
            target = self._labeled_target(scope, PRESS_HOLD_LABEL_MARKERS)
            if target is None:
                target = self._first_visible_in_scope(scope, PRESS_HOLD_SELECTORS)
            if target is None:
                continue
            if self._press_and_hold(page, target):
                return "press_and_hold"
        return ""

    def _wait_for_press_again(self, page: Any) -> Any:
        """点击无障碍入口后，「再次按下/点击一次」按钮可能稍晚才出现，短轮询等它。"""
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            for scope in self._unlock_challenge_scopes(page):
                press_again = self._labeled_target(scope, PRESS_AGAIN_LABEL_MARKERS)
                if press_again is not None:
                    return press_again
            page.wait_for_timeout(random.randint(250, 400))
        return None

    def _unlock_challenge_scopes(self, page: Any) -> list[Any]:
        """挑战控件所在的 frame；找不到时退回主页面本身。

        HUMAN/PerimeterX 的挑战控件并不在 hsprotect.net 那层 frame 里，而在它
        动态创建的嵌套 about:blank 帧中（注册流程的 ``handle_captcha`` 也是先进
        外层 iframe 再进内层 iframe）。这里同时按 URL 特征、嵌套关系与内容特征
        收集，并把内容可确认的挑战帧排在前面。
        """
        frames = self._all_frames(page)
        scopes: list[Any] = []

        marker_frames = [
            frame
            for frame in frames
            if any(
                marker in str(frame.url or "").casefold()
                for marker in CHALLENGE_FRAME_MARKERS
            )
        ]
        content_frames = [
            frame for frame in frames if self._frame_has_challenge_control(frame)
        ]
        nested_frames: list[Any] = []
        for frame in marker_frames:
            for child in self._descendant_frames(frame):
                if child not in marker_frames and child not in content_frames:
                    nested_frames.append(child)

        for frame in content_frames + nested_frames + marker_frames:
            if frame not in scopes:
                scopes.append(frame)

        if not scopes and self.unlock_challenge_visible(page):
            scopes.append(page)
        return scopes

    @staticmethod
    def _all_frames(page: Any) -> list[Any]:
        try:
            frames = page.frames
            if callable(frames):
                frames = frames()
            return list(frames or [])
        except Exception:
            return []

    @staticmethod
    def _descendant_frames(frame: Any) -> list[Any]:
        """frame 的所有后代帧（子帧、孙子帧……），不包含 frame 自身。"""
        found: list[Any] = []
        stack: list[Any] = []
        try:
            children = frame.child_frames
            if callable(children):
                children = children()
            stack.extend(list(children or []))
        except Exception:
            return found
        while stack:
            current = stack.pop(0)
            found.append(current)
            try:
                children = current.child_frames
                if callable(children):
                    children = children()
                stack.extend(list(children or []))
            except Exception:
                continue
        return found

    @staticmethod
    def _frame_has_challenge_control(frame: Any) -> bool:
        """frame 内是否有带无障碍/长按语义的可见挑战控件。"""
        try:
            locator = frame.locator('[role="button"][aria-label]')
            labels = locator.evaluate_all(
                "els => els.map(el => el.getAttribute('aria-label') || '')"
            )
        except Exception:
            return False
        if not isinstance(labels, list):
            return False
        for label in labels:
            text = str(label or "").casefold()
            if any(marker in text for marker in ACCESSIBILITY_LABEL_MARKERS):
                return True
            if any(marker in text for marker in PRESS_HOLD_LABEL_MARKERS):
                return True
        return False

    @staticmethod
    def _labeled_target(scope: Any, markers: tuple[str, ...]) -> Any:
        """在一个 frame/page 内按 aria-label 关键字找到可见控件。"""
        try:
            locator = scope.locator("[aria-label]")
            labels = locator.evaluate_all(
                "els => els.map(el => el.getAttribute('aria-label') || '')"
            )
        except Exception:
            return None
        if not isinstance(labels, list):
            return None
        for index, label in enumerate(labels):
            text = str(label or "").casefold()
            if not any(marker in text for marker in markers):
                continue
            try:
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _first_visible_in_scope(scope: Any, selectors: tuple[str, ...]) -> Any:
        for selector in selectors:
            try:
                locator = scope.locator(selector).first
                if int(locator.count()) > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _press_and_hold(
        self,
        page: Any,
        locator: Any,
        max_hold_seconds: float = 14.0,
        min_hold_seconds: float = 3.5,
    ) -> bool:
        """真实的 mouse down / 抖动保持 / mouse up，挑战消失即提前松手。"""
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if not box:
            return False
        center_x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
        center_y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
        self.smooth_move_to(page, center_x, center_y)
        page.wait_for_timeout(random.randint(120, 260))
        try:
            page.mouse.down()
        except Exception:
            return False
        try:
            release_at = time.monotonic() + max(min_hold_seconds, max_hold_seconds)
            earliest = time.monotonic() + min_hold_seconds
            while time.monotonic() < release_at:
                page.wait_for_timeout(random.randint(180, 320))
                try:
                    page.mouse.move(
                        center_x + random.uniform(-1.2, 1.2),
                        center_y + random.uniform(-1.2, 1.2),
                    )
                except Exception:
                    pass
                if time.monotonic() >= earliest and self._challenge_absent_for(page, 2, 300):
                    break
        finally:
            try:
                page.mouse.up()
            except Exception:
                pass
            self.set_last_pos(center_x, center_y)
        return True

    def _wait_for_challenge_cleared(self, page: Any, settle_seconds: float) -> bool:
        """挑战真正消失（且页面离开锁定/挑战状态）才算通过；重绘空窗不算。"""
        deadline = time.monotonic() + max(1.0, float(settle_seconds))
        while time.monotonic() < deadline:
            if self._challenge_absent_for(page, 3, 500):
                return True
            page.wait_for_timeout(300)
        return False

    def _save_unlock_diagnostic(self, page: Any, name: str) -> None:
        """保留失败现场，便于对照 captcha_attempts.jsonl 排查。"""
        stamp = int(time.time())
        base_path = os.path.join(self.results_dir, "logs", f"{name}_{stamp}")
        os.makedirs(os.path.dirname(base_path), exist_ok=True)
        try:
            page.screenshot(path=f"{base_path}.png", full_page=True)
        except Exception:
            pass
        record: dict[str, Any] = {"url": "", "frames": [], "body_text": ""}
        try:
            record["url"] = str(page.url or "")
        except Exception:
            pass
        try:
            frames = page.frames
            if callable(frames):
                frames = frames()
            record["frames"] = [str(frame.url or "") for frame in list(frames)]
        except Exception:
            pass
        try:
            record["body_text"] = str(page.locator("body").inner_text(timeout=3000) or "")
        except Exception:
            pass
        frame_details = []
        try:
            frames = page.frames
            if callable(frames):
                frames = frames()
            for frame in list(frames):
                detail: dict[str, Any] = {"url": str(frame.url or "")}
                try:
                    detail["aria_labels"] = frame.locator("[aria-label]").evaluate_all(
                        "els => els.map(el => ({a: el.getAttribute('aria-label'), "
                        "tag: (el.tagName||'').toLowerCase(), role: (el.getAttribute('role')||''), "
                        "id: (el.id||''), vis: !!el.offsetParent}))"
                    )
                except Exception:
                    pass
                try:
                    detail["buttons"] = frame.locator("button").evaluate_all(
                        "els => els.map(el => ({txt: (el.innerText||'').trim().slice(0,60), "
                        "a: el.getAttribute('aria-label'), vis: !!el.offsetParent}))"
                    )
                except Exception:
                    pass
                try:
                    detail["role_buttons"] = frame.locator('[role="button"]').evaluate_all(
                        "els => els.map(el => ({txt: (el.innerText||'').trim().slice(0,60), "
                        "a: el.getAttribute('aria-label'), vis: !!el.offsetParent}))"
                    )
                except Exception:
                    pass
                try:
                    html = frame.locator("body").inner_html(timeout=3000)
                    detail["body_html_head"] = html[:6000]
                except Exception:
                    pass
                frame_details.append(detail)
        except Exception:
            pass
        record["frame_details"] = frame_details
        try:
            with open(f"{base_path}.json", "w", encoding="utf-8") as diagnostic:
                json.dump(record, diagnostic, ensure_ascii=False, indent=2)
            print(f"[Keepalive Unlock] 诊断已保存: {base_path}.png/.json", flush=True)
        except OSError:
            pass
