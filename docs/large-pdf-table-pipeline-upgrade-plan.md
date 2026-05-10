# ParseCore 超大表格文档下一阶段升级计划

日期：2026-05-10

## 背景

本计划来自 `D:\app\uploads\bigpdf` 下真实 17,101 页 PDF 的解析验证。该文档主体是跨页表格/目录清单，最终通过轻量逐页文本抽取、记录聚合、SQLite/CSV/JSONL/Excel 导出完成了可查询数据落盘：

- 页数：17,101
- 行级数据：1,249,000 行
- 记录级数据：454,985 条
- 产物：`catalog.sqlite`、`catalog_records.xlsx`、`records_compact.csv`、`records.jsonl`、`pages.jsonl`、`lines.csv`

这次验证说明中台已有大文件、profile、part、quality、export 的基础，但仍有一些能力停留在“可通过脚本补齐”的状态。下一阶段目标是把这条成功路径沉淀为中台默认能力，让宿主产品可以通过稳定 API 获得高质量、可追溯、可查询的数据表。

## 目标

1. 超大 PDF 默认走异步、分片、流式处理，不再让 worker 误跑整文档长任务。
2. 新增面向目录/台账型 PDF 的流式表格 profile，优先产出记录级结构化数据。
3. 将页级、行级、记录级数据和质量信号作为一等产物，支持查询、导出、复跑和审计。
4. 将 Excel、CSV、JSONL、SQLite 等导出能力产品化，避免依赖临时脚本。
5. 对列错位、跨页表头、续行、字段异常建立可解释质量信号。
6. 保持旧 API 和旧字段兼容，宿主产品只需中等改动即可接入，后续可自然升级到人工复核和多引擎对照。

## 非目标

- 不把全文 VLM 作为默认解析主链路。
- 不要求宿主产品一次性重构数据库和业务 UI。
- 不把人工复核平台放入本阶段强制范围。
- 不用重型表格引擎覆盖所有页面；对超大目录型 PDF 优先选择确定性、低内存、可复跑的流式方案。

## 目标链路

```text
文件上传/外部路径
  -> 预检采样
  -> profile=auto 路由
  -> 超大 PDF part 规划
  -> 流式页级解析
  -> 行级归一化
  -> 记录级聚合
  -> 字段抽取与质量信号
  -> 增量落库
  -> 查询 API / 导出 job / part 复跑
```

推荐宿主接入链路保持不变：

```text
POST /v1/parse/uploads
POST /v1/parse/jobs
GET  /v1/parse/jobs/{job_id}
GET  /v1/parse/documents/{doc_id}?projection=structured
GET  /v1/parse/documents/{doc_id}/quality
POST /v1/parse/documents/{doc_id}/export-jobs
```

## 阶段安排

### Phase 1：预检与自动路由

目标：在真正解析前识别“超大目录/台账型 PDF”，把任务路由到轻量流式链路。

任务：

- 新增 `large-pdf-ledger` 或 `large-pdf-catalog` profile。
- 扩展 `profile=auto`，基于页数、文件大小、采样页文本密度、行号密度、日期/编号模式、表头重复度识别目录型 PDF。
- 对超过同步阈值或页数阈值的 PDF，默认要求异步 job，不再建议同步解析。
- 在 profile resolution 中输出路由原因，例如 `page_count_gt_threshold`、`ledger_like_rows_detected`、`table_header_repeated`。

验收：

- 17,101 页样本被自动路由到新 profile。
- 同步入口返回的大文件分流信息仍兼容 `document_too_large_for_sync`。
- 小型普通 PDF 不被错误路由到超大表格 profile。

### Phase 2：part 调度硬化

目标：保证超大 PDF 的 worker 执行单位一定是 part，而不是整文档。

任务：

- 解析 job 创建后先进入 `partition_planning` 或等价阶段。
- 父 job 只负责 preflight、part plan、汇总和状态管理。
- 子 job 必须带 `part_doc_id / parent_doc_id / page_start / page_end / part_index`。
- worker claim 时优先领取 part job；对已标记 partitioned 的父 job 禁止直接执行整文档 parser。
- 增加 part lease、timeout、retry、cancel、failed-only rerun 的组合测试。
- `max_active_parts_per_doc` 默认对超大 PDF 生效，避免单个文档吃满 worker。

