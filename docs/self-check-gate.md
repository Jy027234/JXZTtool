# ParseCore 自检门禁

## 目的

当前默认质量门禁已经完全收口到 ParseCore 自身验证。

这套门禁用于回答四个问题：

1. 代码改动后，基础可靠性是否仍然成立。
2. 默认运行时是否还能正常装配。
3. 已冻结的 payload contract 是否仍和真实输出一致。
4. 解析质量和长尾性能是否出现明显退化。

## 默认入口

极快 smoke：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --skip-regression
```

默认 fast 自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check
```

slow/full 专项自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --profile slow
```

perf 长尾性能跟踪：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --profile perf
```

如需和上一份 perf 报告做趋势对比：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli self-check --profile perf --compare-report var/self-check/previous.perf.json
```

说明：默认入口是 `parsecore self-check`（源码 checkout 下可用 `python -m parsecore.cli self-check`），内部仍复用 `tools/self_check.py`。该入口支持三档门禁。默认 `fast` profile 会跑 `var/regression/suite.fast.json`，只覆盖日常主线 baseline；`slow`（等价别名 `full`）会切到 `var/regression/suite.full.json`，保留主线加中等时长 slow baseline；`perf` 会切到 `var/regression/suite.perf.json`，单独跟踪 `sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 两个重样本。对应回归超时默认值分别为 `900s`、`4200s` 和 `4200s`。此外，`fast`/`full`/`perf` profile 现在都会优先尝试自动带上各自的 `var/regression/provider-suite.*.json` 本地 Provider 对比门禁；如果 suite 或样本缺失，则会以 `provider_comparison_suite.status = "skipped"` 的方式安全降级，而不是把“环境里没有样本”误报成代码回归。

如需单独校验冻结契约，可直接执行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli payload-contract-check
```

`perf` profile 现在会在输出 JSON 中额外写入 `perf_tracking.samples / overview / comparison`。当 `--compare-report` 指向上一份 self-check JSON 时，会按样本生成 `elapsed_s / ocr_total_s / call_s / provider_s / rec_s / max_page_ocr_s` 的 delta，便于 CI 或自托管 runner 长期跟踪趋势。

在 GitHub Actions 的 `performance` job 中，若 runner 提供 `PARSECORE_PERF_HISTORY_DIR`，workflow 会自动读取其中的 `latest.perf.json` 作为 compare report，并在本次执行后把新报告覆盖写回该目录。

输出会同时打印到终端。默认 `fast` profile 写入 [var/self-check/latest.json](../var/self-check/latest.json)，`slow/full` 默认写入 [var/self-check/latest.full.json](../var/self-check/latest.full.json)，`perf` 默认写入 [var/self-check/latest.perf.json](../var/self-check/latest.perf.json)；显式传 `--out` 时仍以手工路径为准。若 provider comparison 实际执行，self-check 还会在同目录额外落盘 `provider-comparison.fast/full/perf.json/.md`，并把这些路径写回 self-check JSON 的 `provider_comparison_artifacts` 与 `checks[].details.report_json/report_markdown`，便于后续排障和复盘。当前 provider comparison 结果还会额外输出 `provider_identity_summary`，收口每个 provider id 的 `provider_version / adapter_version / status_counts`；同时新增 `provider_admission_summary`，把 suite 结果直接翻译成每个 provider 的 `recommended_admission.route_mode / gate_status / route_ready`、`recommended_action`、`requires_config_update`、`drift_fields` 和 `config_patch`。self-check 会把这两份摘要分别写进 `checks[].details.provider_identity_summary` 和 `checks[].details.provider_admission_summary`，并在终端 summary 中追加 `identity_drift / admission_ready / admission_update`，用于提示“同一个 provider 名称是否混入多版实现”“有哪些候选已具备进入 route 的条件”“哪些现有配置建议回写”。如果 provider suite 配置了 `max_providers_requiring_config_update`、`max_providers_with_route_mode_drift`、`max_providers_with_gate_status_drift`、`max_providers_with_gate_checks_drift`、`max_providers_with_route_ready_drift` 这五类 admission drift 预算，self-check 会沿用 provider comparison 的 fail/warn 结果，把“准入配置还没收敛”直接升级成标准门禁信号。

Provider comparison 工件不在 self-check 过程中自动删除。运维清理使用 `parsecore cleanup-provider-comparison-artifacts`，默认只输出 dry-run 清单；它只匹配 `provider-comparison.<profile>.json/.md`，不会匹配 self-check 主报告或其他审计工件。实际删除前必须审核清单并显式传入 `--execute`，具体保留期和命令见 [配置手册](configuration.md#runtime-配置)。

CI 说明：现有 GitHub Actions 已接到 `fast/full/perf` 三档入口。baseline 现在同时保留历史绝对路径和 `fixture_relative_path` 元数据；若在其他机器或 hosted runner 上提供 `PARSECORE_REGRESSION_FIXTURE_ROOT` 指向样本目录，workflow 会优先用相对路径恢复这批样本。若仍缺样本，workflow 才会把 fast 降级为 smoke-only，并跳过 full/perf 专项门禁，而不是直接失败。

## 覆盖范围

`parsecore self-check` 当前串联四类检查：

1. 单测：`unittest discover -s tests -p "test_*.py"`
2. 运行时 smoke：`parsecore.cli describe --config parsecore.toml`
3. Payload contract 校验：`parsecore.cli payload-contract-check`
4. 回归基线：
	- `fast`：`tools/regression_baseline.py check-suite --suite var/regression/suite.fast.json`
	- `slow/full`：`tools/regression_baseline.py check-suite --suite var/regression/suite.full.json`
	- `perf`：`tools/regression_baseline.py check-suite --suite var/regression/suite.perf.json`

默认 provider suite：

	- `fast`：`var/regression/provider-suite.fast.json`
	- `slow/full`：`var/regression/provider-suite.full.json`
	- `perf`：`var/regression/provider-suite.perf.json`

如需显式跑本地 Provider 发布门禁，可额外执行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/provider-suite.fast.json --fixture-root D:/app/uploads --progress
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/provider-suite.full.json --fixture-root D:/app/uploads --progress
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/provider-suite.perf.json --fixture-root D:/app/uploads --progress
```

