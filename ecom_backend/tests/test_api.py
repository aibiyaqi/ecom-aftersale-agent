# -*- coding: utf-8 -*-
"""接口与业务规则测试。

覆盖上一轮报告里的 P0/P1 修复点：
- 鉴权（管理端 / Dify 回调 / 拒绝裸奔）
- 工单幂等（重复调用、并发竞态、会话级去重）
- 事务与回滚（落库失败不产生半成品）
- 本地兜底、意图补全、消息幂等
- 物流/退款结果确定性
- 参数校验、错误码、限流
"""
from __future__ import annotations

import pytest

from app.constants import TICKET_STATUS_PENDING
from app.migrations import run_migrations
from app.ratelimit import limiter

ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}
SERVICE_HEADERS = {"X-Service-Token": "test-service-token"}


# --------------------------------------------------------------------------
# 基础可用性
# --------------------------------------------------------------------------
def test_health_and_pages(client):
    assert client.get("/").status_code == 200
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["db"] == "ok"
    assert client.get("/static/chat.html").status_code == 200
    assert client.get("/static/admin.html").status_code == 200


def test_static_and_db_paths_are_cwd_independent(client, monkeypatch):
    """换工作目录启动不应该影响静态目录与数据库定位。"""
    from pathlib import Path

    other_cwd = Path(__file__).resolve().parents[1] / ".pytest_tmp" / "cwd-check"
    other_cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(other_cwd)
    assert client.get("/static/chat.html").status_code == 200
    assert client.get("/healthz").json()["db"] == "ok"


def test_migrations_are_idempotent():
    run_migrations()
    run_migrations()


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------
def test_service_endpoints_require_token(client):
    payload = {"order_no": "OD2026001"}
    assert client.post("/api/get_order", json=payload).status_code == 401
    assert client.post("/api/get_order", json=payload, headers={"X-Service-Token": "wrong"}).status_code == 401
    assert client.post("/api/get_order", json=payload, headers=SERVICE_HEADERS).status_code == 200
    # Bearer 也支持
    assert (
        client.post("/api/get_order", json=payload, headers={"Authorization": "Bearer test-service-token"}).status_code
        == 200
    )


def test_admin_endpoints_require_token(client):
    assert client.get("/api/chat_list").status_code == 401
    assert client.get("/api/chat_list", headers=ADMIN_HEADERS).status_code == 200
    # 两套令牌不能互相替代
    assert client.get("/api/chat_list", headers=SERVICE_HEADERS).status_code == 401
    assert client.post("/api/get_order", json={"order_no": "OD2026001"}, headers=ADMIN_HEADERS).status_code == 401


def test_unconfigured_token_fails_closed(client, monkeypatch):
    import app.security as security

    class _Stub:
        admin_token = ""
        service_token = ""
        allow_insecure_no_token = False

    monkeypatch.setattr(security, "settings", _Stub())
    resp = client.get("/api/chat_list")
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == 503
    assert body["request_id"]


def test_allow_insecure_flag_for_local_debug(client, monkeypatch):
    import app.security as security

    class _Stub:
        admin_token = ""
        service_token = ""
        allow_insecure_no_token = True

    monkeypatch.setattr(security, "settings", _Stub())
    assert client.get("/api/chat_list").status_code == 200


def test_error_response_shape(client):
    resp = client.post("/api/get_order", json={}, headers=SERVICE_HEADERS)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    assert body["request_id"]
    assert body["errors"]


# --------------------------------------------------------------------------
# 订单 / 物流 / 退款
# --------------------------------------------------------------------------
def test_order_lookup_by_order_no_and_phone(client):
    by_no = client.post("/api/get_order", json={"order_no": "OD2026001"}, headers=SERVICE_HEADERS).json()
    assert by_no["goods_name"] == "iPhone 15"
    assert by_no["paid_amount"] == "5999.00"
    assert by_no["phone"] == "13800001111"

    by_phone = client.post("/api/get_order", json={"phone": "13800002222"}, headers=SERVICE_HEADERS).json()
    assert by_phone["order_no"] == "OD2026002"

    missing = client.post("/api/get_order", json={"order_no": "OD9999999"}, headers=SERVICE_HEADERS)
    assert missing.status_code == 404
    assert "订单不存在" in missing.json()["msg"]


