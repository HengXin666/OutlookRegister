import os
import time
from concurrent.futures import ThreadPoolExecutor

from outlookregister import PROJECT_ROOT
from outlookregister.browser.patchright_controller import PatchrightController
from outlookregister.browser.playwright_controller import PlaywrightController
from outlookregister.config.config_store import ConfigStore, validate_config

# --- 不确定有无帮助 ---
# 0. 视窗大小
# 1. CDP 检测：wait_for_timeout --> time.sleep()
# 2. 使用 launch_persistent_context 
# 3. 避免短时间访问
# 4. 模拟真人轨迹
# 时区
from outlookregister.core.flow_processor import (
    process_single_flow,
)
from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool


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
    return {
        "total": max_tasks,
        "succeeded": succeeded_tasks,
        "failed": failed_tasks,
    }


if __name__ == "__main__":

    data = ConfigStore(PROJECT_ROOT / 'config.json').read()
    os.makedirs("Results", exist_ok=True)

    max_tasks = data["max_tasks"]
    concurrent_flows = data["concurrent_flows"]
    strict_isolation = bool(data.get("strict_isolation", True))
    debug = bool(data.get("debug", False))

    validation_errors = validate_config(data, for_run=True)
    if validation_errors:
        print('[Config] 配置不允许启动任务: ' + '；'.join(validation_errors))
        exit(1)

    proxy_pool = None
    proxy_rotation_cfg = dict(data.get("proxy_rotation") or {})
    auto_rotation = bool(str(
        proxy_rotation_cfg.get("control_url")
        or proxy_rotation_cfg.get("rotation_url")
        or ""
    ).strip())
    if strict_isolation and not debug:
        required = (
            (auto_rotation or proxy_rotation_cfg.get("enabled"))
            and (auto_rotation or proxy_rotation_cfg.get("session_scoped"))
            and (auto_rotation or proxy_rotation_cfg.get("check_proxy"))
            and (auto_rotation or proxy_rotation_cfg.get("enforce_unique_exit_ip"))
            and (auto_rotation or proxy_rotation_cfg.get("verify_browser_exit_ip"))
            and data.get("isolate_hx_email_group", True)
            and data.get("prevent_direct_network_leaks", True)
        )
        if not required:
            print(
                "[ProxyRotate] 严格隔离要求 enabled、session_scoped、"
                "check_proxy、enforce_unique_exit_ip、verify_browser_exit_ip、"
                "isolate_hx_email_group 以及 prevent_direct_network_leaks 全部启用。"
            )
            exit(1)
    proxy_rotation_cfg["required_pool_size"] = concurrent_flows
    if (proxy_rotation_cfg.get("enabled") or auto_rotation) and not debug:
        try:
            proxy_pool = RotatingProxyPool(proxy_rotation_cfg)
            print("[ProxyRotate] 已启用 HX-ProxyGroup 住宅代理节点池")
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
