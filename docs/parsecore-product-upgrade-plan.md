# ParseCore 产品升级优化执行方案

日期：2026-06-24

## 1. 文档目的

本文是 `D:\个人文件\个人开发\解析管理中台` 的主执行稿，用来统一三件事：

1. ParseCore 下一阶段到底优化什么。
2. 哪些能力可以纳入产品主链，哪些只能作为评测和辅助工具。
3. 研发、产品、接入方按什么顺序推进，如何验收。

本方案只讨论本地可控路线，明确不含“外部 OCR API”。

相关文档分工：

- `docs/local-provider-ir-upgrade-plan.md`：技术实现口径。
- `docs/parsecore-next-optimization-requirements.md`：服务端专项优化要求。
- 本文：产品升级顺序、能力边界、纳入清单、工作包和验收标准。

## 2. 一句话结论

ParseCore 应从“解析结果投影服务”升级为“本地 Provider 编排内核 + 统一 IR + 质量门禁 + RAG 覆盖审计 + part 级复跑中心”，而不是继续依赖前端补丁、Skill 直连主链或外部 OCR API。

主路线固定为：

```text
本地 Provider -> 统一 Parse IR -> quality/coverage -> reader/RAG/parts
```

**当前状态（2026-06-24）**

- 已完成：升级方案文档主稿、统一 IR/coverage/reader/product payload 主干、Provider 准入与 route-plan 基础、provider-suite 门禁骨架。
- 进行中：part 级诊断继续向 unit 级 coverage 细化，大文件和复杂 PDF 的局部复跑观测仍在补强。
- 下一步：优先让宿主阅读页切到 `projection=reader`，诊断面切到 `/quality + /coverage + /providers + /parts`，再继续灰度本地候选 Provider。

## 3. 基于源码现状的判断

从当前源码看，ParseCore 已经具备产品升级的主干，不需要再重新找方向。

### 已经具备的基础

- `src/parsecore/runtime.py`
  - 已具备 runtime 编排、`quality_gate`、`providers.local_parser_routing`、part 调度和局部复跑基础。
- `src/parsecore/ir.py`
  - 已能从 `blocks / chunks / tables / quality_signals` 派生 `projection=ir`、`projection=coverage`。
- `src/parsecore/api_payloads.py`
  - 已把 `structured / ir / coverage / reader / quality` 收束到统一投影体系。
- `src/parsecore/api_routes.py`
  - 已暴露 `/reader`、`/coverage`、`/providers`、`/parts`、`/providers/route-plan` 等产品级接口。
- `src/parsecore/parts.py`
  - 已开始承载 warning/failed part 定位、动作建议、局部 rerun 和 rerun 对比。
- `src/parsecore/parsers.py`
  - 已开始沉淀 layout 元数据，并输出 `layout_reading_order_confidence`。
- `tools/provider_comparison_report.py`、`tools/self_check.py`
  - 已具备固定样本、Provider 对比、门禁摘要的雏形。

### 当前真正的短板

1. 统一 IR 还没有正式冻结为产品契约。
2. 阅读页仍未完全切换到 `projection=reader` 主消费模式。
3. 复杂 PDF 的增强路线还没有彻底收敛到本地 Provider 灰度链路。
4. `quality / coverage / providers / parts` 虽然已经存在，但还需要进一步变成标准诊断闭环。
5. 已评估的 Skill 和工具还没有形成明确的“纳入/排除”口径。

结论很直接：现在最需要的不是继续试更多工具，而是把现有能力收束成稳定产品面。

## 4. 当前产品问题的根因

解析后文档的排版质量问题，并不是单一 parser 不够强那么简单，根因主要有五类：

