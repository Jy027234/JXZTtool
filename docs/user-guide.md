# ParseCore 使用说明

## 适用读者

本文面向宿主产品开发、测试、实施和运维人员，说明如何把 ParseCore 作为解析中台使用。底层配置细节见 [configuration.md](configuration.md)，灰度部署见 [gray-deployment.md](gray-deployment.md)。

## 使用形态

ParseCore 当前支持四种使用形态：

| 形态 | 适合场景 | 入口 |
| --- | --- | --- |
| 嵌入式 SDK | Python 产品内直接调用解析能力 | `parsecore.bootstrap.build_runtime` |
| HTTP API | 多产品共享解析服务 | `parsecore.cli serve` / ASGI `create_app` |
| Queue Worker | 大文件、长任务、生产灰度 | `parsecore.cli worker` |
| CLI 工具 | 本地自检、压测、运维任务 | `python -m parsecore.cli ...` |

生产建议优先使用 `HTTP API + queue-worker + Postgres/pgvector`。本地开发或单机验证可以使用默认 SQLite/inline 配置。

## 快速启动

安装开发依赖：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pip install -e ".[api,parsers,test]"
```

启动本地 API：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli serve --config parsecore.toml --host 127.0.0.1 --port 8090
```

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
Invoke-RestMethod http://127.0.0.1:8090/v1/parse/profiles
```

## 推荐解析流程

### 中小文档

中小 Word、Excel、PDF 可用同步入口：

```powershell
$body = @{
  doc_id = "demo-doc"
  file_path = "D:/app/uploads/demo.xlsx"
  media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  options = @{ profile = "auto" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body http://127.0.0.1:8090/v1/parse
```

读取结构化结果：

```powershell
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc?projection=structured"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/demo-doc/quality"
```

### 大文件和超长 PDF

大文件推荐异步链路：

1. 上传到 `/v1/parse/uploads`。
2. 使用返回的 `parsecore_server_file_path` 创建 `/v1/parse/jobs`。
3. 轮询 `/v1/parse/jobs/{job_id}`。
4. 完成后读取 structured projection、records、导出包或 part 视图。

示例：

```powershell
$job = @{
  doc_id = "big-pdf"
  file_path = "D:/app/uploads/big.pdf"
  media_type = "application/pdf"
  options = @{ profile = "large-pdf-catalog" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $job http://127.0.0.1:8090/v1/parse/jobs
```

规划 part：

```powershell
Invoke-RestMethod -Method Post -ContentType "application/json" -Body '{"target_pages_per_part":200}' "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts/plan"
Invoke-RestMethod "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts"
```

异常 part 复跑：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/parse/documents/big-pdf/parts/rerun" -ContentType "application/json" -Body '{"failed_only":true}'
```

## Profile 选择

默认让宿主传 `profile=auto`。只有已知文件类型才显式覆盖：

| Profile | 使用场景 |
| --- | --- |
| `default` | 普通 Word/PDF/文本 |
| `table-heavy` | 表格密集文档 |
| `excel-ledger` | Excel 台账、清单、明细表 |
| `large-pdf` | 长页数 PDF 或同步入口超限 PDF |
| `large-pdf-catalog` | 目录、产品清单、批准目录类超大 PDF |
| `large-pdf-ledger` | PDF 台账、明细表、大批量记录 |
| `ocr-heavy` | OCR 密集 PDF |
| `scan-pdf` | 扫描件 PDF 或图片 |

`large-pdf-catalog` 和 `large-pdf-ledger` 默认走 fast text path，优先产出可追溯 records。若必须高保真表格块或 OCR，可显式传：

```json
{
  "profile": "large-pdf-ledger",
  "post_process": {
    "dual_channel": true,
    "layout_reading_order": true
  },
  "enable_ocr": true
}
```

## Records 查询

解析完成后，中台会持久化 `pages / lines / records`。主文档响应默认不携带这些大 views，records 应通过专用入口分页读取：

```text
GET /v1/parse/documents/{doc_id}/records?limit=100&offset=0
GET /v1/parse/documents/{doc_id}/records?query=TC001A
GET /v1/parse/documents/{doc_id}/records?page_start=2000&page_end=2300
GET /v1/parse/documents/{doc_id}/records?quality_signal=column_shift_suspected
GET /v1/parse/documents/{doc_id}/records?field.certificate_or_project_no=PMA0013-01-XN
```

records 可能来自表格行，也可能来自目录/台账文本行聚合。消费方应优先使用：

- `record_id`
- `source`
- `fields`
- `raw_text`
- `normalized_text`
- `page_start / page_end`
- `quality_signal_codes`

## 导出数据

同步导出适合中小结果集和排查：

```powershell
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=jsonl" -OutFile records.jsonl
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=sqlite" -OutFile records.sqlite
Invoke-WebRequest "http://127.0.0.1:8090/v1/parse/documents/demo-doc/exports?dataset=records&format=xlsx" -OutFile records.xlsx
```

大结果集推荐异步导出包：

```powershell
$body = @{
  include = @("pages", "lines", "records", "quality_signals", "parse_units")
  formats = @{ records = "sqlite"; lines = "csv"; pages = "jsonl"; quality_signals = "jsonl"; parse_units = "tsv" }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -ContentType "application/json" -Body $body "http://127.0.0.1:8090/v1/parse/documents/demo-doc/export-jobs"
```

支持的 dataset：

```text
pages | lines | tables | quality_signals | parse_units | records
```

支持的格式：

```text
jsonl | csv | tsv | sqlite | xlsx
```

## 质量信号与抽检

上线灰度时至少观察：

- `/v1/parse/documents/{doc_id}/quality`
- `/v1/parse/documents/{doc_id}/parts?state=warning|failed`
- `/v1/parse/metrics`
- `/v1/parse/events?limit=100`
- `/v1/parse/prometheus`

业务抽检建议优先查看：

- `quality_summary.total`
- `quality_summary.by_code`
- `column_shift_suspected`
- `record_field_missing`
- `table_ragged_rows`
- `ocr_failed_page`
- `truncated_table`

## 生产配置建议

生产或灰度建议：

- 使用 `parsecore.pgvector.toml.example` 或由它派生的生产配置。
- 使用 `queue-worker`，不要让 API 进程承接所有大文件解析。
- 使用 Postgres 持久化 job、blocks、chunks、document views 和 metrics。
- 设置 `runtime.api_key_env`，除 `/health` 外保护所有 HTTP 入口。
- 保留 `runtime.max_upload_bytes`，同步入口超限后切到 `/v1/parse/uploads + /v1/parse/jobs`。
- 为大 PDF 设置 `runtime.max_active_parts_per_doc = 2-4` 起步。
- 保存 `var/self-check`、导出包目录、数据库备份和灰度基线快照。

## 交付验收

交付给宿主产品前，建议完成：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pytest -q
git diff --check
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
Invoke-RestMethod http://127.0.0.1:8090/health
```

对于真实业务样本，至少抽检：

1. 一个 Word 或普通 PDF。
2. 一个 Excel 表格文档。
3. 一个表格密集 PDF。
4. 一个大 PDF part 解析样本。
5. 一次 records SQLite 或 JSONL 导出。

验收通过后，本版可按"受控生产灰度"交付。

---

## 契约接入顺序

宿主产品接入 ParseCore 输出时，建议按以下顺序逐层接入投影端点：

| 阶段 | 端点 | 用途 | 说明 |
| --- | --- | --- | --- |
| 1 | `GET /v1/parse/documents/{doc_id}/reader` | 页面渲染 | blocks 数组可直接驱动前端阅读器，含 `reader_summary`、`quality_gate` |
| 2 | `GET /v1/parse/documents/{doc_id}/quality` | 质量诊断 | `quality_signals`、`attention_summary` 驱动红点提示和复核流程 |
| 3 | `GET /v1/parse/documents/{doc_id}/providers` | Provider 追踪 | 观察哪些 local provider 被选中、对比报告、rerun 建议 |
| 4 | `GET /v1/parse/documents/{doc_id}/parts` | 分片状态 | 大 PDF 每个 part 的解析状态、质量信号、rerun 合同 |
| 5 | `GET /v1/parse/documents/{doc_id}/coverage` | RAG 覆盖审计 | 页级/unit 级 coverage gap，驱动 RAG 入库策略 |
| 6 | `GET /v1/parse/documents/{doc_id}?projection=ir` | 完整 IR | 全量结构化中间表示，适合深度集成 |

**接入检查清单：**

- [ ] 每个端点返回的 `schema_version` 已在消费方做兼容判断
- [ ] `quality_gate.status` 为 `warn` 或 `fail` 时宿主有对应 UX 处理
- [ ] `rag_coverage_quality.recommended_action` 为 `local_provider_rerun` 时可自动触发复跑
- [ ] `comparison_report` 中的 `summary` 字段可渲染为 provider 比较摘要卡片

## 前端接入字段清单

以下为前端渲染最常用的字段及来源投影：

### Reader 投影（阅读器渲染）

| 字段路径 | 类型 | 说明 |
| --- | --- | --- |
| `blocks[].block_id` | string | 块唯一标识 |
| `blocks[].block_type` | string | 块类型：`title` / `paragraph` / `table` / `image` |
| `blocks[].content` | string | 文本内容或表格 Markdown |
| `blocks[].semantic_role` | string | 语义角色：`body_section` / `table` / `image` / `header_footer` 等 |
| `blocks[].metadata.bbox` | array | 页面包裹框 [x1,y1,x2,y2] |
| `blocks[].metadata.page` | int | 页码 |
| `blocks[].metadata.reading_order` | int | 阅读顺序序号 |
| `blocks[].metadata.source_kind` | string | 来源：`native_text` / `ocr_text` / `structured_table` / `pdf_image` |
| `pages[].page_number` | int | 页码 |
| `pages[].quality_signals` | array | 页级质量信号列表 |
| `reader_summary` | object | 文档摘要统计 |

### Quality 投影（质量红点与复核）

| 字段路径 | 类型 | 说明 |
| --- | --- | --- |
| `quality_signals[].code` | string | 信号代码，如 `column_shift_suspected` |
| `quality_signals[].severity` | string | 严重级别：`info` / `warning` / `error` |
| `quality_signals[].page_numbers` | array | 触发该信号的页码 |
| `attention_summary` | object | 需关注的 part/页汇总 |
| `quality_gate.status` | string | 门禁状态：`pass` / `warn` / `fail` |

### Coverage 投影（RAG 入库审计）

| 字段路径 | 类型 | 说明 |
| --- | --- | --- |
| `coverage.pages[].page_number` | int | 页码 |
| `coverage.pages[].units[].unit_id` | string | KnowledgeUnit ID |
| `coverage.pages[].units[].should_index_for_rag` | bool | 是否建议 RAG 入库 |
| `coverage.pages[].units[].skip_reason` | string/null | 跳过原因 |
| `rag_coverage_quality.status` | string | 覆盖状态：`full` / `partial` / `empty` |
| `rag_coverage_quality.recommended_action` | string | 建议动作 |

## API 错误码参考

所有 API 错误响应统一格式：

```json
{
  "error": "<code>",
  "code": "<code>",
  "message": "<human-readable message>",
  "trace_id": "<request trace id>"
}
```

| 错误码 | HTTP 状态 | 说明 | 常见触发场景 |
| --- | --- | --- | --- |
| `document_not_found` | 404 | 文档不存在 | doc_id 拼写错误或文档已清理 |
| `job_not_found` | 404 | 解析任务不存在 | job_id 不存在 |
| `part_not_found` | 404 | Part 不存在 | part_id 不存在或尚未规划 |
| `schema_not_found` | 404 | Schema 不存在 | schema name 拼写错误 |
| `export_not_found` | 404 | 导出任务不存在 | export_job_id 不存在或文件已过期 |
| `missing_doc_id` | 400 | 缺少 doc_id | 请求体未传 doc_id |
| `invalid_projection` | 400 | 无效投影类型 | projection 参数不在允许枚举中 |
| `invalid_limit` | 400 | 无效的 limit 参数 | limit 非正整数或超范围 |
| `invalid_since_hours` | 400 | 无效的 since_hours | since_hours 非正数 |
| `invalid_sample_size` | 400 | 无效的 sample_size | sample_size 非正整数 |
| `invalid_recent_limit` | 400 | 无效的 recent_limit | recent_limit 非正整数 |
| `invalid_trend_window_hours` | 400 | 无效的趋势窗口 | trend_window_hours 非正数 |
| `invalid_records_query` | 400 | 无效的 records 查询 | query 参数格式错误 |
| `invalid_index_layer` | 400 | 无效的索引层 | index_layer 不在允许枚举中 |
| `invalid_part_rerun` | 400 | Part 复跑参数错误 | 参数缺失或冲突 |
| `invalid_export_job` | 400 | 导出任务参数错误 | dataset/format 不支持 |
| `invalid_multipart` | 400 | Multipart 解析失败 | 上传文件格式错误 |
| `invalid_base64_encoding` | 400 | Base64 解码失败 | 文件 base64 编码损坏 |
| `invalid_quota_units` | 400 | 配额单位无效 | quota 计算参数错误 |
| `file_required` | 400 | 缺少文件 | 上传请求未附带文件 |
| `file_path_not_allowed` | 403 | 文件路径不允许 | 路径在允许目录外 |
| `empty_file` | 400 | 空文件 | 上传文件大小为零 |
| `document_too_large_for_sync` | 413 | 文档超同步限制 | 需改用异步 `/v1/parse/jobs` |
| `quota_exceeded` | 429 | 配额超限 | 租户解析量超出窗口限额 |
| `too_many_inflight_jobs` | 429 | 并发任务过多 | 当前 doc 的 inflight jobs 达上限 |
| `batch_parse_failed` | 400/500 | 批量解析失败 | 批量请求中部分或全部失败 |
| `query_required` | 400 | 缺少查询参数 | 搜索/语义端点未传 q 参数 |

## Reader 最小渲染协议

宿主产品阅读页应基于 `GET /v1/parse/documents/{doc_id}/reader` 投影实现渲染，以下为最小渲染协议：

### 渲染流程

1. **按页分组**：将 `blocks` 数组按 `page_number` 分组。
2. **读序排序**：每个 page 内的 blocks 按 `reading_order` 升序排列。
3. **过滤隐藏块**：`reader_policy = "hidden"` 的 block 不进正文（页眉页脚、页码、解析工件）。
4. **渲染表格块**：`reader_policy = "table"` 或 `type = "table"` 的 block，使用 `table.cells`、`table.header`、`table.caption` 渲染结构化表格。
5. **渲染图示块**：`reader_policy = "source_snapshot"` 或 `type = "figure"` 的 block，使用 `figure.caption`、`figure.alt_text`、`figure.figure_id` 渲染图示区域。
6. **渲染文本块**：`reader_policy = "inline"` 的 block，使用 `text` 字段渲染段落。

### 质量提示与诊断层

- **质量信号**：当 block 的 `quality_signal_codes` 非空时，在该 block 局部展示质量提示徽标（如黄点、红点），具体严重级别从 `/quality` 端点获取。
- **诊断信息隔离**：`semantic_role` 为 `parse_artifact`、`header_footer` 等诊断类 block 应进入提示层或隐藏，不混入正文渲染。
- **RAG 状态指示**：使用 `should_index_for_rag`、`embedding_state`、`skip_reason` 字段可渲染 RAG 入库状态指示（如绿色“已入库”、灰色“已跳过”、红色“失败”）。

### 字段映射参考

| 渲染需求 | 字段 | 说明 |
| --- | --- | --- |
| 页码 | `blocks[].page_number` | 页面编号 |
| 阅读顺序 | `blocks[].reading_order` | 块在页内的阅读顺序 |
| 块类型 | `blocks[].type` | `title` / `text` / `table` / `figure` |
| 显示样式 | `blocks[].display_kind` | `text` / `table` / `figure` / `artifact` |
| 渲染策略 | `blocks[].reader_policy` | `inline` / `hidden` / `table` / `source_snapshot` |
| 正文文本 | `blocks[].text` | 渲染用文本 |
| RAG 文本 | `blocks[].rag_text` | RAG 入库文本（可与正文不同） |
| 结构化表格 | `blocks[].table` | 表格 cells/header/caption 结构 |
| 图示信息 | `blocks[].figure` | 图示 caption/alt_text/figure_id |
| 知识单元 | `blocks[].knowledge_units` | 块关联的 KnowledgeUnit 数组 |
| 质量信号 | `blocks[].knowledge_units[].quality_signal_codes` | 单元级质量信号 |
| 元数据 | `blocks[].provenance` | Provider 来源信息 |

## 视觉抽检样本清单

阅读页替换 Markdown 补丁前，应使用以下固定样本做视觉抽检，确保 reader 投影渲染符合最小渲染协议。

### 样本矩阵

| 样本类型 | 覆盖目的 | 预期渲染验证点 | 来源 |
| --- | --- | --- | --- |
| 多栏 PDF | 读序低置信检测 | `reading_order_confidence < 0.5` 时出现质量提示徽标 | 固定 fixture |
| 表格 PDF | 表格 block 结构化渲染 | `reader_policy = "table"` 的 block 渲染 `table.cells`/`header`/`caption` | 固定 fixture |
| 标题层级 PDF | heading block 类型验证 | `display_kind` 与 `semantic_role` 对应正确的层级样式 | 固定 fixture |
| 图示 PDF | figure block caption/alt_text | `reader_policy = "source_snapshot"` 的 block 渲染 `figure.caption`/`alt_text` | 固定 fixture |
| 页眉页脚 PDF | hidden block 过滤 | `reader_policy = "hidden"` 的 block 不出现在正文 | 固定 fixture |
| 目录重复项 PDF | skip_reason: toc_duplicate | 目录项 `should_index_for_rag = false`，RAG 状态指示为"已跳过" | 固定 fixture |

### 抽检流程

1. **准备样本**：将上述六类样本 PDF 上传到 ParseCore 测试环境，记录每份文档的 `doc_id`。
2. **查看 reader 投影**：访问 `GET /v1/parse/documents/{doc_id}/reader`，按最小渲染协议验证每类 block 的渲染行为。
3. **核对质量信号**：检查 `quality_signal_codes` 是否按预期出现，质量提示徽标级别正确。
4. **核对 RAG 状态**：检查 `should_index_for_rag`、`embedding_state`、`skip_reason` 字段，确认 RAG 入库状态指示正确。
5. **回归对比**：与历史快照（`var/self-check/` 或 `.tmp/validation/`）的 reader blocks 数量、`reading_order` 序列对比，偏差应在 ±5% 以内。

### 抽检通过标准

- 六类样本全部通过渲染验证。
- 无 block 被错误隐藏或错误展示。
- 质量信号与 RAG 状态字段与预期一致。
- 回归对比偏差在允许范围内。

## RAG 侧接入说明

ParseCore 负责将文档解析为结构化 KnowledgeUnit，下游 RAG 系统（如 pgvector、Milvus 等）负责存储和检索。本节说明 RAG 侧如何正确消费 ParseCore 的输出。

### KnowledgeUnit 数据流

```
文档上传
    │
    ▼
[解析] ── Provider 产出 blocks
    │
    ▼
[KnowledgeUnit 构建] ── ir.py _knowledge_units() / _coverage_units()
    │                    每个 block 生成 1~N 个 unit
    │                    unit 含 unit_id / text / rag_text / should_index_for_rag / skip_reason
    ▼
[分块] ── stubs.py ParagraphChunkBuilder
    │      将 unit.text 或 unit.rag_text 切为 chunks
    │      chunk 含 chunk_id / text / embedding
    ▼
[Embedding] ── embeddings.py embed_text()
    │            对每个 chunk 生成向量
    │            状态写入 chunk.embedding_state
    ▼
[RAG 存储] ── 下游系统写入 pgvector/Milvus
              含 chunk_id / text / embedding / metadata
```

### 核心字段说明

| 字段 | 位置 | 说明 |
| --- | --- | --- |
| `unit_id` | `knowledge_units[]` | 单元唯一标识，用于追踪和去重 |
| `should_index_for_rag` | `knowledge_units[]` | `true` 表示该单元应进入 RAG 索引；`false` 表示应跳过 |
| `skip_reason` | `knowledge_units[]` | 跳过原因枚举：`empty_text`/`semantic_role:*`/`toc_duplicate`/`diagnostic_text`/`low_confidence_ocr`/`figure_caption_missing`/`index_policy_skip` |
| `text` | `knowledge_units[]` | 原始文本（用于分块） |
| `rag_text` | `knowledge_units[]` | RAG 专用文本（可能经过清理，如表格展开为自然语言） |
| `embedding_state` | `knowledge_units[]` | 枚举：`pending`/`embedded`/`failed`/`skipped` |
| `embedding_model` | `knowledge_units[]` | 使用的 embedding 模型标识（nullable） |
| `embedding_error_category` | `knowledge_units[]` | 失败类别：`model_unavailable`/`quota_exceeded`/`timeout`/`invalid_dimension`/`unknown`（nullable） |
| `chunk_ids` | `knowledge_units[]` | 关联的 chunk ID 列表 |

### Embedding 状态机

```
pending ── embed 成功 ──▶ embedded
   │
   ├── embed 失败 ──▶ failed (embedding_error_category 记录原因)
   │
   └── should_index_for_rag=false ──▶ skipped
```

下游 RAG 系统应仅检索 `embedding_state = "embedded"` 的 chunk。

### 查询接口

| 端点 | 用途 |
| --- | --- |
| `GET /v1/parse/documents/{doc_id}` | 获取完整文档 IR，含 `knowledge_units` 和 `chunks` |
| `GET /v1/parse/documents/{doc_id}/coverage` | 获取覆盖度报告，含每个 unit 的 RAG 状态和 skip_reason |
| `GET /v1/parse/documents/{doc_id}/reader` | 获取 reader 投影，含 `embedding_state` 用于 UI 展示 |
| `POST /v1/parse/documents/{doc_id}/re-embed` | 重新 embedding 指定文档（模型升级或失败重试时使用） |
| `GET /v1/parse/documents/{doc_id}/quality` | 获取质量信号，含 `quality_signal_codes` 和严重级别 |

### RAG 侧接入检查清单

1. **单元过滤**：只消费 `should_index_for_rag = true` 的 KnowledgeUnit。
2. **文本选择**：优先使用 `rag_text`（若存在），否则使用 `text`。`rag_text` 通常包含表格展开文本、图示描述等 RAG 友好内容。
3. **Embedding 状态同步**：消费 chunk 时检查 `embedding_state`，仅处理 `embedded` 状态的 chunk。
4. **去重**：使用 `unit_id` 做去重，避免重复入库。
5. **质量信号**：检查 `quality_signal_codes` 非空的 unit，决定是否降低检索权重或标记为低质量。
6. **重 embedding**：当 embedding 模型升级时，调用 `POST /re-embed` 端点重新生成向量，无需重新解析文档。
7. **覆盖度监控**：定期检查 `/coverage` 端点，确保 `coverage_state` 为 `covered` 的 unit 比例不低于阈值（建议 ≥ 80%）。

### 典型接入代码

```python
import requests

BASE = "http://localhost:8000"
doc_id = "your-document-id"

# 1. 获取 KnowledgeUnit 列表
resp = requests.get(f"{BASE}/v1/parse/documents/{doc_id}")
doc = resp.json()
units = doc.get("knowledge_units", [])

# 2. 过滤应入库的单元
indexable = [u for u in units if u.get("should_index_for_rag")]

# 3. 获取 chunk 文本和 embedding
for unit in indexable:
    rag_text = unit.get("rag_text") or unit.get("text", "")
    chunk_ids = unit.get("chunk_ids", [])
    # 将 rag_text 和 metadata 写入向量库
    # vector_db.upsert(unit_id=unit["unit_id"], text=rag_text, ...)

# 4. 检查覆盖度
coverage = requests.get(f"{BASE}/v1/parse/documents/{doc_id}/coverage").json()
for unit in coverage.get("units", []):
    if unit["coverage_state"] == "covered":
        print(f"Unit {unit['unit_id']}: OK")
    else:
        print(f"Unit {unit['unit_id']}: {unit['coverage_state']} - {unit.get('skip_reason')}")
```

## 动作合同（Action Suggestion）

ParseCore 在质量门禁、part rerun、coverage gap 等场景中输出 `action_suggestions` 数组，每个建议代表一个可执行动作。动作合同遵循 inspect → compare → execute → verify 四阶段模式。

### Action Suggestion 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `action_id` | string | 动作唯一标识，如 `rerun_part`、`rechunk_document` |
| `label` | string | 人类可读的动作名称 |
| `method` | string | HTTP 方法，如 `POST`、`GET` |
| `endpoint` | string | 调用的 API 端点路径 |
| `scope` | string | 作用域，如 `document`、`part`、`page` |
| `reason_codes` | string[] | 触发该动作的质量信号或原因编码 |
| `auto_execute` | boolean | 是否可自动执行（`false` 表示需人工确认） |
| `payload` | object | 预填充的请求体（可选） |
| `params` | object | URL/查询参数（可选） |
| `context` | object | 附加上下文，如 `page_number`、`part_id`、`unit_ids`（可选） |

### 四阶段模式

```
[inspect] ── 质量门禁/coverage gap 检测
    │         产出 action_suggestions[]
    ▼
[compare] ── 宿主产品评估动作建议
    │         检查 reason_codes / auto_execute / scope
    ▼
[execute] ── 调用 endpoint (method + payload + params)
    │         auto_execute=true 可自动调用
    │         auto_execute=false 需人工确认
    ▼
[verify] ── 通过 /quality、/coverage、/parts 验证结果
              确认 reason_codes 清空或降级
```

### 典型场景

| 场景 | action_id | scope | reason_codes 示例 |
| --- | --- | --- | --- |
| 表格覆盖率低 | `rerun_part` | `part` | `rag_table_without_unit` |
| 图示缺 caption | `rerun_part` | `part` | `rag_figure_caption_missing` |
| 读序低置信 | `rerun_part` | `part` | `reading_order_low_confidence` |
| 重新分块 | `rechunk_document` | `document` | `coverage_gap` |
| 重新 embedding | `reembed_document` | `document` | `chunks_not_embedded` |

## Provider Comparison 产品字段

`GET /v1/parse/documents/{doc_id}/providers` 返回的 `comparison_report` 字段包含 provider 对比信息。

### 核心字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | const | `"2026-06-provider-comparison"` |
| `primary_provider_id` | string (nullable) | 当前路由计划的 primary provider |
| `best_provider_id` | string (nullable) | 质量评分最高的 provider |
| `summary` | object | 对比汇总统计（completed/failed/skipped 计数等） |
| `rankings` | array | 各 provider 的排名详情（provider_id/score/coverage_ratio 等） |

### primary vs best 偏差判断

- `primary_provider_id == best_provider_id`：路由与质量一致，无需调整。
- `primary_provider_id != best_provider_id`：路由 primary 与质量 best 不一致。此时应检查：
  1. `rankings` 中两个 provider 的分数差距是否在偏差预算内（`max_samples_best_provider_differs_from_route_primary`）。
  2. best provider 是否已通过 gate（`gate_status == "passed"` 且 `route_ready == true`）。
  3. 是否存在 identity drift（`provider_version` / `adapter_version` 不一致）。

### 宿主产品消费建议

1. **展示**：在文档详情页展示 `primary_provider_id` 和 `best_provider_id`，偏差时标注风险等级。
2. **动作**：偏差超出预算时，`comparison_actions` 中可能包含切换 primary 的建议。
3. **监控**：持续跟踪 `primary_provider_id` 与 `best_provider_id` 的偏差率，作为 provider 准入门禁指标。

## Coverage 消费指南

Coverage 投影（`GET /v1/parse/documents/{doc_id}/coverage`）提供页级和单元级覆盖度数据。

### 页级字段（coverage.pages[]）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `page_number` | int | 页码 |
| `parsed_text_chars` | int | 该页解析出的文本字符数 |
| `table_count` | int | 该页表格数量 |
| `figure_count` | int | 该页图示数量 |
| `block_count` | int | 该页 block 总数 |
| `unit_ids` | string[] | 该页所有 KnowledgeUnit ID |
| `indexable_unit_ids` | string[] | 应入库的 unit ID（should_index_for_rag=true） |
| `skipped_unit_ids` | string[] | 跳过的 unit ID（should_index_for_rag=false） |
| `indexable_unit_count` | int | 应入库 unit 数量 |
| `chunked_unit_count` | int | 已分块的 unit 数量 |
| `unchunked_unit_ids` | string[] | 未分块的 unit ID（RAG 入库缺失） |
| `unembedded_unit_ids` | string[] | 未 embedding 的 unit ID |
| `table_ids_without_units` | string[] | 缺少 indexable unit 的表格 ID |
| `figure_ids_missing_caption` | string[] | 缺少 caption 的图示 ID |
| `chunk_ids` | string[] | 该页所有 chunk ID |
| `embedded` | boolean | 该页 chunk 是否全部 embedding 完成 |
| `missing_reason` | string (nullable) | 缺失原因（见下表） |
| `provider_ids` | string[] | 该页参与的 provider ID |
| `reading_order_confidence` | float (nullable) | 读序置信度 |
| `quality_signal_codes` | string[] | 该页的质量信号编码 |

### missing_reason 枚举

| 值 | 含义 | 下一步动作 |
| --- | --- | --- |
| `no_indexable_units` | 该页无应入库的 unit | 检查 index_policy 是否为 skip |
| `no_chunks_for_indexable_units` | 有应入库 unit 但未分块 | 检查 chunk builder 配置 |
| `chunks_not_embedded` | 已分块但未 embedding | 检查 embedding provider 状态 |
| `skipped` | 所有 unit 被 skip | 检查 skip_reason 字段 |
| `missing_chunks` | chunk 数量不匹配 | 重新 rechunk |

### 单元级字段（coverage.units[]）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `unit_id` | string | 单元唯一标识 |
| `coverage_state` | string | 覆盖状态：`covered`/`no_indexable_units`/`no_chunks_for_indexable_units`/`chunks_not_embedded`/`skipped`/`missing_chunks` |
| `should_index_for_rag` | boolean | 是否应入库 |
| `skip_reason` | string (nullable) | 跳过原因枚举 |
| `chunk_ids` | string[] | 关联的 chunk ID |
| `chunk_count` | int | chunk 数量 |
| `embedded_chunk_count` | int | 已 embedding 的 chunk 数量 |
| `embedding_state` | string | `pending`/`embedded`/`failed`/`skipped` |
| `quality_signal_codes` | string[] | 单元级质量信号 |

### 消费建议

1. **页级监控**：优先检查 `missing_reason` 非空的页面，这些是覆盖度缺口。
2. **单元级下钻**：从页级 `unchunked_unit_ids` / `unembedded_unit_ids` 下钻到单元级，查看具体 unit 的 `coverage_state` 和 `skip_reason`。
3. **表格/图示专项**：`table_ids_without_units` 和 `figure_ids_missing_caption` 指出哪些结构化内容缺少 RAG 文本，需要 part rerun 修复。
4. **读序监控**：`reading_order_confidence < 0.5` 的页面需要关注读序质量问题。
