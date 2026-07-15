# ParseCore 配置手册

本文是 ParseCore 的配置总入口，面向要把产品跑起来、接入宿主系统、或进入灰度/生产验收的用户。配置文件使用 TOML；密钥只通过环境变量注入，不写入配置文件。

如果只需要交付使用口径，先读 [release-notes.md](release-notes.md) 和 [user-guide.md](user-guide.md)；本文保留完整配置字段和运维细节。

## 快速选择

| 使用场景 | 推荐配置 | 执行模式 | 存储/索引 | 典型命令 |
| --- | --- | --- | --- | --- |
| 本地 SDK 或单进程 API | `parsecore.toml` | `inline` | SQLite + 本地对象目录 | `python -m parsecore.cli serve --config parsecore.toml` |
| API + 独立 Worker | `parsecore.queue.toml` | `queue-worker` | SQLite + 本地对象目录 | `docker compose up -d --build` |
| 灰度/生产持久化 | `parsecore.pgvector.toml.example` | `queue-worker` | Postgres + pgvector | `docker compose --profile pgvector up -d --build` |
| 本地验证 pgvector 链路 | `parsecore.pgvector.fake-embedding.toml.example` | `queue-worker` | Postgres + pgvector + fake embedding | `docker compose --profile pgvector up -d --build` |
| 阿里 Qwen RAG + 二阶段排序 | `parsecore.pgvector.aliyun-rag.toml.example` | `queue-worker` | Postgres + pgvector + Qwen embedding/rerank | 注入 `PARSECORE_ALIYUN_API_KEY` 后按下文 smoke 验收 |
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

# 可选 PDF baseline provider（pymupdf4llm）
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[pymupdf4llm]"

# 可选 Docling 统一结构 provider
# d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[docling]"
```

> `pymupdf4llm` 和 `docling` 是可选 extra，不会随 `.[parsers]` 自动安装。启用前请参阅下文 [可选 Provider 安装](#可选-provider-安装) 章节。

生产或灰度环境通常安装：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api,parsers,worker,storage]"
```

## 环境变量

| 变量 | 用途 | 何时需要 |
| --- | --- | --- |
| `PARSECORE_RUNTIME_CONFIG` | Docker Compose 选择运行配置文件 | 使用 `docker-compose.yml` 时 |
| `PARSECORE_DATABASE_URL` | 覆盖配置文件中的 `storage.database_url` | 密钥注入、隔离端口或宿主进程直连容器数据库时 |
| `PARSECORE_API_KEY` | HTTP 入口鉴权密钥 | 配置 `runtime.api_key_env = "PARSECORE_API_KEY"` 时 |
| `PARSECORE_OCR_API_KEY` | 远程 OCR 网关鉴权密钥 | `providers.ocr.provider = "remote-http"` 且配置 `api_key_env` 时 |
| `PARSECORE_EMBEDDING_API_KEY` | OpenAI-compatible embedding 密钥 | `providers.embedding.enabled = true` 且非 fake provider 时 |
| `PARSECORE_RERANK_API_KEY` | 通用 rerank provider 密钥 | `providers.rerank.enabled = true` 且该 provider 使用此环境变量时 |
| `PARSECORE_ALIYUN_API_KEY` | 阿里 Qwen embedding 与 rerank 共用网关密钥 | 使用 `parsecore.pgvector.aliyun-rag.toml.example` 时 |
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
provider_comparison_artifact_retention_seconds = 2592000
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
| `staged_upload_retention_seconds` | `86400` | 秒数 | 桥接暂存文件保留期，过期上传会在上传入口触发清理。 |
| `part_artifact_retention_seconds` | `604800` | 秒数 | part PDF 等局部解析工件建议保留期，配合 `cleanup_artifacts()` 或运维任务清理。 |
| `export_artifact_retention_seconds` | `2592000` | 秒数 | 异步导出包建议保留期，生产可按容量和审计要求调整。 |
| `provider_comparison_artifact_retention_seconds` | `2592000` | 秒数；`0` 表示关闭 | self-check 生成的 Provider comparison JSON/Markdown 保留期；仅由 `cleanup-provider-comparison-artifacts` 运维命令消费，默认 dry-run，不会在解析或 self-check 时自动删除。 |
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

Provider comparison 工件建议由定时任务从工作区根目录运行。首次和每次调整保留期后先保留默认 dry-run 结果；确认候选仅包含 `provider-comparison.<profile>.json/.md` 后，才追加 `--execute`：

```powershell
# 只输出候选清单，不删除任何文件。
& '.\.venv\Scripts\python.exe' -m parsecore.cli cleanup-provider-comparison-artifacts `
  --config parsecore.toml `
  --root var/self-check `
  --out var/self-check/provider-comparison-cleanup-dry-run.json

# 已审核 dry-run 清单后才允许实际删除。
& '.\.venv\Scripts\python.exe' -m parsecore.cli cleanup-provider-comparison-artifacts `
  --config parsecore.toml `
  --root var/self-check `
  --execute
```

该命令不会清理 `self-check.json`、`latest*.json` 或其他审计 JSON/Markdown；将该配置设为 `0` 可禁用此清理通道。

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

运行时私有文件分区如下：同步解析上传写入 `_api_transient` 并在请求结束后删除，异步桥接上传写入 `_api_uploads`，导出包写入 `_exports/exp_<uuid>`，PDF part 写入 `_parsecore_parts/<doc>/<job>`。这些目录会复核解析后的真实路径仍位于受控根目录；上传文件使用随机名和独占创建，异常或 NTFS ADS 风格扩展名降级为 `.bin`。POSIX 目录/文件权限分别收紧为 `0700/0600`；Windows 依赖隔离子目录的继承 ACL，并执行 best-effort chmod。

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
embedding_dimension = 1536
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
| `index.embedding_dimension` | 正整数，默认 `1536` | pgvector 列维度，必须与实际 embedding 模型返回的向量维度一致。 |

注意：

- `index.mode = "hybrid"` 搭配 SQLite 时不会启用 pgvector，搜索会走可用的关键词/本地回退路径。
- pgvector 已建表后不能只靠改配置切换维度：切换模型维度前必须使用新数据库/schema，或执行受控迁移并重建全部向量；运行时会在不匹配时拒绝写入，避免静默混用向量空间。
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

## Quality Gate 配置

质量门禁用于把 IR / coverage / RAG 覆盖审计收敛成统一动作建议。当前为 report-only：只在 `/v1/runtime`、`projection=structured|ir|coverage`、`/quality` 和 `/providers` 输出 `quality_gate`，不主动阻断解析、不自动触发复跑。