1. 缺少冻结后的统一结构契约，阅读页只能猜块、猜顺序、猜表格。
2. reader 和 RAG 还没有完全共享同一套“结构来源”和“跳过原因”。
3. 复杂版面问题还没有被彻底转成可诊断、可重跑、可比较的问题。
4. 候选 Skill 和工具能力很多，但真正能进生产主链的标准还不够清楚。
5. 过去容易把“解析成功”误当成“RAG 已覆盖”和“阅读页可用”。

因此，本轮优化重点不是“再接一个更强 Skill”，而是把 ParseCore 自己做成稳定的产品中枢。

## 5. 产品升级目标

### 目标一：阅读页排版可直接消费

宿主产品默认消费 `projection=reader`，而不是继续围绕 Markdown 做修补。

验收标准：

- 标题、正文、表格、图示、页眉页脚都有明确 `reader_policy`。
- 多栏和图文混排页不再依赖前端重排。
- 页面缺失、图示缺说明、表格缺结构时，能从 `quality / coverage` 直接解释。

### 目标二：RAG 覆盖可逐页审计

正文、表格、图示说明是否进入 `chunk` 和 `embedding`，必须能逐页、逐 unit 解释。

验收标准：

- `/coverage` 成为标准排查入口。
- 每个 `KnowledgeUnit` 都能回答“是否应入库、是否已入库、为什么没入库”。
- `rag_empty_text_page`、`rag_table_without_unit`、`rag_chunks_not_embedded` 等信号稳定输出。

当前实现已经把这件事推进到 product payload：`/coverage` 除页级汇总外，现已输出 `coverage.units` 与页级 `unit_ids / skipped_unit_ids / unembedded_unit_ids`；`/reader` 的 `knowledge_units` 和 `/quality` 的 coverage entrypoint context 也会共享同一份 unit 级覆盖状态。

同一套 unit 级 coverage 语义现在也已经下沉到 `parse_units / /parts`：part 级 `coverage_summary` 会输出 `gap_unit_ids` 等 unit 计数，`coverage_gap_pages` 会回填具体 `unit_ids / unchunked_unit_ids / unembedded_unit_ids`，而 rerun 前后对比也开始直接比较 gap unit 的增减，而不是只比较页号。

这层信息也已经开始回流到默认诊断入口：`/quality.parts_diagnostics.attention_parts[]` 和 `attention_summary.entrypoints.parts.context` 现可直接给出 `coverage_gap_unit_part_ids / gap_unit_ids / rerun_gap_unit_part_ids` 等聚合上下文，宿主不必自己遍历所有 part 明细再拼“哪个页段还缺哪些 KnowledgeUnit”。

同时，批量局部复跑的执行契约也开始复用这份上下文：`rerun_warning_parts.context.rerun_candidates`、`attention_summary.contracts.preferred_execute_request` 和 `parts_batch_rerun_requests` 现在都能直接带出候选 part 摘要与 gap unit 信息，宿主可以直接用它做“执行前确认”和“执行后验证”的默认数据面。

进一步地，真正执行 `POST /parts/rerun` 或 `POST /parts/{part_id}/rerun` 后，响应体也会直接补 `previous_part_observation` 以及 `contracts.monitor_requests / verify_requests / preferred_verify_request / workflow`，把“先盯 job，再回 parts/quality/coverage 验证结果”的路径做成固定合同，方便产品层把局部复跑接成一条完整闭环，而不是只拿到一个 job_id 自己再拼后续流程。

### 目标三：复杂 PDF 可通过本地 Provider 增强

复杂版面不再只依赖一个 parser 的默认顺序，而要纳入本地 Provider 路由和灰度。

验收标准：

- `/providers/route-plan` 能解释 `primary / fallback / excluded`。
- `/providers` 能集中展示质量摘要、Provider 对比、只读诊断入口。
- `/parts` 能定位异常页段并驱动局部复跑。
- 局部复跑可以按 `required_capabilities` 重算本地路由。

### 目标四：整条链路保持本地可控

所有增强都以本地能力、本地模型、自托管容器内执行为前提。

验收标准：

