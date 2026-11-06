# -*- coding: utf-8 -*-
"""Dify 连通性与模型凭据诊断。

用法（在仓库根目录）：
    python tools/verify_dify.py
    python tools/verify_dify.py --query "查物流 OD2026001"

它会：
1. 读取根目录 .env 的 DIFY_API_KEY
2. 校验 Key 对应哪个应用（名称 / 模式）
3. 真实发一条消息，判断链路是否可用
4. 失败时给出可执行的修复位置，而不是只抛异常

退出码：0 正常；2 模型凭据/模型名问题；3 Key 无效；4 其他错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def api_base(env: dict[str, str]) -> str:
    url = env.get("DIFY_API_URL", "https://api.dify.ai/v1/chat-messages")
    # 取 scheme://host 作为 API 根，兼容自建部署
    parts = httpx.URL(url)
    return f"{parts.scheme}://{parts.host}" + (f":{parts.port}" if parts.port else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="查物流 OD2026001", help="用于验证的用户消息")
    args = parser.parse_args()

    env = load_env()
    key = env.get("DIFY_API_KEY", "")
    if not key:
        print("[FAIL] .env 里没有 DIFY_API_KEY")
        return 3

    base = api_base(env)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    try:
        info = httpx.get(f"{base}/v1/info", headers=headers, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法连接 Dify API：{exc}")
        return 4

    if info.status_code == 401:
        print("[FAIL] DIFY_API_KEY 无效或已被删除（401）")
        return 3
    if info.status_code != 200:
        print(f"[FAIL] /v1/info 返回 {info.status_code}: {info.text[:200]}")
        return 4

    meta = info.json()
    print(f"[OK] 应用：{meta.get('name')}（模式：{meta.get('mode')}）")

    payload = {
        "inputs": {},
        "query": args.query,
        "response_mode": "blocking",
        "user": "dify-verify",
    }
    try:
        resp = httpx.post(f"{base}/v1/chat-messages", headers=headers, json=payload, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 调用 chat-messages 异常：{exc}")
        return 4

    body_text = resp.text[:1000]
    if resp.status_code == 200:
        data = resp.json()
        print(f"[OK] 回复：{data.get('answer', '')[:300]}")
        print(f"[OK] conversation_id：{data.get('conversation_id')}")
        return 0

    print(f"[FAIL] HTTP {resp.status_code}：{body_text}")

    if "Incorrect model credentials" in body_text or "model" in body_text.lower() and "credential" in body_text.lower():
        print(
            "\n原因：应用引用的模型凭据无效（通常是通义/DashScope API Key 失效，或模型名不存在）。\n"
            "修复：\n"
            "  1) 打开 Dify 控制台 -> 右上角头像 -> Settings -> Model Provider\n"
            "  2) 找到 Tongyi（通义千问），检查 API Key；失效就去阿里云百炼重新生成并填入\n"
            "  3) 回到应用，逐个 LLM 节点确认模型在下拉列表中真实存在（推荐 qwen-plus / qwen-max）\n"
            "  4) 重新发布应用后再执行本脚本\n"
        )
        return 2

    if resp.status_code == 400:
        try:
            print("错误码：", json.loads(body_text).get("code"))
        except Exception:  # noqa: BLE001
            pass
    return 4


if __name__ == "__main__":
    sys.exit(main())
