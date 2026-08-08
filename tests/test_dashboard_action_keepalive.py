"""锁定保活、暂停恢复、日志上限与关停测试。"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from outlookregister.browser.outlook_page_state import OutlookPageState
from outlookregister.dashboard.dashboard_actions import (
    AUTHORIZE,
    KEEPALIVE,
    DashboardActionError,
    DashboardActionRunner,
)
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveContext


class DashboardActionRunnerTests(unittest.TestCase):
    def test_locked_keepalive_auto_unlocks_through_the_press_challenge(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            controller.click_unlock_continue.return_value = True
            controller.solve_unlock_challenge.return_value = True
            states = [
                # 锁定页 -> 按压页 -> 恢复页面 -> 循环复检
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertEqual(manual_verification.call_count, 0)
        self.assertEqual(controller.click_unlock_continue.call_count, 1)
        controller.solve_unlock_challenge.assert_called_once_with(page, 2)
        self.assertEqual(state["steps"]["manual_challenge"], "completed")
        self.assertTrue(
            any("账号锁定页已自动解锁" in entry["message"] for entry in state["logs"])
        )

    def test_press_challenge_without_locked_page_is_also_automated(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            controller.solve_unlock_challenge.return_value = True
            states = [
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertEqual(manual_verification.call_count, 0)
        # 直接落在按压页时不需要先点锁定页的继续按钮。
        self.assertEqual(controller.click_unlock_continue.call_count, 0)
        controller.solve_unlock_challenge.assert_called_once_with(page, 2)

    def test_stay_signed_in_prompt_is_auto_confirmed(self):
        """KMSI "Stay signed in?" 页不再被误判为邮箱输入页卡住，而是自动点 Yes 继续。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()

            def fake_locator(selector):
                loc = MagicMock()
                if selector == 'button[data-testid="primaryButton"]':
                    loc.first.count.return_value = 1
                    loc.first.is_visible.return_value = True
                else:
                    loc.first.count.return_value = 0
                    loc.first.is_visible.return_value = False
                return loc

            page.locator.side_effect = fake_locator
            states = [
                OutlookPageState("kmsi", "text:stay-signed-in", "/login.srf"),
                OutlookPageState("kmsi", "text:stay-signed-in", "/login.srf"),
                OutlookPageState("login_form", "dom:input[type=password]", "/login.srf"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertEqual(manual_verification.call_count, 0)
        # 主按钮（Yes）应被拟人点击过。
        page.locator.assert_any_call('button[data-testid="primaryButton"]')

    def test_locked_keepalive_without_continue_button_waits_for_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            controller.click_unlock_continue.return_value = False
            # 页面上也没有任何通用主按钮可点。
            page.locator.return_value.first.count.return_value = 0
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)
        self.assertEqual(controller.solve_unlock_challenge.call_count, 0)
        self.assertIn("自动化已停止并保留浏览器", manual_verification.call_args.args[2])

    def test_locked_keepalive_falls_back_to_manual_after_bounded_hold_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            controller.click_unlock_continue.return_value = True
            controller.solve_unlock_challenge.return_value = False
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)
        # 自动尝试有界：用尽后剩下的按压页直接交给人工，不再反复自动点击。
        self.assertEqual(controller.solve_unlock_challenge.call_count, 2)

    def test_failed_press_attempt_retries_a_second_round_before_asking_a_human(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            controller.click_unlock_continue.return_value = True
            # 第一轮按压没过，第二轮通过：中间不应该打扰人。
            controller.solve_unlock_challenge.side_effect = [False, True]
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("px_challenge", "dom:#px-captcha", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {"keepalive": {"login_timeout_seconds": 30}},
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertEqual(manual_verification.call_count, 0)
        self.assertEqual(controller.solve_unlock_challenge.call_count, 2)

    def test_auto_unlock_can_be_disabled_by_config(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            states = [
                OutlookPageState("locked", "text:account-locked", "/identity/confirm"),
                OutlookPageState("logged_in", "url:authenticated", "/mail/0/"),
            ]

            with (
                patch("outlookregister.dashboard.dashboard_actions.classify_outlook_page", side_effect=states),
                patch.object(runner, "_await_manual_verification") as manual_verification,
            ):
                result = runner._login_outlook_account(
                    page,
                    controller,
                    "user@outlook.com",
                    "private-password",
                    "",
                    None,
                    {
                        "keepalive": {
                            "login_timeout_seconds": 30,
                            "auto_unlock_locked_account": False,
                        }
                    },
                )
            runner.shutdown()

        self.assertEqual(result.name, "logged_in")
        self.assertGreaterEqual(manual_verification.call_count, 1)
        self.assertEqual(controller.click_unlock_continue.call_count, 0)
        self.assertEqual(controller.solve_unlock_challenge.call_count, 0)

    def test_keepalive_pause_blocks_at_checkpoint_until_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            started = threading.Event()
            checkpoint = threading.Event()
            continued = threading.Event()
            observed_options = {}

            def execute(email, action, state=None):
                observed_options.update(runner._action_options(email, action))
                runner._mark_browser_open(email, action)
                started.set()
                checkpoint.wait(timeout=2)
                runner._wait_if_paused(email, action)
                continued.set()
                return "done"

            runner._execute_action = execute
            runner.submit(
                "user@outlook.com",
                KEEPALIVE,
                {"auth_mode": "recovery"},
            )
            self.assertTrue(started.wait(timeout=2))

            paused = runner.pause("user@outlook.com", KEEPALIVE)
            with self.assertRaises(DashboardActionError) as overlapping:
                runner.submit("user@outlook.com", AUTHORIZE)
            self.assertEqual(overlapping.exception.status_code, 409)
            checkpoint.set()
            self.assertEqual(paused["status"], "pausing")
            self.assertFalse(continued.wait(timeout=0.1))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                paused_state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if paused_state["status"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(paused_state["status"], "paused")

            resumed = runner.resume("user@outlook.com", KEEPALIVE)
            self.assertEqual(resumed["status"], "running")
            self.assertTrue(continued.wait(timeout=2))
            self.assertGreaterEqual(
                runner._paused_seconds("user@outlook.com", KEEPALIVE),
                0.09,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(observed_options["auth_mode"], "recovery")
        self.assertTrue(
            any("浏览器保持打开" in entry["message"] for entry in state["logs"])
        )

    def test_pause_rejects_non_keepalive_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            with self.assertRaises(DashboardActionError) as raised:
                runner.pause("user@outlook.com", AUTHORIZE)
            runner.shutdown()

        self.assertEqual(raised.exception.status_code, 409)

    def test_action_logs_are_bounded_and_public_errors_redact_transport_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            runner._config = lambda: {
                "proxy": "http://proxy-user:proxy-pass@127.0.0.1:2334",
                "proxy_rotation": {
                    "control_url": "https://proxy.example/ctl/control-secret",
                },
                "recovery_email": {"hx_email": {"api_key": "hx-secret"}},
            }
            for index in range(125):
                runner._set_state(
                    "user@outlook.com",
                    KEEPALIVE,
                    "running",
                    f"log-{index}",
                )
            state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
            detail = runner._public_error(
                "user@outlook.com",
                "http://other-user:other-pass@example.test/path "
                "https://proxy.example/ctl/control-secret?token=query-secret "
                "Bearer bearer-secret hx-secret",
            )
            runner.shutdown()

        self.assertEqual(len(state["logs"]), 100)
        self.assertEqual(state["logs"][0]["message"], "log-25")
        self.assertNotIn("other-user", detail)
        self.assertNotIn("other-pass", detail)
        self.assertNotIn("control-secret", detail)
        self.assertNotIn("query-secret", detail)
        self.assertNotIn("bearer-secret", detail)
        self.assertNotIn("hx-secret", detail)

    def test_shutdown_unblocks_a_paused_keepalive_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            started = threading.Event()
            checkpoint = threading.Event()

            def execute(email, action, state=None):
                started.set()
                checkpoint.wait(timeout=2)
                runner._wait_if_paused(email, action)
                return "unexpected"

            runner._execute_action = execute
            runner.submit("user@outlook.com", KEEPALIVE)
            self.assertTrue(started.wait(timeout=2))
            runner.pause("user@outlook.com", KEEPALIVE)
            checkpoint.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "paused")

            runner.shutdown()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                if state["status"] == "failed":
                    break
                time.sleep(0.01)

        self.assertEqual(state["status"], "failed")
        self.assertIn("服务正在关闭", state["message"])


    def test_keepalive_failure_waits_for_operator_then_resume_same_browser(self):
        """保活异常路径：浏览器/代理/证据全保留且不退出，点击继续后同浏览器继续。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            controller = MagicMock()
            proxy_pool = MagicMock()
            proxy_lease = MagicMock()
            context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=controller,
                proxy_pool=proxy_pool,
                proxy_lease=proxy_lease,
                flow_id="flow-1",
                worker_id="worker-1",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: context
            failing_login = MagicMock(
                side_effect=DashboardActionError(
                    "Outlook 登录停留在邮箱输入页，账号可能已被拒绝或不可用"
                )
            )
            runner._login_keepalive = failing_login
            outcome = {}

            def run_keepalive():
                try:
                    outcome["message"] = runner._keepalive("user@outlook.com")
                except Exception as exc:  # pragma: no cover
                    outcome["error"] = exc

            thread = threading.Thread(target=run_keepalive)
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = (
                    runner.snapshot()
                    .get("user@outlook.com", {})
                    .get(KEEPALIVE)
                    or {}
                )
                if state.get("status") == "manual_verification_required":
                    break
                time.sleep(0.01)
            self.assertEqual(state.get("status"), "manual_verification_required")
            preserved = runner._preserved_keepalive["user@outlook.com"]
            record_path = results / "logs" / "keepalive_page_records.jsonl"
            self.assertTrue(record_path.exists())
            # 等待人工期间：浏览器/代理/会话全部保留，绝不清洗。
            controller.clean_up.assert_not_called()
            controller.close_thread_browser.assert_not_called()
            controller.hx_email.close.assert_not_called()
            controller.clear_flow_context.assert_not_called()
            proxy_pool.release.assert_not_called()
            self.assertIsNotNone(state.get("page_record"))

            # 用户点击“继续”后，同一 context 继续：登录改为成功，最终正常收尾。
            runner._login_keepalive = lambda context: (MagicMock(), "")
            runner._complete_keepalive = (
                lambda context, login_state, resume_destination: "保活登录完成"
            )
            resumed = runner.resume("user@outlook.com", KEEPALIVE, "manual_challenge")
            thread.join(timeout=5)
            final_state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
            runner.shutdown()

        self.assertNotIn("error", outcome)
        self.assertIs(preserved, context)
        self.assertEqual(resumed["status"], "manual_verification_required")
        self.assertEqual(outcome["message"], "保活登录完成")
        # _keepalive 本身不发布 succeeded（由 _run 收尾），保留线程结束时为 running。
        self.assertEqual(final_state["status"], "running")
        self.assertEqual(runner._preserved_keepalive, {})
        controller.clean_up.assert_called()
        controller.close_thread_browser.assert_called()
        controller.hx_email.close.assert_called()
        proxy_pool.release.assert_called_once()

    def test_keepalive_success_still_cleans_up_browser(self):
        """保活成功路径：仍按原逻辑正常关闭浏览器并释放代理。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            controller = MagicMock()
            proxy_pool = MagicMock()
            context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=controller,
                proxy_pool=proxy_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-1",
                worker_id="worker-1",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: context
            runner._login_keepalive = lambda context: (MagicMock(), "")
            runner._complete_keepalive = (
                lambda context, login_state, resume_destination: "保活登录完成"
            )
            result = runner._keepalive("user@outlook.com")
            runner.shutdown()

        self.assertEqual(result, "保活登录完成")
        controller.clean_up.assert_called()
        controller.close_thread_browser.assert_called()
        controller.hx_email.close.assert_called()
        proxy_pool.release.assert_called_once()
        self.assertEqual(runner._preserved_keepalive, {})

    def test_keepalive_restart_cleans_up_preserved_browser_first(self):
        """用户重新提交保活：先显式清理保留的旧浏览器，再启动新流程。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            first_controller = MagicMock()
            first_pool = MagicMock()
            first_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=first_controller,
                proxy_pool=first_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-1",
                worker_id="worker-1",
                page=None,
                traffic_started=False,
            )
            runner._prepare_keepalive_context = lambda email: first_context
            runner._login_keepalive = MagicMock(
                side_effect=DashboardActionError("模拟保活失败")
            )
            # 浏览器尚未打开（page=None）时异常直接上抛并保留失败现场；
            # 下次重新提交保活时先清理这个保留现场。
            with self.assertRaises(DashboardActionError):
                runner._keepalive("user@outlook.com")
            self.assertIn("user@outlook.com", runner._preserved_keepalive)
            first_controller.clean_up.assert_not_called()

            second_controller = MagicMock()
            second_pool = MagicMock()
            second_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=second_controller,
                proxy_pool=second_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-2",
                worker_id="worker-1",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: second_context
            runner._login_keepalive = lambda context: (MagicMock(), "")
            runner._complete_keepalive = (
                lambda context, login_state, resume_destination: "保活登录完成"
            )
            result = runner._keepalive("user@outlook.com")
            runner.shutdown()

        self.assertEqual(result, "保活登录完成")
        first_controller.clean_up.assert_called()
        first_controller.close_thread_browser.assert_called()
        first_pool.release.assert_called_once()
        self.assertEqual(runner._preserved_keepalive, {})

    def test_keepalive_email_form_rounds_wait_for_operator_and_resume_continues(self):
        """email_form 8 轮不再 fatal：进入人工等待，点击继续后从当前页恢复。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            controller = MagicMock()
            email_form = OutlookPageState("email_form", "input:loginfmt", "/login.srf")
            logged_in = OutlookPageState("logged_in", "url:authenticated", "/mail/0/")
            states = [email_form] * 16 + [logged_in] * 2
            ctx = {
                "timeout_seconds": 60,
                "manual_timeout": 5,
                "started_at": time.monotonic(),
                "paused_at_start": 0.0,
                "net_errors": 0,
                "unknown_rounds": 0,
                "email_rounds": 0,
                "kmsi_rounds": 0,
                "last_state_name": "",
            }
            outcome = {}

            def run_loop():
                try:
                    outcome["result"] = runner._login_outlook_loop(
                        page,
                        controller,
                        "user@outlook.com",
                        "private-password",
                        "",
                        None,
                        {"keepalive": {"login_timeout_seconds": 60}},
                        ctx,
                    )
                except Exception as exc:  # pragma: no cover
                    outcome["error"] = exc

            with patch(
                "outlookregister.dashboard.dashboard_actions.classify_outlook_page",
                side_effect=states,
            ):
                thread = threading.Thread(target=run_loop)
                thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = (
                        runner.snapshot()
                        .get("user@outlook.com", {})
                        .get(KEEPALIVE)
                        or {}
                    )
                    if state.get("status") == "manual_verification_required":
                        break
                    time.sleep(0.01)
                self.assertEqual(state.get("status"), "manual_verification_required")
                resumed = runner.resume("user@outlook.com", KEEPALIVE)
                thread.join(timeout=5)
                final_state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
                runner.shutdown()

        self.assertNotIn("error", outcome)
        self.assertFalse(thread.is_alive())
        self.assertEqual(resumed["status"], "manual_verification_required")
        self.assertEqual(final_state["status"], "running")
        self.assertEqual(outcome["result"].name, "logged_in")

    def test_keepalive_manual_wait_timeout_keeps_waiting_then_resume_continues(self):
        """KEEPALIVE 人工等待超时不 raise：保持等待状态，resume 后继续。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            page = MagicMock()
            outcome = {}

            def run_wait():
                try:
                    outcome["done"] = runner._await_manual_verification(
                        "user@outlook.com",
                        KEEPALIVE,
                        "请完成页面操作后点击继续",
                        timeout_seconds=1,
                        page=page,
                        retry_on_timeout=True,
                    )
                except Exception as exc:  # pragma: no cover
                    outcome["error"] = exc

            thread = threading.Thread(target=run_wait)
            thread.start()
            deadline = time.monotonic() + 5
            timed_out = False
            while time.monotonic() < deadline:
                state = (
                    runner.snapshot()
                    .get("user@outlook.com", {})
                    .get(KEEPALIVE)
                    or {}
                )
                if any(
                    "等待人工验证超时" in entry.get("message", "")
                    for entry in state.get("logs", [])
                ):
                    timed_out = True
                    break
                time.sleep(0.01)
            self.assertTrue(timed_out)
            self.assertEqual(state.get("status"), "manual_verification_required")
            runner.resume("user@outlook.com", KEEPALIVE)
            thread.join(timeout=5)
            final_state = runner.snapshot()["user@outlook.com"][KEEPALIVE]
            runner.shutdown()

        self.assertNotIn("error", outcome)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(outcome["done"])
        self.assertEqual(final_state["status"], "running")

    def test_manual_verification_timeout_still_raises_for_non_keepalive(self):
        """非 keepalive 的等待超时行为保持原样：仍抛出超时错误。"""
        with tempfile.TemporaryDirectory() as directory:
            runner = DashboardActionRunner(directory, directory, max_workers=1)
            with self.assertRaises(DashboardActionError) as raised:
                runner._await_manual_verification(
                    "user@outlook.com",
                    AUTHORIZE,
                    "请完成页面操作",
                    timeout_seconds=1,
                )
            runner.shutdown()

        self.assertIn("人工验证等待超时", str(raised.exception))

    def test_keepalive_manual_wait_can_be_superseded_by_restart(self):
        """manual_verification_required 时重新提交保活：不 409，旧线程退出且旧浏览器被清理。

        这正是前端「开始执行」在等待人工验证/失败状态下点击时走的后端路径：
        新提交取代旧流程，而不是报「该账号已有操作正在执行」。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)

            first_controller = MagicMock()
            first_pool = MagicMock()
            first_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=first_controller,
                proxy_pool=first_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-1",
                worker_id="worker-1",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: first_context
            runner._login_keepalive = MagicMock(
                side_effect=DashboardActionError("模拟保活失败，等待人工")
            )
            first_submit = runner.submit("user@outlook.com", KEEPALIVE)
            self.assertEqual(first_submit["status"], "queued")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = (
                    runner.snapshot()
                    .get("user@outlook.com", {})
                    .get(KEEPALIVE)
                    or {}
                )
                if state.get("status") == "manual_verification_required":
                    break
                time.sleep(0.01)
            self.assertEqual(state.get("status"), "manual_verification_required")
            self.assertIn("user@outlook.com", runner._preserved_keepalive)

            # 用户点击「开始执行」重新提交：不应 409，而应取代旧流程。
            second_controller = MagicMock()
            second_pool = MagicMock()
            second_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=second_controller,
                proxy_pool=second_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-2",
                worker_id="worker-2",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: second_context
            runner._login_keepalive = lambda context: (MagicMock(), "")
            runner._complete_keepalive = (
                lambda context, login_state, resume_destination: "保活登录完成"
            )
            second_submit = runner.submit("user@outlook.com", KEEPALIVE)
            self.assertEqual(second_submit["status"], "queued")

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = (
                    runner.snapshot()
                    .get("user@outlook.com", {})
                    .get(KEEPALIVE)
                    or {}
                )
                if state.get("status") == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state.get("status"), "succeeded")
        # 旧浏览器由新流程 _discard_preserved_keepalive 显式清理（用户已确认关闭）。
        first_controller.clean_up.assert_called()
        first_controller.close_thread_browser.assert_called()
        first_pool.release.assert_called_once()
        # 旧线程被取代后不得把状态覆盖成 failed/running。
        self.assertNotEqual(state.get("status"), "failed")
        self.assertEqual(runner._preserved_keepalive, {})

    def test_keepalive_midflight_superseded_cleans_own_browser(self):
        """旧线程尚未进入保留表时被重新开始取代：旧线程自行清理自己的浏览器/代理。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "Results"
            results.mkdir()
            (results / "account_checkpoints.jsonl").write_text(
                json.dumps(
                    {
                        "outlook_email": "user@outlook.com",
                        "password": "private-password",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner = DashboardActionRunner(root, results, max_workers=1)
            gate = threading.Event()
            entered_login = threading.Event()

            first_controller = MagicMock()
            first_pool = MagicMock()
            first_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=first_controller,
                proxy_pool=first_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-1",
                worker_id="worker-1",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: first_context

            def blocking_login(context):
                entered_login.set()
                gate.wait(timeout=5)
                return MagicMock(), ""

            runner._login_keepalive = blocking_login
            first_submit = runner.submit("user@outlook.com", KEEPALIVE)
            self.assertEqual(first_submit["status"], "queued")
            # 等旧线程真正进入登录阶段（阻塞在 gate 上，尚未保留浏览器）。
            self.assertTrue(entered_login.wait(timeout=5))

            # 重新提交取代旧流程；新流程直接成功收尾。
            second_controller = MagicMock()
            second_pool = MagicMock()
            second_context = _KeepaliveContext(
                email="user@outlook.com",
                password="private-password",
                config={"keepalive": {}, "oauth2": {}},
                auth_mode="password",
                recovery_record={},
                recovery_email="",
                identity_profile={
                    "country_code": "US",
                    "browser_locale": "en-US",
                    "timezone": "UTC",
                },
                controller=second_controller,
                proxy_pool=second_pool,
                proxy_lease=MagicMock(),
                flow_id="flow-2",
                worker_id="worker-2",
                page=MagicMock(),
                traffic_started=True,
            )
            runner._prepare_keepalive_context = lambda email: second_context
            runner._login_keepalive = lambda context: (MagicMock(), "")
            runner._complete_keepalive = (
                lambda context, login_state, resume_destination: "保活登录完成"
            )
            second_submit = runner.submit("user@outlook.com", KEEPALIVE)
            self.assertEqual(second_submit["status"], "queued")
            gate.set()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = (
                    runner.snapshot()
                    .get("user@outlook.com", {})
                    .get(KEEPALIVE)
                    or {}
                )
                if state.get("status") == "succeeded":
                    break
                time.sleep(0.01)
            runner.shutdown()

        self.assertEqual(state.get("status"), "succeeded")
        # 旧线程中途被取代：旧浏览器由旧线程自己清理，不泄漏。
        first_controller.clean_up.assert_called()
        first_controller.close_thread_browser.assert_called()
        first_pool.release.assert_called_once()
        self.assertEqual(runner._preserved_keepalive, {})

if __name__ == "__main__":
    unittest.main()
