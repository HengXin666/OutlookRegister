"""共享类型：ProxyLease / ProxyRotationError 与进程级租约状态。

抽离到独立叶子模块避免 proxy_rotation 与各 mixin 之间的循环导入。
"""

from __future__ import annotations

import hashlib
import threading
import weakref
from dataclasses import dataclass


class ProxyRotationError(Exception):
    """住宅代理服务端换 IP 失败时抛出。"""


class _DeclaredLeaseState:
    """Process-wide ownership for one high-privilege control URL."""

    def __init__(self):
        self.lock = threading.RLock()
        self.leased_node_indexes: set[int] = set()
        self.active_exit_ips: dict[str, tuple[str, str]] = {}
        self.next_index = 0


_declared_states_lock = threading.Lock()
_declared_states = weakref.WeakValueDictionary()


def _declared_lease_state(control_url: str) -> _DeclaredLeaseState:
    # Hash the bearer URL so the process registry cannot accidentally expose it
    # through diagnostics or object representations.
    key = hashlib.sha256(control_url.encode("utf-8")).hexdigest()
    with _declared_states_lock:
        state = _declared_states.get(key)
        if state is None:
            state = _DeclaredLeaseState()
            _declared_states[key] = state
        return state


@dataclass(frozen=True)
class ProxyLease:
    proxy: str
    token: str
    session_id: str = ""
    session_scoped: bool = False
    exit_ip: str = ""
    country_code: str = ""
    browser_locale: str = ""
    timezone: str = ""
    node_index: int = 0
    node_name: str = ""