```toml
[quality_gate]
enabled = true
min_text_page_coverage = 0.98
min_table_unit_coverage = 0.95
min_unit_chunk_coverage = 0.98
min_reading_order_confidence = 0.75
allow_local_rerun = true
allow_manual_review = true
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否输出门禁判定；关闭后返回 `gate = "disabled"`。 |
| `min_text_page_coverage` | 正文页产生可入库 KnowledgeUnit 的最低比例。 |
| `min_table_unit_coverage` | 表格页产生表格/摘要 KnowledgeUnit 的最低比例。 |
| `min_unit_chunk_coverage` | 可入库 KnowledgeUnit 进入 chunk 的最低比例。 |
| `min_reading_order_confidence` | 阅读顺序置信度阈值；当前会消费页级 `reading_order_confidence`，低于阈值时进入 `quality_gate` 并优先建议 `layout` 能力的本地 Provider 重跑。 |
| `allow_local_rerun` | 当覆盖缺口更适合重跑本地 Provider 时，允许输出 `gate = "local_rerun"`。 |
| `allow_manual_review` | 当覆盖缺口需要人工复核时，允许输出 `gate = "manual_review"`。 |

`quality_gate` 输出包含 `gate / passed / blocking / enforcement / recommended_action / action_suggestions / flags / warnings / thresholds / observed`。本阶段 `enforcement = "report_only"` 且 `blocking = false`，宿主产品可先用于质量面板、运营抽检和局部复跑按钮。`action_suggestions` 只描述可用操作，不自动执行；每条建议包含 `action_id / method / endpoint / scope / reason_codes / auto_execute`，并可带 `params / payload / context`。常见动作包括 `reparse_document`、`rechunk_document`、`reembed_document`、`rerun_warning_parts`、`review_parse_ir` 和 `review_quality`。RAG 表格缺 unit 会优先建议本地 Provider 重跑；图示缺 caption 会进入 IR/质量报告复核；阅读顺序置信度低于 `min_reading_order_confidence` 时，会进入 `reading_order_confidence_below_threshold`，并把本地 Provider 能力要求收敛到 `layout`。若 `rerun_warning_parts` 已拿到 `parse_units[].rerun_comparison`，则会把已有 rerun 记录的 warning part 放入 `context.rerun_candidates.skipped_parts`，避免批量建议里重复包含同一页段；当前 `rerun_candidates` 还会补 `eligible_parts / coverage_gap_unit_part_ids / gap_unit_ids`，把“为什么建议重跑这些 part”直接收口到 part 与 KnowledgeUnit。对于 `/providers` 投影，`quality_gate` 还会额外挂出 `provider_comparison.primary_provider_id / best_provider_id / summary / actions`，并把 Provider 对比类动作合并进 `quality_gate.action_suggestions`，让接入方不必同时拼装两套诊断按钮。与此同时，`/quality` 投影也开始补 `provider_diagnostics`、`parts_diagnostics` 和 `attention_summary`，把 Provider 对比摘要、comparison actions、part 汇总、attention parts 以及一组已排好顺序的 `recommended_actions` 收口成默认诊断入口；其中 `attention_summary.entrypoints` 会额外提供 `quality / providers / parts / coverage` 四个视图的 endpoint、attention 状态、badge 数和推荐落点，并通过 `providers.context / parts.context / coverage.context` 补当前需要聚焦的 provider、part 和 coverage gap 上下文。进一步地，`attention_summary.contracts` 会把推荐动作规范成统一的 `request` 契约，并补充 `default_request / inspect_requests / execute_requests / preferred_execute_request / entrypoint_requests / parts_batch_rerun_requests / workflow`，方便宿主把查看和执行分成两条稳定路径；当前只要推荐执行落在 `/parts/rerun`，`preferred_execute_request` 与 `parts_batch_rerun_requests` 也会同步带上 `attention_parts / coverage_gap_unit_part_ids / gap_unit_ids` 这类执行上下文，适合作为批量 rerun 抽屉或确认弹窗的数据源；`workflow` 会额外给出 `inspect -> compare -> execute -> verify` 的阶段化落点，适合作为诊断抽屉、侧边栏或动作向导的默认状态机。

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

可选的本地 Transformer embedding（仅用于明确配置的离线/受控验收，不改变默认路由）：

```toml
[providers.embedding]
enabled = true
provider = "sentence-transformers-local"
# 可填 Hugging Face model id 或本地 snapshot 目录
model = "sentence-transformers/all-MiniLM-L6-v2"
batch_size = 16
options.local_files_only = true
options.device = "cpu"
options.max_length = 256
options.normalize = true
```

安装可选依赖：`py -m pip install -e ".[local-embedding]"`。离线环境应先准备模型目录并把 `model` 改为本地路径；该 provider 使用 mean pooling + L2 normalization 生成真实向量，不使用 fake embedding。

| 字段 | 说明 |
| --- | --- |
| `enabled` | 关闭时不会生成 chunk embedding。 |
| `provider` | 支持 `fake` / `test` / `stub`、`openai-compatible` / `openai` / `dashscope` / `qwen`，以及可选的 `sentence-transformers-local` / `transformers-local`。 |
| `base_url` | OpenAI-compatible API 根地址，不要带 `/embeddings`。 |
| `model` | embedding 模型名。 |
| `api_key_env` | 密钥环境变量名。 |
| `batch_size` | 每批 embedding chunk 数量。 |
| `options.dimensions` | 可选，透传给兼容 OpenAI 的 embedding 接口。 |
| `options.local_files_only` | 本地 Transformer 是否禁止联网加载模型。 |
| `options.device` / `options.max_length` / `options.normalize` | 本地 Transformer 的设备、最大 token 长度和向量归一化开关。 |

若 provider 未配置好，运行时会降级为无 embedding，不阻断基础解析。

### Rerank Provider

Rerank 是可选的检索二阶段：ParseCore 先按既有 hybrid/关键词规则取得候选 chunk，再把至多 `candidate_limit` 条候选交给排序模型。它不参与文档解析、OCR、chunk 生成或 parser 路由，默认关闭。

```toml
[providers.rerank]
enabled = true
provider = "dashscope-compatible"
model = "qwen/qwen3-vl-rerank"
base_url = "https://model-router.edu-aliyun.com/v1"
api_key_env = "PARSECORE_ALIYUN_API_KEY"
timeout_seconds = 30.0
max_retries = 2
candidate_limit = 30
# options = { enable_truncation = true }
```

当前内置的 `dashscope-compatible` 实现已按阿里 Qwen 排序网关实际协议适配：请求为 `POST {base_url}/rerank`，使用 `input.query` 和 `input.documents`；响应读取 `output.results[].index` 与 `relevance_score`。密钥始终只从 `api_key_env` 读取，不会写入 TOML、运行态描述或 smoke 工件。

| 字段 | 说明 |
| --- | --- |
| `enabled` | 显式开启二阶段排序；默认 `false`。 |
| `provider` | 目前支持 `dashscope-compatible` 及其 `qwen-rerank` / `aliyun-rerank` 别名，以及用于测试的 `fake`。 |
| `model` / `base_url` | 排序模型名与兼容网关根地址；根地址不要带 `/rerank`。 |
| `api_key_env` | 排序网关密钥的环境变量名，可与 embedding 复用同一受控密钥。 |
| `candidate_limit` | 初检后送往排序模型的最多候选数；必须为正整数，并至少满足请求的 `limit`。 |
| `options` | 透传到网关 `parameters`，例如由已验证网关支持时设置 `enable_truncation`。 |

排序生效时，搜索响应的 `retrieval_mode` 会显示为 `hybrid+rerank` 或 `keyword-fallback+rerank`，每个命中保留 `retrieval_score` 并给出 `rerank_score`。若网关超时、不可用或返回不安全的索引，运行时保留初检结果、不阻断查询，并记录不含查询文本和密钥的 `rerank_skipped` 事件；同时写入固定 `failure_category` 的 `provider_failure` 运维事件和 Prometheus 终态失败计数。

#### Production RAG 配置模板

默认 `parsecore.toml` 继续保持 embedding 与 rerank 关闭。需要生产 RAG 时，从以下完整模板选择一条，复制为部署专用配置后替换数据库、模型或网关地址；不得把密钥写入 TOML。只有需要二阶段排序的 profile 才开启 `[providers.rerank]`。

- [本地 Transformer + pgvector](../parsecore.pgvector.local-embedding.toml.example)：已按 `all-MiniLM-L6-v2` 的 `384` 维配置，模型目录必须提前放到每个 API/worker 节点，且镜像或虚拟环境需要安装 `.[local-embedding]`。
- [远程 OpenAI-compatible 网关 + pgvector](../parsecore.pgvector.remote-embedding.toml.example)：采用不可路由的示例 URL 和 `PARSECORE_EMBEDDING_API_KEY` 环境变量；部署前由密钥管理系统注入真实 endpoint、模型和凭证。若网关不支持 `dimensions` 请求，删除该 option，并把 `index.embedding_dimension` 改为它实际返回的维度。
- [阿里 Qwen embedding + rerank + pgvector](../parsecore.pgvector.aliyun-rag.toml.example)：使用已验证的 `qwen/text-embedding-v4`（实际返回 `1024` 维）与 `qwen/qwen3-vl-rerank`，共用 `PARSECORE_ALIYUN_API_KEY`。该网关在典型 PDF 切片验收中拒绝 16-input embedding 请求，因此模板固定 `batch_size=5`；启用前还须迁移或新建匹配 `1024` 维的 pgvector schema。

本地路径仅在 pgvector 数据库已可连接、`.[local-embedding]` 已安装且模型目录已就绪后执行：

```powershell
& '.\.venv\Scripts\python.exe' tools/local_rag_acceptance.py `
  --config parsecore.pgvector.local-embedding.toml.example `
  --out-json var/self-check/local-rag-production-profile.json
