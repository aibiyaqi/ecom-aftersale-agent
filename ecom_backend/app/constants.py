# -*- coding: utf-8 -*-
"""业务枚举与常量：状态、意图、风控类型统一在这里定义，避免各处散落字符串。"""

# ---- 工单状态 ----
TICKET_STATUS_PENDING = "待处理"
TICKET_STATUS_PROCESSING = "处理中"
TICKET_STATUS_DONE = "已完成"
TICKET_STATUS_CLOSED = "已关闭"
TICKET_STATUSES: tuple[str, ...] = (
    TICKET_STATUS_PENDING,
    TICKET_STATUS_PROCESSING,
    TICKET_STATUS_DONE,
    TICKET_STATUS_CLOSED,
)
OPEN_TICKET_STATUSES = (TICKET_STATUS_PENDING, TICKET_STATUS_PROCESSING)

# ---- 意图 ----
INTENT_ORDER = "查订单"
INTENT_LOGISTICS = "查物流"
INTENT_REFUND = "退货退款"
INTENT_COMPLAINT = "投诉"
INTENT_MANUAL = "转人工"
INTENT_CHAT = "闲聊"
INTENT_UNKNOWN = "未识别"

# ---- 风控 / 工单类型（归一化后的枚举值，用于幂等键） ----
RISK_MANUAL = "用户主动转人工"
RISK_SYSTEM_FAILURE = "系统自动转人工"
RISK_REFUND = "用户申请退货退款"
RISK_COMPLAINT = "用户投诉"
RISK_OTHER = "其他售后问题"
RISK_TYPES: tuple[str, ...] = (
    RISK_MANUAL,
    RISK_SYSTEM_FAILURE,
    RISK_REFUND,
    RISK_COMPLAINT,
    RISK_OTHER,
)

# ---- 数据来源 ----
SOURCE_LOCAL = "local"
SOURCE_DIFY = "dify"
