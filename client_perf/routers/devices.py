# coding: utf-8
"""设备发现、进程和截图路由。"""

import base64

from fastapi import APIRouter

from client_perf.core.device_manager import (
    DEVICE_TYPE_ANDROID,
    DEVICE_TYPE_HARMONY,
    DEVICE_TYPE_IOS,
    DEVICE_TYPE_IOS_SIMULATOR,
    DeviceManager,
    get_platform_capabilities,
)
from client_perf.routers.common import err, ok

router = APIRouter()


@router.get("/get_devices/")
async def get_devices():
    try:
        return ok(DeviceManager.get_all_devices())
    except Exception as exc:
        return err(str(exc))


@router.get("/platform_capabilities/")
async def platform_capabilities():
    try:
        return ok(get_platform_capabilities())
    except Exception as exc:
        return err(str(exc))


@router.get("/system_info/")
async def system_info(device_type: str = "pc", device_id: str | None = None):
    try:
        info = await DeviceManager.get_device_sys_info(device_type, device_id or "")
        return ok(info)
    except Exception as exc:
        return err(str(exc))


@router.get("/get_pids/")
async def get_pids(
    is_print_tree: bool = False,
    device_type: str = "pc",
    device_id: str | None = None,
):
    try:
        if device_type == DEVICE_TYPE_ANDROID:
            from client_perf.core.android_tools import android_packages
            return ok(await android_packages(device_id))
        if device_type == DEVICE_TYPE_IOS:
            from client_perf.core.ios_tools import ios_apps
            return ok(await ios_apps(device_id))
        if device_type == DEVICE_TYPE_IOS_SIMULATOR:
            from client_perf.core.ios_simulator_tools import ios_simulator_apps
            return ok(await ios_simulator_apps(device_id))
        if device_type == DEVICE_TYPE_HARMONY:
            from client_perf.core.harmony_tools import harmony_packages
            return ok(await harmony_packages(device_id))

        from client_perf.core.pc_tools import pids, process_tree
        return ok(await process_tree() if is_print_tree else await pids())
    except Exception as exc:
        return err(str(exc))


@router.get("/get_device_apps/")
async def get_device_apps(device_type: str, device_id: str):
    try:
        return ok(await DeviceManager.get_device_apps_async(device_type, device_id))
    except Exception as exc:
        return err(str(exc))


@router.get("/pid_img/")
async def pid_img(pid: int = 0, device_type: str = "pc", device_id: str | None = None):
    try:
        image = await DeviceManager.take_screenshot(device_type, device_id or "", pid)
        return base64.b64encode(image).decode() if image else ""
    except Exception as exc:
        return err(str(exc))
