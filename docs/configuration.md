# ParseCore 配置手册

本文是 ParseCore 的配置总入口，面向要把产品跑起来、接入宿主系统、或进入灰度/生产验收的用户。配置文件使用 TOML；密钥只通过环境变量注入，不写入配置文件。

## 快速选择

| 使用场景 | 推荐配置 | 执行模式 | 存储/索引 | 典型命令 |
| --- | --- | --- | --- | --- |
| 本地 SDK 或单进程 API | `parsecore.toml` | `inline` | SQLite + 本地对象目录 | `python -m parsecore.cli serve --config parsecore.toml` |
| API + 独立 Worker | `parsecore.queue.toml` | `queue-worker` | SQLite + 本地对象目录 | `docker compose up -d --build` |
| 灰度/生产持久化 | `parsecore.pgvector.toml.example` | `queue-worker` | Postgres + pgvector | `docker compose --profile pgvector up -d --build` |
| 本地验证 pgvector 链路 | `parsecore.pgvector.fake-embedding.toml.example` | `queue-worker` | Postgres + pgvector + fake embedding | `docker compose --profile pgvector up -d --build` |
| 复用宿主 OCR 网关 | `parsecore.remote-http.toml.example` | `queue-worker` | SQLite + 本地对象目录 | `PARSECORE_RUNTIME_CONFIG=./parsecore.remote-http.toml.example` |

配置加载方式：

- CLI/SDK：显式传 `--config`，或在代码里调用 `build_runtime("parsecore.toml")`。
- Docker Compose：通过 `PARSECORE_RUNTIME_CONFIG` 指向宿主机上的配置文件；容器内统一挂载为 `/app/parsecore.runtime.toml`。
- API Server：`python -m parsecore.cli serve --config <config>`。
- Worker：`python -m parsecore.cli worker --config <config>`。

## 安装依赖

按使用形态安装 extras：

```powershell
# 核心 SDK
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e .

# HTTP API
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api]"

# 解析 PDF/DOCX/Excel/OCR 常用能力
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[parsers]"

# Worker 与 Postgres/pgvector
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[worker,storage]"
```

