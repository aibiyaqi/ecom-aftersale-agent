# -*- coding: utf-8 -*-
"""演示数据：仅在空库时写入；已存在的演示订单只补金额，不覆盖真实数据。"""
from __future__ import annotations

import datetime
import logging

from app.database import SessionLocal
from app.models import TOrder

logger = logging.getLogger(__name__)

MOCK_ORDERS = [
    dict(order_no="OD2026001", phone="13800001111", goods_name="iPhone 15",
         pay_status="已支付", delivery_status="已发货", logistics_no="SF123456",
         after_status="无售后", refund_status="无退款", paid_amount=5999.00,
         create_time=datetime.datetime(2026, 7, 1, 10, 0, 0)),
    dict(order_no="OD2026002", phone="13800002222", goods_name="Nike 跑鞋",
         pay_status="已支付", delivery_status="待发货", logistics_no="",
         after_status="无售后", refund_status="无退款", paid_amount=699.00,
         create_time=datetime.datetime(2026, 7, 5, 14, 30, 0)),
    dict(order_no="OD2026003", phone="13800003333", goods_name="美的电饭煲",
         pay_status="已支付", delivery_status="运输中", logistics_no="YT789012",
         after_status="无售后", refund_status="无退款", paid_amount=399.00,
         create_time=datetime.datetime(2026, 7, 8, 9, 15, 0)),
    dict(order_no="OD2026004", phone="13800004444", goods_name="华为手表",
         pay_status="已支付", delivery_status="已发货", logistics_no="SF345678",
         after_status="退货中", refund_status="退款中", paid_amount=1299.00,
         create_time=datetime.datetime(2026, 6, 28, 16, 20, 0)),
    dict(order_no="OD2026005", phone="13800005555", goods_name="联想笔记本",
         pay_status="未支付", delivery_status="未发货", logistics_no="",
         after_status="无售后", refund_status="无退款", paid_amount=4599.00,
         create_time=datetime.datetime(2026, 7, 10, 11, 0, 0)),
]

_MOCK_NUMBERS = {item["order_no"] for item in MOCK_ORDERS}


def seed_mock_orders() -> None:
    db = SessionLocal()
    try:
        if db.query(TOrder).count() == 0:
            db.add_all([TOrder(**item) for item in MOCK_ORDERS])
            db.commit()
            logger.info("初始化演示订单 %s 条", len(MOCK_ORDERS))
            return

        # 旧库升级：给演示订单补实付金额（只改金额为 0 的演示数据）
        updated = 0
        for item in MOCK_ORDERS:
            row = db.query(TOrder).filter(TOrder.order_no == item["order_no"]).first()
            if row and not row.paid_amount:
                row.paid_amount = item["paid_amount"]
                updated += 1
        if updated:
            db.commit()
            logger.info("补齐演示订单实付金额 %s 条", updated)
    finally:
        db.close()