`--progress` 会把每个样本的开始和结束信息写到 stderr，不改变 JSON stdout；`self-check` 调用 provider comparison 时会默认启用该进度输出，避免长样本门禁看起来像无响应。

候选 Provider 的“连续 3 次稳定运行”可在同一组真实窗口上单独收口。先用相同的 `--sample / --page-start / --page-end` 生成三份双 Provider 报告，再运行稳定性门禁：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/provider_stability_gate.py --provider pymupdf4llm-local --baseline-provider pdf-text --report var/self-check/p0-candidate-stable-comparison-run-1.json --report var/self-check/p0-candidate-stable-comparison-run-2.json --report var/self-check/p0-candidate-stable-comparison-run-3.json --out-json var/self-check/p0-candidate-stability-gate-20260714.json --out-md var/self-check/p0-candidate-stability-gate-20260714.md
```

该门禁只锁定候选的 blocks/chunks/tables、页覆盖、chunk/embedding 和 RAG quality signature；候选与基线的结构差异会作为 warning 保留，不能被误当作“稳定通过后自动切换路由”。

`provider-suite.fast.json` 当前镜像 `suite.fast.json` 的三条主线 baseline；`provider-suite.full.json` 镜像 `suite.full.json` 的五个主线 PDF baseline；两者都先用 `max_samples_best_provider_differs_from_route_primary = 0` 约束 route-plan 主候选与实测最佳 Provider 的偏差不要被放大，同时把 `max_providers_with_multiple_provider_versions = 0`、`max_providers_with_multiple_adapter_versions = 0` 以及五类 admission drift 预算固定为默认值 `0`，避免“同一个 Provider 名称实际切到另一版上游库/Adapter”或“suite 建议已变化但主配置还没收敛”悄悄混进灰度结果。`provider-suite.perf.json` 则继续收口 `sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 两个 provider 对比重样本，并同样约束 identity drift 与 admission drift。现阶段已支持十类 gate policy 预算：`max_provider_reading_order_warning_runs`、`max_provider_quality_warning_runs`、`max_samples_best_provider_differs_from_route_primary`、`max_providers_with_multiple_provider_versions`、`max_providers_with_multiple_adapter_versions`，以及 `max_providers_requiring_config_update`、`max_providers_with_route_mode_drift`、`max_providers_with_gate_status_drift`、`max_providers_with_gate_checks_drift`、`max_providers_with_route_ready_drift`。前五类用于约束读序 warning、Provider 质量 warning、route primary 与实测 best provider 的偏差，以及 Provider 身份漂移是否被放大；后五类用于约束 admission 配置回写数量和 `route_mode / gate_status / gate_checks / route_ready` 的收敛状态。

### Fast Provider 页数预算与超时清理

日常 fast lane 不再重复解析三次 297 页样本。`var/regression/provider-suite.fast.json` 声明 `fast_page_budget`，其中 `max_pages_per_sample`、`max_total_pages` 和 `large_pdf_min_page_count` 都是必填的正整数。fast 预检会读取 PDF 物理页数，并把原文页数、声明的 `page_range`、实际选择页数和总预算写入 `checks[].details.preflight.page_budget`：达到 `large_pdf_min_page_count` 的 PDF 必须有 `page_range`，任何单样本或总页数超预算都会使 Provider check 失败，而不是悄悄跳过或回退为整文档解析。

