import os
import time
import json
from get_token import get_access_token
from concurrent.futures import ThreadPoolExecutor
from utils import random_email, generate_strong_password
from proxy_rotation import ProxyRotationError, RotatingProxyPool
from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController



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
    traffic = getattr(controller, 'traffic', None)
    traffic_started = False

    try:
        if proxy_pool is not None:
            # 每个窗口使用同一渠道下的独立服务端会话。
            proxy_lease = proxy_pool.acquire_proxy()
            controller.set_proxy(proxy_lease.proxy)

        page = controller.get_thread_page()

        email = random_email()
        password = generate_strong_password()
        outlook_email = f'{email}{controller.email_suffix}'
        if traffic is not None:
            traffic.start_task(outlook_email)
            traffic_started = True
            traffic.attach_page(page, 'residential_registration', 'residential_browser')

        # 调用 controller 特定的注册方法 
        result = controller.outlook_register(page, email, password)

        if result and proxy_lease is not None:
            proxy_pool.switch_after_registration(proxy_lease)
            print(
                f"[ProxyRotate] 注册完成，窗口会话 {proxy_lease.session_id} "
                f"已切换到 {proxy_pool.post_registration_route.upper()}"
            )
            if traffic is not None:
                traffic.set_page_stage(page, 'post_registration', 'post_registration_browser')

        if result and not controller.enable_oauth2:
            return True
        elif not result:
            return False

        oauth_page = controller.get_oauth_page(page)
        if not oauth_page:
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'oauth_launch_failed',
                '无法启动 OAuth2 浏览器',
            )
            print('[Error: OAuth2] - 无法启动使用默认代理的 OAuth2 浏览器。')
            return False
        if traffic is not None:
            traffic.attach_page(oauth_page, 'oauth_browser', 'oauth_browser')
        token_result = get_access_token(
            oauth_page,
            email,
            password=password,
            proxy=controller.proxy,
            traffic_recorder=traffic,
        )
        if token_result[0]:
            refresh_token, access_token, expire_at =  token_result
            with controller.results_lock:
                with open(os.path.join(os.path.dirname(__file__), 'Results', 'outlook_token.txt'), 'a', encoding='utf-8') as f2:
                    f2.write(f"{email}{controller.email_suffix}---{password}---{refresh_token}---{access_token}---{expire_at}\n")
            controller._write_account_checkpoint(
                f"{email}{controller.email_suffix}",
                password,
                'oauth_success',
                'OAuth2 token 已保存到 outlook_token.txt',
            )
            print(f'[Success: TokenAuth] - {email}{controller.email_suffix}')
            try:
                imported = controller.hx_email.import_outlook_account(
                    email=f"{email}{controller.email_suffix}",
                    password=password,
                    recovery_email=controller.get_recovery_email(),
                    client_id=controller.oauth_client_id,
                    refresh_token=refresh_token,
                    proxy_url=controller.proxy,
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
            controller.clean_up(oauth_page, "done_browser")
        controller.clean_up(page, "done_browser")
        if proxy_pool is not None:
            controller.close_thread_browser()
            controller.set_proxy(None)
            proxy_pool.release(proxy_lease)
        if traffic_started:
            traffic.finish_task()

def run_concurrent_flows(controller, concurrent_flows=10, max_tasks=100, proxy_pool=None):
    task_counter = 0
    succeeded_tasks = 0
    failed_tasks = 0

    with ThreadPoolExecutor(max_workers=concurrent_flows) as executor:
        running_futures = set()

        while task_counter < max_tasks or len(running_futures) > 0:
            done_futures = {f for f in running_futures if f.done()}
            for future in done_futures:
                try:
                    if future.result():
                        succeeded_tasks += 1
                    else:
                        failed_tasks += 1
                except Exception as e:
                    failed_tasks += 1
                    print(e)
                running_futures.remove(future)

            while len(running_futures) < concurrent_flows and task_counter < max_tasks:
                new_future = executor.submit(process_single_flow, controller, proxy_pool)
                running_futures.add(new_future)
                task_counter += 1
                if max_tasks > 1 and task_counter % (max_tasks // 2) == 0:
                    print(f"已提交 {task_counter}/{max_tasks} 任务.")
                elif max_tasks == 1:
                    print(f"已提交 {task_counter}/{max_tasks} 任务.")

            time.sleep(0.5)

    print(f"\n[Result] - 共: {max_tasks}, 成功 {succeeded_tasks}, 失败 {failed_tasks}")


if __name__ == "__main__":

    with open('config.json', 'r', encoding='utf-8') as f:
        data = json.load(f) 
    os.makedirs("Results", exist_ok=True)

    max_tasks = data["max_tasks"]
    concurrent_flows = data["concurrent_flows"]

    proxy_pool = None
    proxy_rotation_cfg = data.get("proxy_rotation") or {}
    if proxy_rotation_cfg.get("enabled"):
        try:
            proxy_pool = RotatingProxyPool(proxy_rotation_cfg)
            print(f"[ProxyRotate] 已启用 HX-ProxyGroup 住宅代理轮换, 共 {len(proxy_pool.entries)} 个渠道 token")
        except ProxyRotationError as e:
            print(f"[ProxyRotate] 配置错误: {e}")
            exit(1)

    if data["choose_browser"] =="patchright":
        selected_controller = PatchrightController()
    elif data["choose_browser"] =="playwright":
        selected_controller = PlaywrightController()
    else:
        print("不支持的浏览器类型，填写patchright或者playwright")
  

    try:
        run_concurrent_flows(selected_controller, concurrent_flows, max_tasks, proxy_pool)
    finally:
        selected_controller.clean_up(type="all_browser")
