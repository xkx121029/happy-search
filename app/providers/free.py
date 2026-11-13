"""免配置引擎编排：并发调用多个无 Key 搜索引擎 -> 引擎级隔离 -> RRF 融合。

只要任意一个引擎成功就返回结果；全部失败才由上层判定 web 源不可用。
返回 (统一候选列表, 引擎状态明细)，供 source_stats.engines 透出。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..config import settings
from ..core.models import RawResult
from ..core.ranker import normalize_url
from .free_engines import ENGINE_META, ENGINES, EngineContext

logger = logging.getLogger("happysearch")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json,application/xml,application/rss+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def enabled_engines() -> list[str]:
    names: list[str] = []
    for name in settings.free_engines:
        name = name.strip().lower()
        if name == "all":
            names.extend(ENGINES.keys())
        elif name in ENGINES and name not in names:
            names.append(name)
    unknown = [n for n in settings.free_engines if n and n not in ENGINES and n != "all"]
    if unknown:
        logger.warning("忽略未知免配置引擎: %s", unknown)
    return names


def _fuse(outcomes: list[tuple[str, list[RawResult]]], limit: int) -> list[RawResult]:
    fused: dict[str, dict[str, Any]] = {}
    for engine, items in outcomes:
        weight = ENGINE_META.get(engine, ("", "", 0.8))[2]
        for idx, item in enumerate(items, start=1):
            key = normalize_url(item.url)
            if not key:
                continue
            entry = fused.setdefault(
                key, {"item": item, "rrf": 0.0, "engines": [], "raw_score": 0.0}
            )
            entry["rrf"] += weight / (60 + idx)
            entry["engines"].append(engine)
            if item.raw_score >= entry["raw_score"]:
                entry["raw_score"] = item.raw_score
                entry["item"] = item

    merged: list[RawResult] = []
    for rank, entry in enumerate(
        sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[:limit], start=1
    ):
        item: RawResult = entry["item"]
        item.rank_in_source = rank
        item.extra["engine"] = entry["engines"][0]
        item.extra["engines"] = list(dict.fromkeys(entry["engines"]))
        merged.append(item)
    return merged


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
    requested = engines if engines is not None else enabled_engines()
    names = [n for n in requested if n in ENGINES]
    unknown = [n for n in requested if n not in ENGINES]
    if unknown:
        logger.warning("忽略未知免配置引擎: %s", unknown)
    timeout = timeout or settings.free_timeout_s
    stats: list[dict[str, Any]] = []

    if not names:
        return [], stats

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), headers=_HEADERS, follow_redirects=True
    ) as client:
        ctx = EngineContext(
            client=client,
            query=query,
            # 单引擎最多取 50 条（多数第三方 API/结果页不支持更大值）；
            # 融合阶段仍按外层 limit 输出，保证能填满 AI 请求的条数
            limit=min(limit, 50),
            language=language,
            region=region,
            time_range=time_range,
        )

        async def one(name: str) -> tuple[str, list[RawResult], dict[str, Any]]:
            started = time.perf_counter()
            status, error, items = "ok", None, []
            try:
                raw = await asyncio.wait_for(ENGINES[name](ctx), timeout=timeout + 0.5)
                items = [r for r in raw if r.url.startswith(("http://", "https://"))]
                if not items:
                    status = "empty"
            except Exception as exc:  # noqa: BLE001 - 单引擎失败必须隔离
                status, error = "failed", f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning("free engine failed: %s -> %s", name, error)
            stat = {
                "engine": name,
                "status": status,
                "count": len(items),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": error,
            }
            return name, items, stat

        outcomes = await asyncio.gather(
            *(one(name) for name in names), return_exceptions=True
        )

    stats: list[dict[str, Any]] = []
    merged_inputs: list[tuple[str, list[RawResult]]] = []
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # 理论不应发生（one 已捕获所有 Exception）；兜底防止个别引擎的未预期异常连坐其他引擎
            logger.exception("unexpected engine failure during gather")
            continue
        name, items, stat = outcome
        stats.append(stat)
        merged_inputs.append((name, items))
    merged = _fuse(merged_inputs, limit)
    return merged, stats
