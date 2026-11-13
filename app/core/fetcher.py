"""网页抓取模块：URL -> 安全校验 -> 下载 -> 正文/元数据提取 -> 结构化 JSON。

对外接口：fetch_many() / fetch_for_enrich()
安全默认：拒绝非 http(s)、拒绝私网/回环/保留 IP（SSRF 防护）、限制下载大小与重定向。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..errors import FetchFailure
from ..schemas import FetchRequest
from ..util import utc_now

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_BLOCKED_PORTS = {22, 23, 25, 110, 143, 3306, 5432, 6379, 9200, 11211, 27017}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_CONTENT_CLASS_RE = re.compile(
    r"(^|\s|_|-)(content|article|post|entry|markdown|prose|main-body)(\s|$|_|-)", re.I
)


@dataclass
class DownloadResult:
    status_code: int
    final_url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    truncated: bool = False


# ------------------------------------------------------------------ 安全校验
async def _assert_safe_target_async(url: str) -> str:
    """异步安全校验：DNS 解析等阻塞调用放到 worker 线程，避免卡住事件循环。"""
    return await asyncio.to_thread(_assert_safe_target, url)


def _is_forbidden_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _assert_safe_target(url: str) -> str:
    """校验单个 URL；返回规范化 URL。测试可 monkeypatch 此函数以离线运行。"""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise FetchFailure("INVALID_URL", f"URL 协议或主机非法: {url[:120]}")
    if parsed.username or parsed.password:
        raise FetchFailure("INVALID_URL", "URL 不允许携带认证信息")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise FetchFailure("INVALID_URL", "URL 主机为空")
    if settings.allow_private_networks:
        return url
    if host in _BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        raise FetchFailure("FORBIDDEN_TARGET", f"禁止抓取内网主机: {host}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FetchFailure("INVALID_URL", "URL 端口非法") from exc
    if port and port in _BLOCKED_PORTS:
        raise FetchFailure("FORBIDDEN_TARGET", f"禁止抓取敏感端口: {port}")

    addresses: list[str] = []
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror as exc:
            raise FetchFailure("FETCH_NETWORK_ERROR", f"DNS 解析失败: {host}") from exc
    if not addresses:
        raise FetchFailure("FETCH_NETWORK_ERROR", f"DNS 未返回地址: {host}")
    for addr in addresses:
        if _is_forbidden_ip(addr):
            raise FetchFailure("FORBIDDEN_TARGET", f"目标解析到禁止地址: {addr}")
    return url


# ------------------------------------------------------------------ 下载
async def _check_robots(client: httpx.AsyncClient, url: str, timeout: float) -> None:
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url, timeout=timeout)
        if resp.status_code != 200:
            return
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        if not parser.can_fetch(settings.user_agent, url):
            raise FetchFailure("ROBOTS_DISALLOWED", f"robots.txt 禁止抓取: {url}")
    except FetchFailure:
        raise
    except httpx.HTTPError:
        return  # robots 不可达时放行


async def _download(url: str, *, timeout_s: float, max_bytes: int, respect_robots: bool) -> DownloadResult:
    """下载单个页面（手动处理重定向以便逐跳复检 SSRF）。测试可 monkeypatch。"""
    headers = {"User-Agent": settings.user_agent, "Accept": "text/html,*/*;q=0.8"}
    current = await _assert_safe_target_async(url)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s), headers=headers, follow_redirects=False
    ) as client:
        if respect_robots or settings.force_respect_robots:
            await _check_robots(client, current, timeout_s)
        for _ in range(settings.max_redirects + 1):
            async with client.stream("GET", current) as resp:
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECT_CODES and location:
                    current = await _assert_safe_target_async(urljoin(current, location))
                    continue
                if resp.status_code >= 400:
                    raise FetchFailure("FETCH_HTTP_ERROR", f"HTTP {resp.status_code}")
                buf = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes(65536):
                    remaining = max_bytes - len(buf)
                    if remaining <= 0:
                        truncated = True
                        break
                    buf.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                return DownloadResult(
                    status_code=resp.status_code,
                    final_url=str(resp.url),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=bytes(buf),
                    truncated=truncated,
                )
    raise FetchFailure("FETCH_HTTP_ERROR", "重定向次数超过上限")


# ------------------------------------------------------------------ 解析
def _decode(body: bytes, content_type: str) -> tuple[str, str]:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    charset = match.group(1) if match else None
    if not charset:
        head = body[:2048].decode("ascii", errors="ignore")
        m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
        charset = m.group(1) if m else "utf-8"
    try:
        return body.decode(charset, errors="replace"), charset
    except LookupError:
        return body.decode("utf-8", errors="replace"), "utf-8"


def _meta(soup: BeautifulSoup, limit: int = 50) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("itemprop")
        value = tag.get("content")
        if key and value and len(out) < limit:
            out[str(key).lower()] = str(value)[:500]
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    if canonical and canonical.get("href"):
        out["canonical"] = str(canonical.get("href"))[:500]
    return out


def _clean_text(raw: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text))


def parse_document(dl: DownloadResult, url: str, opts: FetchRequest) -> dict[str, Any]:
    """把下载结果解析为结构化 dict（FetchItem 字段集合）。"""
    content_type = dl.headers.get("content-type", "application/octet-stream")
    charset: str | None = None
    warnings: list[str] = []
    if dl.truncated:
        warnings.append("TRUNCATED_BODY")

    raw_text, charset = _decode(dl.body, content_type)
    if opts.include_raw_html and "html" in content_type:
        raw_html: str | None = raw_text[: opts.max_chars]
    else:
        raw_html = None

    metadata: dict[str, Any] = {}
    title = description = language = author = published_at = None
    links: list[dict[str, str]] = []
    images: list[dict[str, str]] = []
    text = ""

    ct = content_type.lower()
    if "json" in ct:
        try:
            parsed_json = json.loads(raw_text)
            text = json.dumps(parsed_json, ensure_ascii=False, indent=2)
        except ValueError:
            text = raw_text
        warnings.append("UNSUPPORTED_CONTENT_TYPE")
    elif any(x in ct for x in ("text/plain", "xml", "javascript", "csv")):
        text = raw_text
        warnings.append("UNSUPPORTED_CONTENT_TYPE")
    elif "html" not in ct:
        text = ""
        warnings.append("UNSUPPORTED_CONTENT_TYPE")
        warnings.append("BINARY_CONTENT")
    else:
        soup = BeautifulSoup(raw_text, "html.parser")
        if opts.include_metadata:
            metadata = _meta(soup)
        language = (soup.html.get("lang") if soup.html else None) or metadata.get("language")
        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title
            else metadata.get("og:title") or metadata.get("title")
        )
        description = metadata.get("description") or metadata.get("og:description")
        author = metadata.get("author")
        published_at = metadata.get("article:published_time") or metadata.get("date")
        if not published_at:
            time_tag = soup.find("time", attrs={"datetime": True})
            if time_tag:
                published_at = time_tag.get("datetime")

        for tag in soup(
            ["script", "style", "noscript", "nav", "header", "footer",
             "aside", "form", "iframe", "svg", "button"]
        ):
            tag.decompose()
        main = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"class": _CONTENT_CLASS_RE})
            or soup.find(attrs={"id": _CONTENT_CLASS_RE})
            or soup.body
            or soup
        )
        text = _clean_text(main.get_text("\n", strip=True))
        if not text:
            warnings.append("NO_MAIN_CONTENT")

        if opts.include_links:
            seen: set[str] = set()
            for a in main.find_all("a", href=True):
                href = urljoin(dl.final_url or url, a["href"])
                if not href.startswith(("http://", "https://")) or href in seen:
                    continue
                seen.add(href)
                links.append({"text": a.get_text(" ", strip=True)[:120], "url": href})
                if len(links) >= 200:
                    break
        if opts.include_images:
            for img in main.find_all("img", src=True):
                images.append(
                    {
                        "alt": str(img.get("alt") or "")[:120],
                        "url": urljoin(dl.final_url or url, img["src"]),
                    }
                )
                if len(images) >= 100:
                    break

    if len(text) > opts.max_chars:
        text = text[: opts.max_chars]
        warnings.append("TRUNCATED_TEXT")

    return {
        "url": url,
        "final_url": dl.final_url or url,
        "status_code": dl.status_code,
        "content_type": content_type.split(";")[0].strip(),
        "charset": charset,
        "title": title,
        "description": description,
        "language": language,
        "author": author,
        "published_at": published_at,
        "text": text,
        "word_count": _word_count(text),
        "links": links,
        "images": images,
        "metadata": metadata,
        "raw_html": raw_html,
        "warnings": warnings,
        "fetched_at": utc_now(),
        "elapsed_ms": 0,
    }


# ------------------------------------------------------------------ 编排
async def fetch_one(url: str, opts: FetchRequest) -> dict[str, Any]:
    start = time.perf_counter()
    await _assert_safe_target_async(url)
    timeout_s = opts.timeout_ms / 1000.0
    try:
        dl = await _download(
            url,
            timeout_s=timeout_s,
            max_bytes=opts.max_bytes,
            respect_robots=opts.respect_robots,
        )
    except FetchFailure:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise FetchFailure("FETCH_TIMEOUT", f"抓取超时（{opts.timeout_ms}ms）") from exc
    except httpx.HTTPError as exc:
        raise FetchFailure("FETCH_NETWORK_ERROR", str(exc)[:200]) from exc

    try:
        item = parse_document(dl, url, opts)
    except Exception as exc:  # noqa: BLE001 - 解析异常必须降级为单项失败
        raise FetchFailure("FETCH_PARSE_ERROR", str(exc)[:200]) from exc
    item["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
    return item


async def fetch_many(targets: list[str], opts: FetchRequest) -> dict[str, Any]:
    """批量抓取：部分失败进 failed[]，不抛异常；全部失败由服务层映射错误码。"""
    semaphore = asyncio.Semaphore(opts.max_concurrent)

    async def guarded(url: str) -> dict[str, Any]:
        async with semaphore:
            return await fetch_one(url, opts)

    outcomes = await asyncio.gather(
        *(guarded(url) for url in targets), return_exceptions=True
    )
    items: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for url, outcome in zip(targets, outcomes):
        if isinstance(outcome, FetchFailure):
            failed.append({"url": url, "code": outcome.code, "message": outcome.message})
        elif isinstance(outcome, Exception):
            failed.append({"url": url, "code": "FETCH_NETWORK_ERROR", "message": str(outcome)[:200]})
        else:
            items.append(outcome)
    return {
        "items": items,
        "failed": failed,
        "stats": {
            "total": len(targets),
            "succeeded": len(items),
            "failed": len(failed),
        },
    }


async def fetch_for_enrich(
    urls: list[str], *, timeout_ms: int = 4000, max_chars: int = 8000
) -> dict[str, dict[str, Any]]:
    """搜索结果联动：返回 {url: 结构化页面 JSON}，失败 URL 不出现在映射中。"""
    if not urls:
        return {}
    opts = FetchRequest(
        urls=urls,
        timeout_ms=max(1000, min(timeout_ms, 30000)),
        max_chars=max(100, min(max_chars, 200000)),
        include_links=False,
        include_images=False,
        include_raw_html=False,
        max_concurrent=min(5, len(urls)),
    )
    data = await fetch_many(opts.targets, opts)
    return {item["url"]: item for item in data["items"]}
