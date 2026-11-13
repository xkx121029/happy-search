"""接口契约测试：全部离线运行，不访问外网。

覆盖：统一信封 / 错误码 / 分页 / 抓取 / SSRF / 排序融合 / 多引擎隔离与融合 /
商业 API 回退 / 请求级引擎 / AI 自定义返回条数 / 本地索引原子写入与容量上限。
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import services
from app.config import settings
from app.core import fetcher
from app.core.models import RawResult
from app.errors import FetchFailure, SearchError
from app.main import app, limiter
from app.providers import free, web, web_api
from app.providers.local import LocalIndex
from app.schemas import FetchRequest, SearchRequest


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter._buckets.clear()
    yield


# ---------------------------------------------------------------- 基础

def test_health(client: TestClient) -> None:
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["error"] is None


def test_root(client: TestClient) -> None:
    assert client.get("/").json()["success"] is True


def test_schema_endpoint(client: TestClient) -> None:
    body = client.get("/v1/schema").json()
    assert "SearchRequest" in body["data"]["input_schemas"]
    assert "INVALID_REQUEST" in body["data"]["error_codes"]


def test_not_found_envelope(client: TestClient) -> None:
    resp = client.get("/v1/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"


def test_validation_error_envelope(client: TestClient) -> None:
    resp = client.post("/v1/search", json={"query": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["field"] == "query"


def test_method_not_allowed(client: TestClient) -> None:
    resp = client.delete("/v1/search")
    assert resp.status_code in (405, 404)
    assert resp.json()["success"] is False


# ---------------------------------------------------------------- 搜索链路
class _FakeWeb:
    def __init__(self, items: list[RawResult]) -> None:
        self.items = items

    async def __call__(self, query: str, limit: int = 10, **kwargs):
        return self.items, [{"engine": "fake", "status": "ok", "count": len(self.items), "elapsed_ms": 1, "error": None}]


def _patch_sources(monkeypatch, items: list[RawResult]) -> None:
    monkeypatch.setattr(web, "search", _FakeWeb(items))

    def _no_local(*args, **kwargs):
        return []

    monkeypatch.setattr(services.index, "search", _no_local)


def _item(title: str, url: str, score: float = 0.9) -> RawResult:
    return RawResult(
        title=title,
        url=url,
        snippet=f"{title} 摘要",
        source="web",
        raw_score=score,
        rank_in_source=1,
        domain="example.com",
    )


def test_search_success(client: TestClient, monkeypatch) -> None:
    _patch_sources(monkeypatch, [_item("A", "https://example.com/a"), _item("B", "https://example.com/b")])
    resp = client.post("/v1/search", json={"query": "测试", "sources": ["web"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]["results"]) == 2
    assert body["data"]["results"][0]["rank"] == 1
    assert body["data"]["results"][0]["score_breakdown"]["relevance"] > 0


def test_search_dedup(client: TestClient, monkeypatch) -> None:
    _patch_sources(
        monkeypatch,
        [_item("A", "https://example.com/a"), _item("A2", "https://example.com/a/")],
    )
    body = client.post("/v1/search", json={"query": "测试", "sources": ["web"]}).json()
    assert len(body["data"]["results"]) == 1


def test_search_pagination(client: TestClient, monkeypatch) -> None:
    items = [_item(f"T{i}", f"https://example.com/{i}") for i in range(5)]
    _patch_sources(monkeypatch, items)
    body = client.post("/v1/search", json={"query": "测试", "sources": ["web"], "page": 2, "page_size": 2}).json()
    data = body["data"]
    assert data["pagination"]["page"] == 2
    assert len(data["results"]) == 2
    assert data["results"][0]["rank"] == 3


def test_search_top_k_follows_page_size(client: TestClient, monkeypatch) -> None:
    items = [_item(f"T{i}", f"https://example.com/k{i}") for i in range(30)]
    _patch_sources(monkeypatch, items)
    data = client.post("/v1/search", json={"query": "测试", "sources": ["web"], "top_k": 7}).json()["data"]
    assert len(data["results"]) == 7
    assert data["pagination"]["requested"] == 7
    assert data["pagination"]["returned"] == 7
    assert data["pagination"]["page_size"] == 7


def test_search_limit_alias(client: TestClient, monkeypatch) -> None:
    items = [_item(f"T{i}", f"https://example.com/l{i}") for i in range(20)]
    _patch_sources(monkeypatch, items)
    data = client.post("/v1/search", json={"query": "测试", "sources": ["web"], "limit": 6}).json()["data"]
    assert len(data["results"]) == 6
    assert data["pagination"]["requested"] == 6


def test_search_requested_exceeds_candidates(client: TestClient, monkeypatch) -> None:
    _patch_sources(monkeypatch, [_item("only", "https://example.com/only")])
    data = client.post("/v1/search", json={"query": "测试", "sources": ["web"], "top_k": 20}).json()["data"]
    assert data["pagination"]["requested"] == 20
    assert data["pagination"]["returned"] == 1


def test_search_top_k_upper_bound(client: TestClient) -> None:
    resp = client.post("/v1/search", json={"query": "x", "top_k": 201})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_search_domain_filter(client: TestClient, monkeypatch) -> None:
    items = [
        _item("keep", "https://keep.example.org/x"),
        RawResult(
            title="drop",
            url="https://skip.example.net/y",
            snippet="s",
            source="web",
            raw_score=0.9,
            rank_in_source=2,
            domain="skip.example.net",
        ),
    ]
    _patch_sources(monkeypatch, items)
    data = client.post(
        "/v1/search",
        json={"query": "测试", "sources": ["web"], "domains": ["example.org"]},
    ).json()["data"]
    assert len(data["results"]) == 1
    assert data["results"][0]["domain"] == "keep.example.org"


def test_search_empty_not_error(client: TestClient, monkeypatch) -> None:
    _patch_sources(monkeypatch, [])
    body = client.post("/v1/search", json={"query": "无结果", "sources": ["web"]}).json()
    assert body["success"] is True
    assert body["data"]["results"] == []
    assert body["data"]["pagination"]["returned"] == 0


def test_search_source_unavailable(client: TestClient, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("all engines down")

    monkeypatch.setattr(web, "search", boom)
    resp = client.post("/v1/search", json={"query": "测试", "sources": ["web"]})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "SOURCE_UNAVAILABLE"
    assert body["error"]["retryable"] is True


def test_search_degraded_flag(client: TestClient, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("web down")

    monkeypatch.setattr(web, "search", boom)
    monkeypatch.setattr(services.index, "search", lambda *a, **k: [_item("L", "https://example.com/local")])
    body = client.post("/v1/search", json={"query": "测试", "sources": ["web", "local_index"]}).json()
    assert body["success"] is True
    assert body["data"]["degraded"] is True
    assert "web" in body["data"]["source_stats"]["failed_sources"]


# ---------------------------------------------------------------- 抓取
@pytest.fixture(autouse=True)
def offline_fetch(monkeypatch):
    """抓取测试全部离线：不发真实网络请求，也不做真实 DNS 解析。"""

    async def fake_download(url: str, *, timeout_s: float, max_bytes: int, respect_robots: bool):
        if "bad" in url:
            raise FetchFailure("FETCH_HTTP_ERROR", "HTTP 500")
        html = b"<html lang='zh'><head><title>TT</title><meta name='description' content='DD'></head><body><article><p>Hello world</p><a href='/next'>N</a></article></body></html>"
        return fetcher.DownloadResult(
            status_code=200,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8"},
            body=html,
        )

    monkeypatch.setattr(fetcher, "_download", fake_download)
    yield


def test_fetch_single(client: TestClient) -> None:
    body = client.post("/v1/fetch", json={"url": "https://ok.example.com/page"}).json()
    assert body["success"] is True
    item = body["data"]["items"][0]
    assert item["title"] == "TT"
    assert "Hello world" in item["text"]
    assert item["links"][0]["url"] == "https://ok.example.com/next"


def test_fetch_partial_failure(client: TestClient) -> None:
    body = client.post(
        "/v1/fetch",
        json={"urls": ["https://bad.example.com/x", "https://ok.example.com/y"]},
    ).json()
    assert body["success"] is True
    assert len(body["data"]["items"]) == 1
    assert body["data"]["failed"][0]["code"] == "FETCH_HTTP_ERROR"


def test_fetch_all_failed(client: TestClient) -> None:
    resp = client.post("/v1/fetch", json={"urls": ["https://bad.example.com/1", "https://bad.example.com/2"]})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "FETCH_ALL_FAILED"


def test_fetch_rejects_private_target(client: TestClient) -> None:
    resp = client.post("/v1/fetch", json={"url": "http://127.0.0.1:8000/admin"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_TARGET"


def test_fetch_rejects_bad_scheme(client: TestClient) -> None:
    resp = client.post("/v1/fetch", json={"url": "ftp://example.com/a"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_URL"


def test_fetch_max_urls(client: TestClient) -> None:
    resp = client.post("/v1/fetch", json={"urls": [f"https://e.com/{i}" for i in range(11)]})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 多引擎
class _Resp:
    def __init__(self, text: str = "", payload: dict | None = None, status: int = 200) -> None:
        self.text = text
        self._payload = payload or {}
        self.status_code = status
        self.content = text.encode("utf-8")

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_free_engines_fusion_and_isolation(monkeypatch) -> None:
    async def ok_engine(ctx):
        return [RawResult(title="same", url="https://a.com/1", snippet="s", source="web", raw_score=0.8, rank_in_source=1)]

    async def other_engine(ctx):
        return [RawResult(title="other", url="https://a.com/2", snippet="s", source="web", raw_score=0.8, rank_in_source=1)]

    async def boom_engine(ctx):
        raise RuntimeError("down")

    monkeypatch.setitem(free.ENGINES, "ok1", ok_engine)
    monkeypatch.setitem(free.ENGINES, "ok2", ok_engine)
    monkeypatch.setitem(free.ENGINES, "other", other_engine)
    monkeypatch.setitem(free.ENGINES, "boom", boom_engine)

    results, stats = asyncio.run(
        free.search("q", limit=5, engines=["ok1", "ok2", "other", "boom"])
    )
    urls = [r.url for r in results]
    assert urls[0] == "https://a.com/1"
    assert urls.count("https://a.com/1") == 1
    statuses = {s["engine"]: s["status"] for s in stats}
    assert statuses["boom"] == "failed"
    assert statuses["ok1"] == "ok"


def test_free_engines_all_failed(client: TestClient, monkeypatch) -> None:
    async def boom_engine(ctx):
        raise RuntimeError("down")

    monkeypatch.setitem(free.ENGINES, "boom", boom_engine)
    monkeypatch.setattr(free.settings, "free_engines", ("boom",))
    resp = client.post("/v1/search", json={"query": "x", "sources": ["web"]})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_web_provider_fallback(client: TestClient, monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("tavily down")

    async def fallback(*args, **kwargs):
        return [RawResult(title="fb", url="https://fb.com/1", snippet="s", source="web", raw_score=0.8, rank_in_source=1)], [
            {"engine": "ddg_html", "status": "ok", "count": 1, "elapsed_ms": 1, "error": None}
        ]

    monkeypatch.setattr(web_api, "search", boom)
    monkeypatch.setattr(free, "search", fallback)
    monkeypatch.setattr(web.settings, "web_provider", "tavily")
    monkeypatch.setattr(web.settings, "tavily_api_key", "test-key")
    data = client.post("/v1/search", json={"query": "x", "sources": ["web"]}).json()["data"]
    assert data["results"][0]["url"] == "https://fb.com/1"
    engines = {e["engine"]: e["status"] for e in data["source_stats"]["engines"]}
    assert engines["tavily"] == "failed"
    assert engines["ddg_html"] == "ok"


def test_request_level_engines(client: TestClient, monkeypatch) -> None:
    seen: dict = {}

    async def fake(ctx_list: list):
        return [], []

    async def spy(query, limit=10, engines=None, **kwargs):
        seen["engines"] = engines
        return [], []

    monkeypatch.setattr(web, "search", spy)
    client.post("/v1/search", json={"query": "x", "sources": ["web"], "engines": ["wikipedia", "hn"]})
    assert seen["engines"] == ["wikipedia", "hn"]


# ---------------------------------------------------------------- 本地索引
@pytest.fixture()
def tmp_index(tmp_path, monkeypatch):
    idx = LocalIndex(str(tmp_path / "docs.json"))
    monkeypatch.setattr(services, "index", idx)
    return idx


def test_documents_and_local_search(client: TestClient, tmp_index) -> None:
    resp = client.post(
        "/v1/documents",
        json={"documents": [{"title": "机器学习入门", "content": "梯度下降与反向传播", "tags": ["ml"]}]},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["indexed"] == 1
    data = client.post(
        "/v1/search", json={"query": "梯度下降", "sources": ["local_index"]}
    ).json()["data"]
    assert data["results"][0]["title"] == "机器学习入门"


def test_documents_capacity_limit(client: TestClient, tmp_path, monkeypatch) -> None:
    idx = LocalIndex(str(tmp_path / "cap.json"), max_documents=1)
    monkeypatch.setattr(services, "index", idx)
    client.post("/v1/documents", json={"documents": [{"title": "a", "content": "aaa"}]})
    resp = client.post("/v1/documents", json={"documents": [{"title": "b", "content": "bbb"}]})
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "INDEX_LIMIT"


def test_local_index_atomic_write(tmp_path) -> None:
    path = tmp_path / "docs.json"
    idx = LocalIndex(str(path))
    idx.add([{"id": "x1", "title": "t", "content": "c"}])
    assert path.exists()
    assert list(tmp_path.glob("*.tmp*")) == []
    assert json.loads(path.read_text(encoding="utf-8"))["documents"][0]["id"] == "x1"
