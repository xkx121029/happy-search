"""统一 JSON 输入输出 Schema（Pydantic v2，兼容 Python 3.9+）。

约定：
- 顶层信封字段恒定存在且语义不变；
- data 内新增字段只增不改；可选字段在关闭对应开关时省略；
- 数组字段无数据时返回 []，不返回 null。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import settings

API_VERSION = settings.api_version
VALID_SOURCES = ("web", "local_index", "knowledge_base")


# ---------------------------------------------------------------- 输入：搜索
class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=512, description="检索查询词")
    sources: list[str] = Field(default_factory=lambda: ["web", "local_index"])
    top_k: int = Field(
        default=10,
        ge=1,
        le=settings.max_results_per_page,
        description="AI 自定义期望返回条数；page_size 未给时自动跟随该值",
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=settings.max_results_per_page,
        description="top_k 的别名；只给 limit 时等价于 top_k，同时给出时取较小值",
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: Optional[int] = Field(
        default=None,
        ge=1,
        le=settings.max_results_per_page,
        description="每页条数；留空时自动等于 top_k",
    )
    language: str = Field(default="zh", max_length=8)
    region: str = Field(default="cn", max_length=8)
    time_range: Literal["day", "week", "month", "year", "all"] = "all"
    safe_search: bool = True
    include_answer: bool = False
    include_raw_content: bool = False
    enrich_results: bool = Field(default=False, description="抓取结果网页并转结构化 JSON 挂到 result.page")
    enrich_limit: int = Field(default=3, ge=1, le=10)
    engines: list[str] = Field(
        default_factory=list,
        description="按请求指定免配置引擎（如 ddg_html,wikipedia,hn）；留空用服务端默认集合",
    )
    domains: list[str] = Field(default_factory=list, max_length=20, description="最多 20 个域名过滤")
    filters: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=10000, ge=1000, le=30000)
    stream: bool = False

    @model_validator(mode="after")
    def _normalize_result_count(self) -> "SearchRequest":
        if self.limit is not None:
            if "top_k" in self.model_fields_set:
                self.top_k = min(self.top_k, self.limit)
            else:
                self.top_k = self.limit
        return self

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query 不能为空白字符")
        return v

    @field_validator("sources")
    @classmethod
    def _check_sources(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("sources 不能为空")
        v = [s.strip().lower() for s in v]
        invalid = [s for s in v if s not in VALID_SOURCES]
        if invalid:
            raise ValueError(f"不支持的检索源: {invalid}，可选 {list(VALID_SOURCES)}")
        return list(dict.fromkeys(v))

    @field_validator("domains")
    @classmethod
    def _check_domains(cls, v: list[str]) -> list[str]:
        return [d.strip() for d in v if d and d.strip()]

    @field_validator("engines")
    @classmethod
    def _check_engines(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(x.strip().lower() for x in v if x and x.strip()))[:20]


# ---------------------------------------------------------------- 输入：抓取
class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: Optional[str] = Field(default=None, description="单个目标 URL")
    urls: Optional[list[str]] = Field(default=None, description="批量目标 URL，最多 10 个")
    timeout_ms: int = Field(default=8000, ge=1000, le=30000)
    max_bytes: int = Field(default=2097152, ge=1024, le=10485760)
    max_chars: int = Field(default=50000, ge=100, le=200000)
    include_links: bool = True
    include_images: bool = False
    include_metadata: bool = True
    include_raw_html: bool = False
    respect_robots: bool = False
    max_concurrent: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _check_targets(self) -> "FetchRequest":
        if not self.targets:
            raise ValueError("必须提供 url 或 urls 中的至少一个目标")
        if len(self.targets) > 10:
            raise ValueError("单次最多抓取 10 个 URL")
        return self

    @property
    def targets(self) -> list[str]:
        raw = ([self.url] if self.url else []) + (self.urls or [])
        seen: set[str] = set()
        out: list[str] = []
        for item in raw:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out


# ---------------------------------------------------------------- 输入：索引提交
class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    title: str = Field(min_length=1, max_length=512)
    url: Optional[str] = None
    content: str = Field(min_length=1, max_length=1_000_000, description="正文，最大 1MB")
    tags: list[str] = Field(default_factory=list)
    published_at: Optional[str] = None


class DocumentsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    documents: list[DocumentInput] = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------- 输出
class ErrorDetail(BaseModel):
    code: str
    message: str
    field: Optional[str] = None
    details: Optional[dict[str, Any]] = None
    retryable: bool = False
    retry_after_ms: Optional[int] = None


class ResponseEnvelope(BaseModel):
    """所有接口（含异常）的唯一响应结构。"""

    success: bool
    request_id: str
    timestamp: str
    elapsed_ms: int
    data: Optional[dict[str, Any]]
    error: Optional[dict[str, Any]]
    api_version: str = API_VERSION


class ResultItem(BaseModel):
    rank: int
    title: str
    url: str
    snippet: str
    source: str
    score: float
    score_breakdown: dict[str, float]
    published_at: Optional[str] = None
    domain: Optional[str] = None
    content_type: Optional[str] = None
    highlights: list[str] = Field(default_factory=list)
    engine: Optional[str] = Field(default=None, description="结果来自哪个免配置搜索引擎")
    raw_content: Optional[str] = None
    page: Optional[dict[str, Any]] = None


class Pagination(BaseModel):
    page: int
    page_size: int = Field(description="本页实际条数上限（page_size 未给时等于 top_k）")
    requested: int = Field(description="AI 请求的结果条数（top_k/limit 归一化后的值）")
    returned: int = Field(description="本页实际返回条数；可能小于 requested（候选不足）")
    total_estimate: int
    has_more: bool


class SearchData(BaseModel):
    query: str
    rewritten_query: str
    results: list[ResultItem]
    answer: Optional[dict[str, Any]] = None
    related_queries: list[str] = Field(default_factory=list)
    pagination: Pagination
    source_stats: dict[str, Any]
    degraded: bool = Field(
        default=False,
        description="部分检索源失败但仍有结果；AI 可据此判断结果完整度",
    )


class FetchItem(BaseModel):
    url: str
    final_url: str
    status_code: int
    content_type: str
    charset: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[str] = None
    text: str
    word_count: int
    links: list[dict[str, str]] = Field(default_factory=list)
    images: list[dict[str, str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_html: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    fetched_at: str
    elapsed_ms: int


class FetchFailureItem(BaseModel):
    url: str
    code: str
    message: str


class FetchData(BaseModel):
    items: list[FetchItem]
    failed: list[FetchFailureItem] = Field(default_factory=list)
    stats: dict[str, int]
