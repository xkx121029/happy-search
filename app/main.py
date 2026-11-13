"""HappySearch API 入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
所有响应（含异常）都遵循 ResponseEnvelope 统一 JSON 结构。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .providers.free import enabled_engines
from .providers.free_engines import ENGINE_META
from .providers.local import index
from .errors import ERROR_SPECS, SearchError
from .gateway import RateLimiter
from .schemas import (
    API_VERSION,
    DocumentsRequest,
    FetchItem,
    FetchRequest,
    ResponseEnvelope,
    ResultItem,
    SearchRequest,
)
from .services import execute_documents, execute_fetch, execute_search
from .util import Timer, envelope, new_request_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("happysearch")

app = FastAPI(
    title="HappySearch",
    description="面向 AI 调用的搜索引擎：统一 JSON 输入输出，支持多源检索与网页抓取转 JSON",
    version=API_VERSION,
)
limiter = RateLimiter(settings.rate_limit_qps, settings.rate_limit_burst)
PUBLIC_PATHS = {"/", "/v1/health", "/v1/schema", "/docs", "/redoc", "/openapi.json"}


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", new_request_id())


def _elapsed(request: Request) -> int:
    timer: Timer | None = getattr(request.state, "timer", None)
    return timer.elapsed_ms if timer else 0


def _ok(request: Request, data: dict[str, Any]) -> dict[str, Any]:
    return envelope(
        success=True, request_id=_rid(request), elapsed_ms=_elapsed(request), data=data
    )


def _error_response(request: Request, error: SearchError) -> JSONResponse:
    body = envelope(
        success=False,
        request_id=_rid(request),
        elapsed_ms=_elapsed(request),
        data=None,
        error=error.as_dict(),
    )
    return JSONResponse(status_code=error.http_status, content=body)


# ------------------------------------------------------------------ 中间件
@app.middleware("http")
async def gateway_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or new_request_id()
    request.state.timer = Timer()
    path = request.url.path
    is_public = path in PUBLIC_PATHS

    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_body_bytes:
        return _error_response(
            request,
            SearchError(
                "PAYLOAD_TOO_LARGE",
                f"请求体超过 {settings.max_body_bytes} 字节",
                details={"max_body_bytes": settings.max_body_bytes},
            ),
        )

    if settings.api_key and not is_public:
        if request.headers.get("authorization", "") != f"Bearer {settings.api_key}":
            return _error_response(request, SearchError("UNAUTHORIZED"))

    if not is_public:
        key = (
            request.headers.get("authorization", "")
            or (request.client.host if request.client else "anonymous")
        )
        allowed, retry_after = limiter.check(key)
        if not allowed:
            return _error_response(
                request,
                SearchError(
                    "RATE_LIMITED",
                    retry_after_ms=retry_after,
                    details={"qps": settings.rate_limit_qps, "burst": settings.rate_limit_burst},
                ),
            )

    try:
        response = await call_next(request)
    except SearchError as exc:
        return _error_response(request, exc)
    except Exception as exc:  # noqa: BLE001 - 兜底：任何异常也必须返回合法 JSON
        logger.exception("unhandled error rid=%s", _rid(request))
        return _error_response(
            request, SearchError("INTERNAL_ERROR", str(exc)[:200])
        )

    response.headers["X-Request-ID"] = _rid(request)
    response.headers["X-API-Version"] = API_VERSION
    return response


# ------------------------------------------------------------------ 异常处理器
@app.exception_handler(SearchError)
async def on_search_error(request: Request, exc: SearchError) -> JSONResponse:
    return _error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    loc = [str(x) for x in first.get("loc", ())]
    if loc and loc[0] in ("body", "query", "path", "header", "cookie"):
        loc = loc[1:]
    field = ".".join(loc) or None
    return _error_response(
        request,
        SearchError(
            "INVALID_REQUEST",
            str(first.get("msg", "请求参数校验失败")),
            field=field,
            details={"errors": len(exc.errors())},
        ),
    )


@app.exception_handler(PydanticValidationError)
async def on_pydantic_error(request: Request, exc: PydanticValidationError) -> JSONResponse:
    first = (exc.errors() or [{}])[0]
    loc = [str(x) for x in first.get("loc", ())]
    return _error_response(
        request,
        SearchError(
            "INVALID_REQUEST",
            str(first.get("msg", "请求参数校验失败")),
            field=".".join(loc) or None,
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "PAYLOAD_TOO_LARGE"}.get(
        exc.status_code, "INVALID_REQUEST"
    )
    return _error_response(request, SearchError(code, str(exc.detail)))


# ------------------------------------------------------------------ 元接口
@app.get("/")
async def root(request: Request) -> dict[str, Any]:
    return _ok(
        request,
        {
            "service": "HappySearch",
            "api_version": API_VERSION,
            "endpoints": ["/v1/search", "/v1/fetch", "/v1/documents", "/v1/schema", "/v1/health"],
        },
    )


@app.get("/v1/health")
async def health(request: Request) -> dict[str, Any]:
    return _ok(
        request,
        {
            "status": "ok",
            "version": API_VERSION,
            "sources": ["web", "local_index", "knowledge_base"],
            "web_provider": settings.web_provider,
            "free_engines": enabled_engines(),
            "free_engine_catalog": [
                {"name": name, "description": meta[0], "type": meta[1], "weight": meta[2]}
                for name, meta in ENGINE_META.items()
            ],
            "indexed_documents": index.total,
            "max_results_per_page": settings.max_results_per_page,
        },
    )


@app.get("/v1/schema")
async def schema(request: Request) -> dict[str, Any]:
    """AI 自发现接口：动态返回输入/输出 JSON Schema 与错误码表。"""
    return _ok(
        request,
        {
            "api_version": API_VERSION,
            "endpoints": {
                "POST /v1/search": "多源检索",
                "GET /v1/search": "查询串快速检索",
                "POST /v1/fetch": "爬取网页并转结构化 JSON",
                "GET /v1/fetch": "单 URL 快速抓取",
                "POST /v1/documents": "提交本地文档到索引",
            },
            "input_schemas": {
                "SearchRequest": SearchRequest.model_json_schema(),
                "FetchRequest": FetchRequest.model_json_schema(),
                "DocumentsRequest": DocumentsRequest.model_json_schema(),
            },
            "output_schema": {
                "envelope": ResponseEnvelope.model_json_schema(),
                "result_item": ResultItem.model_json_schema(),
                "fetch_item": FetchItem.model_json_schema(),
            },
            "error_codes": {
                code: {"http": spec[0], "retryable": spec[1], "message": spec[2]}
                for code, spec in ERROR_SPECS.items()
            },
            "constraints": {
                "timeout_ms_default": 10000,
                "timeout_ms_max": 30000,
                "qps": settings.rate_limit_qps,
                "burst": settings.rate_limit_burst,
                "top_k_max": settings.max_results_per_page,
                "top_k_hard_max": 200,
                "top_k_default": 10,
                "page_size_default": "跟随 top_k（未显式给出时）",
                "result_count_fields": ["top_k", "limit", "page_size"],
                "limit_alias": "limit 等价于 top_k；两者同时给出时取较小值",
                "page_pool_max": 1000,
                "fetch_urls_max": 10,
                "fetch_max_bytes_default": 2097152,
                "max_body_bytes": settings.max_body_bytes,
                "web_provider": settings.web_provider,
                "free_engines_available": list(ENGINE_META.keys()),
                "free_engines_default": enabled_engines(),
                "free_engine_timeout_s": settings.free_timeout_s,
                "request_level_engines_field": "SearchRequest.engines",
            },
        },
    )


# ------------------------------------------------------------------ 搜索接口
def _query_to_search_request(params) -> SearchRequest:
    data: dict[str, Any] = {"query": params.get("query", "")}
    if "sources" in params:
        data["sources"] = [s.strip() for s in params["sources"].split(",") if s.strip()]
    if "domains" in params:
        data["domains"] = [d.strip() for d in params["domains"].split(",") if d.strip()]
    if "engines" in params:
        data["engines"] = [e.strip() for e in params["engines"].split(",") if e.strip()]
    ints = ("top_k", "limit", "page", "page_size", "enrich_limit", "timeout_ms")
    bools = ("safe_search", "include_answer", "include_raw_content", "enrich_results", "stream")
    strs = ("language", "region", "time_range")
    for key in ints:
        if key in params:
            try:
                data[key] = int(params[key])
            except ValueError as exc:
                raise SearchError("INVALID_REQUEST", f"{key} 必须是整数", field=key) from exc
    for key in bools:
        if key in params:
            data[key] = params[key].lower() in ("1", "true", "yes", "on")
    for key in strs:
        if key in params:
            data[key] = params[key]
    return SearchRequest(**data)


async def _respond_search(request: Request, req: SearchRequest):
    if req.stream:
        async def events():
            try:
                data = await execute_search(req)
            except SearchError as exc:
                yield "data: " + json.dumps(
                    {
                        "event": "error",
                        "data": envelope(
                            success=False,
                            request_id=_rid(request),
                            elapsed_ms=_elapsed(request),
                            error=exc.as_dict(),
                        ),
                    },
                    ensure_ascii=False,
                ) + "\n\n"
                return
            for item in data["results"]:
                yield "data: " + json.dumps(
                    {"event": "result", "data": item}, ensure_ascii=False
                ) + "\n\n"
            yield "data: " + json.dumps(
                {"event": "final", "data": _ok(request, data)}, ensure_ascii=False
            ) + "\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-ID": _rid(request)},
        )
    return _ok(request, await execute_search(req))


@app.post("/v1/search")
async def post_search(req: SearchRequest, request: Request):
    return await _respond_search(request, req)


@app.get("/v1/search")
async def get_search(request: Request):
    return await _respond_search(request, _query_to_search_request(request.query_params))


# ------------------------------------------------------------------ 抓取接口
def _query_to_fetch_request(params) -> FetchRequest:
    data: dict[str, Any] = {}
    if "url" in params:
        data["url"] = params["url"]
    if "urls" in params:
        data["urls"] = [u.strip() for u in params["urls"].split(",") if u.strip()]
    ints = ("timeout_ms", "max_bytes", "max_chars", "max_concurrent")
    bools = ("include_links", "include_images", "include_metadata", "include_raw_html", "respect_robots")
    for key in ints:
        if key in params:
            try:
                data[key] = int(params[key])
            except ValueError as exc:
                raise SearchError("INVALID_REQUEST", f"{key} 必须是整数", field=key) from exc
    for key in bools:
        if key in params:
            data[key] = params[key].lower() in ("1", "true", "yes", "on")
    return FetchRequest(**data)


@app.post("/v1/fetch")
async def post_fetch(req: FetchRequest, request: Request):
    return _ok(request, await execute_fetch(req))


@app.get("/v1/fetch")
async def get_fetch(request: Request):
    return _ok(request, await execute_fetch(_query_to_fetch_request(request.query_params)))


# ------------------------------------------------------------------ 索引提交
@app.post("/v1/documents")
async def post_documents(req: DocumentsRequest, request: Request):
    return _ok(request, await execute_documents(req))