生产或灰度环境通常安装：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api,parsers,worker,storage]"
```

## 环境变量

| 变量 | 用途 | 何时需要 |
| --- | --- | --- |
| `PARSECORE_RUNTIME_CONFIG` | Docker Compose 选择运行配置文件 | 使用 `docker-compose.yml` 时 |
| `PARSECORE_API_KEY` | HTTP 入口鉴权密钥 | 配置 `runtime.api_key_env = "PARSECORE_API_KEY"` 时 |
| `PARSECORE_OCR_API_KEY` | 远程 OCR 网关鉴权密钥 | `providers.ocr.provider = "remote-http"` 且配置 `api_key_env` 时 |
| `PARSECORE_EMBEDDING_API_KEY` | OpenAI-compatible embedding 密钥 | `providers.embedding.enabled = true` 且非 fake provider 时 |
| `PARSECORE_LLM_API_KEY` | LLM provider 密钥 | `providers.llm.enabled = true` 时 |
| `PARSECORE_REGRESSION_FIXTURE_ROOT` | 回归样本根目录覆盖 | 自检样本不在默认路径时 |
| `PARSECORE_REGRESSION_HEARTBEAT_S` | 回归解析 heartbeat 间隔 | 长样本自检时 |
| `PARSECORE_TEST_POSTGRES_URL` | Postgres 存储测试连接串 | 本地运行 Postgres 测试时 |
| `PARSECORE_PERF_HISTORY_DIR` | perf 历史报告目录 | nightly/perf 趋势跟踪时 |
| `PARSECORE_PREVIOUS_PERF_REPORT` | 指定上一份 perf 报告 | 手动比较性能趋势时 |

PowerShell 示例：

```powershell
$env:PARSECORE_API_KEY = "replace-with-a-long-random-secret"
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
```

## 顶层项目配置

```toml
[project]
name = "parsecore-starter"
mode = "embedded-sdk"
```

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `project.name` | `parsecore` | 出现在 runtime describe 中，便于环境识别。 |
| `project.mode` | `embedded-sdk` | 当前产品主形态；保留字段，暂不建议改成其他值。 |

## Runtime 配置

```toml
[runtime]
execution_mode = "inline"
max_workers = 2
max_inflight_jobs = 0
max_active_parts_per_doc = 0
job_timeout_seconds = 0
part_timeout_seconds = 0
retry_backoff_seconds = 1.0
retry_backoff_max_seconds = 60.0
poll_interval_ms = 500
max_upload_bytes = 52428800
staged_upload_max_bytes = 0
allow_external_file_paths = false
quota_enforce = false
quota_window_hours = 24
quota_default_limit_units = 0
max_attempts = 3
log_path = "var/logs/job_events.jsonl"
# api_key_env = "PARSECORE_API_KEY"
```

| 字段 | 默认值 | 可选值/范围 | 说明 |
| --- | ---: | --- | --- |
| `execution_mode` | `inline` | `inline` / `queue-worker` | `inline` 由 API 进程执行后台 job；`queue-worker` 由 API 提交 job、Worker 独立消费。 |
| `max_workers` | `2` | 正整数 | inline 后台 job 执行线程数。同步 `/parse` 和 `/parse/batch` 仍在请求内完成。 |
| `max_inflight_jobs` | `0` | `0` 或正整数 | inline job 背压上限；`0` 表示自动取 `max_workers * 4`。超过时返回 `429 too_many_inflight_jobs`。 |
| `max_active_parts_per_doc` | `0` | `0` 或正整数 | 单文档 active part 限流，`0` 表示不额外限制；inline 与 queue-worker 模式均会跳过已达上限的同文档 part，生产建议按 worker 总量设为 `2-4`。 |
| `job_timeout_seconds` | `0` | `0` 或正整数 | queue-worker 软超时回收阈值；`0` 表示关闭。超时不会强杀正在运行的解析器，只会把过期 active job 重新排队或 dead-letter。 |
| `part_timeout_seconds` | `0` | `0` 或正整数 | PDF part job 专用软超时；未设置时回退到 `job_timeout_seconds`。 |
| `retry_backoff_seconds` | `1.0` | 非负数 | queue-worker job 失败后重新进入 pending 前的指数退避基准。 |
| `retry_backoff_max_seconds` | `60.0` | 非负数 | 指数退避上限，避免长时间阻塞同一个失败任务。 |
| `poll_interval_ms` | `1000` | 正整数 | Worker 拉取待处理 job 的轮询间隔。 |
| `max_upload_bytes` | `0` | 字节数 | `/parse`、`/v1/parse`、`/parse/batch`、`/v1/parse/batch` 同步上传保护；`0` 表示不限制。推荐生产保留 50 MiB 或按业务调整。 |
| `staged_upload_max_bytes` | `0` | 字节数 | `/parse/uploads` 与 `/v1/parse/uploads` 桥接暂存上限；`0` 表示不限制，用于承接同步入口拒绝的大文件。 |
| `allow_external_file_paths` | `false` | bool | 控制 `/v1/parse/jobs` 是否允许读取 `storage.object_store` 之外的服务端本地路径。生产建议保持 `false`。 |
| `api_key_env` | 空 | 环境变量名 | 配置后除 `/health` 外都要求 `x-api-key` 或 `Authorization: Bearer`。环境变量为空会启动失败。 |
| `quota_enforce` | `false` | bool | 开启后按租户和 `quota_key` 做硬限校验。 |
| `quota_window_hours` | `24` | 正数 | quota 统计时间窗，单位小时。 |
| `quota_default_limit_units` | `0` | 非负整数 | 默认 quota 上限；`0` 表示未设置默认硬限。 |
| `max_attempts` | `3` | 正整数 | Worker 模式下 job 最大尝试次数；未达到上限的失败 job 会按退避时间回到 `pending`，达到上限后写入 dead-letter。 |
| `log_path` | `var/logs/job_events.jsonl` | 路径 | 运行事件日志路径。 |

调度内部会为每次 claim 写入 `claim_token / claimed_at / lease_expires_at / next_attempt_at`。这些字段会出现在 job 查询 payload 中，宿主通常只需要透传或忽略；中台会用 `claim_token` 拒绝超时回收后的旧 worker 写回，避免旧 attempt 把新 attempt 的状态或产物覆盖掉。

生产建议：

- 面向外部或跨团队调用时启用 `runtime.api_key_env`。
- 保留 `max_upload_bytes`，避免超大文件直接压垮同步 API 进程。
- 需要承接大文件时保持 `staged_upload_max_bytes = 0`，或设为明显高于同步阈值的业务上限。
- 保持 `allow_external_file_paths = false`，跨服务提交文件优先使用 `/parse/uploads` 或 `/v1/parse/uploads`。
- 大文件、并发或长耗时解析使用 `queue-worker`。
- 灰度初期把 `max_workers` 和 `max_inflight_jobs` 设保守，再根据 `/v1/parse/metrics` 调整。

## 鉴权、上传限制与配额

启用 API key：

```toml
[runtime]
api_key_env = "PARSECORE_API_KEY"
```

调用方式：

```powershell
$headers = @{ "x-api-key" = $env:PARSECORE_API_KEY }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8090/v1/runtime
```

上传限制：

```toml
[runtime]
max_upload_bytes = 52428800
staged_upload_max_bytes = 0
```

超过同步上限时，`/parse`、`/v1/parse`、`/parse/batch`、`/v1/parse/batch` 返回 `413 document_too_large_for_sync`。响应中保留 `actual_bytes / limit_bytes`，并追加：

```json
{
  "recommended_endpoint": "/v1/parse/uploads",
  "recommended_job_endpoint": "/v1/parse/jobs",
  "profile": "auto",
  "resolved_profile": "large-pdf",
  "can_force_sync": true,
  "force_sync_param_names": ["force_sync", "allow_sync_large_document"]
}
```

`/parse/uploads` 与 `/v1/parse/uploads` 使用 `staged_upload_max_bytes` 控制桥接暂存大小；默认 `0` 表示不限制，因此同步入口拒绝的大文件仍能进入异步 job 链路。生产面向外部开放时，建议同时设置 `staged_upload_api_key_env` 与业务侧文件大小上限。

宿主产品推荐处理流程：

1. 小文件沿用原同步入口，并默认传 `profile=auto`。
2. 收到 `413 document_too_large_for_sync` 后，记录 `actual_bytes / limit_bytes / resolved_profile / trace_id`，把同一文件转交 `/v1/parse/uploads`。
3. 上传桥接返回 `parsecore_server_file_path` 后，调用 `/v1/parse/jobs`，继续传 `profile=auto` 或响应中建议的显式 profile。
4. 轮询 job 完成后，读取 `/v1/parse/documents/{doc_id}?projection=structured`，把 `tables / quality_signals / parse_units` 落到宿主 JSON 字段。

宿主可通过 `GET /v1/parse/profiles` 或 `/v1/runtime` 的 `profiles` 字段发现支持的 profile、auto 阈值和推荐异步 profile。`projection=structured` 和 `/quality` 会返回 `profile_resolution`，包含 `requested_profile / resolved_profile / source / reasons / recommended_async / limits / profile_known`。如果请求传了未知 profile，系统保持兼容不拒绝，并通过 `profile_warning=unknown_profile` 暴露风险。

`max_upload_bytes` 和 `staged_upload_max_bytes` 是两层不同保护：前者保护同步 API 响应时长和内存，后者保护桥接暂存入口容量。低成本接入时通常保留同步 50 MiB 阈值，并让 `staged_upload_max_bytes = 0` 或设置为更高的业务上限；这样宿主只需要在 413 分支切换链路，不需要提前准确判断每种文件大小。

`/v1/parse/jobs` 本地路径边界：

```toml
[runtime]
allow_external_file_paths = false