- 不依赖外部 OCR API。
- Skill 不直接写业务 chunks、embedding 或宿主数据库。
- 候选能力必须通过 Adapter 和统一 IR 接入。

### 目标五：大文件和发布门禁一起稳定

大文件不能再靠“整文重跑 + 人工猜测”排障。

验收标准：

- 大文件默认 part 化。
- part 级 diagnostics 和 rerun comparison 成为统一排障入口。
- Provider 对比、coverage 审计、性能趋势进入固定门禁。

## 6. 本轮边界

### 纳入本轮

- PDF / DOCX / Excel / 图片文档的本地解析能力升级。
- 统一 IR、reader 契约、RAG 覆盖审计、quality gate、part rerun。
- 本地 Provider 路由、固定样本评测、发布门禁和大文件运维。

### 明确不纳入本轮

- 外部 OCR API、第三方云 OCR、按量调用型识别服务。
- Skill 直接承担生产解析主链。
- 候选工具直接写业务 chunks、embedding 或宿主数据库。
- 继续用前端补丁兜底解析排版。
- 把 VLM / 多模态模型当全文主解析器。

说明：

- 仓库中的 `remote-http` 配置和相关文档保留为历史兼容资产，不是本轮主路线。
- 如果未来要恢复外部 OCR 或第三方转码能力，必须作为独立议题重新评审，而不是默认回到主链。

## 7. 已评估能力的纳入结论

这一节用于回答“这些 Skill 和工具到底怎么放进产品”。

### 7.1 可进入主路线的能力类型

能进入 ParseCore 主路线的能力，必须同时满足：

1. 能被 Adapter 化。
2. 能输出统一 IR 或可稳定映射到 IR。
3. 能被 `quality / coverage / providers / parts` 统一观测。
4. 不依赖外部 OCR API。
5. 可做固定样本、性能、许可证和回归门禁。

当前实现口径已经补到配置层：`providers.local_parsers` 支持 `route_mode / gate_status / gate_checks`，而 registry、route-plan 会同步暴露 `admission` 视图。候选 provider 可以先以“评测态”进入 registry、route-plan 和 provider-suite，对照样本、许可证、性能、可观测性门禁；只有显式标记为可路由的 provider 才会进入执行链路。

最新实现已经把 provider-suite 与这套准入口径接上：本地 Provider 对比报告会直接输出 `provider_admission_summary`，为每个 provider 给出 `recommended_admission`、`recommended_action`、`requires_config_update`、`drift_fields` 和 `config_patch`，让“固定样本门禁”能够直接推动 route/evaluate 配置收敛，而不是继续停留在人工阅读报告阶段。

当前内置 provider-suite 也已直接使用这套 admission drift 预算：`max_providers_requiring_config_update`、`max_providers_with_route_mode_drift`、`max_providers_with_gate_status_drift`、`max_providers_with_gate_checks_drift`、`max_providers_with_route_ready_drift`。这样“样本表现已经变了，但主配置还没回写”的状态会被显式拦住，不再依赖人工复盘报告。

### 7.2 已评估工具结论

| 能力/Skill | 结论 | 在本方案中的位置 |
| --- | --- | --- |
| `pdfkit-py` | 可继续评估 | 作为 PDF 本地 Provider 候选或离线诊断工具，但必须先 Adapter 化，不能直接替代主链 |
| 官方 `pdf` Skill | 不进生产主链 | 作为 PDF 诊断、渲染、bbox 验证和脚本参考 |
| 官方 `docx` Skill | 不进生产主链 | 作为 DOCX 预览、转换、校验和 QA 工具 |
| MarkItDown / `doc-converter` | 不进生产主链 | 作为 Markdown baseline、小文档 fallback 对照、批量评测工具 |
| `yescan-transoffice-universal` | 明确排除 | 依赖第三方外部服务，不符合“无外部 OCR API”的本轮边界；最多保留为单独议题下的离线对照工具 |

