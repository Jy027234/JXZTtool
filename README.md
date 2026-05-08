# ParseCore Starter Kit

基于现有《解析设计》沉淀出的首版可嵌入解析管理骨架。

目标不是先做大而全的平台，而是先交付一个可植入其他产品的解析能力内核，满足以下约束：

- 嵌入优先，未来可外拔为独立服务
- 解析异步优先，RAG 保留同步和异步两种调用模式
- 以 Block / Chunk 为统一结构，不直接围绕全文字符串建模
- LLM 仅用于增强环节，不承担全文主解析

## 能力边界

ParseCore 当前只负责解析流水线内的公共能力，不吞并宿主产品的检索与业务判断：

- 负责：文档解析、Block/Chunk 生成、结构化 metadata、可选 embedding 产出、异步任务与重跑
- 不负责：RAG 检索 API、向量库产品选型、合规比对、SOP/工卡匹配、业务规则判定
- 宿主产品可直接消费 `semantic_role`、`embedding`、layout metadata 等字段，自行决定写入 pgvector、Qdrant 或其他检索层

## 当前交付范围

当前仓库提供的是 Starter Kit，而不是完整业务实现：

- 项目目录骨架
- 核心数据模型与协议接口
- 可运行的最小 Runtime
- 可挂载的 ASGI API
- SQLite 持久化 JobStore 与查询接口
- 真实 DOCX 解析器与文本解析器
- Excel `.xls/.xlsx/.xlsm` 原生表格解析器
- PDF / OCR 结构块 `semantic_role` 标注（如 `toc_entry`、`highlights_entry`、`warning`）
- 可选 OpenAI-compatible embedding provider 与 chunk 级 embedding 落库
- 可切换的 `inline` / `queue-worker` 执行模式
- 同步上传入口的文件大小保护与分层 CI 门禁
- 可选 API key 入口鉴权（`x-api-key` / `Authorization: Bearer`，`/health` 例外）
- 独立 worker 入口与容器运行骨架
- 配置模板
- 基础单元测试

## 建议演进路径

1. 先在当前仓库把 ParseCore 的契约、状态机和产品接入边界定稿。
2. 再把真实解析器、任务队列、数据库和向量检索逐步替换进来。
3. 以 ParseCore 自检门禁、可选入口鉴权和受控灰度接入目标产品；单一宿主历史资料只保留为归档证据。

## 文档导航

- [docs/ocr-gateway-contract.md](docs/ocr-gateway-contract.md)：`remote-http` OCR 网关的固定请求/响应契约与验收口径
- [docs/ocr-integration-checklist.md](docs/ocr-integration-checklist.md)：宿主接 OCR provider 前的配置、探活、事件与回滚检查清单
- [docs/configuration.md](docs/configuration.md)：配置文件选择、配置项说明、常用场景与验收命令
- [docs/go-live-readiness.md](docs/go-live-readiness.md)：主线版本进入产品灰度前的必做项、遗留问题分级与回滚口径
- [docs/self-check-gate.md](docs/self-check-gate.md)：默认自检门禁、退出码语义与当前性能/可靠性结论
- [docs/performance-stability.md](docs/performance-stability.md)：分层 CI、上传保护与 OCR benchmark 的执行口径
- [docs/gray-deployment.md](docs/gray-deployment.md)：queue-worker + Postgres + pgvector 灰度推荐配置与回滚口径

## 目录结构