[storage]
object_store = "local://./var/uploads"
```

默认情况下，`POST /v1/parse/jobs` 的 `file_path` 必须指向已存在的普通文件，且解析后的绝对路径必须位于 `storage.object_store` 指定的本地目录内。推荐通过 `/parse/uploads` 或 `/v1/parse/uploads` 先把文件暂存到 `_api_uploads`，再使用返回的 `parsecore_server_file_path` 创建 job。

桥接上传当前要求 `storage.object_store` 使用 `local://...`。如果配置成非本地对象存储，`/parse/uploads` 与 `/v1/parse/uploads` 会返回 `500 staged_upload_requires_local_object_store`，避免把暂存文件退回到系统临时目录后绕开路径边界。

错误口径：

| 场景 | HTTP | code |
| --- | ---: | --- |
| 缺少 `doc_id` | 400 | `missing_doc_id` |
| 缺少 `file_path` | 400 | `missing_file_path` |
| 路径不存在或不是普通文件 | 400 | `invalid_file_path` |
| 路径解析后不在 object store 内 | 403 | `file_path_not_allowed` |

Quota 示例：

```toml
[runtime]
quota_enforce = true
quota_window_hours = 24
quota_default_limit_units = 1000

[runtime.quota_limits]
"tenant-a:starter" = 200
"tenant-a:*" = 500
"*:batch" = 100
"*:*" = 1000
```

