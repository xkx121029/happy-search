"""业务编排层：搜索链路与抓取链路。所有返回值都是已结构化的 dict。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import settings
from .core import fetcher
from .core.models import RawResult
from .core.ranker import apply_filters, rank
from .errors import SearchError
from .providers import web
from .providers.local import index
from .schemas import (
    DocumentsRequest,
    FetchData,
    FetchRequest,
    ResultItem,
    SearchData,
    SearchRequest,
)

logger = logging.getLogger("happysearch")


# ---------------------------------------------------------------- 检索源
async def _run_source(source: str, req: SearchRequest, query: str, limit: int):
    if source == "web":
        if not settings.web_search_enabled:
            raise RuntimeError("web 检索源已被服务端禁用")
        return await web.search(
            query,
            limit=limit,
            language=req.language,
            region=req.region,
            time_range=req.time_range,
            engines=req.engines or None,
        )
    if source in ("local_index", "knowledge_base"):
        results = await asyncio.to_thread(
            index.search,
            req.query,
            limit,
            source=source,
            tags=req.filters.get("tags"),
        )
        return results, []
    raise RuntimeError(f"未知检索源: {source}")


def _rewrite_query(req: SearchRequest) -> str:
    include = [d for d in req.domains if not d.startswith("-")][:3]
    if include:
        return req.query + " " + " ".join(f"site:{d.lstrip('.')}" for d in include)
    return req.query


async def _gather(
    req: SearchRequest,
) -> tuple[dict[str, list[RawResult]], list[str], list[dict]]:
    query = _rewrite_query(req)
    # 融合候选池需大于期望返回条数，留出去重损耗；free.py 会把每引擎请求条数再收敛到 50
    limit = max(req.top_k * 2, 20)
    tasks = {s: _run_source(s, req, query, limit) for s in req.sources}
    outcomes = await asyncio.gather(*tasks.values(), return_exceptions=True)
    groups: dict[str, list[RawResult]] = {}
    failed: list[str] = []
    engine_stats: list[dict] = []
    for source, outcome in zip(tasks.keys(), outcomes):
        if isinstance(outcome, Exception):
            logger.warning("source unavailable: %s -> %s", source, repr(outcome)[:300])
            failed.append(source)
            groups[source] = []
        else:
            items, stats = outcome if isinstance(outcome, tuple) else (outcome, [])
            groups[source] = items
            engine_stats.extend(stats)
            # 所有免配置引擎均异常（而非查询无结果）时，判定 web 源不可用
            if (
                source == "web"
                and not items
                and stats
                and all(s.get("status") == "failed" for s in stats)
            ):
                logger.warning("all web engines failed, marking web source unavailable")
                failed.append(source)
    return groups, failed, engine_stats


# ---------------------------------------------------------------- 搜索
def _extractive_answer(results: list[dict], query: str) -> dict[str, Any] | None:
    parts: list[str] = []
    citations: list[int] = []
    for item in results[:3]:
        snippet = (item.get("snippet") or "").strip()
        if snippet:
            parts.append(snippet[:200])
            citations.append(item["rank"])
    if not parts:
        return None
    return {
        "text": " ".join(parts)[:600],
        "citations": citations,
        "model": "extractive-v1",
    }


async def _execute_search(req: SearchRequest) -> dict[str, Any]:
    # page_size 未显式给出时自动跟随 top_k，保证 AI 指定 top_k/limit 就能拿到对应条数
    per_page = min(req.page_size or req.top_k, req.top_k, settings.max_results_per_page)
    if req.page * per_page > 1000:
        raise SearchError(
            "PAGINATION_LIMIT",
            "page * page_size 不得超过 1000",
            field="page",
            details={"max_results": 1000, "requested": req.page * per_page},
        )

    groups, failed_sources, engine_stats = await _gather(req)
    if failed_sources and len(failed_sources) == len(req.sources):
        raise SearchError(
            "SOURCE_UNAVAILABLE",
            "所有检索源均不可用",
            details={"failed_sources": failed_sources, "engines": engine_stats},
        )
    # 部分源失败但仍有结果（含全部成功场景 degraded=False），向后兼容只增字段
    degraded = bool(failed_sources) and len(failed_sources) < len(req.sources)

    groups = apply_filters(
        groups,
        time_range=req.time_range,
        domains=req.domains,
        safe_search=req.safe_search,
    )
    ranked = rank(groups, req.query, pool_size=max(req.top_k * 5, 50))

    start = (req.page - 1) * per_page
    page_raw = ranked[start : start + per_page]

    # 内部字段不外泄
    doc_contents: dict[str, str | None] = {}
    results: list[dict[str, Any]] = []
    for raw in page_raw:
        item = dict(raw)
        doc_contents[item["url"]] = item.pop("_doc_content", None)
        results.append(item)

    http_urls = [r["url"] for r in results if r["url"].startswith(("http://", "https://"))]

    # 能力 1：抓取结果对应网页并转结构化 JSON（result.page）
    if req.enrich_results and http_urls:
        pages = await fetcher.fetch_for_enrich(
            http_urls[: req.enrich_limit], timeout_ms=min(req.timeout_ms, 5000)
        )
        for item in results:
            page = pages.get(item["url"])
            if page:
                page.pop("raw_html", None)
                item["page"] = page

    # 能力 2：附带网页正文全文（result.raw_content）
    if req.include_raw_content and results:
        first = results[0]
        if doc_contents.get(first["url"]):
            first["raw_content"] = doc_contents[first["url"]]
        elif first["url"] in http_urls:
            pages = await fetcher.fetch_for_enrich(
                [first["url"]], timeout_ms=min(req.timeout_ms, 5000), max_chars=50000
            )
            fetched = pages.get(first["url"])
            if fetched:
                first["raw_content"] = fetched["text"]

    answer = _extractive_answer(results, req.query) if req.include_answer else None
    data = SearchData(
        query=req.query,
        rewritten_query=_rewrite_query(req),
        results=[ResultItem(**item) for item in results],
        answer=answer,
        related_queries=[],
        pagination={
            "page": req.page,
            "page_size": per_page,
            "requested": req.top_k,
            "returned": len(results),
            "total_estimate": len(ranked),
            "has_more": start + len(results) < len(ranked),
        },
        source_stats={
            "candidates": {s: len(items) for s, items in groups.items()},
            "failed_sources": failed_sources,
            "engines": engine_stats,
        },
        degraded=degraded,
    ).model_dump()

    # 可选字段按需出现；核心字段恒定存在（含 null）
    for item in data["results"]:
        if item.get("raw_content") is None:
            item.pop("raw_content", None)
        if item.get("page") is None:
            item.pop("page", None)
        if item.get("engine") is None:
            item.pop("engine", None)
    if data.get("answer") is None:
        data.pop("answer", None)
    return data


async def execute_search(req: SearchRequest) -> dict[str, Any]:
    """带全链路超时的搜索入口。"""
    try:
        return await asyncio.wait_for(_execute_search(req), timeout=req.timeout_ms / 1000.0)
    except asyncio.TimeoutError as exc:
        raise SearchError(
            "QUERY_TIMEOUT", f"检索超过 {req.timeout_ms}ms 未完成"
        ) from exc


# ---------------------------------------------------------------- 抓取
async def execute_fetch(req: FetchRequest) -> dict[str, Any]:
    try:
        result = await asyncio.wait_for(
            fetcher.fetch_many(req.targets, req),
            timeout=(req.timeout_ms * 2 + 2000) / 1000.0,
        )
    except asyncio.TimeoutError as exc:
        raise SearchError("QUERY_TIMEOUT", "批量抓取总耗时超限") from exc

    if result["stats"]["succeeded"] == 0:
        raise SearchError(
            "FETCH_ALL_FAILED",
            "所有网页抓取均失败",
            details={"failed": result["failed"]},
        )
    data = FetchData(**result).model_dump()
    for item in data["items"]:
        if item.get("raw_html") is None:
            item.pop("raw_html", None)
    return data


# ---------------------------------------------------------------- 索引提交
async def execute_documents(req: DocumentsRequest) -> dict[str, Any]:
    try:
        ids = await asyncio.to_thread(
            index.add, [doc.model_dump() for doc in req.documents]
        )
    except ValueError as exc:
        # 本地索引容量上限（HAPPYSEARCH_LOCAL_MAX_DOCS）
        raise SearchError("INDEX_LIMIT", str(exc)[:200]) from exc
    return {"indexed": len(ids), "ids": ids, "total": index.total}
