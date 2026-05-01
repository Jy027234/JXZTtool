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
poll_interval_ms = 500
max_upload_bytes = 52428800
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
| `poll_interval_ms` | `1000` | 正整数 | Worker 拉取待处理 job 的轮询间隔。 |
| `max_upload_bytes` | `0` | 字节数 | `/parse` 与 `/parse/batch` 上传保护；`0` 表示不限制。推荐生产保留 50 MiB 或按业务调整。 |
| `api_key_env` | 空 | 环境变量名 | 配置后除 `/health` 外都要求 `x-api-key` 或 `Authorization: Bearer`。环境变量为空会启动失败。 |
| `quota_enforce` | `false` | bool | 开启后按租户和 `quota_key` 做硬限校验。 |
| `quota_window_hours` | `24` | 正数 | quota 统计时间窗，单位小时。 |
| `quota_default_limit_units` | `0` | 非负整数 | 默认 quota 上限；`0` 表示未设置默认硬限。 |
| `max_attempts` | `3` | 正整数 | Worker 模式下 job 最大尝试次数。 |
| `log_path` | `var/logs/job_events.jsonl` | 路径 | 运行事件日志路径。 |

生产建议：

- 面向外部或跨团队调用时启用 `runtime.api_key_env`。
- 保留 `max_upload_bytes`，避免超大文件直接压垮 API 进程。
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
```

超过上限时同步上传接口返回 `413 file_too_large`，响应中包含实际大小和限制大小。

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

说明：`/v1/parse/jobs` 等异步 job 入口需要 Worker 消费；`/parse` 和 `/v1/parse/batch` 是同步解析入口。

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
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --config parsecore.toml
```

Excel 真实样本：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/excel_sample_quality.py --config parsecore.toml --sample-dir D:/app/uploads --out-json var/self-check/excel-sample-quality.json --out-md var/self-check/excel-sample-quality.md
```

解析性能基线：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/parse_perf_baseline.py --config parsecore.toml --sample-dir D:/app/uploads --extensions .pdf,.docx,.xls,.xlsx,.xlsm --out-json var/self-check/parse-perf-baseline.json --out-md var/self-check/parse-perf-baseline.md
```

HTTP smoke：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/metrics
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/prometheus
```

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
- `tools/self_check.py`、真实样本质量报告、性能基线均通过并留档。
- 灰度时已准备 `/health`、`/v1/parse/metrics`、`/v1/parse/events`、`/v1/parse/prometheus` 的观测面板。
- OCR 观测建议至少覆盖 `ocr_attempted / ocr_fallback / ocr_rejected / ocr_failed` 四类事件与对应 Prometheus 计数器。
- 已准备回滚配置：通常回到 `parsecore.queue.toml` 或关闭 OCR fallback、上传放宽/收紧等低风险开关。
