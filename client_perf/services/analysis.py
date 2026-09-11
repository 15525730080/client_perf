# coding: utf-8
"""
analysis.py — 统一业务分析服务层。

集中原 comparison.py 的对比逻辑与列匹配能力，并提供：
  * 精确且兼容单位后缀的指标列匹配（cpu 不会误取 cpu_usage_all）
  * 统一的 extract_detailed_data：按每个指标自身时间点生成时间序列，
    同时为兼容标签 Excel 额外生成 timestamps 与对齐数组（_aligned）
  * compare_labels(label_ids) / advanced_compare(task_ids, base_task_id)
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from client_perf.db import LabelCollection, TaskCollection
from client_perf.util import DataCollect

# ── 指标映射：metric_key -> (csv_stem, csv_column_prefix) ──────
# csv_column_prefix 是 CSV 表头中数值列的前缀（不含单位括号部分）。
_METRIC_MAP: dict[str, tuple[str, str]] = {
    "cpu":        ("cpu",          "cpu_usage"),
    "memory":     ("memory",       "process_memory_usage"),
    "fps":        ("fps",          "fps"),
    "gpu":        ("gpu",          "gpu"),
    "threads":    ("process_info", "num_threads"),
    "handles":    ("process_info", "num_handles"),
    "disk_read":  ("disk_io",      "disk_read_rate"),
    "disk_write": ("disk_io",      "disk_write_rate"),
    "net_sent":   ("network_io",   "net_sent_rate"),
    "net_recv":   ("network_io",   "net_recv_rate"),
}

# advanced_compare 使用的指标映射（与基础映射一致，均包含 handles）。
_ADVANCED_METRIC_MAP: dict[str, tuple[str, str]] = dict(_METRIC_MAP)

# 越小越好的指标：用于高级对比的瓶颈方向判断。
# 明确要求 GPU 视为越小越好；handles 同样是越少越好。
_BETTER_SMALLER: set[str] = {
    "cpu", "memory", "gpu", "handles",
    "disk_read", "disk_write", "net_sent", "net_recv",
}


# ── 列匹配 ──────────────────────────────────────────────────────

def _is_metric_column(key: str, col_prefix: str) -> bool:
    """精确匹配指标列，兼容单位后缀（如 (%) / (MB)），但排除 cpu_usage_all 之类。

    col_prefix 之后必须是单位括号 '('、空白或字符串结尾；不能是字母 / 数字 / 下划线。
    这样 `cpu_usage` 能命中 `cpu_usage(%)`，但不会命中 `cpu_usage_all(%)`。
    """
    if not key.startswith(col_prefix):
        return False
    rest = key[len(col_prefix):]
    if rest == "":
        return True
    ch = rest[0]
    return (not ch.isalnum()) and (ch != "_")


def _extract_cell(row: dict, col_prefix: str) -> float | None:
    """从一行中提取匹配 col_prefix 的（第一个）数值列。"""
    for k, v in row.items():
        if k == "time" or v is None:
            continue
        if not isinstance(v, (int, float)):
            continue
        if _is_metric_column(k, col_prefix):
            return float(v)
    return None


def _extract_values(data: list[dict], stem: str, col_prefix: str) -> list[float]:
    """从 get_all_data() 结果中提取指定指标的数值列表（按出现顺序）。"""
    for item in data:
        if item.get("name") != stem:
            continue
        vals: list[float] = []
        for row in item.get("value", []):
            v = _extract_cell(row, col_prefix)
            if v is not None:
                vals.append(v)
        return vals
    return []


def _extract_points(data: list[dict], stem: str, col_prefix: str) -> list[tuple[float, Any]]:
    """从 get_all_data() 结果中提取指定指标的 (数值, 时间戳) 列表（按出现顺序）。

    用于高级对比的异常值检测，以便为每个异常值补回真实时间戳，
    避免前端以 undefined 时间戳构造 Invalid Date。
    """
    for item in data:
        if item.get("name") != stem:
            continue
        pts: list[tuple[float, Any]] = []
        for row in item.get("value", []):
            v = _extract_cell(row, col_prefix)
            if v is not None:
                pts.append((v, row.get("time")))
        return pts
    return []


def _calc_avg(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 4) if vals else None


def _calc_max(vals: list[float]) -> float | None:
    return round(max(vals), 4) if vals else None


def _calc_min(vals: list[float]) -> float | None:
    return round(min(vals), 4) if vals else None


def _build_task_avg(data: list[dict]) -> dict[str, float | None]:
    """返回 {metric_avg_key: value} 形如 {"cpu_avg": 12.3, ...}。"""
    result: dict[str, float | None] = {}
    for metric, (stem, col) in _METRIC_MAP.items():
        vals = _extract_values(data, stem, col)
        result[f"{metric}_avg"] = _calc_avg(vals)
        result[f"{metric}_max"] = _calc_max(vals)
        result[f"{metric}_min"] = _calc_min(vals)
    return result


# ── 统一明细提取 ────────────────────────────────────────────────

def extract_detailed_data(
    data: list[dict],
    metric_map: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """提取每个指标的详细时间序列。

    返回结构：
      {
        "timestamps": [...],                       # 对齐参考时间轴（所有指标时间点的并集）
        "_aligned": { metric: [v, ...] },          # 与 timestamps 对齐的标量数组（兼容标签 Excel）
        "cpu":    [{"time": t, "value": v}, ...],   # 各指标按自身时间点生成
        "memory": [...],
        ...
      }
    """
    metric_map = metric_map or _METRIC_MAP

    # 1) 各指标按自身时间点生成时间序列
    per_metric: dict[str, list[dict]] = {}
    for metric, (stem, col_prefix) in metric_map.items():
        series: list[dict] = []
        for item in data:
            if item.get("name") != stem:
                continue
            for row in item.get("value", []):
                t = row.get("time")
                v = _extract_cell(row, col_prefix)
                if v is not None:
                    series.append({"time": t, "value": v})
        per_metric[metric] = series

    # 2) 对齐参考时间轴：所有指标时间点的并集，升序
    all_ts: set = set()
    for series in per_metric.values():
        for p in series:
            if p["time"] is not None:
                all_ts.add(p["time"])
    timestamps = sorted(all_ts)

    # 3) 与 timestamps 对齐的标量数组（缺失补 None），用于标签 Excel
    aligned: dict[str, list[float | None]] = {}
    for metric, series in per_metric.items():
        tv = {p["time"]: p["value"] for p in series if p["time"] is not None}
        aligned[metric] = [tv.get(t) for t in timestamps]

    result: dict[str, Any] = {"timestamps": timestamps, "_aligned": aligned}
    result.update(per_metric)
    return result


# ── 多任务对比 ──────────────────────────────────────────────────

class TaskComparison:

    @classmethod
    async def create_comparison(
        cls,
        task_ids: list[int],
        base_task_id: int | None = None,
    ) -> dict[str, Any]:
        """对比多个任务，返回结构化对比结果（兼容原 comparison.py 输出）。"""
        if not task_ids:
            raise ValueError("task_ids 不能为空")

        base_id = base_task_id if base_task_id in task_ids else task_ids[0]

        async def _load(tid: int):
            info = await TaskCollection.get_item_task(tid)
            # is_format=False 获取原始数据，不补全时间点
            data = await DataCollect(info["file_dir"]).get_all_data(is_format=False)
            return info, data

        results = await asyncio.gather(*[_load(tid) for tid in task_ids])
        task_infos = [info for info, _ in results]
        task_data_list = [data for _, data in results]

        avgs = [_build_task_avg(d) for d in task_data_list]

        base_idx = next(i for i, info in enumerate(task_infos) if info["id"] == base_id)
        base_avg = avgs[base_idx]
        base_info = task_infos[base_idx]

        tasks_result: list[dict[str, Any]] = []
        for i, info in enumerate(task_infos):
            avg = avgs[i]
            diff: dict[str, float | None] = {}
            pct: dict[str, float | None] = {}
            for k, v in avg.items():
                bv = base_avg.get(k)
                if v is not None and bv is not None:
                    delta = round(v - bv, 4)
                    diff[k] = delta
                    pct[k] = round(delta / bv * 100, 2) if bv != 0 else None
                else:
                    diff[k] = None
                    pct[k] = None

            detail = extract_detailed_data(task_data_list[i])
            raw_data = {metric_name: detail[metric_name] for metric_name in _METRIC_MAP}

            tasks_result.append({
                "id":          info["id"],
                "name":        info.get("name", ""),
                "version":     info.get("version", ""),
                "platform":    info.get("platform", ""),
                "device_type": info.get("device_type", "pc"),
                "start_time":  info.get("start_time", ""),
                "avg":  avg,
                "diff": diff,
                "pct":  pct,
                "data": raw_data,
            })

        return {
            "base_task": {
                "id":      base_info["id"],
                "name":    base_info.get("name", ""),
                "version": base_info.get("version", ""),
            },
            "tasks": tasks_result,
        }


# ── 标签对比 ────────────────────────────────────────────────────

async def compare_labels(label_ids: list[int]) -> dict[str, Any]:
    """基于区间标签对比，返回结构与 create_comparison 兼容。"""
    if len(label_ids) < 2:
        raise ValueError("至少需要两个标签")

    labels = [await LabelCollection.get_label(lid) for lid in label_ids]

    async def _load_sliced(label: dict):
        info = await TaskCollection.get_item_task(label["task_id"])
        raw = await DataCollect(info["file_dir"]).get_all_data(is_format=False)
        sliced = [
            {
                "name": item["name"],
                "value": [
                    v for v in item.get("value", [])
                    if label["start_ts"] <= v.get("time", 0) <= label["end_ts"]
                ],
            }
            for item in raw
        ]
        return info, sliced, label

    results = await asyncio.gather(*[_load_sliced(lb) for lb in labels])

    tasks_result: list[dict[str, Any]] = []
    for info, sliced_data, label in results:
        tasks_result.append({
            "id":          info["id"],
            "name":        info.get("name", ""),
            "version":     info.get("version", ""),
            "label_id":    label["id"],
            "label_name":  label["name"],
            "label_color": label["color"],
            "start_ts":    label["start_ts"],
            "end_ts":      label["end_ts"],
            "avg":         _build_task_avg(sliced_data),
            "data":        extract_detailed_data(sliced_data),
        })

    base = tasks_result[0]
    for t in tasks_result:
        diff, pct = {}, {}
        for k, v in t["avg"].items():
            bv = base["avg"].get(k)
            if v is not None and bv is not None:
                d = round(v - bv, 4)
                diff[k] = d
                pct[k] = round(d / bv * 100, 2) if bv != 0 else None
            else:
                diff[k] = pct[k] = None
        t["diff"] = diff
        t["pct"] = pct

    return {
        "base_task": {
            "id":         base["id"],
            "name":       base["name"],
            "label_name": base["label_name"],
        },
        "tasks": tasks_result,
    }


# ── 高级对比（统计显著性 + 异常值 + 瓶颈）──────────────────────

def _stats(arr: list[float]) -> tuple[float, float, int]:
    n = len(arr)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(arr) / n
    var = sum((x - mean) ** 2 for x in arr) / max(n - 1, 1)
    return mean, var, n


async def advanced_compare(
    task_ids: list[int],
    base_task_id: int | None = None,
) -> dict[str, Any]:
    """统计显著性 + 异常值检测 + 瓶颈分析。包含 handles，GPU 视为越小越好。"""
    if len(task_ids) < 2:
        raise ValueError("高级分析需要至少两个任务")

    base_id = base_task_id if base_task_id in task_ids else task_ids[0]
    cmp_id = next(task_id for task_id in task_ids if task_id != base_id)

    base_info = await TaskCollection.get_item_task(base_id)
    cmp_info = await TaskCollection.get_item_task(cmp_id)
    base_data = await DataCollect(base_info["file_dir"]).get_all_data()
    cmp_data = await DataCollect(cmp_info["file_dir"]).get_all_data()

    significance: dict[str, Any] = {}
    outliers_result: dict[str, Any] = {}
    bottlenecks: list[dict] = []

    for metric, (stem, col) in _ADVANCED_METRIC_MAP.items():
        bp = _extract_points(base_data, stem, col)
        cp = _extract_points(cmp_data, stem, col)
        bv = [v for v, _ in bp]
        cv = [v for v, _ in cp]
        if not bv or not cv:
            continue

        bm, bvar, bn = _stats(bv)
        cm, cvar, cn = _stats(cv)
        diff = cm - bm
        pct = (diff / bm * 100) if bm != 0 else None

        se = ((bvar / bn) + (cvar / cn)) ** 0.5 if (bvar / bn + cvar / cn) > 0 else 1e-9
        t = abs(diff / se) if se > 0 else 0
        p = 2 * (1 - min(0.9999, 0.5 * (1 + math.erf(t / math.sqrt(2)))))
        significant = p < 0.05

        significance[metric] = {
            "base_mean": round(bm, 4),
            "compare_mean": round(cm, 4),
            "diff": round(diff, 4),
            "percent_change": round(pct, 2) if pct is not None else None,
            "p_value": round(p, 4),
            "confidence": round((1 - p) * 100, 2),
            "is_significant": significant,
        }

        # 异常值（Z-score > 2.5）
        combined = bv + cv
        mc = sum(combined) / len(combined)
        sc = (sum((x - mc) ** 2 for x in combined) / max(len(combined) - 1, 1)) ** 0.5
        outlier_list = [
            {
                "value": round(v, 4),
                "z_score": round(abs((v - mc) / sc) if sc else 0, 2),
                "is_high": v > mc,
                "timestamp": ts,
            }
            for (v, ts) in cp if sc and abs((v - mc) / sc) > 2.5
        ]
        if outlier_list:
            outliers_result[metric] = {
                "outliers": outlier_list[:20],
                "summary": f"检测到 {len(outlier_list)} 个异常值",
            }

        # 瓶颈（bm==0 等导致 pct 不可用时跳过）
        if significant and pct is not None and abs(pct) > 20:
            is_worse = (pct > 0) if metric in _BETTER_SMALLER else (pct < 0)
            bottlenecks.append({
                "metric": metric,
                "percent_change": round(pct, 2),
                "is_worse": is_worse,
            })

    bottlenecks.sort(key=lambda x: abs(x["percent_change"]), reverse=True)

    return {
        "base_task":    {"id": base_id, "name": base_info.get("name", "")},
        "compare_task": {"id": cmp_id,  "name": cmp_info.get("name", "")},
        "advanced_analysis": {
            "statistical_significance": significance,
            "outliers": outliers_result,
            "bottleneck_analysis": {
                "potential_bottlenecks": bottlenecks,
                "summary": f"共发现 {len(bottlenecks)} 个潜在性能瓶颈",
            },
        },
    }
