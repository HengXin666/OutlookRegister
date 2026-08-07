import threading
import uuid

from outlookregister import PROJECT_ROOT
from outlookregister.config.identity_profiles import select_identity_profile
from outlookregister.config.utils import generate_strong_password, random_email
from outlookregister.oauth.get_token import get_access_token
from outlookregister.proxy.proxy_rotation import ProxyRotationError

# --- 不确定有无帮助 ---
# 0. 视窗大小
# 1. CDP 检测：wait_for_timeout --> time.sleep()
# 2. 使用 launch_persistent_context 
# 3. 避免短时间访问
# 4. 模拟真人轨迹
# 时区

def process_single_flow(controller, proxy_pool=None):
    page = None
    oauth_page = None
    proxy_lease = None
    flow_id = uuid.uuid4().hex
    worker_id = str(threading.get_ident())
    route_after_flow = False
    traffic = getattr(controller, 'traffic', None)
    traffic_started = False

    try:
        if getattr(controller, 'strict_isolation', False) and proxy_pool is None and not getattr(controller, 'debug', False):
            raise ProxyRotationError(
                'strict_isolation=true 时必须为每个 flow 提供代理租约'
            )
        configured_identity = getattr(controller, "identity_config", None)
        if not isinstance(configured_identity, dict):
            configured_identity = {
                "country_code": getattr(controller, "country_code", ""),
                "browser_locale": getattr(controller, "browser_locale", ""),
                "timezone": getattr(controller, "browser_timezone", ""),
            }
        identity_profile = select_identity_profile(configured_identity)
        requested_country = identity_profile["country_code"]

        if proxy_pool is not None:
            # Automatic HX mode obtains the identity from the verified exit IP;
            # the configured country pool is deliberately ignored in that mode.
            if getattr(proxy_pool, "auto_identity", False):
                proxy_lease = proxy_pool.acquire_proxy()
                identity_profile = proxy_pool.identity_profile_for_lease(proxy_lease)
                requested_country = identity_profile["country_code"]
            else:
                # 每个窗口使用同一渠道下的独立服务端会话。
                if requested_country:
                    try:
                        proxy_lease = proxy_pool.acquire_proxy(requested_country)
                    except TypeError:
                        # Keep small third-party/test pool adapters source-compatible.
                        proxy_lease = proxy_pool.acquire_proxy()
                else:
                    proxy_lease = proxy_pool.acquire_proxy()
            if getattr(controller, 'strict_isolation', False) and (
                not getattr(proxy_lease, 'session_scoped', False)
                or not str(getattr(proxy_lease, 'exit_ip', '')).strip()
            ):
                raise ProxyRotationError(
                    'strict_isolation=true 要求 session-scoped 且已确认出口 IP 的租约'
                )
            controller.set_proxy(proxy_lease.proxy)

        email = random_email()
        password = generate_strong_password()
        outlook_email = f'{email}{controller.email_suffix}'
        task_proxy = (
            proxy_lease.proxy if proxy_lease is not None else controller.get_proxy()
        )
        controller.set_flow_context(
            flow_id,
            proxy_session_id=getattr(proxy_lease, 'session_id', ''),
            proxy_exit_ip=getattr(proxy_lease, 'exit_ip', ''),
            proxy_country_code=(
                getattr(proxy_lease, 'country_code', '') or requested_country
            ),
            worker_id=worker_id,
            browser_locale=identity_profile["browser_locale"],
            browser_timezone=identity_profile["timezone"],
            flow_country_code=requested_country,
        )
        # Establish flow-local state before creating the page so all browser
        # setup and diagnostics are associated with this flow.
        page = controller.get_thread_page()
        if traffic is not None:
            traffic.start_task(
                outlook_email,
                flow_id=flow_id,
                proxy_session_id=getattr(proxy_lease, 'session_id', ''),
                proxy_exit_ip=getattr(proxy_lease, 'exit_ip', ''),
                proxy_country_code=(
                    getattr(proxy_lease, 'country_code', '') or requested_country
                ),
                identity_country_code=requested_country,
                browser_locale=identity_profile["browser_locale"],
                browser_timezone=identity_profile["timezone"],
                worker_id=worker_id,
            )
            traffic_started = True
            traffic.attach_page(page, 'residential_registration', 'residential_browser')

        if proxy_pool is not None:
            try:
                proxy_pool.verify_browser_page(page, proxy_lease)
            except Exception as exc:
                controller._write_account_checkpoint(
                    outlook_email,
                    password,
                    'browser_proxy_verification_failed',
                    str(exc),
                )
                raise

        # 调用 controller 特定的注册方法 
        result = controller.outlook_register(page, email, password)
        route_after_flow = bool(result)

        if result and traffic is not None:
            traffic.set_page_stage(page, 'post_registration', 'post_registration_browser')

        if result and not controller.enable_oauth2:
            return True
        elif not result:
            return False

        oauth_page = controller.get_oauth_page(page, proxy=task_proxy)
        if not oauth_page:
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'oauth_launch_failed',
                '无法启动 OAuth2 浏览器',
            )
            print('[Error: OAuth2] - 无法启动使用当前 flow 代理的 OAuth2 浏览器。')
            return False
        if traffic is not None:
            traffic.attach_page(oauth_page, 'oauth_browser', 'oauth_browser')
        if proxy_pool is not None:
            try:
                proxy_pool.verify_browser_page(oauth_page, proxy_lease)
            except Exception as exc:
                controller._write_account_checkpoint(
                    f"{email}{controller.email_suffix}",
                    password,
                    'oauth_proxy_verification_failed',
                    str(exc),
                )
                raise
        recovery_email = controller.get_recovery_email()
        recovery_mailbox = (
            controller.get_recovery_mailbox()
            if hasattr(controller, 'get_recovery_mailbox')
            else None
        )
        flow_hx_email = (
            controller.get_flow_hx_email()
            if hasattr(controller, 'get_flow_hx_email')
            else controller.hx_email
        )

        def recovery_challenge_handler(challenge_page):
            return controller.confirm_recovery_email_challenge(
                challenge_page,
                flow_hx_email,
                recovery_mailbox,
                recovery_email,
            )

        token_result = get_access_token(
            oauth_page,
            email,
            password=password,
            proxy=task_proxy,
            traffic_recorder=traffic,
            recovery_challenge_handler=(
                recovery_challenge_handler
                if recovery_email and recovery_mailbox
                else None
            ),
        )
        if token_result[0]:
            refresh_token, access_token, expire_at =  token_result
            with controller.results_lock:
                with open(str(PROJECT_ROOT / 'Results' / 'outlook_token.txt'), 'a', encoding='utf-8') as f2:
                    f2.write(f"{email}{controller.email_suffix}---{password}---{refresh_token}---{access_token}---{expire_at}\n")
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'oauth_success',
                'OAuth2 token 已保存到 outlook_token.txt',
            )
            print(f'[Success: TokenAuth] - {email}{controller.email_suffix}')
            try:
                imported = flow_hx_email.import_outlook_account(
                    email=f"{email}{controller.email_suffix}",
                    password=password,
                    recovery_email=controller.get_recovery_email(),
                    client_id=controller.oauth_client_id,
                    refresh_token=refresh_token,
                    proxy_url=(
                        task_proxy
                        or getattr(controller, 'hx_email_proxy_url', '')
                    ),
                )
            except Exception as exc:
                controller._write_account_checkpoint(
                    f"{email}{controller.email_suffix}",
                    password,
                    'hx_email_import_failed',
                    str(exc),
                )
                raise
            print(
                '[Success: HX-Email Import] - '
                f'account_id={imported["account_id"]}, group_id={imported["group_id"]}'
            )
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'hx_email_imported',
                f'account_id={imported["account_id"]}, group_id={imported["group_id"]}',
            )
            return True
        else:
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'oauth_failed',
                'OAuth2 token 获取失败，基础账号凭证已保留',
            )
            return False

    except Exception as e:
        print(e)
        return False
    
    finally:
        if oauth_page is not None:
            try:
                controller.clean_up(oauth_page, "done_browser")
            except Exception as exc:
                print(f"[Cleanup] OAuth 浏览器清理失败: {exc}")
        try:
            controller.clean_up(page, "done_browser")
        except Exception as exc:
            print(f"[Cleanup] 注册浏览器清理失败: {exc}")
        try:
            controller.close_thread_browser()
        except Exception as exc:
            print(f"[Cleanup] 注册浏览器进程清理失败: {exc}")
        if proxy_pool is not None:
            if route_after_flow and proxy_lease is not None:
                try:
                    proxy_lease = proxy_pool.switch_after_registration(proxy_lease)
                    print(
                        f"[ProxyRotate] 流程完成，窗口会话 {proxy_lease.session_id} "
                        f"已切换到 {proxy_pool.post_registration_route.upper()}"
                    )
                except Exception as exc:
                    print(f"[ProxyRotate] 完成后路由切换失败: {exc}")
            try:
                controller.set_proxy(None)
            except Exception as exc:
                print(f"[Cleanup] 清除线程代理失败: {exc}")
            try:
                proxy_pool.release(proxy_lease)
            except Exception as exc:
                print(f"[ProxyRotate] 释放会话失败: {exc}")
        if traffic_started:
            try:
                traffic.finish_task()
            except Exception as exc:
                print(f"[Traffic] 任务流量记录失败: {exc}")
        try:
            controller.clear_flow_context()
        except Exception as exc:
            print(f"[Cleanup] 清除 flow 状态失败: {exc}")
