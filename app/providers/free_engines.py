"""免配置搜索引擎集合：所有引擎均无需 API Key / 无需注册。

两类来源：
- 结构化 API（JSON / RSS / Atom）：ddg_instant、wikipedia、searx、hn、arxiv、google_news、bing_news
- 搜索结果页（HTML 解析）：ddg_html、ddg_lite、bing_html、baidu_html、mojeek

统一契约：async (ctx: EngineContext) -> list[RawResult]
单个引擎抛出的任何异常由 free.py 隔离，不影响其他引擎。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..core.models import RawResult
from . import ddg

logger = logging.getLogger("happysearch")


@dataclass(frozen=True)
class EngineContext:
    client: httpx.AsyncClient
    query: str
    limit: int
    language: str
    region: str
    time_range: str


# name -> (说明, 类型, 融合权重)
ENGINE_META: dict[str, tuple[str, str, float]] = {
    "ddg_html": ("DuckDuckGo HTML", "html", 1.0),
    "ddg_lite": ("DuckDuckGo Lite", "html", 1.0),
    "ddg_instant": ("DuckDuckGo Instant Answer JSON API", "api", 0.7),
    "wikipedia": ("Wikipedia MediaWiki Search API", "api", 0.75),
    "bing_html": ("Bing 搜索结果页", "html", 1.0),
    "bing_rss": ("Bing 搜索 RSS API（无需 Key）", "api", 1.0),
    "so360_html": ("360 搜索结果页", "html", 0.9),
    "baidu_html": ("百度搜索结果页", "html", 0.95),
    "mojeek": ("Mojeek 独立索引", "html", 0.9),
    "searx": ("SearXNG 公共实例 JSON API", "api", 0.9),
    "hn": ("Hacker News Algolia API", "api", 0.65),
    "arxiv": ("arXiv Atom API", "api", 0.7),
    "google_news": ("Google News RSS", "api", 0.7),
    "bing_news": ("Bing News RSS", "api", 0.65),
}


def _mk(
    title: str,
    url: str,
    snippet: str,
    rank: int,
    *,
    score: float | None = None,
    published_at: str | None = None,
    domain: str | None = None,
) -> RawResult:
    from urllib.parse import urlparse

    if not url.startswith(("http://", "https://")):
        url = ""  # 由 free.py 统一过滤，避免单条坏数据毁掉整个引擎
    return RawResult(
        title=(title or url).strip()[:300],
        url=url,
        snippet=re.sub(r"\s+", " ", snippet or "").strip()[:500],
        source="web",
        raw_score=score if score is not None else max(0.9 - 0.03 * (rank - 1), 0.5),
        rank_in_source=rank,
        published_at=published_at,
        domain=domain or (urlparse(url).netloc or None),
        content_type="text/html",
    )


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ddg_payload(ctx: EngineContext) -> dict[str, str]:
    payload = {"q": ctx.query, "kl": f"{ctx.region}-{ctx.language}"}
    df = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(ctx.time_range)
    if df:
        payload["df"] = df
    return payload


# ------------------------------------------------------------------ JSON / XML API
async def _ddg_html(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.post(ddg.DDG_HTML, data=_ddg_payload(ctx))
    resp.raise_for_status()
    return ddg._parse_html_endpoint(resp.text, ctx.limit)


async def _ddg_lite(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.post(ddg.DDG_LITE, data=_ddg_payload(ctx))
    resp.raise_for_status()
    return ddg._parse_lite_endpoint(resp.text, ctx.limit)


async def _ddg_instant(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://api.duckduckgo.com/",
        params={"q": ctx.query, "format": "json", "no_html": "1", "skip_disambig": "1"},
    )
    resp.raise_for_status()
    data = resp.json()
    out: list[RawResult] = []
    if data.get("AbstractURL"):
        out.append(
            _mk(
                data.get("Heading") or ctx.query,
                data["AbstractURL"],
                data.get("AbstractText", ""),
                len(out) + 1,
            )
        )
    topics: list = list(data.get("RelatedTopics") or [])
    nested = [t for t in topics if isinstance(t, dict) and "Topics" in t]
    for group in nested:
        topics.extend(group.get("Topics") or [])
    for topic in topics:
        if not isinstance(topic, dict) or "Topics" in topic:
            continue
        url, text = topic.get("FirstURL"), topic.get("Text")
        if url and text:
            out.append(_mk(text.split(" - ")[0], url, text, len(out) + 1))
        if len(out) >= ctx.limit:
            break
    return out[: ctx.limit]


async def _wikipedia(ctx: EngineContext) -> list[RawResult]:
    lang = "zh" if ctx.language.lower().startswith("zh") else (ctx.language if ctx.language else "en")
    resp = await ctx.client.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": ctx.query,
            "srlimit": str(ctx.limit),
            "format": "json",
            "utf8": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json().get("query", {}).get("search", [])
    out: list[RawResult] = []
    for idx, row in enumerate(rows[: ctx.limit], start=1):
        title = row.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", row.get("snippet", "")).replace("&quot;", '"')
        url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='/_:()')}"
        out.append(_mk(title, url, snippet, idx, published_at=_iso(row.get("timestamp"))))
    return out


async def _searx(ctx: EngineContext) -> list[RawResult]:
    last_error: Exception | None = None
    for base in settings.searx_urls:
        try:
            resp = await ctx.client.get(
                base.rstrip("/") + "/search",
                params={"q": ctx.query, "format": "json", "language": ctx.language},
            )
            resp.raise_for_status()
            rows = resp.json().get("results", [])
            if not rows:
                continue
            return [
                _mk(
                    r.get("title", ""),
                    r.get("url", ""),
                    r.get("content", ""),
                    idx,
                    score=float(r.get("score") or 0.7),
                    published_at=_iso(r.get("publishedDate")),
                )
                for idx, r in enumerate(rows[: ctx.limit], start=1)
                if r.get("url", "").startswith(("http://", "https://"))
            ]
        except Exception as exc:  # noqa: BLE001 - 换下一个公共实例
            last_error = exc
    if last_error:
        raise last_error
    return []


async def _hn(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": ctx.query, "tags": "story", "hitsPerPage": str(ctx.limit)},
    )
    resp.raise_for_status()
    out: list[RawResult] = []
    for idx, hit in enumerate(resp.json().get("hits", [])[: ctx.limit], start=1):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        out.append(
            _mk(
                hit.get("title") or hit.get("story_title") or "HN story",
                url,
                hit.get("story_text") or f"{hit.get('points') or 0} points, {hit.get('num_comments') or 0} comments",
                idx,
                published_at=_iso(hit.get("created_at")),
            )
        )
    return out


async def _arxiv(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{ctx.query}",
            "start": "0",
            "max_results": str(ctx.limit),
            "sortBy": "relevance",
        },
    )
    resp.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.content)
    out: list[RawResult] = []
    for entry in root.findall("a:entry", ns)[: ctx.limit]:
        href = None
        for link in entry.findall("a:link", ns):
            if link.get("rel") in (None, "alternate") and link.get("href"):
                href = link.get("href")
                break
        if not href:
            continue
        out.append(
            _mk(
                re.sub(r"\s+", " ", entry.findtext("a:title", default="", namespaces=ns)).strip(),
                href,
                entry.findtext("a:summary", default="", namespaces=ns),
                len(out) + 1,
                published_at=_iso(entry.findtext("a:published", default="", namespaces=ns)),
            )
        )
    return out


def _rss_items(resp: httpx.Response, ctx: EngineContext, engine: str) -> list[RawResult]:
    resp.raise_for_status()
    body = resp.content.strip()
    if not body.startswith((b"<?xml", b"<rss", b"<feed")):
        return []  # 被重定向到 HTML 页面时视为无结果，而不是解析异常
    root = ET.fromstring(body)
    out: list[RawResult] = []
    for item in root.findall(".//item")[: ctx.limit]:
        url = (item.findtext("link") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        desc = item.findtext("description") or ""
        snippet = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        out.append(
            _mk(
                item.findtext("title") or url,
                url,
                snippet,
                len(out) + 1,
                published_at=_iso(item.findtext("pubDate")),
            )
        )
    return out


async def _google_news(ctx: EngineContext) -> list[RawResult]:
    ceid = f"{ctx.region.upper()}:{ctx.language}"
    query = ctx.query
    when = {"day": " when:1d", "week": " when:7d", "month": " when:30d", "year": " when:365d"}.get(
        ctx.time_range, ""
    )
    resp = await ctx.client.get(
        "https://news.google.com/rss/search",
        params={"q": query + when, "hl": f"{ctx.language}-{ctx.region.upper()}", "gl": ctx.region.upper(), "ceid": ceid},
    )
    return _rss_items(resp, ctx, "google_news")


async def _bing_news(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://www.bing.com/news/search",
        params={"q": ctx.query, "format": "rss"},
    )
    return _rss_items(resp, ctx, "bing_news")


async def _bing_rss(ctx: EngineContext) -> list[RawResult]:
    """Bing 官方搜索 RSS（format=rss），免 Key、返回标准 XML。"""
    resp = await ctx.client.get(
        "https://www.bing.com/search",
        params={"q": ctx.query, "format": "rss", "count": str(ctx.limit)},
    )
    return _rss_items(resp, ctx, "bing_rss")


# ------------------------------------------------------------------ HTML 结果页
async def _bing_html(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://www.bing.com/search",
        params={"q": ctx.query, "count": str(ctx.limit), "setlang": ctx.language},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[RawResult] = []
    for node in soup.select("li.b_algo")[: ctx.limit]:
        a = node.select_one("h2 a")
        cap = node.select_one(".b_caption p") or node.select_one("p")
        if not a or not a.get("href", "").startswith(("http://", "https://")):
            continue
        out.append(_mk(a.get_text(" ", strip=True), a["href"], cap.get_text(" ", strip=True) if cap else "", len(out) + 1))
    return out


async def _baidu_html(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get(
        "https://www.baidu.com/s",
        params={"wd": ctx.query, "rn": str(ctx.limit)},
    )
    resp.raise_for_status()
    if "wappass.baidu.com" in str(resp.url) or "captcha" in resp.text[:2000].lower():
        return []  # 触发验证码，视为本引擎无结果
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[RawResult] = []
    for node in soup.select(".result, .c-container")[: ctx.limit]:
        a = node.select_one("h3 a")
        cap = node.select_one(".c-abstract") or node.select_one(".c-span-last")
        href = a.get("href") if a else ""
        if not href or not href.startswith(("http://", "https://")):
            continue
        out.append(
            _mk(
                a.get_text(" ", strip=True),
                href,
                cap.get_text(" ", strip=True) if cap else "",
                len(out) + 1,
                domain="baidu.com",
                score=max(0.75 - 0.03 * len(out), 0.5),
            )
        )
    return out


async def _so360_html(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get("https://www.so.com/s", params={"q": ctx.query})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[RawResult] = []
    for node in soup.select(".res-list")[: ctx.limit * 2]:
        a = node.select_one("h3 a")
        if not a:
            continue
        href = a.get("data-mdurl") or a.get("href") or ""
        if not href.startswith(("http://", "https://")):
            continue
        cap = node.select_one(".res-desc") or node.select_one("p")
        out.append(
            _mk(
                a.get_text(" ", strip=True),
                href,
                cap.get_text(" ", strip=True) if cap else "",
                len(out) + 1,
            )
        )
        if len(out) >= ctx.limit:
            break
    return out


async def _mojeek(ctx: EngineContext) -> list[RawResult]:
    resp = await ctx.client.get("https://www.mojeek.com/search", params={"q": ctx.query})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[RawResult] = []
    for node in soup.select("li.results-standard, .result")[: ctx.limit]:
        a = node.select_one("h2 a") or node.select_one("a.ob")
        cap = node.select_one("p.s") or node.select_one("p")
        if not a or not a.get("href", "").startswith(("http://", "https://")):
            continue
        out.append(_mk(a.get_text(" ", strip=True), a["href"], cap.get_text(" ", strip=True) if cap else "", len(out) + 1))
    return out


ENGINES: dict[str, Callable[[EngineContext], Awaitable[list[RawResult]]]] = {
    "ddg_html": _ddg_html,
    "ddg_lite": _ddg_lite,
    "ddg_instant": _ddg_instant,
    "wikipedia": _wikipedia,
    "bing_html": _bing_html,
    "bing_rss": _bing_rss,
    "so360_html": _so360_html,
    "baidu_html": _baidu_html,
    "mojeek": _mojeek,
    "searx": _searx,
    "hn": _hn,
    "arxiv": _arxiv,
    "google_news": _google_news,
    "bing_news": _bing_news,
}
