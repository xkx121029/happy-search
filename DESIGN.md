# HappySearch —— 面向 AI 调用的搜索引擎系统 · 设计方案 v1

> 目标：构建一个以搜索能力为核心、可被 AI Agent / LLM 程序直接调用的搜索引擎系统。接口统一、JSON 结构稳定、可直接解析使用。

---

## 1. 整体架构与模块划分

```
                        ┌─────────────────────────────┐
  AI Agent / 程序调用 ──▶│  API Gateway（网关层）        │  鉴权、限流、协议适配
                        └──────────┬──────────────────┘
                                   ▼
                        ┌─────────────────────────────┐
                        │  Query Service（查询服务）    │  校验、改写、意图识别
                        └──┬───────────┬──────────────┘
              计划/改写查询 │           │ 原始查询
              （可选RAG）   ▼           ▼
        ┌──────────────────────┐   ┌──────────────────────┐
        │ Retriever 检索模块     │   │ Ranker 排序模块        │
        │ - 多源检索器           │──▶│ - 粗排(RRF/加权)       │
        │   · 本地索引检索       │   │ - 精排(语义/规则)      │
        │   · Web/外部API检索    │   │ - 去重、过滤、聚合     │
        │   · 知识库检索         │   └──────────┬───────────┘
        └──────────┬───────────┘              ▼
                   ▼               ┌─────────────────────────────┐
        ┌──────────────────────┐   │ Aggregator 结果聚合层        │
        │ Indexer 索引模块       │   │ - 多源合并、片段截取          │
        │ - 爬取/摄取管线        │   │ ── 可选 enrich ──┐           │
        │ - 分词、清洗、结构化    │   │ - 结构化为标准 Output Schema │
        │ - 建立倒排+向量索引     │   └──────────┬──────────────────┘
        └──────────────────────┘              ▼
        ┌─────────────────────────────┐  ┌─────────────────────────────┐
        │ Fetcher 网页抓取模块【新增】 │◀─│ Response Assembler           │
        │ - SSRF 校验 / robots        │  │ - 统一信封、错误码、trace_id  │
        │ - 下载 + 正文提取           │  └─────────────────────────────┘
        │ - HTML → 结构化 JSON        │
        └─────────────────────────────┘
```

### 模块职责

| 模块 | 职责 | 关键点 |
|---|---|---|
| **API Gateway** | 对外唯一入口；鉴权（API Key）、限流、超时控制、请求日志 | 无状态，可水平扩展 |
| **Query Service** | 输入校验（JSON Schema）、查询规范化、意图识别（是否需要联网/知识库）、查询改写扩展 | 输出不合法直接返回标准化错误 |
| **Retriever 检索模块** | 按查询计划并行调用各检索源：本地索引（倒排+向量）、外部 Web 搜索、内部知识库 | 多源并发检索，单源失败不影响整体（降级） |
| **Free Engines 免配置引擎集【新增】** | 14 个无需 Key 的搜索源（JSON/RSS/Atom API + HTML 结果页）并发调用、独立超时、失败隔离，内部 RRF 预融合成一个 `web` 候选列表 | 任一引擎成功即可出结果；状态明细透出到 `source_stats.engines` |
| **Ranker 排序模块** | 多源结果合并：RRF（Reciprocal Rank Fusion）粗排 + 规则/语义精排；去重（URL/内容指纹）、过滤敏感结果 | 排序可解释，回传 score 与 source |
| **Aggregator 聚合层** | 片段截取（snippet 生成）、高亮、按域名/时间聚合、控制返回条数 | 保证输出结构 100% 符合 Schema |
| **Indexer 索引模块** | 异步摄取管线：爬取/导入 → 清洗 → 分词 → 建倒排索引 + 向量索引 | 与查询链路解耦，不影响在线延迟 |
| **Fetcher 网页抓取模块【新增】** | 输入 URL → 安全校验 → 下载 → 正文/元数据提取 → **结构化 JSON 返回**；同时为搜索结果提供 `enrich` 联动 | 独立接口 `/v1/fetch`；SSRF 防护、大小/超时/并发限制、部分失败降级 |
| **Response Assembler** | 组装统一响应信封（request_id、elapsed_ms、usage），异常统一映射为错误码 | 任何情况都返回合法 JSON |

