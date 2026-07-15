# P0 质量审计与性能门禁记录（2026-07-14）

本记录把 P0 的“只读质量审计”从一次性命令收口为可复现工件。审计不会修改 Provider 路由、生产任务或业务数据。

## 执行入口

固定样本清单：

- [p0-quality-samples.json](D:\个人文件\个人开发\解析管理中台\fixtures\provider-evaluation\p0-quality-samples.json)
- 样本根目录：`D:\app\uploads`
- 审计工具：[tools/p0_quality_audit.py](D:\个人文件\个人开发\解析管理中台\tools\p0_quality_audit.py)

执行命令：

```powershell
$env:PYTHONPATH = "src"
py -3 tools/p0_quality_audit.py `
  --config parsecore.toml `
  --manifest fixtures/provider-evaluation/p0-quality-samples.json `
  --sample-root D:\app\uploads `
  --out-dir var/self-check/p0-quality-audit-20260714 `
  --progress
```

RAG 链路可在不改生产配置的前提下用本地确定性 fake embedding 复核：

```powershell
py -3 tools/p0_quality_audit.py `
  --config parsecore.toml `
  --manifest fixtures/provider-evaluation/p0-quality-samples.json `
  --sample-root D:\app\uploads `
  --out-dir var/self-check/p0-quality-audit-20260714-fake `
  --embedding-provider fake `
  --progress
```

已补充可选的真实本地 Transformer embedding 验收通道；它只读取显式配置的本地模型，
不改默认 `parsecore.toml` 路由。当前复测使用 `sentence-transformers/all-MiniLM-L6-v2`
（384 维、CPU、local-files-only）：

```powershell
$env:PYTHONPATH = "src"
py -3 tools/p0_quality_audit.py `
  --config var/self-check/p0-local-embedding.toml `
  --manifest fixtures/provider-evaluation/p0-quality-samples.json `
  --sample-root D:\app\uploads `
  --out-dir var/self-check/p0-quality-audit-20260714-local-embedding `
  --embedding-provider configured `
  --progress --fail-on-errors