验收：

- 17,101 页样本不会出现 worker 领取父 job 后尝试整文档解析。
- 中断 worker 后，未完成 part 可被重新领取，已完成 part 不重复覆盖。
- 父文档在部分 part 完成后可返回 `partial` structured projection。

### Phase 3：流式目录/台账解析器

目标：把这次临时脚本沉淀为中台 parser/backend/stage 能力。

任务：

- 新增流式 PDF 文本读取 stage，按页输出 `pages` 和 `lines`，避免一次性加载全量结果。
- 新增记录聚合器，支持按序号、证件编号、项目编号、日期、缩进和跨页续行聚合记录。
- 为记录保留完整 provenance：`page_start / page_end / line_start / line_end / raw_text / normalized_text`。
- 支持 profile 配置字段：

```toml
[profiles.large-pdf-catalog]
target_pages_per_part = 200
record_start_patterns = ["^\\d+\\s+"]
field_hints = ["certificate_no", "holder", "model", "latest_date"]
stream_exports = true
```

- 对每个 part 增量写入中间表，父文档合并时不重跑已完成 part。

验收：

- 17,101 页样本可在低内存模式下完成页级、行级、记录级产出。
- 输出记录数与当前验证基线保持可解释差异，差异超过阈值时生成质量报告。
- 任意 part 可单独复跑并只替换该 part 的记录。

### Phase 4：结构化记录模型与查询

目标：让“记录级数据表”成为中台标准产物，而不是导出脚本私有格式。

新增 Record 模型：

```json
{
  "record_id": "rec_xxx",
  "doc_id": "doc_xxx",
  "parse_run_id": "job_xxx",
  "parse_unit_id": "part_001",
  "section": "七、已获批准的PMA产品目录清单",
  "row_number": 31223,
  "page_start": 2214,
  "page_end": 2214,
  "fields": {
    "certificate_or_project_no": "PMA0013-01-XN",
    "holder_or_name": "重庆兴山泉航空设备有限公司",
    "model_or_part_no": "SQ-737-1518",
    "latest_date": "2025-..."
  },
  "raw_text": "...",
  "normalized_text": "...",
  "quality_signals": []
}
```

任务：

- 在 structured projection 中新增可选 `records_summary`，避免默认返回超大 records 数组。
- 新增 records 查询接口，支持分页、页码、章节、字段、关键词和质量信号筛选。
- SQLite/Postgres 存储层补记录表和全文检索索引。
- 保持 `tables / cells / quality_signals / parse_units` 现有契约不破坏。

建议接口：

```text
GET /v1/parse/documents/{doc_id}/records?limit=100&offset=0
GET /v1/parse/documents/{doc_id}/records?query=波音
GET /v1/parse/documents/{doc_id}/records?section=PMA&page_start=2000&page_end=2300
GET /v1/parse/documents/{doc_id}/records/{record_id}
```

验收：

- 454,985 条记录级数据不通过单个 document payload 全量返回。
- SQLite 查询和 API 分页查询结果一致。
- 关键词查询可返回 record id、页码、字段和原文片段。

### Phase 5：列错位与质量信号

目标：把“表格列错位”从人工肉眼问题变成可检测、可筛选、可复跑的质量信号。

任务：

- 增加表头识别与跨页表头继承。
- 增加字段类型校验：日期、证件编号、序号、型号/件号、公司名。
- 增加列漂移检测：同一字段在相邻行中的位置突然偏移时标记 warning。
- 增加续行检测：未出现新序号但延续上一行字段时合并并标记。
- 增加异常页段聚合，输出最差 part 和最差页 TOP N。

新增质量信号建议：