当前 fast 套件覆盖 7 页正文、表格、跨页页眉/页脚及空白页证据窗口，执行默认 `pdf-text` 与 `pymupdf4llm-local` shadow 对比；Docling 等重型候选仍保留在 full/perf lane。Provider 质量分相同时，排名会先保持当前 route primary、再比较 elapsed time，避免一次性微小耗时波动把 shadow Provider 误建议为默认路由；质量分真正更高的候选仍会作为 route mismatch 进入门禁。Provider 子进程超时后，Windows 会使用 `taskkill.exe /PID <pid> /T /F` 清理进程树，并把清理和输出 drain 结果写入 check JSON；若树清理失败，再记录 root-process fallback kill。

## 退出码语义

1. `0`：全部通过
2. `1`：至少一项必需检查失败
3. `2`：没有硬失败，但存在退化或超时

## Provider Comparison Check

### 启用方式

```powershell
# 显式指定 suite
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m tools.self_check --provider-suite var/regression/provider-suite.fast.json

# 自动发现（suite 文件放在 var/regression/ 下）
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m tools.self_check

# 候选 warm-state 测量（仅 Provider 对比，不改变默认路由）
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m tools.self_check --provider-suite var/regression/provider-suite.fast.json --reuse-parser-instances
```

### 输出位置

- JSON: `var/self-check/provider-comparison.{profile}.json`
- Markdown: `var/self-check/provider-comparison.{profile}.md`

`self_check` JSON 中的标准化字段：

| 字段 | 说明 |
|------|------|
| `checks[].details.name` | `provider_comparison_suite` |
| `checks[].details.status` | `ok` / `degraded` / `failed` / `timeout` |
| `checks[].details.sample_count` | suite 样本数 |
| `checks[].details.output_json_path` | provider comparison JSON 工件路径 |
| `checks[].details.output_md_path` | provider comparison Markdown 工件路径 |
| `checks[].details.summary` | 样本和 provider 统计摘要 |
| `checks[].details.gate_summary` | gate 判定结果和 warning 计数 |
| `checks[].details.reuse_parser_instances` | 是否启用候选 parser 实例复用 |

`--reuse-parser-instances` 仅用于同一 suite 内的 warm-state probe；报告会把
`measurement.parser_lifecycle` 标为 `provider_instance_reused`，默认不传该参数时仍为
`new_per_run` 冷启动口径。两种测量口径不能直接混合成同一性能基线。

### 判定规则

- `status=ok`：所有样本通过 gate policy
- `status=degraded`：有样本超时或降级
- `status=failed`：有样本硬失败或 gate policy 不通过

## 2026-05-10 当前结论

### 可靠性

- 本轮交付前全量回归通过：`322 passed, 5 skipped`。
- `git diff --check` 通过。
- records/document views/导出/大 PDF part 相关主链路已补充定向测试。
- 真实 17,101 页 PDF 样本已完成产物抽检：`pages=17,101`、`lines=1,249,000`、`records=454,985`，SQLite 查询可用。
- 当前版本可作为 `0.1.0 可交付灰度版` 进入受控生产灰度。

### 继续观察

- OCR 重样本与复杂跨页表格仍按专项性能窗口跟踪。
- records 同步 HTTP 导出后续可继续 streaming 化。
- Parquet、异常页截图包、raw cells trace 包属于后续增强，不阻塞当前交付。

## 2026-04-28 历史结论

### 可靠性