```text
.
├─ archive/
├─ docs/
│  ├─ architecture.md
│  ├─ configuration.md
│  ├─ go-live-readiness.md
│  ├─ gray-deployment.md
│  ├─ implementation-plan.md
│  ├─ ocr-integration-checklist.md
│  ├─ ocr-gateway-contract.md
│  ├─ performance-stability.md
│  └─ self-check-gate.md
├─ src/
│  └─ parsecore/
│     ├─ __init__.py
│     ├─ asgi.py
│     ├─ bootstrap.py
│     ├─ cli.py
│     ├─ config.py
│     ├─ contracts.py
│     ├─ models.py
│     ├─ runtime.py
│     ├─ stores.py
│     ├─ worker.py
│     └─ stubs.py
├─ tests/
│  ├─ test_asgi.py
│  └─ test_runtime.py
├─ .dockerignore
├─ Dockerfile
├─ app.py
├─ docker-compose.yml
├─ parsecore.toml
├─ parsecore.queue.toml
├─ parsecore.pgvector.toml.example
├─ parsecore.pgvector.fake-embedding.toml.example
├─ parsecore.remote-http.toml.example
└─ pyproject.toml
```

## 快速开始

安装为开发模式：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e .
```

如果要启动 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api]"
```

如果要运行完整单测：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[test]"
```

查看当前骨架描述：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
```

模拟提交一个解析任务：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli submit --config parsecore.toml --doc-id demo-doc --file-path samples/demo.docx --media-type application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

启动本地 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

启动本地 queue worker：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli worker --config parsecore.queue.toml
```

用容器启动 API + worker：

```powershell
docker compose up -d --build
```

启用桥接专用鉴权的容器示例：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.queue.bridge-auth.toml.example"
$env:PARSECORE_UPLOAD_BRIDGE_API_KEY = "bridge-secret"
docker compose up -d --build --force-recreate parsecore-api parsecore-worker
```

用 Postgres + pgvector profile 启动容器：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
```

用 Postgres + pgvector + 本地 fake embedding 启动容器：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.fake-embedding.toml.example"
docker compose --profile pgvector up -d --build
```

说明：

- `parsecore-api` / `parsecore-worker` 现在统一挂载 `PARSECORE_RUNTIME_CONFIG` 指向的配置文件；不设置时仍默认使用 `parsecore.queue.toml`
- `parsecore-postgres` 通过 `pgvector` profile 提供，适合本地联调、自检和持久化验证
- 若只想切 OCR provider，不改存储，可把 `PARSECORE_RUNTIME_CONFIG` 指到 `parsecore.remote-http.toml.example` 或你自己的配置文件
- 若要只给 `/parse/uploads` / `/v1/parse/uploads` 打开专用鉴权，可把 `PARSECORE_RUNTIME_CONFIG` 指到 `parsecore.queue.bridge-auth.toml.example`，再设置 `PARSECORE_UPLOAD_BRIDGE_API_KEY`
- 若只想把 `chunk_embeddings` 与 hybrid search 路径在本地跑通，不依赖外部 key，可使用 `parsecore.pgvector.fake-embedding.toml.example`
- 示例配置默认启用 `max_upload_bytes = 52428800`，同步上传超过 50 MiB 时返回 `413 file_too_large`
- 示例配置默认启用 `allow_external_file_paths = false`，`/v1/parse/jobs` 只接受已存在且位于 `storage.object_store` 下的服务端文件路径；跨服务传文件推荐走 `/parse/uploads` 或 `/v1/parse/uploads`
- 示例配置默认启用 `staged_upload_retention_seconds = 86400`，桥接上传目录 `_api_uploads` 会在新上传到达时顺手清理 24 小时前的旧暂存文件；如果需要对 `/parse/uploads` 单独加保护，可配置 `staged_upload_api_key_env`

运行测试：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

运行默认自检门禁：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py
```

运行 slow/full 专项自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --profile slow
```

运行 perf 长尾性能跟踪：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --profile perf
```

说明：

- 默认 `fast` profile 会执行单测、runtime smoke 和 `var/regression/suite.fast.json`
- `slow/full` profile 会执行 `var/regression/suite.full.json`，覆盖主线样本加中等时长 slow baseline
- `perf` profile 会执行 `var/regression/suite.perf.json`，专门跟踪 `sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 两个重样本
- 若样本目录不在原始机器路径下，可设置 `PARSECORE_REGRESSION_FIXTURE_ROOT` 指向实际 PDF 目录，baseline 会优先按 `fixture_relative_path` 解析
- 默认输出分别写入 `var/self-check/latest.json`、`var/self-check/latest.full.json` 和 `var/self-check/latest.perf.json`

