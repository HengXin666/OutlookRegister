"""Browser and OAuth resource ownership helpers."""

from __future__ import annotations

from typing import Any


class _BaseControllerResources:
    def get_thread_browser(self) -> Any:
        if not hasattr(self.thread_local, "browser"):
            playwright, browser = self.launch_browser()
            if not playwright:
                return False
            self.thread_local.playwright = playwright
            self.thread_local.browser = browser
            with self.cleanup_lock:
                self.active_resources.append((playwright, browser))
        return self.thread_local.browser

    def get_oauth_page(self, source_page: Any, proxy: str | None = None) -> Any:
        """Copy the signed-in session while preserving the current flow proxy."""
        selected_proxy = self.get_proxy() if proxy is None else proxy
        shared_playwright = getattr(self.thread_local, "playwright", None)
        playwright, browser = self.launch_browser(
            proxy=selected_proxy,
            playwright=shared_playwright,
        )
        if not playwright:
            return False
        owned_playwright = None if shared_playwright is not None else playwright
        with self.cleanup_lock:
            self.active_resources.append((owned_playwright, browser))
        context = None
        try:
            context = self.new_browser_context(browser)
            cookies = source_page.context.cookies()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            with self.cleanup_lock:
                self.oauth_browsers[id(page)] = (owned_playwright, browser)
            return page
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
            if owned_playwright is not None:
                try:
                    owned_playwright.stop()
                except Exception:
                    pass
            with self.cleanup_lock:
                self.active_resources = [
                    (resource_playwright, resource_browser)
                    for resource_playwright, resource_browser in self.active_resources
                    if resource_browser is not browser
                ]
            raise

    def close_page_context(self, page: Any) -> None:
        """Close a page context and its dedicated OAuth browser, if any."""
        if page is None:
            return
        try:
            context = page.context
        except Exception:
            context = None
        with self.cleanup_lock:
            resource = self.oauth_browsers.pop(id(page), None)
        if isinstance(resource, tuple):
            playwright, browser = resource
        else:
            playwright, browser = None, resource
        traffic = getattr(self, "traffic", None)
        if traffic is not None:
            try:
                traffic.detach_page(page)
            except Exception:
                pass
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
                with self.cleanup_lock:
                    self.active_resources = [
                        (resource_playwright, resource_browser)
                        for resource_playwright, resource_browser in self.active_resources
                        if resource_browser is not browser
                    ]
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def close_all_resources(self) -> None:
        with self.cleanup_lock:
            resources = list(self.active_resources)
            self.active_resources.clear()
            self.oauth_browsers.clear()
        for playwright, browser in resources:
            try:
                browser.close()
            except Exception:
                pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass

    def close_thread_browser(self) -> None:
        """Close the registration browser so a rotated proxy applies next time."""
        browser = getattr(self.thread_local, "browser", None)
        playwright = getattr(self.thread_local, "playwright", None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
            with self.cleanup_lock:
                self.active_resources = [
                    (resource_playwright, resource_browser)
                    for resource_playwright, resource_browser in self.active_resources
                    if resource_browser is not browser
                ]
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        for attribute in ("browser", "playwright"):
            if hasattr(self.thread_local, attribute):
                delattr(self.thread_local, attribute)
