# 性能与稳定性优化口径

## 分层门禁

当前质量门禁按反馈速度分三层：

1. 本地快速门禁：提交前运行单测和 runtime smoke。

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --skip-regression
```

2. PR / 主分支 CI：`.github/workflows/parsecore-ci.yml` 自动运行快速门禁，并额外验证 base SDK import 不会加载 API 可选依赖。

3. 定时或手动 CI：运行完整回归套件和 Postgres + pgvector smoke。OCR 长尾和存储层问题不阻塞日常 PR，但会在夜间或发版前暴露。

若要把 `perf` 从一次性手工执行升级成持续趋势，建议为夜间或手动 CI 准备一台带持久磁盘的 runner，并至少提供：

- `PARSECORE_REGRESSION_FIXTURE_ROOT`：perf 样本根目录
- `PARSECORE_PERF_HISTORY_DIR`：用于保存 `latest.perf.json` 的持久目录

可选项：

- `PARSECORE_PREVIOUS_PERF_REPORT`：显式指定上一份 perf 报告路径；若未配置，则 workflow 会优先读取 `PARSECORE_PERF_HISTORY_DIR/latest.perf.json`

## 上传保护

## 大 PDF 压测入口

`parsecore large-pdf-stress` 用于验证页段规划、part 子 job、父文档增量合并和 manifest part index。默认是 plan-only，适合先验证 17000 页级别的切分成本；需要压实际解析链路时再加 `--execute-parts`，并用 `--max-parts` 先抽样。

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli large-pdf-stress --config parsecore.toml --generate-pages 17000 --target-pages-per-part 200 --out-json var/self-check/large-pdf-stress.json --out-md var/self-check/large-pdf-stress.md
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli large-pdf-stress --config parsecore.toml --pdf D:/samples/large.pdf --target-pages-per-part 200 --execute-parts --max-parts 5
```

报告会输出 `planned_parts / executed_parts / plan_elapsed_s / part_timings / manifest_part_index`。其中 `manifest_part_index` 对应文档 `index_manifest.part_index.parts[]`，可用于确认 `chunk_ids / page_range / index_version` 是否随 part rerun 正常刷新。

API 同步入口支持运行时文件大小保护：

```toml
[runtime]
max_upload_bytes = 52428800
staged_upload_max_bytes = 0
```

覆盖入口：

- `POST /parse`
- `POST /v1/parse`
- `POST /parse/batch`
- `POST /v1/parse/batch`

超过同步限制时返回 `413 document_too_large_for_sync`，响应会带 `actual_bytes / limit_bytes`、`recommended_endpoint=/v1/parse/uploads`、`recommended_job_endpoint=/v1/parse/jobs`、`can_force_sync` 和 `force_sync_param_names`，便于宿主系统记录并切换异步链路。设置为 `0` 表示关闭同步上传大小限制。

桥接上传入口 `/parse/uploads` 和 `/v1/parse/uploads` 使用 `staged_upload_max_bytes`。默认 `0` 表示不限制，用于承接大文件异步任务；对公网开放时建议同时启用 `staged_upload_api_key_env`，并按业务上限设置暂存大小。

推荐宿主 client 把 `document_too_large_for_sync` 视为可恢复分流信号，而不是解析失败：

```text
同步提交 profile=auto
  -> 2xx：按原兼容字段消费，必要时补读 projection=structured
  -> 413 document_too_large_for_sync：上传到 /v1/parse/uploads
      -> 用 parsecore_server_file_path 创建 /v1/parse/jobs
      -> 轮询 job
      -> 读取 projection=structured
```

这样宿主不需要提前维护复杂文件分类规则，只要保留一条 413 分支即可承接大文件。`staged_upload_max_bytes` 应高于 `max_upload_bytes`；若两者设成相同值，大文件会同时被同步入口和桥接入口拒绝，宿主就只能回到外部对象存储或人工拆分。

宿主启动时可调用 `GET /v1/parse/profiles` 读取支持的 profile 和 auto 阈值；文档完成后用 `profile_resolution` 回写 resolved profile、reasons 和 profile warning，便于后续按租户、文件类型和 profile 聚合稳定性。

## Profile 自动路由与质量信号

