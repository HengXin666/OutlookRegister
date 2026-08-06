import random
from patchright.sync_api import sync_playwright
from .base_controller import BaseBrowserController, build_browser_proxy_settings


class PatchrightController(BaseBrowserController):

    def launch_browser(self, proxy=None, playwright=None):
        p = None
        try:
            p = playwright or sync_playwright().start()

            selected_proxy = proxy if proxy is not None else self.get_proxy()
            proxy_settings = build_browser_proxy_settings(selected_proxy)

            b = p.chromium.launch(
                headless=False,            
                args=self.browser_launch_args(),
                proxy=proxy_settings
            )

            return p, b

        except Exception as e:
            if playwright is None and p is not None:
                try:
                    p.stop()
                except Exception:
                    pass
            print(f"启动浏览器失败: {e}")
            return False, False
        
    def handle_captcha(self, page):

        frame1 = page.frame_locator('iframe[title="验证质询"]')
        frame2 = frame1.frame_locator('iframe[style*="display: block"]')

        for _ in range(0, self.max_captcha_retries + 1):
            self.record_captcha_attempt()

            page.wait_for_timeout(random.randint(250, 450))
            loc = frame2.locator('[aria-label="可访问性挑战"]')
            self.smooth_click(page, loc)

            page.wait_for_timeout(random.randint(300, 600))
            loc2 = frame2.locator('[aria-label="再次按下"]')
            self.smooth_click(page, loc2)

            try:
                page.locator('.draw').wait_for(state="detached", timeout=14000)
                try:
                    # 简单的认为加载8秒后成功，暂不考虑请求.
                    page.locator('[role="status"][aria-label="正在加载..."]').wait_for(timeout=5000)

                    captcha_passed = False
                    for _ in range(20):
                        if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                            print("[Error: Rate limit] - 正常通过验证码，但当前IP注册频率过快。")
                            return False
                        elif frame2.locator('[aria-label="可访问性挑战"]').count() > 0:  
                            captcha_passed = False
                            page.wait_for_timeout(random.randint(500, 1000))
                            break
                        elif page.get_by_text("暂时跳过").count() > 0:
                            captcha_passed = True
                            break
                        page.wait_for_timeout(random.randint(375, 425))
                    else:
                        if frame2.locator('[aria-label="可访问性挑战"]').count() == 0:
                            captcha_passed = True

                    if captcha_passed:
                        break

                except Exception:
                    if page.get_by_text('暂时跳过').count() > 0:
                        break
                    frame1.locator(':has-text("请再试一次"), :has-text("Keep going"), :has-text("a few more tries")').first.wait_for(timeout=15000)
                    continue

            except Exception:
                if page.get_by_text('暂时跳过').count() > 0:
                     break
                return False
        else: 
            return False

        return True

    def get_thread_page(self):
        browser = self.get_thread_browser()
        if not browser:
            raise RuntimeError('无法创建注册浏览器')
        context = self.new_browser_context(browser)
        try:
            return context.new_page()
        except Exception:
            try:
                context.close()
            except Exception:
                pass
            raise

    def clean_up(self, page=None, type="all_browser"):
        if type == "done_browser" and page:
            self.close_page_context(page)

        elif type == "all_browser":
            self.close_all_resources()
