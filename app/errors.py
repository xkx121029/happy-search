"""统一错误码与异常定义。所有错误最终都映射为统一 JSON 信封。"""
from __future__ import annotations

from typing import Any

# code -> (http_status, retryable, 默认消息)
ERROR_SPECS: dict[str, tuple[int, bool, str]] = {
    "INVALID_REQUEST": (400, False, "请求参数不合法"),
    "UNAUTHORIZED": (401, False, "API Key 缺失或无效"),
    "FORBIDDEN_TARGET": (403, False, "目标地址被禁止（SSRF 防护）"),
    "NOT_FOUND": (404, False, "接口路径不存在"),
    "METHOD_NOT_ALLOWED": (405, False, "HTTP 方法不允许"),
    "PAYLOAD_TOO_LARGE": (413, False, "请求体超过大小限制"),
    "RATE_LIMITED": (429, True, "请求过于频繁，请稍后重试"),
    "INVALID_URL": (400, False, "URL 格式非法或协议不支持"),
    "PAGINATION_LIMIT": (400, False, "翻页超出 1000 条结果池上限"),
    "QUERY_TIMEOUT": (504, True, "检索超时"),
    "SOURCE_UNAVAILABLE": (503, True, "所有检索源均不可用"),
    "FETCH_ALL_FAILED": (502, True, "所有网页抓取均失败"),
    "INDEX_LIMIT": (413, False, "本地索引文档数已达上限"),
    "NOT_IMPLEMENTED": (501, False, "该能力尚未实现"),
    "INTERNAL_ERROR": (500, True, "服务内部异常"),
}

# 抓取单项失败码（出现在 data.failed[].code，不占用顶层错误码）
FETCH_FAILURE_CODES = (
    "INVALID_URL",
    "FORBIDDEN_TARGET",
    "FETCH_TIMEOUT",
    "FETCH_HTTP_ERROR",
    "FETCH_TOO_LARGE",
    "FETCH_PARSE_ERROR",
    "ROBOTS_DISALLOWED",
    "FETCH_NETWORK_ERROR",
)


class SearchError(Exception):
    """业务异常：一定会被渲染成统一 JSON 错误信封。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        status, retryable, default_message = ERROR_SPECS.get(
            code, (500, True, "未知错误")
        )
        self.code = code
        self.http_status = status
        self.retryable = retryable
        self.message = message or default_message
        self.field = field
        self.details = details
        self.retry_after_ms = retry_after_ms
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "details": self.details,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
        }


class FetchFailure(Exception):
    """单个 URL 抓取失败，用于填充 data.failed[]，不影响整体成功。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code if code in FETCH_FAILURE_CODES else "FETCH_NETWORK_ERROR"
        self.message = message
        super().__init__(message)
