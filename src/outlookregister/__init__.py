"""OutlookRegister application package.

All runtime modules live under ``src/outlookregister``. The top-level project
root (the directory containing ``config.json``, ``dashboard/``, ``Results/`` …)
is always three levels above this ``__init__`` file::

    OutlookRegister/
    ├── config.json
    └── src/outlookregister/__init__.py   <- this file

so that moved sub-modules can resolve project-relative paths deterministically
without hard-coding their own depth.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
"""Absolute path to the OutlookRegister project root directory."""
