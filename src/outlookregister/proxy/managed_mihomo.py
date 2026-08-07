"""Bounded local Mihomo instances for HX residential endpoints."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from outlookregister.proxy.mihomo_config import (  # noqa: F401  复用并兼容旧导入
    SUPPORTED_PROTOCOLS,
    SUPPORTED_RESIDENTIAL_PROTOCOLS,
    ManagedMihomoError,
    _parse_residential_endpoint,
    _parse_standard_uri,
    _parse_vmess,
    _query_value,
    build_mihomo_config,
)

PREFERRED_LOCAL_PORT = 2334





class _Instance:
    def __init__(self, process, directory, log_handle):
        self.process = process
        self.directory = directory
        self.log_handle = log_handle


class ManagedMihomo:
    """Own one short-lived loopback Mihomo process per active declared node."""

    def __init__(self, binary: str | None = None, start_timeout: float = 5.0):
        self._binary = str(binary or os.environ.get("HX_MIHOMO_BIN") or "mihomo").strip()
        self._start_timeout = max(float(start_timeout), 0.5)
        self._instances: dict[int, _Instance] = {}
        self._lock = threading.RLock()
        atexit.register(self.close)

    def start(self, node_index: int, endpoint: dict) -> str:
        with self._lock:
            self._stop_locked(node_index)
            executable = self._resolve_binary()
            local_port = _available_loopback_port(PREFERRED_LOCAL_PORT)
            config = build_mihomo_config(endpoint, local_port)
            directory = tempfile.TemporaryDirectory(prefix=f"outlook-hx-{node_index}-")
            config_path = Path(directory.name) / "config.json"
            log_path = Path(directory.name) / "mihomo.log"
            try:
                config_path.write_text(json.dumps(config, ensure_ascii=True), encoding="utf-8")
                config_path.chmod(0o600)
                subprocess.run(
                    [executable, "-t", "-d", directory.name, "-f", str(config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._start_timeout,
                    check=True,
                )
                log_handle = log_path.open("wb")
                log_path.chmod(0o600)
                process = subprocess.Popen(
                    [executable, "-d", directory.name, "-f", str(config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
                instance = _Instance(process, directory, log_handle)
                self._instances[node_index] = instance
                self._wait_ready(instance, local_port)
            except (OSError, subprocess.SubprocessError) as exc:
                self._stop_locked(node_index)
                directory.cleanup()
                raise ManagedMihomoError("本机 Mihomo 启动或配置校验失败") from exc
            except ManagedMihomoError:
                self._stop_locked(node_index)
                directory.cleanup()
                raise
            return f"http://127.0.0.1:{local_port}"

    def stop(self, node_index: int) -> None:
        with self._lock:
            self._stop_locked(node_index)

    def is_active(self, node_index: int) -> bool:
        with self._lock:
            return node_index in self._instances

    def failure_detail(self, node_index: int) -> str:
        """Return a bounded Mihomo warning before the short-lived instance stops."""
        with self._lock:
            instance = self._instances.get(node_index)
            if instance is None:
                return ""
            try:
                instance.log_handle.flush()
                lines = (Path(instance.directory.name) / "mihomo.log").read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
            except OSError:
                return ""
            warnings = [
                line for line in lines
                if "level=warning" in line or "level=error" in line
            ]
            if not warnings:
                return ""
            detail = warnings[-1][-1000:]
            detail = re.sub(
                r"(?i)\b(?:vless|vmess|trojan)://\S+",
                "[redacted endpoint]",
                detail,
            )
            return re.sub(
                r"(?i)\b(https?|socks5)://[^@\s]+@",
                r"\1://[redacted]@",
                detail,
            )

    def close(self) -> None:
        with self._lock:
            for node_index in list(self._instances):
                self._stop_locked(node_index)

    def _resolve_binary(self) -> str:
        executable = shutil.which(self._binary)
        if not executable:
            raise ManagedMihomoError(
                "未找到 mihomo；请安装 Mihomo 或通过 HX_MIHOMO_BIN 指定可执行文件"
            )
        return executable

    def _wait_ready(self, instance: _Instance, port: int) -> None:
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if instance.process.poll() is not None:
                raise ManagedMihomoError("本机 Mihomo 在监听就绪前退出")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise ManagedMihomoError("等待本机 Mihomo 监听就绪超时")

    def _stop_locked(self, node_index: int) -> None:
        instance = self._instances.pop(node_index, None)
        if instance is None:
            return
        process = instance.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        instance.log_handle.close()
        instance.directory.cleanup()


def _available_loopback_port(preferred_port: int | None = None) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if preferred_port is not None:
            try:
                listener.bind(("127.0.0.1", preferred_port))
                return int(listener.getsockname()[1])
            except OSError:
                pass
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
