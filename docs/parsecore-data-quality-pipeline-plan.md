# ParseCore Data Quality Pipeline 升级计划

日期：2026-05-09

## 背景

ParseCore 已经被嵌入到其他产品中，后续升级不能要求宿主产品一次性重构。下一阶段目标不是只解决单个超大 PDF，而是把中台从“解析接口”升级为“结构化数据生产线”：保留旧接口兼容，同时新增异步任务、结构化表格、质量信号、异常复跑和导出能力。

## 改造原则

1. 兼容优先：保留 `/parse`、`/parse/batch`、`/v1/parse`、`/v1/parse/batch` 的旧响应口径。
2. 新字段只追加：旧字段继续保留，新能力通过 `schema_version`、`projection` 和可选字段暴露。
3. 中等改动先落地：宿主产品先改 parser client、schema 和少量 JSON 存储字段，不强制建设完整复核平台。
4. 深度改动可承接：所有新增模型都保留 `parse_run_id`、`table_id`、`quality_signal`、`parse_unit` 等定位字段，后续可自然演进成人工复核、导出和多引擎对照。

## 目标形态

从：

```text
上传文件 -> 同步解析 -> 消费 pages/text/markdown
```

升级为：

```text
上传文件 -> 异步解析 job -> 获取兼容结果 + 结构化结果 + 质量报告
```

## 接口策略

保留旧接口：

```text
POST /parse
POST /parse/batch
POST /v1/parse
POST /v1/parse/batch
```

推荐新主链路：

```text
POST /v1/parse/uploads
POST /v1/parse/jobs
GET  /v1/parse/jobs/{job_id}
GET  /v1/parse/documents/{doc_id}?projection=compat
GET  /v1/parse/documents/{doc_id}?projection=structured
GET  /v1/parse/documents/{doc_id}?projection=full
GET  /v1/parse/documents/{doc_id}/quality
```

Projection 口径：

| projection | 用途 | 内容 |
| --- | --- | --- |
| `compat` | 旧 parser-service 兼容 | `pages / quality / raw_quality / output_quality / ocr_decision_trace` |
| `structured` | 中等改动推荐读取 | `pages / tables / quality_signals / parse_units / index_manifest` |
| `full` | 调试、复核、深度改造 | `structured` 加 `job / blocks / chunks` |

## ParseResultV2 契约

```json
{
  "schema_version": "2026-06",
  "projection": "structured",
  "doc_id": "doc_xxx",
  "parse_run_id": "job_xxx",
  "profile": "table-heavy",
  "state": "done",
  "compat_pages": [],
  "pages": [],
  "tables": [],
  "quality": {},
  "quality_signals": [],
  "ocr_decision_trace": {},
  "parse_units": [],
  "index_manifest": {}
}
```

统一表格模型：

```json
{
  "table_id": "doc_p1_t1",
  "source_doc_id": "doc_xxx",
  "part_doc_id": "doc_xxx_part_001",
  "page_number": 1,
  "table_index": 1,
  "source_parser": "pdf-text",
  "bbox": [0, 0, 100, 100],
  "rows": 20,
  "cols": 8,
  "header_rows": 1,
  "cells": [],
  "warnings": []
}
```

Cell 模型：

```json
{
  "row_index": 0,
  "col_index": 0,
  "text": "Part Number",
  "confidence": 1.0,
  "source_page_number": 1,
  "source_table_index": 1,
  "warnings": []
}
```

质量信号模型：

```json
{
  "code": "table_header_missing",
  "severity": "warning",
  "message": "Table header row is empty",
  "page_number": 1,
  "table_id": "doc_p1_t1",
  "row_index": 0,
  "col_index": null
}
```

## 中等改动范围

### 中台侧

1. `projection=compat|structured|full`。
2. `ParseResultV2` 可选字段。
3. `tables[].cells` 结构化输出。
4. `quality_signals` 基础规则。
5. `parse_units` 模型占位，第一期先用单 unit 表示当前完整文档，后续扩展为 PDF 页段、Excel sheet、DOCX section。
6. `profile` 字段占位，先支持请求 options 透传和基础自动推断，后续扩展为 profile 路由。

### 宿主产品侧

1. parser client 支持异步上传、创建 job、轮询 job、读取文档结果。
2. response schema 接受 V2 可选字段。
3. 数据库先增加 JSON 字段：`parse_quality_json / structured_tables_json / parse_units_json / parse_trace_json`。
4. 大文件默认走异步，小文件继续兼容同步。

### 宿主低成本接入口径

宿主产品不需要一次性改检索、复核 UI 或业务规则。推荐先把 ParseCore 当成一个“可自动降级到异步的解析后端”接入：

