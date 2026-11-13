"""离线可运行的接口契约测试（不依赖外网）。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import fetcher
from app.core.models import RawResult
from app.errors import FetchFailure
from app.gateway import RateLimiter
from app.providers import free, web
from app.providers.local import LocalIndex

client = TestClient(main.app)
REAL_ASSERT = fetcher._assert_safe_target
REAL_WEB_SEARCH = web.search
LAST_WEB_KWARGS: dict = {}

SAMPLE_HTML = """
<!doctype html><html lang="zh-CN"><head>
<title>大模型推理优化指南</title>
<meta name="description" content="KV 缓存与投机解码实践">
<meta property="article:published_time" content="2026-10-01T00:00:00Z">
<meta name="author" content="HappySearch Team">
</head><body><nav>导航</nav>
<article><h1>大模型推理优化指南</h1>
<p>本文讨论 KV 缓存、量化与投机解码的最新进展。</p>
<a href="/next">下一篇</a></article>
<script>var x=1;</script></body></html>
"""

ENVELOPE_KEYS = {
    "success", "request_id", "timestamp", "elapsed_ms", "data", "error", "api_version"
}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "limiter", RateLimiter(10000, 20000))
    local = LocalIndex(str(tmp_path / "documents.json"))
    local.add(
        [
            {
                "title": "KV 缓存优化笔记",
                "content": "大模型推理中的 KV 缓存优化与量化方法总结",
                "url": "https://kb.example.com/kv-cache",
                "tags": ["llm"],
            }
        ]
    )
    monkeypatch.setattr("app.services.index", local)

    async def fake_web(query, limit=10, **kwargs):
        LAST_WEB_KWARGS.clear()
        LAST_WEB_KWARGS.update(kwargs)
        results = [
            RawResult(
                title="大模型推理优化最新进展",
                url="https://arxiv.org/abs/2026.00001",
                snippet="介绍 KV 缓存优化与投机解码的推理加速方案",
                source="web",
                raw_score=0.9,
                rank_in_source=1,
                domain="arxiv.org",
                content_type="text/html",
                extra={"engine": "mock_engine"},
            ),
            RawResult(
                title="推理优化实践",
                url="https://example.com/practice",
                snippet="量化与批处理提升大模型推理吞吐",
                source="web",
                raw_score=0.8,
                rank_in_source=2,
                domain="example.com",
                content_type="text/html",
                extra={"engine": "mock_engine"},
            ),
        ]
        stats = [
            {"engine": "mock_engine", "status": "ok", "count": 2, "elapsed_ms": 1, "error": None}
        ]
        return results, stats

    monkeypatch.setattr(web, "search", fake_web)

    async def fake_download(url, *, timeout_s, max_bytes, respect_robots):
        if "fail.example" in url:
            raise FetchFailure("FETCH_HTTP_ERROR", "HTTP 503")
        return fetcher.DownloadResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8"},
            body=SAMPLE_HTML.encode("utf-8"),
            truncated=False,
        )

    def fake_assert(url):
        if url.startswith("https://example.com") or url.startswith("https://arxiv.org"):
            return url
        if url.startswith("https://mock.example") or url.startswith("https://fail.example"):
            return url
        return REAL_ASSERT(url)

    monkeypatch.setattr(fetcher, "_download", fake_download)
    monkeypatch.setattr(fetcher, "_assert_safe_target", fake_assert)


def test_health_and_schema_contract():
    for path in ("/v1/health", "/v1/schema"):
        resp = client.get(path)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["success"] is True and body["error"] is None
        assert body["api_version"] == "1.0"
        assert body["request_id"].startswith("req_")
    schema = client.get("/v1/schema").json()["data"]
    assert "SearchRequest" in schema["input_schemas"]
    assert "FetchRequest" in schema["input_schemas"]
    assert "INVALID_REQUEST" in schema["error_codes"]


def test_invalid_request():
    resp = client.post("/v1/search", json={})
    assert resp.status_code == 400
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is False and body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["retryable"] is False
    assert body["error"]["field"] == "query"


def test_search_success_structure():
    resp = client.post(
        "/v1/search",
        json={"query": "大模型推理优化", "top_k": 5, "sources": ["web", "local_index"]},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert isinstance(data["results"], list) and data["results"]
    first = data["results"][0]
    for key in ("rank", "title", "url", "snippet", "source", "score", "score_breakdown", "highlights"):
        assert key in first
    assert set(first["score_breakdown"]) == {"relevance", "freshness", "authority", "source_weight"}
    assert all(
        data["results"][i]["score"] >= data["results"][i + 1]["score"]
        for i in range(len(data["results"]) - 1)
    )
    assert first["engine"] == "mock_engine"
    assert "page" not in first and "raw_content" not in first
    assert data["pagination"]["page"] == 1
    assert "failed_sources" in data["source_stats"]
    assert data["source_stats"]["engines"][0]["engine"] == "mock_engine"
    assert data["related_queries"] == []


def test_pagination_and_get_search():
    resp = client.get("/v1/search", params={"query": "推理优化", "page": 2, "page_size": 3})
    assert resp.status_code == 200
    assert resp.json()["data"]["pagination"]["page"] == 2
    resp = client.post("/v1/search", json={"query": "x", "page": 101, "page_size": 50})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PAGINATION_LIMIT"


def test_fetch_to_json():
    resp = client.post(
        "/v1/fetch",
        json={"url": "https://example.com/article", "include_links": True, "include_metadata": True},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    item = data["items"][0]
    assert item["title"] == "大模型推理优化指南"
    assert "KV 缓存" in item["text"] and "导航" not in item["text"]
    assert "var x=1" not in item["text"]
    assert item["metadata"]["description"] == "KV 缓存与投机解码实践"
    assert item["metadata"]["article:published_time"] == "2026-10-01T00:00:00Z"
    assert item["language"] == "zh-CN" and item["author"] == "HappySearch Team"
    assert item["links"][0]["url"] == "https://example.com/next"
    assert item["word_count"] > 0 and item["status_code"] == 200
    assert "raw_html" not in item
    assert data["failed"] == [] and data["stats"]["succeeded"] == 1


def test_fetch_batch_partial_failure():
    resp = client.post(
        "/v1/fetch",
        json={"urls": ["https://example.com/a", "https://fail.example/x"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    data = body["data"]
    assert body["success"] is True
    assert data["stats"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert data["failed"][0]["code"] == "FETCH_HTTP_ERROR"


def test_fetch_all_failed_and_ssrf():
    resp = client.post("/v1/fetch", json={"url": "https://fail.example/x"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "FETCH_ALL_FAILED"
    assert resp.json()["error"]["retryable"] is True

    resp = client.get("/v1/fetch", params={"url": "http://127.0.0.1:8000/admin"})
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["failed"][0]["code"] == "FORBIDDEN_TARGET"

    resp = client.post("/v1/fetch", json={"url": "file:///etc/passwd"})
    assert resp.status_code == 502
    assert resp.json()["error"]["details"]["failed"][0]["code"] == "INVALID_URL"


def test_search_enrich_attaches_page_json():
    resp = client.post(
        "/v1/search",
        json={
            "query": "推理优化",
            "sources": ["web"],
            "top_k": 2,
            "enrich_results": True,
            "enrich_limit": 2,
        },
    )
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert results[0]["page"]["text"].startswith("大模型推理优化指南")
    assert "raw_html" not in results[0]["page"]


def test_documents_and_local_search():
    resp = client.post(
        "/v1/documents",
        json={
            "documents": [
                {"title": "自定义文档", "content": "关于向量检索与混合召回的说明", "tags": ["rag"]}
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["indexed"] == 1

    resp = client.post(
        "/v1/search", json={"query": "向量检索", "sources": ["local_index"], "top_k": 5}
    )
    data = resp.json()["data"]
    assert data["results"] and data["results"][0]["title"] == "自定义文档"


def test_not_found_is_standard_json():
    resp = client.get("/v1/not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["error"]["code"] == "NOT_FOUND"


def test_stream_events():
    with client.stream("GET", "/v1/search", params={"query": "推理", "stream": "true", "sources": "web"}) as resp:
        assert resp.status_code == 200
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data: ")]
    assert len(lines) >= 2
    assert '"event": "result"' in lines[0]
    assert '"event": "final"' in lines[-1]


def _raw(url: str, title: str, score: float = 0.8) -> RawResult:
    return RawResult(
        title=title,
        url=url,
        snippet=title + " 摘要",
        source="web",
        raw_score=score,
        rank_in_source=1,
        domain=url.split("/")[2],
        content_type="text/html",
    )


def test_free_engine_fusion_and_failure_isolation(monkeypatch):
    async def engine_a(ctx):
        return [_raw("https://a.example/x", "A", 0.9), _raw("https://b.example/y", "B", 0.7)]

    async def engine_b(ctx):
        return [_raw("https://a.example/x", "A", 0.6), _raw("https://c.example/z", "C", 0.6)]

    async def engine_bad(ctx):
        raise RuntimeError("engine down")

    monkeypatch.setattr(
        free,
        "ENGINES",
        {"a": engine_a, "b": engine_b, "bad": engine_bad},
    )
    monkeypatch.setattr(
        free,
        "ENGINE_META",
        {"a": ("A", "api", 1.0), "b": ("B", "api", 1.0), "bad": ("bad", "api", 1.0)},
    )
    results, stats = asyncio.run(
        free.search("q", limit=10, engines=["a", "b", "bad", "unknown"], timeout=1)
    )

    assert [r.url for r in results] == [
        "https://a.example/x",  # 被两个引擎命中，RRF 分最高
        "https://b.example/y",
        "https://c.example/z",
    ]
    assert results[0].extra["engines"] == ["a", "b"]
    assert [r.rank_in_source for r in results] == [1, 2, 3]
    assert all(r.source == "web" for r in results)
    by_engine = {s["engine"]: s for s in stats}
    assert by_engine["a"]["count"] == 2 and by_engine["a"]["status"] == "ok"
    assert by_engine["bad"]["status"] == "failed" and "RuntimeError" in by_engine["bad"]["error"]
    assert "unknown" not in by_engine  # 未知引擎被忽略


def test_request_level_engines_are_forwarded():
    resp = client.post(
        "/v1/search",
        json={"query": "推理优化", "sources": ["web"], "engines": ["wikipedia", "hn"]},
    )
    assert resp.status_code == 200
    assert LAST_WEB_KWARGS["engines"] == ["wikipedia", "hn"]

    resp = client.get("/v1/search", params={"query": "推理", "engines": "arxiv,hn"})
    assert resp.status_code == 200
    assert LAST_WEB_KWARGS["engines"] == ["arxiv", "hn"]


def test_free_engine_catalog_is_discoverable():
    health = client.get("/v1/health").json()["data"]
    names = {item["name"] for item in health["free_engine_catalog"]}
    assert {"ddg_html", "ddg_lite", "ddg_instant", "wikipedia", "bing_html",
            "bing_rss", "so360_html", "baidu_html", "mojeek", "searx", "hn",
            "arxiv", "google_news", "bing_news"} <= names
    assert "wikipedia" in health["free_engines"]
    schema = client.get("/v1/schema").json()["data"]
    assert "wikipedia" in schema["constraints"]["free_engines_available"]
    assert schema["constraints"]["request_level_engines_field"] == "SearchRequest.engines"


def test_commercial_provider_falls_back_to_free(monkeypatch):
    monkeypatch.setattr(web, "settings", SimpleNamespace(web_provider="tavily"))

    async def failing_api(*args, **kwargs):
        raise RuntimeError("api unreachable")

    async def fake_free(query, limit=10, **kwargs):
        return [_raw("https://free.example/r", "FREE")], [
            {"engine": "ddg_html", "status": "ok", "count": 1, "elapsed_ms": 1, "error": None}
        ]

    monkeypatch.setattr(web.web_api, "search", failing_api)
    monkeypatch.setattr(web.free, "search", fake_free)
    results, stats = asyncio.run(REAL_WEB_SEARCH("q"))
    assert len(results) == 1 and results[0].url == "https://free.example/r"
    assert stats[0]["engine"] == "tavily" and stats[0]["status"] == "failed"
    assert stats[1]["engine"] == "ddg_html" and stats[1]["status"] == "ok"


def _many_results(query, limit=10, **kwargs):
    n = min(limit, 300)
    return [
        RawResult(
            title=f"结果 {i}",
            url=f"https://count.example/{i}",
            snippet=f"第 {i} 条测试候选",
            source="web",
            raw_score=max(0.95 - 0.001 * i, 0.1),
            rank_in_source=i + 1,
            domain="count.example",
            content_type="text/html",
            extra={"engine": "mock_many"},
        )
        for i in range(n)
    ]


def test_ai_custom_result_count(monkeypatch):
    from app.config import settings

    async def many(query, limit=10, **kwargs):
        return _many_results(query, limit, **kwargs), []

    monkeypatch.setattr(web, "search", many)
    url = "/v1/search"

    # 1) 只给 top_k：page_size 自动跟随，必须真的返回对应条数
    resp = client.post(url, json={"query": "test", "sources": ["web"], "top_k": 37})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["results"]) == 37
    assert [x["rank"] for x in data["results"]] == list(range(1, 38))
    assert data["pagination"] == {
        "page": 1,
        "page_size": 37,
        "requested": 37,
        "returned": 37,
        "total_estimate": 74,
        "has_more": True,
    }

    # 2) limit 别名等价于 top_k
    resp = client.post(url, json={"query": "test", "sources": ["web"], "limit": 23})
    data = resp.json()["data"]
    assert len(data["results"]) == 23
    assert data["pagination"]["requested"] == 23 and data["pagination"]["page_size"] == 23

    # 3) top_k 与 limit 同时给出时取较小值
    resp = client.post(url, json={"query": "test", "sources": ["web"], "top_k": 30, "limit": 12})
    data = resp.json()["data"]
    assert len(data["results"]) == 12 and data["pagination"]["requested"] == 12

    # 4) 显式 page_size 分页，requested 仍为 top_k
    resp = client.post(
        url, json={"query": "test", "sources": ["web"], "top_k": 50, "page_size": 7, "page": 2}
    )
    data = resp.json()["data"]
    assert len(data["results"]) == 7
    assert data["pagination"]["page"] == 2
    assert data["pagination"]["page_size"] == 7
    assert data["pagination"]["requested"] == 50
    assert data["pagination"]["returned"] == 7
    assert data["results"][0]["rank"] == 8

    # 5) GET 同样生效
    resp = client.get(url, params={"query": "test", "sources": "web", "top_k": 9})
    data = resp.json()["data"]
    assert len(data["results"]) == 9 and data["pagination"]["page_size"] == 9

    # 6) 超过服务端上限 -> 标准错误信封
    resp = client.post(url, json={"query": "test", "top_k": 201})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    assert body["error"]["code"] == "INVALID_REQUEST" and body["error"]["field"] == "top_k"

    # 7) 约束可自发现
    health = client.get("/v1/health").json()["data"]
    constraints = client.get("/v1/schema").json()["data"]["constraints"]
    assert health["max_results_per_page"] == settings.max_results_per_page
    assert constraints["top_k_max"] == settings.max_results_per_page
    assert constraints["top_k_hard_max"] == 200
    assert "limit" in constraints["result_count_fields"]


def test_all_engines_failed_vs_no_results(monkeypatch):
    def stat(status):
        return {"engine": "mock", "status": status, "count": 0, "elapsed_ms": 1, "error": None}

    async def all_failed(query, limit=10, **kwargs):
        return [], [dict(stat("failed"), engine="a"), dict(stat("failed"), engine="b")]

    monkeypatch.setattr(web, "search", all_failed)
    resp = client.post("/v1/search", json={"query": "x", "sources": ["web"], "top_k": 37})
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False and body["data"] is None
    assert body["error"]["code"] == "SOURCE_UNAVAILABLE"
    assert len(body["error"]["details"]["engines"]) == 2

    async def all_empty(query, limit=10, **kwargs):
        return [], [stat("empty")]

    monkeypatch.setattr(web, "search", all_empty)
    resp = client.post("/v1/search", json={"query": "x", "sources": ["web"], "top_k": 37})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["results"] == []
    assert data["pagination"]["requested"] == 37
    assert data["pagination"]["returned"] == 0


def test_requested_vs_returned_when_candidates_insufficient(monkeypatch):
    async def few(query, limit=10, **kwargs):
        return _many_results(query, 3, **kwargs)[:3], []

    monkeypatch.setattr(web, "search", few)
    resp = client.post("/v1/search", json={"query": "test", "sources": ["web"], "top_k": 40})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["results"]) == 3
    assert data["pagination"]["requested"] == 40
    assert data["pagination"]["returned"] == 3
    assert data["pagination"]["has_more"] is False


def test_local_index_atomic_save_and_capacity(tmp_path):
    idx = LocalIndex(path=str(tmp_path / "docs.json"), max_documents=2)
    idx.add([{"id": "1", "title": "a", "content": "x"}])
    # 原子写入后文件应存在且可被重新加载
    assert idx._path.exists()
    reloaded = LocalIndex(path=str(tmp_path / "docs.json"))
    assert reloaded.total == 1
    # 达到容量上限还能再写一条，超过则拒绝（防内存耗尽）
    idx.add([{"id": "2", "title": "b", "content": "y"}])
    with pytest.raises(ValueError):
        idx.add([{"id": "3", "title": "c", "content": "z"}])


class _FakeIndex:
    total = 0

    def search(self, query, limit=10, *, source="local_index", tags=None):
        return [
            RawResult(
                title="local",
                url="https://local.example/1",
                snippet="s",
                source="local_index",
                raw_score=0.9,
            )
        ]

    def add(self, documents):
        raise ValueError("本地索引文档数已达上限 2")


def test_documents_index_limit(monkeypatch):
    from app import services

    monkeypatch.setattr(services, "index", _FakeIndex())
    resp = client.post(
        "/v1/documents", json={"documents": [{"title": "t", "content": "c"}]}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "INDEX_LIMIT"


def test_degraded_flag_on_partial_source_failure(monkeypatch):
    from app import services

    async def failing_web(query, limit=10, **kwargs):
        raise RuntimeError("web down")

    monkeypatch.setattr(web, "search", failing_web)
    monkeypatch.setattr(services, "index", _FakeIndex())
    resp = client.post(
        "/v1/search", json={"query": "x", "sources": ["web", "local_index"], "top_k": 5}
    )
    data = resp.json()["data"]
    assert resp.status_code == 200
    assert data["degraded"] is True
    assert data["results"][0]["source"] == "local_index"
    assert "web" in data["source_stats"]["failed_sources"]


def test_free_engine_exception_isolation(monkeypatch):
    from app.providers import free as free_mod

    async def good(ctx):
        return [
            RawResult(
                title="g",
                url="https://g.example/1",
                snippet="s",
                source="web",
                raw_score=0.9,
            )
        ]

    async def bad(ctx):
        raise RuntimeError("bad engine")

    monkeypatch.setattr(free_mod, "ENGINES", {"good": good, "bad": bad})
    items, stats = asyncio.run(free_mod.search("q", limit=10, engines=["good", "bad"]))
    assert len(items) == 1
    assert {s["engine"] for s in stats} == {"good", "bad"}
    assert stats[0]["status"] in ("ok", "failed") and stats[1]["status"] in ("ok", "failed")
