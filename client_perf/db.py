# coding: utf-8
"""
数据库层 — SQLAlchemy 2.x async ORM（驱动：aiosqlite）

设计原则
--------
* ORM 模型定义在本文件底部（_models 区），对外只暴露 Collection 类。
* Collection 类的方法签名与旧版完全兼容，api.py / comparison.py 无需修改。
* 数据库迁移：startup 时执行 _run_migrations()，用 ALTER TABLE 幂等地补列，
  新增表直接 CREATE TABLE IF NOT EXISTS，永远不删已有数据。
"""
from __future__ import annotations

import datetime
import json
import os
import platform
from typing import Any

from sqlalchemy import (
    Boolean, Column, Float, Integer, String, Text,
    delete, select, update,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from client_perf.log import log as logger
from client_perf.paths import PROJECT_ROOT as _PATH_PROJECT_ROOT, get_db_path

# ── 数据库路径 ────────────────────────────────────────────────
# 保留 _PROJECT_ROOT 以兼容既有测试和旧数据库查找逻辑。
_PROJECT_ROOT = str(_PATH_PROJECT_ROOT)


def _resolve_db_path() -> str:
    env_path = os.environ.get("CLIENT_PERF_DB_PATH")
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))

    root_db = os.path.join(_PROJECT_ROOT, "task.sqlite")
    if os.path.isfile(root_db):
        return root_db

    cwd_db = os.path.join(os.getcwd(), "task.sqlite")
    if os.path.isfile(cwd_db):
        return cwd_db

    return str(get_db_path())


DB_PATH = _resolve_db_path()
_DB_URL  = f"sqlite+aiosqlite:///{DB_PATH}"

# ── Engine & Session ──────────────────────────────────────────
# timeout=5 等价于设置 busy_timeout 为 5s（并发写时自动等待而非立即报错）。
_engine = create_async_engine(
    _DB_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 5},
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


# ── ORM Base ──────────────────────────────────────────────────

class _Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════
#  ORM 模型
# ══════════════════════════════════════════════════════════════

class TaskModel(_Base):
    __tablename__ = "tasks"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    start_time      = Column(String)
    end_time        = Column(String)
    serialno        = Column(String)
    status          = Column(Integer, default=0)
    file_dir        = Column(String)
    target_pid      = Column(Integer, default=0)
    target_pid_name = Column(String)
    monitor_pid     = Column(Integer)
    platform        = Column(String, default="unknown")
    name            = Column(String)
    include_child   = Column(Integer, default=0)
    version         = Column(String)
    is_baseline     = Column(Integer, default=0)
    device_type     = Column(String, default="pc")
    device_id       = Column(String)
    package_name    = Column(String)


class ComparisonReportModel(_Base):
    __tablename__ = "comparison_reports"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    name         = Column(String)
    create_time  = Column(String)
    task_ids     = Column(Text)          # JSON list
    base_task_id = Column(Integer)
    report_path  = Column(String)
    description  = Column(Text)


class LabelModel(_Base):
    __tablename__ = "labels"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    task_id     = Column(Integer, nullable=False)
    name        = Column(String, nullable=False)
    start_ts    = Column(Float, nullable=False)
    end_ts      = Column(Float, nullable=False)
    color       = Column(String, default="#3b6ef0")
    note        = Column(Text, default="")
    create_time = Column(String)


# ══════════════════════════════════════════════════════════════
#  迁移：幂等地补列 / 建表
# ══════════════════════════════════════════════════════════════

# 每个元素：(table, column, col_def_sql)
# 只需列出"可能缺失"的列（新版本新增的列）
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("tasks", "version",      "TEXT"),
    ("tasks", "is_baseline",  "INTEGER DEFAULT 0"),
    ("tasks", "device_type",  "TEXT DEFAULT 'pc'"),
    ("tasks", "device_id",    "TEXT"),
    ("tasks", "package_name", "TEXT"),
    ("tasks", "include_child","INTEGER DEFAULT 0"),
    ("comparison_reports", "report_path",  "TEXT"),
    ("comparison_reports", "description",  "TEXT"),
    ("labels", "color",       "TEXT DEFAULT '#3b6ef0'"),
    ("labels", "note",        "TEXT DEFAULT ''"),
    ("labels", "create_time", "TEXT"),
]


