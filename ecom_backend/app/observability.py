# -*- coding: utf-8 -*-
"""可观测性：请求 ID、访问日志、统一异常响应。

统一错误响应结构（4xx/5xx）：
    {"code": 400, "msg": "...", "request_id": "..."}
前端/调用方可凭 request_id 与服务端日志对齐。
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id_ctx.get()


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = _request_id_ctx.get()
        return True


def configure_logging() -> None:
    """幂等配置根日志：带 request_id，输出到 stderr（容器友好）。"""
    root = logging.getLogger()
    if getattr(root, "_ecom_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_RequestIdFilter())
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    root._ecom_configured = True  # type: ignore[attr-defined]

    # 第三方库降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """注入/透传 X-Request-ID，并记录访问日志与耗时。"""

    async def dispatch(self, request: Request, call_next: Callable):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = _request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logging.getLogger("access").info(
                "%s %s -> %s (%.1fms)",
                request.method,
                request.url.path,
                status_code,
                elapsed_ms,
            )
            _request_id_ctx.reset(token)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """限制请求体大小：字段级 max_length 是解析后才校验，这里在读取阶段就挡住。

    纯 ASGI 实现，兼容 Content-Length 缺失的分块上传（按实际读取字节计数）。
    """

    def __init__(self, app, max_bytes: int = 64 * 1024) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(send, "请求体过大")
            return

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if started:
                raise
            await self._reject(send, "请求体过大")

    async def _reject(self, send, msg: str) -> None:
        body = (
            '{"code": 413, "msg": "%s", "request_id": "%s"}'
            % (msg, current_request_id())
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _error_response(status_code: int, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "msg": msg, "request_id": current_request_id()},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, (list, dict)):
            msg = detail  # type: ignore[assignment]
        else:
            msg = str(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "msg": msg, "request_id": current_request_id()},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError):
        errors = []
        for item in exc.errors():
            loc = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            errors.append({"field": loc or "body", "message": item.get("msg", "参数不合法")})
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "msg": "参数校验失败",
                "errors": errors,
                "request_id": current_request_id(),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception):
        # 只记日志，不把堆栈/内部信息返回给调用方
        logging.getLogger("error").exception("未处理异常: %s", exc)
        return _error_response(500, "服务器内部错误，请稍后重试或联系管理员")
