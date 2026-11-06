# -*- coding: utf-8 -*-
"""业务服务层：工单幂等、状态流转、审计。

把"建单"收敛到一个入口，避免 /api/create_ticket、本地兜底、自动转人工
三处各写一份去重逻辑而彼此不一致。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    RISK_COMPLAINT,
    RISK_MANUAL,
    RISK_OTHER,
    RISK_REFUND,
    RISK_SYSTEM_FAILURE,
    TICKET_STATUS_PENDING,
    TICKET_STATUSES,
)
from app.models import TServiceTicket, TServiceTicketEvent

logger = logging.getLogger(__name__)


def normalize_risk_type(raw: str | None, fallback: str = RISK_OTHER) -> str:
    """把 Dify 传来的自由文本 risk_reason 归一化成受控枚举，保证幂等键稳定。"""
    text = (raw or "").strip()
    if not text:
        return fallback
    if any(kw in text for kw in ("投诉", "举报", "差评")):
        return RISK_COMPLAINT
    # 先判人工，避免"售后专员/售后工单"被误归到退款
    if any(kw in text for kw in ("人工", "客服", "坐席")):
        return RISK_MANUAL
    if any(kw in text for kw in ("退货", "退款", "退换")):
        return RISK_REFUND
    if any(kw in text for kw in ("系统", "异常", "失败")):
        return RISK_SYSTEM_FAILURE
    return fallback


def ticket_dedupe_key(
    order_no: str | None,
    session_id: str | None,
    risk_type: str | None,
    handle_status: str,
) -> str:
    """幂等键：(订单号 或 会话) + 风控类型 + 当前状态。

    无订单号时用会话维度，避免"系统故障刷单"把全局工单撑爆；
    状态变化后键随之变化，已完成的工单不会阻止下一次真实售后。
    """
    scope = (order_no or "").strip() or f"session:{(session_id or 'anonymous').strip()}"
    raw = f"{scope}|{(risk_type or '').strip()}|{(handle_status or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def new_ticket_id() -> str:
    return f"TK{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:4].upper()}"


def find_ticket_by_key(db: Session, dedupe_key: str) -> TServiceTicket | None:
    return (
        db.query(TServiceTicket)
        .filter(TServiceTicket.dedupe_key == dedupe_key)
        .first()
    )


def get_or_create_ticket(
    db: Session,
    *,
    order_no: str | None,
    risk_type: str,
    user_text: str,
    session_id: str | None = None,
    source: str = "local",
    risk_reason_raw: str | None = None,
    handle_status: str = TICKET_STATUS_PENDING,
    dedupe_scope: str | None = None,
) -> tuple[TServiceTicket, bool]:
    """返回 (工单, 是否新建)。并发下靠唯一索引兜底，不会产生重复单。

    dedupe_scope：只参与幂等键、不落库的额外作用域（例如无订单号时按诉求内容去重）。
    """
    key = ticket_dedupe_key(
        order_no, dedupe_scope or session_id, risk_type, handle_status
    )
    existing = find_ticket_by_key(db, key)
    if existing:
        logger.info("复用已有工单 %s（key=%s）", existing.ticket_id, key)
        return existing, False

    ticket = TServiceTicket(
        ticket_id=new_ticket_id(),
        order_no=order_no,
        user_text=user_text,
        risk_type=risk_type,
        handle_status=handle_status,
        dedupe_key=key,
        risk_reason_raw=(risk_reason_raw or "")[:200] or None,
        source=source,
        session_id=session_id,
    )
    try:
        with db.begin_nested():
            db.add(ticket)
            db.flush()
    except IntegrityError:
        # 并发竞态：另一个请求先插入了同一个幂等键
        logger.info("工单并发冲突，回查已有工单（key=%s）", key)
        existing = find_ticket_by_key(db, key)
        if existing is None:  # pragma: no cover - 理论上不会发生
            raise
        return existing, False

    logger.info("新建工单 %s（order_no=%s, risk=%s, source=%s）", ticket.ticket_id, order_no, risk_type, source)
    return ticket, True


class TicketConflictError(RuntimeError):
    """工单已被他人认领/状态冲突。"""


class TicketClosedError(RuntimeError):
    """工单已结束（已完成/已关闭），不允许再流转。"""


def _add_event(db: Session, ticket: TServiceTicket, event: TServiceTicketEvent) -> None:
    """写审计；若状态键与历史数据冲突，退化为工单维度键后重试。"""
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        logger.warning("工单 %s 状态键冲突，改用工单维度键", ticket.ticket_id)
        ticket.dedupe_key = f"{(ticket.dedupe_key or '')[:30]}:{ticket.ticket_id[-8:]}"
        db.add(event)
        db.flush()


def change_status(
    db: Session,
    ticket: TServiceTicket,
    new_status: str,
    *,
    operator: str | None = None,
    note: str | None = None,
) -> TServiceTicketEvent | None:
    """状态流转：校验枚举、重算幂等键、写审计。状态未变化时返回 None。"""
    if new_status not in TICKET_STATUSES:
        raise ValueError(f"非法工单状态: {new_status}")
    old_status = ticket.handle_status
    if old_status == new_status:
        return None

    ticket.handle_status = new_status
    # 键里含状态：状态变更后需要同步，否则"已完成"的旧键会挡住新单或留下脏键
    ticket.dedupe_key = ticket_dedupe_key(
        ticket.order_no, ticket.session_id, ticket.risk_type, new_status
    )
    event = TServiceTicketEvent(
        ticket_id=ticket.ticket_id,
        action="status_change",
        from_status=old_status,
        to_status=new_status,
        operator=operator,
        note=note,
    )
    _add_event(db, ticket, event)
    return event


def claim_ticket(
    db: Session, ticket: TServiceTicket, operator: str
) -> TServiceTicket:
    """坐席认领：记录负责人，待处理自动进入处理中，并写审计。"""
    operator = (operator or "").strip()
    if not operator:
        raise ValueError("缺少操作人")
    if ticket.handle_status in {"已完成", "已关闭"}:
        raise TicketClosedError("工单已结束，无法认领")
    if ticket.assignee and ticket.assignee != operator:
        raise TicketConflictError(f"工单已被 {ticket.assignee} 认领")

    old_status = ticket.handle_status
    ticket.assignee = operator
    if old_status == "待处理":
        ticket.handle_status = "处理中"
        ticket.dedupe_key = ticket_dedupe_key(
            ticket.order_no, ticket.session_id, ticket.risk_type, ticket.handle_status
        )
    event = TServiceTicketEvent(
        ticket_id=ticket.ticket_id,
        action="claim",
        from_status=old_status,
        to_status=ticket.handle_status,
        operator=operator,
    )
    _add_event(db, ticket, event)
    logger.info("工单 %s 被 %s 认领", ticket.ticket_id, operator)
    return ticket


def assign_ticket(
    db: Session,
    ticket: TServiceTicket,
    assignee: str,
    operator: str | None = None,
) -> TServiceTicket:
    """指派：管理员把工单交给指定坐席，待处理自动进入处理中。"""
    assignee = (assignee or "").strip()
    if not assignee:
        raise ValueError("缺少被指派人")
    if ticket.handle_status in {"已完成", "已关闭"}:
        raise TicketClosedError("工单已结束，无法指派")

    old_status = ticket.handle_status
    ticket.assignee = assignee
    if old_status == "待处理":
        ticket.handle_status = "处理中"
        ticket.dedupe_key = ticket_dedupe_key(
            ticket.order_no, ticket.session_id, ticket.risk_type, ticket.handle_status
        )
    event = TServiceTicketEvent(
        ticket_id=ticket.ticket_id,
        action="assign",
        from_status=old_status,
        to_status=ticket.handle_status,
        operator=operator,
        note=f"指派给 {assignee}",
    )
    _add_event(db, ticket, event)
    logger.info("工单 %s 指派给 %s（操作人 %s）", ticket.ticket_id, assignee, operator)
    return ticket