async def _run_migrations() -> None:
    """
    1. 启用 SQLite WAL + busy_timeout，提升并发健壮性。
    2. 用 ORM metadata 创建所有不存在的表（CREATE TABLE IF NOT EXISTS）。
    3. 逐列检查 _MIGRATIONS，缺失则 ALTER TABLE ADD COLUMN（SQLite 支持）。
    """
    async with _engine.begin() as conn:
        # WAL 写入模式 + 忙等待超时（连接级，每次启动幂等设置）
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        await conn.exec_driver_sql("PRAGMA busy_timeout=5000;")

        # 建表（幂等）
        await conn.run_sync(_Base.metadata.create_all)

        # 补列
        for table, col, col_def in _MIGRATIONS:
            # 查询现有列名
            result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            existing = {row[1] for row in result.fetchall()}   # row[1] = name
            if col not in existing:
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"
                )
                logger.info(f"[migration] ALTER TABLE {table} ADD COLUMN {col}")


async def create_tables() -> None:
    """对外入口：建表 + 迁移，在 FastAPI lifespan 中调用。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    await _run_migrations()
    logger.info(f"数据库就绪: {DB_PATH}")


# ══════════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def _model_to_dict(obj: Any) -> dict[str, Any]:
    """将 ORM 实例转为普通 dict（过滤 SQLAlchemy 内部属性）。"""
    return {
        c.key: getattr(obj, c.key)
        for c in obj.__table__.columns
    }


TASK_CREATED = 0
TASK_RUNNING = 1
TASK_STOPPED = 2
TASK_FAILED = 3


# ══════════════════════════════════════════════════════════════
#  TaskCollection
# ══════════════════════════════════════════════════════════════

class TaskCollection:

    @classmethod
    async def create_task(
        cls,
        pid: int,
        pid_name: str,
        base_dir: str,
        name: str,
        include_child: bool,
        device_type: str = "pc",
        device_id: str | None = None,
        package_name: str | None = None,
    ) -> tuple[int, str]:
        """创建任务，返回 (task_id, file_dir)"""
        async with _Session() as s, s.begin():
            # 防重复
            if device_type == "pc":
                dup = (await s.execute(
                    select(TaskModel)
                    .where(TaskModel.target_pid == pid,
                           TaskModel.status.in_([0, 1]))
                )).scalar_one_or_none()
            else:
                dup = (await s.execute(
                    select(TaskModel)
                    .where(TaskModel.device_type == device_type,
                           TaskModel.device_id == device_id,
                           TaskModel.package_name == package_name,
                           TaskModel.status.in_([0, 1]))
                )).scalar_one_or_none()

            if dup:
                raise RuntimeError(
                    f"已有运行中的任务 id={dup.id} name={dup.name}"
                )

            if device_type == "pc":
                task_platform = platform.system()
                serialno = platform.node()
            elif device_type == "android":
                task_platform, serialno = "Android", device_id or ""
            elif device_type == "ios":
                task_platform, serialno = "iOS", device_id or ""
            elif device_type == "ios_simulator":
                task_platform, serialno = "iOS Simulator", device_id or ""
            else:
                task_platform = device_type
                serialno = device_id or platform.node()

            task = TaskModel(
                start_time=_now(),
                serialno=serialno,
                status=TASK_CREATED,
                target_pid=pid,
                target_pid_name=pid_name or "",
                platform=task_platform,
                name=name or "",
                include_child=int(include_child),
                device_type=device_type,
                device_id=device_id,
                package_name=package_name,
            )
            s.add(task)
            await s.flush()          # 获取自增 id

            file_dir = os.path.join(base_dir, str(task.id))
            task.file_dir = file_dir

        return task.id, file_dir

    @classmethod
    async def set_task_running(cls, task_id: int, monitor_pid: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            await s.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(status=TASK_RUNNING, monitor_pid=monitor_pid)
            )
            task = await s.get(TaskModel, task_id)
        return _model_to_dict(task)

    @classmethod
    async def mark_task_starting(cls, task_id: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            task.status = TASK_RUNNING
        return _model_to_dict(task)

    @classmethod
    async def stop_task(cls, task_id: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            task.status = TASK_STOPPED
            task.end_time = _now()
        return _model_to_dict(task)

    @classmethod
    async def fail_task(cls, task_id: int) -> dict[str, Any]:
        """将异常退出的采集任务标记为失败，避免误判为正常结束。"""
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            task.status = TASK_FAILED
            task.end_time = _now()
        return _model_to_dict(task)

    @classmethod
    async def get_all_task(cls) -> list[dict[str, Any]]:
        async with _Session() as s:
            rows = (await s.execute(
                select(TaskModel).order_by(TaskModel.id.desc())
            )).scalars().all()
        return [_model_to_dict(r) for r in rows]

    @classmethod
    async def get_item_task(cls, task_id: int) -> dict[str, Any]:
        async with _Session() as s:
            task = await s.get(TaskModel, task_id)
        if not task:
            raise RuntimeError(f"任务 {task_id} 不存在")
        return _model_to_dict(task)

    @classmethod
    async def delete_task(cls, task_id: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            if task.status == 1:
                raise RuntimeError("任务运行中，不能删除")
            result = _model_to_dict(task)
            await s.delete(task)
        return result

    @classmethod
    async def change_task_name(cls, task_id: int, new_name: str) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            task.name = new_name
        return _model_to_dict(task)

    @classmethod
    async def set_task_version(cls, task_id: int, version: str) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")
            task.version = version
        return _model_to_dict(task)

    @classmethod
    async def set_task_baseline(cls, task_id: int, is_baseline: bool = True) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            # 验证目标任务存在
            task = await s.get(TaskModel, task_id)
            if not task:
                raise RuntimeError(f"任务 {task_id} 不存在")

            if is_baseline:
                # 仅取消其它已置位基线的任务，再置位目标任务（单一基线）
                await s.execute(
                    update(TaskModel)
                    .where(TaskModel.is_baseline == 1, TaskModel.id != task_id)
                    .values(is_baseline=0)
                )
            await s.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(is_baseline=int(is_baseline))
            )
            task = await s.get(TaskModel, task_id)
        return _model_to_dict(task)

    @classmethod
    async def get_baseline_task(cls) -> dict[str, Any] | None:
        async with _Session() as s:
            task = (await s.execute(
                select(TaskModel).where(TaskModel.is_baseline == 1).limit(1)
            )).scalar_one_or_none()
        return _model_to_dict(task) if task else None

    @classmethod
    async def recover_interrupted_tasks(cls) -> int:
        """启动时收敛上次异常退出遗留的待启动/运行中任务，不操作历史 PID。"""
        async with _Session() as s, s.begin():
            result = await s.execute(
                update(TaskModel)
                .where(TaskModel.status.in_([0, 1]))
                .values(status=2, end_time=_now(), monitor_pid=None)
            )
        return result.rowcount or 0


# ══════════════════════════════════════════════════════════════
#  ComparisonReportCollection
# ══════════════════════════════════════════════════════════════

class ComparisonReportCollection:

    @classmethod
    async def create_report(
        cls,
        name: str,
        task_ids: list[int],
        base_task_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            report = ComparisonReportModel(
                name=name,
                create_time=_now(),
                task_ids=json.dumps(task_ids),
                base_task_id=base_task_id,
                description=description,
            )
            s.add(report)
            await s.flush()
            result = _model_to_dict(report)
        return result

    @classmethod
    async def update_report(cls, report_id: int, **kwargs: Any) -> dict[str, Any]:
        if not kwargs:
            return await cls.get_report(report_id)
        async with _Session() as s, s.begin():
            report = await s.get(ComparisonReportModel, report_id)
            if not report:
                raise RuntimeError(f"对比报告 {report_id} 不存在")
            for key, value in kwargs.items():
                if not hasattr(report, key):
                    raise ValueError(f"不支持的报告字段: {key}")
                setattr(report, key, value)
        return _model_to_dict(report)

    @classmethod
    async def get_report(cls, report_id: int) -> dict[str, Any]:
        async with _Session() as s:
            report = await s.get(ComparisonReportModel, report_id)
        if not report:
            raise RuntimeError(f"对比报告 {report_id} 不存在")
        return _model_to_dict(report)

    @classmethod
    async def get_all_reports(cls) -> list[dict[str, Any]]:
        async with _Session() as s:
            rows = (await s.execute(
                select(ComparisonReportModel)
                .order_by(ComparisonReportModel.id.desc())
            )).scalars().all()
        return [_model_to_dict(r) for r in rows]

    @classmethod
    async def delete_report(cls, report_id: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            report = await s.get(ComparisonReportModel, report_id)
            if not report:
                raise RuntimeError(f"对比报告 {report_id} 不存在")
            result = _model_to_dict(report)
            await s.delete(report)
        return result


# ══════════════════════════════════════════════════════════════
#  LabelCollection
# ══════════════════════════════════════════════════════════════

class LabelCollection:
    """任务时间轴区间标签的 CRUD。"""

    @classmethod
    async def create_label(
        cls,
        task_id: int,
        name: str,
        start_ts: float,
        end_ts: float,
        color: str = "#3b6ef0",
        note: str = "",
    ) -> dict[str, Any]:
        if start_ts >= end_ts:
            raise ValueError("start_ts 必须小于 end_ts")
        async with _Session() as s, s.begin():
            label = LabelModel(
                task_id=task_id,
                name=name,
                start_ts=start_ts,
                end_ts=end_ts,
                color=color,
                note=note,
                create_time=_now(),
            )
            s.add(label)
            await s.flush()
            result = _model_to_dict(label)
        return result

    @classmethod
    async def get_labels_by_task(cls, task_id: int) -> list[dict[str, Any]]:
        async with _Session() as s:
            rows = (await s.execute(
                select(LabelModel)
                .where(LabelModel.task_id == task_id)
                .order_by(LabelModel.start_ts)
            )).scalars().all()
        return [_model_to_dict(r) for r in rows]

    @classmethod
    async def get_all_labels(cls) -> list[dict[str, Any]]:
        """返回所有任务的标签，按 task_id + start_ts 排序。"""
        async with _Session() as s:
            rows = (await s.execute(
                select(LabelModel)
                .order_by(LabelModel.task_id, LabelModel.start_ts)
            )).scalars().all()
        return [_model_to_dict(r) for r in rows]

    @classmethod
    async def get_label(cls, label_id: int) -> dict[str, Any]:
        async with _Session() as s:
            label = await s.get(LabelModel, label_id)
        if not label:
            raise RuntimeError(f"标签 {label_id} 不存在")
        return _model_to_dict(label)

    @classmethod
    async def update_label(
        cls,
        label_id: int,
        name: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
        color: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if name     is not None: fields["name"]     = name
        if start_ts is not None: fields["start_ts"] = start_ts
        if end_ts   is not None: fields["end_ts"]   = end_ts
        if color    is not None: fields["color"]    = color
        if note     is not None: fields["note"]     = note
        if not fields:
            return await cls.get_label(label_id)
        async with _Session() as s, s.begin():
            label = await s.get(LabelModel, label_id)
            if not label:
                raise RuntimeError(f"标签 {label_id} 不存在")
            new_start = fields.get("start_ts", label.start_ts)
            new_end = fields.get("end_ts", label.end_ts)
            if new_start >= new_end:
                raise ValueError("start_ts 必须小于 end_ts")
            for key, value in fields.items():
                setattr(label, key, value)
        return _model_to_dict(label)

    @classmethod
    async def delete_label(cls, label_id: int) -> dict[str, Any]:
        async with _Session() as s, s.begin():
            label = await s.get(LabelModel, label_id)
            if not label:
                raise RuntimeError(f"标签 {label_id} 不存在")
            result = _model_to_dict(label)
            await s.delete(label)
        return result
