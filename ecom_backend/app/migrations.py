# -*- coding: utf-8 -*-
"""轻量迁移：在没有 Alembic 的情况下，让旧 orders.db 平滑升级。

只做两件事：
1. 建缺失的表 / 列（ADD COLUMN 幂等）
2. 回填新字段（幂等键、默认值）

正式上 MySQL 时建议换 Alembic，这里的实现保持"能重复执行、不丢数据"。
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import Base, engine
from app.models import TOrder, TChatRecord, TServiceTicket, TServiceTicketEvent  # noqa: F401
from app.services.tickets import ticket_dedupe_key

logger = logging.getLogger(__name__)

# 旧库可能缺的列：(表, 列, DDL 片段)
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "t_order": {
        "paid_amount": "NUMERIC(12,2) NOT NULL DEFAULT 0",
        "updated_at": "DATETIME",
    },
    "t_chat_record": {
        "session_id": "VARCHAR(64)",
        "client_msg_id": "VARCHAR(64)",
    },
    "t_service_ticket": {
        "dedupe_key": "VARCHAR(64)",
        "risk_reason_raw": "VARCHAR(200)",
        "source": "VARCHAR(20)",
        "session_id": "VARCHAR(64)",
        "assignee": "VARCHAR(50)",
        "updated_at": "DATETIME",
    },
}

# (索引名, 表, 列, 是否唯一)；索引名与 SQLAlchemy 模型自动生成的保持一致，避免重复建
_INDEXES = (
    ("ix_t_order_phone", "t_order", ("phone",), False),
    ("ix_t_chat_record_intent", "t_chat_record", ("intent",), False),
    ("ix_t_service_ticket_order_no", "t_service_ticket", ("order_no",), False),
    ("ix_t_service_ticket_event_ticket_id", "t_service_ticket_event", ("ticket_id",), False),
    ("ix_chat_session_created", "t_chat_record", ("session_id", "create_time"), False),
    ("ix_ticket_status_created", "t_service_ticket", ("handle_status", "create_time"), False),
    ("ix_event_ticket", "t_service_ticket_event", ("ticket_id", "create_time"), False),
    ("uq_ticket_dedupe_key", "t_service_ticket", ("dedupe_key",), True),
    ("uq_chat_session_msg", "t_chat_record", ("session_id", "client_msg_id"), True),
)


def _index_sql(
    backend: str, table: str, name: str, columns: tuple[str, ...], unique: bool
) -> str:
    """按方言生成建索引语句：MySQL 不支持 CREATE INDEX IF NOT EXISTS。"""
    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    if backend.startswith("sqlite"):
        prefix += " IF NOT EXISTS"
    return f"{prefix} {name} ON {table} ({', '.join(columns)})"


def _has_unique_on(table: str, columns: tuple[str, ...]) -> bool:
    """判断某组列是否已有唯一约束/唯一索引（避免重复建索引）。"""
    inspector = inspect(engine)
    for index in inspector.get_indexes(table):
        if index.get("unique") and tuple(index.get("column_names") or ()) == columns:
            return True
    try:
        for constraint in inspector.get_unique_constraints(table):
            if tuple(constraint.get("column_names") or ()) == columns:
                return True
    except NotImplementedError:  # pragma: no cover - 个别方言不支持
        return False
    return False


def _has_index_on(table: str, columns: tuple[str, ...]) -> bool:
    """同列组合已有索引即视为满足，避免旧库/模型自动索引造成重复索引。"""
    return any(
        tuple(index.get("column_names") or ()) == columns
        for index in inspect(engine).get_indexes(table)
    )


def _ensure_index(table: str, name: str, columns: tuple[str, ...], unique: bool) -> None:
    if _has_index_on(table, columns):
        if not unique or _has_unique_on(table, columns):
            return

    backend = engine.url.get_backend_name()
    sql = _index_sql(backend, table, name, columns, unique)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except IntegrityError:
        logger.warning(
            "迁移：%s(%s) 存在重复数据，跳过唯一索引 %s（请先清理历史重复）",
            table,
            ", ".join(columns),
            name,
        )
        return
    logger.info("迁移：为 %s(%s) 建索引 %s", table, ", ".join(columns), name)


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _EXPECTED_COLUMNS.items():
            if table not in existing_tables:
                continue
            current = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in current:
                    continue
                logger.info("迁移：%s 增加列 %s", table, name)
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _backfill() -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE t_order SET paid_amount = 0 WHERE paid_amount IS NULL"))
        conn.execute(text("UPDATE t_order SET updated_at = create_time WHERE updated_at IS NULL"))
        conn.execute(
            text("UPDATE t_service_ticket SET source = 'local' WHERE source IS NULL")
        )
        conn.execute(
            text(
                "UPDATE t_service_ticket SET updated_at = create_time "
                "WHERE updated_at IS NULL"
            )
        )


def _backfill_dedupe_keys() -> None:
    """为历史工单补幂等键；重复的历史脏数据保留最早一条，其余置空（不删数据）。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        tickets = (
            db.query(TServiceTicket)
            .filter(TServiceTicket.dedupe_key.is_(None))
            .order_by(TServiceTicket.create_time.asc())
            .all()
        )
        if not tickets:
            return
        # 已存在的幂等键要先占位，否则重复执行迁移时会撞唯一索引
        seen: set[str] = {
            key
            for (key,) in db.query(TServiceTicket.dedupe_key)
            .filter(TServiceTicket.dedupe_key.isnot(None))
            .all()
            if key
        }
        duplicates = 0
        assigned = 0
        for ticket in tickets:
            key = ticket_dedupe_key(
                ticket.order_no, ticket.session_id, ticket.risk_type, ticket.handle_status
            )
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            ticket.dedupe_key = key
            assigned += 1
        db.commit()
        logger.info("迁移：回填工单幂等键 %s 条，跳过历史重复 %s 条", assigned, duplicates)
    except Exception:  # pragma: no cover - 迁移失败不应阻塞启动，但必须留痕
        db.rollback()
        logger.exception("迁移：回填工单幂等键失败")
        raise
    finally:
        db.close()


def run_migrations() -> None:
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _backfill()
    for name, table, columns, unique in _INDEXES:
        _ensure_index(table, name, columns, unique)
    _backfill_dedupe_keys()
    if not engine.url.get_backend_name().startswith("sqlite"):
        logger.info("非 SQLite 环境：后续 DDL 变更建议改用 Alembic 管理")
    logger.info("数据库迁移完成: %s", engine.url.render_as_string(hide_password=True))