### 数据流转（一次请求）

1. 调用方 → 网关：`POST /v1/search`，JSON body。
2. 网关鉴权/限流 → Query Service：Schema 校验失败即返回 `400 INVALID_REQUEST`。
3. Query Service 生成查询计划（目标源列表、top_k、语言/地区参数）。
4. Retriever 并行检索各源（每个源有独立超时，如 3s），其中 `web` 源由 Free Engines 并发调用 N 个免配置引擎并 RRF 预融合；商业 Provider 失败时自动回退到该引擎集。返回统一候选集 `dict[source, list[RawResult]]` 与引擎状态明细。
5. Ranker 合并去重、RRF 打分 → 精排 → 截取 top_n。
6. Aggregator 生成 snippet、补全元数据 → Response Assembler 组装统一 JSON 返回。
7. **（新能力）** 若 `enrich_results=true`，Aggregator 对 Top-N 结果的 URL 并发调用 Fetcher，抓取对应网页并转成结构化 JSON，挂在每条结果的 `page` 字段；也可由调用方直接 `POST /v1/fetch` 单独抓取任意 URL。
8. 全程写 trace 日志，request_id 贯穿，便于 AI 程序排障。

---

## 2. 对外接口设计

### 2.1 接口清单（v1）

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/search` | 核心搜索接口（主推，AI 调用） |
| `GET`  | `/v1/search` | 简单搜索（查询串参数，便于快速验证） |
| `GET`  | `/v1/health` | 健康检查 |
| `GET`  | `/v1/schema` | 动态获取输入/输出 JSON Schema（AI 自发现） |
| `POST` | `/v1/documents` | 索引提交（批量写入自有文档） |
| `POST` | `/v1/fetch`【新增】 | **爬取一个或多个网页，提取正文与元数据，转结构化 JSON 返回** |
| `GET`  | `/v1/fetch?url=...` | 单 URL 快速抓取（便于 AI 工具直接拼 URL 调用） |

### 2.2 调用示例

```http
POST /v1/search HTTP/1.1
Content-Type: application/json
Authorization: Bearer <API_KEY>

