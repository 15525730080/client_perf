# coding: utf-8
"""任务、标签与高级分析路由。"""

import traceback

from fastapi import APIRouter

from client_perf.log import log as logger
from client_perf.routers.common import err, ok, parse_ids
from client_perf.services.analysis import (
    TaskComparison,
    advanced_compare,
    compare_labels as compare_labels_service,
)

router = APIRouter()


@router.get("/compare_tasks/")
async def compare_tasks(task_ids: str, base_task_id: int | None = None):
    try:
        return ok(await TaskComparison.create_comparison(parse_ids(task_ids), base_task_id))
    except Exception as exc:
        return err(str(exc))


@router.get("/advanced_compare_tasks/")
async def advanced_compare_tasks(task_ids: str, base_task_id: int | None = None):
    try:
        return ok(await advanced_compare(parse_ids(task_ids), base_task_id))
    except ValueError as exc:
        return err(str(exc), 400)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@router.get("/compare_labels/")
async def compare_labels(label_ids: str):
    try:
        return ok(await compare_labels_service(parse_ids(label_ids)))
    except ValueError as exc:
        return err(str(exc), 400)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))
