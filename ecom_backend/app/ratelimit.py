# -*- coding: utf-8 -*-
"""单实例内存限流。

公开的 /api/send_msg 会触发一次 LLM 调用，必须有最基本的滥用保护。
多副本部署时把它换成 Redis（INCR + EXPIRE 或滑动窗口），接口保持不变。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.config import settings

_WINDOW_SECONDS = 60


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = _WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            # 顺手清理长期不活跃的 key，避免字典无限增长
            if len(self._hits) > 10000:
                for stale_key in [k for k, v in self._hits.items() if not v][:1000]:
                    self._hits.pop(stale_key, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)


def rate_limit_send_msg(request: Request) -> None:
    """/api/send_msg 的依赖：按客户端 IP 限流。"""
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.allow(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="消息发送过于频繁，请稍后再试",
            headers={"Retry-After": str(_WINDOW_SECONDS)},
        )