{
  "query": "2026年大模型推理优化最新进展",
  "sources": ["web", "local_index"],
  "top_k": 37,
  "page": 1,
  "language": "zh",
  "time_range": "month"
}
```

---

## 3. 统一 JSON 输入规范（Request Schema）

### 3.1 字段定义

**请求体顶层结构（POST /v1/search）：**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | **是** | 检索查询词，1~512 字符，非空 |
| `sources` | string[] | 否 | 检索源列表，默认 `["web","local_index"]`。可选值：`web` / `local_index` / `knowledge_base` |
| `top_k`【可自定义】 | integer | 否 | **AI 自定义返回条数**，默认 `10`，范围 1 ~ 服务端上限（默认 100，`HAPPYSEARCH_MAX_RESULTS` 可配，硬上限 200） |
| `limit`【新增】 | integer | 否 | `top_k` 的别名；只给 `limit` 即等价于 `top_k`，两者同时给出时取较小值 |
| `page` | integer | 否 | 页码，从 1 开始，默认 `1` |
| `page_size`【可自定义】 | integer | 否 | 每页条数，范围同上；**留空时自动等于 `top_k`**（保证只写 `top_k` 就能拿到对应条数）；显式给出时按它分页，但不超过 `top_k` |
| `language` | string | 否 | 结果语言偏好，ISO 639-1（`zh`/`en`...），默认 `zh` |
| `region` | string | 否 | 地区偏好，如 `cn`、`us`，默认 `cn` |
| `time_range` | string | 否 | 时间过滤：`day` / `week` / `month` / `year` / `all`，默认 `all` |
| `safe_search` | boolean | 否 | 安全过滤开关，默认 `true` |
| `include_answer` | boolean | 否 | 是否附带 AI 摘要答案（RAG），默认 `false` |
| `include_raw_content` | boolean | 否 | 是否返回网页正文全文，默认 `false` |
| `domains` | string[] | 否 | 限定/排除域名，元素前缀 `-` 表示排除，如 `["-example.com"]` |
| `filters` | object | 否 | 扩展过滤（日期范围、文档类型、标签等） |
| `enrich_results`【新增】 | boolean | 否 | 是否抓取 Top-N 结果对应的网页并转结构化 JSON 附在 `result.page`，默认 `false` |
| `enrich_limit`【新增】 | integer | 否 | enrich 抓取条数，默认 3，范围 1~10 |
| `engines`【新增】 | string[] | 否 | 按请求指定免配置引擎，如 `["wikipedia","hn"]`；留空用服务端默认集合（14 个），未知名称忽略 |
| `timeout_ms` | integer | 否 | 单次请求服务端总超时，默认 10000，范围 1000~30000 |
| `stream` | boolean | 否 | 是否 SSE 流式返回，默认 `false` |

**校验规则：** 未知字段忽略（前向兼容）；`query` 缺失/为空/超长 → `400 INVALID_REQUEST`；数值越界（含 `top_k` 超过服务端上限）→ 同错误码，并在 `error.field` 标明字段名。

### 3.2 分页方式与自定义返回数量【更新】

- AI 通过 `top_k`（或别名 `limit`）直接指定期望返回条数；`page_size` 未给时自动跟随 `top_k`，因此 `{"query":"...","top_k":37}` 即返回 37 条。
- 服务端按自定义条数放大候选池：融合候选 `max(top_k*2, 20)`、排序池 `max(top_k*5, 50)`；多引擎去重融合补齐，单引擎最多取 50 条。
- 标准 **页码分页**：`page` + `page_size`，响应中 `pagination` 恒定包含 `page`、`page_size`、`requested`（AI 请求条数）、`returned`（实际返回条数）、`total_estimate`、`has_more`。
- 候选不足时 `returned < requested` 仍属成功（`success=true`），AI 依据这两个字段判断是否换查询或换源。
- 单次请求总可回溯结果池上限 **1000 条**（`page * page_size ≤ 1000`），超出返回 `400 PAGINATION_LIMIT`。
- `enrich_limit`（≤10）独立控制抓取网页数量，不随结果条数增长。

---

## 4. 统一 JSON 输出规范（Response Schema）

### 4.1 顶层信封（所有接口统一）

```json
{
  "success": true,
  "request_id": "req_01HX8Z...",
  "timestamp": "2026-11-12T08:30:00Z",
  "elapsed_ms": 842,
  "data": { },
  "error": null,
  "api_version": "1.0"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `success` | boolean | 请求是否成功（业务层面） |
| `request_id` | string | 全链路追踪 ID，AI 程序排障必读 |
| `timestamp` | string | 服务端时间，ISO 8601 UTC |
| `elapsed_ms` | integer | 服务端总耗时 |
| `data` | object\|null | 成功时的业务数据；失败时为 `null` |
| `error` | object\|null | 失败时的错误对象；成功时为 `null` |
| `api_version` | string | API 版本，用于 AI 程序兼容判断 |

### 4.2 成功时 `data` 结构

```json
{
  "query": "原始查询",
  "rewritten_query": "改写后的查询（若有）",
  "results": [
    {
      "rank": 1,
      "title": "结果标题",
      "url": "https://...",
      "snippet": "与查询相关的内容摘要（≤300字符）",
      "source": "web",
      "score": 0.87,
      "score_breakdown": {
        "relevance": 0.9,
        "freshness": 0.8,
        "authority": 0.9
      },
      "published_at": "2026-10-01T00:00:00Z",
      "domain": "arxiv.org",
      "content_type": "text/html",
      "highlights": ["<em>推理优化</em> 相关片段"],
      "engine": "bing_html（结果来源引擎，新增可选字段）",
      "raw_content": "仅 include_raw_content=true 时返回，否则省略",
      "page": "仅 enrich_results=true 时返回：对应网页抓取后的结构化 JSON 对象（同 4.4 items[]）"
    }
  ],
  "answer": {
    "text": "AI 摘要答案（仅 include_answer=true 时返回，否则省略）",
    "citations": [1, 3],
    "model": "rag-model-v1"
  },
  "related_queries": ["大模型KV缓存优化", "投机解码"],
  "pagination": {
    "page": 1,
    "page_size": 37,
    "requested": 37,
    "returned": 37,
    "total_estimate": 128,
    "has_more": true
  },
  "source_stats": {
    "candidates": { "web": 8, "local_index": 4 },
    "failed_sources": [],
    "engines": [
      {"engine": "bing_html", "status": "ok", "count": 10, "elapsed_ms": 655, "error": null},
      {"engine": "ddg_html", "status": "failed", "count": 0, "elapsed_ms": 3003, "error": "ConnectTimeout"}
    ]
  }
}
```

**`pagination` 字段说明：** `requested` = AI 请求的条数（`top_k`/`limit` 归一化后的值），`returned` = 本页实际返回条数；两者恒定存在，供 AI 判断自定义数量是否被满足。`page_size` 为实际每页上限（`page_size` 未给时等于 `top_k`）。

### 4.3 排序依据

- **粗排**：多源各自 Top-N 候选经 RRF 融合：`score_rrf = Σ 1/(60 + rank_i)`。
- **精排加权总分**：`score = 0.6×relevance + 0.2×freshness + 0.1×authority + 0.1×source_weight`。
- `rank` 严格按 `score` 降序，相同 `score` 按 `published_at` 降序。
- `score` 归一化到 0~1，`score_breakdown` 提供可解释分项，供 AI 自行重排。

### 4.4 网页抓取能力 `/v1/fetch`【新增】

**定位**：把任意（合规）网页 URL 转成 AI 可直接消费的结构化 JSON，与搜索结果解耦，也可通过 `enrich_results=true` 在搜索链路内自动联动。

**输入字段（POST /v1/fetch）：**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | string | 二选一必填 | 单个目标 URL，仅允许 `http` / `https` |
| `urls` | string[] | 二选一必填 | 批量目标，最多 10 个，自动去重 |
| `timeout_ms` | integer | 否 | 单 URL 抓取超时，默认 8000，范围 1000~30000 |
| `max_bytes` | integer | 否 | 单页下载字节上限，默认 2097152（2MB），上限 10MB |
| `max_chars` | integer | 否 | 正文文本截断长度，默认 50000，上限 200000 |
| `include_links` | boolean | 否 | 是否返回页面链接列表，默认 `true` |
| `include_images` | boolean | 否 | 是否返回图片列表，默认 `false` |
| `include_metadata` | boolean | 否 | 是否返回 meta/OG/JSON-LD 元数据，默认 `true` |
| `include_raw_html` | boolean | 否 | 是否附带原始 HTML（截断后），默认 `false` |
| `respect_robots` | boolean | 否 | 是否遵循 `robots.txt`，默认 `false`，服务端可全局强制开启 |
| `max_concurrent` | integer | 否 | 批量抓取并发数，默认 5，范围 1~10 |

**输出 `data` 结构：**

```json
{
  "items": [
    {
      "url": "https://example.com/a",
      "final_url": "https://example.com/a",
      "status_code": 200,
      "content_type": "text/html",
      "charset": "utf-8",
      "title": "页面标题",
      "description": "meta description",
      "language": "zh",
      "author": null,
      "published_at": "2026-10-01T00:00:00Z",
      "text": "清洗后的正文纯文本（供 LLM 直接注入上下文）",
      "word_count": 1234,
      "links": [{"text": "锚文本", "url": "https://..."}],
      "images": [],
      "metadata": {"og:title": "...", "canonical": "..."},
      "raw_html": "仅 include_raw_html=true 时返回",
      "warnings": [],
      "fetched_at": "2026-11-12T08:30:00Z",
      "elapsed_ms": 320
    }
  ],
  "failed": [
    {"url": "https://bad.example", "code": "FETCH_TIMEOUT", "message": "抓取超时"}
  ],
  "stats": {"total": 2, "succeeded": 1, "failed": 1}
}
```

**关键约定：**
- 正文提取顺序：`<article>` → `<main>` → 最大文本块 → `<body>` fallback；移除 `script/style/nav/header/footer/aside/form` 后清洗空白。
- 非 HTML 内容（JSON / 纯文本）以原始文本返回并带 `warnings: ["UNSUPPORTED_CONTENT_TYPE"]`；PDF 等二进制不解析。
- **部分失败不算整体失败**：失败项进 `failed` 数组；全部失败才返回 `FETCH_ALL_FAILED`。
- 搜索联动时，抓取结果以 `result.page` 挂载（同 `items[]` 结构，不含 `raw_html`），字段只增不改，老客户端忽略即可。
- 安全：默认拒绝内网/回环/保留 IP、非 http(s) scheme、DNS 解析到私网的地址（防 SSRF）；重定向逐跳复检；限制下载大小与重定向次数。

### 4.5 错误对象与错误码

```json
{
  "success": false,
  "request_id": "req_01HX8Z...",
  "data": null,
  "error": {
    "code": "INVALID_REQUEST",
    "message": "字段 query 缺失或为空",
    "field": "query",
    "details": { "constraint": "required, 1-512 chars" },
    "retryable": false,
    "retry_after_ms": null
  },
  "api_version": "1.0"
}
```

| 错误码 | HTTP | retryable | 说明 |
|---|---|---|---|
| `INVALID_REQUEST` | 400 | false | JSON 解析失败/必填缺失/类型错误/越界 |
| `UNAUTHORIZED` | 401 | false | API Key 缺失或无效 |
| `RATE_LIMITED` | 429 | true | 触发限流，遵循 `retry_after_ms` |
| `PAGINATION_LIMIT` | 400 | false | 翻页超出 1000 条上限 |
| `QUERY_TIMEOUT` | 504 | true | 超过 `timeout_ms` 未完成 |
| `SOURCE_UNAVAILABLE` | 503 | true | 所有检索源均失败（部分失败则正常返回并在 `source_stats.failed_sources` 标注） |
| `PAYLOAD_TOO_LARGE` | 413 | false | 请求体超限（默认 64KB） |
| `INTERNAL_ERROR` | 500 | true | 服务内部异常 |
| `INVALID_URL` | 400 | false | URL 格式非法或 scheme 非 http(s) |
| `FORBIDDEN_TARGET` | 403 | false | 目标命中 SSRF 防护（内网/回环/保留地址） |
| `FETCH_ALL_FAILED` | 502 | true | 批量抓取全部失败（详见 `data.failed`） |
| `INDEX_LIMIT` | 413 | false | 本地索引文档数达容量上限（`HAPPYSEARCH_LOCAL_MAX_DOCS`） |
| `NOT_FOUND` | 404 | false | 路径不存在 |
| `NOT_IMPLEMENTED` | 501 | false | 尚未实现的能力开关 |

抓取失败项 `failed[].code` 使用细粒度码：`INVALID_URL` / `FORBIDDEN_TARGET` / `FETCH_TIMEOUT` / `FETCH_HTTP_ERROR` / `FETCH_TOO_LARGE` / `FETCH_PARSE_ERROR` / `ROBOTS_DISALLOWED` / `FETCH_NETWORK_ERROR`。

**异常情况 JSON 表达原则：** 永远返回 HTTP 可达 + 统一信封；`success=false` 时 `data=null`、`error` 必有 `code/message/retryable`；部分源失败**不**算错误，由 `source_stats.failed_sources` 数组承载；`retryable=true` 时给出 `retry_after_ms` 建议。

### 4.6 免配置搜索引擎集【新增】

`web` 源默认 `HAPPYSEARCH_WEB_PROVIDER=auto`：并发调用全部无需 Key 的引擎，各自独立超时与异常隔离，内部按 RRF 预融合为一个候选列表，再与 `local_index` / `knowledge_base` 一起进入 4.3 的全局排序。

| 引擎 | 类型 | 来源 |
|---|---|---|
| `ddg_html` / `ddg_lite` | HTML | DuckDuckGo HTML / Lite |
| `ddg_instant` | JSON API | DuckDuckGo Instant Answer |
| `wikipedia` | JSON API | MediaWiki `list=search` |
| `bing_html` | HTML | Bing 网页结果页 |
| `bing_rss` | RSS API | `bing.com/search?format=rss` |
| `so360_html` | HTML | 360 搜索 |
| `baidu_html` | HTML | 百度搜索（验证码页自动视为空） |
| `mojeek` | HTML | Mojeek 独立索引 |
| `searx` | JSON API | SearXNG 公共/自建实例 |
| `hn` | JSON API | Hacker News Algolia API |
| `arxiv` | Atom API | arXiv 官方 API |
| `google_news` | RSS API | Google News RSS |
| `bing_news` | RSS API | Bing News RSS |

**引擎级状态**：`source_stats.engines[]` 每项为 `{engine, status, count, elapsed_ms, error}`，`status ∈ {ok, empty, failed}`；失败只记录不中断。结果级 `results[].engine` 标明来源；多引擎命中同一 URL 时去重并累加 RRF 贡献，取贡献最高者标记。

**可控性**：服务端用 `HAPPYSEARCH_FREE_ENGINES` 裁剪、`HAPPYSEARCH_FREE_TIMEOUT` 控制单引擎超时、`HAPPYSEARCH_SEARX_URLS` 指定 SearXNG 实例、`HAPPYSEARCH_LOCAL_MAX_DOCS` 限制本地索引容量；调用方可用 `SearchRequest.engines`（POST 数组 / GET 逗号串）按请求覆盖。商业 Provider（`tavily` / `serper` / `bing`）失败时自动回退该引擎集。只要任一引擎成功即 `success=true`；全部引擎失败且无其他源才返回 `SOURCE_UNAVAILABLE`；**仅部分源失败但仍有结果时** `success=true` 且 `data.degraded=true`（新增，向后兼容），便于 AI 判断结果完整度。

---

## 5. 适用场景与调用方式

### 5.1 适用场景
1. **AI Agent 工具调用**：作为 LLM Function Calling / MCP Tool 的 search 工具。
2. **RAG 知识增强**：`include_answer=true` 或检索结果直接注入上下文。
3. **企业内部知识检索**：`local_index` + `knowledge_base` 源检索自有文档。
4. **信息聚合监控**：定时查询 + 时间过滤，用于舆情/动态跟踪。
5. **网页内容结构化【新增】**：给定 URL 爬取正文/元数据转 JSON，用于深度阅读、引用溯源、构建索引语料。
6. **零配置联网检索【新增】**：无需任何 API Key，多引擎自动聚合与降级，适合 Agent 开箱即用、跨网络环境的容灾检索。

### 5.2 调用方式
- **REST**：`POST /v1/search`（Bearer Token 鉴权）。
- **MCP Server**：暴露 `search` 工具，输入输出即上述 Schema（v1 兼容封装）。
- **SSE 流式**：`stream=true` 时按 `data: {json}\n\n` 逐块推送 results，最后一块为 `final`。
- **客户端 SDK**：Python / TypeScript（后续版本提供）。

### 5.3 关键约束

| 约束项 | 默认值 | 说明 |
|---|---|---|
| 单请求超时 | 10s（可传 `timeout_ms`，上限 30s） | 超时返回 `QUERY_TIMEOUT` |
| 并发限制 | 10 QPS / Key，突发 20 | 超限 `RATE_LIMITED` |
| 结果数量上限【更新】 | AI 自定义 `top_k`/`limit`/`page_size`，默认上限 100（`HAPPYSEARCH_MAX_RESULTS` 可配，硬上限 200） | 翻页总池 ≤ 1000；`enrich_limit` 抓取条数 ≤ 10 |
| 免配置引擎【新增】 | 默认 14 个并发，单引擎 3s 超时 | 请求级 `engines` 最多 20 个；未知名称忽略 |
| 请求体大小 | ≤ 64KB | 超限 `PAYLOAD_TOO_LARGE` |
| 检索源超时 | 单源 3s | 单源失败降级不影响整体 |
| 抓取 URL 数量 | 单请求 ≤ 10 个 | 并发 ≤ 10，逐 URL 独立超时 |
| 抓取下载上限 | 2MB / 页（可配置，≤ 10MB） | 超限截断并给出 warning |
| 抓取正文长度 | 50000 字符（≤ 200000） | 防止响应体过大撑爆 LLM 上下文 |
| 响应结构稳定性 | Schema 版本化 | 新增字段只增不改语义；删除/变更需升级 `api_version` |

### 5.4 结构稳定性保障（供 AI 程序安全解析）
1. 输出严格遵循发布到 `/v1/schema` 的 JSON Schema（可下载做本地校验）。
2. **字段只加不删**：新增字段总是可选；AI 程序按"存在才读"原则解析。
3. `success` + `error.code` 是唯一可靠分支判断依据，不要依赖 HTTP 状态码以外的主观推断。
4. 数组类型字段（`results`）无结果时返回 `[]` 而非 `null`。
5. `request_id` 贯穿日志，任何异常可凭其排查。

---

## 6. 技术选型建议（实现阶段）

| 层 | 选型 |
|---|---|
| 服务框架 | Python FastAPI（异步、自动生成 OpenAPI）或 Node.js |
| 索引引擎 | 本地：`meilisearch`（开箱即用倒排）；向量：`chroma`/`qdrant` |
| 外部检索 | 默认 `auto`：14 个免配置引擎（DDG / Wikipedia / Bing HTML+RSS / 360 / 百度 / Mojeek / SearXNG / HN / arXiv / 新闻 RSS）并发聚合；另可切 Tavily / Serper / Bing 商业 API，失败自动回退 |
| 网页抓取 | `httpx` 流式下载 + `BeautifulSoup` 正文/元数据提取 + SSRF 防护（`ipaddress` + DNS 复检） |
| 缓存 | Redis（查询结果缓存 5~15min，降低延迟与配额消耗） |
| 队列 | 索引摄取走异步任务（`celery`/内置 worker） |
| 部署 | Docker Compose 单机起步，模块化便于拆分 |

---

## 7. 里程碑（实现阶段规划）

1. **M1（已实现）**：FastAPI 骨架 + 统一信封/错误码 + `/v1/search` 多源检索 + RRF 排序 + 分页 + 统一 Schema 自发现。
2. **M1（已实现）**：`/v1/fetch` 网页抓取 → 结构化 JSON，含 SSRF 防护、批量并发、部分失败降级；搜索 `enrich_results` 联动。
3. **M1（已实现）【新增】**：14 个免配置搜索引擎集成，并发聚合 + 引擎级失败隔离 + 请求级 `engines` 指定 + 商业 API 自动回退。
4. **M2**：Meilisearch/Qdrant 持久化索引、Redis 缓存、真正的 LLM 摘要。
5. **M3**：MCP Server 封装 + Python/TS SDK + Docker Compose 部署。

---

*当前状态：网页抓取、14 个免配置引擎聚合、AI 自定义返回条数、鲁棒性加固（本地索引原子写入/锁/容量上限、DNS 异步解析、引擎异常隔离、部分失败 `degraded` 信号）均已实现；22 项离线契约测试全部通过，真实网络验证 14 引擎中 6 个可用并成功融合返回结果。*

## 8. M1 交付清单

| 交付物 | 路径 |
|---|---|
| 统一 Schema / 错误码 / 信封 | `app/schemas.py`、`app/errors.py`、`app/util.py` |
| API 网关（鉴权/限流/超时/异常统一渲染） | `app/main.py`、`app/gateway.py` |
| 多源检索 + RRF 排序 + 分页 + 聚合 | `app/services.py`、`app/core/ranker.py`、`app/providers/*` |
| **14 个免配置搜索引擎聚合**【新增】 | `app/providers/free.py`、`app/providers/free_engines.py`、`app/providers/web.py` |
| **网页抓取转结构化 JSON** | `app/core/fetcher.py` + `POST/GET /v1/fetch` |
| 引擎状态可观测 / 请求级指定 | `source_stats.engines`、`results[].engine`、`SearchRequest.engines` |
| **AI 自定义返回条数**【新增】 | `SearchRequest.top_k` / `limit` / `page_size`、`pagination.requested` / `returned`、`HAPPYSEARCH_MAX_RESULTS` |
| 搜索结果联动抓取 | `SearchRequest.enrich_results` → `result.page` |
| AI 自发现 Schema、SSE 流式 | `GET /v1/schema`、`stream=true` |
| 运行说明与离线契约测试 | `README.md`、`tests/test_api.py` |
