# -*- coding: utf-8 -*-
"""数据模型：订单 / 对话记录 / 售后工单 / 工单操作审计。"""
from __future__ import annotations

import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


def _now() -> datetime.datetime:
    return datetime.datetime.now()


class TOrder(Base):
    __tablename__ = "t_order"

    order_no = Column(String(50), primary_key=True, index=True)
    phone = Column(String(20), index=True)
    goods_name = Column(String(200))
    pay_status = Column(String(20))
    delivery_status = Column(String(20))
    logistics_no = Column(String(50))
    after_status = Column(String(50))
    refund_status = Column(String(50))
    # 实付金额，退款金额据此计算，不再硬编码
    paid_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    create_time = Column(DateTime, default=_now, index=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class TChatRecord(Base):
    __tablename__ = "t_chat_record"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_text = Column(Text, nullable=False)
    intent = Column(String(50), index=True)
    order_no = Column(String(50), nullable=True, index=True)
    ai_reply = Column(Text)
    is_manual = Column(Integer, default=0)
    ticket_id = Column(String(50), nullable=True)
    conversation_id = Column(String(100), nullable=True)
    # 前端会话标识，用于多用户/多会话隔离
    session_id = Column(String(64), nullable=True)
    # 前端生成的单条消息 ID，用于接口幂等（双击/重试不重复落库）
    client_msg_id = Column(String(64), nullable=True)
    create_time = Column(DateTime, default=_now, index=True)

    __table_args__ = (
        UniqueConstraint("session_id", "client_msg_id", name="uq_chat_session_msg"),
        Index("ix_chat_session_created", "session_id", "create_time"),
    )


class TServiceTicket(Base):
    __tablename__ = "t_service_ticket"

    ticket_id = Column(String(50), primary_key=True, index=True)
    order_no = Column(String(50), nullable=True, index=True)
    user_text = Column(Text)
    risk_type = Column(String(50), index=True)
    handle_status = Column(String(20), default="待处理", index=True)
    # 幂等键：(订单|会话) + 风控类型 + 状态 的摘要，唯一约束防重复建单
    dedupe_key = Column(String(64), nullable=True, unique=True, index=True)
    # Dify 原始 risk_reason，保留追溯
    risk_reason_raw = Column(String(200), nullable=True)
    source = Column(String(20), default="local")
    session_id = Column(String(64), nullable=True)
    assignee = Column(String(50), nullable=True)
    create_time = Column(DateTime, default=_now, index=True)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (Index("ix_ticket_status_created", "handle_status", "create_time"),)


class TServiceTicketEvent(Base):
    """工单操作审计：谁在什么时候把状态从 A 改到 B。"""

    __tablename__ = "t_service_ticket_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String(50), index=True, nullable=False)
    action = Column(String(30), nullable=False)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=True)
    operator = Column(String(50), nullable=True)
    note = Column(String(200), nullable=True)
    create_time = Column(DateTime, default=_now, index=True)