```

远程路径仅在 pgvector 数据库已可连接，且真实网关 endpoint、模型和凭证均已通过部署环境注入后执行：

```powershell
& '.\.venv\Scripts\python.exe' tools/_embedding_smoke.py `
  --config parsecore.pgvector.remote-embedding.toml.example `
  --require-live `
  --out-json var/self-check/remote-embedding-smoke.json
```

阿里 Qwen profile 可在数据库验收前先单独验证排序网关，不会写入文档或数据库：

```powershell
& '.\.venv\Scripts\python.exe' tools/_rerank_smoke.py `
  --config parsecore.pgvector.aliyun-rag.toml.example `
  --require-live `
  --out-json var/self-check/aliyun-rerank-smoke.json
```

阿里 Qwen profile 的端到端技术验收可用 `PARSECORE_DATABASE_URL` 临时覆盖模板数据库地址，不需要修改或复制配置文件：

```powershell
$env:PARSECORE_DATABASE_URL = "postgresql://parsecore:replace-me@127.0.0.1:55433/parsecore"
$env:PARSECORE_ALIYUN_API_KEY = "由密钥管理系统临时注入"
& '.\.venv\Scripts\python.exe' tools/_embedding_smoke.py `
  --config parsecore.pgvector.aliyun-rag.toml.example `
  --require-live `
  --out-json var/self-check/aliyun-pgvector-rag-smoke.json
Remove-Item Env:PARSECORE_DATABASE_URL, Env:PARSECORE_ALIYUN_API_KEY
```

`_embedding_smoke.py` 使用受控的临时 DOCX，执行解析、embedding、pgvector 写入、hybrid 检索和可选 rerank；它可以证明技术链路及维度配置正确，但不能替代经批准的典型业务文档相关性验收。独立 `_rerank_smoke.py` 只验证排序 transport，不连接数据库。

对真实外部文档或受控 PDF 页段执行可复现的 production profile 验收：

```powershell
& '.\.venv\Scripts\python.exe' tools/production_rag_acceptance.py `
  --config parsecore.pgvector.aliyun-rag.toml.example `
  --document D:\path\to\approved-manual.pdf `
  --query-suite fixtures\rag\approved-query-suite.json `
  --doc-id approved-manual-pages-204-206 `
  --page-start 204 `
  --page-end 206 `
  --out-json var/self-check/approved-production-rag.json
```

查询套件必须显式声明 `approval_status / top_k / min_hit_rate_at_k / require_rerank / cases[]`；每个 case 提供稳定 `id`、`query` 与至少一个 `expected_any` 短语。工具不会把命中文本写进 JSON，只记录可审计分数和期望短语是否命中。`approval_status` 为 draft 时，即使技术门禁通过也不能称为业务 Gold。

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

### Local Provider Registry

本地 Provider Registry 用于声明内置解析器和候选本地解析引擎，供 `/v1/parse/providers`、`/v1/parse/providers/route-plan`、`/v1/runtime`、`projection=ir` 和灰度路由读取。默认只读，不改变实际解析器；只有显式开启 `providers.local_parser_routing.enabled` 后，ParseCore 才会按 route-plan 的 primary/fallback 在已注册 `[[parsers]]` 中选择实际 parser。

```toml
[providers.local_parser_routing]
enabled = false
fallback_to_default = true
include_disabled = false

[[providers.local_parsers]]
id = "pdf-text"
enabled = true
priority = 100
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default", "table-heavy", "large-pdf"]
capabilities = ["native-text", "layout", "tables", "local-ocr-fallback"]
route_mode = "route"
gate_status = "passed"
gate_checks = ["samples", "license", "performance", "observability"]

[[providers.local_parsers]]
id = "pymupdf4llm-local"
enabled = false
priority = 80
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default", "table-heavy"]
capabilities = ["markdown", "json", "rag-baseline"]
route_mode = "evaluate"
gate_status = "pending"
gate_checks = ["samples", "license", "performance", "observability"]
```

| 字段 | 说明 |
| --- | --- |
| `id` | Provider ID，建议使用 `{engine}-{mode}`，如 `docling-local`。 |
| `enabled` | 是否允许进入灰度路由；`false` 时仍可被发现和评估。 |
| `priority` | 同一文件类型/profile 下的排序权重，数值越高越靠前。 |
| `media_types` / `extensions` | 该 provider 声明支持的输入类型。 |
| `profiles` | 适用的 ParseCore profile。 |
| `capabilities` | 能力标签，例如 `layout`、`tables`、`reading-order`、`rag-baseline`。 |
| `route_mode` | `route` 表示允许进入执行路由候选；`evaluate` 表示仅保留在 registry、route-plan 和 provider-suite 中做评测/对照。 |
| `gate_status` | `passed` / `pending` / `failed`。只有 `passed` 才允许进入执行路由。 |
| `gate_checks` | 当前针对该 provider 记录的门禁维度，推荐固定为 `samples / license / performance / observability`。 |
| `options` | 可选 provider 私有选项；本阶段只配置和透传，不触发外部服务调用。 |

`pymupdf4llm-local` 当前支持将受控选项透传给上游 `to_markdown`，包括 `ignore_graphics`、`graphics_limit`、`table_strategy`、`ignore_images`、`dpi` 等；默认不设置这些选项，保持现状行为。它们只适合离线候选评测或人工批准后的灰度 profile，不能把 `ignore_graphics=true` 直接用于表格文档：第 165 页探针显示该快路径虽可把单页耗时从约 4.99s 降到约 0.1s，但 Markdown 表格分隔符从 630 变为 0。

普通 PDF 的临时调参复跑已记录在 [provider-gold-tuned-ignore-graphics-ordinary-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-tuned-ignore-graphics-ordinary-20260714.json)，该工件不代表准入通过，也不会修改主配置。

`providers.local_parser_routing` 控制实际解析路由：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 默认 `false`，route-plan 仅用于解释和报告；设为 `true` 后才会按 primary/fallback 选择已注册 parser。 |
| `fallback_to_default` | 当 route-plan 选出的 provider 没有对应 `[[parsers]]` 或不支持当前文件时，是否回退到原有 parser 顺序。建议生产保持 `true`。 |
| `include_disabled` | 是否允许 disabled provider 进入执行路由候选。建议保持 `false`，disabled provider 仅用于发现、报告和离线评估。 |

本阶段推荐只把内置主链 provider 设为 `route_mode = "route"` 且 `gate_status = "passed"`；候选 provider 先保持 `route_mode = "evaluate"`，待 adapter、固定样本、许可证、性能基线和可观测性门禁确认后，再切到正式执行路由。开启执行路由后，job options 以及 `structured / ir / coverage / reader / quality / providers` 投影都会写入 `local_provider_routing` 决策，包含 `selected_provider_id / route_status / eligible_provider_ids / requested`，便于回溯当时为什么用了哪个 parser。质量门禁建议本地 Provider 重跑时，`action_suggestions[].context.local_provider_routing` 会同时返回当前路由配置、是否处于 `inspect_only`、以及是否需要先启用 `providers.local_parser_routing.enabled`，避免把只读 route-plan 误当成已生效的执行路由；文本覆盖类能力要求使用 `native-text / local-ocr-fallback`，不把外部 OCR API 作为 route-plan 候选条件。

只读路由计划示例：

```powershell
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/providers/route-plan?file_name=manual.pdf&profile=table-heavy&capability=tables"
```

响应会返回 `selection.primary_provider_id / fallback_provider_ids / candidates[].route_role / candidates[].exclusion_reasons / candidates[].admission`。其中 `evaluation_only`、`gate_pending`、`gate_failed` 会把 provider 保留在对照视图里，但排除出执行路由。默认不会自动替换解析器，只有开启 `providers.local_parser_routing.enabled` 后才参与实际 parser 选择，也不会调用任何外部服务。

## 可选 Provider 安装

`pymupdf4llm-local` 已有可选 adapter。启用前需要安装额外依赖：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[pymupdf4llm]"
```

