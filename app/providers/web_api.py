"""需要 API Key 的 Web 检索 Provider（tavily / serper / bing）。

通过 HAPPYSEARCH_WEB_PROVIDER 选择，全部返回统一的 list[RawResult]。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings
from ..core.models import RawResult

logger = logging.getLogger("happysearch")


def _domain(url: str) -> str | None:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


def _build(title: str, url: str, snippet: str, score: float, published_at: str | None, rank: int) -> RawResult:
    return RawResult(
        title=title or url,
        url=url,
        snippet=(snippet or "")[:500],
        source="web",
        raw_score=max(0.0, min(1.0, score)),
        rank_in_source=rank,
        published_at=published_at,
        domain=_domain(url),
        content_type="text/html",
    )


async def _tavily(client: httpx.AsyncClient, query: str, limit: int, time_range: str) -> list[RawResult]:
    body: dict[str, Any] = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": False,
    }
    if time_range in ("day", "week", "month", "year"):
        body["time_range"] = time_range if time_range != "year" else "year"
    headers = {"Authorization": f"Bearer {settings.tavily_api_key}"}
    resp = await client.post("https://api.tavily.com/search", json=body, headers=headers)
    resp.raise_for_status()
    rows = resp.json().get("results", [])
    return [
        _build(
            r.get("title", ""),
            r.get("url", ""),
            r.get("content", ""),
            float(r.get("score") or 0.7),
            r.get("published_date"),
            idx,
        )
        for idx, r in enumerate(rows[:limit], start=1)
        if r.get("url", "").startswith(("http://", "https://"))
    ]


async def _serper(client: httpx.AsyncClient, query: str, limit: int, time_range: str) -> list[RawResult]:
    body: dict[str, Any] = {"q": query, "num": limit}
    if time_range == "day":
        body["tbs"] = "qdr:d"
    elif time_range == "week":
        body["tbs"] = "qdr:w"
    elif time_range == "month":
        body["tbs"] = "qdr:m"
    elif time_range == "year":
        body["tbs"] = "qdr:y"
    resp = await client.post(
        "https://google.serper.dev/search",
        json=body,
        headers={"X-API-KEY": settings.serper_api_key or ""},
    )
    resp.raise_for_status()
    rows = resp.json().get("organic", [])
    return [
        _build(
            r.get("title", ""),
            r.get("link", ""),
            r.get("snippet", ""),
            max(0.9 - 0.03 * (idx - 1), 0.5),
            r.get("date"),
            idx,
        )
        for idx, r in enumerate(rows[:limit], start=1)
        if r.get("link", "").startswith(("http://", "https://"))
    ]


async def _bing(
    client: httpx.AsyncClient, query: str, limit: int, language: str, region: str, time_range: str
) -> list[RawResult]:
    params: dict[str, str] = {
        "q": query,
        "count": str(limit),
        "mkt": f"{language}-{region.upper()}",
    }
    if time_range in ("day", "week", "month"):
        params["freshness"] = {"day": "Day", "week": "Week", "month": "Month"}[time_range]
    resp = await client.get(
        "https://api.bing.microsoft.com/v7.0/search",
        params=params,
        headers={"Ocp-Apim-Subscription-Key": settings.bing_api_key or ""},
    )
    resp.raise_for_status()
    rows = resp.json().get("webPages", {}).get("value", [])
    return [
        _build(
            r.get("name", ""),
            r.get("url", ""),
            r.get("snippet", ""),
            max(0.9 - 0.03 * (idx - 1), 0.5),
            r.get("dateLastCrawled"),
            idx,
        )
        for idx, r in enumerate(rows[:limit], start=1)
        if r.get("url", "").startswith(("http://", "https://"))
    ]


async def search(
    query: str,
    limit: int = 10,
    *,
    language: str = "zh",
    region: str = "cn",
    time_range: str = "all",
    timeout: float | None = None,
) -> list[RawResult]:
    provider = settings.web_provider
    key = {
        "tavily": settings.tavily_api_key,
        "serper": settings.serper_api_key,
        "bing": settings.bing_api_key,
    }.get(provider)
    if not key:
        raise RuntimeError(
            f"web_provider={provider} 缺少 API Key，请设置对应环境变量或改用 ddg"
        )
    timeout = timeout or settings.web_source_timeout_s
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if provider == "tavily":
            return await _tavily(client, query, limit, time_range)
        if provider == "serper":
            return await _serper(client, query, limit, time_range)
        if provider == "bing":
            return await _bing(client, query, limit, language, region, time_range)
    raise RuntimeError(f"不支持的 web_provider: {provider}")