1. 保留现有同步调用路径，小文件继续调用 `/parse`、`/v1/parse` 或 batch 入口。
2. 同步入口返回 `413 document_too_large_for_sync` 时，不把它当失败重试；读取响应里的 `recommended_endpoint`、`recommended_job_endpoint`、`profile`、`resolved_profile`，切到 `/v1/parse/uploads + /v1/parse/jobs`。
3. 新建 job 时默认传 `profile=auto`，只有已知样本才显式传 `table-heavy`、`large-pdf`、`ocr-heavy`、`excel-ledger` 或 `scan-pdf`。
4. 读取结果默认用 `projection=structured`，旧页面字段继续双写到原表，新结构只落 JSON 字段，避免第一阶段改业务表关系。
5. `quality_signals` 先作为提示和运营观测使用，不阻断业务流；等样本稳定后再把高置信号接入人工复核或业务校验。

这条路径下，宿主最小改动集中在 parser client：增加一次 413 分支、一次上传桥接、一次 job 轮询和一次 structured 读取；旧消费者仍可读取兼容字段。

## 分阶段实施

### Phase 0：契约冻结

- 保存本计划文档。
- 固化 `ParseResultV2 / Table / Cell / QualitySignal / ParseUnit` schema。
- 明确 projection 兼容策略。

### Phase 1：中台 V2 投影

- 在 `/v1/parse/documents/{doc_id}` 支持 `projection`。
- 新增 `/v1/parse/documents/{doc_id}/quality`。
- 输出结构化 `tables / cells / quality_signals / parse_units`。

### Phase 2：产品 parser client 升级

- 小文件保留同步。
- 同步大文件不再推荐直接调用 `/parse`、`/v1/parse` 或 `/v1/parse/batch`；超过宿主侧阈值或触发 `413 document_too_large_for_sync` 时，客户端应切换到 `/v1/parse/uploads + /v1/parse/jobs`，并把任务状态回写到宿主业务单据。
- 大文件使用 `/v1/parse/uploads + /v1/parse/jobs`，创建 job 时带 `profile=auto`，由中台按文件类型、大小、页数、表格密度和 OCR 信号推断实际 profile。
- 表格密集文档可显式传 `profile=table-heavy`，优先验证 `tables/cells/quality_signals` 落库与消费链路。
- 超大 PDF、长页数 PDF 或已知慢样本可显式传 `profile=large-pdf`，默认走异步 job、轮询与结果读取，不把同步 HTTP 请求作为主链路。
- 支持轮询与读取 `projection=structured`；结果读取时 projection 负责返回形态，profile 只负责解析策略，两者不要混用。
- 结果双写旧字段和新 JSON 字段。

### Phase 3：Profile 与自动路由

- 内置 `default / large-pdf / table-heavy / ocr-heavy / excel-ledger / scan-pdf`。
- 支持 `profile=auto`，同步入口、桥接上传创建 job、`/v1/parse/jobs` 使用同一套 resolver。
- 当前自动路由先使用文件名、扩展名、media type 与字节数做低成本判断：Excel 优先 `excel-ledger`，图片和扫描倾向 `scan-pdf/ocr-heavy`，超过同步阈值或长 PDF 倾向 `large-pdf`，表格类样本倾向 `table-heavy`，无法判断时回到 `default`。
- 后续在解析前采样页数、表格密度、OCR 坏页信号和历史质量信号，逐步把 `auto` 从“入口分流”升级为“质量感知路由”。

### Phase 4：异常检测与复跑

- 质量信号扩展到页级、表格级、单元格级。
- 支持异常 part 重跑接口。
- 多引擎和 OCR 只用于异常范围，不默认全量启用。

quality_signals 扩展路线：

1. 第一批保持轻量：`table_header_missing`、`empty_table`、`truncated_table`、`ocr_failed_page`、`low_text_density` 等只依赖现有解析结果的规则。
2. 第二批加入定位：所有信号尽量带 `page_number / table_id / row_index / col_index / bbox / parse_unit_id`，让宿主无需理解 parser 内部结构也能做跳转、标注和复核。
3. 第三批加入动作建议：为信号补 `recommended_action`，例如 `review_table_header`、`retry_with_ocr`、`split_large_pdf`、`rerun_table_profile`。
4. 第四批加入稳定性指标：按租户、profile、parser、文件类型聚合信号密度，帮助宿主判断是单文档问题还是某类样本需要调整路由。

异常 part 视图与复跑接口规划：