匹配优先级：

```text
tenant:quota_key > tenant:* > *:quota_key > *:* > quota_default_limit_units
```

超限返回 `429 quota_exceeded`，响应包含 `tenant_id / quota_key / used_units / requested_units / limit_units / window_hours`。

## 存储与索引

```toml
[storage]
database_url = "sqlite:///./var/parsecore.db"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"
```

| 字段 | 支持值 | 说明 |
| --- | --- | --- |
| `storage.database_url` | `sqlite:///...` | 本地单机持久化，适合 SDK、本地 API、小规模试用。 |
| `storage.database_url` | `postgresql://...` / `postgres://...` | Postgres 持久化，适合 API + Worker、灰度、生产。 |
| `storage.database_url` | `memory://` / `memory` | 进程内临时存储，只适合测试。 |
| `storage.object_store` | `local://...` | 当前保留为本地对象目录配置，建议指向可持久化目录。 |
| `index.mode` | `hybrid` | 默认值；Postgres 下会启用 pgvector 索引，非 Postgres 下自动退化。 |
| `index.mode` | `pgvector` | 显式要求 pgvector 索引；需配合 Postgres。 |
| `index.mode` | `null` / `memory` | 不启用持久索引。 |

注意：

- `index.mode = "hybrid"` 搭配 SQLite 时不会启用 pgvector，搜索会走可用的关键词/本地回退路径。
- API 与 Worker 分进程部署时，生产应使用 Postgres，否则多进程之间无法共享内存态数据。
- Docker Compose 的 pgvector profile 已提供 `parsecore-postgres` 服务。

## Translation 与产品适配

```toml
[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"
```

| 字段 | 说明 |
| --- | --- |
| `translation.enabled` | 当前使用 EchoTranslator，占位保留；可保持默认。 |
| `translation.strategy` | 当前建议保持 `lazy`。 |
| `product.adapter` | 兼容旧配置字段，当前会归一化为 `embedded`。 |

## Provider 配置

### OCR Provider

本地 RapidOCR：

```toml
[providers.ocr]
enabled = true
provider = "rapidocr"
```

远程 HTTP OCR：