结论要说透一点：这些 Skill 和工具可以帮助我们做评测、渲染校验、格式对照，甚至能改善个别样本的导出观感，但不能从产品主链上根治“解析后文档排版质量”问题。真正决定排版质量是否稳定的，仍然是 ParseCore 内部的 Provider 路由、统一 IR、阅读顺序/表格/图示归一化，以及 quality/coverage/parts 这套可诊断闭环。

### 7.3 对 Skill 路线的最终口径

这不是“不走 Skill 模式”，而是角色调整：

- Skill 负责评测、包装、命令编排、报告生成。
- ParseCore 负责统一 IR、质量门禁、覆盖审计、局部复跑和产品输出契约。
- 真正进入生产主链的能力，必须沉到 Provider Adapter，而不是停留在 Skill 层。

## 8. 目标架构

```text
上传文件
  -> profile / preflight
  -> local provider route-plan
  -> provider adapter
  -> normalize to Parse IR
  -> quality gate + coverage audit
  -> reader / RAG knowledge units / parts / exports
```

核心决策如下：

### 8.1 Provider 只负责解析

候选引擎只负责产出原始结构或中间结构，不直接承担业务入库。

### 8.2 IR 是唯一稳定契约

reader、RAG、质量诊断、Provider 对比、导出只消费 IR，不直接消费某个 parser 的私有输出。

### 8.3 quality gate 先 report-only，再逐步动作化

`quality_gate` 下一阶段要承担三件事：

- 解释质量问题。
- 提供局部复跑建议。
- 为固定样本和发布门禁给出摘要。

### 8.4 诊断流由 ParseCore 输出

复杂文档默认走：

```text
inspect -> compare -> execute -> verify
```

即：

1. 先看 `quality / providers / parts / coverage`。
2. 再决定是否看 Provider 对比或 route-plan。
3. 确认后再触发 `rerun_warning_parts / reparse_document / rechunk_document / reembed_document`。
4. 回到诊断视图验证是否改善。

宿主产品不需要自己发明一套新的诊断状态机。

## 9. 核心工作包

### 工作包 A：统一 IR 与 reader 契约冻结

目标：把阅读页从“猜结构”改成“渲染结构”。

重点模块：

- `src/parsecore/ir.py`
- `src/parsecore/api_payloads.py`
- `src/parsecore/runtime.py`

要做的事：

1. 冻结 `Page / Block / Table / Figure / KnowledgeUnit` 字段口径。
2. 稳定 `projection=reader` 的块结构和表格/图示对象。
3. 把 `quality_signal_codes / rag_text / rag_chunk_ids / source_unit_ids` 固定为 reader 标准字段。
4. 通过 `/v1/parse/schemas/*` 对外发布可回归契约，至少覆盖 `coverage / ir / parts / providers / quality / reader` 六类主链 payload。
5. 将 `provider_registry / local_provider_routing` 一并冻结为正式契约，避免本地 Provider 准入和执行路由继续停留在“文档约定”层。

验收标准：

- 宿主阅读页可以默认只消费 `projection=reader`。
- 阅读页不再靠空行、缩进、分隔线猜结构。
- IR 的 schema 版本变更可显式管理。

### 工作包 B：版面质量与复杂 PDF 增强

目标：把排版质量问题尽量消化在 ParseCore，而不是让宿主补丁兜底。

重点模块：

- `src/parsecore/parsers.py`
- `src/parsecore/pipelines.py`
- `src/parsecore/quality.py`
- `src/parsecore/parts.py`

要做的事：

1. 强化多栏阅读顺序、图注绑定、跨页表归并、表格结构化。
2. 让 `reading_order_confidence` 成为正式质量轴。
3. 把版面问题收敛成 `quality_signals + parts diagnostics + local_provider_rerun`。

验收标准：

