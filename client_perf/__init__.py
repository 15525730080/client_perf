"""client-perf package metadata."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# native-start 将隔离构建根写入环境变量；把其中的包目录放到最前面，
# 让扩展模块优先于源码 .py，同时避免 .so 散落在源码同级。
# 同时把构建根加入 sys.path：__mypyc 共享运行时库以顶层模块导入，
# 且 uvicorn --reload 拉起的子进程只会走这里的 env-var 路径。
_native_root = os.environ.get("CLIENT_PERF_NATIVE_ROOT")
if _native_root:
    _native_root_path = Path(_native_root)
    _native_package = _native_root_path / "client_perf"
    if _native_package.is_dir():
        if str(_native_root_path) not in sys.path:
            sys.path.insert(0, str(_native_root_path))
        __path__.insert(0, str(_native_package))

__version__ = "5.0.2"