`profile=auto` 是灰度默认值。当前路由主要按文件扩展名、media type、文件大小和入口上下文选择 effective profile：

- `large-pdf`：同步超限、长页数或已知慢 PDF，优先异步。
- `table-heavy`：表格密集 PDF/DOCX，重点观察表结构和表头信号。
- `excel-ledger`：Excel 台账，重点观察 sheet、cell range、merged cells 和截断信号。
- `ocr-heavy` / `scan-pdf`：扫描件、图片型文档或 OCR fallback 多的样本。
- `default`：无法判断或普通小文件。

性能和稳定性观测不要只看耗时，也要把 `quality_signals` 纳入灰度面板。第一阶段建议统计：

- 每百页 signal 数、warning/error 占比。
- 按 `profile / parser / media_type / tenant_id` 聚合的 signal 密度。
- `table_header_missing / empty_table / truncated_table / ocr_failed_page / low_text_density` 等高频 code。
- 同一文档在 `profile=auto` 与显式 profile 下的 signal 差异。

后续 quality_signals 会继续追加页级、表格级、单元格级和动作建议字段。宿主告警应按 `severity` 和已知 `code` 做白名单策略，未知 code 先记录和展示，不应导致解析主流程失败。

## API 鉴权

对外或共享环境建议显式开启入口鉴权：

```toml
[runtime]
api_key_env = "PARSECORE_API_KEY"
```

行为口径：

- 配置 `api_key_env` 后，除 `GET /health` 外，其余 HTTP 接口都要求 `x-api-key` 或 `Authorization: Bearer`。
- 若配置了 `api_key_env` 但对应环境变量为空，应用会在启动阶段直接失败，而不是静默裸奔。
- `GET /v1/runtime` 的 `runtime.api_auth_enabled` 会暴露当前是否开启了入口鉴权，便于联调时快速确认。

建议至少验证以下三项：

1. `GET /health` 无鉴权仍返回 `200`。
2. `GET /v1/runtime` 无鉴权返回 `401 unauthorized`。
3. 使用正确的 `x-api-key` 或 `Authorization: Bearer` 后，受保护接口恢复正常。

## OCR Benchmark

OCR 重样本不要混在普通 PR 反馈里压时长，使用专项工具观察：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/ocr_benchmark.py --config parsecore.toml --pdf samples/heavy-ocr.pdf --out var/self-check/ocr-benchmark.json
```

输出包含：

- 文档总耗时、blocks/chunks 数量
- OCR attempted / fallback / failed 页数
- OCR 总耗时、最慢 OCR 页耗时
- top OCR 热页和分类旋转稀疏页
- 基础结构质量摘要与 noisy pages

发版前建议固定 1 到 3 个真实 OCR 长尾样本，保存 benchmark JSON，与上一个灰度版本对比：

- `ocr_total_elapsed_s` 是否明显放大
- `max_ocr_page_elapsed_s` 是否出现新尖峰
- `ocr_failed_pages` 是否从 0 变为非 0
- `very_short_ratio` 和 noisy pages 是否异常升高

## Perf 趋势跟踪

当前 perf workflow 已支持两类路径：

1. 单次执行：只产出当次 `parsecore-self-check-perf.json` 与 Actions summary。
2. 持续趋势执行：runner 提供 `PARSECORE_PERF_HISTORY_DIR` 后，workflow 会自动读取上一份 `latest.perf.json` 做 `--compare-report`，并在本次结束后覆盖写回最新报告。

Actions summary 会额外展示：

- 每个 perf 样本的 `elapsed_s / ocr_total_s / call_s / provider_s / rec_s / max_page_ocr_s`
- 与上一份 perf 报告相比的 delta 表
- 当前最慢样本和 OCR 最重样本

同一份摘要也可以在本地从 self-check JSON 生成：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/perf_trend_report.py var/self-check/latest.perf.json --out-md var/self-check/latest.perf.md --out-json var/self-check/latest.perf.summary.json
```

推荐约定：

```text
PARSECORE_REGRESSION_FIXTURE_ROOT=/mnt/parsecore-fixtures
PARSECORE_PERF_HISTORY_DIR=/var/lib/parsecore/perf-history
```

