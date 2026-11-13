"""全局配置（全部可用环境变量覆盖，无需改代码）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default_factory=lambda: os.getenv("HAPPYSEARCH_API_KEY") or None)
    api_version: str = "1.0"

    # 网关约束
    rate_limit_qps: int = field(default_factory=lambda: _int("HAPPYSEARCH_QPS", 10))
    rate_limit_burst: int = field(default_factory=lambda: _int("HAPPYSEARCH_BURST", 20))
    max_body_bytes: int = field(default_factory=lambda: _int("HAPPYSEARCH_MAX_BODY", 65536))
    # AI 单次可请求的最大结果条数（默认 100，硬上限 200）
    max_results_per_page: int = field(
        default_factory=lambda: min(200, max(1, _int("HAPPYSEARCH_MAX_RESULTS", 100)))
    )
    web_source_timeout_s: float = 3.0

    # 抓取约束
    allow_private_networks: bool = field(
        default_factory=lambda: _bool("HAPPYSEARCH_ALLOW_PRIVATE_NETWORKS", False)
    )
    force_respect_robots: bool = field(
        default_factory=lambda: _bool("HAPPYSEARCH_FORCE_RESPECT_ROBOTS", False)
    )
    user_agent: str = "HappySearchBot/1.0 (+AI-search; compatible)"
    max_redirects: int = 5

    # 本地索引
    local_docs_path: str = field(
        default_factory=lambda: os.getenv("HAPPYSEARCH_DOCS_PATH", "data/documents.json")
    )
    # 本地索引文档容量上限（防无限写入耗尽内存），可用 HAPPYSEARCH_LOCAL_MAX_DOCS 调整
    local_max_documents: int = field(
        default_factory=lambda: max(1, _int("HAPPYSEARCH_LOCAL_MAX_DOCS", 50000))
    )
    web_search_enabled: bool = field(
        default_factory=lambda: _bool("HAPPYSEARCH_WEB_ENABLED", True)
    )
    # web_provider: auto(免配置引擎聚合，默认) / ddg / tavily / serper / bing
    web_provider: str = field(
        default_factory=lambda: os.getenv("HAPPYSEARCH_WEB_PROVIDER", "auto").strip().lower()
    )
    tavily_api_key: str | None = field(default_factory=lambda: os.getenv("TAVILY_API_KEY") or None)
    serper_api_key: str | None = field(default_factory=lambda: os.getenv("SERPER_API_KEY") or None)
    bing_api_key: str | None = field(default_factory=lambda: os.getenv("BING_SEARCH_KEY") or None)

    # 免配置引擎（全部无需 Key），可用 HAPPYSEARCH_FREE_ENGINES 裁剪，或设为 all
    free_engines: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip().lower()
            for x in os.getenv(
                "HAPPYSEARCH_FREE_ENGINES",
                "ddg_html,ddg_lite,ddg_instant,wikipedia,bing_html,bing_rss,"
                "so360_html,baidu_html,mojeek,searx,hn,arxiv,google_news,bing_news",
            ).split(",")
            if x.strip()
        )
    )
    free_timeout_s: float = field(
        default_factory=lambda: _float("HAPPYSEARCH_FREE_TIMEOUT", 3.0)
    )
    searx_urls: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            x.strip().rstrip("/")
            for x in os.getenv(
                "HAPPYSEARCH_SEARX_URLS",
                "https://searx.be,https://priv.au,https://search.bus-hit.me",
            ).split(",")
            if x.strip()
        )
    )
    safe_search_terms: tuple[str, ...] = ()


settings = Settings()
