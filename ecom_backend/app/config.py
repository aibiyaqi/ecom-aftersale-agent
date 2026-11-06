# -*- coding: utf-8 -*-
"""集中配置：所有可变项走环境变量，敏感项不给默认值。

加载顺序（后者覆盖前者）：
1. 代码内默认值
2. 项目根目录 .env
3. ecom_backend/.env
4. 进程环境变量
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ecom_backend/
BASE_DIR = Path(__file__).resolve().parent.parent
# 仓库根目录
PROJECT_ROOT = BASE_DIR.parent

for _env_file in (PROJECT_ROOT / ".env", BASE_DIR / ".env"):
    if _env_file.is_file():
        load_dotenv(_env_file, override=False)


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _default_database_url() -> str:
    """默认把 SQLite 放在 ecom_backend/orders.db，不随启动目录漂移。"""
    db_path = (BASE_DIR / "orders.db").as_posix()
    return f"sqlite:///{db_path}"


@dataclass(frozen=True)
class Settings:
    # ---- 运行环境 ----
    app_name: str = "电商智能订单售后处理系统"
    version: str = "2.0.0"
    environment: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # ---- 数据库 ----
    database_url: str = os.getenv("DATABASE_URL", "") or _default_database_url()
    db_echo: bool = _get_bool("DB_ECHO", False)

    # ---- Dify ----
    # chat: /v1/chat-messages（当前 advanced-chat 应用）
    # workflow: /v1/workflows/run（推荐，可拿结构化 outputs）
    dify_app_mode: str = os.getenv("DIFY_APP_MODE", "chat").strip().lower()
    dify_api_url: str = os.getenv("DIFY_API_URL", "").strip()
    dify_api_key: str = os.getenv("DIFY_API_KEY", "").strip()
    dify_user: str = os.getenv("DIFY_USER", "ecom_system_user")
    dify_timeout: float = float(os.getenv("DIFY_TIMEOUT", "60"))
    dify_max_retries: int = int(os.getenv("DIFY_MAX_RETRIES", "1"))

    # ---- 鉴权 ----
    admin_token: str = os.getenv("ADMIN_TOKEN", "").strip()
    service_token: str = os.getenv("SERVICE_TOKEN", "").strip()
    # 演示环境允许空 Token 直接放行；生产必须关闭
    allow_insecure_no_token: bool = _get_bool("ALLOW_INSECURE_NO_TOKEN", False)

    # ---- 网络 / 静态资源 ----
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: _get_list(
            "CORS_ORIGINS", "http://localhost:5000,http://127.0.0.1:5000"
        )
    )
    static_dir: Path = BASE_DIR / "app" / "static"

    # ---- 限流（单实例内存版，多副本请换 Redis） ----
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
    max_user_text_length: int = int(os.getenv("MAX_USER_TEXT_LENGTH", "1000"))
    # 请求体上限：字段级 max_length 是解析后才校验的，这里在读取阶段就挡住大包
    max_request_bytes: int = int(os.getenv("MAX_REQUEST_BYTES", str(64 * 1024)))

    def __post_init__(self) -> None:
        """模式与端点自洽：避免切了 workflow 却仍打 chat-messages。"""
        if not self.dify_api_url:
            return
        if self.dify_app_mode == "workflow" and "chat-messages" in self.dify_api_url:
            fixed = self.dify_api_url.replace("chat-messages", "workflows/run")
            logger.warning(
                "DIFY_APP_MODE=workflow 但 DIFY_API_URL 指向 chat-messages，已自动改为 %s",
                fixed,
            )
            object.__setattr__(self, "dify_api_url", fixed)
        elif self.dify_app_mode == "chat" and "workflows/run" in self.dify_api_url:
            fixed = self.dify_api_url.replace("workflows/run", "chat-messages")
            logger.warning(
                "DIFY_APP_MODE=chat 但 DIFY_API_URL 指向 workflows/run，已自动改为 %s",
                fixed,
            )
            object.__setattr__(self, "dify_api_url", fixed)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def dify_endpoint(self) -> str:
        """未显式配置时按应用模式推导端点。"""
        if self.dify_api_url:
            return self.dify_api_url
        if self.dify_app_mode == "workflow":
            return "https://api.dify.ai/v1/workflows/run"
        return "https://api.dify.ai/v1/chat-messages"


settings = Settings()