```toml
[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://ocr.example.com"
api_key_env = "PARSECORE_OCR_API_KEY"
timeout_seconds = 10.0
max_retries = 2
options = { endpoint_path = "/ocr/v1", headers = { "X-OCR-Tenant" = "tenant-a" }, det_use_dilation = true }
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | `false` 时禁用 image OCR 和 PDF 坏页 OCR fallback。 |
| `provider` | 支持 `rapidocr`、`remote-http`，也兼容 `rapidocr_onnxruntime`、`http-json` 等别名。 |
| `base_url` | 远程 OCR 网关根地址。 |
| `api_key_env` | 可选；配置后请求使用 `Authorization: Bearer <key>`。 |
| `timeout_seconds` | 单次 OCR 请求超时。 |
| `max_retries` | 失败重试次数；总尝试次数为 `max_retries + 1`。 |
| `options.endpoint_path` | 远程 OCR 路径，默认 `/ocr`。 |
| `options.headers` | 附加 HTTP headers。 |
| 其他 `options.*` | 透传给 OCR provider 或 RapidOCR 初始化。 |

远程 OCR 请求/响应契约见 [ocr-gateway-contract.md](ocr-gateway-contract.md)。

### Embedding Provider

默认关闭：

```toml
[providers.embedding]
enabled = false
```

本地 fake embedding，用于链路验证：

```toml
[providers.embedding]
enabled = true
provider = "fake"
```

OpenAI-compatible provider：

```toml
[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://api.openai.com/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
timeout_seconds = 30.0
max_retries = 2
batch_size = 16
options.dimensions = 1536
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 关闭时不会生成 chunk embedding。 |
| `provider` | 支持 `fake` / `test` / `stub` 和 `openai-compatible` / `openai` / `dashscope` / `qwen`。 |
| `base_url` | OpenAI-compatible API 根地址，不要带 `/embeddings`。 |
| `model` | embedding 模型名。 |
| `api_key_env` | 密钥环境变量名。 |
| `batch_size` | 每批 embedding chunk 数量。 |
| `options.dimensions` | 可选，透传给兼容 OpenAI 的 embedding 接口。 |

若 provider 未配置好，运行时会降级为无 embedding，不阻断基础解析。

### LLM Provider

LLM 当前用于 PDF 低置信度边界 refinement，默认关闭，失败时保持原解析结果。

```toml
[providers.llm]
enabled = false
provider = "qwen-dashscope"
model = "qwen3.6-35b-a3b"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "PARSECORE_LLM_API_KEY"
timeout_seconds = 30.0
max_retries = 2
options.temperature = 0.0
options.top_p = 0.1
options.max_calls_per_doc = 50
```

生产建议先保持关闭；若开启，应先用固定样本跑 `self_check` 和解析性能基线，确认没有时延和成本异常。

## Parser 配置

每个 parser 使用一个 `[[parsers]]` 块：

```toml
[[parsers]]
name = "excel-native"
media_types = [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.ms-excel",
]
extensions = [".xlsx", ".xlsm", ".xls"]
```

| Parser | 文件类型 | 说明 |
| --- | --- | --- |
| `docx-native` | `.docx` | 原生 DOCX 解析。 |
| `pdf-text` | `.pdf` | PDF 文本、结构、表格、OCR fallback。 |
| `image-ocr` | `.png` / `.jpg` / `.jpeg` | 图片 OCR。 |
| `excel-native` | `.xls` / `.xlsx` / `.xlsm` | 原生表格解析，输出 `TABLE` block。 |
| `text-native` | `.txt` / `.md` | 纯文本/Markdown。 |

删除某个 `[[parsers]]` 块即可关闭对应 parser。新增 parser 时需要代码中已有同名 parser 实现。

### PDF 后处理

```toml
[parsers.options.post_process]
dual_channel = true
layout_reading_order = true
dual_table_min_rows = 2
dual_table_min_cols = 2
ocr_bad_pages = true
ocr_bad_page_min_cid_tokens = 5
ocr_bad_page_min_cid_char_ratio = 0.25
ocr_render_resolution = 110
ocr_confidence_threshold = 0.5
ocr_merge_line_gap_ratio = 1.6
```

| 字段 | 说明 |
| --- | --- |
| `dual_channel` | 开启 pdfplumber 表格抽取 + 文本通道组合。 |
| `layout_reading_order` | 按 layout 读序整理页面文本。 |
| `dual_table_min_rows` / `dual_table_min_cols` | 表格识别最小行列数。 |
| `ocr_bad_pages` | 坏页触发 OCR fallback。 |
| `ocr_bad_page_min_cid_tokens` | CID token 数超过阈值时考虑坏页。 |
| `ocr_bad_page_min_cid_char_ratio` | CID 字符占比超过阈值时考虑坏页。 |
| `ocr_render_resolution` | OCR 渲染分辨率，越高越慢。 |
| `ocr_confidence_threshold` | OCR 结果置信度阈值。 |
| `ocr_merge_line_gap_ratio` | OCR 行合并间距比例。 |

调参建议：

- 优先保持默认；PDF 后处理非常容易影响结构稳定性。
- 需要调短块合并、页眉页脚去除等高级项时，先跑 `tools/min_length_scan.py` 或固定样本自检。
- OCR 变慢时，先降低需要 OCR 的页面比例，再考虑降低 `ocr_render_resolution`。

### Excel 解析

```toml
[[parsers]]
name = "excel-native"
extensions = [".xlsx", ".xlsm", ".xls"]
# options.max_rows_per_sheet = 5000
# options.max_cols_per_sheet = 100
# options.max_metadata_cells = 1000
# options.include_hidden_sheets = true
```

| 字段 | 说明 |
| --- | --- |
| `options.max_rows_per_sheet` | 每个 worksheet 最多扫描行数，避免异常大表耗尽资源。 |
| `options.max_cols_per_sheet` | 每个 worksheet 最多扫描列数。 |
| `options.max_metadata_cells` | 完整 cell metadata 上限；超过后降级为 `cells_preview`。 |
| `options.include_hidden_sheets` | 是否解析隐藏 sheet。 |

Excel 输出会按 worksheet 内空行与标题行识别多个表格区域，表格 block metadata 包含 `sheet_name / cell_range / source_cell_range / sheet_table_index / table_title / header_row / header_values / merged_cells / has_formula / hidden_sheet`。

## 常用配置场景

### 本地 API + API Key

```toml
[runtime]
execution_mode = "inline"
api_key_env = "PARSECORE_API_KEY"
max_upload_bytes = 52428800
```

```powershell
$env:PARSECORE_API_KEY = "replace-with-secret"
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

### API + Worker

```toml
[runtime]
execution_mode = "queue-worker"
poll_interval_ms = 500
```

启动：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.queue.toml --host 127.0.0.1 --port 8090
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli worker --config parsecore.queue.toml
```

说明：`/v1/parse/jobs` 等异步 job 入口需要 Worker 消费；`/parse` 和 `/v1/parse/batch` 是同步解析入口。Worker 模式下跨服务传文件建议走 `/v1/parse/uploads`，确保暂存文件落在 `storage.object_store` 下。

### Postgres + pgvector 灰度

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
```

本地没有真实 embedding key 时：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.fake-embedding.toml.example"
docker compose --profile pgvector up -d --build
```

### 远程 OCR 网关

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.remote-http.toml.example"
$env:PARSECORE_OCR_API_KEY = "replace-with-ocr-secret"
docker compose up -d --build
```

验收：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/ocr_benchmark.py --config parsecore.remote-http.toml.example --pdf samples/heavy-ocr.pdf --out var/self-check/ocr-benchmark.json
```

`ocr_benchmark` 结果中的 `ocr_decision_trace` 字段可用于核验 OCR 决策口径（attempted / fallback / rejected / failed）以及 `native_text_token_count / final_text_token_count` 变化。

## 验收命令

配置变更后建议至少执行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --config parsecore.toml
```

Excel 真实样本：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/excel_sample_quality.py --config parsecore.toml --sample-dir D:/app/uploads --out-json var/self-check/excel-sample-quality.json --out-md var/self-check/excel-sample-quality.md
```

解析性能基线：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/parse_perf_baseline.py --config parsecore.toml --sample-dir D:/app/uploads --extensions .pdf,.docx,.xls,.xlsx,.xlsm --out-json var/self-check/parse-perf-baseline.json --out-md var/self-check/parse-perf-baseline.md
```

大 PDF part 调度压测：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli large-pdf-stress --config parsecore.toml --generate-pages 1000 --target-pages-per-part 200
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli large-pdf-stress --config parsecore.toml --pdf D:/samples/large.pdf --target-pages-per-part 200 --execute-parts --max-parts 3
```

HTTP smoke：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/metrics
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/prometheus
```

文档结果 projection：

```powershell
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc?projection=compat"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc?projection=structured"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc?projection=full"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc/quality"
```

`compat` 用于旧 parser-service 消费方；`structured` 返回 `schema_version = "2026-06"`，并包含 `tables / cells / quality_signals / parse_units`；`full` 在 structured 基础上额外带 `job / blocks / chunks`，适合调试和后续人工复核。

projection 落地后的下一步是把解析策略从读取参数中拆出来，在异步 job 创建时传 `profile`：

```powershell
$body = @{
  doc_id = "demo-doc"
  file_path = "D:/app/uploads/demo.pdf"
  profile = "auto"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body http://127.0.0.1:8090/v1/parse/jobs
```

推荐口径：

- 默认接入传 `profile=auto`，由中台按文件类型、大小、页数、表格密度和 OCR 信号选择策略。
- 表格密集文件传 `profile=table-heavy`，重点验收 `projection=structured` 下的 `tables / cells / quality_signals`。
- 超大 PDF、长页数 PDF 或同步接口返回 `413 document_too_large_for_sync` 的文件传 `profile=large-pdf`，走 `/v1/parse/uploads + /v1/parse/jobs` 异步链路，再轮询 job 并读取 `projection=structured`。
- 超大目录、清单、台账型 PDF 可显式传 `profile=large-pdf-catalog` 或 `profile=large-pdf-ledger`；`profile=auto` 会先按文件名提示、页数/大小和表格密度做保守路由。
- `profile` 控制解析执行策略，`projection` 控制结果返回形态；不要用 `projection=full` 代替 profile 灰度。

当前 `profile=auto` 是宿主侧最省改造的默认值：调用方只需要传一个稳定参数，中台会按文件扩展名、media type、大小和入口上下文解析为 effective profile，并把结果写入 job options 或 413 detail。宿主可先只记录 `profile / resolved_profile`，等灰度样本足够后再对少数文件类型显式覆盖。

profile 建议用法：

| profile | 适用样本 | 宿主接入建议 |
| --- | --- | --- |
| `auto` | 默认入口 | 新接入统一使用，便于中台持续升级路由规则。 |
| `table-heavy` | PDF/DOCX 中表格密集、表头稳定性重要 | 优先双写 `tables` 和 `quality_signals`，观察表格信号密度。 |
| `large-pdf` | 超过同步阈值、长页数 PDF、已知慢样本 | 默认走异步上传和 job 轮询，不再压同步 HTTP。 |
| `large-pdf-catalog` | 超大目录、产品清单、批准目录类 PDF | 走异步与 part 调度，优先保留记录级可追溯数据。 |
| `large-pdf-ledger` | 超大台账、明细表、表格密度高的 PDF | 走异步与 part 调度，重点观察 records 与列错位信号。 |
| `ocr-heavy` | 文字层质量差、OCR fallback 多的 PDF | 重点观察 OCR trace 和 `ocr_failed_page` 类信号。 |
| `excel-ledger` | `.xls/.xlsx/.xlsm` 台账、明细表 | 验证 sheet、cell range、merged cells 和截断信号。 |
| `scan-pdf` | 扫描件或图片型 PDF | 走异步优先，避免同步请求长时间占用连接。 |

`quality_signals` 后续会保持追加式扩展。宿主 schema 建议把它当数组 JSON 存储，不要只按当前 code 建固定列；消费时按 `code / severity / page_number / table_id / row_index / col_index / bbox` 做宽松解析，未知 code 默认展示或记录，不阻断主流程。

## 导出中心与复跑规划

当前版本已提供同步导出 MVP，适合中小结果集或排查场景：

```powershell
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=tables&format=csv" -OutFile tables.csv
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=quality_signals&format=jsonl" -OutFile quality_signals.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=parse_units&format=tsv" -OutFile parse_units.tsv
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=jsonl" -OutFile records.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=sqlite" -OutFile records.sqlite
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=xlsx" -OutFile records.xlsx
```

支持参数：

- `dataset=tables|quality_signals|parse_units|records`
- `format=jsonl|csv|tsv|sqlite|xlsx`
- `tenant_id=...` 可选，默认 `default`

解析完成后，中台会把结构化 `pages / lines / records` 作为 document views 持久化到当前 JobStore；records 查询会优先读取持久化结果，缺失时再回退到 block 现场投影。记录级查询入口用于分页读取派生 records，避免把大结果集塞进主文档响应。records 可能来自结构化表格行，也可能来自 `large-pdf-catalog` / `large-pdf-ledger` 下按序号行聚合出的文本记录；消费方应按 `source / fields / raw_text / normalized_text / page_start / page_end` 宽松解析。

```text
GET /v1/parse/documents/{doc_id}/records?limit=100&offset=0
GET /v1/parse/documents/{doc_id}/records?query=TC001A
GET /v1/parse/documents/{doc_id}/records?page_start=2000&page_end=2300
```

CSV/TSV/XLSX/SQLite 会把嵌套字段如 `cells/detail/warnings/fields` 稳定序列化成 JSON 字符串。SQLite 导出会生成一个与 dataset 同名的数据表，适合大结果集的离线查询；XLSX 更适合给业务人员抽检 compact records。当前也已提供异步导出包 MVP：

```text
POST /v1/parse/documents/{doc_id}/export-jobs
GET  /v1/parse/export-jobs/{export_id}
GET  /v1/parse/export-jobs/{export_id}/download?file=quality_signals.jsonl
```

异步包会生成 `manifest.json` 以及按 include/formats 指定的 `tables.csv / quality_signals.jsonl / parse_units.tsv / records.jsonl / records.sqlite / records.xlsx`。manifest 会记录 `manifest_schema_version / tenant_id / schema_version / parse_run_id / profile / profile_resolution / request.include / request.formats / request.filters`，每个文件条目包含 `dataset / format / path / content_type / bytes / records`。`parquet`、异常页截图、raw cells 与 trace 打包作为后续增强。创建导出时建议带 `include`、`formats` 和 `filters`，例如只导出 `severity=warning/error` 或指定 `page_range`。

当前也已提供 PDF part 调度与复跑第一版，供宿主产品按页段排障和小范围重跑：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts/plan" -Body '{"target_pages_per_part":200}' -ContentType "application/json"
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts" | ConvertFrom-Json
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts?state=warning|failed" | ConvertFrom-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts/demo-doc-part-3/rerun"
```

`/parts/plan` 会基于最新 PDF job 轻量探测页数，生成物理 part PDF 和子 job。每个子 job 使用独立 `part_doc_id`，避免覆盖父文档；子 part 完成后会刷新父文档的 blocks/chunks、structured projection、`parse_units` 和 part 状态。`/parts` 返回 `part_id / page_range / state / quality_signal_codes / severity_counts / job_id / rerun_supported`。

文档级重跑仍保留：

```text
POST /v1/parse/documents/{doc_id}/reparse
POST /v1/parse/documents/{doc_id}/rechunk
POST /v1/parse/documents/{doc_id}/re-embed
```

本轮生产增强接口已落地：

```text
POST /v1/parse/documents/{doc_id}/parts/rerun
POST /v1/parse/documents/{doc_id}/parts/{part_id}/cancel
```

批量复跑请求支持：

```json
{
  "part_ids": ["demo-doc-part-3", "demo-doc-part-5"],
  "failed_only": true,
  "state": ["failed", "cancelled"],
  "profile": "auto"
}
```

取消接口只保证尚未运行的 part：持久化状态为 `pending`，inline runner 内部队列中的 part 也会先移出队列再转为 `cancelled`；如果 part 已在运行中，不强杀 worker 进程，会返回当前状态。运行态 job 的软 timeout 会使当前 `claim_token` 失效，旧 worker 后续写回会被拒绝，再由复跑或下一次 attempt 接管。

宿主仍建议把 `quality_signals` 与 `parse_units` 原样落 JSON，避免把复跑粒度写死为整文档。part 级复跑已经能重跑指定页段；本轮生产增强补齐了批量复跑、取消、限流和父文档 part 前缀增量索引，part 成功完成后只替换该 part 对应的 blocks/chunks/index rows，并在 `index_manifest.part_index.parts[]` 记录 `chunk_ids / page_range / index_version`。

## 超长 PDF 配置口径

17000 页 PDF 的当前推荐接入方式是异步分流，而不是同步强跑：

1. 同步入口保持 `max_upload_bytes`，收到 `413 document_too_large_for_sync` 后切到 `/v1/parse/uploads + /v1/parse/jobs`。
2. 创建 job 时传 `profile=auto` 或显式 `profile=large-pdf`。
3. 读取 `projection=structured`，把 `parse_units / quality_signals / profile_resolution` 落库。
4. 在宿主侧把这类任务标记为长任务，轮询间隔和超时策略与普通小文件区分。

PDF 页段调度第一版已经落地：先轻量探测页数，再生成连续页段 `parse_units`，按 `target_pages_per_part` 分批解析并增量合并结果。建议默认普通文本页段 100-300 页、OCR 密集页段 20-50 页。超大生产任务建议优先在 queue-worker 模式运行，避免 inline 服务一次性排入过多后台 part。

如果启用了 API key：

```powershell
$headers = @{ "x-api-key" = $env:PARSECORE_API_KEY }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8090/v1/runtime
```

## 生产检查清单

上线前确认：

- 已选择正确配置文件，并通过 `describe` 确认 `execution_mode / database_url / index_mode / parsers / providers`。
- 对外 API 已配置 `runtime.api_key_env`，密钥只存在环境变量或密钥管理系统中。
- `max_upload_bytes` 与业务文件上限一致。
- 生产使用 API + Worker 时，`database_url` 指向 Postgres，而不是本地 SQLite。
- OCR、embedding、LLM provider 的 `base_url / api_key_env / timeout_seconds / max_retries` 已按真实网关设置。
- `providers.embedding.enabled = true` 时已明确是 fake 还是真实 provider。
- Excel 大表场景已设置 `max_rows_per_sheet / max_cols_per_sheet / max_metadata_cells` 或完成性能基线。
- 超大 PDF / part 调度场景已评估 `runtime.max_active_parts_per_doc`，并准备 `parts_total / parts_done / parts_failed / parts_active / parts_queued / parts_cancelled` 指标面板。
- `parsecore self-check`、真实样本质量报告、性能基线和必要的大 PDF stress 报告均通过并留档。
- 灰度时已准备 `/health`、`/v1/parse/metrics`、`/v1/parse/events`、`/v1/parse/prometheus` 的观测面板。
- OCR 观测建议至少覆盖 `ocr_attempted / ocr_fallback / ocr_rejected / ocr_failed` 四类事件与对应 Prometheus 计数器。
- 已准备回滚配置：通常回到 `parsecore.queue.toml` 或关闭 OCR fallback、上传放宽/收紧等低风险开关。
