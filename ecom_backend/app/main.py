# -*- coding: utf-8 -*-
"""FastAPI 入口。

启动顺序：日志 -> 迁移 -> 演示数据 -> 路由。
不再在 import 阶段建表/写库，避免测试与多进程启动时的副作用。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import admin, chat, dify_apis
from app.config import settings
from app.database import engine
from app.migrations import run_migrations
from app.observability import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    configure_logging,
    register_error_handlers,
)
from app.seed import seed_mock_orders

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    logger.info("启动 %s v%s（env=%s）", settings.app_name, settings.version, settings.environment)
    if not settings.admin_token or not settings.service_token:
        logger.warning(
            "鉴权令牌未完整配置：管理端=%s，业务接口=%s；未配置的接口将返回 503",
            "已配置" if settings.admin_token else "缺失",
            "已配置" if settings.service_token else "缺失",
        )
    run_migrations()
    seed_mock_orders()
    yield
    logger.info("服务停止")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan,
    description="订单售后智能处理：Dify 负责理解，后端负责数据与流转。",
)

configure_logging()

# 跨域：白名单来自 CORS_ORIGINS，默认只放行本地页面
_allow_all = "*" in settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Token", "X-Service-Token", "X-Operator", "X-Request-ID"],
)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(RequestContextMiddleware)

register_error_handlers(app)

app.include_router(dify_apis.router)
app.include_router(chat.router)
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/", tags=["运维"])
async def root():
    return {
        "name": settings.app_name,
        "version": settings.version,
        "docs": "/docs",
        "pages": {"chat": "/static/chat.html", "admin": "/static/admin.html"},
        "tips": "业务接口需要 X-Service-Token，管理接口需要 X-Admin-Token",
    }


@app.get("/healthz", tags=["运维"])
async def healthz():
    """健康检查：数据库可用性 + 版本，供容器/网关探活。"""
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 健康检查需要吞异常并上报
        logger.error("健康检查失败: %s", exc)
        db_status = "error"
    return {"status": "ok" if db_status == "ok" else "degraded", "db": db_status, "version": settings.version}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