def test_logistics_is_deterministic(client):
    first = client.post("/api/get_logistics", json={"order_no": "OD2026001"}, headers=SERVICE_HEADERS).json()
    second = client.post("/api/get_logistics", json={"order_no": "OD2026001"}, headers=SERVICE_HEADERS).json()
    assert first == second
    assert first["status"] == "已揽收"
    assert first["carrier"] == "顺丰速运"

    pending = client.post("/api/get_logistics", json={"order_no": "OD2026002"}, headers=SERVICE_HEADERS).json()
    assert pending["status"] == "待发货"
    assert pending["logistics_no"] == ""


def test_refund_amount_from_order_data(client):
    refunding = client.post("/api/get_refund", json={"order_no": "OD2026004"}, headers=SERVICE_HEADERS).json()
    assert refunding["refund_status"] == "退款中"
    assert refunding["refund_amount"] == "1299.00"
    assert refunding["paid_amount"] == "1299.00"

    no_refund = client.post("/api/get_refund", json={"order_no": "OD2026001"}, headers=SERVICE_HEADERS).json()
    assert no_refund["refund_amount"] == "0.00"


def test_refund_amount_is_not_hardcoded(client):
    """不同订单的退款金额必须不同（旧实现固定返回 3999.00）。"""
    amounts = {
        client.post("/api/get_refund", json={"order_no": no}, headers=SERVICE_HEADERS).json()["refund_amount"]
        for no in ("OD2026001", "OD2026004")
    }
    assert len(amounts) == 2


# --------------------------------------------------------------------------
# 建单幂等
# --------------------------------------------------------------------------
def _create_ticket(client, **payload):
    return client.post("/api/create_ticket", headers=SERVICE_HEADERS, json=payload)


def test_create_ticket_is_idempotent(client):
    first = _create_ticket(client, order_no="OD2026001", user_text="我要退货", risk_reason="退货退款")
    assert first.status_code == 200
    assert first.json()["duplicated"] is False

    second = _create_ticket(client, order_no="OD2026001", user_text="我要退货", risk_reason="退货退款")
    assert second.json()["duplicated"] is True
    assert second.json()["ticket_id"] == first.json()["ticket_id"]


def test_create_ticket_normalizes_risk_type(client):
    """Dify 传来的自由文本（甚至模板残留）要归一化，否则幂等键会漂移。"""
    first = _create_ticket(client, order_no="OD2026001", user_text="退款", risk_reason="{{查询退款进度}}")
    assert first.json()["risk_type"] == "用户申请退货退款"
    second = _create_ticket(client, order_no="OD2026001", user_text="退款", risk_reason="退款")
    assert second.json()["ticket_id"] == first.json()["ticket_id"]


def test_create_ticket_rejects_unknown_order(client):
    resp = _create_ticket(client, order_no="OD0000000", user_text="退货", risk_reason="退货")
    assert resp.status_code == 404


def test_create_ticket_without_order_is_deduped_by_content(client):
    """无订单号的诉求（如纯咨询）也要能建单，且同一句话不会反复建单。"""
    first = _create_ticket(client, user_text="你们几点上班", risk_reason="人工")
    assert first.status_code == 200
    second = _create_ticket(client, user_text="你们几点上班", risk_reason="人工")
    assert second.json()["ticket_id"] == first.json()["ticket_id"]
    assert second.json()["duplicated"] is True

    other = _create_ticket(client, user_text="发票怎么开", risk_reason="人工")
    assert other.json()["ticket_id"] != first.json()["ticket_id"]


# --------------------------------------------------------------------------
# 工单状态机与审计
# --------------------------------------------------------------------------
def test_ticket_status_flow_and_audit(client):
    ticket_id = _create_ticket(client, order_no="OD2026002", user_text="投诉", risk_reason="投诉").json()["ticket_id"]

    resp = client.put(
        "/api/ticket_status",
        headers=ADMIN_HEADERS,
        json={"ticket_id": ticket_id, "status": "处理中", "note": "已分配客服"},
    )
    assert resp.status_code == 200
    assert resp.json()["handle_status"] == "处理中"

    events = client.get("/api/ticket_events", params={"ticket_id": ticket_id}, headers=ADMIN_HEADERS).json()
    assert events["total"] == 1
    assert events["list"][0]["from_status"] == TICKET_STATUS_PENDING
    assert events["list"][0]["to_status"] == "处理中"
    assert events["list"][0]["note"] == "已分配客服"
    assert events["list"][0]["operator"] == "admin"