然后显式增加 parser 配置：

```toml
[[parsers]]
name = "pymupdf4llm-local"
media_types = ["application/pdf"]
extensions = [".pdf"]
options = { page_chunks = true, detect_tables = true }
```

建议先只在测试环境或指定 profile 中使用它做 baseline 对照，不替换默认 `pdf-text` 主链路。

`docling-local` 现也提供可选 adapter。启用前需要安装额外依赖：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[docling]"
```

然后显式增加 parser 配置：

```toml
[[parsers]]
name = "docling-local"
media_types = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]
extensions = [".pdf", ".docx"]
options = { detect_tables = true }
```

`docling-local` 适合作为 PDF / DOCX 的统一结构对照 provider。建议先在 route-plan、provider-suite 或问题页段灰度中使用，再决定是否进入执行路由。

对已确认“无表格、无 OCR 需求”的候选页，可在离线对标配置中显式开启
`fast-text` 组合：

```toml
options = {
  detect_tables = true,
  reuse_converter = true,
  do_ocr = false,
  do_table_structure = false,
  force_backend_text = true,
  layout_batch_size = 1,
  table_batch_size = 1,
  ocr_batch_size = 1,
  queue_max_size = 8,
}
```

该组合不是默认值：它能明显减少文本页耗时，但会丢失表格结构。当前受控结果见
`var/self-check/docling-pipeline-profile-probe-r18.json`，只有在页级信号确认无表格/OCR
需求后才可作为候选 fallback。

`reuse_converter = true` 是另一个显式候选开关：它在同一个长生命周期 worker
中复用已构造的 `DocumentConverter`，适合分片重试或重复解析，减少模型/布局资源的
冷启动；10 页受控 probe 观察到 cold `29.883 s` → warm `13.299 s`（约 `-55.5%`），
结构指纹保持一致，且首块 provenance 会写入 `converter_reuse_enabled` 与
`converter_cache_hit`，便于对比报告区分冷/热解析，详见
`var/self-check/docling-reuse-probe-r19.json`。默认关闭，
且不会把并发安全假设带入 `pdf-text` 主路线；仍需全量、工作集和并发复测。

在 Windows 中文路径的虚拟环境中，`DoclingParser` 会在首次加载时为
`docling-parse` 的原生 glyph 资源创建临时 ASCII junction；如果运行环境禁止
junction，则退回一次性 ASCII 目录复制。这样无需手工设置 `PYTHONPATH`，也不会
改变默认 `pdf-text` 路由。若日志仍出现 `additional.dat does not exist`，请检查
临时目录写权限并重新安装 `.[docling]`。

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
| `pymupdf4llm-local` | `.pdf` | 可选 PDF Markdown/JSON baseline provider，需安装 `.[pymupdf4llm]`。 |
| `docling-local` | `.pdf` / `.docx` | 可选 Docling 统一结构 provider，需安装 `.[docling]`。 |
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
| `parse_cache` | 是否在同一 worker 内缓存同源重解析结果；默认库行为关闭，生产配置可按固定样本验证后开启。 |
| `parse_cache_max_entries` | 同一 `pdf-text` parser 的缓存 LRU 上限，控制重解析缓存的内存边界。 |

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
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli payload-contract-check
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli p1-contract-acceptance --out var/self-check/p1-contract-acceptance-20260715.json
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

该报告除 `elapsed_s / peak_kb / mb_per_s / blocks / chunks / tables` 外，还会为每个样本写入 `provider_report.comparison_report`，并在 Markdown 中展示 `primary_provider / best_provider / provider_score`，用于把固定样本性能与本地 Provider 对比口径放在同一份报告中。`parse_perf_baseline.py` 为兼容历史内存基线默认启用 `tracemalloc`；只测量低干扰端到端延迟时追加 `--no-track-python-memory`，此时 `peak_kb=null`，Provider 内存轴保持 pending，不能与内存追踪通道的耗时混作同一序列。独立的 Provider 对比工具默认不启用 `tracemalloc`，`elapsed_s` 只计 parser 执行时间；需要 Python 峰值内存诊断时显式追加 `--track-python-memory`，但该模式不应作为真实耗时 SLA。

本地 Provider 离线对比：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --sample D:/samples/manual.pdf --provider pdf-text --provider pymupdf4llm-local --out-json var/self-check/provider-comparison.json --out-md var/self-check/provider-comparison.md
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/suite.fast.json --fixture-root D:/app/uploads --provider pdf-text --provider pymupdf4llm-local --out-json var/self-check/provider-comparison.suite.json --out-md var/self-check/provider-comparison.suite.md
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --sample D:/samples/manual.pdf --provider pdf-text --page-start 200 --page-end 230 --out-json var/self-check/provider-comparison.part.json --out-md var/self-check/provider-comparison.part.md
```

