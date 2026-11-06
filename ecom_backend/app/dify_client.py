# -*- coding: utf-8 -*-
"""Dify 调用客户端。

两种应用模式（配置 DIFY_APP_MODE 切换）：
- chat     : POST /v1/chat-messages，返回 answer 文本（当前 advanced-chat 应用）
- workflow : POST /v1/workflows/run，返回 data.outputs 结构化字段（推荐）

无论哪种模式，最终都归一化成同一个 dict：
    {ok, reply, is_manual, ticket_id, intent, order_no, conversation_id, error}
调用方只依赖这个契约，不再用"回复是否为空"猜测成败。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app import config

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "抱歉，系统暂时无法处理您的问题，已为您转接人工客服，请保持电话畅通。"

# 纯文本回复里"确认已转人工"的强信号（只在没有结构化字段时兜底）。
# 刻意收窄：像"已提交售后工单"这类正常告知不能被判成转人工，否则会重复建单。
MANUAL_PHRASES = (
    "已为您转接人工客服",
    "已转接人工客服",
    "已为您转人工",
    "正在为您转接人工",
)

# 只有这些文案才说明 404 是"订单不存在"这种业务结果
BUSINESS_NOT_FOUND_MARKERS = ("订单不存在", "未找到订单", "订单号不存在", "order not found")


def _is_business_not_found(detail: str) -> bool:
    text = (detail or "").lower()
    return any(marker.lower() in text for marker in BUSINESS_NOT_FOUND_MARKERS)


def _empty_result(*, ok: bool, error: str | None, conversation_id: str | None) -> dict:
    return {
        "ok": ok,
        "reply": None,
        "is_manual": False,
        "ticket_id": None,
        "intent": None,
        "order_no": None,
        "conversation_id": conversation_id,
        "error": error,
    }


def _parse_json_object(text: str) -> dict | None:
    """从文本中解析 JSON 对象，兼容 ```json 代码块与前后夹杂说明文字。"""
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 } 之间
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "是"}
    return bool(value)


def _clean_text(text: str) -> str:
    """清掉 Dify 模板残留的空花括号，保留正文。"""
    cleaned = re.sub(r"\{\s*\}", "", text or "")
    return cleaned.strip()


def _normalize_outputs(data: dict, conversation_id: str | None) -> dict:
    """把结构化输出归一化为标准结果。"""
    reply = data.get("reply") or data.get("answer") or data.get("text")
    intent = data.get("intent")
    order_no = data.get("order_no") or data.get("orderNo")
    ticket_id = data.get("ticket_id") or data.get("ticketId")
    is_manual = _as_bool(data.get("is_manual", data.get("isManual", False)))
    return {
        "ok": True,
        "reply": (_clean_text(str(reply)) if reply else None),
        "is_manual": is_manual,
        "ticket_id": str(ticket_id) if ticket_id else None,
        "intent": str(intent) if intent else None,
        "order_no": str(order_no) if order_no else None,
        "conversation_id": data.get("conversation_id") or conversation_id,
        "error": None,
    }


def _parse_dify_response(payload: dict, conversation_id: str | None) -> dict:
    """兼容 chat / workflow 两种返回体。"""
    if not isinstance(payload, dict):
        return _empty_result(ok=False, error="Dify 返回体不是 JSON 对象", conversation_id=conversation_id)

    # workflow: {"data": {"outputs": {...}, "status": "succeeded"}}
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("outputs"), dict):
        if data.get("status") and data["status"] != "succeeded":
            return _empty_result(
                ok=False, error=f"Dify 工作流状态异常: {data.get('status')}", conversation_id=conversation_id
            )
        return _normalize_outputs(data["outputs"], data.get("conversation_id") or conversation_id)

    # chat: {"answer": "...", "conversation_id": "..."}
    answer = payload.get("answer")
    if isinstance(answer, str):
        new_conversation_id = payload.get("conversation_id") or conversation_id
        parsed = _parse_json_object(answer)
        if parsed is not None:
            return _normalize_outputs(parsed, new_conversation_id)
        text = _clean_text(answer)
        if not text:
            return _empty_result(ok=True, error="Dify 返回空回复", conversation_id=new_conversation_id)
        return {
            "ok": True,
            "reply": text,
            "is_manual": any(phrase in text for phrase in MANUAL_PHRASES),
            "ticket_id": None,
            "intent": None,
            "order_no": None,
            "conversation_id": new_conversation_id,
            "error": None,
        }

    # 有些应用把结构化结果直接放在顶层
    if any(key in payload for key in ("reply", "intent", "is_manual", "ticket_id")):
        return _normalize_outputs(payload, conversation_id)

    return _empty_result(ok=False, error="无法识别的 Dify 返回结构", conversation_id=conversation_id)


def _build_request(user_text: str, conversation_id: str | None) -> tuple[str, dict]:
    if config.settings.dify_app_mode == "workflow":
        payload = {
            "inputs": {"user_text": user_text},
            "response_mode": "blocking",
            "user": config.settings.dify_user,
        }
    else:
        payload = {
            "inputs": {},
            "query": user_text,
            "response_mode": "blocking",
            "user": config.settings.dify_user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
    return config.settings.dify_endpoint, payload


async def run_dify_workflow(user_text: str, conversation_id: str | None = None) -> dict:
    """调用 Dify 并返回归一化结果；任何失败都返回 ok=False，由上层决定是否本地兜底。"""
    if not config.settings.dify_api_key:
        logger.error("未配置 DIFY_API_KEY，跳过 Dify 调用")
        return _empty_result(ok=False, error="未配置 DIFY_API_KEY", conversation_id=conversation_id)

    url, payload = _build_request(user_text, conversation_id)
    headers = {
        "Authorization": f"Bearer {config.settings.dify_api_key}",
        "Content-Type": "application/json",
    }
    attempts = max(0, config.settings.dify_max_retries) + 1
    last_error = "unknown"

    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=config.settings.dify_timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                result = _parse_dify_response(response.json(), conversation_id)
                logger.info(
                    "Dify 调用成功: mode=%s intent=%s order_no=%s is_manual=%s",
                    config.settings.dify_app_mode,
                    result.get("intent"),
                    result.get("order_no"),
                    result.get("is_manual"),
                )
                return result

        except httpx.TimeoutException:
            # 超时不再重试：重试只会把用户等待翻倍
            logger.error("Dify 请求超时（%.1fs）", config.settings.dify_timeout)
            return _empty_result(ok=False, error="Dify 请求超时", conversation_id=conversation_id)

        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                detail = body.get("message") or body.get("detail") or ""
            except Exception:  # noqa: BLE001 - 错误体可能不是 JSON
                detail = exc.response.text[:200]

            logger.error("Dify 返回错误 %s: %s", exc.response.status_code, detail)
            last_error = f"HTTP {exc.response.status_code}: {detail}"

            if "model credentials" in detail.lower():
                logger.error(
                    "Dify 模型凭据无效：请到 Dify 控制台 Settings → Model Provider 检查对应模型供应商的 API Key，"
                    "并确认应用里引用的模型名真实存在"
                )

            # 只有带明确业务语义的 404 才当成"订单不存在"；
            # 端点配错/应用被删也会 404，那种必须按故障处理，不能伪装成业务结果。
            if exc.response.status_code == 404 and _is_business_not_found(detail):
                return {
                    "ok": True,
                    "reply": "抱歉，没有查到相关信息，请核对订单号后重试。",
                    "is_manual": False,
                    "ticket_id": None,
                    "intent": "查订单",
                    "order_no": None,
                    "conversation_id": conversation_id,
                    "error": None,
                }
            if exc.response.status_code == 404:
                logger.error(
                    "Dify 返回 404 但不像业务结果，请检查 DIFY_API_URL / 应用是否被删除：%s", detail
                )
                return _empty_result(ok=False, error=last_error, conversation_id=conversation_id)
            # 5xx 可重试
            if exc.response.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            return _empty_result(ok=False, error=last_error, conversation_id=conversation_id)

        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_error = f"网络错误: {exc}"
            logger.warning("Dify 网络错误（第 %s/%s 次）: %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
                continue
            return _empty_result(ok=False, error=last_error, conversation_id=conversation_id)

        except Exception as exc:  # noqa: BLE001 - 兜底，避免异常穿透到接口层
            logger.exception("Dify 调用未预期异常: %s", exc)
            return _empty_result(ok=False, error=str(exc), conversation_id=conversation_id)

    return _empty_result(ok=False, error=last_error, conversation_id=conversation_id)
