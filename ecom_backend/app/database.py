# -*- coding: utf-8 -*-
"""数据库连接：URL 走配置，SQLite 专属参数按方言隔离。"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_connect_args: dict = {}
if settings.is_sqlite:
    # check_same_thread=False：允许 FastAPI 线程池复用连接
    # timeout=30：写锁等待 30s，配合 busy_timeout
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    echo=settings.db_echo,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()


if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_connection, _connection_record):
        """每条新连接都设置 WAL / busy_timeout，避免并发写直接报 locked。"""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
        finally:
            cursor.close()


def get_db():
    """FastAPI 依赖：每个请求一个会话，结束必定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