该工具对同一样本逐个运行已配置的本地 parser，输出 `ir_summary / coverage_summary / rag_coverage_quality / provider_report.comparison_report`。未配置、依赖缺失或不支持文件类型的候选会写成 `skipped/failed`，不会中断整批样本；远程 OCR provider 会在该离线工具中禁用，避免 Provider 对比时触发外部 OCR API。真正失败的 provider 还会写入 `failure_category`，并在 gate summary 的 `failure_categories` 中汇总 `provider_unavailable / timeout / invalid_input / permission_denied / unsupported / provider_failed`，用于区分环境阻塞与解析器运行退化。若候选暴露了 Docling converter telemetry，单次 run 还会带 `converter_reuse_enabled / converter_cache_hit / converter_cache_state`，suite summary 汇总 `converter_cache.observations / hits / misses`，方便把冷启动成本从稳态吞吐中拆出来。`--suite` 可读取新的 `samples / fixtures / cases` 清单，也可直接复用现有 `entries -> baseline -> fixtures` 回归套件；每个样本可用 `providers / provider_ids / profile / fixture_relative_path / page_range` 覆盖全局参数，suite 顶层还可声明 `gate_policy.max_provider_reading_order_warning_runs`、`gate_policy.max_provider_quality_warning_runs`、`gate_policy.max_samples_best_provider_differs_from_route_primary`、`gate_policy.max_providers_with_multiple_provider_versions`、`gate_policy.max_providers_with_multiple_adapter_versions` 这类基础门禁预算。仓库当前内置了 `var/regression/provider-suite.fast.json`、`var/regression/provider-suite.full.json` 与 `var/regression/provider-suite.perf.json` 三套工件，分别对应 `fast`、`full` 与 `perf` 自检默认 Provider 门禁。默认 auto route-plan 模式下，报告仍会保留 disabled/未接入候选的 `skipped` 解释，但 gate 不再把这类 `parser_not_configured` / `unsupported_media_type_or_extension` 直接当成 warning；只有显式指定的 Provider 被跳过，或出现非预期 skipped，才会进入 `provider_runs_skipped`。现在每次 provider run 还会显式回填 `provider_version / adapter_version`，suite 顶层新增 `provider_identity_summary`，用于追踪“同一个 provider id 实际跑的是哪版上游库/哪版 Adapter”；一旦同名 Provider 混入多版上游库或多版 Adapter，gate 会先给出 drift warning，再按上面的 budget 决定是否 fail。与此同时，报告还会输出 `provider_admission_summary`，把 suite 结果直接翻译成每个 provider 的 `recommended_admission.route_mode / gate_status / route_ready`、`recommended_action` 与 `requires_config_update`，并补齐 `drift_fields / drift_details / config_patch`，便于把对比结论直接回写到 `providers.local_parsers` 配置。如果希望把“准入建议还没收敛”也纳入 fail gate，suite 顶层还支持 `gate_policy.max_providers_requiring_config_update`、`gate_policy.max_providers_with_route_mode_drift`、`gate_policy.max_providers_with_gate_status_drift`、`gate_policy.max_providers_with_gate_checks_drift`、`gate_policy.max_providers_with_route_ready_drift` 五类 admission drift 预算，分别约束待回写 provider 数量以及 `route_mode / gate_status / gate_checks / route_ready` 四类配置漂移。`parsecore self-check` 在实际执行 provider comparison 时，会把 identity 和 admission 两份摘要一起带回检查结果，并在 summary 中追加 `identity_drift / admission_ready / admission_update` 计数。与此同时，self-check 仍会在输出目录自动落盘 `provider-comparison.fast/full/perf.json/.md`，并把路径写回 self-check JSON。`--fixture-root` 或 `PARSECORE_REGRESSION_FIXTURE_ROOT` 用于跨机器恢复真实样本路径。对 PDF 还支持 `--page-start / --page-end` 或 suite 内 `page_range` 局部评估：工具会先切出 part 文件，再把 IR / coverage / provider_report 页码平移回原始页号，便于大文件异常页和采样页灰度对比。

Provider comparison 与运行时 embedding/rerank 共用固定低基数失败枚举：`invalid_input / permission_denied / rate_limited / timeout / provider_unavailable / invalid_response / configuration_error / unsupported / provider_failed`。运行时只对终态失败写入 `provider_failure` 事件和 `parse_provider_failure_total`，重试继续使用原有 `embedding_retry` 计数，避免重复放大故障；失败分类仅用于诊断，不改变重试、fallback 或检索排序。

如需在同一 suite 内测量候选 Provider 的 warm-state，可追加 `--reuse-parser-instances`，让每个 Provider 在所有样本间复用一个 parser 实例，并在 measurement 中标记 `parser_lifecycle=provider_instance_reused`；默认关闭，仍按 `new_per_run` 生成冷启动基线，不能把两种口径直接混合比较。

`pdf-text` 若启用了 `post_process.parse_cache`，Provider 对比还会读取首块 provenance 中的
`parse_cache_state=cold|warm`，并在 suite summary 汇总 `parse_cache.observations / hits / misses`，用于量化同源重解析或 part retry 的缓存收益。

### P2 人工确认 gold corpus

`tools/provider_gold_evaluation.py` 在既有离线对比结果上执行页级准入评分。它只读取本地副本并写出报告，绝不修改 provider 配置、默认路由、活动产物或线上任务。每一个候选 block 都保留页码、位置、`provider_id / provider_version / source_kind` 和表格 cell 证据，以支持人工复核。

仓库提供 [gold-corpus-v1.json](../fixtures/provider-evaluation/gold-corpus-v1.json) 作为受控入口；当前已导入 50 个 `approved` review-queue 页面。2026-07-14 本次批准由用户明确授权的 `Codex (AI-assisted, user-authorized)` 完成：它核对源截图/文本探针、SHA-256、`pdf-text` 基线块顺序与页级探针，并生成受限 expected labels；这不是独立人工 gold，也不改变 Provider 许可证或路由准入。若治理要求人工签字，仍需由 named reviewer 逐页复核。候选在至少 50 页受控批准前只能保持 shadow-only；当前即使达到 50 页，仍须通过许可证、结构质量和稳定运行门禁。

实际审核入口分为两处：先打开 [RISK_REVIEW.md](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\RISK_REVIEW.md)（前 20 个高风险页）或证据包 [README.md](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\README.md)（全部 50 页），查看对应 PNG 和文本探针；然后只在受控队列 [gold-review-queue-v1.json](D:\个人文件\个人开发\解析管理中台\fixtures\provider-evaluation\gold-review-queue-v1.json) 中填写 `review.reviewer / review.reviewed_at / review.source_screenshot / review.notes`、`expected.blockKinds / anchors / orderedAnchors / tableAnchors / criticalTokens / mustNotBeHeading`，最后把该页 `review_status` 改为 `approved` 或 `rejected`。`manifest.json`、`RISK_REVIEW.md`、`provider-gold-pending-full-20260714.json` 和 `var/self-check` 下的生成副本都是证据/报告，不是审批入口；gold corpus 会按 `imports` 自动读取受控队列。

编辑后可用只读校验器检查队列，不会自动修改任何页面：

```powershell
py tools/provider_gold_review_status.py `
  --queue fixtures/provider-evaluation/gold-review-queue-v1.json `
  --evidence-root output/pdf/provider-gold-review-20260714 `
  --out-json var/self-check/provider-gold-review-status-20260714.json `
  --require-minimum
```

`--require-minimum` 会在 approved 页数未达到 50 页时返回非零；当前队列校验结果为 `status=ready, 50 approved / 0 pending / 0 rejected / 0 errors`。只有校验达到 `status=ready` 后，才继续运行 Provider gold evaluation。

本次用户已明确授权由 Codex 执行 AI 辅助核验，可复现命令如下；默认是 dry-run，只有追加 `--approve` 才会写入受控队列。该工具会把审核范围写成 `ai_assisted_review_not_human_gold`，不会修改 Provider 路由或许可证：

```powershell
py tools/ai_gold_review.py `
  --queue fixtures/provider-evaluation/gold-review-queue-v1.json `
  --manifest output/pdf/provider-gold-review-20260714/manifest.json `
  --evaluation var/self-check/provider-gold-pending-full-20260714.json `
  --out-queue fixtures/provider-evaluation/gold-review-queue-v1.json `
  --audit-out var/self-check/provider-gold-ai-review-20260714.json `
  --approve
```

