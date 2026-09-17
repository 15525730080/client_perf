# coding: utf-8
"""client-perf 命令行入口。

CLI 直接复用 DeviceManager、TaskCollection 和 DataCollect，不通过 HTTP 回调自身。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Sequence

from client_perf import __version__


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client-perf",
        description="client-perf 客户端性能采集与分析工具",
    )
    parser.add_argument("--version", action="version", version=f"client-perf {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址；如需局域网访问可显式传 0.0.0.0")
    serve.add_argument("--port", type=int, default=8080, help="监听端口")
    serve.add_argument("--reload", action="store_true", help="启用开发模式热重载")
    serve.add_argument("--log-level", default="info", help="uvicorn 日志级别")

    subparsers.add_parser("devices", help="列出已连接设备")
    subparsers.add_parser("capabilities", help="查看各平台采集能力")

    system_info = subparsers.add_parser("system-info", help="查看设备系统信息")
    system_info.add_argument(
        "--device-type",
        default="pc",
        choices=("pc", "android", "ios", "ios_simulator", "harmony"),
    )
    system_info.add_argument("--device-id", default="", help="设备 ID/UDID")

    apps = subparsers.add_parser("apps", help="列出设备应用")
    apps.add_argument(
        "--device-type",
        required=True,
        choices=("android", "ios", "ios_simulator", "harmony"),
    )
    apps.add_argument("--device-id", required=True, help="设备 ID/UDID")

    subparsers.add_parser("tasks", help="列出采集任务")

    result = subparsers.add_parser("result", help="输出任务采集结果")
    result.add_argument("task_id", type=int, help="任务 ID")
    result.add_argument("--raw", action="store_true", help="输出未对齐的原始时序数据")

    subparsers.add_parser("native-start", help="先执行 nativebuild，再按原参数启动")

    return parser


def _run_server(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "client_perf.api:app",
        host=args.host,
        port=args.port,
        workers=1,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


async def _run_async_command(args: argparse.Namespace) -> int:
    if args.command == "devices":
        from client_perf.core.device_manager import DeviceManager

        print_json(DeviceManager.get_all_devices())
        return 0

    if args.command == "capabilities":
        from client_perf.core.device_manager import get_platform_capabilities

        print_json(get_platform_capabilities())
        return 0

    if args.command == "system-info":
        from client_perf.core.device_manager import DeviceManager

        print_json(await DeviceManager.get_device_sys_info(args.device_type, args.device_id))
        return 0

    if args.command == "apps":
        from client_perf.core.device_manager import DeviceManager

        print_json(await DeviceManager.get_device_apps_async(args.device_type, args.device_id))
        return 0

    if args.command in {"tasks", "result"}:
        from client_perf.db import TaskCollection, create_tables

        await create_tables()
        if args.command == "tasks":
            print_json(await TaskCollection.get_all_task())
            return 0

        from client_perf.util import DataCollect

        task = await TaskCollection.get_item_task(args.task_id)
        data = await DataCollect(task["file_dir"]).get_all_data(is_format=not args.raw)
        print_json({"task": task, "data": data})
        return 0

    raise ValueError(f"不支持的命令: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    try:
        # native-start 只增加一次原生构建，随后完全复用原启动参数和执行路径。
        if raw_args[:1] == ["native-start"]:
            from client_perf import native_build

            native_build.main([])
            native_build.activate_native()
            raw_args = raw_args[1:]

        # 保持旧用法兼容：无参数或直接传 --host/--port/--reload 时启动 Web 服务。
        if not raw_args or raw_args[0] not in {
            "serve", "devices", "capabilities", "system-info", "apps", "tasks", "result", "native-start",
            "-h", "--help", "--version",
        }:
            raw_args.insert(0, "serve")
        args = parser.parse_args(raw_args)

        if args.command == "serve":
            return _run_server(args)
        return asyncio.run(_run_async_command(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        parser.exit(1, f"client-perf: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
