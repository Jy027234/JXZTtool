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
- 大文件使用 `/v1/parse/uploads + /v1/parse/jobs`。
- 支持轮询与读取 `projection=structured`。
- 结果双写旧字段和新 JSON 字段。

### Phase 3：Profile 与自动路由

- 内置 `default / large-pdf / table-heavy / ocr-heavy / excel-ledger / scan-pdf`。
- 支持 `profile=auto`。
- 按文件类型、大小、页数和表格密度推荐 profile。

### Phase 4：异常检测与复跑

- 质量信号扩展到页级、表格级、单元格级。
- 支持异常 part 重跑接口。
- 多引擎和 OCR 只用于异常范围，不默认全量启用。

### Phase 5：导出与人工复核

- JSONL / CSV / TSV / Parquet 导出。
- 异常页截图、raw cells、trace 打包。
- 人工复核 UI 基于 `quality_signals + table_id + bbox` 构建。

## 第一批实现边界

本轮先实施 Phase 0 和 Phase 1 的中台侧能力：

1. 新增计划文档。
2. 增加 `projection=compat|structured|full`。
3. 增加 `/quality` 入口。
4. 输出基础 `tables/cells`。
5. 输出基础 `quality_signals`。
6. 输出单文档级 `parse_units`。

暂不做：

- 物理 PDF 自动分片。
- 宿主产品 parser client 改造。
- 完整导出中心。
- 人工复核 UI。
- 默认多引擎解析。
