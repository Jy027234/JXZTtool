# 首轮产品优化结果审计（历史快照，已被 H-01/H-02 工件取代）

> 当前权威结论和完整门禁在 `var/self-check/optimization-h02-result-audit.json` 与 `var/self-check/optimization-h02-result-audit.md`：`22/22` 通过，状态为 `passed_with_observation`，建议 `proceed_with_tail_monitoring`。
>
> 本文保留首轮趋势数据。其 `tracked_elapsed_*` 门禁已不再适用：`tracemalloc` 通道现在只用于 Python allocation 历史与尾部观察，真实发布 SLA 只读取纯延迟通道。H-02 的正式缓存语义为 `ocr_warm`（parse cache bypass、页面 OCR cache 显式观察）。

- 审计状态：**passed_with_observation**
- 发布建议：`proceed_with_tail_monitoring`
- 生成时间：`2026-07-15T03:26:53.720970+00:00`
- 固定样本：`D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf`
- 样本 SHA256：`AAA5F0A33F6BB716E407052842AB60F505B5E319CFB87B448A7249D28B678DCF`

## 首轮性能对比（历史）

| 测量通道 | 历史值 | 当前值 | 提升 | 稳定性 |
| --- | ---: | ---: | ---: | --- |
| 内存追踪耗时 | 148.144 s | 中位数 117.0 s | 21.023% | 极差 38.059 s，CV 11.784% |
| 纯延迟 | 29.168 s | 中位数 23.276 s | 20.2% | 极差 0.932 s，CV 1.446% |
| Python 峰值内存 | 775464.208 KB | 均值 729078.254 KB | 5.982% | 极差 2.234 KB |

## 首轮可靠性与质量（历史）

| 指标 | 历史值 | 当前值 |
| --- | ---: | ---: |
| 内容块 | 2035 | 2035 |
| 分块 | 2035 | 2035 |
| 物理页覆盖 | 254 | 297 |
| 表格 | 44 | 44 |
| 噪声率 | 0.0 | 0.0 |
| 审计占位项 | 0 | 43 |

- 结构确定性：`True`
- 内存追踪通道成功：`True`
- 纯延迟通道成功：`True`
- 当前质量指纹：`{"best_provider_id": "pdf-text", "blocks": 2078, "chunks": 2035, "figures": 411, "pages": 297, "primary_provider_id": "pdf-text", "quality_gate": "accept_with_warning", "quality_gate_flags": ["rag_chunks_not_embedded"], "quality_gate_passed": true, "result_status": "done", "tables": 44}`

## 首轮发布门禁（历史规则）

| 门禁 | 状态 | 证据 |
| --- | --- | --- |
| tracked_elapsed_vs_original | passed | median 117.0s vs original 148.144s |
| tracked_elapsed_vs_prior_stability | passed | median 117.0s vs prior median 135.035s |
| clean_latency_vs_historical | passed | median 23.276s vs historical 29.168s |
| python_peak_memory_vs_original | passed | mean 729078.254KB vs original 775464.208KB |
| clean_latency_stability | passed | runs=5 cv=1.446% range=0.932s |
| structural_determinism | passed | tracked and clean-latency run signatures are internally identical |
| content_quality_preserved | passed | content blocks/chunks/tables/figures preserved; physical-page coverage increased |
| self_check | passed | status=ok checks=4 |
| p1_contract_acceptance | passed | passed=8/8 payloads=24 |
| sample_identity | passed | all evidence references 36d65cd6b61346e28e97dbaf829646de.pdf |

## 首轮观察项（历史）

- `tracked_memory_instrumentation_tail_outlier` (observation): tracemalloc lane max 152.561s is more than 20% above median 117.0s; clean latency CV is 1.446%
- `non_blocking_quality_gate_flags` (observation): quality gate passed with flags: rag_chunks_not_embedded
- `physical_page_audit_artifacts` (information): 43 empty/non-extractable page artifacts preserve physical-page evidence and are excluded from content-quality denominators

## 首轮证据工件（历史）

- original_tracked: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-baseline-20260714.json`
- prior_tracked_stability: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-current-20260714-stability.json`
- current_tracked: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-current-20260715-r1.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-current-20260715-r2.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-current-20260715-r3.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-current-20260715-r4-post-gate-fix.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-current-20260715-r5-tail-confirm.json`
- historical_latency: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-baseline-before-page-completeness-20260715\baseline.json`
- current_latency: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-latency-20260715-r1.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-latency-20260715-r2.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-latency-20260715-r3.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-latency-20260715-r4.json`, `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-latency-20260715-r5.json`
- historical_regression: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-baseline-before-page-completeness-20260715\baseline.json`
- current_regression: `D:\个人文件\个人开发\解析管理中台\var\regression\baseline.json`
- self_check: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-self-check-20260715-r3.json`
- p1_acceptance: `D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-audit-p1-contract-20260715-r2.json`
