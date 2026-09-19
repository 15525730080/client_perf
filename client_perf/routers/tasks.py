# coding: utf-8
"""采集任务生命周期路由。"""

import os
import platform
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from client_perf.core.device_manager import DeviceManager
from client_perf.db import TaskCollection
from client_perf.paths import get_data_dir
from client_perf.routers.common import err, ok
from client_perf.task_handle import TaskHandle
from client_perf.util import DataCollect

router = APIRouter()
BASE_DIR = get_data_dir() / "test_result"


def _task_result_dir(file_dir: str) -> Path:
    """校验任务结果目录必须位于本项目的 test_result 下。"""
    root = BASE_DIR.resolve()
    candidate = Path(file_dir).resolve()
    if candidate == root or root not in candidate.parents:
        raise RuntimeError(f"拒绝删除任务结果目录之外的路径: {candidate}")
    return candidate


@router.get("/get_all_task/")
async def get_all_task():
    return ok(await TaskCollection.get_all_task())


@router.get("/run_task/")
async def run_task(
    pid: int = 0,
    pid_name: str = "",
    task_name: str = "",
    include_child: bool = False,
    device_type: str = "pc",
    device_id: str | None = None,
    package_name: str | None = None,
):
    try:
        if device_type != "pc" and not pid:
            if not device_id or not package_name:
                return err("移动端任务必须指定运行中的应用和固定主 PID")
            apps = await DeviceManager.get_device_apps_async(device_type, device_id)
            matched_app = next(
                (
                    app for app in apps
                    if (app.get("package_name") or app.get("bundle_id")) == package_name
                    and int(app.get("pid") or 0) > 0
                ),
                None,
            )
            if not matched_app:
                return err(f"应用未运行或无法解析固定主 PID: {package_name}")
            pid = int(matched_app["pid"])

        task_id, file_dir = await TaskCollection.create_task(
            pid,
            pid_name,
            str(BASE_DIR),
            task_name,
            include_child,
            device_type=device_type,
            device_id=device_id,
            package_name=package_name,
        )
        await TaskCollection.mark_task_starting(task_id)

        handle = TaskHandle(
            serialno=device_id or platform.node(),
            file_dir=file_dir,
            task_id=task_id,
            platform_name=platform.system() if device_type == "pc" else device_type,
            target_pid=pid,
            include_child=include_child,
            device_type=device_type,
            device_id=device_id,
            package_name=package_name,
        )
        handle.start()
        return ok()
    except Exception as exc:
        if "task_id" in locals():
            try:
                await TaskCollection.fail_task(task_id)
            except Exception:
                pass
        return err(str(exc))


@router.get("/stop_task/")
async def stop_task(task_id: int):
    try:
        task = await TaskCollection.stop_task(task_id)
        TaskHandle.stop_handle(task.get("monitor_pid"))
        return ok()
    except Exception as exc:
        return err(str(exc))


@router.get("/task_status/")
async def task_status(task_id: int):
    try:
        task = await TaskCollection.get_item_task(task_id)
        return ok(task.get("status"))
    except Exception as exc:
        return err(str(exc))


@router.get("/result/")
async def task_result(task_id: int):
    try:
        task = await TaskCollection.get_item_task(task_id)
        return ok(await DataCollect(task["file_dir"]).get_all_data())
    except Exception as exc:
        return err(str(exc))


@router.get("/task_screenshot/")
async def task_screenshot(task_id: int, timestamp: float):
    """返回最接近指标时间点的任务截图，兼容秒/纳秒文件名和常见图片扩展名。"""
    try:
        task = await TaskCollection.get_item_task(task_id)
        screenshot_dir = _task_result_dir(task["file_dir"]) / "screenshot"
        if not screenshot_dir.is_dir():
            raise HTTPException(status_code=404, detail="该任务没有截图")

        candidates = []
        for image_path in screenshot_dir.iterdir():
            if not image_path.is_file() or image_path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            try:
                image_timestamp = int(image_path.stem)
            except ValueError:
                continue
            if image_timestamp > 10_000_000_000:
                image_timestamp /= 1_000_000_000
            candidates.append((abs(image_timestamp - timestamp), image_path))

        if not candidates:
            raise HTTPException(status_code=404, detail="该时间点没有可用截图")
        delta, image_path = min(candidates, key=lambda item: item[0])
        if delta > 2:
            raise HTTPException(status_code=404, detail="该时间点没有匹配截图")
        return FileResponse(image_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/delete_task/")
async def delete_task(task_id: int):
    try:
        item = await TaskCollection.get_item_task(task_id)
        result_dir = _task_result_dir(item["file_dir"]) if item.get("file_dir") else None
        await TaskCollection.delete_task(task_id)
        if result_dir and result_dir.exists():
            shutil.rmtree(result_dir)
        return ok()
    except Exception as exc:
        return err(str(exc))


@router.get("/change_task_name/")
async def change_task_name(task_id: int, new_name: str):
    try:
        task = await TaskCollection.change_task_name(task_id, new_name)
        return ok(f"已重命名为: {task['name']}")
    except Exception as exc:
        return err(str(exc))


@router.get("/set_task_version/")
async def set_task_version(task_id: int, version: str):
    try:
        await TaskCollection.set_task_version(task_id, version)
        return ok(f"已设置版本: {version}")
    except Exception as exc:
        return err(str(exc))


@router.get("/set_task_baseline/")
async def set_task_baseline(task_id: int, is_baseline: bool = True):
    try:
        await TaskCollection.set_task_baseline(task_id, is_baseline)
        return ok("已设置基线" if is_baseline else "已取消基线")
    except Exception as exc:
        return err(str(exc))


@router.get("/get_baseline_task/")
async def get_baseline_task():
    try:
        return ok(await TaskCollection.get_baseline_task())
    except Exception as exc:
        return err(str(exc))