def test_ticket_status_rejects_illegal_value(client):
    ticket_id = _create_ticket(client, order_no="OD2026003", user_text="投诉", risk_reason="投诉").json()["ticket_id"]
    resp = client.put(
        "/api/ticket_status",
        headers=ADMIN_HEADERS,
        json={"ticket_id": ticket_id, "status": "随便写"},
    )
    assert resp.status_code == 422

    still = client.get("/api/ticket_list", headers=ADMIN_HEADERS).json()["list"]
    assert [t["handle_status"] for t in still if t["ticket_id"] == ticket_id] == [TICKET_STATUS_PENDING]


def test_ticket_status_missing_ticket_returns_404(client):
    resp = client.put(
        "/api/ticket_status",
        headers=ADMIN_HEADERS,
        json={"ticket_id": "TK-NOT-EXIST", "status": "已完成"},
    )
    assert resp.status_code == 404


def test_ticket_claim(client):
    ticket_id = _create_ticket(client, order_no="OD2026001", user_text="退货", risk_reason="退货").json()["ticket_id"]
    headers = {**ADMIN_HEADERS, "X-Operator": "alice"}

    resp = client.post("/api/ticket_claim", json={"ticket_id": ticket_id}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["assignee"] == "alice"
    assert resp.json()["handle_status"] == "处理中"

    # 待处理 -> 处理中 + 认领审计
    events = client.get("/api/ticket_events", params={"ticket_id": ticket_id}, headers=ADMIN_HEADERS).json()
    actions = [event["action"] for event in events["list"]]
    assert "claim" in actions
    claim_event = next(event for event in events["list"] if event["action"] == "claim")
    assert claim_event["operator"] == "alice"
    assert claim_event["from_status"] == "待处理"
    assert claim_event["to_status"] == "处理中"

    # 别人不能再认领
    other = client.post(
        "/api/ticket_claim",
        json={"ticket_id": ticket_id},
        headers={**ADMIN_HEADERS, "X-Operator": "bob"},
    )
    assert other.status_code == 409
    assert "已被" in other.json()["msg"]

    # 自己重复认领是幂等的
    again = client.post("/api/ticket_claim", json={"ticket_id": ticket_id}, headers=headers)
    assert again.status_code == 200


def test_ticket_assign_and_filter(client):
    ticket_id = _create_ticket(client, order_no="OD2026002", user_text="投诉", risk_reason="投诉").json()["ticket_id"]

    resp = client.post(
        "/api/ticket_assign",
        json={"ticket_id": ticket_id, "assignee": "bob"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["assignee"] == "bob"
    assert resp.json()["handle_status"] == "处理中"

    mine = client.get("/api/ticket_list", params={"assignee": "bob"}, headers=ADMIN_HEADERS).json()
    assert [item["ticket_id"] for item in mine["list"]] == [ticket_id]

    others = client.get("/api/ticket_list", params={"assignee": "alice"}, headers=ADMIN_HEADERS).json()
    assert others["total"] == 0

    assert (
        client.post(
            "/api/ticket_assign",
            json={"ticket_id": "TK-NOT-EXIST", "assignee": "bob"},
            headers=ADMIN_HEADERS,
        ).status_code
        == 404
    )


def test_closed_ticket_cannot_be_claimed(client):
    ticket_id = _create_ticket(client, order_no="OD2026003", user_text="退货", risk_reason="退货").json()["ticket_id"]
    client.put(
        "/api/ticket_status",
        headers=ADMIN_HEADERS,
        json={"ticket_id": ticket_id, "status": "已完成"},
    )
    resp = client.post(
        "/api/ticket_claim",
        json={"ticket_id": ticket_id},
        headers={**ADMIN_HEADERS, "X-Operator": "alice"},
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# 对话主流程
# --------------------------------------------------------------------------
def _send(client, text, session_id="S1", client_msg_id=None):
    payload = {"user_text": text, "session_id": session_id}
    if client_msg_id:
        payload["client_msg_id"] = client_msg_id
    return client.post("/api/send_msg", json=payload)


def test_send_msg_dify_failure_falls_back_to_local_read(client, patch_dify):
    """Dify 超时后，本地应直接查库回答，而不是无脑转人工建单。"""
    patch_dify(ok=False, error="Dify 请求超时")
    resp = _send(client, "查物流 OD2026003")
    assert resp.status_code == 200
    data = resp.json()
    assert "YT789012" in data["reply"]
    assert data["intent"] == "查物流"
    assert data["is_manual"] is False
    assert data["ticket_id"] is None

    tickets = client.get("/api/ticket_list", headers=ADMIN_HEADERS).json()
    assert tickets["total"] == 0


def test_local_intent_prefers_strong_intent_over_generic_order(client, patch_dify):
    """'订单退款' 应判成退款，而不是泛化的查订单。"""
    patch_dify(ok=False, error="Dify 请求超时")
    refund = _send(client, "订单退款 OD2026001", session_id="S-order-refund").json()
    assert refund["intent"] == "退货退款"
    assert refund["ticket_id"]


def test_local_order_lookup_handles_colloquial_phrasing(client, patch_dify):
    patch_dify(ok=False, error="Dify 请求超时")
    data = _send(client, "帮我查一下订单 OD2026001", session_id="S-colloquial").json()
    assert data["intent"] == "查订单"
    assert "支付状态" in data["reply"]
    assert data["ticket_id"] is None


def test_send_msg_manual_ticket_not_duplicated_per_session(client, patch_dify):
    """Dify 持续故障且无订单号时，同一会话只建一张工单（旧实现每说一句建一张）。"""
    patch_dify(ok=False, error="Dify 请求超时")
    first = _send(client, "在吗", client_msg_id="m1").json()
    second = _send(client, "在吗", client_msg_id="m2").json()

    assert first["is_manual"] is True
    assert second["is_manual"] is True
    assert first["ticket_id"] == second["ticket_id"]

    tickets = client.get("/api/ticket_list", headers=ADMIN_HEADERS).json()
    assert tickets["total"] == 1


def test_send_msg_infers_intent_when_dify_returns_none(client, patch_dify):
    """Dify 有回复但没回传 intent 时，本地规则要补上（原分支不可达）。"""
    patch_dify(reply="您的包裹正在运输途中。", intent=None)
    data = _send(client, "查一下物流 OD2026003").json()
    assert data["intent"] == "查物流"
    assert data["is_manual"] is False


def test_send_msg_client_msg_id_is_idempotent(client, patch_dify, monkeypatch):
    calls = {"n": 0}
    base = patch_dify(reply="您好，已收到。", intent="闲聊")

    async def counting(user_text, conversation_id=None):
        calls["n"] += 1
        return await base(user_text, conversation_id)

    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "run_dify_workflow", counting)

    first = _send(client, "你好", client_msg_id="same-id").json()
    second = _send(client, "你好", client_msg_id="same-id").json()

    assert calls["n"] == 1
    assert first["reply"] == second["reply"]
    assert second.get("duplicated") is True

    records = client.get("/api/chat_list", headers=ADMIN_HEADERS).json()
    assert records["total"] == 1


def test_send_msg_local_refund_creates_single_ticket(client, patch_dify):
    patch_dify(ok=True, reply=None, intent=None)  # Dify 空回复 → 本地接管
    first = _send(client, "我要退货 OD2026001", client_msg_id="r1").json()
    second = _send(client, "我要退货 OD2026001", client_msg_id="r2").json()

    assert first["intent"] == "退货退款"
    assert first["ticket_id"]
    assert first["ticket_id"] == second["ticket_id"]


def test_send_msg_manual_keyword_skips_dify(client, patch_dify, monkeypatch):
    """用户明确说"转人工"时不该再消耗一次模型调用。"""
    calls = {"n": 0}
    base = patch_dify(reply="不该被调用", intent="闲聊")

    async def counting(user_text, conversation_id=None):
        calls["n"] += 1
        return await base(user_text, conversation_id)

    import app.api.chat as chat_module

    monkeypatch.setattr(chat_module, "run_dify_workflow", counting)

    data = _send(client, "转人工", session_id="S-manual").json()
    assert calls["n"] == 0
    assert data["is_manual"] is True
    assert data["intent"] == "转人工"
    assert data["ticket_id"]


def test_negated_refund_does_not_create_ticket(client, patch_dify):
    patch_dify(ok=True, reply="好的，已为您取消退款申请。", intent="闲聊")
    data = _send(client, "不要退款了", session_id="S-neg").json()
    assert data["ticket_id"] is None


def test_send_msg_validation(client):
    assert client.post("/api/send_msg", json={"user_text": "", "session_id": "S-val"}).status_code == 422
    assert (
        client.post("/api/send_msg", json={"user_text": "x" * 1001, "session_id": "S-val"}).status_code
        == 422
    )


def test_send_msg_requires_session_id(client):
    """session_id 必填：缺失会导致跨会话串单与共享匿名工单。"""
    assert client.post("/api/send_msg", json={"user_text": "你好"}).status_code == 422
    assert client.post("/api/send_msg", json={"user_text": "你好", "session_id": ""}).status_code == 422
    assert client.post("/api/send_msg", json={"user_text": "你好", "session_id": "S-ok"}).status_code == 200


def test_request_body_size_limit(client):
    """超过 MAX_REQUEST_BYTES 的请求在读取阶段就 413，不进入解析。"""
    big = b'{"user_text":"' + b"x" * 70000 + b'","session_id":"S"}'
    resp = client.post(
        "/api/send_msg", content=big, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == 413


def test_index_sql_is_dialect_aware():
    """MySQL 不支持 CREATE INDEX IF NOT EXISTS，迁移 SQL 必须按方言生成。"""
    from app.migrations import _index_sql

    sqlite_sql = _index_sql("sqlite", "t_demo", "ix_demo", ("a", "b"), False)
    mysql_sql = _index_sql("mysql", "t_demo", "ix_demo", ("a", "b"), False)
    mysql_unique = _index_sql("mysql", "t_demo", "uq_demo", ("a",), True)

    assert "IF NOT EXISTS" in sqlite_sql
    assert "IF NOT EXISTS" not in mysql_sql
    assert mysql_sql == "CREATE INDEX ix_demo ON t_demo (a, b)"
    assert mysql_unique == "CREATE UNIQUE INDEX uq_demo ON t_demo (a)"


def test_settings_aligns_dify_endpoint_with_mode():
    from app.config import Settings

    workflow = Settings(
        dify_app_mode="workflow",
        dify_api_url="https://api.dify.ai/v1/chat-messages",
    )
    assert workflow.dify_api_url.endswith("/v1/workflows/run")

    chat = Settings(
        dify_app_mode="chat",
        dify_api_url="https://api.dify.ai/v1/workflows/run",
    )
    assert chat.dify_api_url.endswith("/v1/chat-messages")


def test_dify_404_classification():
    """只有业务语义明确的 404 才算"订单不存在"，其余按故障处理。"""
    from app.dify_client import _is_business_not_found

    assert _is_business_not_found("订单不存在: OD0000000")
    assert _is_business_not_found("order not found")
    assert not _is_business_not_found("Not Found")
    assert not _is_business_not_found("")


def test_send_msg_rate_limit(client):
    limiter.limit = 1
    limiter.reset()
    assert _send(client, "你好", session_id="S-rate").status_code == 200
    resp = _send(client, "在吗", session_id="S-rate")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After")


def test_chat_list_filters(client, patch_dify):
    patch_dify(reply="订单已发货。", intent="查物流")
    _send(client, "查物流 OD2026001", session_id="S-f")
    patch_dify(reply="已转接。", intent="转人工", is_manual=True)
    _send(client, "转人工看看", session_id="S-f")

    manual = client.get("/api/chat_list", params={"is_manual": 1}, headers=ADMIN_HEADERS).json()
    assert manual["total"] >= 1
    assert all(item["is_manual"] == 1 for item in manual["list"])

    logistics = client.get("/api/chat_list", params={"intent": "查物流"}, headers=ADMIN_HEADERS).json()
    assert logistics["total"] >= 1
    assert all(item["intent"] == "查物流" for item in logistics["list"])


def test_admin_order_detail(client):
    ok = client.get("/api/order_detail", params={"order_no": "OD2026001"}, headers=ADMIN_HEADERS)
    assert ok.status_code == 200
    assert ok.json()["data"]["phone"] == "13800001111"

    assert client.get("/api/order_detail", headers=ADMIN_HEADERS).status_code == 422
    assert (
        client.get("/api/order_detail", params={"order_no": "OD0000000"}, headers=ADMIN_HEADERS).status_code
        == 404
    )


def test_order_detail_no_phone_leak_without_admin(client):
    """订单详情（含手机号）不能匿名访问。"""
    assert client.get("/api/order_detail", params={"order_no": "OD2026001"}).status_code == 401


def test_frontend_escapes_user_content():
    """后台页面必须用 textContent 渲染，禁止 innerHTML 拼接用户输入。"""
    from pathlib import Path

    html = Path(__file__).resolve().parents[1] / "app" / "static" / "admin.html"
    content = html.read_text(encoding="utf-8")
    assert "innerHTML" not in content
    assert "textContent" in content
