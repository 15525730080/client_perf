# coding: utf-8
"""
services — 统一业务服务层。

将原本散落在 api.py / comparison.py 的「对比分析」与「Excel 导出」逻辑集中到：
  * analysis.py        —— 指标提取、对比、标签对比、高级对比
  * export_service.py  —— 四类 Excel 导出

api.py / comparison.py 仅作为薄层或兼容 shim，引用本包即可。
"""
from client_perf.services.analysis import (
    TaskComparison,
    advanced_compare,
    compare_labels,
    extract_detailed_data,
    _build_task_avg,
    _extract_values,
    _METRIC_MAP,
)
from client_perf.services.export_service import (
    create_excel_report,
    create_comparison_excel,
    export_label_comparison_excel_func,
    export_advanced_excel,
)

__all__ = [
    # analysis
    "TaskComparison",
    "advanced_compare",
    "compare_labels",
    "extract_detailed_data",
    "_build_task_avg",
    "_extract_values",
    "_METRIC_MAP",
    # export
    "create_excel_report",
    "create_comparison_excel",
    "export_label_comparison_excel_func",
    "export_advanced_excel",
]
