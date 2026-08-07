"""Browser context options and human-like pointer/input helpers."""

from __future__ import annotations

import random
from typing import Any


class _BaseControllerInteraction:
    def browser_context_options(self) -> dict[str, str]:
        options: dict[str, str] = {}
        browser_locale = str(
            getattr(
                self.thread_local,
                "browser_locale",
                getattr(self, "browser_locale", ""),
            )
            or ""
        ).strip()
        browser_timezone = str(
            getattr(
                self.thread_local,
                "browser_timezone",
                getattr(self, "browser_timezone", ""),
            )
            or ""
        ).strip()
        if browser_locale:
            options["locale"] = browser_locale
        if browser_timezone:
            options["timezone_id"] = browser_timezone
        return options

    def new_browser_context(self, browser: Any) -> Any:
        options = self.browser_context_options()
        return browser.new_context(**options) if options else browser.new_context()

    def browser_launch_args(self) -> list[str]:
        browser_locale = str(
            getattr(
                self.thread_local,
                "browser_locale",
                getattr(self, "browser_locale", ""),
            )
            or ""
        ).strip()
        args = [f"--lang={browser_locale}"] if browser_locale else []
        if self.prevent_direct_network_leaks and not getattr(self, "debug", False):
            args.extend(
                (
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-quic",
                )
            )
        return args

    def get_last_pos(self) -> tuple[float, float] | None:
        return getattr(self.thread_local, "last_pos", None)

    def set_last_pos(self, x: float, y: float) -> None:
        self.thread_local.last_pos = (float(x), float(y))

    def reset_last_pos(self) -> None:
        if hasattr(self.thread_local, "last_pos"):
            delattr(self.thread_local, "last_pos")

    def wait_random_ratio(self, page: Any, min_ratio: float, delta: float = 0.02) -> None:
        actual_ratio = random.uniform(min_ratio, min_ratio + delta)
        page.wait_for_timeout(actual_ratio * self.wait_time)

    def smooth_move_to(
        self,
        page: Any,
        target_x: float,
        target_y: float,
        steps: int | None = None,
    ) -> None:
        last_pos = self.get_last_pos()
        if not last_pos:
            last_pos = (random.uniform(150, 450), random.uniform(100, 350))
            try:
                page.mouse.move(last_pos[0], last_pos[1])
            except Exception:
                pass
        if steps is None:
            steps = random.randint(6, 14)
        try:
            page.mouse.move(target_x, target_y, steps=steps)
        except Exception:
            pass
        self.set_last_pos(target_x, target_y)

    def smooth_click(
        self,
        page: Any,
        locator: Any,
        offset_range: float = 5,
        click_delay_range: tuple[int, int] = (60, 160),
    ) -> bool:
        try:
            box = locator.bounding_box()
            if not box:
                locator.click()
                return False
            tx = box["x"] + box["width"] / 2 + random.uniform(-offset_range, offset_range)
            ty = box["y"] + box["height"] / 2 + random.uniform(-offset_range, offset_range)
            self.smooth_move_to(page, tx, ty)
            page.wait_for_timeout(random.randint(*click_delay_range))
            page.mouse.click(tx, ty)
            self.set_last_pos(tx, ty)
            return True
        except Exception:
            try:
                locator.click()
            except Exception:
                pass
            return False

    def smooth_type(
        self,
        page: Any,
        locator: Any,
        text: str,
        click_first: bool = True,
    ) -> None:
        if click_first:
            self.smooth_click(page, locator)
        for char in text:
            try:
                locator.type(char, delay=random.randint(40, 110))
            except Exception:
                break
