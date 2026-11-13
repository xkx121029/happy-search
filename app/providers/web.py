"""Web 检索统一门面：按配置选择 Provider，统一返回 (候选列表, 引擎状态)。

- auto（默认）：并发调用全部免配置引擎并 RRF 融合，零配置即可用
- ddg / tavily / serper / bing：单引擎；失败自动回退到免配置引擎聚合
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings
from ..core.models import RawResult
from . import ddg, free, web_api


def _stat(engine: str, status: str, count: int, started: float, error: str | None = None) -> dict[str, Any]:
    return {
        "engine": engine,
        "status": status,
        "count": count,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }


async def search(
    query: str,
    limit: int = 10,
    *,
    language: str = "zh",
    region: str = "cn",
    time_range: str = "all",
    timeout: float | None = None,
    engines: list[str] | None = None,
) -> tuple[list[RawResult], list[dict[str, Any]]]:
    provider = settings.web_provider
    kwargs = {
        "language": language,
        "region": region,
        "time_range": time_range,
        "timeout": timeout,
    }

    if provider in ("", "auto", "free", "all"):
        return await free.search(query, limit, engines=engines, **kwargs)

    started = time.perf_counter()
    try:
        if provider in ("ddg", "duckduckgo"):
            results = await ddg.search(query, limit, **kwargs)
        else:
            results = await web_api.search(query, limit, **kwargs)
        return results, [_stat(provider, "ok" if results else "empty", len(results), started)]
    except Exception as exc:  # noqa: BLE001 - 主引擎失败自动回退免配置引擎
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        fallback, stats = await free.search(query, limit, engines=engines, **kwargs)
        return fallback, [_stat(provider, "failed", 0, started, error)] + stats