| code | severity | 含义 |
| --- | --- | --- |
| `column_shift_suspected` | warning | 疑似列内容错位 |
| `row_continuation_detected` | info | 检测到跨行续接 |
| `header_inherited_from_previous_page` | info | 使用上一页表头 |
| `date_parse_failed` | warning | 日期字段无法规范化 |
| `record_field_missing` | warning | 关键字段缺失 |
| `record_boundary_uncertain` | warning | 记录边界置信度不足 |

验收：

- 质量报告能列出疑似错列页段和样例记录。
- 可按 quality signal 查询 records。
- 异常 part 可触发单 part 复跑或导出排查包。

### Phase 6：导出 job 产品化

目标：把本次 Excel/SQLite/CSV/JSONL 产物变成内置异步导出能力。

任务：

- 扩展 export job，支持 `records`、`pages`、`lines`、`quality_signals`、`manifest`。
- 支持格式：`xlsx`、`csv`、`jsonl`、`sqlite`；后续可追加 `parquet`。
- 大结果集一律流式写出，避免内存中拼装完整表。
- 导出包包含 `manifest.json`、`summary.json` 和 README。
- Excel 默认只放 compact records；原始全文放 JSONL/SQLite，避免 Excel 文件过大或换行破坏阅读。

建议请求：

```json
{
  "include": ["records", "quality_signals", "manifest"],
  "formats": ["xlsx", "csv", "jsonl", "sqlite"],
  "filters": {
    "page_range": {"start": 1, "end": 17101},
    "severity": ["warning", "error"]
  }
}
```

验收：

- 17,101 页样本可生成 Excel、SQLite、CSV、JSONL 导出包。
- 导出 job 可查询状态、重试、下载单文件。
- 导出结果中的 record count 与 records 查询接口一致。

### Phase 7：性能基线与发布门禁

目标：把真实超大 PDF 变成稳定性样本，避免后续改动让性能或结果质量退化。

任务：

- 将该类样本登记为本地/夜间 perf fixture，不放入普通 PR 快速门禁。
- 增加 benchmark 输出：
  - preflight elapsed
  - plan elapsed
  - part elapsed p50/p90/p99
  - partial available at
  - records per second
  - export elapsed
  - peak memory
  - quality signal density
- 对比上一版 `summary.json`，记录页数、行数、记录数、章节分布和异常信号差异。
- 在 `self-check` 中增加可选 `--large-pdf-fixture` 路径。

验收：

- 夜间或手动 perf 能复跑真实样本。
- 记录数、章节分布、导出格式、查询能力有稳定回归报告。
- 性能退化和质量退化可以分别定位。

## 数据落库建议

中台内部建议增加四类表或等价存储：

| 表 | 用途 |
| --- | --- |
| `parse_pages` | 页级文本、页状态、来源 parser、质量摘要 |
| `parse_lines` | 行级文本、页码、行号、可选 bbox |
| `parse_records` | 记录级结构化字段、原文、归一化文本、part 定位 |
| `parse_record_fts` | 记录级全文检索 |

宿主产品第一阶段不需要拆业务表，只需继续保存 structured JSON 和导出文件引用。等记录查询稳定后，再按业务价值选择是否把 records 同步到宿主业务数据库。

## 配置建议

```toml
[runtime]
execution_mode = "queue-worker"
max_upload_bytes = 52428800
staged_upload_max_bytes = 0
max_active_parts_per_doc = 2
part_timeout_seconds = 900

[profiles.auto]
large_pdf_page_threshold = 1000
large_pdf_bytes_threshold = 52428800
ledger_sample_pages = 8

[profiles.large-pdf-catalog]
target_pages_per_part = 200
stream_pages = true
stream_lines = true
stream_records = true
enable_record_fts = true
default_exports = ["sqlite", "jsonl", "csv", "xlsx"]
```

## 宿主产品改动范围

中等改动即可接入：

1. 大文件走 `/v1/parse/uploads + /v1/parse/jobs`。
2. 创建 job 时默认传 `profile=auto`。
3. 轮询 job，允许 `partial` 状态展示进度。
4. 读取 `projection=structured` 保存质量摘要和 parse units。
5. records 大结果集通过 records 查询接口或 export job 获取，不放进主文档响应。
6. 质量信号先作为提示和运营排障，不阻断主业务。

