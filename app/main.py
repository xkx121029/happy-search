"""FastAPI 应用入口：中间件 + 路由 + 全局异常渲染。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
接口：POST /v1/search、POST /v1/fetch、POST /v1/index、GET /v1/health、GET /v1/schema
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import services
from .__init__ import __version__
from .config import settings
from .errors import ERROR_SPECS, SearchError
from .gateway import RateLimiter
from .providers.free import enabled_engines
from .schemas import (
    API_VERSION,
    DocumentsRequest,
    FetchRequest,
    ResponseEnvelope,
    SearchRequest,
)
from .util import Timer, envelope, new_request_id

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("happysearch")

app = FastAPI(
    title="HappySearch",
    version=__version__,
    description="面向 AI 程序调用的搜索引擎服务（M1 可运行版）",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)

_limiter = RateLimiter(qps=settings.rate_limit_qps, burst=settings.rate_limit_burst)


def _client_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return "key:" + auth[7:].strip()
    return "ip:" + (request.client.host if request.client else "unknown")


def _error_response(
    code: str,
    request_id: str,
    timer: Timer,
    message: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    retry_after_ms: int | None = None,
    http_status: int | None = None,
) -> JSONResponse:
    status, default_retryable, default_message = ERROR_SPECS.get(code, (500, True, "未知错误"))
    body = envelope(
        success=False,
        request_id=request_id,
        elapsed_ms=timer.elapsed_ms,
        error={
            "code": code,
            "message": message or default_message,
            "field": field,
            "details": details,
            "retryable": default_retryable if retryable is None else retryable,
            "retry_after_ms": retry_after_ms,
        },
    )
    headers = {"X-Request-Id": request_id}
    if retry_after_ms:
        headers["Retry-After"] = str(max(1, retry_after_ms // 1000))
    return JSONResponse(
        status_code=http_status or status, content=body, headers=headers
    )


@app.middleware("http")
async def _gateway(request: Request, call_next: Callable) -> Response:
    """网关层：方法校验 / 鉴权 / 限流 / 体积限制 / 统一信封。"""
    timer = Timer()
    request_id = request.headers.get("x-request-id") or new_request_id()
    request.state.request_id = request_id

    path = request.url.path
    protected = path.startswith("/v1/")
    allow_method = {
        "/v1/search": {"POST"},
        "/v1/fetch": {"POST"},
        "/v1/index": {"POST"},
    }.get(path)

    if protected and allow_method and request.method not in allow_method:
        return _error_response("METHOD_NOT_ALLOWED", request_id, timer)

    if protected and settings.api_key:
        provided = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = provided or auth[7:].strip()
        if provided != settings.api_key:
            return _error_response("UNAUTHORIZED", request_id, timer)

    if protected:
        allowed, wait_ms = _limiter.check(_client_key(request))
        if not allowed:
            return _error_response("RATE_LIMITED", request_id, timer, retry_after_ms=wait_ms)

    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return _error_response("PAYLOAD_TOO_LARGE", request_id, timer)

    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(SearchError)
async def _handle_search_error(request: Request, exc: SearchError) -> JSONResponse:
    return _error_response(
        exc.code,
        getattr(request.state, "request_id", new_request_id()),
        Timer(),
        message=exc.message,
        field=exc.field,
        details=exc.details,
        retry_after_ms=exc.retry_after_ms,
        http_status=exc.http_status,
    )


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(x) for x in first.get("loc", []) if x != "body"]
    field = ".".join(loc) or None
    return _error_response(
        "INVALID_REQUEST",
        getattr(request.state, "request_id", new_request_id()),
        Timer(),
        message=str(first.get("msg", "请求参数不合法"))[:200],
        field=field,
    )


@app.exception_handler(StarletteHTTPException)
async def _handle_http_error(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "UNAUTHORIZED"}.get(
        exc.status_code, "INTERNAL_ERROR"
    )
    return _error_response(
        code,
        getattr(request.state, "request_id", new_request_id()),
        Timer(),
        http_status=exc.status_code,
    )


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unexpected error: %s", exc)
    return _error_response(
        "INTERNAL_ERROR", getattr(request.state, "request_id", new_request_id()), Timer()
    )


# ---------------------------------------------------------------- 路由
@app.get("/", include_in_schema=False)
async def _root() -> dict[str, Any]:
    return envelope(
        success=True,
        request_id=new_request_id(),
        elapsed_ms=0,
        data={
            "name": "HappySearch",
            "version": __version__,
            "api_version": API_VERSION,
            "endpoints": [
                "POST /v1/search",
                "POST /v1/fetch",
                "POST /v1/index",
                "GET /v1/health",
                "GET /v1/schema",
            ],
            "docs": "/docs",
        },
    )


@app.get("/v1/health")
async def _health() -> dict[str, Any]:
    engines = enabled_engines() if settings.web_search_enabled else []
    return envelope(
        success=True,
        request_id=new_request_id(),
        elapsed_ms=0,
        data={
            "status": "ok",
            "version": __version__,
            "api_version": API_VERSION,
            "web_search_enabled": settings.web_search_enabled,
            "web_provider": settings.web_provider,
            "free_engines": engines,
            "local_index_documents": 0 if not settings.web_search_enabled else None,
            "auth_required": bool(settings.api_key),
        },
    )


@app.get("/v1/schema")
async def _schema() -> dict[str, Any]:
    return envelope(
        success=True,
        request_id=new_request_id(),
        elapsed_ms=0,
        data={
            "schema": ResponseEnvelope.model_json_schema(),
            "search_request": SearchRequest.model_json_schema(),
            "fetch_request": FetchRequest.model_json_schema(),
        },
    )


@app.post("/v1/search")
async def _search(req: SearchRequest) -> dict[str, Any]:
    timer = Timer()
    request_id = new_request_id()
    data = await services.execute_search(req)
    return envelope(
        success=True, request_id=request_id, elapsed_ms=timer.elapsed_ms, data=data
    )


@app.post("/v1/fetch")
async def _fetch(req: FetchRequest) -> dict[str, Any]:
    timer = Timer()
    request_id = new_request_id()
    data = await services.execute_fetch(req)
    return envelope(
        success=True, request_id=request_id, elapsed_ms=timer.elapsed_ms, data=data
    )


@app.post("/v1/index")
async def _index(req: DocumentsRequest) -> dict[str, Any]:
    timer = Timer()
    request_id = new_request_id()
    data = await services.execute_documents(req)
    return envelope(
        success=True, request_id=request_id, elapsed_ms=timer.elapsed_ms, data=data
    )