```

## 自动化结果

| 指标 | 结果 | 判定 |
| --- | ---: | --- |
| 固定样本 | 7 类 | 7/7 完成 |
| 页级审计记录 | 31 页 | 每个请求页都有记录 |
| chunk→block→page 追溯 | 746/746（最新 r4） | 100% |
| 缺口原因完整率 | 31/31 | 100% |
| 解析失败 | 0 | 通过 |
| fake embedding 链路 | 746/746 chunks fully traceable；7/7 样本的 embedding coverage 通过 | 本地链路通过，不代表真实 embedding/RAG 发布验收 |
| 真实本地 embedding 链路 | 746/746 chunks embedded；31 页；7/7 样本；traceability 100%；gate passed | 受控本地模型链路通过；不等同于远程 embedding 网关或线上 RAG 命中率验收 |
| Provider 对比样本 | 3 个 PDF × 1–5 页窗口 | `pdf-text`、`pymupdf4llm-local`、`docling-local` 共 9/9 完成；窗口 gate `accept_with_warning`，Docling 整文档长跑仍受吞吐/内存门禁约束 |
| 候选稳定性窗口 | 3 次 × 3 个 PDF 窗口 | `pymupdf4llm-local` 9/9 完成，结构/coverage/embedding 指纹三次一致；稳定性 gate=`accept_with_warning`，warning 仅来自候选与 `pdf-text` 的结构差异 |

详细工件：

- [summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714\summary.json)
- [coverage_report.jsonl](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714\coverage_report.jsonl)
- [p0-quality-audit-20260714-r3/summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714-r3\summary.json)：空白页诊断版，2 页明确为 `page_without_extractable_content`，并记录 effective embedding provider=`none`。
- [p0-quality-audit-20260714-fake-r2/summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714-fake-r2\summary.json)：fake embedding 复核，632/632 chunks embedded，除 2 个空白页外无 coverage gap。
- [p0-quality-audit-20260714-r4-fake/summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714-r4-fake\summary.json)：按当前代码重跑的最新 7 类真实样本审计，7/7 完成、31 页、1,919 blocks、746 chunks，gate 通过；746/746 chunks 可追溯到 block/page，7/7 样本 embedding coverage 通过，2 个无可提取内容页仍显式带 `page_without_extractable_content`。
- [p0-quality-audit-20260714-local-embedding/summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714-local-embedding\summary.json)：使用显式配置的真实本地 Transformer embedding 重跑；7/7 完成、31 页、1,919 blocks、746 chunks，746/746 embedded、746/746 可追溯，gate=`passed`；普通 PDF 第 4 页和多栏手册第 2 页仍按探针标记为 `page_without_extractable_content`。
- [optimization-current-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-current-20260714.json)：典型 297 页 PDF 当前版本复测，132.323 s、713,724 KB、2,035 blocks/chunks、44 tables、411 figures。
- [optimization-current-20260714-r3.json](D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-current-20260714-r3.json)：加入受控 Provider 参数透传后的默认路径复测，121.366 s、713,721 KB，质量计数与上轮完全一致。
- [optimization-current-20260714-stability.json](D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-current-20260714-stability.json)：同一典型 PDF 默认 `pdf-text` 路径连续 3 次复测；中位数 135.035 s、峰值内存均值 715,962.351 KB，三次均无错误且 blocks/chunks/tables/figures 均为 2,035/2,035/44/411。
- [provider-comparison-p0-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison-p0-20260714.json)
- [provider-comparison-candidates-pdf-venv-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison-candidates-pdf-venv-20260714.json)：`pymupdf4llm-local` 已完成 3/3，版本 `0.3.4`，平均耗时较 `pdf-text` 下降约 61.9%、平均峰值内存下降约 87.6%；每个样本均为实测 best provider，但当前仍保持 evaluate，未自动改路由。
- [provider-comparison-candidates-docx-venv-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison-candidates-docx-venv-20260714.json)：历史 DOCX 对照，记录当时 Docling 依赖缺失；当前依赖已安装，中文路径兼容由适配器处理。
- [provider-comparison.pages-1-5-r16.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison.pages-1-5-r16.json)：安装 Docling 2.113.0 后的 3 个真实 PDF 固定 1–5 页窗口，9/9 provider runs 完成；`docling-local` 结构输出可用但明显慢于基线/候选。
- [provider-docling-probe-r17-final.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-docling-probe-r17-final.json)：最新中文路径兼容回归探针，`pdf-text` 与 `docling-local` 均完成，0 failed。
- [docling-pipeline-profile-probe-r18.json](D:\个人文件\个人开发\解析管理中台\var\self-check\docling-pipeline-profile-probe-r18.json)：Docling `default`/`fast-text` 受控页段对照；快路径只保留为无表格/OCR 页候选，不能全局启用。
- [docling-reuse-probe-r19.json](D:\个人文件\个人开发\解析管理中台\var\self-check\docling-reuse-probe-r19.json)：同一 parser 的 Docling cold/warm 复用探针，10 页结构指纹一致；仅保留候选，不代表全量或并发准入。
- [provider-comparison-candidates-table-ocr-venv-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison-candidates-table-ocr-venv-20260714.json)：表格/OCR 各 10 页双 Provider 对照；`pymupdf4llm-local` 更快但表格计数不同，仍需人工 gold 校验。
- [p0-candidate-stability-gate-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-candidate-stability-gate-20260714.json)：新增稳定性门禁，基于同一组 3 个真实 PDF 的 1–5 页窗口连续 3 次双 Provider 报告；候选 9/9 完成，quality signature 稳定，覆盖率与 embedding 均为 100%，gate=`accept_with_warning`。
- [p0-candidate-stability-gate-20260714.md](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-candidate-stability-gate-20260714.md)：稳定性门禁的可读摘要；耗时只做波动观测，不把候选与基线的结构差异误判为稳定性失败。
- [provider-license-audit-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-license-audit-20260714.json)：许可证证据审计；`pdf-text` 为内置实现，`docling-local` 的直接包元数据为 MIT，`pymupdf4llm-local` 观察到 AGPL/商业双授权仍需合规决策，`mineru-local` 未安装；审计只收集证据，不自动改路由。
- [p0-self-check-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714.json)
- [p0-self-check-20260714-r18.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r18.json)：最新快速 self-check 通过，regression suite 3/3；内置 unit test 摘要 `544 passed, skipped=5`。
- [p0-self-check-20260714-r19.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r19.json)：新增 Docling converter provenance 后的最新快速 self-check，状态 `ok`，regression suite 3/3；内置 unit test 摘要 `544 passed, skipped=5`。
- [p0-self-check-20260714-r23.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r23.json)：解析缓存 cold/warm telemetry 回归的历史 self-check 工件，状态 `ok`，regression suite 3/3；内置 unit test 摘要 `548 passed, skipped=5`，默认 `reuse_parser_instances=false`。当前最新工件见 r24。
- [p0-self-check-20260714-r24.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r24.json)：加入稳定性/许可证门禁工具后的最新快速 self-check，状态 `ok`，unit test 摘要 `548 passed, skipped=5`、payload 6/6、regression suite 3/3；随后独立全量 pytest 为 `564 passed, 5 skipped, 51 subtests passed`。
- [p0-self-check-20260714-r14.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r14.json)：包含 Provider suite 的 fast self-check 历史工件；单测、payload、runtime 和 regression 均通过，`pdf-text` 与 `pymupdf4llm-local` 各 3/3 完成；当时唯一失败是 Docling 依赖未安装，未改变默认 route。
- [gold-review-queue-v1.json](D:\个人文件\个人开发\解析管理中台\fixtures\provider-evaluation\gold-review-queue-v1.json)：50/50 页面已由 `Codex (AI-assisted, user-authorized)` 完成批准，覆盖 5 个 PDF；该批准是用户明确授权的 AI 辅助核验，不冒充独立人工 gold。
- [gold evidence manifest](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\manifest.json)：已渲染 50/50 个源页面，包含 PNG、pypdf 文本探针和 SHA-256；当前 `approved=50 / pending=0 / rejected=0`。
- [RISK_REVIEW.md](D:\个人文件\个人开发\解析管理中台\output\pdf\provider-gold-review-20260714\RISK_REVIEW.md)：把已批准样本的前 20 个高风险页直接链接到 PNG/文本证据，作为只读复核索引。
- [provider-gold-review-status-20260714-ai-r2.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-review-status-20260714-ai-r2.json)：只读审核状态校验为 `status=ready, 50 approved / 0 pending / 0 rejected / 0 errors`；不会自动修改队列。
- [provider-gold-ai-review-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-review-20260714.json)：AI 辅助审核审计，记录 50 页证据哈希、基线交叉检查和 8 个视觉 spot-check；明确标注 `scope=ai_assisted_review_not_human_gold`。
- [provider-gold-pending-full-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-pending-full-20260714.json)：50 个待审核页面完成基线/候选双跑（100 provider runs，99 完成）；候选 50/50 完成，整体平均耗时 1.132 s，基线完成页平均 3.096 s，但因 approved gold=0、许可证未审批和基线缺页，准入建议仍为 `remain_shadow_only`，没有改写 route。
- [provider-gold-ai-approved-20260714-r2.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-approved-20260714-r2.json)：关闭默认 `tracemalloc` 性能扰动后，在 50/50 AI 辅助批准页面上重跑的基线/候选评估；`pdf-text` 49/50 scored、平均 2.027 s，候选 50/50 完成、平均 0.272 s、p95 0.561 s、最大 5.818 s。候选仍因许可证、结构差异和普通 PDF 第 165 页长尾保持 `remain_shadow_only`。
- [provider-gold-current-20260714-r3.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-current-20260714-r3.json)：按当前 `.venv` 和当前代码重跑 50 页双 Provider；100 次运行中 99 次完成，候选 50/50 完成但因许可证 hard veto 未进入评分，基线 49/50，准入建议仍为 `remain_shadow_only`。
- [p0-candidate-gold-stability-gate-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-candidate-gold-stability-gate-20260714.json)：基于当前代码的 3 次 50 页双 Provider 复评；候选 150/150 次运行完成、50 页 quality signature 三次一致，gate=`accept_with_warning`，warning 仅来自候选与基线的结构差异。当前 r3 对照中 50/50 页的 block/table/figure 计数均不同，候选耗时均值约 0.269 s、p95 约 0.547 s、最大 5.636 s；这证明性能优势和运行稳定性，但不证明质量等价。
- [provider-gold-tuned-ignore-graphics-ordinary-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-tuned-ignore-graphics-ordinary-20260714.json)：临时 `ignore_graphics=true` 对照；候选普通 PDF 平均 0.276 s、最大 1.837 s，但表格数 `3 → 0`，仅作为被否决的调参证据，不进入默认配置。
- [provider-gold-pending-ordinary-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-pending-ordinary-20260714.json)：普通 PDF 10 页子集明细，可定位第 165 页等长尾页。
- `tools/_embedding_smoke.py --config parsecore.toml --require-live` 仍会因默认配置未启用远程 embedding 而返回 `status=skipped`；针对显式本地模型配置的 smoke 已通过：5/5 chunks embedded、384 维、向量范数均值 1.0，并完成语义 search 命中。远程 embedding 网关和线上 RAG 命中率仍需真实凭证/服务环境另行验收。

历史 7 类样本审计曾识别到 2 个请求页没有自然产生 block：普通 PDF 第 4 页、多栏手册第 2 页。pypdf 页探针确认两页均无可提取文本和图片，现标为 `page_without_extractable_content`，不再误报为 parser 未输出；若探针无法确认，工具仍保留 `parser_page_not_emitted` 信号。全量上传目录审计的最新数字见第二十六轮。

默认审计仍使用空 embedding provider，因此生产默认路径不会因为本次验收而隐式联网或改变模型。fake 与真实本地模型两条受控审计均显示解析→chunk→embedding 的 coverage/追溯闭环；真实本地模型结果不能替代远程 embedding 网关和线上 RAG 命中质量验收。表格样本还暴露了 `table_empty_ratio_high`、`table_col_count_changed`、`column_shift_suspected`，扫描样本暴露了 OCR attempted/failed，均已进入页级 JSONL。

## 超大 PDF 计划门禁

对 17,101 页合成 PDF 的分片计划：343 个分片、0 错误、计划耗时 5.44 秒；关闭分片执行时，`snapshot_blocks_min` 明确标记为 skipped，不冒充内容执行验收。此前提前生成 343 个分片文件的计划耗时为 10.926 秒；延迟到执行时物化后下降约 50.2%。

统一 self-check 已复现该门禁（最新不含 Provider suite 的 [p0-self-check-20260714-r25.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r25.json)：`549 tests passed, skipped=5`，fast regression suite 3/3）；随后独立全量 pytest 回归为 `565 passed, 5 skipped, 51 subtests passed`。此前旧 regression baseline 的 1,624 blocks 已与当前 2,035 blocks 质量指纹同步，旧文件备份在 [regression-baseline-before-refresh-20260714](D:\个人文件\个人开发\解析管理中台\var\self-check\regression-baseline-before-refresh-20260714)。

随后在生产配置下完成 7 份非重叠受控报告，覆盖第 1–343 号分片：各批均为 0 错误，最后一批覆盖第 15,001–17,101 页。聚合门禁确认 17,101/17,101 页、343/343 个分片、17,444 个 block、17,444 个 chunk，页段无缺口、无重叠。各批使用延迟物化、串行有界执行，未将并发实验结果混入门禁。详细单批工件和聚合工件：[p0-large-pdf-stress-full-coverage-20260714.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-large-pdf-stress-full-coverage-20260714.json)、[p0-large-pdf-stress-full-coverage-20260714.md](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-large-pdf-stress-full-coverage-20260714.md)。压力工具现支持 `--part-start`，可以按页段继续执行；该聚合结果证明合成大文件分片内容已全覆盖，但不替代真实样本、gold 和 Provider 发布验收。

## 第二十六轮：真实上传目录全量审计（2026-07-15，已完成）

针对 `D:\app\uploads` 去重后形成 28 份真实源文档清单（21 PDF、4 DOCX、2 XLSX、1 XLS；排除生成目录和 17,101 页合成压力文件）：

- 审计工件：[p0-upload-full-audit-20260715-local-embedding/summary.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-upload-full-audit-20260715-local-embedding\summary.json)
- 断点续跑入口：[tools/p0_quality_audit.py](D:\个人文件\个人开发\解析管理中台\tools\p0_quality_audit.py)，支持 `--sample-id`、`--resume`、`--rerun-sample-id`；样本 profile 变更或显式重跑会自动刷新旧结果。
- 28/28 份完成；请求页 2,131 页，coverage 页 2,139 页（含 8 个非 PDF 逻辑页），19,394 blocks、18,218 chunks；本地 Transformer embedding 与 chunk→block→page 追溯均为 100%。
- 64 个 PDF 页被探针确认为无可提取文本/图片（`text_chars=0` 且 `image_count=0`），均保留 `page_without_extractable_content` 页级记录；视觉证据包显示其中 63 页为纯白页、1 页仅有两条矢量横线；此前跨页合并造成的 386 个 `parser_page_not_emitted` 已归零。
- [空白页视觉证据包](D:\个人文件\个人开发\解析管理中台\output\pdf\p0-empty-page-evidence-20260715\manifest.json)：64/64 页成功以 30 DPI 渲染，63 页 `visual_blank=true`，无渲染错误。
- 两个低清扫描样本的 OCR 空文本页不再触发文档级失败：解析结果保留不可索引页工件，并写出 `ocr_attempted / ocr_failed / empty_page` 信号，避免页级证据丢失。
- 全量审计 gate=`passed`；但该 gate 只证明每个请求页都有 coverage 记录和完整缺口原因，不代表 64 个空白页具备可检索正文。
- 最新 fast self-check：[p0-self-check-20260715-r26.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260715-r26.json)，状态 `ok`，unit `550 passed / 5 skipped`、payload `6/6`、regression `3/3`；加入本地 RAG acceptance 与 readiness scope 单测后的独立全量 pytest 为 `582 passed, 5 skipped, 51 subtests passed`。

## 第二十七轮：空页显式保留与候选连续稳定性（2026-07-15，已完成）

- 空页占位符修复后的同一全量审计工件仍为 28/28 完成，coverage 页 2,139，blocks/chunks 更新为 19,458/18,218，`missing_page_count=0`；此前 64 个真实上传目录缺页现在均有显式 `empty_page`、不可索引 block 和完整页级 coverage，不再依赖“缺页补行”。
- 三份受影响上传 PDF 的 64 页仍由源文件探针确认 `text_chars=0` 且 `image_count=0`；新证据包 [p0-empty-page-evidence-20260715-r2/manifest.json](D:\个人文件\个人开发\解析管理中台\output\pdf\p0-empty-page-evidence-20260715-r2\manifest.json) 共渲染 66 个显式空页（其中 64 个来自这三份上传 PDF，另含 2 个低清 OCR fixture），渲染错误 0；64 个上传页中 63 页纯白、1 页仅有矢量横线。
- 候选 Provider 最新 r7/r8/r9 均为基线/候选 50/50 完成、无 provider failure；[p0-candidate-gold-stability-gate-20260715-r2.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-candidate-gold-stability-gate-20260715-r2.json) 显示连续 3 次 `quality_signature_stable=true`，门禁 `accept_with_warning`。普通 PDF 第 100 页基线现已完成；候选仍因许可证和结构差异保持 shadow/evaluate，不自动切换 route。
- 主要工件：[provider-gold-current-20260715-r7.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-current-20260715-r7.json)、[provider-gold-current-20260715-r8.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-current-20260715-r8.json)、[provider-gold-current-20260715-r9.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-current-20260715-r9.json)。
- 按用户授权完成的空页 AI-assisted disposition：[p0-empty-page-review-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-empty-page-review-20260715.json) 为 64/64 `approved_non_indexable`（63 纯白、1 矢量近空白、0 pending）；该结果关闭当前验收流中的空页技术缺口，但仍明确标注不是独立业务签字。
- 机器可读的收口判断见 [p0-release-readiness-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-release-readiness-20260715.json)：本地闭环 4/4（含真实 HTTP OpenAI-compatible gateway transport smoke），必需外部 blocker 2 个（候选许可证/商业授权、远程 embedding/RAG live），可选治理项 1 个（独立 named-human gold），默认 route 未改变。兼容网关工件：[embedding-compat-gateway-smoke-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\embedding-compat-gateway-smoke-20260715.json)。

## 第二十八轮：候选全 Gold 复核与 P0 scope readiness（2026-07-15，已完成）

- `docling-local` 已按同一 50 页 Gold 完成全量双 Provider 评估：[provider-gold-docling-20260715-r1.json](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-docling-20260715-r1.json)。50/50 页均完成，但候选平均耗时 `11.444 s`、p95 `19.914 s`、最大 `234.983 s`；平均 Gold 分数仅 `53.021`（基线 `pdf-text=98.5`），39/50 页出现 critical token 缺失，故不能作为 AGPL 候选的合规替代，也不进入 route。
- 新增真实本地 Transformer→index manifest→semantic search 端到端验收：[local-rag-acceptance-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\local-rag-acceptance-20260715.json)。6/6 chunks embedded、384 维、manifest coverage `1.0`、4/4 查询命中 `hit@3=1.0`、MRR=`1.0`；该工件明确标记为 self-hosted local，不冒充远程生产网关。
- readiness 现区分 `p0-core` 与 `production` scope。[p0-core-readiness-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-core-readiness-20260715.json) 的本地闭环为 5/5，`release_ready=true`；候选 route promotion 与远程 embedding/RAG 仍作为 production scope 的 2 个非本地外部闭环保留在 [p0-release-readiness-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-release-readiness-20260715.json)，不再与默认 `pdf-text` P0 路径混为一谈。

## P0 总体状态

**P0 默认路由的开发与本地验收已完成；`p0-core` readiness 已通过。另有 2 个生产扩展闭环（候选 route promotion、远程 embedding/RAG）和 1 个按治理要求决定的可选项目。**

尚未闭环的生产扩展项（不阻塞 `p0-core` 默认 `pdf-text` 路径）：

1. **候选 Provider route promotion**：`pymupdf4llm-local` 已完成 3 次 × 50 页稳定运行，但 Gold 结构差异和 AGPL/商业双授权仍禁止切换 route；`docling-local` 的 50 页全 Gold 已证明平均质量/性能不达准入（见第二十八轮）。`mineru-local / paddleocr-local` 仍未安装或处于 skipped/evaluate。
2. **远程 embedding/RAG 线上验收**：本地 Transformer 已完成真实端到端的 6/6 chunks、coverage `1.0`、hit@3 `1.0`，全量上传审计也完成 18,218/18,218 chunks embedding 和 100% 追溯；仍缺真实 `PARSECORE_EMBEDDING_API_KEY`、可用远程网关、线上索引写入、召回/重排以及真实业务命中率证据。默认配置的 `--require-live` smoke 不能在缺凭证时通过，合成 17,101 页压力覆盖也不能替代线上业务验收。

**可选治理项（不计入上述 2 个生产扩展闭环）**：若组织要求独立 named-human gold，仍需对 50 页进行人工签字；当前批准是用户授权的 AI-assisted review，不能替代独立人工 gold。64 个空页已经按用户授权完成 AI-assisted disposition。除此之外，P0 默认路由开发项已没有待实现代码任务。
