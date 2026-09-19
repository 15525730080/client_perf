# coding: utf-8
"""
TaskHandle — 每个性能采集任务在独立子进程中运行。

支持平台：pc / android / ios / harmony
"""
import asyncio
import os
import traceback
from multiprocessing import Process

import psutil

from client_perf.db import TaskCollection
from client_perf.log import log as logger


class TaskHandle(Process):

    def __init__(
        self,
        serialno: str,
        target_pid: int,
        file_dir: str,
        task_id: int,
        platform_name: str,
        include_child: bool,
        device_type: str = "pc",
        device_id: str | None = None,
        package_name: str | None = None,
    ) -> None:
        super().__init__(daemon=True)
        self.serialno = serialno
        self.target_pid = target_pid
        self.file_dir = file_dir
        self.task_id = task_id
        self.platform_name = platform_name
        self.include_child = include_child
        self.device_type = device_type
        self.device_id = device_id
        self.package_name = package_name

        os.makedirs(self.file_dir, exist_ok=True)

    # ── 子进程入口 ────────────────────────────────────────────

    def run(self) -> None:
        logger.info(
            f"[TaskHandle] start task_id={self.task_id} "
            f"device_type={self.device_type} device_id={self.device_id} "
            f"package={self.package_name}"
        )
        try:
            task = asyncio.run(TaskCollection.set_task_running(self.task_id, self.pid))
            if task.get("status") != 1:
                logger.info(
                    f"[TaskHandle] task_id={self.task_id} 已取消，跳过采集启动"
                )
                return

            if self.device_type == "android":
                self._run_android()
            elif self.device_type == "ios":
                self._run_ios()
            elif self.device_type == "ios_simulator":
                self._run_ios_simulator()
            elif self.device_type == "harmony":
                self._run_harmony()
            else:
                self._run_pc()
        except Exception:
            logger.error(traceback.format_exc())
            try:
                asyncio.run(TaskCollection.fail_task(self.task_id))
            except Exception:
                logger.error("更新失败任务状态失败:\n%s", traceback.format_exc())
        else:
            try:
                asyncio.run(TaskCollection.stop_task(self.task_id))
            except Exception:
                logger.error("更新已结束任务状态失败:\n%s", traceback.format_exc())

    # ── 各平台采集 ────────────────────────────────────────────

    def _run_pc(self) -> None:
        from client_perf.core.pc_tools import perf as pc_perf
        asyncio.run(
            pc_perf(self.target_pid, self.file_dir, include_child=self.include_child)
        )

    def _run_android(self) -> None:
        from client_perf.core.android_tools import android_perf, ADB_AVAILABLE
        if not ADB_AVAILABLE:
            logger.error("adbutils 未安装，无法执行 Android 性能采集")
            return
        asyncio.run(
            android_perf(
                serial=self.device_id,
                package_name=self.package_name or "",
                pid=self.target_pid,
                save_dir=self.file_dir,
                include_child=self.include_child,
            )
        )

    def _run_ios(self) -> None:
        from client_perf.core.ios_tools import ios_perf
        asyncio.run(
            ios_perf(
                udid=self.device_id,
                bundle_id=self.package_name or "",
                pid=self.target_pid,
                save_dir=self.file_dir,
                include_child=self.include_child,
            )
        )

    def _run_ios_simulator(self) -> None:
        from client_perf.core.ios_simulator_tools import ios_simulator_perf
        asyncio.run(
            ios_simulator_perf(
                udid=self.device_id or "booted",
                bundle_id=self.package_name or "",
                pid=self.target_pid,
                save_dir=self.file_dir,
                include_child=self.include_child,
            )
        )

    def _run_harmony(self) -> None:
        from client_perf.core.harmony_tools import harmony_perf, HDC_AVAILABLE
        if not HDC_AVAILABLE:
            logger.error("hdc 未找到，无法执行 HarmonyOS 性能采集")
            return
        asyncio.run(
            harmony_perf(
                serial=self.device_id,
                package_name=self.package_name or "",
                pid=self.target_pid,
                save_dir=self.file_dir,
                include_child=self.include_child,
            )
        )

    # ── 停止 ──────────────────────────────────────────────────

    @staticmethod
    def stop_handle(monitor_pid: int) -> None:
        """强制终止采集子进程及其所有子进程"""
        if not monitor_pid:
            return
        logger.info(f"[TaskHandle] stop monitor_pid={monitor_pid}")
        try:
            proc = psutil.Process(monitor_pid)
            for child in proc.children(recursive=True):
                try:
                    os.kill(child.pid, 9)
                except Exception:
                    pass
            os.kill(proc.pid, 9)
        except psutil.NoSuchProcess:
            logger.warning(f"进程 {monitor_pid} 已不存在")
        except Exception:
            logger.error(traceback.format_exc())