批准后的状态审计工件为 [provider-gold-review-status-20260714-ai-r2.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-review-status-20260714-ai-r2.json)，AI 审核审计为 [provider-gold-ai-review-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-review-20260714.json)。

源文件映射必须留在本机或受控工件存储，不能提交业务 PDF 或凭据。以 [source-map.example.json](../fixtures/provider-evaluation/source-map.example.json) 复制出本地映射后运行：

```powershell
py tools/provider_gold_evaluation.py --config parsecore.toml --gold-corpus fixtures/provider-evaluation/gold-corpus-v1.json --source-map D:/secure/provider-gold-source-map.json --provider pymupdf4llm-local --provider docling-local --baseline-provider pdf-text --out-json var/self-check/provider-gold-v1.json
```

若尚未选出 50–100 页，可先生成待审核而非已批准的模板；两份文档各取 25 页即可形成 50 页初始队列：

```powershell
py tools/provider_gold_review_queue.py --source-map D:/secure/provider-gold-source-map.json --pages-per-document 25 --out-json D:/secure/provider-gold-review-queue.json
```

生成器不会调用 parser，也不会填充标签或将任何页面标为 `approved`。评审人必须在队列中填入 screenshot、block kinds、anchors、顺序、table anchors、关键 token 和页眉页脚排除项；完成后才能将相应记录合并至 gold corpus。

如果需要把原始页截图和文本探针一次性准备好，可在受控本地目录生成只读 evidence packet：

```powershell
py tools/provider_gold_evidence.py `
  --queue var/self-check/p0-gold-review-queue-20260714.json `
  --source-map var/self-check/p0-gold-source-map-20260714.json `
  --out-dir output/pdf/provider-gold-review-20260714 `
  --dpi 150 `
  --evaluation-json var/self-check/provider-gold-pending-full-20260714.json
