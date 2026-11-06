# -*- coding: utf-8 -*-
"""测试夹具。

注意：必须在 import app 之前设置环境变量——config 在导入时就固化了配置。
"""
from __future__ import annotations

import os
from pathlib import Path

# 测试库放在工程内（沙箱/临时目录可能不可写），每次运行重建
_TMP_DIR = Path(__file__).resolve().parents[1] / ".pytest_tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)
for _stale in _TMP_DIR.glob("test-*.db*"):
    _stale.unlink(missing_ok=True)

_DB_PATH = _TMP_DIR / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["DIFY_API_KEY"] = "test-dify-key"
os.environ["DIFY_APP_MODE"] = "chat"
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["ALLOW_INSECURE_NO_TOKEN"] = "false"
os.environ["APP_ENV"] = "test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import chat as chat_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import TChatRecord, TServiceTicket, TServiceTicketEvent  # noqa: E402
from app.ratelimit import limiter  # noqa: E402

ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}
SERVICE_HEADERS = {"X-Service-Token": "test-service-token"}


@pytest.fixture(scope="session")
def client():
    # 用 with 触发 lifespan：迁移 + 演示数据
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_dynamic_tables():
    """每个用例前清空对话/工单/审计，保留 5 条演示订单。"""
    db = SessionLocal()
    try:
        db.query(TServiceTicketEvent).delete()
        db.query(TServiceTicket).delete()
        db.query(TChatRecord).delete()
        db.commit()
    finally:
        db.close()
    limiter.limit = 1000
    limiter.reset()
    yield
    limiter.limit = 1000
    limiter.reset()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def fake_dify(reply=None, *, ok=True, is_manual=False, ticket_id=None, intent=None,
              order_no=None, error=None, conversation_id="conv-1"):
    """构造一个假的 run_dify_workflow，用于替换真实调用。"""

    async def _runner(user_text, conversation_id=None):  # noqa: A002 - 与真实签名保持一致
        return {
            "ok": ok,
            "reply": reply,
            "is_manual": is_manual,
            "ticket_id": ticket_id,
            "intent": intent,
            "order_no": order_no,
            "conversation_id": conversation_id or "conv-1",
            "error": error,
        }

    return _runner


@pytest.fixture
def patch_dify(monkeypatch):
    def _patch(**kwargs):
        runner = fake_dify(**kwargs)
        monkeypatch.setattr(chat_module, "run_dify_workflow", runner)
        return runner

    return _patch
