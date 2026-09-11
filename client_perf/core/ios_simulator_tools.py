# coding: utf-8
"""iOS Simulator support based on xcrun simctl and host process metrics."""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import psutil

from client_perf.core.monitor import Monitor
from client_perf.log import log as logger

XCRUN_PATH = shutil.which("xcrun")
SIMCTL_AVAILABLE = platform.system() == "Darwin" and bool(XCRUN_PATH)


def _run_simctl(args: list[str], timeout: int = 15, text: bool = True):
    if not SIMCTL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            [XCRUN_PATH, "simctl", *args],
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            logger.error("simctl %s 失败: %s", " ".join(args), result.stderr)
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("simctl %s 异常: %s", " ".join(args), exc)
        return None


def get_ios_simulator_devices() -> List[Dict]:
    raw = _run_simctl(["list", "devices", "available", "--json"])
    if not raw:
        return []
    try:
        runtimes = json.loads(raw).get("devices", {})
    except (TypeError, json.JSONDecodeError):
        return []

    devices = []
    for runtime_id, entries in runtimes.items():
        runtime_parts = runtime_id.rsplit(".", 1)[-1].split("-")
        runtime = " ".join(runtime_parts[:2])
        if len(runtime_parts) > 2:
            runtime += "." + ".".join(runtime_parts[2:])
        for item in entries:
            devices.append({
                "device_type": "ios_simulator",
                "serial": item.get("udid", ""),
                "model": item.get("name", "iOS Simulator"),
                "name": item.get("name", "iOS Simulator"),
                "platform": "iOS Simulator",
                "os_version": runtime,
                "status": "online" if item.get("state") == "Booted" else "offline",
            })
    return devices


def _launchctl_rows(udid: str) -> list[tuple[int, str]]:
    raw = _run_simctl(["spawn", udid, "launchctl", "list"])
    rows = []
    for line in (raw or "").splitlines()[1:]:
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[2]))
    return rows


def _bundle_id_from_label(label: str) -> str:
    match = re.match(r"UIKitApplication:([^\[]+)", label)
    return match.group(1) if match else label


async def ios_simulator_apps(udid: str) -> List[Dict]:
    def real_func():
        apps = []
        for pid, label in _launchctl_rows(udid):
            if not label.startswith("UIKitApplication:"):
                continue
            bundle_id = _bundle_id_from_label(label)
            apps.append({
                "package_name": bundle_id,
                "name": bundle_id,
                "pid": pid,
                "status": "running",
            })
        return sorted(apps, key=lambda item: item["package_name"])

    return await asyncio.to_thread(real_func)


def _target_processes(pid: int, include_child: bool) -> list[psutil.Process]:
    if not pid:
        return []
    try:
        main = psutil.Process(pid)
        processes = [main]
        if include_child:
            processes.extend(main.children(recursive=True))
        return [process for process in processes if process.is_running()]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


