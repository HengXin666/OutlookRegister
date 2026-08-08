import os
import time
from concurrent.futures import ThreadPoolExecutor

from outlookregister import PROJECT_ROOT
from outlookregister.browser.patchright_controller import PatchrightController
from outlookregister.browser.playwright_controller import PlaywrightController
from outlookregister.config.config_store import ConfigStore, validate_config
from outlookregister.config.config_validators import _stage_group_name
from outlookregister.config.proxy_rotation_config import MANUAL_SOURCE

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
from outlookregister.proxy.proxy_pool_factory import build_proxy_pool
from outlookregister.proxy.proxy_rotation import ProxyRotationError


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

    proxy_rotation_cfg = dict(data.get("proxy_rotation") or {})
    auto_rotation = bool(str(
        proxy_rotation_cfg.get("control_url")
        or proxy_rotation_cfg.get("rotation_url")
        or ""
    ).strip())
    manual_mode = str(data.get("proxy_source") or "").strip().casefold() == MANUAL_SOURCE
    if strict_isolation and not debug:
        # A deterministic per-stage group name satisfies the isolation contract
        # just as well as a per-flow group, so accept either.
        grouped = bool(
            data.get("isolate_hx_email_group", False)
            or _stage_group_name(data, "register")
        )
        if manual_mode:
            required = grouped and data.get("prevent_direct_network_leaks", True)
            requirement_text = (
                "[ProxyRotate] 严格隔离要求 prevent_direct_network_leaks，"
                "以及 register_account_group 或 isolate_hx_email_group 之一。"
            )
        else:
            required = (
                (auto_rotation or proxy_rotation_cfg.get("enabled"))
                and (auto_rotation or proxy_rotation_cfg.get("session_scoped"))
                and (auto_rotation or proxy_rotation_cfg.get("check_proxy"))
                and (auto_rotation or proxy_rotation_cfg.get("enforce_unique_exit_ip"))
                and (auto_rotation or proxy_rotation_cfg.get("verify_browser_exit_ip"))
                and grouped
                and data.get("prevent_direct_network_leaks", True)
            )
            requirement_text = (
                "[ProxyRotate] 严格隔离要求 enabled、session_scoped、"
                "check_proxy、enforce_unique_exit_ip、verify_browser_exit_ip、"
                "prevent_direct_network_leaks，以及 register_account_group 或 "
                "isolate_hx_email_group 之一。"
            )
        if not required:
            print(requirement_text)
            exit(1)

    try:
        proxy_pool = build_proxy_pool(
            data,
            required_pool_size=concurrent_flows,
            config_path=PROJECT_ROOT / 'config.json',
        )
    except ProxyRotationError as e:
        print(f"[ProxyRotate] 配置错误: {e}")
        exit(1)
    if proxy_pool is not None:
        print(
            "[ProxyRotate] 已启用手动代理列表"
            if manual_mode
            else "[ProxyRotate] 已启用 HX-ProxyGroup 住宅代理节点池"
        )

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
