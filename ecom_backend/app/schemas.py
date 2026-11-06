# -*- coding: utf-8 -*-
"""接口入参模型：统一做长度、格式、必填校验，非法输入在进入业务前被拦住。"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.config import settings

OrderNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=50)]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^1[3-9]\d{9}$")]
UserText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=settings.max_user_text_length),
]
SessionId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
ClientMsgId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]

_ORDER_NO_RE = re.compile(r"^[A-Za-z0-9_-]{4,50}$")


class OrderQueryRequest(BaseModel):
    """Dify 回调：订单号与手机号二选一。"""

    order_no: Optional[OrderNo] = None
    phone: Optional[Phone] = None

    @field_validator("order_no")
    @classmethod
    def _check_order_no(cls, value: Optional[str]) -> Optional[str]:
        if value and not _ORDER_NO_RE.match(value):
            raise ValueError("订单号格式不合法（仅允许字母、数字、下划线、短横线）")
        return value

    @model_validator(mode="after")
    def _require_one(self) -> "OrderQueryRequest":
        if not self.order_no and not self.phone:
            raise ValueError("order_no 与 phone 至少提供一个")
        return self


class CreateTicketRequest(BaseModel):
    """Dify 建单入参。

    order_no 允许缺省：投诉/咨询类诉求可能没有订单号，此时按诉求内容做幂等；
    但只要有订单号，就要求它真实存在。
    """

    order_no: Optional[OrderNo] = None
    user_text: UserText
    risk_reason: Optional[Annotated[str, StringConstraints(max_length=200)]] = None
    session_id: Optional[SessionId] = None

    @field_validator("order_no", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("order_no")
    @classmethod
    def _check_order_no(cls, value: Optional[str]) -> Optional[str]:
        if value and not _ORDER_NO_RE.match(value):
            raise ValueError("订单号格式不合法")
        return value


class SendMsgRequest(BaseModel):
    user_text: UserText
    # 必填：会话隔离与"上一单"回溯都依赖它，缺失会造成跨用户串单
    session_id: SessionId
    # 前端生成的单条消息 ID，用于接口幂等（双击/网络重试不重复落库）
    client_msg_id: Optional[ClientMsgId] = None


class TicketStatusUpdate(BaseModel):
    ticket_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]
    status: Literal["待处理", "处理中", "已完成", "已关闭"]
    note: Optional[Annotated[str, StringConstraints(max_length=200)]] = None


class TicketClaimRequest(BaseModel):
    """坐席认领工单：操作人取自请求头 X-Operator。"""

    ticket_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]


class TicketAssignRequest(BaseModel):
    """管理员指派工单给指定坐席。"""

    ticket_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]
    assignee: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class ApiResult(BaseModel):
    code: int = 200
    msg: str = "ok"


class PageMeta(BaseModel):
    total: int
    page: int
    size: int
    pages: int = 0
