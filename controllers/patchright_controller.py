"""兼容垫片：转发到 src/outlookregister/browser/patchright_controller。"""
import sys as _sys
from pathlib import Path as _Path

_src_parent = str(_Path(__file__).resolve().parent.parent)
if _src_parent not in _sys.path:
    _sys.path.insert(0, _src_parent)

import src.outlookregister.browser.patchright_controller as _target
_sys.modules[__name__] = _target
