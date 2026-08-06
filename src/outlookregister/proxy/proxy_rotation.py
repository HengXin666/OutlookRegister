from dataclasses import dataclass  # noqa: F401  (部分历史代码引用 dataclass 装饰器；类型已迁移到 proxy_pool_types)
import ipaddress  # noqa: F401
import json  # noqa: F401
import threading  # noqa: F401
import uuid  # noqa: F401

from src.outlookregister.proxy.proxy_pool_types import (  # noqa: F401  兼容旧导入
    ProxyRotationError,
    ProxyLease,
    _DeclaredLeaseState,
    _declared_lease_state,
    _declared_states,
    _declared_states_lock,
)
from src.outlookregister.proxy.proxy_pool_config import _ProxyPoolConfig
from src.outlookregister.proxy.proxy_pool_core import _ProxyPoolCore
from src.outlookregister.proxy.proxy_pool_control_nodes import _ProxyPoolControlNodes
from src.outlookregister.proxy.proxy_pool_sessions import _ProxyPoolSessions
from src.outlookregister.proxy.proxy_pool_verify import _ProxyPoolVerify


class RotatingProxyPool(_ProxyPoolConfig, _ProxyPoolCore, _ProxyPoolControlNodes, _ProxyPoolSessions, _ProxyPoolVerify):
    """
    对接 HX-ProxyGroup 的住宅代理换 IP 接口。

    每次整体注册流程开始前调用 acquire_proxy()：

      1. 从渠道声明的固定节点池租用一个空闲节点；
      2. 调用该节点的 next 接口刷新住宅出口；
      3. 优先使用节点返回的浏览器兼容入口；否则把 VLESS/VMess/Trojan WS 端点落地到本机 Mihomo；
      4. 浏览器关闭后只释放进程内租约，不删除服务端节点。
    """

    pass