async def ios_simulator_cpu(pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    def real_func():
        processes = _target_processes(pid, include_child)
        if not processes:
            # 无 PID / 进程不存在 → 进程级 CPU 不可用；核心数为宿主机真实值
            return {"cpu_usage": None, "cpu_usage_all": None, "cpu_core_num": psutil.cpu_count() or 1, "time": int(time.time())}
        before = {}
        for process in processes:
            try:
                before[process.pid] = sum(process.cpu_times()[:2])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        started = time.monotonic()
        time.sleep(1)
        elapsed = max(time.monotonic() - started, 0.001)
        used = 0.0
        for process in processes:
            try:
                if process.pid in before:
                    used += max(0.0, sum(process.cpu_times()[:2]) - before[process.pid])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        cores = psutil.cpu_count() or 1
        usage = used / elapsed * 100.0
        return {"cpu_usage": round(usage, 2), "cpu_usage_all": round(usage / cores, 2), "cpu_core_num": cores, "time": int(time.time())}

    return await asyncio.to_thread(real_func)


async def ios_simulator_memory(pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    def real_func():
        processes = _target_processes(pid, include_child)
        if not processes:
            # 无 PID / 进程不存在 → 进程内存不可用；字段始终存在
            return {"process_memory_usage": None, "time": int(time.time())}
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {"process_memory_usage": round(total / 1024 / 1024, 2), "time": int(time.time())}

    return await asyncio.to_thread(real_func)


async def ios_simulator_process_info(pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    def real_func():
        processes = _target_processes(pid, include_child)
        if not processes:
            # 无 PID / 进程不存在 → 线程数不可用；iOS/Simulator 不支持 handles → None
            return {"num_threads": None, "num_handles": None, "time": int(time.time())}
        threads = 0
        for process in processes:
            try:
                threads += process.num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass
        # num_handles：iOS / Simulator 无 handles 概念，始终为 None
        return {"num_threads": threads, "num_handles": None, "time": int(time.time())}

    return await asyncio.to_thread(real_func)


async def ios_simulator_disk_io(**kwargs) -> Dict:
    # 模拟器没有可信的“按进程磁盘”来源，按规范“无可信磁盘来源字段为 None”。
    return {"disk_read_rate": None, "disk_write_rate": None, "disk_read": None, "disk_write": None, "time": int(time.time())}


async def ios_simulator_network_io(**kwargs) -> Dict:
    # 模拟器没有可信的“按进程网络”来源；macOS 也不暴露稳定的 per-process 字节计数。
    # 从不回退到整机(host-wide)网络计数。按规范“无可信网络来源字段为 None”。
    return {"net_sent_rate": None, "net_recv_rate": None, "net_sent": None, "net_recv": None, "time": int(time.time())}


async def ios_simulator_fps(**kwargs) -> Dict:
    # 模拟器没有可信的 FPS 来源，按规范“无可信 FPS 来源字段为 None”；
    # frames 始终为 []（无 FPS 明细时）。
    return {"fps": None, "frames": [], "time": int(time.time())}


async def ios_simulator_gpu(**kwargs) -> Dict:
    # 模拟器没有可信的 GPU 来源，按规范“无可信 GPU 来源字段为 None”。
    return {"gpu": None, "time": int(time.time())}


async def ios_simulator_sys_info(udid: str, **kwargs) -> Dict:
    device = next((item for item in get_ios_simulator_devices() if item["serial"] == udid), None)
    return {**(device or {"platform": "iOS Simulator", "serial": udid}), "time": int(time.time())}


async def ios_simulator_screenshot(udid: str, save_dir: str = None, **kwargs) -> Optional[bytes]:
    data = await asyncio.to_thread(_run_simctl, ["io", udid, "screenshot", "-"], 20, False)
    if not data:
        return None
    if save_dir:
        screenshot_dir = Path(save_dir) / "screenshot"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        (screenshot_dir / f"{time.time_ns()}.png").write_bytes(data)
        return None
    return BytesIO(data).getvalue()


async def ios_simulator_perf(udid: str, bundle_id: str, pid: int, save_dir: str, include_child: bool = False):
    if not pid and bundle_id:
        apps = await ios_simulator_apps(udid)
        pid = next((item["pid"] for item in apps if item["package_name"] == bundle_id), 0)

    common = {"pid": pid, "include_child": include_child, "save_dir": save_dir}
    monitors = [
        Monitor(ios_simulator_cpu, **common, monitor_name="cpu", key_value=["time", "cpu_usage(%)", "cpu_usage_all(%)", "cpu_core_num(个)"]),
        Monitor(ios_simulator_memory, **common, monitor_name="memory", key_value=["time", "process_memory_usage(M)"]),
        Monitor(ios_simulator_process_info, **common, monitor_name="process_info", key_value=["time", "num_threads(个)", "num_handles(个)"]),
        Monitor(ios_simulator_fps, **common, monitor_name="fps", key_value=["time", "fps(帧)", "frames"]),
        Monitor(ios_simulator_gpu, **common, monitor_name="gpu", key_value=["time", "gpu(%)"]),
        Monitor(ios_simulator_disk_io, **common, monitor_name="disk_io", key_value=["time", "disk_read_rate(MB/s)", "disk_write_rate(MB/s)", "disk_read(字节)", "disk_write(字节)"]),
        Monitor(ios_simulator_network_io, **common, monitor_name="network_io", key_value=["time", "net_sent_rate(MB/s)", "net_recv_rate(MB/s)", "net_sent(字节)", "net_recv(字节)"]),
        Monitor(ios_simulator_screenshot, udid=udid, save_dir=save_dir, is_out=False),
    ]
    await asyncio.gather(*(monitor.run() for monitor in monitors))
