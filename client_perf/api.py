# coding: utf-8
"""client-perf FastAPI 应用入口。

这里只负责应用装配、静态资源、全局异常处理和生命周期；
具体业务路由位于 client_perf.routers，分析与导出位于 client_perf.services。
"""

import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import RedirectResponse

from client_perf import __version__
from client_perf.db import TaskCollection, create_tables
from client_perf.log import log as logger
from client_perf.paths import STATIC_DIR
from client_perf.routers import comparison, devices, exports, labels, tasks
from client_perf.routers.common import err

BASE_DIR = Path(__file__).parent / "test_result"
if not BASE_DIR.is_dir():
    BASE_DIR.mkdir(parents=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await create_tables()
    recovered = await TaskCollection.recover_interrupted_tasks()
    if recovered:
        logger.warning("已将 %s 个上次异常中断的任务标记为停止", recovered)

    try:
        yield
    finally:
        try:
            from client_perf.core.ios_tools import TunnelManager

            TunnelManager.stop_all_tunnels()
        except ImportError:
            pass


app = FastAPI(title="client-perf", version=__version__, lifespan=_lifespan)


@app.middleware("http")
async def _error_handler(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logger.error(traceback.format_exc())
        return err(str(exc))


@app.get("/")
def index():
    return RedirectResponse(url="/test_result/index.html")


app.include_router(devices.router)
app.include_router(tasks.router)
app.include_router(comparison.router)
app.include_router(exports.router)
app.include_router(labels.router)

# 同时兼容 /test_result 和旧的 /static 访问路径。
app.mount("/test_result", StaticFiles(directory=str(BASE_DIR)), name="test_result")
app.mount("/static", StaticFiles(directory=str(BASE_DIR)), name="static")
