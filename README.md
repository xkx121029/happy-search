# HappySearch

面向 AI 程序调用的搜索引擎服务（M1 可运行版）：统一 JSON 输入输出，多源检索 + RRF 排序 + **网页抓取转结构化 JSON**。

解决的问题：给大模型 / Agent 一个稳定、可自托管、**零 API Key 即可用**的检索出口。AI 只需发一个 JSON，就能拿到固定结构的结果（含来源、分页、失败明细），需要正文时再用 `/v1/fetch` 把网页抓成结构化 JSON，不必自己处理 14 个搜索源的解析、限流与失败回退。

- 运行环境：Python **3.9+**（开发验证版本 3.9.7）
- 无外部服务依赖：不需要数据库、不需要 Redis、不强制任何搜索 API Key
- 协议：MIT，见 [LICENSE](./LICENSE)

完整设计见 [DESIGN.md](./DESIGN.md)。

## 快速启动

```bash
# 1. 建虚拟环境（可选但推荐）
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后验证：

```bash
curl http://localhost:8000/v1/health
```

打开 `http://localhost:8000/docs` 查看 Swagger；`GET /v1/schema` 返回机器可读的输入/输出 JSON Schema（供 AI 自发现）。

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HAPPYSEARCH_API_KEY` | 空（不鉴权） | 设置后所有 `/v1/*` 需 `Authorization: Bearer <key>`，health/schema 除外 |
| `HAPPYSEARCH_QPS` / `HAPPYSEARCH_BURST` | 10 / 20 | 每 Key 令牌桶限流 |
| `HAPPYSEARCH_MAX_BODY` | 65536 | 请求体字节上限 |
| `HAPPYSEARCH_MAX_RESULTS` | 100 | AI 单次可请求的最大结果条数（硬上限 200，超出返回 `INVALID_REQUEST`） |
| `HAPPYSEARCH_LOCAL_MAX_DOCS` | 50000 | 本地索引文档容量上限（超出返回 `INDEX_LIMIT`） |
| `HAPPYSEARCH_WEB_ENABLED` | true | 关闭后只查本地索引 |
| `HAPPYSEARCH_WEB_PROVIDER` | `auto` | `auto`（免配置引擎聚合）/ `ddg` / `tavily` / `serper` / `bing`；单引擎失败自动回退到免配置聚合 |
| `HAPPYSEARCH_FREE_ENGINES` | 全部 14 个 | 逗号分隔裁剪引擎，或设为 `all` |
| `HAPPYSEARCH_FREE_TIMEOUT` | 3 | 单引擎超时（秒），各引擎并发执行 |
| `HAPPYSEARCH_SEARX_URLS` | 3 个公共实例 | SearXNG 实例列表，逗号分隔（可指向自建实例） |
| `TAVILY_API_KEY` / `SERPER_API_KEY` / `BING_SEARCH_KEY` | 空 | 选用对应商业 Provider 时必填 |
| `HAPPYSEARCH_ALLOW_PRIVATE_NETWORKS` | false | 放开抓取内网地址（仅内网部署时使用） |
| `HAPPYSEARCH_FORCE_RESPECT_ROBOTS` | false | 强制遵循 robots.txt |
| `HAPPYSEARCH_DOCS_PATH` | `data/documents.json` | 本地索引持久化路径 |

## 免配置搜索引擎（默认 `auto`，零 Key）

`web` 源默认**并发调用全部 14 个无需注册、无需 API Key 的引擎**，各自独立超时（默认 3s）、独立隔离失败，再按 RRF 融合成统一候选列表进入排序。只要任意一个引擎成功就返回结果；全部失败才判定 `web` 源不可用。

| 引擎名 | 类型 | 来源 | 备注 |
|---|---|---|---|
| `ddg_html` / `ddg_lite` | HTML | DuckDuckGo HTML / Lite | 免费双端点 |
| `ddg_instant` | JSON API | DuckDuckGo Instant Answer | 官方公开接口，即时答案/相关主题 |
| `wikipedia` | JSON API | MediaWiki `list=search` | 按 `language` 自动选中/英文维基 |
| `bing_html` | HTML | Bing 网页结果页 | 通用网页 |
| `bing_rss` | RSS API | `bing.com/search?format=rss` | 官方标准 XML，免 Key |
| `so360_html` | HTML | 360 搜索 | 国内可达性好 |
| `baidu_html` | HTML | 百度搜索 | 触发验证码时自动视为空结果 |
| `mojeek` | HTML | Mojeek 独立索引 | 部分区域返回 403，自动隔离 |
| `searx` | JSON API | SearXNG 公共实例 | 可指向自建实例 |
| `hn` | JSON API | Hacker News Algolia API | 技术类垂直源 |
| `arxiv` | Atom API | arXiv 官方 API | 论文垂直源 |
| `google_news` | RSS API | Google News RSS | 新闻时效源，支持 `time_range` |
| `bing_news` | RSS API | Bing News RSS | 返回非 XML 时自动视为空 |

**可观测与可控：**
- `data.source_stats.engines`：`[{engine, status, count, elapsed_ms, error}]`，`status` 为 `ok` / `empty` / `failed`，AI 可据此判断数据覆盖度。
- `data.results[].engine`：该条结果来自哪个引擎（多引擎命中时取贡献最高的）。
- 请求级指定引擎：`POST {"query":"...","engines":["wikipedia","hn"]}`。
- 服务端裁剪：`HAPPYSEARCH_FREE_ENGINES=bing_html,bing_rss,wikipedia`；未知引擎名自动忽略并记日志。
- 配置商业 Provider（`tavily` / `serper` / `bing`）且调用失败时，自动回退到免配置引擎聚合，并在 `engines` 里记录失败原因。
- 实时清单：`GET /v1/health` 的 `free_engines`。

> 本机实测（当前网络）：`bing_html`、`bing_rss`、`so360_html`、`baidu_html` 等可用，DDG / Wikipedia / Google News 连接超时；得益于多引擎隔离，仍正常返回 5 条融合排序结果。

## AI 自定义返回结果数量

AI 可完全控制单次返回条数，无需服务端改配置：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `top_k` | int | 10 | **主参数**：期望返回的结果条数，范围 1 ~ `HAPPYSEARCH_MAX_RESULTS`（默认 100，硬上限 200） |
| `limit` | int | 空 | `top_k` 的别名，便于 AI 直接写 `limit`；只给 `limit` 即等价于 `top_k`，两者同时给出时**取较小值** |
| `page_size` | int | 跟随 `top_k` | 每页条数；**留空时自动等于 `top_k`**，所以只写 `top_k: 37` 就会返回 37 条 |

规则与保证：
- `page_size` 显式给出时按 `page_size` 分页，但不会超过 `top_k`（`top_k` 仍是本次总量上限）。
- 服务端按自定义条数放大候选池：融合候选数 = `max(top_k*2, 20)`，排序池 = `max(top_k*5, 50)`；单引擎最多取 50 条，由多引擎去重融合补齐。
- 实际条数可能少于请求值（候选用尽），此时 `success` 仍为 `true`，通过 `data.pagination.returned` 告知 AI。
- 翻页总池仍受 `page * page_size ≤ 1000` 约束，超出返回 `PAGINATION_LIMIT`。
- `enrich_results` 的网页抓取数量由 `enrich_limit`（≤10）独立控制，不随结果条数增长。

响应中 `data.pagination` 新增两个字段（字段只增不改）：

```json
"pagination": {
  "page": 1,
  "page_size": 37,
  "requested": 37,
  "returned": 37,
  "total_estimate": 74,
  "has_more": true
}
```

- `requested`：AI 请求条数（`top_k`/`limit` 归一化后的值）。
- `returned`：本页实际返回条数；`returned < requested` 表示候选不足，AI 可据此决定是否放宽查询或换源。

上限可通过 `GET /v1/schema` 里 `SearchRequest` 的 `top_k` / `limit` / `page_size` 的 `maximum` 字段自发现；超过上限返回标准 `INVALID_REQUEST` 错误信封并带 `error.field`。

## 鲁棒性与故障降级

系统面向不可靠外网与高并发场景设计，关键保护如下：

- **检索源 / 引擎隔离**：单个免配置引擎或单个检索源（`web`/`local_index`/`knowledge_base`）失败，不影响其他源/引擎，也不取消仍在进行的请求。
- **失败信号清晰**：所有检索源均失败返回 `503 SOURCE_UNAVAILABLE`（`error.details.engines` 附带引擎级明细）；仅部分源失败但仍有结果时，响应 `success=true` 且新增 `data.degraded=true`，AI 可据此判断结果完整度。
- **本地索引数据安全**：写入采用「临时文件 + 原子替换」，避免写一半崩溃损坏数据；读写通过锁快照保证并发一致；并设容量上限（`HAPPYSEARCH_LOCAL_MAX_DOCS`，默认 50000），超出返回 `413 INDEX_LIMIT`。
- **SSRF 防护不阻塞事件循环**：网页抓取的 DNS 解析等阻塞调用置于后台线程，高并发下不拖慢整个服务。
- **输入边界收紧**：`query` 1~512 字符、检索源白名单、`domains` ≤20、`content` ≤1MB、`documents` ≤100 条、`timeout_ms` 1~30s、`top_k`/`limit` ≤100、`enrich_limit` ≤10、抓取 URL ≤10。
- **全局兜底**：任意未捕获异常都返回合法 JSON 的 `500 INTERNAL_ERROR`，不向 AI 泄露堆栈。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/search` | 多源检索、排序、分页；`enrich_results=true` 时联动抓取网页 |
| POST | `/v1/fetch` | **爬取网页 → 正文/元数据 → 结构化 JSON** |
| POST | `/v1/index` | 写入本地文档索引 |
| GET | `/v1/schema` | 输入输出 Schema |
| GET | `/v1/health` | 健康检查 |

## 调用示例

搜索：

```bash
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\":\"大模型推理优化\",\"top_k\":5,\"sources\":[\"web\",\"local_index\"]}"
```

AI 自定义返回条数（`top_k` 或别名 `limit`，`page_size` 未给时自动跟随）：

```bash
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\":\"大模型推理优化\",\"top_k\":37}"
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\":\"大模型推理优化\",\"limit\":50,\"page\":2,\"page_size\":10}"
```

按请求指定免配置引擎（只查维基百科与 Hacker News）：

```bash
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\":\"transformer\",\"engines\":[\"wikipedia\",\"hn\"]}"
```

搜索并抓取结果网页（结果里多出 `page` 对象）：

```bash
curl -X POST http://localhost:8000/v1/search -H "Content-Type: application/json" -d "{\"query\":\"大模型推理优化\",\"enrich_results\":true,\"enrich_limit\":3}"
```

抓取网页转 JSON：

```bash
curl -X POST http://localhost:8000/v1/fetch -H "Content-Type: application/json" -d "{\"urls\":[\"https://example.com\"],\"include_links\":true,\"max_chars\":20000}"
```

## 响应约定（AI 解析要点）

```json
{
  "success": true,
  "request_id": "req_...",
  "timestamp": "2026-11-12T08:30:00.000Z",
  "elapsed_ms": 842,
  "data": { "...": "业务数据" },
  "error": null,
  "api_version": "1.0"
}
```

- **任何情况**（含 4xx/5xx/超时/异常）都返回上述结构；判断分支只看 `success` 与 `error.code`。
- `data` 中的数组字段无结果时为 `[]`；可选字段（如 `page`、`raw_content`、`answer`、`engine`）仅在有值/对应开关开启时出现。
- `data.pagination.requested` / `returned` 恒定存在，AI 用它判断自定义返回条数是否被满足。
- 查询本身无结果时 `success=true`、`results=[]`、`returned=0`；若所有免配置引擎都异常则返回 `503 SOURCE_UNAVAILABLE`，并在 `error.details.engines` 附带引擎失败明细，二者可明确区分。
- 部分检索源/部分引擎/部分 URL 失败不算整体失败，见 `source_stats.failed_sources`、`source_stats.engines`、`data.failed`。
- `error.retryable=true` 时参考 `error.retry_after_ms` 重试。

## 关键约束

| 项 | 值 |
|---|---|
| 单请求超时 | 默认 10000ms，最大 30000ms |
| 限流 | 10 QPS / 突发 20（每 Key） |
| 结果条数 | AI 自定义 `top_k` / `limit` / `page_size`，默认上限 100（`HAPPYSEARCH_MAX_RESULTS` 可配，硬上限 200），翻页池 ≤ 1000 |
| 免配置引擎 | 默认 14 个并发，单引擎 3s 超时，可用 `HAPPYSEARCH_FREE_ENGINES` 裁剪；请求级 `engines` 最多 20 个 |
| 抓取批量 | ≤ 10 URL，并发 ≤ 10，单页下载 ≤ 2MB，正文 ≤ 50000 字符 |
| 抓取安全 | 仅 http(s)，默认拒绝内网/回环/保留 IP 与敏感端口（SSRF 防护），重定向逐跳复检 |

## 目录结构

```
app/
  main.py            # API 路由、网关中间件、异常统一渲染
  schemas.py         # 统一 JSON 输入输出 Schema（Pydantic）
  errors.py          # 错误码表与异常
  services.py        # 搜索/抓取/索引业务编排
  gateway.py         # 令牌桶限流
  core/
    fetcher.py       # 网页抓取 -> 结构化 JSON（SSRF、robots、正文提取）
    ranker.py        # RRF 粗排 + 加权精排 + 去重过滤
    models.py        # 内部候选结果模型与分词
  providers/
    web.py           # Web 检索统一门面（auto / 单引擎 / 失败回退）
    free.py          # 免配置引擎并发编排 + RRF 融合 + 状态明细
    free_engines.py  # 14 个免配置引擎实现（API + HTML）
    ddg.py           # DuckDuckGo HTML + Lite 双端点
    web_api.py       # 商业源：Tavily / Serper / Bing
    local.py         # 本地索引源（内存 + JSON 持久化）
tests/test_api.py    # 22 项离线契约测试
```

## 测试

```bash
python -m pytest -q
```

22 项测试全程 mock 外部网络，可离线运行；覆盖统一信封、错误码、分页、抓取、SSRF、多引擎 RRF 融合与失败隔离、商业 API 回退、请求级引擎指定、AI 自定义返回条数（`top_k`/`limit`/`page_size`/`requested`/`returned`/上限）、全引擎失败与查询无结果的区分、本地索引原子写入与容量上限、`INDEX_LIMIT`、部分源失败的 `degraded` 标志。

## 已知边界（M1）

- 默认 `auto` 模式聚合 14 个免配置引擎，其中部分（DDG / Wikipedia / Google News / SearXNG 公共实例）在某些网络不可达或反爬，属正常现象，失败被隔离且状态可见；如需稳定商业 API，配置 `HAPPYSEARCH_WEB_PROVIDER=tavily|serper|bing` 及对应 Key，失败时仍自动回退到免配置引擎。
- HTML 类引擎依赖对方页面结构，站点改版可能导致该引擎 `empty`/`failed`；API 类引擎（RSS / JSON / Atom）稳定性更高，建议用 `HAPPYSEARCH_FREE_ENGINES` 保留可用组合以降低无效请求。
- Web 源整体不可用时：若还有其他源成功，返回正常结果并在 `source_stats.failed_sources` 标注；若所有源都失败才返回 `SOURCE_UNAVAILABLE`。
- `include_answer` 目前是抽取式摘要（拼接 Top3 snippet + 引用），M2 接入真正的 LLM 摘要。
- 本地索引为轻量 token 重叠打分，M2 替换为 Meilisearch + 向量库。
- 抓取不执行 JavaScript；SPA 页面可能只得到骨架 HTML。

## 目录说明

| 路径 | 说明 |
|---|---|
| `app/main.py` | 服务入口（`app.main:app`），路由 + 网关中间件 + 异常统一渲染 |
| `app/schemas.py` | 请求/响应 Pydantic 模型，`/v1/schema` 的 Schema 来源 |
| `app/services.py` | 搜索 / 抓取 / 索引三条链路的业务编排 |
| `app/core/` | 抓取器、RRF 排序、候选模型与分词 |
| `app/providers/` | 本地索引、免配置引擎、DDG、商业 API |
| `tests/test_api.py` | 离线契约测试，不访问外网 |
| `DESIGN.md` | 完整设计方案与里程碑 |
| `data/` | 运行时数据目录（本地索引持久化），不入库，首次写入时自动创建 |

## 依赖与协议

运行时依赖仅 5 个：`fastapi`、`uvicorn`、`pydantic`、`httpx`、`beautifulsoup4`（测试额外用 `pytest`）。上表全部为 MIT / BSD / Apache-2.0 系列宽松协议，不含 GPL 等传染性协议，也不含商用限制，因此本项目采用 **MIT** 协议发布，可自由用于商业闭源项目。

## License

[MIT](./LICENSE)