如果 runner 无持久磁盘，仍可通过手工注入 `PARSECORE_PREVIOUS_PERF_REPORT` 路径启用一次性的对比模式，但这种方式不适合作为长期趋势口径。

## 17000 页 PDF 中等改造路线

当前已落地的能力包括同步入口保护、`profile=large-pdf` 异步分流、job 轮询、structured projection、`parse_units`、`quality_signals`，以及 PDF 页段调度第一版。中台现在可以把长 PDF 切成物理 part PDF，分别创建子 job，子 part 完成后刷新父文档 partial/structured 读模型。

当前第一版执行口径：

1. `POST /v1/parse/documents/{doc_id}/parts/plan`：读取最新 PDF job，探测页数，生成连续 `parse_units` 和子 part job。
2. 子 part 使用独立 `part_doc_id` 解析，防止覆盖父文档 blocks/chunks。
3. 父 job 进入 `partial`，子 part 完成后合并已完成 part 的 blocks/chunks 并刷新父 structured projection。
4. `POST /v1/parse/documents/{doc_id}/parts/{part_id}/rerun`：只重跑指定页段，其他 part 结果保留。
5. 普通文本型 PDF 建议 100-300 页/part；OCR 密集样本建议 20-50 页/part。

已落地的生产增强能力：

- 单文档 active parts 限流，配置口径为 `runtime.max_active_parts_per_doc`，生产建议先设 `2-4`。
- 尚未运行 part 取消，接口口径为 `POST /v1/parse/documents/{doc_id}/parts/{part_id}/cancel`。
- queue-worker 失败指数退避、job/part 软 timeout、claim_token 写回保护，以及批量 failed-only rerun。
- part 成功完成后，父文档按 `part_id` 前缀替换 blocks/chunks，并只重建受影响 part 的 index rows；`index_manifest.part_index.parts[]` 记录每个 part 的 `chunk_ids / page_range / index_version`。

性能验收建议新增三组指标：

- 调度指标：`parts_total / parts_done / parts_failed / parts_active / parts_queued / parts_cancelled / partial_available_at_s`。
- 长尾指标：`part_elapsed_p50/p90/p99`、最慢 part 页段、part timeout 次数。
- 合并指标：manifest 刷新耗时、增量 chunks 数、增量 embedding 耗时。

异常 part 视图与复跑接口规划为：

```text
GET  /v1/parse/documents/{doc_id}/parts
POST /v1/parse/documents/{doc_id}/parts/{part_id}/rerun
POST /v1/parse/documents/{doc_id}/parts/rerun
POST /v1/parse/documents/{doc_id}/parts/{part_id}/cancel
```

本轮生产增强接口中，批量复跑请求支持 `part_ids`、`failed_only`、`state`、`profile`。取消只保证尚未运行的 part：运行中的 part 不强杀，会返回当前状态。复跑触发条件建议先来自 `quality_signals`，例如 `ocr_failed_page / truncated_table / low_text_density`。后续应继续支持按 signal 批量复跑，并优先只调整该 part 的 profile 或 OCR 策略，不默认对整份 PDF 开多引擎。

## 导出与排查包

导出中心同步 MVP 已落地。当前排查建议直接导出：

```text
GET /v1/parse/documents/{doc_id}/exports?dataset=tables&format=csv
GET /v1/parse/documents/{doc_id}/exports?dataset=quality_signals&format=jsonl
GET /v1/parse/documents/{doc_id}/exports?dataset=parse_units&format=tsv
```

宿主侧临时导出建议拆成三类：

- 机器消费：把 `pages / tables / quality_signals / parse_units` 转成 JSONL，每行保留 `doc_id / parse_run_id / parse_unit_id / page_number`。
- 运营排查：把 `tables` 和 `quality_signals` 转成 CSV/TSV，按 severity、page range、table_id 筛选。
- 深挖包：只在问题单需要时附带 `ocr_decision_trace`、raw cells、异常页截图或 parser trace，避免默认导出过大。

超大结果集可走已落地的异步导出包 MVP：

```text
POST /v1/parse/documents/{doc_id}/export-jobs
GET  /v1/parse/export-jobs/{export_id}
GET  /v1/parse/export-jobs/{export_id}/download?file=...
```

