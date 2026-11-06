# -*- coding: utf-8 -*-
"""前端对话接口：Dify 编排 + 本地意图兜底 + 单事务持久化。

关键约束（对应上一轮报告里的 P0）：
- 一个请求只开一个事务，工单与对话记录要么都写成功，要么都回滚
- 同步 SQLAlchemy 调用放到线程池，避免阻塞事件循环
- 建单统一走 services.tickets，带幂等键，重复/并发不会刷单
- client_msg_id 幂等：双击或网络重试不会重复落库、重复调 Dify
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.constants import (
    INTENT_COMPLAINT,
    INTENT_LOGISTICS,
    INTENT_MANUAL,
    INTENT_ORDER,
    INTENT_REFUND,
    INTENT_UNKNOWN,
    RISK_COMPLAINT,
    RISK_MANUAL,
    RISK_REFUND,
    RISK_SYSTEM_FAILURE,
    SOURCE_DIFY,
    SOURCE_LOCAL,
)
from app.database import SessionLocal
from app.dify_client import FALLBACK_REPLY, run_dify_workflow
from app.models import TChatRecord, TServiceTicket
from app.ratelimit import rate_limit_send_msg
from app.schemas import SendMsgRequest
from app.services import orders as order_service
from app.services.tickets import get_or_create_ticket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["前端对话"])

# ========== 本地意图规则（Dify 未命中/不可用时的兜底） ==========
# 顺序有意义：先判投诉/退款（强诉求），再判物流，最后才是泛化的"订单"，
# 避免"订单退款"被误判成查订单、"未发货"被当成物流意图。
INTENT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("投诉", "举报", "差评"), INTENT_COMPLAINT),
    (("退货", "退款", "退换", "不想要了", "退掉"), INTENT_REFUND),
    (("物流", "快递", "发货", "到哪了", "运送", "配送", "签收"), INTENT_LOGISTICS),
    (("查订单", "查询订单", "订单详情", "看看订单", "我的订单", "订单状态", "订单号", "订单"),
     INTENT_ORDER),
]

# 否定的售后诉求，例如"不要退款了""不用退货"
NEGATION_PATTERNS = ("不要退", "不用退", "不退了", "取消退", "别退", "无需退")

# 用户明确要求转人工
USER_MANUAL_KEYWORDS = (
    "转人工",
    "人工客服",
    "转接人工",
    "转接客服",
    "找人工",
    "找客服",
    "我要人工",
    "确认转人工",
    "真人客服",
)

MANUAL_REPLY = "已为您转接人工客服，正在为您分配客服人员，请稍候。\n📋 工单已生成，客服将尽快联系您处理。"
ASK_REPHRASE_REPLY = (
    "抱歉，我没有完全理解您的问题。您可以补充订单号（例如 OD2026001）再描述一次，"
    "或直接回复「转人工」，由人工客服为您处理。"
)

_ORDER_NO_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4}\d{6,})(?![0-9])")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")


# --------------------------------------------------------------------------
# 纯函数：文本理解
# --------------------------------------------------------------------------
def extract_order_no(text: str) -> str | None:
    match = _ORDER_NO_RE.search((text or "").upper())
    return match.group(1).upper() if match else None


def extract_phone(text: str) -> str | None:
    match = _PHONE_RE.search(text or "")
    return match.group(1) if match else None


def detect_manual_intent(text: str) -> bool:
    content = text or ""
    return any(keyword in content for keyword in USER_MANUAL_KEYWORDS)


def local_intent_classifier(text: str) -> str | None:
    """关键词匹配意图；带否定词时直接返回 None，避免"不要退款了"被建退款单。"""
    content = (text or "").lower()
    if any(pattern in content for pattern in NEGATION_PATTERNS):
        return None
    for keywords, intent_name in INTENT_RULES:
        if any(keyword in content for keyword in keywords):
            return intent_name
    return None


# --------------------------------------------------------------------------
# 读操作（线程池执行）
# --------------------------------------------------------------------------
def _find_cached_turn(session_id: str, client_msg_id: str) -> dict | None:
    db = SessionLocal()
    try:
        record = (
            db.query(TChatRecord)
            .filter(
                TChatRecord.session_id == session_id,
                TChatRecord.client_msg_id == client_msg_id,
            )
            .first()
        )
        return _record_to_response(record, session_id) if record else None
    finally:
        db.close()


def _load_last_conversation(session_id: str | None) -> str | None:
    db = SessionLocal()
    try:
        query = db.query(TChatRecord).filter(TChatRecord.conversation_id.isnot(None))
        if session_id:
            query = query.filter(TChatRecord.session_id == session_id)
        record = query.order_by(TChatRecord.id.desc()).first()
        return record.conversation_id if record else None
    finally:
        db.close()


def _last_order_no(db: Session, session_id: str | None) -> str | None:
    query = db.query(TChatRecord).filter(
        TChatRecord.order_no.isnot(None), TChatRecord.order_no != ""
    )
    if session_id:
        query = query.filter(TChatRecord.session_id == session_id)
    record = query.order_by(TChatRecord.id.desc()).first()
    return record.order_no if record else None


# --------------------------------------------------------------------------
# 意图决策（纯函数，方便单测）
# --------------------------------------------------------------------------
def build_turn_plan(
    *,
    dify_result: dict[str, Any],
    user_text: str,
    user_wants_manual: bool,
    local_intent: str | None,
) -> dict[str, Any]:
    reply = (dify_result.get("reply") or "").strip() or None
    intent = dify_result.get("intent")
    order_no = dify_result.get("order_no")
    ticket_id = dify_result.get("ticket_id")
    is_manual = bool(dify_result.get("is_manual"))
    dify_ok = bool(dify_result.get("ok"))

    plan: dict[str, Any] = {
        "reply": reply,
        "intent": intent,
        "order_no": order_no,
        "ticket_id": ticket_id,
        "is_manual": is_manual,
        "need_local": False,
        "local_intent": None,
        "conversation_id": dify_result.get("conversation_id"),
        "user_wants_manual": user_wants_manual,
        "dify_error": dify_result.get("error"),
    }

    # 1) 用户明确要求转人工：直接走人工，不再让 Dify 决定
    if user_wants_manual:
        plan.update(reply=MANUAL_REPLY, intent=INTENT_MANUAL, is_manual=True)
        return plan

    # 2) Dify 不可用 / 空回复：优先本地兜底
    if not dify_ok or not reply:
        if local_intent:
            plan.update(need_local=True, local_intent=local_intent, is_manual=False)
            return plan
        if not dify_ok:
            # 本地也接不住 → 自动转人工（由持久化层按会话去重建单）
            plan.update(reply=FALLBACK_REPLY, is_manual=True, intent=intent or INTENT_UNKNOWN)
            return plan
        plan.update(reply=ASK_REPHRASE_REPLY, is_manual=False, intent=intent or INTENT_UNKNOWN)
        return plan

    # 3) Dify 正常回复：补全意图（原实现这里的分支不可达）
    if not intent:
        plan["intent"] = local_intent or INTENT_UNKNOWN
        logger.info("Dify 未回传意图，本地规则补充: %s", plan["intent"])

    return plan


# --------------------------------------------------------------------------
# 本地意图处理（需要 DB，放在持久化事务内执行）
# --------------------------------------------------------------------------
def _handle_local_intent(
    db: Session,
    intent: str,
    user_text: str,
    session_id: str | None,
    order_no: str | None,
    phone: str | None,
) -> dict[str, Any]:
    order_no = order_no or extract_order_no(user_text)
    phone = phone or extract_phone(user_text)

    if intent == INTENT_ORDER:
        order = order_service.find_order(db, order_no, phone)
        if not order:
            hint = order_no or phone or ""
            return {"reply": f"未找到{hint}对应的订单，请核对订单号/手机号后重试。", "order_no": order_no, "ticket_id": None}
        return {"reply": order_service.order_summary_text(order), "order_no": order.order_no, "ticket_id": None}

    if intent == INTENT_LOGISTICS:
        order = order_service.find_order(db, order_no, phone)
        if not order:
            return {"reply": "请提供订单号，例如：查物流 OD2026001", "order_no": order_no, "ticket_id": None}
        info = order_service.logistics_info(order)
        if not info["logistics_no"]:
            return {
                "reply": f"订单 {order.order_no}（{order.goods_name}）{info['status']}，暂无物流单号。",
                "order_no": order.order_no,
                "ticket_id": None,
            }
        return {
            "reply": (
                f"订单 {order.order_no}（{order.goods_name}）物流信息：\n"
                f"📌 物流公司：{info['carrier']}\n"
                f"📌 物流单号：{info['logistics_no']}\n"
                f"📌 当前状态：{info['status']}"
            ),
            "order_no": order.order_no,
            "ticket_id": None,
        }

    if intent == INTENT_REFUND:
        order = order_service.find_order(db, order_no, phone)
        if not order:
            return {"reply": "请提供要退货的订单号，例如：退货 OD2026001", "order_no": order_no, "ticket_id": None}
        if order.refund_status in {"退款中", "退款成功", "已退款"}:
            return {
                "reply": f"订单 {order.order_no}（{order.goods_name}）当前退款状态：{order.refund_status}，请耐心等待。",
                "order_no": order.order_no,
                "ticket_id": None,
            }
        ticket, created = get_or_create_ticket(
            db,
            order_no=order.order_no,
            risk_type=RISK_REFUND,
            user_text=user_text,
            session_id=session_id,
            source=SOURCE_LOCAL,
        )
        action = "已为您提交" if created else "已存在处理中的申请"
        return {
            "reply": (
                f"{action}订单 {order.order_no}（{order.goods_name}）的退货退款申请。\n"
                f"📋 工单编号：{ticket.ticket_id}\n客服将在 24 小时内联系您处理。"
            ),
            "order_no": order.order_no,
            "ticket_id": ticket.ticket_id,
        }

    if intent == INTENT_COMPLAINT:
        order = order_service.find_order(db, order_no, phone)
        order_no = order.order_no if order else order_no
        ticket, created = get_or_create_ticket(
            db,
            order_no=order_no,
            risk_type=RISK_COMPLAINT,
            user_text=user_text,
            session_id=session_id,
            source=SOURCE_LOCAL,
        )
        action = "已收到" if created else "已记录（处理中）"
        return {
            "reply": f"{action}您的投诉反馈。\n📋 投诉编号：{ticket.ticket_id}\n我们会尽快核实处理。",
            "order_no": order_no,
            "ticket_id": ticket.ticket_id,
        }

    return {"reply": None, "order_no": order_no, "ticket_id": None}


# --------------------------------------------------------------------------
# 写操作：一个事务内完成建单 + 落记录
# --------------------------------------------------------------------------
def _record_to_response(record: TChatRecord, session_id: str | None) -> dict[str, Any]:
    return {
        "reply": record.ai_reply,
        "is_manual": bool(record.is_manual),
        "ticket_id": record.ticket_id,
        "intent": record.intent,
        "order_no": record.order_no,
        "session_id": session_id,
        "duplicated": True,
    }


def _commit_turn(
    plan: dict[str, Any],
    *,
    user_text: str,
    session_id: str | None,
    client_msg_id: str | None,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        reply = plan["reply"]
        intent = plan["intent"]
        order_no = plan["order_no"]
        ticket_id = plan["ticket_id"]
        is_manual = plan["is_manual"]

        # 先解析出订单号，本地兜底才能复用会话里的上一单
        if not order_no:
            order_no = extract_order_no(user_text) or _last_order_no(db, session_id)

        if plan["need_local"]:
            local_result = _handle_local_intent(
                db,
                plan["local_intent"],
                user_text,
                session_id,
                order_no,
                extract_phone(user_text),
            )
            if local_result.get("reply"):
                reply = local_result["reply"]
                order_no = local_result.get("order_no") or order_no
                intent = plan["local_intent"]
                ticket_id = local_result.get("ticket_id") or None
                is_manual = False

        if not order_no:
            order_no = extract_order_no(user_text) or _last_order_no(db, session_id)

        # 兜底：任何路径都不允许把 null 回复发给前端
        if not reply:
            reply = ASK_REPHRASE_REPLY

        # Dify 返回的 ticket_id 必须真实存在，否则视为无效（避免记录悬空工单号）
        if ticket_id:
            exists = (
                db.query(TServiceTicket)
                .filter(TServiceTicket.ticket_id == ticket_id)
                .first()
            )
            if not exists:
                logger.warning("Dify 返回的工单号不存在，忽略: %s", ticket_id)
                ticket_id = None

        if is_manual and not ticket_id:
            risk_type = RISK_MANUAL if plan["user_wants_manual"] else RISK_SYSTEM_FAILURE
            source = SOURCE_DIFY if plan["dify_error"] is None and not plan["user_wants_manual"] else SOURCE_LOCAL
            ticket, _ = get_or_create_ticket(
                db,
                order_no=order_no,
                risk_type=risk_type,
                user_text=user_text,
                session_id=session_id,
                source=source,
            )
            ticket_id = ticket.ticket_id

        record = TChatRecord(
            user_text=user_text,
            intent=intent or INTENT_UNKNOWN,
            order_no=order_no,
            ai_reply=reply,
            is_manual=1 if is_manual else 0,
            ticket_id=ticket_id,
            conversation_id=plan["conversation_id"],
            session_id=session_id,
            client_msg_id=client_msg_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        logger.info(
            "对话落库 id=%s intent=%s order_no=%s is_manual=%s ticket=%s",
            record.id,
            record.intent,
            record.order_no,
            record.is_manual,
            record.ticket_id,
        )
        return {
            "reply": reply,
            "is_manual": is_manual,
            "ticket_id": ticket_id,
            "intent": record.intent,
            "order_no": order_no,
            "session_id": session_id,
            "duplicated": False,
        }

    except IntegrityError:
        # client_msg_id 并发重复：返回先落库的那条
        db.rollback()
        if session_id and client_msg_id:
            existing = (
                db.query(TChatRecord)
                .filter(
                    TChatRecord.session_id == session_id,
                    TChatRecord.client_msg_id == client_msg_id,
                )
                .first()
            )
            if existing:
                logger.info("检测到重复消息，返回已落库结果 id=%s", existing.id)
                return _record_to_response(existing, session_id)
        raise
    except Exception:
        db.rollback()
        logger.exception("对话落库失败，已回滚")
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
@router.post("/send_msg", dependencies=[Depends(rate_limit_send_msg)])
async def send_message(req: SendMsgRequest) -> dict[str, Any]:
    session_id = req.session_id
    client_msg_id = req.client_msg_id or None

    # 0) 幂等：同一会话同一消息 ID 直接返回上次结果
    if session_id and client_msg_id:
        cached = await run_in_threadpool(_find_cached_turn, session_id, client_msg_id)
        if cached:
            return cached

    # 1) 多轮上下文：取同会话上次的 conversation_id
    last_conversation_id = await run_in_threadpool(_load_last_conversation, session_id)

    user_wants_manual = detect_manual_intent(req.user_text)
    local_intent = local_intent_classifier(req.user_text)

    # 2) 用户已明确转人工，没必要再打 Dify（省一次模型调用）
    if user_wants_manual:
        plan = build_turn_plan(
            dify_result={
                "ok": True,
                "reply": None,
                "is_manual": True,
                "conversation_id": last_conversation_id,
            },
            user_text=req.user_text,
            user_wants_manual=True,
            local_intent=None,
        )
    else:
        dify_result = await run_dify_workflow(req.user_text, conversation_id=last_conversation_id)
        plan = build_turn_plan(
            dify_result=dify_result,
            user_text=req.user_text,
            user_wants_manual=False,
            local_intent=local_intent,
        )

    # 3) 单事务持久化（线程池执行，不阻塞事件循环）
    return await run_in_threadpool(
        _commit_turn,
        plan,
        user_text=req.user_text,
        session_id=session_id,
        client_msg_id=client_msg_id,
    )