- `reading_order_confidence` 稳定进入 `quality_gate`。
- 表格、图示、正文块 reader 表现不再依赖宿主前端补丁。
- 局部问题优先通过 part 定位和 rerun 解决。

### 工作包 C：RAG 覆盖审计闭环

目标：解决“页面可读但没有进 RAG”的隐形质量问题。

重点模块：

- `src/parsecore/ir.py`
- `src/parsecore/api_payloads.py`
- `src/parsecore/exports.py`
- `src/parsecore/export_jobs.py`

要做的事：

1. 继续以 `KnowledgeUnit` 作为 chunk builder 的稳定输入。
2. 强化页级和 unit 级 `missing_reason / skip_reason / embedded`。
3. 让 `coverage` 成为回归、运营抽检和问题归因的统一口径。

验收标准：

- 每个 chunk 都能反查 page 和 source unit。
- `/coverage` 能解释“为什么未入库”，而不仅是“有没有入库”。
- coverage 问题能直接形成 `quality_gate` 动作建议。

### 工作包 D：本地 Provider 路由与灰度体系

目标：把候选本地能力吸纳进同一条受控链路。

重点模块：

- `src/parsecore/config.py`
- `src/parsecore/runtime.py`
- `src/parsecore/api_routes.py`
- `tools/provider_comparison_report.py`
- `tools/self_check.py`

要做的事：

1. 保持内置 `pdf-text / docx-native / excel-native / image-ocr / text-native` 为稳定主链。
2. 将 `pymupdf4llm-local`、`docling-local`、`mineru-local`、`paddleocr-local`、`pdfkit-py` 这类候选先纳入评测与 route-plan。
3. 只有通过样本、许可证、性能、可观测性门禁的候选，才允许进入执行路由。

验收标准：

- 所有候选都能输出统一 IR 或被稳定映射到 IR。
- Provider 对比可同时衡量覆盖率、读序、表格、图示、耗时、内存。
- 没有任何候选因为“看起来更强”就直接替换默认主链。

### 工作包 E：大文件与 part 运维产品化

目标：让超大 PDF 和复杂长文档进入统一工作流。

重点模块：

- `src/parsecore/runtime.py`
- `src/parsecore/pdf_parts.py`
- `src/parsecore/parts.py`
- `src/parsecore/api_routes.py`

要做的事：

1. 强化 `/parts/plan`、`/parts`、`/parts/rerun`、`/parts/cancel`。
2. 在 part 级补充“重跑前后质量差异”和“Provider 变化”观测。
3. 把大文件默认排障口径收束到 part，而不是整文重跑。

验收标准：

- 大文件默认可 part 化执行与排障。
- 批量重跑会自动跳过已有 rerun 对比记录的 warning part。
- 产品、测试、运维使用同一套 part 级诊断口径。

### 工作包 F：发布门禁与持续观测

目标：让解析优化变成持续能力，而不是一次性项目。

重点模块：

- `tools/self_check.py`
- `tools/parse_perf_baseline.py`
- `tools/provider_comparison_report.py`
- `docs/performance-stability.md`

要做的事：

1. 把固定样本、Provider 对比、coverage 审计、large-pdf stress 统一进门禁。
2. 强化“质量是否真的变好”的对比，而不只看是否解析成功。
3. 为候选 Provider 持续追踪性能、内存、失败率、读序质量。

验收标准：

- 发布决策不只看成功率，也看排版质量和 RAG 覆盖质量。
- 候选 Provider 是否进入灰度有明确、可复现的依据。
- 自检结果能直接给出质量风险摘要。

## 10. 推荐推进顺序

### P0：冻结主路线

目标：先把团队路线收紧，停止分叉试验。

本阶段要求：

- 统一认定主路线为“本地 Provider + 统一 IR + quality/coverage + reader + part rerun”。
- 明确不把 Skill 直连生产解析作为主方向。
- 明确本轮不接外部 OCR API。
- 固定代表样本集和门禁口径。