```

该命令会写出 `manifest.json`、`README.md`、每页 PNG 和 pypdf 文本探针；输出中的 `review_status` 保持 `pending`，不能替代人工 gold，也不会改写 `fixtures/provider-evaluation/gold-corpus-v1.json`。

2026-07-14 已实际生成 [provider-gold-review-20260714/manifest.json](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\manifest.json)，50/50 页证据就绪，当前状态为 `approved=50 / pending=0 / rejected=0`；原始待审核双跑记录仍保留在 [provider-gold-pending-full-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-pending-full-20260714.json)，批准后真实耗时复评记录为 [provider-gold-ai-approved-20260714-r2.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-approved-20260714-r2.json)。最新复评中候选 50/50 完成、基线 49/50，候选仍因许可证、结构风险和长尾被阻断，不会自动修改 route。

执行 `provider_gold_evidence.py` 时追加 `--evaluation-json var/self-check/provider-gold-pending-full-20260714.json`，会在证据目录生成 [RISK_REVIEW.md](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\RISK_REVIEW.md)，把前 20 个风险页映射到 PNG/文本探针；它不会改变任何 `review_status`。

评测报告中的 `gold_evaluation.risk_summary` 是只读的人工复核排序：`provider_metrics` 给出平均/p95/最大耗时与结构总量，`document_metrics` 按文档聚合，`priority_pages` 列出候选长尾、基线缺失、block/table/figure 变化页；它不把 pending 页面转换为 gold，也不改变准入结论。

可先使用重复的 `--document-id` 只诊断一个已映射文档，例如 `--document-id doc-62f0d9e0-3536-42d9-955c-9ea7447595b8 --include-seed`。这仅方便标注和问题页检查，绝不会因为子集通过而晋升 provider。

评分权重为完整性 25、reading order 20、表格结构 25、标题层级 15、关键 token 10、运行成本 5。缺页/乱序/重复、关键 token 丢失、表格锚点缺失、block provenance 缺失，以及未获许可证与数据合规批准，都会触发 hard veto。报告即使给出 `manual_canary_config_review`，也只是建议：还必须达到较生产 provider 至少 +5 分、reading order 与表格不回退、连续三轮稳定，并通过独立配置评审后，才可将指定候选 profile 灰度为 `route`。默认 `pdf-text` 路由不会被该工具改写。

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

最小运维面板建议至少展示以下字段：

| 面板区域 | 数据来源 | 关键字段 |
| --- | --- | --- |
| API 健康 | `/health`、`/v1/runtime` | 当前配置、execution_mode、api_auth_enabled、payload_schemas、provider_registry.summary |
| 队列与任务 | `/v1/parse/metrics`、`/v1/parse/events`、`/v1/parse/jobs/{job_id}` | active/pending/failed/done、duration p95/p99、retry/dead_letter、claim_token、lease_expires_at |
| 文档质量 | `/v1/parse/documents/{doc_id}/quality`、`/v1/parse/events?event_type=document_quality`、`/v1/parse/prometheus` | quality_gate.gate、recommended_action、attention_summary、quality_signal_counts、coverage/embedding ratio、provider warning、`parse_document_quality_total` |
| RAG 覆盖 | `/coverage`、`projection=coverage` | text_page_coverage_ratio、table_unit_coverage_ratio、unit_chunk_coverage_ratio、gap_unit_ids |
| Provider 灰度与故障 | `/providers`、`/v1/parse/providers/route-plan`、`/v1/parse/events?event_type=provider_failure`、`/v1/parse/prometheus` | primary_provider_id、best_provider_id、route_status、excluded reasons、admission drift、provider_type/provider_id/failure_category、`parse_provider_failure_total` |
| Part 运维 | `/parts`、rerun response contracts | warning/failed parts、rerun_status、provider_changed_parts、monitor_requests、verify_requests |
| 导出与工件 | export job manifest、retention 配置 | export status、file count、record count、artifact age、retention_seconds |

事件日志 `runtime.log_path` 默认写入 `var/logs/job_events.jsonl`。事件字段应按 `job_id / doc_id / part_id / tenant_id / stage / error_category` 建索引；`api_key / token / authorization / secret / password` 等敏感字段会在事件日志写入前脱敏。

完成态解析还会向进程内 EventAggregator 写入脱敏 `document_quality` 事件。它使用与 `/quality` 相同的配置阈值、coverage/embedding 语义和 report-only gate 优先级，但从本次 blocks/index manifest 生成轻量运维摘要，不在 execute 写路径中重建完整 Provider/part 诊断投影；`/quality` 仍是详细诊断的权威接口。事件只携带定位 ID、固定 `quality_gate / quality_flags`、质量分、质量信号数、Provider warning 数、coverage ratio 和 embedding ratio，不携带文档正文或原始异常。分片父文档只在全部 part 合并并进入 `DONE` 后记录最终观测。异步任务的 job 状态可能比该观测先短暂可见，接入方应按最终一致方式查询事件。

Prometheus 暴露 `parse_document_quality_total{gate}`、`parse_document_quality_flag_total{flag}`，以及 `parse_document_quality_score`、`parse_document_text_page_coverage_ratio`、`parse_document_table_unit_coverage_ratio`、`parse_document_unit_chunk_coverage_ratio`、`parse_document_embedded_chunk_ratio`、`parse_document_quality_signal_count`、`parse_document_provider_warning_count` 的 `_sum/_count`。这些指标不使用 `doc_id` 标签，未知 gate/flag 归并为 `other`，避免高基数；当前聚合随进程重启清零，生产部署应由宿主 Prometheus 持续抓取 `/v1/parse/prometheus`。

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

`large-pdf-catalog` / `large-pdf-ledger` 默认使用 fast text path：跳过重型 pdfplumber 双通道布局、阅读顺序和坏页 OCR fallback，优先保留原生文本行供 records 聚合。若某份目录/台账需要高保真表格块或 OCR，可在创建 job 时显式传 `post_process.dual_channel=true`、`post_process.layout_reading_order=true` 或 `enable_ocr=true`。

`quality_signals` 后续会保持追加式扩展。宿主 schema 建议把它当数组 JSON 存储，不要只按当前 code 建固定列；消费时按 `code / severity / page_number / table_id / row_index / col_index / bbox` 做宽松解析，未知 code 默认展示或记录，不阻断主流程。

## 导出中心与复跑规划

当前版本已提供同步导出 MVP，适合中小结果集或排查场景：

```powershell
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=pages&format=jsonl" -OutFile pages.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=lines&format=csv" -OutFile lines.csv
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=tables&format=csv" -OutFile tables.csv
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=quality_signals&format=jsonl" -OutFile quality_signals.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=coverage&format=jsonl" -OutFile coverage_report.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=reader&format=jsonl" -OutFile reader_blocks.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=parse_units&format=tsv" -OutFile parse_units.tsv
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=jsonl" -OutFile records.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=sqlite" -OutFile records.sqlite
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=xlsx" -OutFile records.xlsx
```

支持参数：

- `dataset=pages|lines|tables|quality_signals|coverage|reader|parse_units|records`
- `format=jsonl|csv|tsv|sqlite|xlsx`
- `tenant_id=...` 可选，默认 `default`

解析完成后，中台会把结构化 `pages / lines / records` 作为 document views 持久化到当前 JobStore；records 查询会优先读取持久化结果，缺失时再回退到 block 现场投影。主文档快照默认不再携带 `pages / lines / records` views，页面/行导出会按需加载对应 view，记录级查询入口则用于分页读取派生 records，避免把大结果集塞进主文档响应。records 可能来自结构化表格行，也可能来自 `large-pdf-catalog` / `large-pdf-ledger` 下按序号行聚合出的文本记录；消费方应按 `source / fields / raw_text / normalized_text / page_start / page_end` 宽松解析。

```text
GET /v1/parse/documents/{doc_id}/records?limit=100&offset=0
GET /v1/parse/documents/{doc_id}/records?query=TC001A
GET /v1/parse/documents/{doc_id}/records?page_start=2000&page_end=2300
GET /v1/parse/documents/{doc_id}/records?quality_signal=column_shift_suspected
GET /v1/parse/documents/{doc_id}/records?field.certificate_or_project_no=PMA0013-01-XN
GET /v1/parse/documents/{doc_id}/records?field=holder_or_name_start&value=重庆
GET /v1/parse/documents/{doc_id}/coverage
GET /v1/parse/documents/{doc_id}/providers
GET /v1/parse/documents/{doc_id}/reader
GET /v1/parse/documents/{doc_id}?projection=reader
GET /v1/parse/schemas
GET /v1/parse/schemas/document-coverage
GET /v1/parse/schemas/document-ir
GET /v1/parse/schemas/document-parts
GET /v1/parse/schemas/document-providers
GET /v1/parse/schemas/document-quality
GET /v1/parse/schemas/document-reader
```

`/quality` 返回文档级质量总览，除 `quality / quality_signals / quality_gate / rag_coverage_quality / parse_units` 外，当前还会补 `provider_diagnostics`、`parts_diagnostics` 与 `attention_summary`，让宿主在单次请求里同时看到 Provider 对比摘要、comparison actions、part 汇总、attention parts，以及一组已按优先级合并好的 `recommended_actions`。`attention_summary` 还会直接给出 `recommended_focus / recommended_action / recommended_entrypoint`，用来回答“下一步先看 quality、providers 还是 parts”；`attention_summary.entrypoints` 则会给出 `quality / providers / parts / coverage` 四个诊断视图的 endpoint、attention 状态、badge 数和推荐落点，适合驱动宿主的导航和红点。对于宿主联调更直接的筛选需求，`providers.context` 会带 `primary_provider_id / best_provider_id / attention_provider_ids`，`parts.context` 会带 `attention_part_ids / rerun_part_ids / provider_changed_part_ids / coverage_gap_part_ids / coverage_gap_unit_part_ids / rerun_gap_unit_part_ids / unembedded_part_ids / gap_unit_ids`，`coverage.context` 会带 `gap_page_numbers / pages_missing_rag_units / pages_missing_chunks / pages_chunks_not_embedded`。同时，`parts_diagnostics.attention_parts[]` 也已补齐 `coverage_gap_unit_count / gap_unit_ids / unembedded_unit_count / gap_unit_count_delta / gap_unit_ids_added / gap_unit_ids_removed`，方便宿主在不展开 `/parts` 明细页的前提下直接判断哪个 part 还有哪些 KnowledgeUnit 缺口、rerun 后是新增还是减少。另外，`attention_summary.contracts.default_request / preferred_execute_request / recommended_requests / inspect_requests / execute_requests / entrypoint_requests / parts_batch_rerun_requests / workflow` 会把这些建议进一步规范成统一 request 结构，适合宿主直接提交下钻或批量 rerun，并把查看类与执行类请求分开消费；其中 `workflow` 会显式描述 `inspect -> compare -> execute -> verify` 四阶段，帮助宿主把诊断页、Provider 复核和 part/document 复跑串成固定流程。`/coverage` 返回页级 RAG 覆盖报告和 `rag_coverage_quality`，包括 `rag_empty_text_page / rag_units_without_chunks / rag_chunks_not_embedded / rag_table_without_unit / rag_figure_caption_missing` 等信号；`/providers` 返回该文档实际使用的 Provider footprint，包括 provider 汇总、页级 `provider_ids`、覆盖缺口和 RAG 类质量信号，适合灰度对比、排版问题定位和 Provider 路由审计。`/providers.comparison_report` 会按 Provider 输出 `rankings / score / recommendation / axes`，当前覆盖文本覆盖、表格 unit、图示 caption、RAG chunk/embedding 风险，并会在 Provider provenance 提供时汇总 `reading_order_confidence / provider_elapsed_s / provider_memory_mb`；未观测到的字段会进入 `pending_axes`。为方便接入方直接消费，`comparison_report.summary` 现还会输出 `primary_provider_rank / primary_provider_score / primary_provider_recommendation / best_provider_score / best_provider_recommendation / best_provider_differs_from_primary / providers_with_quality_warnings / providers_with_reading_order_warning / providers_with_coverage_gaps / quality_warning_provider_ids / reading_order_warning_provider_ids / coverage_gap_provider_ids / attention_provider_ids / needs_attention / recommended_action`，前端无需再逐条扫描 `rankings` 才能做质量面板或 Provider 复核提示。`/providers` 顶层同时新增 `comparison_actions`，会按当前文档的 Provider 对比态势直接给出 `inspect_provider_comparison`、`inspect_provider_route_plan` 一类只读动作建议；同一份 payload 的 `quality_gate.provider_comparison` 也会带上同样的摘要和动作，并把这些动作合并进 `quality_gate.action_suggestions`，适合作为宿主产品的单一诊断入口。`/parts` 会在每个 part 上返回 `action_suggestions`，用于把 warning/failed 页段映射到 part 级复跑、重建 chunks、重建 embeddings 或质量报告查看入口。

运行期 `index_manifest` 以及 `structured / ir / coverage` 的 `index_manifest` 会追加 `rag_coverage` 摘要。该摘要记录 KnowledgeUnit 到 chunk 的覆盖关系，包括 `unit_count / indexable_unit_count / skipped_unit_count / chunked_unit_count / embedded_chunk_count / embedded_unit_count / unembedded_unit_count / coverage_score / chunk_ids`；其中 `embedded_unit_count` 只统计“至少有一个 chunk 且该 unit 的全部 chunk 都已写入向量”的可索引 KnowledgeUnit，`unembedded_unit_count` 统计已有 chunk 但尚未全部向量化的可索引 KnowledgeUnit。`units[]` 保留每个 unit 的 `page_span / source_block_ids / source_table_ids / should_index_for_rag / skip_reason / chunk_ids / embedded`，用于宿主产品展示“哪些页进了 RAG，哪些没进，为什么”。

`/reader` 与 `projection=reader` 返回阅读页专用结构：顶层包含 `pages / blocks / reader_summary / quality_signals / quality_gate / index_manifest`。每个 reader block 带 `type / display_kind / reader_policy / text / source_block_ids / source_table_ids / source_figure_ids / bbox / provenance`；表格块额外带结构化 `table`，图示块额外带 `figure`。`reader_policy = hidden` 的页眉页脚、解析工件默认不进入 `blocks`，只在页级 `hidden_block_count` 中计数。

`/v1/parse/schemas` 会列出当前已经冻结的 payload contract；当前已提供 `document-coverage / document-ir / document-parts / document-providers / document-quality / document-reader` 六份 JSON Schema，对应覆盖审计、统一 IR、part 诊断、Provider 诊断、质量诊断和阅读页主链。宿主可以直接用这些 schema 做联调、自测或版本门禁；`/v1/runtime` 的 `payload_schemas` 字段也会返回同一份摘要列表，便于启动后自动发现。

同步导出已把 `pages / lines / tables / quality_signals / coverage / reader / parse_units / records` 都纳入正式 dataset。`coverage` 对应页级 RAG 覆盖报告，默认 JSONL 文件名可作为 `coverage_report.jsonl` 保存；`reader` 对应 `projection=reader` 的 reader blocks，可直接用于阅读页联调、排版质量抽检和结构化表格/图示回归。同步 records 导出也可带相同的 `query / page_start / page_end / quality_signal / field.*` 参数，用于直接下载筛选后的 records。CSV/TSV/XLSX/SQLite 会把嵌套字段如 `cells/detail/warnings/fields/table/figure/knowledge_units` 稳定序列化成 JSON 字符串。SQLite 导出会生成一个与 dataset 同名的数据表，适合大结果集的离线查询；XLSX 更适合给业务人员抽检 compact records。当前也已提供异步导出包 MVP：

```text
POST /v1/parse/documents/{doc_id}/export-jobs
GET  /v1/parse/export-jobs/{export_id}
GET  /v1/parse/export-jobs/{export_id}/download?file=quality_signals.jsonl
```

异步包会生成 `manifest.json` 以及按 include/formats 指定的 `pages.jsonl / lines.csv / tables.csv / quality_signals.jsonl / coverage.jsonl / reader.jsonl / parse_units.tsv / records.jsonl / records.sqlite / records.xlsx`。manifest 会记录 `manifest_schema_version / tenant_id / schema_version / parse_run_id / profile / profile_resolution / request.include / request.formats / request.filters`，每个文件条目包含 `dataset / format / path / content_type / bytes / records`。`filters` 当前支持 `page_range`、`severity`、`quality_signal`，并可用 `fields` 或 `field_filters` 对 records 字段做筛选，例如 `{"fields":{"certificate_or_project_no":"PMA0013"}}`；其中 `page_range` 会同时作用于 `pages / lines / tables / quality_signals / coverage / reader / parse_units / records`，`quality_signal` 也可筛选 coverage/reader 的 `quality_signal_codes`。异步包写出 `jsonl/csv/tsv/sqlite/xlsx` 时会直接落盘，避免全量 records 先序列化成大 bytes。`parquet`、异常页截图、raw cells 与 trace 打包作为后续增强。

当前也已提供 PDF part 调度与复跑第一版，供宿主产品按页段排障和小范围重跑：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts/plan" -Body '{"target_pages_per_part":200}' -ContentType "application/json"
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts" | ConvertFrom-Json
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts?state=warning|failed" | ConvertFrom-Json
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/demo-doc/parts/demo-doc-part-3/rerun"
```

