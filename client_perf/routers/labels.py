# coding: utf-8
"""区间标签管理路由。"""

from fastapi import APIRouter
from pydantic import BaseModel

from client_perf.db import LabelCollection
from client_perf.routers.common import err, ok

router = APIRouter()


class LabelCreateBody(BaseModel):
    task_id: int
    name: str
    start_ts: float
    end_ts: float
    color: str = "#3b6ef0"
    note: str = ""


@router.get("/get_labels/")
async def get_labels(task_id: int | None = None):
    try:
        if task_id is not None:
            return ok(await LabelCollection.get_labels_by_task(task_id))
        return ok(await LabelCollection.get_all_labels())
    except Exception as exc:
        return err(str(exc))


@router.post("/create_label/")
async def create_label(body: LabelCreateBody):
    try:
        label = await LabelCollection.create_label(
            task_id=body.task_id,
            name=body.name,
            start_ts=body.start_ts,
            end_ts=body.end_ts,
            color=body.color,
            note=body.note,
        )
        return ok(label)
    except Exception as exc:
        return err(str(exc))


@router.post("/update_label/")
async def update_label(
    label_id: int,
    name: str | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
    color: str | None = None,
    note: str | None = None,
):
    try:
        label = await LabelCollection.update_label(
            label_id=label_id,
            name=name,
            start_ts=start_ts,
            end_ts=end_ts,
            color=color,
            note=note,
        )
        return ok(label)
    except Exception as exc:
        return err(str(exc))


@router.delete("/delete_label/")
async def delete_label(label_id: int):
    try:
        return ok(await LabelCollection.delete_label(label_id))
    except Exception as exc:
        return err(str(exc))
