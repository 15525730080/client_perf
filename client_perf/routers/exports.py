# coding: utf-8
"""Excel 导出和历史报告管理路由。"""

import os
import time
import traceback

from fastapi import APIRouter
from starlette.responses import FileResponse

from client_perf.db import ComparisonReportCollection, TaskCollection
from client_perf.log import log as logger
from client_perf.routers.common import err, ok, parse_ids
from client_perf.services.analysis import TaskComparison, advanced_compare, compare_labels
from client_perf.services.export_service import (
    COMPARISON_REPORT_DIR,
    create_comparison_excel,
    create_excel_report,
    export_advanced_excel,
    export_label_comparison_excel_func,
)
from client_perf.util import DataCollect

router = APIRouter()
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_response(file_path: str) -> FileResponse:
    return FileResponse(
        path=file_path,
        filename=os.path.basename(file_path),
        media_type=_XLSX_MEDIA_TYPE,
    )


def _comparison_report_path(file_path: str) -> str:
    """校验报告路径必须位于生成报告目录内。"""
    root = COMPARISON_REPORT_DIR.resolve()
    candidate = os.path.realpath(file_path)
    if os.path.commonpath([str(root), candidate]) != str(root) or candidate == str(root):
        raise RuntimeError(f"拒绝删除对比报告目录之外的路径: {candidate}")
    return candidate


async def _record_report(
    name: str,
    task_ids: list[int],
    base_task_id: int,
    description: str,
    file_path: str,
) -> None:
    report = await ComparisonReportCollection.create_report(
        name=name,
        task_ids=task_ids,
        base_task_id=base_task_id,
        description=description,
    )
    await ComparisonReportCollection.update_report(report["id"], report_path=file_path)


@router.get("/export_excel/")
async def export_excel(task_id: int):
    try:
        task = await TaskCollection.get_item_task(task_id)
        data = await DataCollect(task["file_dir"]).get_all_data(is_format=False)
        file_path = create_excel_report(
            task.get("name") or f"任务{task_id}",
            data,
            task["file_dir"],
        )
        return _file_response(file_path)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@router.get("/export_comparison_excel/")
async def export_comparison_excel(
    task_ids: str,
    base_task_id: int | None = None,
    report_name: str | None = None,
):
    try:
        ids = parse_ids(task_ids)
        data = await TaskComparison.create_comparison(ids, base_task_id)
        name = report_name or f"性能对比_{time.strftime('%Y%m%d_%H%M%S')}"
        file_path = await create_comparison_excel(data, name)
        resolved_base_id = data["base_task"]["id"]
        await _record_report(name, ids, resolved_base_id, f"对比任务: {task_ids}", file_path)
        return _file_response(file_path)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@router.get("/export_label_comparison_excel/")
async def export_label_comparison_excel(label_ids: str, report_name: str | None = None):
    try:
        ids = parse_ids(label_ids)
        data = await compare_labels(ids)
        name = report_name or f"标签对比_{time.strftime('%Y%m%d_%H%M%S')}"
        file_path = await export_label_comparison_excel_func(data, name)
        task_ids = [task["id"] for task in data["tasks"]]
        base_id = data["base_task"]["id"]
        await _record_report(name, task_ids, base_id, f"对比标签: {label_ids}", file_path)
        return _file_response(file_path)
    except ValueError as exc:
        return err(str(exc), 400)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@router.get("/export_advanced_comparison_excel/")
async def export_advanced_comparison_excel(
    task_ids: str,
    base_task_id: int | None = None,
    report_name: str | None = None,
):
    """导出高级对比分析；补齐前端已调用但历史后端缺失的接口。"""
    try:
        ids = parse_ids(task_ids)
        data = await advanced_compare(ids, base_task_id)
        name = report_name or f"高级性能对比_{time.strftime('%Y%m%d_%H%M%S')}"
        file_path = await export_advanced_excel(data, name)
        resolved_base_id = data["base_task"]["id"]
        await _record_report(name, ids, resolved_base_id, f"高级对比任务: {task_ids}", file_path)
        return _file_response(file_path)
    except ValueError as exc:
        return err(str(exc), 400)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@router.get("/static/comparison_reports/{filename}")
async def download_comparison_report(filename: str):
    """兼容前端历史链接，从可写数据目录安全下载已生成报告。"""
    try:
        if os.path.basename(filename) != filename or not filename.lower().endswith(".xlsx"):
            return err("无效的报告文件名", 400)
        file_path = _comparison_report_path(str(COMPARISON_REPORT_DIR / filename))
        if not os.path.isfile(file_path):
            return err("报告文件不存在", 404)
        return _file_response(file_path)
    except Exception as exc:
        return err(str(exc), 400)


@router.get("/get_comparison_reports/")
async def get_comparison_reports():
    try:
        return ok(await ComparisonReportCollection.get_all_reports())
    except Exception as exc:
        return err(str(exc))


@router.get("/delete_comparison_report/")
async def delete_comparison_report(report_id: int):
    try:
        report = await ComparisonReportCollection.get_report(report_id)
        report_path = _comparison_report_path(report["report_path"]) if report.get("report_path") else None
        await ComparisonReportCollection.delete_report(report_id)
        if report_path and os.path.exists(report_path):
            os.remove(report_path)
        return ok("已删除")
    except Exception as exc:
        return err(str(exc))
