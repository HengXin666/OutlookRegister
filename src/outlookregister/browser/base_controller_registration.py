import random
import time

from faker import Faker


class _BaseRegistration:
    """Microsoft 邮箱注册流程的混合方法。"""

    def outlook_register(self, page, email, password):
        """
        通用逻辑:注册邮箱
        """

        self.reset_last_pos()
        self.thread_local.recovery_email = ''
        self.thread_local.credentials_saved = False
        self.thread_local.captcha_attempts = 0
        self._set_recovery_result()
        outlook_email = f'{email}{self.email_suffix}'
        self._write_account_checkpoint(
            outlook_email,
            password,
            'generated',
            '账号密码已生成，尚未确认 Microsoft 注册结果',
        )
        fake = Faker()

        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1960, 2005))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=20000, wait_until="domcontentloaded")
            consent_btn = page.get_by_text('同意并继续')
            consent_btn.wait_for(timeout=30000)
            start_time = time.time()
            self.wait_random_ratio(page, 0.06)
            self.smooth_click(page, consent_btn)
        except Exception as exc:
            self._write_account_checkpoint(
                outlook_email,
                password,
                'navigation_failed',
                str(exc),
            )
            print("[Error: IP] - IP质量不佳，无法进入注册界面。")
            return False

        try:
            if self.email_suffix == "@hotmail.com":
                self.wait_random_ratio(page, 0.06)
                domain_btn = page.get_by_text("@outlook.com")
                self.smooth_click(page, domain_btn)
                option_btn = page.locator('[role="option"]:text-is("@hotmail.com")')
                self.smooth_click(page, option_btn)


            email_input = page.locator('[aria-label="新建电子邮件"]')
            self.smooth_type(page, email_input, email)

            primary_btn = page.locator('[data-testid="primaryButton"]')
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.04)

            pwd_input = page.locator('[type="password"]')
            self.smooth_type(page, pwd_input, password)
            self.wait_random_ratio(page, 0.03)
            self.smooth_click(page, primary_btn)
            self.wait_random_ratio(page, 0.03)

            if page.get_by_text("请重试。如果仍然不起作用，请稍后再试。").count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'registration_rejected',
                    'Microsoft 提示请稍后重试',
                )
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            year_input = page.locator('[name="BirthYear"]')
            if year_input.count() > 0:
                self.smooth_click(page, year_input)
                year_input.fill(year)

            month_btn = page.locator('[name="BirthMonth"]')
            self.smooth_click(page, month_btn)
            self.wait_random_ratio(page, 0.03)
            m_opt = page.locator(f'[role="option"]:text-is("{month}月")')
            self.smooth_click(page, m_opt)

            self.wait_random_ratio(page, 0.03)
            day_btn = page.locator('[name="BirthDay"]')
            self.smooth_click(page, day_btn)
            self.wait_random_ratio(page, 0.03)

            d_opt = page.locator(f'[role="option"]:text-is("{day}日")')
            if d_opt.count() > 0:
                try:
                    d_opt.scroll_into_view_if_needed()
                except Exception:
                    pass
            self.smooth_click(page, d_opt)

            self.smooth_click(page, primary_btn)

            lname_input = page.locator('#lastNameInput')
            lname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, lname_input, lastname)

            self.wait_random_ratio(page, 0.02)
            fname_input = page.locator('#firstNameInput')
            fname_input.wait_for(state='visible', timeout=8000)
            self.smooth_type(page, fname_input, firstname)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            self.smooth_click(page, primary_btn)
            self._write_account_checkpoint(
                outlook_email,
                password,
                'profile_submitted',
                '姓名资料已提交，等待按压验证及 Microsoft 创建结果',
            )
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            if page.get_by_text('一些异常活动').count() or page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'registration_rejected',
                    'Microsoft 提示异常活动或站点维护',
                )
                print("[Error: IP or browser] - 当前IP注册频率过快。检查IP与是否为指纹浏览器并关闭了无头模式。")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                self._write_account_checkpoint(
                    outlook_email, password, 'captcha_unsupported',
                    '出现 FunCaptcha，当前流程仅支持按压验证码',
                )
                print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            captcha_error = ''
            try:
                captcha_result = self.handle_captcha(page)
            except Exception as exc:
                captcha_result = False
                captcha_error = str(exc)

            if not captcha_result:
                if not self._wait_for_account_created(page):
                    detail = captcha_error or '按压次数用尽，且未发现账号创建成功的页面证据'
                    self._write_account_checkpoint(
                        outlook_email,
                        password,
                        'registration_unconfirmed',
                        detail,
                    )
                    print(f'[Error: Captcha] - {detail}')
                    return False
                print(
                    '[Warning: Captcha Result] - 按压处理返回失败，但页面已进入注册后阶段，'
                    '按账号注册成功继续处理。'
                )
                creation_evidence = 'captcha_returned_false_but_post_signup_page_visible'
            else:
                creation_evidence = 'captcha_handler_completed'

            self._save_registered_credentials(
                outlook_email,
                password,
                creation_evidence,
            )

            page.wait_for_timeout(1000)
            if not self.handle_recovery_email(page):
                recovery_detail = getattr(
                    self.thread_local, 'recovery_result', {}
                ).get('detail', '')
                self._write_account_checkpoint(
                    outlook_email,
                    password,
                    'recovery_failed',
                    recovery_detail,
                )
                self._write_recovery_result(outlook_email)
                return False

        except Exception as exc:
            if (
                not self.thread_local.credentials_saved
                and self._account_created_visible(page)
            ):
                self._save_registered_credentials(
                    outlook_email,
                    password,
                    'exception_but_post_signup_page_visible',
                )
            stage = (
                'post_registration_failed'
                if self.thread_local.credentials_saved
                else 'registration_unconfirmed'
            )
            self._write_account_checkpoint(outlook_email, password, stage, str(exc))
            print(f'[Error: Registration Flow] - {exc}')
            return False

        # Idempotent fallback for future controller implementations that reach here directly.
        self._save_registered_credentials(
            outlook_email,
            password,
            'registration_flow_completed',
        )
        print(f'[Success: Email Registration] - {outlook_email}: {password}')

        if not self.enable_oauth2:
            self._write_recovery_result(outlook_email)
            return True

        start_skip_time = time.time()
        while time.time() - start_skip_time < 20:
            try:
                if self._recovery_page_visible(page):
                    if not self.handle_recovery_email(page):
                        recovery_detail = getattr(
                            self.thread_local, 'recovery_result', {}
                        ).get('detail', '')
                        self._write_account_checkpoint(
                            outlook_email,
                            password,
                            'recovery_failed',
                            recovery_detail,
                        )
                        self._write_recovery_result(outlook_email)
                        return False
                    continue
                btn_skip = page.get_by_text("暂时跳过")
                if btn_skip.count() > 0 and btn_skip.is_visible():
                    self.smooth_click(page, btn_skip)
                    page.wait_for_timeout(random.randint(1000, 1500))
                else:
                    btn_skip.wait_for(timeout=7000)
            except Exception:
                break

        try:
            page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
            return True
        except Exception:
            print('[Error: Timeout] - 邮箱未初始化，无法正常收件。')
            return False
        finally:
            self._write_recovery_result(outlook_email)