运行 OCR 长尾专项 benchmark：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/ocr_benchmark.py --config parsecore.toml --pdf samples/heavy-ocr.pdf --out var/self-check/ocr-benchmark.json
```

运行 Excel 真实样本质量报告：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/excel_sample_quality.py --config parsecore.toml --sample-dir D:/app/uploads --out-json var/self-check/excel-sample-quality.json --out-md var/self-check/excel-sample-quality.md
```

说明：该报告会扫描 `.xls/.xlsx/.xlsm` 样本，输出每个文件的 `tables / titled_tables / merged_cell_tables / empty_tables / truncated_tables / issues`，用于在真实表格文档上固定解析质量口径。

运行轻量解析性能基线：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/parse_perf_baseline.py --config parsecore.toml --sample-dir D:/app/uploads --extensions .xls,.xlsx,.xlsm --out-json var/self-check/parse-perf-baseline.json --out-md var/self-check/parse-perf-baseline.md
```

说明：该基线输出每个样本的 `elapsed_s / peak_kb / mb_per_s / blocks / chunks / tables`，默认只扫表格扩展名；需要覆盖 PDF/DOCX 时可显式传 `--extensions .pdf,.docx,.xls,.xlsx,.xlsm`。

只重算 chunk / embedding（跳过重新解析源文件）：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli submit --config parsecore.toml --doc-id demo-doc --file-path samples/demo.docx --media-type application/vnd.openxmlformats-officedocument.wordprocessingml.document --mode rerun_chunks_only
```

显式 API 路由：

