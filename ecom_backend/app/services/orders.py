# -*- coding: utf-8 -*-
"""订单领域逻辑：查询、物流状态推导、退款金额计算。

物流状态、退款金额此前是 random / 硬编码，同一订单两次查询结果会不一致；
现在全部由订单数据推导，保证幂等、可解释。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import TOrder

# 发货状态 -> 可展示的物流节点（按状态推导，不随机）
_DELIVERY_TO_LOGISTICS = {
    "未发货": ("未发货", "商品尚未发出"),
    "待发货": ("待发货", "仓库正在备货，尚未交接快递"),
    "已发货": ("已揽收", "快递已揽收，等待运输"),
    "运输中": ("运输中", "包裹在运输途中"),
    "派送中": ("派送中", "快递员正在派送"),
    "已签收": ("已签收", "包裹已签收"),
}

# 物流单号前缀 -> 承运商
_CARRIERS = (
    ("SF", "顺丰速运"),
    ("YT", "圆通速递"),
    ("ZTO", "中通快递"),
    ("JD", "京东物流"),
    ("EMS", "中国邮政"),
    ("YD", "韵达快递"),
)


def carrier_of(logistics_no: str | None) -> str:
    number = (logistics_no or "").upper()
    for prefix, name in _CARRIERS:
        if number.startswith(prefix):
            return name
    return "快递"


def find_order(db: Session, order_no: str | None = None, phone: str | None = None) -> TOrder | None:
    """按订单号或手机号查询；手机号取该用户最近一笔订单。"""
    if order_no:
        return db.query(TOrder).filter(TOrder.order_no == order_no).first()
    if phone:
        return (
            db.query(TOrder)
            .filter(TOrder.phone == phone)
            .order_by(TOrder.create_time.desc())
            .first()
        )
    return None


def logistics_info(order: TOrder) -> dict:
    """确定性的物流信息：同一订单任何时候返回一致结果。"""
    if not order.logistics_no:
        status, detail = _DELIVERY_TO_LOGISTICS.get(order.delivery_status, ("未发货", "暂无物流信息"))
        return {
            "order_no": order.order_no,
            "logistics_no": "",
            "carrier": "",
            "status": status,
            "detail": detail,
        }

    status, detail = _DELIVERY_TO_LOGISTICS.get(
        order.delivery_status, ("运输中", "包裹在运输途中")
    )
    return {
        "order_no": order.order_no,
        "logistics_no": order.logistics_no,
        "carrier": carrier_of(order.logistics_no),
        "status": status,
        "detail": f"{carrier_of(order.logistics_no)} {order.logistics_no}，当前状态：{status}",
    }


def refund_amount(order: TOrder) -> Decimal:
    """退款金额来自订单实付金额，只有退款中/已退款才返回非零。"""
    if order.refund_status in {"退款中", "退款成功", "已退款", "部分退款"}:
        return Decimal(order.paid_amount or 0).quantize(Decimal("0.01"))
    return Decimal("0.00")


def refund_info(order: TOrder) -> dict:
    return {
        "order_no": order.order_no,
        "refund_status": order.refund_status,
        "refund_amount": f"{refund_amount(order):.2f}",
        "paid_amount": f"{Decimal(order.paid_amount or 0).quantize(Decimal('0.01')):.2f}",
    }


def order_detail(order: TOrder) -> dict:
    return {
        "order_no": order.order_no,
        "phone": order.phone,
        "goods_name": order.goods_name,
        "pay_status": order.pay_status,
        "delivery_status": order.delivery_status,
        "logistics_no": order.logistics_no,
        "after_status": order.after_status,
        "refund_status": order.refund_status,
        "paid_amount": f"{Decimal(order.paid_amount or 0).quantize(Decimal('0.01')):.2f}",
        "create_time": order.create_time.strftime("%Y-%m-%d %H:%M:%S") if order.create_time else "",
    }


def order_summary_text(order: TOrder) -> str:
    """给本地兜底回复用的订单摘要。"""
    lines = [
        f"订单 {order.order_no}（{order.goods_name}）",
        f"📌 支付状态：{order.pay_status}",
        f"📌 发货状态：{order.delivery_status}",
    ]
    if order.logistics_no:
        lines.append(f"📌 物流单号：{order.logistics_no}（{carrier_of(order.logistics_no)}）")
    lines.append(f"📌 售后状态：{order.after_status}")
    lines.append(f"📌 退款状态：{order.refund_status}")
    return "\n".join(lines)