不建议宿主在第一阶段做的事：

- 不要直接解析中台内部 SQLite 文件作为唯一集成方式。
- 不要假设 quality signal code 固定不变。
- 不要把 454,985 条 records 全量塞进单个 JSON 字段。
- 不要对大文件继续调用同步 `/parse` 作为主链路。

## 里程碑

| 里程碑 | 交付物 | 建议顺序 |
| --- | --- | --- |
| M1 | profile 自动路由 + 父 job 禁止整文档误执行 | 最高 |
| M2 | 流式 pages/lines/records parser MVP | 最高 |
| M3 | records 存储与查询 API | 高 |
| M4 | 列错位质量信号 MVP | 高 |
| M5 | records 导出 job：SQLite/JSONL/CSV/XLSX | 高 |
| M6 | 真实大样本 perf/quality baseline | 中 |
| M7 | part 级异常复跑与导出排查包联动 | 中 |

## 当前实施进展

2026-05-10 首轮已落地：

- 已新增 `large-pdf-catalog` 与 `large-pdf-ledger` profile，并纳入 `profile=auto` 的保守路由和异步推荐 profile 列表。
- 已为 partitioned PDF 父 job 增加执行边界保护：队列 claim 跳过父 job，指定 claim 拒绝父 job，`execute()` 对 `pdf_parent` 做硬拒绝，避免 worker 误跑整文档。
- structured projection 已新增 `records_summary`，默认不返回完整 records 数组。
- 已新增 `GET /v1/parse/documents/{doc_id}/records` 分页查询入口，支持 `limit / offset / query / table_id / page_start / page_end`。
- 同步导出和异步 export job 已支持显式 `dataset=records`，格式覆盖 `jsonl/csv/tsv/sqlite/xlsx`。
- `large-pdf-catalog` / `large-pdf-ledger` 下已支持从 PDF 文本块按“序号开头行 + 续行”聚合 text-derived records，并输出 `record_field_missing`、`row_continuation_detected` 等记录级质量信号。
- 解析完成后已将 `pages / lines / records` 作为 document views 持久化到 JobStore，SQLite、Postgres、InMemory 三种 store 具备同名读写方法；records API 会优先读取持久化结果，缺失时回退到现有 block 投影。

## 风险与应对

| 风险 | 应对 |
| --- | --- |
| 目录型 PDF 版式变化大 | profile 支持规则配置，保留 raw pages/lines 便于重聚合 |
| Excel 行数或单元格内容超限 | Excel 只做 compact 视图，完整原文走 SQLite/JSONL |
| part 合并覆盖错误 | 所有记录带 `parse_unit_id`，复跑只按 part 替换 |
| OCR 或重型表格引擎拖慢整体 | 默认不全量启用，只对低质量页段复跑 |
| 宿主一次性改动过大 | records 查询和 export job 作为新增能力，旧字段继续兼容 |

## 验收清单

- [x] 真实 17,101 页 PDF 自动路由到超大目录型 profile。
- [x] worker 不再直接解析 partitioned 父 job。
- [x] 可产出并持久化 pages、lines、records 三层数据。
- [x] records 支持分页查询和关键词查询。
- [x] 可导出 Excel、SQLite、CSV、JSONL。
- [ ] 质量报告包含列错位、字段缺失、日期异常、边界不确定等信号。
- [ ] 异常 part 可单独复跑，非异常 part 结果保持不变。
- [ ] perf baseline 能对比页数、行数、记录数、章节分布、耗时和导出结果。

## 推荐实施顺序

1. 先做 `profile=auto -> large-pdf-catalog` 和父/子 job 执行边界，解决稳定性风险。
2. 再把本次脚本能力沉淀为流式 parser，优先复现 pages/lines/records 和 SQLite/JSONL。
3. 接着做 records 查询 API 和 compact Excel/CSV 导出，补齐用户可用性。
4. 然后做列错位质量信号和异常 part 复跑联动，提高数据可信度。
5. 最后把真实样本纳入 perf/quality baseline，形成发版门禁。