- `GET /health`：parser-service 兼容健康检查，返回 `status / version / services / service_details`，其中 `services` 当前包含 `pdfplumber / python_docx / openpyxl / xlrd / paddleocr`，`service_details` 会给出注册状态、import 结果、版本和失败原因
- `POST /parse`：parser-service 兼容上传入口，使用 multipart `file` 字段上传文档，返回 `file_name / mime_type / total_pages / pages / metadata`
- `POST /parse/uploads`：上传桥接入口，使用 multipart `file` 字段暂存文件并返回 `parsecore_server_file_path`；表单里传 `create_job=true` 时会在同一请求里继续创建解析任务并一并返回 `job_id / state`
- `POST /parse/batch`：parser-service 兼容根路径，可直接对接现有企业产品客户端
- `POST /v1/parse`：与 `/parse` 等价的版本化上传入口，支持 `enable_ocr`、`tenant_id`、`quota_key`、`quota_units`
- `POST /v1/parse/uploads`：与 `/parse/uploads` 等价的版本化上传桥接入口，适合浏览器或连接器先换取 `parsecore_server_file_path`，再续接 `/v1/parse/jobs`；若同请求传 `create_job=true`，响应会同时携带 `job_id` 与可轮询的任务状态
- 桥接上传要求 `storage.object_store` 为 `local://...`；非本地对象目录会返回 `500 staged_upload_requires_local_object_store`，避免暂存文件落到系统临时目录后绕开路径边界
- `POST /v1/parse/batch`：与 `/parse/batch` 等价的版本化同步入口，接收 `file_base64`、`file_name`，同步返回 `success / total_pages / pages[] / parser_used / quality / raw_quality / output_quality / ocr_decision_trace / error`
- `POST /v1/parse/documents/{doc_id}/reparse`：重新执行完整解析
- `POST /v1/parse/documents/{doc_id}/rechunk`：复用已存 blocks，重算 chunk / embedding / index
- `POST /v1/parse/documents/{doc_id}/re-embed`：复用已存 blocks + chunks，仅重算 embedding / index
- `GET /v1/parse/documents/{doc_id}/search?q=...&role=warning&role=title`：基于已存 chunks 的轻量检索，支持 `semantic_role` 过滤并对 title/warning/note 等角色做内建权重
- `GET /v1/parse/jobs?tenant_id=...&quota_key=...`：按租户与配额键过滤任务列表
- `GET /v1/parse/quotas/usage?tenant_id=...&since_hours=...`：查看租户/配额维度的作业计数与 quota_units 聚合（支持时间窗口）
- `GET /v1/parse/metrics?tenant_id=...&sample_size=200`：查看租户维度轻量运行指标（失败率、活跃任务数、耗时 p50/p90/p99）
- `GET /v1/parse/events?event_type=ocr_failed&tenant_id=...`：查看最近观测事件；除 quota / inflight / embedding 外，现已包含 OCR 摘要事件 `ocr_attempted / ocr_fallback / ocr_rejected / ocr_failed`
- `GET /v1/parse/prometheus`：Prometheus 文本指标出口；除 quota / inflight / embedding 外，现已包含 `parse_ocr_attempt_total / parse_ocr_fallback_total / parse_ocr_rejected_total / parse_ocr_failed_total`
- `GET /v1/parse/dashboard?tenant_id=...&sample_size=200&recent_limit=5`：单请求聚合租户 usage + metrics + recent_jobs
- `since_hours`：可选时间窗口（小时），用于 `quotas/usage`、`metrics`、`dashboard` 仅统计最近 N 小时任务
- `POST /v1/parse/jobs`：创建异步解析 job；默认要求 `file_path` 指向 `storage.object_store` 内已存在的普通文件，越界返回 `403 file_path_not_allowed`
- `POST /v1/parse/jobs` 与文档重跑接口在 inline 模式下支持 inflight 背压：超过阈值返回 `429 too_many_inflight_jobs`
- `staged_upload_api_key_env`：仅保护 `/parse/uploads` 与 `/v1/parse/uploads`；配置后调用方需提供 `x-api-key` 或 `Authorization: Bearer ...`，不会影响 `/v1/runtime` 等其他接口
- `staged_upload_retention_seconds`：桥接暂存文件的保留秒数；服务会在新的桥接上传请求到达时清理 `_api_uploads` 下超过该时长的旧文件
- `pages[]`：同步 batch 响应中的页级结构包含 `page_number / page_type / text / tables_markdown / tables / artifacts / confidence`，可直接映射现有 parser-service 消费方；`page_type` 除 `body` 外，还会按结构语义输出 `toc / front_matter / appendix / signature`
- `pages[] OCR 字段`：当页面触发 OCR 决策时会附带 `ocr_attempted / ocr_fallback / ocr_rejected / ocr_attempt_reasons / ocr_acceptance_reasons / ocr_rejection_reasons / ocr_error_reasons / native_text_token_count / final_text_token_count`
- `ocr_decision_trace`：batch 顶层 OCR 决策汇总，包含 `ocr_attempted_pages / ocr_fallback_pages / ocr_rejected_pages / ocr_failed_pages / native_text_token_count / final_text_token_count` 以及可选原因列表字段
- `metadata`：上传解析响应中包含 `parser`，PDF 额外回传 `ocr_enabled`，用于和企业产品现有 `ParseResult` 结构对齐
- `excel-native`：`.xls/.xlsx/.xlsm` 会按 worksheet 内的空行与标题行分隔识别多个表格区域，输出 `TABLE` block，并在 metadata 中携带 `sheet_name / cell_range / source_cell_range / sheet_table_index / table_title / header_row / header_values / merged_cells / has_formula / hidden_sheet`；大型表格的完整 `cells` 元数据会按 `max_metadata_cells` 限制降级为 `cells_preview`
- `enable_ocr`：`/parse` 与 `/v1/parse` 以及 batch 入口上的 request 级开关；显式传 `true` 时会为该请求打开 PDF OCR 回退，显式传 `false` 时会覆盖配置默认值并关闭 OCR 回退
- `services`：健康检查中的能力矩阵会结合当前注册 parser 与实际 runtime 可用性返回；`openpyxl` 代表 `.xlsx/.xlsm` parser 可用性，`xlrd` 代表 `.xls` parser 可用性，兼容字段名 `paddleocr` 在 ParseCore 中代表 RapidOCR 驱动的 OCR 能力可用性
- `x-trace-id`：所有 HTTP 响应都会回传该请求头；若调用方未传入，ParseCore 会自动生成，便于宿主系统串联日志与事件
- 错误包：除 batch 兼容字段外，其余错误响应统一包含 `error / code / message / trace_id`，需要附加上下文时再补 `detail`