```text
GET  /v1/parse/documents/{doc_id}/parts?state=failed|warning|done
POST /v1/parse/documents/{doc_id}/parts/{part_id}/rerun
POST /v1/parse/documents/{doc_id}/parts/rerun
POST /v1/parse/documents/{doc_id}/parts/{part_id}/cancel
GET  /v1/parse/documents/{doc_id}/parts/{part_id}/runs/{run_id}
```

请求口径：

```json
{
  "reason": "quality_signal",
  "signal_codes": ["ocr_failed_page", "truncated_table"],
  "profile": "auto",
  "engine": "default",
  "page_range": { "start": 1201, "end": 1250 },
  "reuse_clean_parts": true
}
```

本轮生产增强接口已落地：

- `POST /v1/parse/documents/{doc_id}/parts/rerun`：批量复跑入口，支持 `part_ids`、`failed_only`、`state`、`profile`，用于从异常 part 视图或质量信号面板直接触发小范围重跑。
- `POST /v1/parse/documents/{doc_id}/parts/{part_id}/cancel`：取消单个尚未运行的 part；持久化状态为 `pending`，inline runner 内部队列会先移出队列再转为 `cancelled`，运行中的 part 不强杀，会返回当前状态。
- `runtime.max_active_parts_per_doc`：单文档 active part 限流，inline 与 queue-worker 模式均会跳过已达上限的同文档 part，生产建议先按 worker 总量设为 `2-4`。

落库与合并策略：

1. `parse_unit_id / part_id / page_range / source_doc_id / parse_run_id` 是复跑最小定位字段，第一页段落地时就要保留。
2. 单 part 复跑只覆盖该 part 产出的 `pages / tables / quality_signals / blocks / chunks`，不重写其他 part 的结果。
3. 复跑结果保留 `parent_parse_run_id` 和 `rerun_reason`，便于宿主审计为什么某段内容和初次解析不同。
4. 复跑完成后生成新的 `index_manifest`，标记哪些 chunks 需要重建 embedding，避免全量 re-embed。

当前状态：`GET /v1/parse/documents/{doc_id}/parts` 和 `POST /v1/parse/documents/{doc_id}/parts/{part_id}/rerun` 第一版已落地。parts 视图基于真实 part job 与 `parse_units + quality_signals` 返回 `part_id / page_range / state / quality_signal_codes / severity_counts / job_id / rerun_supported`；复跑会创建新的指定 part 子 job，并在完成后刷新父文档读模型。

### 17000 页 PDF 页段调度路线

17000 页 PDF 不建议继续依赖单次同步解析，也不建议第一步就做完整分布式编排。中等改造目标是把超长 PDF 拆成可观测、可复跑、可逐步合并的页段任务，同时保持宿主产品只接入异步 job 和 structured projection。

推荐默认参数：

| 项 | 建议值 | 说明 |
| --- | ---: | --- |
| `target_pages_per_part` | 100-300 | 普通文本型 PDF 先用较大页段，减少调度开销。 |
| `ocr_heavy_pages_per_part` | 20-50 | OCR 密集或扫描件降低页段大小，避免单 part 长尾。 |
| `max_active_parts_per_doc` | 2-4 | 单文档内限流，防止一个 17000 页任务吃满 worker。 |
| `part_timeout_seconds` | 按 profile 配置 | queue-worker 软超时回收阈值；超时后按 `max_attempts` 和退避策略重试或 dead-letter，文档保留 partial 状态。 |
| `merge_checkpoint_parts` | 10-20 | 每完成一批 part 刷新一次 manifest 和质量摘要。 |

中等改造步骤：

1. 入队阶段先做 PDF 轻量探测，只读取页数、基本 metadata 和少量采样页，超过阈值时把 job 标记为 `partitioned=true`。
2. 生成 `parse_units`：每个 unit 对应连续页段，记录 `page_start / page_end / state / attempts / profile / parser_options`。
3. Worker 仍执行现有 parser，但通过页段参数限制处理范围；第一期可先支持 PDF 文本通道页段，OCR 页段随后接入。
4. 每个 part 独立写入中间结果，完成后增量合并 `pages / tables / quality_signals`，并刷新 `index_manifest`。
5. 文档状态区分 `running / partial / done / failed`：只要部分 part 成功，宿主就可以读取 partial structured 结果和异常 part 列表。
6. 异常 part 通过上一节规划接口复跑，优先只对失败页段启用 OCR、多引擎或更小页段，不默认全量重跑。

已落地边界：`profile=large-pdf`、同步 413 分流、异步上传与 job、PDF 物理页段切分、part 子 job、父文档 `partial` 状态、part 级结果合并、part 级复跑、structured 读取、质量信号和 profile_resolution。本轮生产增强已补齐单文档 active part 限流 `max_active_parts_per_doc`、批量复跑 `/parts/rerun`、尚未运行 part 取消 `/parts/{part_id}/cancel`、queue-worker 失败退避和 job/part 软超时。仍需增强：只重建受影响 part 的 embedding/index layer。

