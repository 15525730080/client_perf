# coding: utf-8
"""路由层共享响应和参数工具。"""

from fastapi.responses import JSONResponse


def ok(data=None):
    return JSONResponse({"code": 200, "msg": data})


def err(msg: str, code: int = 500):
    return JSONResponse({"code": code, "msg": msg})


def parse_ids(raw_ids: str) -> list[int]:
    return [int(value.strip()) for value in raw_ids.split(",") if value.strip()]
