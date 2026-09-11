# coding: utf-8
"""
comparison.py — 兼容 shim（轻量转发层）。

业务逻辑已集中到 client_perf.services.analysis，本文件仅重新导出历史公共接口，
保证 api.py 等既有引用（TaskComparison / _build_task_avg / _METRIC_MAP 等）无需改动。
"""
from client_perf.services.analysis import (
    TaskComparison,
    _METRIC_MAP,
    _extract_values,
    _calc_avg,
    _calc_max,
    _calc_min,
    _build_task_avg,
    extract_detailed_data,
    compare_labels,
    advanced_compare,
)

__all__ = [
    "TaskComparison",
    "_METRIC_MAP",
    "_extract_values",
    "_calc_avg",
    "_calc_max",
    "_calc_min",
    "_build_task_avg",
    "extract_detailed_data",
    "compare_labels",
    "advanced_compare",
]
