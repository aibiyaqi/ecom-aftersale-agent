# -*- coding: utf-8 -*-
"""校验 Dify DSL：CI 里挡住"改坏工作流"的提交。

检查项：
- YAML 可解析、节点 id 唯一、边的两端存在
- 所有节点从 start 可达
- 4 个 HTTP 节点都走环境变量、带服务令牌、有超时
- 没有 human-input（会阻塞 blocking 调用）
- 没有残留 ngrok 域名或非法模板占位符

用法：python tools/check_dify_dsl.py [path]
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dify_app_dsl.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    graph = data["workflow"]["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]

    errors: list[str] = []

    if len(nodes) != len(graph["nodes"]):
        errors.append("节点 id 重复")

    for edge in edges:
        if edge["source"] not in nodes:
            errors.append(f"边指向不存在的节点: {edge['source']}")
        if edge["target"] not in nodes:
            errors.append(f"边指向不存在的节点: {edge['target']}")

    start = next((nid for nid, n in nodes.items() if n["data"].get("type") == "start"), None)
    if not start:
        errors.append("缺少 start 节点")
    else:
        out = defaultdict(list)
        for edge in edges:
            out[edge["source"]].append(edge["target"])
        seen, queue = {start}, deque([start])
        while queue:
            for nxt in out[queue.popleft()]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        for nid in nodes:
            if nid not in seen:
                errors.append(f"节点不可达: {nid}")

    http_nodes = [n for n in graph["nodes"] if n["data"].get("type") == "http-request"]
    if len(http_nodes) != 4:
        errors.append(f"HTTP 节点数量应为 4，实际 {len(http_nodes)}")

    for node in http_nodes:
        data_ = node["data"]
        title = data_.get("title", node["id"])
        url = str(data_.get("url", ""))
        headers = str(data_.get("headers", ""))
        timeout = data_.get("timeout") or {}
        if not url.startswith("{{#env.BACKEND_BASE_URL#}}/api/"):
            errors.append(f"{title}: URL 未走环境变量: {url}")
        if "X-Service-Token:{{#env.SERVICE_TOKEN#}}" not in headers:
            errors.append(f"{title}: 缺少 X-Service-Token 请求头")
        if not timeout.get("max_read_timeout"):
            errors.append(f"{title}: 未设置读取超时")

    env_names = {item.get("name") for item in data["workflow"].get("environment_variables") or []}
    for required in ("BACKEND_BASE_URL", "SERVICE_TOKEN"):
        if required not in env_names:
            errors.append(f"缺少环境变量定义: {required}")

    if any(n["data"].get("type") == "human-input" for n in nodes.values()):
        errors.append("存在 human-input 节点（会阻塞同步调用）")

    text = path.read_text(encoding="utf-8")
    if "ngrok-free" in text:
        errors.append("仍残留 ngrok 域名")
    if "{{{" in text:
        errors.append("存在非法模板占位符 {{{")

    if errors:
        for item in errors:
            print("ERROR", item)
        return 1

    print(f"DSL 校验通过：{len(nodes)} 节点 / {len(edges)} 边 / {len(http_nodes)} 个 HTTP 节点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
