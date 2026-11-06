# -*- coding: utf-8 -*-
"""上传前自检：确认密钥/数据库/个人信息不会被带进仓库。

用法（仓库根目录）：
    python tools/preflight_secrets.py
    python tools/preflight_secrets.py --strict   # 警告也算失败，CI 用

它做三件事：
1. 确认 .gitignore 覆盖了必禁项（.env / *.db / *.ifpkg / 旧 DSL 备份）
2. 扫描"会被提交"的文件，命中密钥直接失败
3. 对手机号、ngrok 域名、数据集 ID 给出警告，人工确认

退出码：0 通过；1 发现阻断项。
"""
from __future__ import annotations

import argparse
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_tmp",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
    ".tools",
}

# 本地允许存在、但必须被 .gitignore 排除的文件
IGNORED_NAMES = {".env"}
IGNORED_SUFFIXES = {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".ifpkg", ".pem", ".key", ".log"}
IGNORED_EXACT = {"dify_app_dsl.v1.backup.yml"}
IGNORED_GLOBS = ("orders.db*", "pytest-cache-files-*")

REQUIRED_GITIGNORE = (".env", "*.db", "*.ifpkg", "dify_app_dsl.v1.backup.yml")

SECRET_PATTERNS = {
    "Dify 应用密钥": re.compile(r"\bapp-[A-Za-z0-9]{16,}\b"),
    "OpenAI 风格密钥": re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    "GitHub Token": re.compile(r"\b(ghp_|github_pat_)[A-Za-z0-9_]{16,}\b"),
    "AWS Key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "阿里云 AK": re.compile(r"\bLTAI[0-9A-Za-z]{12,}\b"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

WARN_PATTERNS = {
    "ngrok 隧道域名": re.compile(r"[a-z0-9-]+\.ngrok-free\.(dev|app)"),
    "中国大陆手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "Dify 数据集 ID": re.compile(r"dataset_ids:\s*\n\s*-\s*[A-Za-z0-9+/=]{10,}"),
}

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip"}

# 这些文件里的手机号是功能必需的虚构演示数据，不做 PII 报警
PII_ALLOWLIST = {"ecom_backend/app/seed.py", "ecom_backend/tests/test_api.py"}


def is_ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    name = path.name
    if name in IGNORED_NAMES or name in IGNORED_EXACT:
        return True
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return True
    if any(fnmatch(rel, pattern) or fnmatch(name, pattern) for pattern in IGNORED_GLOBS):
        return True
    return False


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(ROOT).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if is_ignored(path):
            continue
        yield path


def check_gitignore() -> list[str]:
    gitignore = ROOT / ".gitignore"
    if not gitignore.is_file():
        return ["缺少 .gitignore"]
    content = gitignore.read_text(encoding="utf-8")
    return [
        f".gitignore 缺少规则：{rule}"
        for rule in REQUIRED_GITIGNORE
        if rule not in content
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="警告也视为失败")
    args = parser.parse_args()

    errors: list[str] = check_gitignore()
    warnings: list[str] = []
    scanned = 0

    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1

        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(text)
            if match:
                errors.append(f"[{label}] {rel}: {match.group(0)[:6]}***")

        for label, pattern in WARN_PATTERNS.items():
            if label == "中国大陆手机号" and rel in PII_ALLOWLIST:
                continue
            for match in pattern.finditer(text):
                warnings.append(f"[{label}] {rel}: {match.group(0)[:40]}")

    for item in sorted(set(errors)):
        print("ERROR", item)
    for item in sorted(set(warnings)):
        print("WARN ", item)

    if errors:
        print(f"\n未通过：{len(set(errors))} 个阻断项")
        return 1
    if warnings and args.strict:
        print(f"\n未通过（strict）：{len(set(warnings))} 个警告")
        return 1

    print(
        f"\n通过：扫描 {scanned} 个待提交文件；警告 {len(set(warnings))} 条（人工确认即可）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
