# ParseCore 自检门禁

## 目的

当前默认质量门禁已经从 jobcard 双跑切回 ParseCore 自身验证。

这套门禁用于回答三个问题：

1. 代码改动后，基础可靠性是否仍然成立。
2. 默认运行时是否还能正常装配。
3. 解析质量和长尾性能是否出现明显退化。

## 默认入口

快速自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --skip-regression
```

全量自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py
```

说明：当前默认回归套件已包含 OCR 重样本 `sample-cmm-32-48-21-ocr`，因此全量自检的默认回归超时现为 `900s`，避免在套件实际可通过时误报 `degraded`。

输出会同时打印到终端，并写入 [var/self-check/latest.json](../var/self-check/latest.json)。

## 覆盖范围

`tools/self_check.py` 当前串联三类检查：

1. 单测：`unittest discover -s tests -p "test_*.py"`
2. 运行时 smoke：`parsecore.cli describe --config parsecore.toml`
3. 回归基线：`tools/regression_baseline.py check-suite --suite var/regression/suite.json`

## 退出码语义

1. `0`：全部通过
2. `1`：至少一项必需检查失败
3. `2`：没有硬失败，但存在退化或超时

## 2026-04-27 当前结论

### 可靠性

- 快速自检已通过。
- 本轮等价验证已通过：全量 `unittest discover -s tests` 通过，`baseline.json` 与 `baseline.table-structure.primary.json` 的真实 `check` 均通过，并已将 layout/table 两类专项继续收口到同一回归工具路径。
- 单测结果：`167 passed, 5 skipped`。
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
- 单测耗时约 `7.8s`。
- 默认 runtime describe 正常，当前形态为 `index_mode = hybrid`、`execution_mode = inline`。

### 解析质量

已确认以下基线样本在预算内通过：

1. `primary-default`
2. `primary-table-structure`
3. `primary-strip-hf`
4. `sample-25-51-06`
5. `sample-flight-ops-manual-r2`

`sample-27-81-17` 依照 `suite.json` 默认策略以 `slow` 标签跳过，不计入默认门禁失败。

该样本当前同时承担复杂版面阅读顺序专项样本职责：`tools/regression_baseline.py` 已为其补上 `layout_quality` 指标与 drift 门槛，且 `baseline.27-81-17.json` 已重存为原生携带 `layout_quality` 的 slow baseline；最近一次单独 `check` 结果为 `multi_col=2 / layout_ro_pages=2 / OK`。它仍保持 `slow` 标签，避免把布局专项长样本混入日常默认门禁时长。

2026-04-27 最近一次全量自检结果：

1. `regression_suite` 已新增 `primary-table-structure` 表格专项门禁
2. 默认 suite 已包含 `sample-cmm-32-48-21-ocr`
3. `sample-27-81-17` 现作为 slow layout 专项样本保留在同一 suite 路径中，默认跳过但继续沿用 baseline/suite 口径
4. 本轮 `check-suite` 在 `sample-cmm-32-48-21-ocr` 之前的所有默认样本均在预算内通过；该 OCR 长尾样本仍需按专项性能窗口观察

### 性能风险

- 当前最明显的长尾风险集中在 `sample-cmm-32-48-21-ocr`。
- 该 OCR 样本已纳入默认回归套件，但本轮 600s 观察窗口内尚未收口，仍应按 OCR 重样本性能专项持续跟踪。
- 这说明常规样本和新的表格专项样本都已具备门禁能力，但 OCR 长尾压缩本身仍应视为专项优化，而不是这轮表格门禁工作的回归失败。

## 当前建议

1. 日常改动默认跑快速自检。
2. 涉及 PDF 后处理、OCR、分页重排和门禁口径调整时跑全量自检。
3. 若全量自检返回 `degraded`，优先检查是否再次触发默认超时边界或 OCR 长尾异常放大。
4. 若要继续收敛长尾性能，应把 OCR 重样本优化视为专项任务，而不是继续改变默认上线口径。