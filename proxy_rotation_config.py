"""兼容垫片：转发到 src/outlookregister/config.proxy_rotation_config。

将旧导入路径解析为目标模块本身，旧 ``from <name> import ...``（含私有名）继续可用。
新代码应直接从 ``src.outlookregister.config.proxy_rotation_config`` 导入。
"""
import sys as _sys
from pathlib import Path as _Path

_src_parent = str(_Path(__file__).resolve().parent)
if _src_parent not in _sys.path:
    _sys.path.insert(0, _src_parent)

import src.outlookregister.config.proxy_rotation_config as _target
_sys.modules[__name__] = _target