API 依赖说明：

- `api` 可选依赖现已包含 `python-multipart`，用于支持 `/parse` 与 `/v1/parse` 的 multipart 文件上传
- `parsers` 可选依赖现已包含 `openpyxl` 与 `xlrd`，用于支持 `excel-native` 解析 `.xls/.xlsx/.xlsm`
- `parsers` 可选依赖现已包含 `rapidocr_onnxruntime`，用于支撑 `image-ocr` parser 与 PDF 坏页 OCR 回退
- `test` 可选依赖包含 `pytest / httpx / numpy / Pillow / starlette`，用于新环境快速补齐单测依赖

背压与并发说明：

- `runtime.max_workers`：后台并行执行 worker 数（inline 模式）
- `runtime.max_inflight_jobs`：允许的 in-flight 任务上限（0 表示自动按 `max_workers * 4`）
- embedding 阶段采用分批调用；单批失败会自动重试并只对失败批次降级，不影响其它批次继续写入 embedding

配额硬限说明：

- `runtime.quota_enforce = true` 时，提交任务会按租户与 `quota_key` 做 `quota_units` 硬限校验
- 支持 `runtime.quota_window_hours` 时间窗（默认 24h）与 `runtime.quota_default_limit_units` 默认阈值
- 支持 `runtime.quota_limits` 覆盖规则，优先级：`tenant:quota_key` > `tenant:*` > `*:quota_key` > `*:*` > 默认阈值
- 超限返回 `429 quota_exceeded`，响应包含 `used_units/requested_units/limit_units/window_hours`

租户隔离说明：

- 文档读取/搜索/重跑接口支持 `tenant_id` 查询参数，并按租户过滤文档所属的最新作业。
- 若未传 `tenant_id`，默认按 `default` 租户处理；非 `default` 租户文档必须显式传参，否则返回 `document_not_found`。
- 底层 `blocks`/`chunks` 存储已按 `tenant_id + doc_id` 物理分区，避免同 `doc_id` 跨租户覆盖与读取串扰。

搜索响应包含：

- `retrieval_mode = "hybrid"`：query embedding 可用且至少有一条 chunk 向量参与排序
- `retrieval_mode = "keyword-fallback"`：query embedding 不可用，或无可参与的 chunk 向量，自动回退关键词排序

检索策略：

- 默认采用混合检索：向量优先（query embedding + cosine），关键词得分兜底
- 当查询 embedding 不可用（未配置 key/服务异常/维度不匹配）时自动回退到纯关键词，不中断请求
- 语义角色权重在融合后生效：title/warning 等提高排序优先级，toc_entry/lep_entry 适度降权

搜索说明：

- 当前是 runtime 内置的轻量检索面，优先服务嵌入式接入和本地验证
- 当宿主后续接入 pgvector / 外部检索层时，可以沿用相同的 `semantic_role` 过滤语义

启用 embedding provider：

```toml
[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://api.openai.com/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
batch_size = 16
```

本地 fake embedding provider：

```toml
[providers.embedding]
enabled = true
provider = "fake"
```

说明：

