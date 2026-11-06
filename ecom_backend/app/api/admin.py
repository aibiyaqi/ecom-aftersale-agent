# -*- coding: utf-8 -*-
"""后台管理接口：对话记录、工单、订单详情。

全部要求 X-Admin-Token。工单状态变更走状态机 + 审计，不再接受任意字符串。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.constants import TICKET_STATUSES
from app.database import get_db
from app.models import TChatRecord, TServiceTicket, TServiceTicketEvent
from app.schemas import TicketAssignRequest, TicketClaimRequest, TicketStatusUpdate
from app.security import require_admin
from app.services import orders as order_service
from app.services.tickets import (
    TicketClosedError,
    TicketConflictError,
    assign_ticket,
    change_status,
    claim_ticket,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["后台管理"],
    dependencies=[Depends(require_admin)],
)


def _paginate(query, page: int, size: int):
    total = query.count()
    rows = query.offset((page - 1) * size).limit(size).all()
    pages = (total + size - 1) // size if size else 0
    return total, rows, pages


def _get_ticket_or_404(db: Session, ticket_id: str) -> TServiceTicket:
    ticket = (
        db.query(TServiceTicket)
        .filter(TServiceTicket.ticket_id == ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


@router.get("/chat_list")
async def get_chat_list(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    intent: Optional[str] = None,
    is_manual: Optional[int] = Query(None, ge=0, le=1),
    session_id: Optional[str] = None,
    order_no: Optional[str] = None,
    keyword: Optional[str] = Query(None, max_length=50),
    db: Session = Depends(get_db),
):
    query = db.query(TChatRecord)
    if intent:
        query = query.filter(TChatRecord.intent == intent)
    if is_manual is not None:
        query = query.filter(TChatRecord.is_manual == is_manual)
    if session_id:
        query = query.filter(TChatRecord.session_id == session_id)
    if order_no:
        query = query.filter(TChatRecord.order_no == order_no)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(TChatRecord.user_text.like(like), TChatRecord.ai_reply.like(like)))

    total, records, pages = _paginate(query.order_by(TChatRecord.id.desc()), page, size)
    return {
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
        "list": [
            {
                "id": r.id,
                "user_text": r.user_text,
                "intent": r.intent,
                "order_no": r.order_no,
                "ai_reply": r.ai_reply,
                "is_manual": r.is_manual,
                "ticket_id": r.ticket_id,
                "session_id": r.session_id,
                "create_time": r.create_time.strftime("%Y-%m-%d %H:%M:%S") if r.create_time else "",
            }
            for r in records
        ],
    }


@router.get("/ticket_list")
async def get_ticket_list(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    order_no: Optional[str] = None,
    assignee: Optional[str] = Query(None, max_length=50),
    keyword: Optional[str] = Query(None, max_length=50),
    db: Session = Depends(get_db),
):
    query = db.query(TServiceTicket)
    if status:
        if status not in TICKET_STATUSES:
            raise HTTPException(status_code=400, detail=f"非法状态，可选值：{'/'.join(TICKET_STATUSES)}")
        query = query.filter(TServiceTicket.handle_status == status)
    if order_no:
        query = query.filter(TServiceTicket.order_no == order_no)
    if assignee:
        query = query.filter(TServiceTicket.assignee == assignee)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(TServiceTicket.user_text.like(like), TServiceTicket.ticket_id.like(like))
        )

    total, tickets, pages = _paginate(
        query.order_by(TServiceTicket.create_time.desc()), page, size
    )
    return {
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
        "list": [
            {
                "ticket_id": t.ticket_id,
                "order_no": t.order_no,
                "user_text": t.user_text,
                "risk_type": t.risk_type,
                "handle_status": t.handle_status,
                "assignee": t.assignee,
                "source": t.source,
                "create_time": t.create_time.strftime("%Y-%m-%d %H:%M:%S") if t.create_time else "",
                "updated_at": t.updated_at.strftime("%Y-%m-%d %H:%M:%S") if t.updated_at else "",
            }
            for t in tickets
        ],
    }


@router.put("/ticket_status")
async def update_ticket_status(
    payload: TicketStatusUpdate,
    operator: Optional[str] = Header(None, alias="X-Operator"),
    db: Session = Depends(get_db),
):
    ticket = (
        db.query(TServiceTicket)
        .filter(TServiceTicket.ticket_id == payload.ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")

    try:
        change_status(
            db,
            ticket,
            payload.status,
            operator=(operator or "admin")[:50],
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    logger.info(
        "工单状态更新 %s -> %s by %s", ticket.ticket_id, payload.status, operator or "admin"
    )
    return {
        "code": 200,
        "msg": "更新成功",
        "ticket_id": ticket.ticket_id,
        "handle_status": ticket.handle_status,
    }


@router.post("/ticket_claim")
async def claim_ticket_endpoint(
    payload: TicketClaimRequest,
    operator: Optional[str] = Header(None, alias="X-Operator"),
    db: Session = Depends(get_db),
):
    """坐席认领：待处理 -> 处理中，并记录负责人。"""
    ticket = _get_ticket_or_404(db, payload.ticket_id)
    operator = (operator or "admin")[:50]
    try:
        claim_ticket(db, ticket, operator)
    except TicketClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TicketConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return {
        "code": 200,
        "msg": "认领成功",
        "ticket_id": ticket.ticket_id,
        "assignee": ticket.assignee,
        "handle_status": ticket.handle_status,
    }


@router.post("/ticket_assign")
async def assign_ticket_endpoint(
    payload: TicketAssignRequest,
    operator: Optional[str] = Header(None, alias="X-Operator"),
    db: Session = Depends(get_db),
):
    """管理员指派工单给指定坐席。"""
    ticket = _get_ticket_or_404(db, payload.ticket_id)
    operator = (operator or "admin")[:50]
    try:
        assign_ticket(db, ticket, payload.assignee, operator)
    except TicketClosedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    return {
        "code": 200,
        "msg": "指派成功",
        "ticket_id": ticket.ticket_id,
        "assignee": ticket.assignee,
        "handle_status": ticket.handle_status,
    }


@router.get("/ticket_events")
async def get_ticket_events(
    ticket_id: str,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """工单操作审计：谁在什么时候把状态改成了什么。"""
    query = db.query(TServiceTicketEvent).filter(TServiceTicketEvent.ticket_id == ticket_id)
    total, events, pages = _paginate(query.order_by(TServiceTicketEvent.id.desc()), page, size)
    return {
        "total": total,
        "page": page,
        "size": size,
        "pages": pages,
        "list": [
            {
                "action": e.action,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "operator": e.operator,
                "note": e.note,
                "create_time": e.create_time.strftime("%Y-%m-%d %H:%M:%S") if e.create_time else "",
            }
            for e in events
        ],
    }


@router.get("/order_detail")
async def get_order_detail(
    order_no: Optional[str] = None,
    phone: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if not order_no and not phone:
        raise HTTPException(status_code=422, detail="order_no 与 phone 至少提供一个")
    order = order_service.find_order(db, order_no, phone)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {"code": 200, "data": order_service.order_detail(order)}
