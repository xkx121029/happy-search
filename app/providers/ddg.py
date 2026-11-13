"""Web 检索 Provider：DuckDuckGo（免费、无需 API Key）。

主用 html 端点，失败时自动降级到 lite 端点；生产可替换为 Tavily / SerpAPI / Bing，
只需保持 search() 返回 list[RawResult] 的契约。
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..core.models import RawResult

logger = logging.getLogger("happysearch")
DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
_ENDPOINTS = (DDG_HTML, DDG_LITE)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_DF = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _unwrap_url(href: str) -> str:
    """还原 DDG 跳转链接中的真实 URL。"""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return unquote(target)
    return href


def _domain(url: str) -> str | None:
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None


def _make(title: str, url: str, snippet: str, rank: int) -> RawResult:
    return RawResult(
        title=title or url,
        url=url,
        snippet=snippet[:500],
        source="web",
        raw_score=max(0.95 - 0.04 * (rank - 1), 0.5),
        rank_in_source=rank,
        domain=_domain(url),
        content_type="text/html",
    )


def _parse_html_endpoint(html: str, limit: int) -> list[RawResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[RawResult] = []
    for node in soup.select(".result"):
        a = node.select_one("a.result__a")
        if not a:
            continue
        url = _unwrap_url(a.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        snip_node = node.select_one(".result__snippet")
        snippet = snip_node.get_text(" ", strip=True) if snip_node else ""
        results.append(_make(a.get_text(" ", strip=True), url, snippet, len(results) + 1))
        if len(results) >= limit:
            break
    return results


def _parse_lite_endpoint(html: str, limit: int) -> list[RawResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[RawResult] = []
    for a in soup.select("a.result-link"):
        url = _unwrap_url(a.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        snip = a.find_next("td", class_="result-snippet")
        snippet = snip.get_text(" ", strip=True) if snip else ""
        results.append(_make(a.get_text(" ", strip=True), url, snippet, len(results) + 1))
        if len(results) >= limit:
            break
    return results


async def search(
    query: str,
    limit: int = 10,
    *,
    language: str = "zh",
    region: str = "cn",
    time_range: str = "all",
    timeout: float | None = None,
) -> list[RawResult]:
    """返回候选结果；全部端点异常时抛错，由调用层降级为源失败。"""
    timeout = timeout or settings.web_source_timeout_s
    payload: dict[str, str] = {"q": query, "kl": f"{region}-{language}"}
    if time_range in _DF:
        payload["df"] = _DF[time_range]

    errors: list[str] = []
    empty_but_ok = False
    for endpoint in _ENDPOINTS:
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=_HEADERS, follow_redirects=True
            ) as client:
                resp = await client.post(endpoint, data=payload)
                resp.raise_for_status()
                html = resp.text
            parser = _parse_html_endpoint if endpoint == DDG_HTML else _parse_lite_endpoint
            results = parser(html, limit)
            if results:
                return results
            empty_but_ok = True
        except Exception as exc:  # noqa: BLE001 - 单端点失败需继续降级
            errors.append(f"{endpoint}: {type(exc).__name__}: {str(exc)[:120]}")
            logger.warning("web provider endpoint failed: %s", errors[-1])

    if empty_but_ok and not errors:
        return []
    raise RuntimeError("; ".join(errors) or "web provider 无结果")