### Phase 5：导出与人工复核

- JSONL / CSV / TSV / Parquet 导出。
- 异常页截图、raw cells、trace 打包。
- 人工复核 UI 基于 `quality_signals + table_id + bbox` 构建。

导出中心 MVP 已落地：

```text
GET /v1/parse/documents/{doc_id}/exports?dataset=tables&format=csv
GET /v1/parse/documents/{doc_id}/exports?dataset=quality_signals&format=jsonl
GET /v1/parse/documents/{doc_id}/exports?dataset=parse_units&format=tsv
```

支持 `dataset=tables|quality_signals|parse_units` 和 `format=jsonl|csv|tsv`。CSV/TSV 中的嵌套字段会以 JSON 字符串输出，适合运营排查和宿主侧二次加工。

异步导出包 MVP 已落地：

```text
POST /v1/parse/documents/{doc_id}/export-jobs
GET  /v1/parse/export-jobs/{export_id}
GET  /v1/parse/export-jobs/{export_id}/download?file=...
```

创建导出请求：

```json
{
  "doc_id": "doc_xxx",
  "projection": "structured",
  "formats": ["jsonl", "csv"],
  "include": ["pages", "tables", "quality_signals", "parse_units", "trace"],
  "filters": {
    "severity": ["warning", "error"],
    "page_range": { "start": 1, "end": 300 },
    "part_state": ["failed", "warning"]
  }
}
```

使用口径：

1. 宿主产品在 job 完成或 partial 可读后创建导出任务，不在解析 HTTP 请求里同步生成大包。
2. `jsonl` 作为默认机器消费格式，每行带 `doc_id / parse_run_id / parse_unit_id / page_number / table_id`。
3. `csv/tsv` 只导出表格和质量信号的扁平视图，适合运营排查和人工复核前置筛选。
4. `parquet` 面向后续离线分析，第一期可只作为规划项，不阻塞导出中心 MVP。
5. `trace`、异常页截图和 raw cells 只在显式 include 时打包，避免默认导出体积过大。

当前状态：同步导出和异步导出包 MVP 已落地；`parquet`、异常页截图、raw cells 与 trace 打包仍未落地。

## 已实施边界

Phase 0 和 Phase 1 的中台侧能力已完成：

1. 新增计划文档。
2. 增加 `projection=compat|structured|full`。
3. 增加 `/quality` 入口。
4. 输出基础 `tables/cells`。
5. 输出基础 `quality_signals`。
6. 输出单文档级 `parse_units`。

Phase 2 的中台承接能力已完成：

1. 新增 `profile` resolver，支持 `auto / default / large-pdf / table-heavy / ocr-heavy / excel-ledger / scan-pdf`。
2. `/parse`、`/v1/parse`、`/parse/batch`、`/v1/parse/batch` 在同步超限或 profile 建议异步时返回 `413 document_too_large_for_sync`。
3. 错误 detail 固定返回 `/v1/parse/uploads + /v1/parse/jobs` 推荐链路和 resolved profile。
4. `/parse/uploads`、`/v1/parse/uploads` 使用独立 `staged_upload_max_bytes`，默认保持可承接同步入口拒绝的大文件。
5. `/v1/parse/jobs` 和桥接上传创建 job 时会把 effective profile 写入 job options。
6. 新增 `/v1/parse/profiles` 与 `/v1/runtime.profiles`，支持宿主产品发现 supported profiles、auto 阈值和推荐异步 profile。
7. `projection=structured` 与 `/quality` 输出 `profile_resolution`，保留 requested/resolved/source/reasons/limits。
8. 表格质量信号扩展到截断、隐藏 sheet、合并单元格、公式单元格、空表头和重复表头，全部只追加字段。
9. 新增 `/v1/parse/documents/{doc_id}/parts` 只读视图，宿主可按 `state=warning|failed` 查找异常页段。
10. 新增 PDF part 调度、父文档 partial 合并、part 级复跑和异步导出包 MVP。

暂不做：

- 宿主产品 parser client 自动迁移；本轮只把中台接口能力准备好。
- 人工复核 UI。
- 默认多引擎解析。
- 生产级优先级和更细 timeout 编排；批量 part 复跑、尚未运行 part 取消、active part 限流、失败退避和软 timeout 已完成第一版。
- 只重建受影响 part 的 embedding/index layer。
- `parquet`、截图、raw cells、trace 打包。