- `provider = "fake"` 会生成确定性的 1536 维向量，与默认 pgvector 索引维度一致，适合本地 `re-embed`、hybrid search 和 API/存储链路验证
- `provider = "fake"` / `"test"` / `"stub"` 都会走同一个本地 provider，不需要 `PARSECORE_EMBEDDING_API_KEY`
- 生产环境仍应切回 `openai-compatible` 或宿主侧真实 embedding provider

控制 OCR provider：

本地 RapidOCR：

```toml
[providers.ocr]
enabled = true
provider = "rapidocr"
# options.det_use_dilation = true
```

远程 OCR 网关：

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

说明：

- `providers.ocr.enabled = false` 时，`image-ocr` parser 仍可保留在配置中，但 `/health.services.paddleocr` 会返回 `false`，PDF 坏页 OCR 回退也会被显式关闭
- 当前内置 provider 为 `rapidocr` 和 `remote-http`；兼容健康检查字段名仍保留 `paddleocr`，是为了对齐企业产品既有探活契约
- `remote-http` 会把上传图片和 PDF 坏页回退图像统一序列化成 base64 JSON，请求 `POST {base_url}{endpoint_path}`，未显式配置时 `endpoint_path` 默认是 `/ocr`
- `options.endpoint_path` 与 `options.headers` 由 ParseCore 作为传输层配置消费，其余 `options.*` 会原样放进请求体里的 `options` 字段，便于透传宿主 OCR 网关自己的开关
- `remote-http` 预期响应体里包含 `result` 或 `results` 列表，列表项可为 `{ bbox, text, confidence }` 结构；可选 `elapsed` 字段会被透传为 OCR 调用耗时
- 当 PDF 坏页触发 OCR 但 provider 失败时，相关 block metadata 现在会显式带出 `ocr_attempted = true`、`ocr_attempt_reason` 与 `ocr_error_reason`，不再和“根本没触发 OCR”混在一起
- `tools/regression_baseline.py` 的 `layout_signals` 现已额外输出 `ocr_attempted_pages` / `ocr_failed_pages`，可直接观察远程 OCR 网关是否在真实样本上发生失败或退化
- `event_aggregator` 现会按文档汇总 OCR 摘要事件，并把页数记入 Prometheus 计数；因此 `/v1/parse/events` 更适合看具体 `attempt_reasons / error_reasons`，而 `/v1/parse/prometheus` 更适合看租户维度的 OCR 失败页总量
- 详细 HTTP 契约见 [docs/ocr-gateway-contract.md](docs/ocr-gateway-contract.md)，宿主侧接入步骤见 [docs/ocr-integration-checklist.md](docs/ocr-integration-checklist.md)

真实 embedding 端到端 smoke test：

```powershell
$env:PARSECORE_EMBEDDING_API_KEY = "..."
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/_embedding_smoke.py
```

说明：

- 脚本会临时强制启用 embedding provider，构造一个 DOCX 样本，跑完整 submit 流程
- 输出包含 `embedded_chunk_ratio`、`mean_embedding_dim_norm`、`embedding_dim` 和一组 search 命中样本
- 如果未配置 `PARSECORE_EMBEDDING_API_KEY`，默认输出 `skipped` 并退出；传 `--require-live` 会改为非零退出

本地 fake embedding 验证路径：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.fake-embedding.toml.example"
docker compose build parsecore-api parsecore-worker
docker compose --profile pgvector up -d parsecore-postgres parsecore-api parsecore-worker
```

说明：

- 这条路径不依赖外部 embedding key
- 适合验证 `chunk_embeddings` 是否落库，以及搜索是否从 `keyword-fallback` 升级为 `hybrid`

## 下一步优先级

1. 把 `tools/self_check.py` 固化为默认自检入口，并继续收敛 OCR 长尾样本性能。
2. 在 queue-worker + pgvector 模式下固化入口鉴权、灰度配置与回滚口径。
3. 为 `fast/full/perf` 三档门禁补稳定样本环境与持续趋势跟踪。
4. 继续优化 OCR provider 的失败诊断、embedding 覆盖率与检索命中质量。
