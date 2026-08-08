"""账号锁定页按压验证（保活路径）的浏览器层单测。"""

import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from outlookregister.browser.base_controller_interaction import (
    _BaseControllerInteraction,
)
from outlookregister.browser.base_controller_unlock import _BaseUnlockChallenge
from outlookregister.browser.outlook_page_state import OutlookPageState


class _FakeLocator:
    """够用的 locator 替身：只暴露被解锁流程调用到的方法。"""

    def __init__(self, labels=None, count=1, visible=True, box=None):
        self.labels = labels or []
        self._count = count
        self._visible = visible
        self._box = box or {"x": 100.0, "y": 200.0, "width": 240.0, "height": 60.0}
        self.clicked = 0

    def evaluate_all(self, _script):
        return list(self.labels)

    def nth(self, index):
        child = _FakeLocator(count=1, visible=self._visible, box=self._box)
        child.index = index
        return child

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def bounding_box(self):
        return dict(self._box)

    def click(self, **_kwargs):
        self.clicked += 1


class _FakeFrame:
    def __init__(self, url, locators, children=None):
        self.url = url
        self._locators = locators
        self.child_frames = list(children or [])

    def locator(self, selector):
        return self._locators.get(selector, _FakeLocator(count=0, visible=False))


class _FakePage:
    def __init__(self, frames):
        self.frames = frames
        self.mouse = MagicMock()
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def locator(self, _selector):
        return _FakeLocator(count=0, visible=False)


class _UnlockController(_BaseControllerInteraction, _BaseUnlockChallenge):
    def __init__(self, results_dir):
        self.thread_local = threading.local()
        self.results_dir = results_dir
        self.wait_time = 100
        self.prevent_direct_network_leaks = False
        self.attempts = 0
        self.clicks = []

    def record_captcha_attempt(self):
        self.attempts += 1
        return self.attempts

    def smooth_click(self, page, locator, **kwargs):
        self.clicks.append(locator)
        return True


def _states(*names):
    return [OutlookPageState(name, f"test:{name}", "/identity/confirm") for name in names]


class UnlockChallengeTests(unittest.TestCase):
    def test_accessibility_path_mirrors_the_registration_solution(self):
        labels = _FakeLocator(labels=["Accessibility challenge", "Press again"])
        frame = _FakeFrame(
            "https://client.hsprotect.net/challenge?abc=1",
            {"[aria-label]": labels},
        )
        page = _FakePage([_FakeFrame("https://account.live.com/Abuse", {}), frame])

        with tempfile.TemporaryDirectory() as directory:
            controller = _UnlockController(directory)
            with patch(
                "outlookregister.browser.base_controller_unlock.classify_outlook_page",
                side_effect=_states("px_challenge", "logged_in"),
            ):
                solved = controller.solve_unlock_challenge(page, max_attempts=2)

        self.assertTrue(solved)
        # 先点无障碍挑战，再点「再次按下」，与注册流程一致。
        self.assertEqual(len(controller.clicks), 2)
        self.assertEqual(controller.attempts, 1)
        page.mouse.down.assert_not_called()

    def test_press_and_hold_is_used_when_no_accessibility_button_exists(self):
        target = _FakeLocator(labels=["Press and hold"])
        frame = _FakeFrame(
            "https://client.hsprotect.net/challenge",
            {"[aria-label]": target},
        )
        page = _FakePage([frame])

        with tempfile.TemporaryDirectory() as directory:
            controller = _UnlockController(directory)
            with patch(
                "outlookregister.browser.base_controller_unlock.classify_outlook_page",
                # 按住期间轮询到挑战消失即提前松手。
                side_effect=_states(*(["px_challenge"] * 40 + ["logged_in"] * 10)),
            ):
                solved = controller.solve_unlock_challenge(
                    page,
                    max_attempts=1,
                    settle_seconds=2.0,
                )

        self.assertTrue(solved)
        page.mouse.down.assert_called_once()
        page.mouse.up.assert_called_once()

    def test_bounded_attempts_report_failure_without_hanging(self):
        labels = _FakeLocator(labels=["Accessibility challenge", "Press again"])
        frame = _FakeFrame("https://client.hsprotect.net/x", {"[aria-label]": labels})
        page = _FakePage([frame])

        with tempfile.TemporaryDirectory() as directory:
            controller = _UnlockController(directory)
            with patch(
                "outlookregister.browser.base_controller_unlock.classify_outlook_page",
                return_value=OutlookPageState("px_challenge", "test", "/x"),
            ):
                solved = controller.solve_unlock_challenge(
                    page,
                    max_attempts=2,
                    settle_seconds=0.2,
                )

        self.assertFalse(solved)
        self.assertEqual(controller.attempts, 2)

    def test_nested_about_blank_challenge_frame_is_solved_by_content(self):
        # 真实失败现场：hsprotect 外层 frame 里只有空的 #px-captcha 容器，
        # 真正的「无障碍挑战 / 长按」按钮在它动态创建的嵌套 about:blank 帧里。
        challenge_labels = _FakeLocator(
            labels=[
                "アクセス可能なチャレンジ",
                "長押しヒューマンチャレンジ",
                "もう一度押してください",
            ]
        )
        inner = _FakeFrame(
            "about:blank",
            {
                "[aria-label]": challenge_labels,
                '[role="button"][aria-label]': challenge_labels,
            },
        )
        outer = _FakeFrame(
            "https://iframe.hsprotect.net/index.html?app_id=PXzC5j78di&ch_ctx=1",
            {},
            children=[inner],
        )
        page = _FakePage(
            [
                _FakeFrame("https://account.live.com/Abuse", {}),
                _FakeFrame(
                    "https://iframe.hsprotect.net/index.html?app_id=PXzC5j78di",
                    {},
                ),
                outer,
                inner,
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            controller = _UnlockController(directory)
            scopes = controller._unlock_challenge_scopes(page)
            urls = [str(scope.url) for scope in scopes]
            # 内容可确认的 about:blank 挑战帧必须排在 hsprotect 外层帧之前。
            self.assertIn("about:blank", urls)
            self.assertLess(
                urls.index("about:blank"),
                urls.index("https://iframe.hsprotect.net/index.html?app_id=PXzC5j78di"),
            )
            with patch(
                "outlookregister.browser.base_controller_unlock.classify_outlook_page",
                side_effect=_states("px_challenge", "logged_in"),
            ):
                solved = controller.solve_unlock_challenge(page, max_attempts=1)

        self.assertTrue(solved)
        # 无障碍路径：先点「アクセス可能なチャレンジ」，再点「もう一度押してください」。
        self.assertEqual(len(controller.clicks), 2)
        page.mouse.down.assert_not_called()

    def test_click_unlock_continue_uses_the_fluent_primary_button(self):
        primary = _FakeLocator()
        page = MagicMock()
        page.locator.side_effect = lambda selector: (
            primary
            if selector == '[data-testid="primaryButton"]'
            else _FakeLocator(count=0, visible=False)
        )

        with tempfile.TemporaryDirectory() as directory:
            controller = _UnlockController(directory)
            clicked = controller.click_unlock_continue(page)

        self.assertTrue(clicked)
        self.assertEqual(controller.clicks, [primary])


if __name__ == "__main__":
    unittest.main()