- 快速自检已通过。
- 本轮等价验证已通过：全量 `unittest discover -s tests` 通过，`baseline.json` 与 `baseline.table-structure.primary.json` 的真实 `check` 均通过，并已将 layout/table 两类专项继续收口到同一回归工具路径。
- 单测结果：`210 passed, 5 skipped`。
- 性能与稳定性补强：已新增分层 CI、同步上传大小保护和 OCR benchmark 专项工具。
- 布局专项新增混排页顺序校验：正文与表格块不再按“先表后文”固定顺序输出，已改为按页面垂直锚点交错输出并由 `tests.test_pdf_parser_options` 覆盖。
- 布局专项新增图注邻接校验：当 `Figure/Fig./Illustration` 标签被单独切段时，parser 会将其与下一段 caption 说明自动合并，已由 `tests.test_pdf_parser_figure_caption` 覆盖。
- 结构索引准备项已收口：artifact typed item 已显式携带 `semantic_role` 与 `structure_tags`，并把 `semantic_role` 写入 provenance；同时 pipeline 级可观测信息已补齐 `pipeline_name/options_hash/cache_hit/cache_miss/active_stages` 并由 `tests.test_runtime` 覆盖。
- Phase 7 已启动第一批：runtime/document API 现会返回 `index_manifest`，且 index adapter 已开始同时接收主索引 chunk 与结构索引 typed item 的写入骨架。
- Phase 7 本轮新增：结构检索、任务级检索、索引层聚合指标与 `parsecore batch-reindex` 批处理入口均已落地；embedding tier 也已显式写入 manifest，默认仍仅启用 `small`。
- Phase 7 继续推进：`high_precision` 层已从预留态转为可执行层（启用 `small+large` tier 时写入 manifest，并支持 `search` 接口 `index_layer=high_precision` 过滤检索）。
- Phase 7 本轮再推进：`high_precision` 检索已优先走 index adapter 的独立层读取，不再仅依赖 runtime 内存筛选；manifest 会携带 `chunk_ids` 供索引层精确持久化与回放。
- Phase 7 观测补齐：`/v1/parse/indexes/metrics` 已新增 `high_precision` 汇总指标（documents/document_coverage/items/item_ratio_vs_primary），便于持续评估高精度层覆盖与成本。
- Phase 7 效果观测补齐：`/v1/parse/indexes/metrics` 现同时返回按索引层聚合的 `search_effectiveness` 与 `high_precision.query_*` 指标，可直接观察高精度层查询命中率与平均命中量。
- Phase 7 可观测增强：检索效果指标已改为持久化聚合（JobStore），不再只依赖进程内状态；runtime 重启后 `search_effectiveness` 与 `high_precision.query_*` 仍可延续读取。
- Phase 7 可观测再增强：`/v1/parse/indexes/metrics` 现增加 `search_effectiveness_trends`（`1h/6h/24h`），支持直接观察各索引层查询效果的短周期趋势。
- Phase 7 可观测再收口：趋势窗口不再写死，`/v1/parse/indexes/metrics` 现可通过 `trend_window_hours` 自定义桶（支持多值），并对非法值返回 `invalid_trend_window_hours`。
- 单测耗时约 `9.7s`。
- 默认 runtime describe 正常，当前形态为 `index_mode = hybrid`、`execution_mode = inline`。

### 解析质量

fast profile 当前覆盖以下基线样本：

1. `primary-default`
2. `primary-table-structure`
3. `primary-strip-hf`

`sample-flight-ops-manual-r2` 与 `sample-25-51-06` 现留给 `slow/full` 专项窗口；`sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 已拆到独立 `perf` 窗口，不计入默认 fast 门禁失败。

`sample-27-81-17` 当前同时承担复杂版面阅读顺序专项样本职责：`tools/regression_baseline.py` 已为其补上 `layout_quality` 指标与 drift 门槛，且 `baseline.27-81-17.json` 已重存为原生携带 `layout_quality` 的专项 baseline；最近一次单独 `check` 结果为 `multi_col=2 / layout_ro_pages=2 / OK`。它现转入 `perf` 窗口，避免继续把布局/OCR 重样本压在 `full` 门禁内。

2026-04-27 最近一次全量自检结果：

1. `regression_suite` 已新增 `primary-table-structure` 表格专项门禁
2. 默认 suite 现保留 `primary-default`、`primary-table-structure`、`primary-strip-hf` 与 `sample-flight-ops-manual-r2` 作为日常门禁
3. `sample-25-51-06` 继续保留在 `slow/full` 窗口；`sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 已拆到独立 `perf` 窗口，继续沿用 baseline/tooling 口径
4. 布局/OCR 重样本现通过 `tools/regression_baseline.py check-suite --suite var/regression/suite.perf.json` 单独纳入性能跟踪窗口观察

### 性能风险

- 当前最明显的长尾风险集中在 `sample-cmm-32-48-21-ocr`。
- 该 OCR 样本已纳入默认回归套件，但本轮 600s 观察窗口内尚未收口，仍应按 OCR 重样本性能专项持续跟踪。
- 这说明常规样本和新的表格专项样本都已具备门禁能力，但 OCR 长尾压缩本身仍应视为专项优化，而不是这轮表格门禁工作的回归失败。

## 当前建议

1. 日常改动默认跑 `parsecore self-check` 的 fast profile。
2. 涉及 PDF 后处理、长文样本和门禁口径调整时跑 `parsecore self-check --profile slow`。
3. 涉及 `sample-27-81-17`、`sample-cmm-32-48-21-ocr` 这类布局/OCR 重样本性能跟踪时跑 `parsecore self-check --profile perf`。
4. 若 `slow/full` 或 `perf` 返回 `degraded`，优先检查是否再次触发超时边界或 OCR 长尾异常放大。
5. 若要继续收敛长尾性能，应把 OCR/复杂版面重样本优化视为专项任务，而不是继续改变默认上线口径。
