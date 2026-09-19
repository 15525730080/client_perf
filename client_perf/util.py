# coding: utf-8
"""
DataCollect — 读取任务目录下所有 CSV，返回结构化 JSON 数据。
"""
import asyncio
import csv
from pathlib import Path
from typing import Any

from client_perf.log import log as logger


class DataCollect:

    def __init__(self, save_dir: str) -> None:
        self.save_dir = save_dir
        self.csv_files: list[Path] = self._get_csv_files()

    def _get_csv_files(self) -> list[Path]:
        p = Path(self.save_dir)
        if not p.exists():
            return []
        return [f for f in p.iterdir() if f.is_file() and f.suffix == ".csv"]

    # ── CSV → list[dict] ─────────────────────────────────────

    @staticmethod
    async def _csv_to_records(file_path: Path) -> list[dict[str, Any]]:
        """异步读取 CSV，返回 list[dict]，数值字段自动转 float"""
        def _read() -> list[dict[str, Any]]:
            records: list[dict[str, Any]] = []
            with open(file_path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    parsed: dict[str, Any] = {}
                    for k, v in row.items():
                        if v == "" or v is None:
                            parsed[k] = None
                        else:
                            try:
                                parsed[k] = float(v) if "." in v else int(v)
                            except (ValueError, TypeError):
                                parsed[k] = v
                    records.append(parsed)
            return records

        return await asyncio.to_thread(_read)

    # ── 数据整形 ──────────────────────────────────────────────

    @staticmethod
    def _format(all_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        对齐时间轴：找出全局 [start_time, end_time]，
        每个指标缺失的秒数补 {"time": t}，并计算 max_value / avg_value。
        """
        # 过滤掉空数据
        valid = [d for d in all_data if d.get("value")]
        if not valid:
            return all_data

        start_time = min(d["value"][0]["time"] for d in valid if d["value"])
        end_time   = max(d["value"][-1]["time"] for d in valid if d["value"])

        for data in all_data:
            values = data.get("value") or []
            if not values:
                data["max_value"] = {}
                data["avg_value"] = {}
                continue

            # 补全缺失时间点
            existing = {v["time"] for v in values}
            for t in range(int(start_time), int(end_time) + 1):
                if t not in existing:
                    values.append({"time": t})
            values.sort(key=lambda x: x["time"])
            data["value"] = values

            # 统计（只统计有完整字段的行）
            full_rows = [v for v in values if len(v) > 1]
            if not full_rows:
                data["max_value"] = {}
                data["avg_value"] = {}
                continue

            numeric_keys: set[str] = set()
            for row in full_rows:
                for key, value in row.items():
                    if key == "time":
                        continue
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        numeric_keys.add(key)

            max_val: dict[str, float] = {}
            sum_val: dict[str, float] = {key: 0.0 for key in numeric_keys}
            cnt_val: dict[str, int] = {key: 0 for key in numeric_keys}

            for row in full_rows:
                for key in numeric_keys:
                    value = row.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        max_val[key] = max(max_val.get(key, value), value)
                        sum_val[key] += value
                        cnt_val[key] += 1

            data["max_value"] = {
                key: round(max_val[key], 4)
                for key in numeric_keys
                if key in max_val
            }
            data["avg_value"] = {
                key: round(sum_val[key] / cnt_val[key], 4)
                for key in numeric_keys
                if cnt_val[key] > 0
            }

        return all_data

    # ── 公开接口 ──────────────────────────────────────────────

    async def get_all_data(self, is_format: bool = True) -> list[dict[str, Any]]:
        """读取所有 CSV，返回 [{"name": stem, "value": [...], "max_value": {}, "avg_value": {}}]"""
        tasks = [self._csv_to_records(f) for f in self.csv_files]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_data = []
        for i, f in enumerate(self.csv_files):
            r = results[i]
            if isinstance(r, Exception):
                logger.error(f"读取 {f} 失败: {r}")
                r = []
            all_data.append({"name": f.stem, "value": r})

        if is_format:
            return self._format(all_data)
        return all_data