### P1：先让 reader 和 coverage 真正落地

目标：先让现有成果转化成宿主可直接消费的价值。

本阶段要求：

- 宿主优先接 `projection=reader`。
- 质量排查优先接 `/quality`、`/coverage`、`/providers`。
- 局部问题优先通过 `/parts` 和 `/parts/rerun` 解决。

### P2：再做复杂 PDF 的本地 Provider 灰度

目标：用本地候选补复杂版面短板。

本阶段要求：

- 所有候选先进入评测与 route-plan。
- 只在指定 profile、指定样本、指定页段灰度。
- 任何候选都不允许直接全量替换默认主链。

### P3：把大文件和发布门禁一起做稳

目标：让产品从“能用”升级到“可持续运营”。

本阶段要求：

- 大文件默认 part 化。
- 自检、性能、Provider 对比、coverage 审计进入固定门禁。
- 版本发布以质量趋势、解释能力和回归稳定性为标准。

## 11. 建议的量化验收指标

建议对固定样本集设定以下主指标：

| 指标 | 目标 |
| --- | ---: |
| 正文页 `text_page_coverage_ratio` | >= 0.98 |
| 表格页 `table_unit_coverage_ratio` | >= 0.95 |
| `unit_chunk_coverage_ratio` | >= 0.98 |
| chunk 来源可追溯率 | 100% |
| 页级 `missing_reason` 完整率 | 100% |
| 读序低置信 warning 页占比 | 持续下降并纳入门禁预算 |
| route primary 与 best provider 偏差样本数 | 受 suite gate 控制 |

补充原则：

- 如果某个指标暂时达不到，也必须先做到“可解释”。
- 在复杂样本上，解释能力优先级高于表面通过率。

## 12. 对宿主产品的接入建议

若企业文档管理项目要尽快从本轮升级中获益，建议按下面顺序接入：

1. 阅读页优先接 `projection=reader`，逐步淘汰 Markdown 补丁。
2. 诊断首屏优先接 `/quality`，利用 `attention_summary` 做默认入口。
3. 复杂文档下钻接 `/providers`、`/parts`、`/coverage`。
4. RAG 侧优先消费 `KnowledgeUnit / parse_units / rag_coverage`，不要继续猜 chunk 来源。
5. 执行动作时遵循 `inspect -> compare -> execute -> verify`，不要一上来就整文重跑。

这样做的价值是：宿主改造范围可控，但可以尽快拿到排版质量、解释能力和局部复跑能力提升。

## 13. 近期落地清单

建议按以下顺序启动下一轮开发：

1. 冻结 IR 契约和 `projection=reader` 输出字段。
2. 让 `/quality`、`/providers`、`/parts`、`/coverage` 成为统一诊断面。
3. 完善 `reading_order_confidence`、表格 unit、图示 caption 等关键质量轴。
4. 固化 `provider-suite.fast/full/perf` 和自检门禁。
5. 按本地路线继续评估 `pymupdf4llm-local`、`docling-local`、`pdfkit-py` 等候选。
6. 默认把大文件问题收口到 part 化运维。
7. 接入方阅读页和管理端同步切到新契约，不再追加前端排版补丁债务。

## 14. 最终判断

ParseCore 当前最合理的产品化路线不是“再找一个 Skill 直接替代解析”，而是：

```text
Skill 负责评测与包装
本地 Provider 负责解析增强
ParseCore 负责统一 IR、质量门禁、覆盖审计和局部复跑
宿主产品只消费稳定契约
```

这条路线最贴合当前源码状态，也最符合长期产品演进需要。它能同时解决三件事：

1. 解析后文档的排版质量问题有了结构性解决路径。
2. RAG 是否真正覆盖业务内容变得可观测、可审计。
3. 候选本地能力可以进入同一条受控灰度链路，而不会把产品重新带回碎片化集成状态。
