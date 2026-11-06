# -*- coding: utf-8 -*-
"""供 Dify HTTP 节点回调的 4 个业务接口。

- 全部要求 X-Service-Token（或 Bearer），公网暴露不再等于裸奔
- 返回顺序/结构保持不变，Dify 侧无需改节点
- 物流状态、退款金额由订单数据推导，杜绝随机与硬编码
- 建单走统一的幂等服务，重复调用返回同一个 ticket_id
"""
from __future__ import annotations

import datetime
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.constants import SOURCE_DIFY
from app.database import get_db
from app.schemas import CreateTicketRequest, OrderQueryRequest
from app.security import require_service
from app.services import orders as order_service
from app.services.tickets import get_or_create_ticket, normalize_risk_type

router = APIRouter(
    prefix="/api",
    tags=["Dify业务接口"],
    dependencies=[Depends(require_service)],
)


def _load_order(db: Session, req: OrderQueryRequest):
    order = order_service.find_order(db, req.order_no, req.phone)
    if not order:
        key = req.order_no or req.phone
        raise HTTPException(status_code=404, detail=f"订单不存在: {key}")
    return order


# ---------- 1. 查询订单详情 ----------
@router.post("/get_order")
async def get_order(req: OrderQueryRequest, db: Session = Depends(get_db)):
    return order_service.order_detail(_load_order(db, req))


# ---------- 2. 查询物流进度 ----------
@router.post("/get_logistics")
async def get_logistics(req: OrderQueryRequest, db: Session = Depends(get_db)):
    return order_service.logistics_info(_load_order(db, req))


# ---------- 3. 查询退款进度 ----------
@router.post("/get_refund")
async def get_refund(req: OrderQueryRequest, db: Session = Depends(get_db)):
    return order_service.refund_info(_load_order(db, req))


# ---------- 4. 创建人工工单 ----------
@router.post("/create_ticket")
async def create_ticket(req: CreateTicketRequest, db: Session = Depends(get_db)):
    order = None
    if req.order_no:
        order = order_service.find_order(db, order_no=req.order_no)
        if not order:
            raise HTTPException(status_code=404, detail=f"订单不存在: {req.order_no}")

    # 无订单号（如纯咨询/无单投诉）时，按"会话或诉求内容+日期"做幂等：
    # 既挡住同一句话反复建单，又不会把不同日期的同类诉求永久合并。
    dedupe_scope = req.session_id or (
        "content:"
        + hashlib.sha1(req.user_text.encode("utf-8")).hexdigest()[:12]
        + ":"
        + datetime.date.today().isoformat()
    )

    risk_type = normalize_risk_type(req.risk_reason, fallback="其他售后问题")
    ticket, created = get_or_create_ticket(
        db,
        order_no=order.order_no if order else None,
        risk_type=risk_type,
        user_text=req.user_text,
        session_id=req.session_id,
        dedupe_scope=dedupe_scope,
        source=SOURCE_DIFY,
        risk_reason_raw=req.risk_reason,
    )
    db.commit()
    db.refresh(ticket)

    return {
        "ticket_id": ticket.ticket_id,
        "status": "success",
        "duplicated": not created,
        "risk_type": ticket.risk_type,
        "handle_status": ticket.handle_status,
    }
