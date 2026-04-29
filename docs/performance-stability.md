# 性能与稳定性优化口径

## 分层门禁

当前质量门禁按反馈速度分三层：

1. 本地快速门禁：提交前运行单测和 runtime smoke。

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --skip-regression
```

2. PR / 主分支 CI：`.github/workflows/parsecore-ci.yml` 自动运行快速门禁，并额外验证 base SDK import 不会加载 API 可选依赖。

3. 定时或手动 CI：运行完整回归套件和 Postgres + pgvector smoke。OCR 长尾和存储层问题不阻塞日常 PR，但会在夜间或发版前暴露。

若要把 `perf` 从一次性手工执行升级成持续趋势，建议为夜间或手动 CI 准备一台带持久磁盘的 runner，并至少提供：

- `PARSECORE_REGRESSION_FIXTURE_ROOT`：perf 样本根目录
- `PARSECORE_PERF_HISTORY_DIR`：用于保存 `latest.perf.json` 的持久目录

可选项：

- `PARSECORE_PREVIOUS_PERF_REPORT`：显式指定上一份 perf 报告路径；若未配置，则 workflow 会优先读取 `PARSECORE_PERF_HISTORY_DIR/latest.perf.json`

## 上传保护

API 同步入口支持运行时文件大小保护：

```toml
[runtime]
max_upload_bytes = 52428800
```

覆盖入口：

- `POST /parse`
- `POST /v1/parse`
- `POST /parse/batch`
- `POST /v1/parse/batch`

超过限制时返回 `413 file_too_large`，响应会带 `actual_bytes / limit_bytes`，便于宿主系统记录和提示。设置为 `0` 表示关闭限制。

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

推荐约定：

```text
PARSECORE_REGRESSION_FIXTURE_ROOT=/mnt/parsecore-fixtures
PARSECORE_PERF_HISTORY_DIR=/var/lib/parsecore/perf-history
```

如果 runner 无持久磁盘，仍可通过手工注入 `PARSECORE_PREVIOUS_PERF_REPORT` 路径启用一次性的对比模式，但这种方式不适合作为长期趋势口径。

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
- `parse_ringbuffer_size`

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
