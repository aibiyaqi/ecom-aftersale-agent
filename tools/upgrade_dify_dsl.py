# -*- coding: utf-8 -*-
"""Dify DSL 升级脚本（可重复执行，执行前自动备份）。

修正 5 类问题：
1. 4 个 HTTP 节点硬编码 ngrok 域名 -> 环境变量 BACKEND_BASE_URL
2. 回调接口没有鉴权头 -> 增加 X-Service-Token:{{#env.SERVICE_TOKEN#}}
3. HTTP 节点超时无上限（max_*_timeout: 0）-> 收敛到 5/10 秒
4. 查订单/物流/退款请求体写死 OD2026001 -> 引用参数提取器的 order_id
5. 人工分支的 human-input 节点（3 小时超时、无 action 出边、阻塞 API）-> 删除，
   改为建单后直接回复"已提交人工处理"

另外统一修正 `{{{#var#}}}}` 这类非法模板占位符为 `{{#var#}}`。
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import yaml

DSL = Path(__file__).resolve().parents[1] / "dify_app_dsl.yml"
BACKUP = DSL.with_name("dify_app_dsl.v1.backup.yml")

PARAM_NODE = "1778390805959"          # 参数提取器（order_id / ticket_type / description）
ORDER_HTTP = "1783571850971"
LOGISTICS_HTTP = "1783571984499"
REFUND_HTTP = "1783571992677"
CREATE_TICKET_HTTP = "1783573499869"
TICKET_CODE = "1783742581524"         # 生成 create_ticket 请求体的代码节点
REFUND_CODE = "1783744768040"
HUMAN_INPUT = "1778393630023"
MANUAL_ANSWER = "1778481758327"       # "直接回复2-人工"


def _env_block() -> str:
    def item(name: str, description: str, value: str) -> str:
        return (
            f"  - description: {description}\n"
            f"    id: {uuid.uuid4().hex}\n"
            f"    name: {name}\n"
            f"    selector:\n"
            f"    - env\n"
            f"    - {name}\n"
            f"    value: '{value}'\n"
            f"    value_type: string\n"
        )

    return (
        "  environment_variables:\n"
        + item("BACKEND_BASE_URL", "后端服务公网地址（不含结尾斜杠），例如 https://api.example.com", "https://your-domain.example.com")
        + item("SERVICE_TOKEN", "与后端 .env 的 SERVICE_TOKEN 一致，用于回调接口鉴权", "请替换为SERVICE_TOKEN")
    )


def transform(text: str) -> str:
    # 1. 环境变量
    assert "  environment_variables: []" in text, "未找到 environment_variables 占位"
    text = text.replace("  environment_variables: []", _env_block(), 1)

    # 2. URL 去 ngrok
    text, replaced = re.subn(
        r"url: https://revise-reviver-numeral\.ngrok-free\.dev(/api/[a-z_]+)",
        r"url: '{{#env.BACKEND_BASE_URL#}}\1'",
        text,
    )
    assert replaced == 4, f"应替换 4 个 URL，实际 {replaced}"
    assert "ngrok-free.dev" not in text, "仍有 ngrok 域名残留"

    # 3. 请求头：Content-Type + 服务令牌
    headers = (
        "        headers: |-\n"
        "          Content-Type:application/json\n"
        "          X-Service-Token:{{#env.SERVICE_TOKEN#}}"
    )
    for old in (
        "        headers: X-API-Key:dify_order_2024",
        "        headers: Content-Type:application/json",
        "        headers: X-API-Key:dify_itsm_2024",
    ):
        text = text.replace(old, headers)
    assert text.count("X-Service-Token:{{#env.SERVICE_TOKEN#}}") == 4

    # 4. 超时与重试
    text = text.replace("max_connect_timeout: 0", "max_connect_timeout: 5")
    text = text.replace("max_read_timeout: 0", "max_read_timeout: 10")
    text = text.replace("max_write_timeout: 0", "max_write_timeout: 10")
    text = text.replace("          read: 3", "          read: 10")
    text = text.replace("          write: 2", "          write: 10")
    text = text.replace("max_retries: 3", "max_retries: 2")
    text = text.replace("retry_interval: 100", "retry_interval: 500")
    # create_ticket 节点原本只有 max_*，补上实际超时，避免依赖默认值
    text = text.replace(
        "          max_connect_timeout: 5\n"
        "          max_read_timeout: 10\n"
        "          max_write_timeout: 10\n"
        "        title: HTTP 请求 4-人工",
        "          connect: 5\n"
        "          max_connect_timeout: 5\n"
        "          max_read_timeout: 10\n"
        "          max_write_timeout: 10\n"
        "          read: 10\n"
        "          write: 10\n"
        "        title: HTTP 请求 4-人工",
    )

    # 5. 请求体：不再写死演示订单号
    text = text.replace(
        """value: '{"order_no": "OD2026001"}'""",
        """value: '{"order_no": "{{#1778390805959.order_id#}}"}'""",
    )
    assert (
        """value: '{"order_no": "OD2026001"}'""" not in text
    ), "请求体仍写死演示订单号"

    # 6. create_ticket 请求体改用代码节点生成的 JSON（避免描述里的引号破坏 JSON）
    idx = text.index("key-value-1128")
    value_at = text.index("value: ", idx)
    type_at = text.index("type: json", value_at)
    text = (
        text[:value_at]
        + "value: '{{#1783742581524.request_body#}}'\n          "
        + text[type_at:]
    )

    # 7. 非法三/四花括号占位符归一化（含历史手工编辑留下的残字）
    text = text.replace("{{{#1783571850971.body#}}求.body}}", "{{#1783571850971.body#}}")
    text = text.replace("{{{#1783571984499.body#}}dy}}", "{{#1783571984499.body#}}")
    text = re.sub(r"\{\{\{+#([^#}\n]+)#\}\}\}+", r"{{#\1#}}", text)
    assert "{{{" not in text, "仍有非法占位符"

    # 8. 删除 human-input 节点及其两条边，改接 建单 -> 人工回复
    node_pattern = re.compile(
        r"\n    - data:(?:(?!\n    - data:).)*?id: '" + HUMAN_INPUT + r"'(?:(?!\n    - data:).)*",
        re.S,
    )
    text, removed = node_pattern.subn("", text, count=1)
    assert removed == 1, "未删除 human-input 节点"

    edge_pattern = re.compile(
        r"\n    - data:(?:(?!\n    - data:).)*?" + HUMAN_INPUT + r"(?:(?!\n    - data:).)*",
        re.S,
    )
    text, removed = edge_pattern.subn("", text)
    assert removed == 2, f"应删除 2 条 human-input 相关边，实际 {removed}"

    new_edge = (
        "\n    - data:\n"
        "        isInIteration: false\n"
        "        isInLoop: false\n"
        "        sourceType: http-request\n"
        "        targetType: answer\n"
        f"      id: {CREATE_TICKET_HTTP}-source-{MANUAL_ANSWER}-target\n"
        "      selected: false\n"
        f"      source: '{CREATE_TICKET_HTTP}'\n"
        "      sourceHandle: source\n"
        f"      target: '{MANUAL_ANSWER}'\n"
        "      targetHandle: target\n"
        "      type: custom\n"
        "      zIndex: 0"
    )
    text = text.replace("\n    nodes:\n", new_edge + "\n    nodes:\n", 1)

    # 9. 模型名：qwen3.7-plus 不是通义有效模型
    text = text.replace("name: qwen3.7-plus", "name: qwen-plus")

    return text


def _assert_no_duplicate_keys(node, path: str = "root") -> None:
    from yaml.nodes import MappingNode, SequenceNode

    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            assert key not in seen, f"{path} 存在重复键: {key}"
            seen.add(key)
            _assert_no_duplicate_keys(value_node, f"{path}.{key}")
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            _assert_no_duplicate_keys(item, f"{path}[{index}]")


def validate(text: str) -> None:
    _assert_no_duplicate_keys(yaml.compose(text))
    data = yaml.safe_load(text)
    graph = data["workflow"]["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]

    assert HUMAN_INPUT not in nodes, "human-input 仍然存在"
    assert len(nodes) == len(graph["nodes"]), "节点 id 重复"

    edge_pairs = {(edge["source"], edge["target"]) for edge in edges}
    assert (CREATE_TICKET_HTTP, MANUAL_ANSWER) in edge_pairs, "人工分支未接回 answer"

    for edge in edges:
        assert edge["source"] in nodes, f"边指向不存在的节点: {edge['source']}"
        assert edge["target"] in nodes, f"边指向不存在的节点: {edge['target']}"

    http_nodes = [n for n in graph["nodes"] if n["data"].get("type") == "http-request"]
    assert len(http_nodes) == 4
    for node in http_nodes:
        data = node["data"]
        assert data["url"].startswith("{{#env.BACKEND_BASE_URL#}}/api/"), data["url"]
        assert "X-Service-Token:{{#env.SERVICE_TOKEN#}}" in data["headers"]
        assert data["timeout"]["max_read_timeout"] == 10

    # 每个非 start 节点都要有入边；每个非 answer 节点都要有出边
    for node_id, node in nodes.items():
        node_type = node["data"].get("type")
        if node_type != "start":
            assert any(e["target"] == node_id for e in edges), f"{node_id} 没有入边"
        if node_type not in {"answer", "end"}:
            assert any(e["source"] == node_id for e in edges), f"{node_id} 没有出边"

    print(f"校验通过：节点 {len(nodes)} 个，边 {len(edges)} 条，HTTP 节点 4 个")


def main() -> None:
    original = DSL.read_text(encoding="utf-8")
    if not BACKUP.exists():
        shutil.copy(DSL, BACKUP)
        print(f"已备份原始 DSL -> {BACKUP.name}")

    before = yaml.safe_load(original)["workflow"]["graph"]
    upgraded = transform(original)
    validate(upgraded)
    DSL.write_text(upgraded, encoding="utf-8")

    after = yaml.safe_load(upgraded)["workflow"]["graph"]
    print(f"节点 {len(before['nodes'])} -> {len(after['nodes'])}，边 {len(before['edges'])} -> {len(after['edges'])}")
    print(f"已写入 {DSL.name}")


if __name__ == "__main__":
    main()
