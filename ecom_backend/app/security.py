# -*- coding: utf-8 -*-
"""接口鉴权。

两套凭据，互不复用：
- ADMIN_TOKEN：管理后台（对话记录 / 工单 / 订单详情），通过 X-Admin-Token 或 Bearer 传递
- SERVICE_TOKEN：Dify 回调的 4 个业务接口，通过 X-Service-Token 或 Bearer 传递

未配置 Token 时默认拒绝（503），只有显式设置 ALLOW_INSECURE_NO_TOKEN=true 才放行，
避免"忘了配就裸奔"。
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


def _extract_token(request: Request, header_name: str) -> Optional[str]:
    token = request.headers.get(header_name)
    if token:
        return token.strip()
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _verify(request: Request, header_name: str, expected: str, scope: str) -> None:
    if not expected:
        if settings.allow_insecure_no_token:
            logger.warning("%s 鉴权未配置 Token，当前按 ALLOW_INSECURE_NO_TOKEN 放行", scope)
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{scope}未配置访问令牌，服务已拒绝访问，请配置 {header_name.replace('X-', '').replace('-', '_').upper()}",
        )

    provided = _extract_token(request, header_name)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{scope}未授权：缺少或错误的 {header_name}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(request: Request) -> None:
    """管理后台接口依赖。"""
    _verify(request, "X-Admin-Token", settings.admin_token, "管理后台")


def require_service(request: Request) -> None:
    """Dify 回调业务接口依赖。"""
    _verify(request, "X-Service-Token", settings.service_token, "业务接口")
