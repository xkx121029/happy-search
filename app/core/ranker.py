"""多源融合排序：RRF 粗排 -> 加权精排 -> 去重/过滤 -> 结构化结果。"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from ..config import settings
from .models import RawResult, tokenize

SOURCE_WEIGHTS = {"web": 1.0, "local_index": 0.95, "knowledge_base": 0.9}
AUTHORITY = {
    "arxiv.org": 0.95,
    "github.com": 0.9,
    "wikipedia.org": 0.9,
    "docs.python.org": 0.9,
    "developer.mozilla.org": 0.9,
    "nature.com": 0.95,
    "science.org": 0.95,
}
_CUTOFF_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _round(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 4)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def normalize_url(url: str) -> str:
    if not url or url.startswith("local://"):
        return url
    try:
        p = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(query), ""))
    except ValueError:
        return url


def _dedup_key(item: RawResult) -> str:
    url = normalize_url(item.url)
    if url and not url.startswith("local://"):
        return "u:" + url
    return "c:" + hashlib.md5((item.title + item.snippet).encode("utf-8")).hexdigest()


def apply_filters(
    groups: dict[str, list[RawResult]],
    *,
    time_range: str = "all",
    domains: list[str] | None = None,
    safe_search: bool = True,
) -> dict[str, list[RawResult]]:
    domains = domains or []
    include = [d.lstrip(".").lower() for d in domains if not d.startswith("-")]
    exclude = [d[1:].lstrip(".").lower() for d in domains if d.startswith("-")]
    cutoff = None
    if time_range in _CUTOFF_DAYS:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_CUTOFF_DAYS[time_range])
    terms = settings.safe_search_terms

    out: dict[str, list[RawResult]] = {}
    for source, items in groups.items():
        kept: list[RawResult] = []
        for item in items:
            domain = (item.domain or "").lower()
            if include and not any(domain.endswith(d) for d in include):
                continue
            if exclude and any(domain.endswith(d) for d in exclude):
                continue
            if cutoff and item.published_at:
                dt = _parse_dt(item.published_at)
                if dt and dt < cutoff:
                    continue
            if safe_search and terms:
                blob = (item.title + " " + item.snippet).lower()
                if any(t in blob for t in terms):
                    continue
            kept.append(item)
        out[source] = kept
    return out


def _freshness(published_at: str | None) -> float:
    dt = _parse_dt(published_at)
    if not dt:
        return 0.5
    days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    return _round(math.exp(-days / 180.0))


def _authority(domain: str | None) -> float:
    if not domain:
        return 0.6
    d = domain.lower()
    for key, value in AUTHORITY.items():
        if d == key or d.endswith("." + key):
            return value
    return 0.6


def _highlight(snippet: str, query: str) -> list[str]:
    if not snippet:
        return []
    tokens = sorted({t for t in tokenize(query) if len(t) >= 2}, key=len, reverse=True)[:5]
    text = snippet
    matched = False
    for token in tokens:
        pattern = re.compile(re.escape(token), re.IGNORECASE)
        if pattern.search(text):
            text = pattern.sub(lambda m: f"<em>{m.group(0)}</em>", text)
            matched = True
    return [text[:300]] if matched else []


def rank(
    groups: dict[str, list[RawResult]],
    query: str,
    *,
    pool_size: int = 50,
) -> list[dict]:
    """返回已排序、已结构化的结果列表（rank 为全局排名）。"""
    fused: dict[str, dict] = {}
    for source, items in groups.items():
        weight = SOURCE_WEIGHTS.get(source, 0.8)
        for idx, item in enumerate(items, start=1):
            key = _dedup_key(item)
            entry = fused.setdefault(
                key, {"item": item, "rrf": 0.0, "raw_score": 0.0, "sources": set()}
            )
            entry["rrf"] += weight / (60 + idx)
            entry["raw_score"] = max(entry["raw_score"], item.raw_score)
            entry["sources"].add(source)
            if item.raw_score >= entry["raw_score"]:
                entry["item"] = item

    # 粗排：按 RRF 取候选池
    candidates = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[
        : max(pool_size * 4, 50)
    ]

    # 精排：加权可解释打分
    scored: list[dict] = []
    for entry in candidates:
        item: RawResult = entry["item"]
        relevance = _round(entry["raw_score"])
        freshness = _freshness(item.published_at)
        authority = _authority(item.domain)
        source_weight = max(
            (SOURCE_WEIGHTS.get(s, 0.8) for s in entry["sources"]), default=0.8
        )
        score = _round(
            0.6 * relevance + 0.2 * freshness + 0.1 * authority + 0.1 * source_weight
        )
        scored.append(
            {
                "rank": 0,
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
                "source": item.source,
                "score": score,
                "score_breakdown": {
                    "relevance": relevance,
                    "freshness": freshness,
                    "authority": authority,
                    "source_weight": source_weight,
                },
                "published_at": item.published_at,
                "domain": item.domain,
                "content_type": item.content_type,
                "highlights": _highlight(item.snippet, query),
                "engine": item.extra.get("engine"),
                "_doc_content": item.extra.get("content"),
                "_sort_ts": _parse_dt(item.published_at).timestamp()
                if _parse_dt(item.published_at)
                else 0.0,
            }
        )

    scored.sort(key=lambda r: (-r["score"], -r["_sort_ts"]))
    final: list[dict] = []
    for idx, item in enumerate(scored[:pool_size], start=1):
        item.pop("_sort_ts", None)
        item["rank"] = idx
        final.append(item)
    return final
