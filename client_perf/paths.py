"""应用资源与可写运行数据路径。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "test_result"
PROJECT_ROOT = PACKAGE_DIR.parent


def _default_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "client_perf"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "client_perf"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "client_perf"


def get_data_dir() -> Path:
    """返回可写运行数据目录；可用 CLIENT_PERF_DATA_DIR 覆盖。"""
    configured = os.environ.get("CLIENT_PERF_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else _default_user_data_dir()


def get_db_path() -> Path:
    """解析数据库路径，并兼容源码目录或当前目录中的旧数据库。"""
    configured = os.environ.get("CLIENT_PERF_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    for legacy in (PROJECT_ROOT / "task.sqlite", Path.cwd() / "task.sqlite"):
        if legacy.is_file():
            return legacy.resolve()
    return get_data_dir() / "task.sqlite"


def ensure_runtime_dirs() -> Path:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