`/parts/plan` 会基于最新 PDF job 轻量探测页数，生成物理 part PDF 和子 job。每个子 job 使用独立 `part_doc_id`，避免覆盖父文档；子 part 完成后会刷新父文档的 blocks/chunks、structured projection、`parse_units`、document views 和 part 状态。当前 store 已支持按 part 前缀替换 `pages / lines / records`，因此单 part 复跑会优先局部替换对应视图；无法局部替换时再回退到父文档视图重建。`/parts` 返回 `part_id / page_range / state / quality_signal_codes / severity_counts / job_id / rerun_supported`，并会在可用时补充 `provider_route_plan / local_provider_routing / provider_ids / selected_provider_id / route_status / coverage_summary / coverage_gap_pages / rag_coverage_quality / previous_part_observation / rerun_comparison / diagnostics`，便于观察局部复跑究竟按什么能力要求选中了哪个本地 Provider、这个页段当前还有哪些 RAG 覆盖缺口，以及这次 rerun 相比上一轮到底是改善、退化还是仅更换了 Provider。当前 `coverage_summary / coverage_gap_pages / diagnostics / rerun_comparison` 已继续向 unit 级口径收敛：会补 `gap_unit_ids / coverage_gap_unit_count / unembedded_unit_count / gap_unit_count_delta / gap_unit_ids_added / gap_unit_ids_removed`，让 part 运维可以从页段直接定位到具体 KnowledgeUnit。`diagnostics` 会收口 `rerun_status / provider_changed / previous_selected_provider_id / current_selected_provider_id / quality_signal_count_delta / coverage_gap_delta / recommended_focus` 等宿主更适合直接渲染的字段；顶层 `part_summary` 也会汇总 `rerun_compared_parts / rerun_statuses / provider_changed_parts / selected_provider_ids`。若 `rerun_comparison.status` 已显示 `unchanged / regressed / mixed`，part 级 `action_suggestions` 会优先建议查看 `projection=ir` 或检查 Provider route-plan，而不是默认继续重复 rerun；文档级 `quality_gate` 的 `rerun_warning_parts` 也会读取同一份 `rerun_comparison`，把已有 rerun 对比记录的 warning part 自动移出批量复跑候选。

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
  "profile": "auto",
  "provider_route_plan": {
    "required_capabilities": ["tables", "layout"]
  }
}
```

无论是 `POST /parts/rerun` 还是 `POST /parts/{part_id}/rerun`，响应体现在都会补 `previous_part_observation` 与 `contracts`。其中 `contracts.monitor_requests` 会给出每个 rerun job 的 `GET /v1/parse/jobs/{job_id}` 监控入口，`contracts.verify_requests` 会固定挂出 `/parts?state=warning|failed`、`/quality`、`/coverage` 三个验收入口，`preferred_verify_request` 默认指向 `/parts`，`workflow` 则把执行后的推荐顺序收敛成 `monitor -> verify`。这样宿主产品在触发局部复跑后，不需要再自己拼“看 job、看 part、看 coverage”的后续跳转。

当请求里带 `provider_route_plan.required_capabilities` 时，part 复跑会把这组能力要求写入子 job options，供开启了 `providers.local_parser_routing.enabled` 的 runtime 在真正执行前重新计算一次本地 Provider 路由。part job 不再盲继承源 job 的旧 `local_provider_routing` 决策；如果只是切 profile 或切 capability，也会按当前 part 文件重新选 primary/fallback。

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