第一期已经支持 `jsonl/csv/tsv` 和 manifest；`parquet`、截图包、raw cells 和完整 trace 包可以等 part 调度与复跑接口稳定后再补。

只读 part 视图也已作为排障入口落地：

```text
GET /v1/parse/documents/{doc_id}/parts
GET /v1/parse/documents/{doc_id}/parts?state=warning|failed
```

它会把 `parse_units + quality_signals` 合并成 `part_id / page_range / state / quality_signal_codes / severity_counts`。已由 PDF 页段调度产生的 part 会返回 `rerun_supported=true` 和 `job_id`，可通过单 part 或批量复跑接口做小范围恢复；非页段化文档仍会保持 `rerun_supported=false`。

## 监控与告警口径

建议把以下四个接口作为默认运行态观察面：

1. `GET /v1/parse/metrics`：失败率、活跃任务数、耗时分位。
2. `GET /v1/parse/indexes/metrics`：索引覆盖、查询效果和趋势桶。
3. `GET /v1/parse/events`：最近事件与具体错误原因。
4. `GET /v1/parse/prometheus`：Prometheus 格式计数器，适合接告警系统。

当前 Prometheus 关键指标：

- `parse_quota_exceeded_total`
- `parse_inflight_full_total`
- `parse_embedding_retry_total`
- `parse_embedding_skipped_total`
- `parse_ocr_attempt_total`
- `parse_ocr_fallback_total`
- `parse_ocr_failed_total`
- `parse_job_retry_scheduled_total`
- `parse_job_timeout_total`
- `parse_ringbuffer_size`

本轮生产增强指标建议为 part 调度补充：

- `parts_total`
- `parts_done`
- `parts_failed`
- `parts_active`
- `parts_queued`
- `parts_cancelled`
- `parts_retry_pending`

建议默认告警规则：

1. 主线失败率告警：最近 15 到 30 分钟 `failure_rate > 0.05`，并结合 `durations_s.p99` 明显偏离基线。
2. 背压告警：`increase(parse_inflight_full_total[15m]) > 0`，说明当前 worker 或入口吞吐已经顶满。
3. 配额异常告警：`increase(parse_quota_exceeded_total[15m])` 持续增长且集中在同一租户，说明配额配置或流量模型失真。
4. embedding 退化告警：`increase(parse_embedding_skipped_total[30m]) > 0`，或 `retry_total` 相比平时显著放大。
5. OCR 回退失败告警：`increase(parse_ocr_failed_total[30m]) > 0` 且 `increase(parse_ocr_attempt_total[30m]) > 0`；若接 PromQL，可进一步关注 `increase(parse_ocr_failed_total[30m]) / clamp_min(increase(parse_ocr_attempt_total[30m]), 1) > 0.05`。

## 排障顺序

建议按固定顺序排查，避免把接入问题、吞吐问题和 OCR 长尾混在一起：

1. 先看 `GET /health` 与 `GET /v1/runtime`，确认服务存活、parser 注册和 `api_auth_enabled` 状态。
2. 再看 `GET /v1/parse/metrics`，确认 `failure_rate`、`active_jobs` 和 `durations_s.p99` 是否异常。
3. 若入口报 `429`，先看 `parse_inflight_full_total` 与 `too_many_inflight_jobs` 事件，而不是先怀疑 parser 回归。
4. 若租户报配额问题，先看 `parse_quota_exceeded_total` 与 `/v1/parse/quotas/usage`，确认是限额命中还是错误配置。
5. 若 OCR 样本异常，再看 `GET /v1/parse/events?event_type=ocr_failed` 与 `parse_ocr_failed_total`，最后才回到 benchmark JSON 对重样本做深挖。

## 建议执行顺序

1. 日常开发：快速本地门禁。
2. PR：CI 快速门禁。
3. 对外环境：显式开启 `runtime.api_key_env` 并完成鉴权验收。
4. 涉及 parser、OCR、layout、存储、索引：手动触发完整 CI。
5. 灰度前：跑 OCR benchmark、Postgres + pgvector smoke，并确认告警规则已接入。
6. 灰度中：持续观察 `/v1/parse/metrics`、`/v1/parse/events`、`/v1/parse/indexes/metrics` 和 `/v1/parse/prometheus`。
