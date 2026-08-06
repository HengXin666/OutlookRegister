from __future__ import annotations

import ipaddress
import json
import threading
import time
import uuid
from dataclasses import replace
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from src.outlookregister.proxy.managed_mihomo import ManagedMihomo, ManagedMihomoError, SUPPORTED_PROTOCOLS
from src.outlookregister.proxy.proxy_pool_types import ProxyLease, ProxyRotationError, _declared_lease_state
from src.outlookregister.config.proxy_rotation_config import (
    parse_control_plane_url,
    parse_remote_control_plane_url,
    parse_remote_residential_control_url,
    validate_proxy_endpoint,
    validate_remote_proxy_endpoint,
    validate_rotation_token,
)
from src.outlookregister.config.identity_profiles import (
    is_valid_country_code,
    is_valid_timezone,
    select_identity_profile,
)


class _ProxyPoolControlNodes:
    def _acquire_declared_proxy(self):
        """Lease and rotate one server-declared residential node."""
        with self._declared_state.lock:
            self._ensure_control_nodes()
            start_index = self._declared_state.next_index % len(self.entries)
            self._declared_state.next_index += 1
            candidates = [
                self.entries[(start_index + offset) % len(self.entries)]
                for offset in range(len(self.entries))
                if self.entries[(start_index + offset) % len(self.entries)]["index"]
                not in self._leased_node_indexes
            ]

            if not candidates:
                raise ProxyRotationError(
                    "HX-ProxyGroup 住宅节点池已全部占用；请降低并发或增加渠道会话数"
                )

            errors = []
            for node in candidates:
                node_index = node["index"]
                if node_index in self._leased_node_indexes:
                    continue
                self._leased_node_indexes.add(node_index)
                try:
                    return self._rotate_and_verify_declared_node(node)
                except ProxyRotationError as exc:
                    self._leased_node_indexes.discard(node_index)
                    errors.append(f"节点 {node_index}: {exc}")

            raise ProxyRotationError(
                "所有 HX-ProxyGroup 住宅节点均不可用: " + " | ".join(errors)
            )

    def _ensure_control_nodes(self):
        if self._control_nodes_loaded:
            return
        response = self._request("GET", f"{self.control_path}/nodes")
        if response.status_code != 200:
            if response.status_code == 404:
                raise ProxyRotationError(
                    "HX-ProxyGroup 住宅 control token 无效、已轮换或渠道未启用"
                )
            raise ProxyRotationError(
                f"读取住宅节点池失败: HTTP {response.status_code} "
                f"({self._response_detail(response)})"
            )
        payload = self._json(response, "读取住宅节点池")
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ProxyRotationError("HX-ProxyGroup 没有声明可用的住宅节点")

        nodes = []
        seen_indexes = set()
        for position, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ProxyRotationError(
                    f"HX-ProxyGroup 住宅节点 {position + 1} 响应格式错误"
                )
            try:
                node_index = int(raw_node.get("index"))
            except (TypeError, ValueError) as exc:
                raise ProxyRotationError(
                    f"HX-ProxyGroup 住宅节点 {position + 1} 缺少有效 index"
                ) from exc
            if node_index < 1 or node_index in seen_indexes:
                raise ProxyRotationError("HX-ProxyGroup 住宅节点 index 无效或重复")
            seen_indexes.add(node_index)
            nodes.append({
                "index": node_index,
                "node_name": str(raw_node.get("node_name") or f"node-{node_index}"),
                "proxy_url": raw_node.get("proxy_url"),
                "endpoints": raw_node.get("endpoints") if isinstance(raw_node.get("endpoints"), list) else [],
                "residential_endpoint": (
                    raw_node.get("residential_endpoint")
                    if isinstance(raw_node.get("residential_endpoint"), dict)
                    else None
                ),
                "country_code": str(raw_node.get("country_code") or "").strip(),
                "route_mode": str(raw_node.get("route_mode") or "residential"),
                "hint": str(raw_node.get("hint") or "").strip(),
            })
        if self.required_pool_size and len(nodes) < self.required_pool_size:
            raise ProxyRotationError(
                f"住宅节点池容量不足: nodes={len(nodes)}, "
                f"required={self.required_pool_size}"
            )
        self.entries = nodes
        self._control_nodes_loaded = True

    def _rotate_and_verify_declared_node(self, node):
        last_error = None
        for attempt in range(self.max_rotate_retries + 1):
            response = self._request(
                "POST",
                f"{self.control_path}/nodes/{node['index']}/next",
            )
            if response.status_code == 429:
                last_error = "服务端限流(rotate_rate_limited)"
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                if response.status_code == 404:
                    last_error = "control token 或住宅节点已失效"
                else:
                    last_error = (
                        f"HTTP {response.status_code} "
                        f"({self._response_detail(response)})"
                    )
                time.sleep(0.5 * (attempt + 1))
                continue
            payload = self._json(response, "刷新住宅节点")
            try:
                returned_index = int(payload.get("index"))
            except (TypeError, ValueError) as exc:
                raise ProxyRotationError("刷新住宅节点响应缺少有效 index") from exc
            if returned_index != node["index"]:
                raise ProxyRotationError("刷新住宅节点响应的 index 不一致")
            updated_node = {
                **node,
                "node_name": str(payload.get("node_name") or node["node_name"]),
                "proxy_url": payload.get("proxy_url"),
                "endpoints": payload.get("endpoints") if isinstance(payload.get("endpoints"), list) else node.get("endpoints", []),
                "residential_endpoint": (
                    payload.get("residential_endpoint")
                    if isinstance(payload.get("residential_endpoint"), dict)
                    else None
                ),
                "country_code": str(payload.get("country_code") or "").strip(),
                "route_mode": str(payload.get("route_mode") or "residential"),
                "hint": str(payload.get("hint") or "").strip(),
            }
            try:
                proxy = self._proxy_from_control_node(updated_node)
                identity = self._verify_exit_identity(
                    proxy,
                    expected_country=updated_node["country_code"],
                    verify_listener_credentials=bool(
                        str(updated_node.get("proxy_url") or "").strip()
                    ) and not isinstance(updated_node.get("residential_endpoint"), dict),
                )
                lease = ProxyLease(
                    proxy=proxy,
                    token="control-node",
                    session_id=f"node-{node['index']}",
                    session_scoped=True,
                    exit_ip=identity["exit_ip"],
                    country_code=identity["country_code"],
                    browser_locale=identity["browser_locale"],
                    timezone=identity["timezone"],
                    node_index=node["index"],
                    node_name=updated_node["node_name"],
                )
                self._reserve_exit_ip(lease)
            except ProxyRotationError as exc:
                diagnostic = ""
                managed_data_plane = False
                if self._local_data_plane is not None:
                    managed_data_plane = self._local_data_plane.is_active(node["index"])
                    diagnostic = self._local_data_plane.failure_detail(node["index"])
                    self._local_data_plane.stop(node["index"])
                last_error = str(exc)
                if managed_data_plane and diagnostic:
                    last_error = f"{last_error}；本机 Mihomo: {diagnostic}"
                time.sleep(0.5 * (attempt + 1))
                continue
            for position, current in enumerate(self.entries):
                if current["index"] == node["index"]:
                    self.entries[position] = updated_node
                    break
            print(
                "[ProxyRotate] 住宅节点校验通过 - "
                f"node={lease.node_index}, exit_ip={lease.exit_ip}, "
                f"country={lease.country_code}"
            )
            return lease
        raise ProxyRotationError(last_error or "住宅节点刷新失败")

    def _proxy_from_control_node(self, node):
        residential_endpoint = node.get("residential_endpoint")
        if isinstance(residential_endpoint, dict) and self._local_data_plane is not None:
            try:
                return self._local_data_plane.start(node["index"], {
                    **residential_endpoint,
                    "transport": "tcp",
                })
            except ManagedMihomoError as exc:
                raise ProxyRotationError(str(exc)) from exc
        proxy = str(node.get("proxy_url") or "").strip()
        if proxy:
            try:
                return validate_remote_proxy_endpoint(proxy)
            except ValueError as exc:
                raise ProxyRotationError(
                    f"HX-ProxyGroup 返回的住宅节点代理入口无效: {exc}"
                ) from exc
        if self._local_data_plane is not None:
            for endpoint in node.get("endpoints") or []:
                if not isinstance(endpoint, dict):
                    continue
                protocol = str(endpoint.get("protocol") or "").strip().casefold()
                transport = str(endpoint.get("transport") or "").strip().casefold()
                if protocol not in SUPPORTED_PROTOCOLS or transport != "ws":
                    continue
                try:
                    return self._local_data_plane.start(node["index"], endpoint)
                except ManagedMihomoError as exc:
                    raise ProxyRotationError(str(exc)) from exc
        raise ProxyRotationError(
            "节点没有可用的数据端点；api-list 渠道应返回住宅端点，其他渠道必须发布 WebSocket 端点"
        )
