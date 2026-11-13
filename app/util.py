"""公共工具：时间、ID、统一信封。"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .schemas import API_VERSION


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:20]


def envelope(
    *,
    success: bool,
    request_id: str,
    elapsed_ms: int,
    data: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "request_id": request_id,
        "timestamp": utc_now(),
        "elapsed_ms": elapsed_ms,
        "data": data,
        "error": error,
        "api_version": API_VERSION,
    }


class Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
