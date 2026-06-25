# ParseCore 本地 Provider 与统一 IR 升级优化方案

日期：2026-06-24

## 背景

企业文档管理项目当前暴露出的核心问题，不是阅读页缺少更多前端修补规则，而是解析结果缺少稳定、可审计、可复用的结构化中间层。阅读页排版、RAG 入库覆盖、表格与流程图识别、质量追踪都应回到 ParseCore 的解析生产线中解决。

ParseCore 现有代码已经具备较好的升级基础：

- `ParserAdapter` 协议可承接不同解析器。
- `Block / Chunk` 已经是统一消费模型。
- `PdfTextParser` 已支持 `pypdf + pdfplumber` 双通道、表格抽取、坏页 OCR、页级 layout metadata。
- `ImageOcrParser` 默认走本地 RapidOCR。
- `profile=auto`、`projection=compat|structured|full`、`quality_signals`、`parse_units`、`records / parts / export` 已经形成结构化生产线雏形。

本方案的目标，是把这些能力收敛成“本地 Provider + 统一 IR + 质量门禁 + RAG/阅读页契约”的产品化链路，让 ParseCore 成为可控、可灰度、可解释的解析内核。

## 当前开发进展

2026-06-24 已启动第一阶段开发，完成 P0/P1 的最小闭环：

- 新增 `parsecore.ir`，从现有 `Block / Chunk / tables / quality_signals` 构建 `projection=ir` 和 `projection=coverage`。
- `projection=ir` 已输出 `providers / pages / blocks / tables / figures / knowledge_units / coverage`。
- `projection=coverage` 已输出页级 RAG 覆盖报告，能定位应入库 KnowledgeUnit 缺少 chunk 的问题。
- `projection=coverage` 现已进一步补齐 unit 级解释：`coverage.units` 会直接输出每个 KnowledgeUnit 的 `coverage_state / missing_reason / embedded / quality_signal_codes`，页级同时暴露 `unit_ids / indexable_unit_ids / skipped_unit_ids / unembedded_unit_ids`，便于把“哪一页有问题”继续收口到“哪一个 unit 为什么没进 RAG”。
- 新增 `GET /v1/parse/documents/{doc_id}/coverage`，同时支持 `GET /v1/parse/documents/{doc_id}?projection=coverage`。
- `coverage` 已进入同步和异步导出 dataset，可下载 `coverage_report.jsonl` / `coverage.jsonl`。
- 新增 `rag_coverage_quality` 汇总，输出 `score / gate / flags / warnings / recommended_action`。
- 阅读页策略字段已进入 IR：`reader_policy / index_policy / display_kind / source_kind / provenance`。
- 新增 `projection=reader`、`GET /v1/parse/documents/{doc_id}/reader` 和 `dataset=reader` 导出，从 IR 派生可直接渲染/抽检的 reader blocks，结构化表格/图示并过滤 hidden 页眉页脚。
- `projection=reader` 已绑定 KnowledgeUnit 和质量信号：reader block 输出 `source_unit_ids / rag_text / rag_chunk_ids / knowledge_units / quality_signal_codes`，阅读页可直接展示真实入库文本、chunk 覆盖、跳过原因和块级诊断提示。
- `projection=reader` 里的 `knowledge_units` 现与 `/coverage` 共享同一份 unit 级覆盖状态，直接补出 `coverage_state / missing_reason / embedded / quality_signal_codes`，避免阅读页再自己拼“该 unit 是否已 chunk / embed”。
- 新增 `GET /v1/parse/schemas`：当前已对外发布 `document-coverage / document-ir / document-parts / document-providers / document-quality / document-reader` 六份 JSON Schema，把统一 IR、阅读页和诊断主链的主干字段冻结成可回归契约；`/v1/runtime.payload_schemas` 也会同步暴露可用 contract 列表，便于宿主联调和版本门禁。
- 其中 `provider_registry` 与 `local_provider_routing` 已不再停留在宽松对象：当前 payload contract 会显式冻结本地 Provider 准入信息（`route_mode / gate_status / gate_checks / route_ready`）以及执行路由决策（`selected_provider_id / selected_route_role / requested.*`），并对旧版简化结构做 projection 归一化，避免历史数据直接脱离新契约。
- 新增 Local Provider Registry 配置骨架：`[[providers.local_parsers]]`，并通过 `/v1/parse/providers`、`/v1/runtime` 和 `projection=ir` 暴露。
- 新增 `GET /v1/parse/providers/route-plan`，按 `media_type / extension / file_name / profile / capability` 生成只读本地 Provider 候选计划，输出 primary/fallback/excluded 及排除原因。
- `providers/route-plan` 已按 `priority desc, id asc` 排序 eligible Provider，并输出 `routing_policy / selection_rank / selection_reason`，避免灰度重跑时受配置顺序影响。
- `providers.local_parsers` 现已支持 `route_mode / gate_status / gate_checks`，Local Provider Registry 与 route-plan 会同步暴露 `admission.route_mode / gate_status / gate_checks / route_ready`：候选 provider 可以先以 `evaluate` 形态进入 registry、route-plan 和 provider-suite，对照样本、许可证、性能与可观测性门禁；只有 `route_mode=route` 且 `gate_status=passed` 的 provider 才允许进入执行路由。
- 新增默认关闭的 `providers.local_parser_routing` 执行路由开关；开启后 runtime 会按 route-plan 的 primary/fallback 在已注册 `[[parsers]]` 中选择实际 parser，并把 `local_provider_routing` 决策写入 job options 以及 `structured / ir / coverage / reader / quality / providers` 投影，默认关闭时仍保持旧 parser 顺序。
- 新增 `GET /v1/parse/documents/{doc_id}/providers`，输出单文档实际 Provider footprint、页级 provider_ids、coverage gap 与 RAG 信号，便于灰度对比和排版问题定位。
- `/providers` 已追加 `comparison_report`，按 Provider 汇总文本覆盖、表格 unit、图示 caption、RAG chunk/embedding 风险；Provider provenance 填充后会进一步观测阅读顺序置信度、耗时和内存。
- `tools/parse_perf_baseline.py` 已接入 `provider_report.comparison_report`，固定样本报告可同时追踪解析性能和本地 Provider 质量排序。
- 新增 `tools/provider_comparison_report.py`，可对同一样本逐个运行已配置的本地 Provider，输出统一 IR、coverage、RAG 覆盖质量和 provider comparison；支持 `--sample` 临时样本和 `--suite` 固定样本集，兼容现有 regression baseline fixtures；未配置候选会显式 skipped，远程 OCR 在该离线工具中禁用；自动候选顺序复用 route-plan 的 primary/fallback，Markdown 报告会展示 route primary/fallback，并输出 `gate_summary` 作为 CI/发布门禁摘要。现已支持 PDF `--page-start/--page-end` 和 suite 样本 `page_range` 局部评估，报告页码保持原始文档页号，便于大文件异常页和采样页灰度；同时新增 `provider_admission_summary`，把 suite 结果直接收口成每个 provider 的 `recommended_admission / recommended_action / requires_config_update / drift_fields / config_patch`，用于把“对比报告”推进到“准入建议”，并支持直接生成配置回写片段。
- 新增 `[quality_gate]` 配置和 report-only `quality_gate` 输出，覆盖 `structured / ir / coverage / quality / providers`，把覆盖审计转成可配置动作建议。
- `quality_gate.action_suggestions` 和 part 级 `action_suggestions` 已输出可展示操作入口，如重建 chunks、重建 embeddings、重跑 warning parts、查看 IR/质量报告；当前只建议，不自动执行。
- `/providers` 投影里的 `quality_gate` 已开始补充 `provider_comparison.summary/actions`，并把 Provider 对比动作合并回 `quality_gate.action_suggestions`，使宿主在接单文档诊断 payload 时不必再手工拼装 `quality_gate` 和 `comparison_actions` 两套路由。
- `/quality` 投影已开始补 `provider_diagnostics`、`parts_diagnostics` 与 `attention_summary`：前者收口 Provider 对比摘要和 comparison actions，后两者收口 part 汇总、attention parts 以及一组已排好顺序的推荐动作。`attention_summary.entrypoints` 还会给出 `quality / providers / parts / coverage` 四个视图的 endpoint、attention 状态、badge 数和推荐落点，并通过 `providers.context / parts.context / coverage.context` 补充当前需要聚焦的 provider、part 与 coverage gap 上下文；`attention_summary.contracts` 则把这些建议进一步规范成统一 request 结构，包含 `default_request / preferred_execute_request / recommended_requests / inspect_requests / execute_requests / entrypoint_requests / parts_batch_rerun_requests / workflow`，使宿主可以先用一份诊断 payload 做问题定性，再按需下钻 `/providers` 或 `/parts`。其中 `workflow` 会显式输出 `inspect -> compare -> execute -> verify` 四阶段，约定“先打开诊断视图，再查看 Provider 对比或 route-plan，最后触发局部复跑或整文档动作”，减少宿主自行拼状态机。
- `/quality` 的 part 诊断也已继续向 unit 级覆盖缺口对齐：`parts_diagnostics.attention_parts[]` 现会直接补 `coverage_gap_unit_count / gap_unit_ids / unembedded_unit_count / gap_unit_count_delta / gap_unit_ids_added / gap_unit_ids_removed`；`attention_summary.entrypoints.parts.context` 也会聚合 `coverage_gap_unit_part_ids / rerun_gap_unit_part_ids / unembedded_part_ids / gap_unit_ids`，让宿主仅消费 `/quality` 就能知道“哪些 part 还卡着哪些 KnowledgeUnit、哪些 rerun 刚刚消除了 unit gap”。
- `quality_gate.action_suggestions` 与 `attention_summary.contracts` 也已开始消费同一份 part/unit 诊断：`rerun_warning_parts.context.rerun_candidates` 会补 `eligible_parts / coverage_gap_unit_part_ids / gap_unit_ids`，而当推荐执行落到 `/parts/rerun` 时，`preferred_execute_request` 和 `parts_batch_rerun_requests` 也会直接带 `attention_parts / gap_unit_ids` 上下文，使宿主在真正执行局部复跑前不必再额外回查 `/parts` 明细。
- `POST /parts/rerun` 与 `POST /parts/{part_id}/rerun` 的执行结果也已开始补 `previous_part_observation` 和 `contracts.monitor_requests / verify_requests / preferred_verify_request / workflow`，把“执行局部复跑后下一步去哪里监控、去哪里验收”收口成稳定协议，宿主不必再自己硬编码 follow-up 跳转。
- `attention_summary.entrypoints.coverage.context` 现也会直接补 `gap_page_numbers / gap_unit_ids / skipped_unit_count / unembedded_unit_count`，让宿主从 `/quality` 下钻 `/coverage` 时不再只拿到页号，而能直接定位到具体的 KnowledgeUnit 缺口。
- `local_provider_rerun` 建议已接入 Provider route-plan：先输出只读 `inspect_provider_route_plan`，并按质量信号推导 `required_capabilities`，如表格缺 unit 对应 `tables`，图示缺 caption 对应 `layout/figures`，阅读顺序低置信度对应 `layout`，文本覆盖类对应 `native-text/local-ocr-fallback`；建议上下文会带 `local_provider_routing`，明确当前是 `inspect_only` 还是已开启执行路由，以及是否需要先启用 `providers.local_parser_routing.enabled`，不把外部 OCR API 纳入升级方案。
- `PdfTextParser` 现已把页级 `layout_reading_order_confidence` 写入 block metadata，`projection=coverage / structured / reader / ir` 与 `quality_gate` 会消费该字段；低于 `min_reading_order_confidence` 时会产出 `reading_order_low_confidence / reading_order_confidence_below_threshold`，把排版读序问题纳入与 RAG 缺口同一条产品动作链路。
- `parse_units / /parts` 已开始附带 part 级 `coverage_summary / coverage_gap_pages / rag_coverage_quality`，并在 rerun 后补 `previous_part_observation / rerun_comparison`，使局部复跑除了能看到 Provider 路由，还能直接判断该页段当前是否仍有 RAG 覆盖缺口，以及这次重跑相对上一轮是改善、退化还是仅切换了 Provider。
- `parse_units / /parts` 的 coverage 口径现已继续向 unit 级对齐：part 级 `coverage_summary` 会补 `gap_unit_ids / total_unit_count / skipped_unit_count / embedded_unit_count / unembedded_unit_count`，`coverage_gap_pages` 也会回填 `unit_ids / indexable_unit_ids / unchunked_unit_ids / unembedded_unit_ids`；自动生成的 `rerun_comparison` 还会进一步比较 `gap_unit_count_delta / unembedded_unit_count_delta / gap_unit_ids_added / gap_unit_ids_removed`，让大文件排障从“哪几页还异常”继续收口到“哪几个 KnowledgeUnit 仍未闭环”。
- `/parts` 现已在原始 `rerun_comparison` 之外补充更适合产品消费的 `diagnostics`，收口 `rerun_status / provider_changed / previous_selected_provider_id / current_selected_provider_id / quality_signal_count_delta / coverage_gap_delta / recommended_focus`；`part_summary` 也同步汇总 `rerun_compared_parts / rerun_statuses / provider_changed_parts / selected_provider_ids`，便于宿主直接做大文件排障面板。
- part 级 `action_suggestions` 已开始消费 `rerun_comparison`：当同一页段 rerun 后结果 `unchanged / regressed / mixed` 时，会优先引导查看 `projection=ir` 或 `providers/route-plan`，减少重复无效 rerun。
- 文档级 `quality_gate.action_suggestions` 中的 `rerun_warning_parts` 也已开始消费 `parse_units[].rerun_comparison`：批量复跑会自动跳过已有 rerun 对比记录的 warning part，并通过 `context.rerun_candidates` 暴露 `eligible_part_ids / skipped_parts`，把“先看 IR / route-plan，还是直接整文档重跑”的判断前移到产品层。
- `index_manifest.rag_coverage` 已追加 KnowledgeUnit 覆盖摘要与 unit 映射，并已进入运行期 index manifest；记录 `unit_count / skipped_unit_count / coverage_score / chunk_ids / units[].page_span / units[].skip_reason / units[].embedded`。
- RAG 覆盖审计已新增 `rag_table_without_unit` 与 `rag_figure_caption_missing`，能把表格未生成可入库 unit、图示缺 caption 的页面进入 `coverage / quality_gate / parts` 动作链路。
- 默认 `ParagraphChunkBuilder` 已开始遵守 KnowledgeUnit 的入库策略：空文本、页眉页脚、解析工件、页码/版本单元不再生成实际 chunk；这些块仍保留在 IR / coverage 中，作为可审计的 skipped unit。
- 新增 `pymupdf4llm-local` 可选 adapter：默认不注册，安装 `.[pymupdf4llm]` 并显式配置 `[[parsers]]` 后可作为 PDF Markdown baseline。
- 新增 `docling-local` 可选 adapter：默认不注册，安装 `.[docling]` 并显式配置 `[[parsers]]` 后可作为 PDF / DOCX 的统一结构对照 provider；其输出会复用统一 Markdown -> Block/Table 归一化链路，并可直接进入 provider comparison 与 route-plan 灰度。
- IR / providers 投影现已稳定回填 `provider_version / adapter_version`：外部本地 Provider（如 `docling-local`、`pymupdf4llm-local`）会保留上游库版本，内置 parser 会统一标记 `parsecore-builtin` + `2026-06-local-provider-adapter`，便于 route-plan、comparison report 和回归门禁追溯“同一个 provider 名称是否已经换过实现”。
- 当前实现只复用本地解析结果和已有 chunks，不引入任何外部 OCR API。

## 目标

1. 提升 PDF、DOCX、Excel、图片型文档的解析稳定性和版面还原质量。
2. 为阅读页提供可直接渲染的结构化块，而不是让前端猜段落、猜表格、猜流程图。
3. 为 RAG 提供逐页覆盖审计，确保正文、表格、图示说明有明确入库或排除原因。
4. 把不同解析工具统一纳入 Provider Adapter，不让工具直接写业务 chunk 或绕过 ParseCore。
5. 保持现有 API 与 projection 兼容，允许宿主产品渐进接入。
6. 所有 OCR 路线限定为本地引擎、本地模型或自托管容器内能力，不接入第三方云端识别服务。

## 非目标

- 不把 Skill 当成生产解析内核；Skill 只作为评估、诊断、包装或本地命令调用入口。
- 不让任何候选工具直接写宿主产品的 RAG chunks、embedding 或业务库。
- 不以阅读页补丁作为解析质量提升的主路线。
- 不把 LLM/VLM 用作全文主解析器；如后续引入，也只能做小范围增强或复核。
- 不要求企业文档管理项目一次性重构解析、阅读页、RAG 和文档库表结构。
- 不在本轮方案内推进远程 OCR 网关、第三方云识别服务或按量调用型识别 API。

## 目标架构

```text
上传文件
  -> preflight / profile=auto
  -> ParseCore Runtime
  -> Local Provider Registry
  -> Provider Adapter 输出原始结构
  -> Normalize to Parse IR
  -> Quality Gate / Coverage Audit
  -> Reader Blocks / RAG Knowledge Units / Audit Reports
```

关键约束：

- Provider 只负责解析，不负责业务入库。
- Normalize 层负责把不同 Provider 的输出收敛到统一 IR。
- Quality Gate 决定是否接受、降级、局部复跑或标记人工复核。
- Reader 和 RAG 都只消费 IR，不直接理解某个解析器的私有输出。

## Provider 策略

### 现有 Provider 收敛

| Provider | 当前基础 | 升级方向 |
| --- | --- | --- |
| `pdf-text` | `pypdf + pdfplumber`，支持表格、layout、坏页 OCR | 继续作为数字 PDF 默认 provider，补齐页级质量、阅读顺序和 RAG 覆盖审计 |
| `docx-native` | 原生 OOXML / Word 结构读取 | 输出 section、标题层级、表格、图片占位和列表结构到 IR |
| `excel-native` | `openpyxl / xlrd` 表格读取 | 强化 sheet、merged cell、header、records、ledger profile |
| `image-ocr` | 本地 RapidOCR | 只处理图片、扫描页或坏页局部 fallback，输出 bbox、confidence、reading_order |
| `text-native` | 纯文本解析 | 作为低风险 baseline，补齐 source_kind 和 quality 标记 |

### 新增本地候选 Provider

候选 Provider 必须通过 Adapter 接入，不直接进入宿主业务链路。

| 候选 | 推荐用途 | 接入级别 | 注意事项 |
| --- | --- | --- | --- |
| `pymupdf4llm-local` | 轻量 PDF baseline、Markdown/JSON、RAG 友好抽取 | P1 | 适合作为 `pdf-text` 对照 provider，优先验证阅读顺序和 bbox |
| `docling-local` | 多格式统一结构、阅读顺序、表格、图片引用 | P1 | 适合作为统一文档结构 provider，对 DOCX/PDF 都可做对照 |
| `mineru-local` | 复杂 PDF、论文/手册、公式、表格、多栏、扫描件 | P2 | 依赖和模型较重，建议先离线评测再进入灰度 |
| `paddleocr-local` | 本地 OCR、版面分析、表格结构、PP-Structure 类能力 | P2 | 需评估 CPU/GPU、模型体积、中文英文混排和部署成本 |
| `marker-local` | PDF 转 Markdown/JSON、表格/公式/图片处理 | P3 | 需重点确认许可证与商业使用边界 |

Provider 命名建议使用 `{engine}-{mode}`，例如 `docling-local`、`mineru-local`，并在结果中记录 `provider_id`、`provider_version`、`adapter_version`、`runtime_options`。

## 离线评估清单

每个候选 provider（`mineru-local`、`paddleocr-local`、`marker-local` 等）在正式接入前，必须完成以下结构化评估。

### 评估维度

| # | 维度 | 评分标准 | 通过阈值 | 数据来源 |
|---|------|----------|----------|----------|
| 1 | **许可证合规** | 商业使用是否允许 | 必须 "允许" | LICENSE 文件 / 官方声明 |
| 2 | **安装可行性** | 依赖体积、模型体积、CPU/GPU 需求 | 模型 < 2GB（CPU 模式）或 < 6GB（GPU 模式） | `pip install` 日志 + `du -sh` |
| 3 | **文本覆盖率** | text_page_coverage_ratio vs pymupdf4llm baseline | 不低于 baseline - 0.05 | `provider_comparison_report --suite` |
| 4 | **表格结构** | table_unit_coverage_ratio vs docling baseline | 不低于 baseline - 0.10 | `provider_comparison_report --suite` |
| 5 | **阅读顺序** | reading_order_confidence_avg | ≥ 0.75 | coverage projection 聚合 |
| 6 | **性能** | elapsed_s_p50 vs baseline | 不超过 baseline × 2.0 | `tools/parse_perf_baseline.py` |
| 7 | **失败隔离** | 异常输入不崩溃、错误消息清晰 | 100% 通过 | `tests/test_docling_parser.py` 模式扩展 |

### 评估样本集

每个候选 provider 必须在以下 5 类样本上完成评测：

| 样本类型 | 描述 | 页数 | 来源 |
|----------|------|------|------|
| 纯文本 PDF | 无表格/图片/扫描 | 5-10 | `var/fixtures/` |
| 表格密集 PDF | 多表/跨页表头 | 10-20 | `var/fixtures/` |
| 图文混排 PDF | 图片+caption+正文 | 10-20 | `var/fixtures/` |
| 扫描件 PDF | 图片页、低质量 OCR | 5-10 | `var/fixtures/` |
| 多栏 PDF | 学术论文/手册格式 | 10-20 | `var/fixtures/` |

### 评估结果模板

```markdown
### [provider-name] 离线评估报告

- 评估日期: YYYY-MM-DD
- 评估人: [name]
- provider_version: [version]
- adapter_version: [version]

| 维度 | 结果 | 阈值 | 通过 |
|------|------|------|------|
| 许可证合规 | 允许/不允许 | 必须"允许" | ✅/❌ |
| 安装可行性 | 模型 XGB | < 2GB/6GB | ✅/❌ |
| 文本覆盖率 | 0.XX | ≥ baseline - 0.05 | ✅/❌ |
| 表格结构 | 0.XX | ≥ baseline - 0.10 | ✅/❌ |
| 阅读顺序 | 0.XX | ≥ 0.75 | ✅/❌ |
| 性能 | X.XXs | ≤ baseline × 2.0 | ✅/❌ |
| 失败隔离 | X/X 通过 | 100% | ✅/❌ |

**综合判定**: 准入/准入(灰度)/不准入

**备注**:
- [发现的问题和限制]
```

### Skill 的位置

Skill 可以保留在研发流程中，但定位应改为：

1. 记录工具安装、命令、样本评测和输出解释。
2. 包装本地命令行工具，给 Provider Adapter 调用。
3. 生成评估报告或诊断材料。
4. 作为人工排障入口。

Skill 不应：

- 直接写 chunks。
- 直接写 embedding。
- 直接修改宿主业务库。
- 绕过 ParseCore 的 IR、质量门禁和覆盖审计。

## Parse IR 升级

当前 `Block.metadata` 已经承载了大量 layout 信息。下一步建议把隐含 metadata 固化为显式 IR 契约，再由 projection 转成兼容输出。

### IR 顶层结构

```json
{
  "schema_version": "2026-06-ir",
  "doc_id": "doc_xxx",
  "parse_run_id": "job_xxx",
  "profile": "table-heavy",
  "providers": [],
  "pages": [],
  "blocks": [],
  "tables": [],
  "figures": [],
  "knowledge_units": [],
  "quality": {},
  "coverage": {}
}
```

### Page

```json
{
  "page_id": "doc_p0001",
  "page_number": 1,
  "width": 595.2,
  "height": 841.8,
  "rotation": 0,
  "source_kind": "digital_pdf",
  "provider_ids": ["pdf-text"],
  "quality_flags": [],
  "reading_order_confidence": 0.92
}
```

### Block

```json
{
  "block_id": "doc_b000001",
  "page_number": 1,
  "page_span": [1, 1],
  "type": "paragraph",
  "semantic_role": "body_section",
  "text": "正文内容",
  "bbox": [72.0, 128.0, 520.0, 168.0],
  "reading_order": 12,
  "confidence": 0.97,
  "source_kind": "native_text",
  "display_kind": "text",
  "reader_policy": "inline",
  "index_policy": "index",
  "quality_flags": [],
  "provenance": {
    "provider_id": "pdf-text",
    "provider_version": "parsecore-builtin",
    "source_page_number": 1
  }
}
```

### Table

```json
{
  "table_id": "doc_p0001_t01",
  "page_number": 1,
  "bbox": [50.0, 220.0, 545.0, 480.0],
  "rows": 10,
  "cols": 6,
  "header_rows": 1,
  "cells": [],
  "caption": "表 1：修订记录",
  "confidence": 0.88,
  "reader_policy": "table",
  "index_policy": "index_table_summary_and_cells",
  "quality_flags": []
}
```

### Figure

```json
{
  "figure_id": "doc_p0002_f01",
  "page_number": 2,
  "bbox": [80.0, 150.0, 500.0, 420.0],
  "figure_type": "flowchart",
  "caption": "图 2：审批流程",
  "alt_text": "",
  "reader_policy": "source_snapshot",
  "index_policy": "index_caption_only",
  "quality_flags": ["figure_text_not_extracted"]
}
```

### Knowledge Unit

`KnowledgeUnit` 是 RAG 入库前的稳定单元，建议从 Block/Table/Figure 派生，而不是让 chunk builder 直接面对原始 parser 输出。

```json
{
  "unit_id": "doc_ku000001",
  "doc_id": "doc_xxx",
  "source_block_ids": ["doc_b000001", "doc_b000002"],
  "source_table_ids": [],
  "page_span": [1, 1],
  "text": "用于 embedding 的文本",
  "unit_type": "paragraph_group",
  "semantic_role": "body_section",
  "should_index_for_rag": true,
  "skip_reason": null,
  "quality_flags": [],
  "chunk_ids": []
}
```

## RAG 覆盖审计

为解决“页面看得到，但 RAG 未入库”或“截图/诊断内容误入库”的问题，ParseCore 应输出页级和单元级覆盖报告。

### 页级覆盖字段

```json
{
  "page_number": 1,
  "parsed_text_chars": 1850,
  "table_count": 2,
  "figure_count": 1,
  "block_count": 18,
  "indexable_unit_count": 12,
  "chunk_ids": ["chunk_001", "chunk_002"],
  "embedded": true,
  "missing_reason": null,
  "provider_ids": ["pdf-text"],
  "quality_signal_codes": []
}
```

### 强制规则

1. 正文页 `parsed_text_chars > 0` 时，不能出现 `indexable_unit_count = 0` 且无 `missing_reason`。
2. 表格页必须生成表格 unit、表格摘要 unit 或明确 `skip_reason`。
3. 图示页至少应生成 caption/周边正文 unit；图内文字未抽取时必须输出质量信号。
4. 页眉页脚、目录重复项、解析诊断文本、截图路径、坐标日志不得进入 RAG。
5. 每个 chunk 必须能反查到 `page_span`、`source_block_ids` 或 `source_table_ids`。

### 推荐新增输出

- `coverage_report.jsonl`：每行一页，适合大文件流式读取。
- `index_manifest.rag_coverage`：保留现有 manifest 字段，并追加 `unit_count`、`indexable_unit_count`、`skipped_unit_count`、`chunked_unit_count`、`coverage_score`、`chunk_ids` 和 `units[]` 映射。
- `quality_signals`：输出 RAG 类信号，例如 `rag_empty_text_page`、`rag_units_without_chunks`、`rag_chunks_not_embedded`、`rag_table_without_unit`、`rag_figure_caption_missing`。

## 阅读页排版优化

阅读页的原则应从“修复 parser 的 Markdown”改为“渲染 Parse IR”。

### Reader 消费策略

| IR 类型 | 阅读页策略 | RAG 策略 |
| --- | --- | --- |
| `title` | 按标题层级渲染 | 入库，保留层级路径 |
| `paragraph` | 按阅读顺序渲染，禁止前端重新猜列 | 入库 |
| `table` | 渲染结构化表格，保留 caption 和页码 | 表格摘要 + cell text 入库 |
| `figure` | 优先显示源页裁剪图或占位，caption 内联 | caption/周边说明入库 |
| `header_footer` | 默认弱化或隐藏 | 默认不入库 |
| `parse_artifact` | 不在正文展示 | 不入库 |

### 排版质量原则

1. 前端不再通过 Markdown 行距、空格、分隔线猜结构。
2. 表格必须走结构化渲染，不把表格退化成纯文本段落。
3. 图示和流程图如果没有可靠结构化文本，阅读页显示源图证据，RAG 只收 caption 和邻近说明。
4. 多栏 PDF 的阅读顺序必须由 Provider/Normalize 层给出，前端只执行顺序。
5. 所有“为什么这里缺内容”的解释来自 quality/coverage，不靠用户肉眼猜。

## 质量门禁

建议把质量拆成四层，避免一个总分掩盖问题。

| 层级 | 关注点 | 示例信号 |
| --- | --- | --- |
| `raw_quality` | 原始文件和页面可解析性 | password_protected、empty_page、scan_page_detected |
| `provider_quality` | Provider 输出可信度 | low_confidence_ocr、table_cells_empty、reading_order_low_confidence |
| `output_quality` | IR 是否可消费 | block_without_page、table_without_bbox、figure_without_caption |
| `rag_coverage_quality` | RAG 是否覆盖该进的内容 | rag_empty_text_page、rag_table_without_unit、rag_figure_caption_missing |

质量门禁动作：

- `accept`：结果可直接消费。
- `accept_with_warning`：可消费，但阅读页或质量面板提示。
- `local_rerun`：同页或同 part 改用另一个本地 Provider 重跑。
- `manual_review`：进入人工复核或运营样本池。
- `reject`：解析失败，不生成可消费结果。

## Profile 与路由

现有 profile 可以继续保留，建议增强为“预检 + 样本页探测 + 质量反馈”的组合。

| Profile | 推荐路由 |
| --- | --- |
| `default` | `pdf-text/docx-native/excel-native/text-native` |
| `table-heavy` | `pdf-text` 开启 pdfplumber 表格强化，必要时对照 `docling-local` |
| `large-pdf` | part 拆分、records/export、局部复跑 |
| `large-pdf-catalog` | 强化目录、页码、标题路径和 index manifest |
| `large-pdf-ledger` | 强化 records、表格、跨页表头 |
| `ocr-heavy` | 本地 RapidOCR / `paddleocr-local`，按页局部处理 |
| `scan-pdf` | 图片页检测、本地 OCR、低置信页复核 |
| `excel-ledger` | sheet/records/header/merged cell 强化 |

路由原则：

1. 数字 PDF 先走轻量本地 provider，低质量页再局部对照。
2. 扫描页只对异常页或扫描页走 OCR，不全量默认 OCR。
3. 大文件默认拆 part，避免单次请求和单进程长时间占用。
4. 多 provider 对照只用于采样、坏页、关键页或灰度阶段，不作为所有文档默认路径。

## API 与数据模型建议

### Projection 扩展

保留现有：

```text
projection=compat
projection=structured
projection=full
```

建议新增或在 `full` 中稳定暴露：

```text
projection=ir
projection=coverage
projection=reader
```

兼容策略：

- `compat` 不破坏旧消费者。
- `structured` 面向宿主产品中等改动，返回 pages/tables/quality/parse_units/index_manifest。
- `full` 面向排障和复核。
- `ir` 面向阅读页和新 RAG 管线。
- `coverage` 面向质量运营、入库审计和自动化回归。
- `reader` 面向阅读页直接渲染，返回 `pages / blocks / reader_summary`，表格和图示保留结构化对象，hidden 块不进入正文渲染；reader block 同时带 `source_unit_ids / rag_text / rag_chunk_ids / knowledge_units / quality_signal_codes`，用于入库覆盖提示和诊断面板。

### Block / Chunk metadata 最小追加字段

```json
{
  "page_number": 1,
  "page_span": [1, 1],
  "bbox": [0, 0, 0, 0],
  "reading_order": 1,
  "confidence": 0.95,
  "provider_id": "pdf-text",
  "source_kind": "native_text",
  "reader_policy": "inline",
  "index_policy": "index",
  "quality_flags": []
}
```

### 新增接口建议

```text
GET /v1/parse/documents/{doc_id}?projection=ir
GET /v1/parse/documents/{doc_id}?projection=coverage
GET /v1/parse/documents/{doc_id}?projection=reader
GET /v1/parse/documents/{doc_id}/coverage
GET /v1/parse/providers/route-plan?file_name=manual.pdf&profile=table-heavy&capability=tables
GET /v1/parse/documents/{doc_id}/providers
GET /v1/parse/documents/{doc_id}/reader
POST /v1/parse/documents/{doc_id}/parts/rerun
```

其中 `providers/route-plan` 默认只做本地候选 Provider 解释和灰度规划，不触发解析；显式开启 `providers.local_parser_routing.enabled` 后，runtime 会按 route-plan 的 primary/fallback 在已注册 parser 中选择实际 provider，并在 job options 和各类文档投影写入 `local_provider_routing` 决策。eligible Provider 按 `priority desc, id asc` 排序，候选项会标出 `selection_rank / selection_reason`；`parts/rerun` 现在已可接收 `provider_route_plan.required_capabilities`，让质量门禁触发的局部复跑在真正执行前按当前 part 文件重新计算本地 Provider，而不是沿用旧 route 决策。`partition_parts / parse_units / /parts` 也开始回填 `provider_route_plan / local_provider_routing / provider_ids`，使产品侧能直接看到这次局部复跑为什么选中某个 Provider。

## 配置建议

保留当前 `parsecore.toml` 的本地默认策略：

```toml
[providers.ocr]
enabled = true
provider = "rapidocr"
```

建议新增 Provider Registry 配置：

```toml
[providers.local_parser_routing]
enabled = false
fallback_to_default = true
include_disabled = false

[[providers.local_parsers]]
id = "pdf-text"
enabled = true
priority = 100
media_types = ["application/pdf"]
profiles = ["default", "table-heavy", "large-pdf"]

[[providers.local_parsers]]
id = "pymupdf4llm-local"
enabled = false
priority = 80
media_types = ["application/pdf"]
profiles = ["default", "table-heavy"]

[[providers.local_parsers]]
id = "docling-local"
enabled = false
priority = 70
media_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
profiles = ["table-heavy", "scan-pdf"]

[[providers.local_parsers]]
id = "mineru-local"
enabled = false
priority = 60
media_types = ["application/pdf"]
profiles = ["scan-pdf", "table-heavy"]
```

建议新增质量门禁配置：

```toml
[quality_gate]
enabled = true
min_text_page_coverage = 0.98
min_table_unit_coverage = 0.95
min_reading_order_confidence = 0.75
allow_local_rerun = true
allow_manual_review = true
```

## 实施阶段

### P0：只读质量审计

目标：先知道问题在哪里，不改业务逻辑。

任务：

1. 固定样本集：普通 PDF、扫描 PDF、多栏 PDF、表格密集 PDF、DOCX、Excel、超大 PDF。
2. 用现有 ParseCore 跑 `projection=full`。
3. 生成 `coverage_report.jsonl` 和质量摘要。
4. 标出正文空入库、表格未入库、图示无说明、阅读顺序异常、坏页 OCR 触发原因。

验收：

- 每份样本都有页级覆盖报告。
- 每个 chunk 能反查来源页和 block。
- 每个未入库页都有 `missing_reason` 或质量信号。

### P1：Provider Adapter 与 IR 契约冻结

目标：让所有解析器都进入同一输出模型。

任务：

1. 新增 `ParseIRDocument / ParseIRPage / ParseIRBlock / ParseIRTable / ParseIRFigure / KnowledgeUnit`。
2. 为现有 `pdf-text/docx-native/excel-native/image-ocr` 写 normalize adapter。
3. 在 `projection=ir` 下输出新模型。
4. 保持 `compat/structured/full` 旧口径可用。

验收：

- 旧测试通过。
- 新 IR 能覆盖现有 blocks/tables/quality_signals。
- 阅读页可只依赖 IR 渲染主内容。

### P2：PDF 本地 Provider 对比与路由

目标：用本地候选引擎提升复杂 PDF 和版面质量。

任务：

1. 接入 `pymupdf4llm-local` 作为轻量对照。
2. 离线评估 `docling-local`、`mineru-local`、`paddleocr-local`。
3. 建立 provider 对比报告：文本覆盖率、表格结构、阅读顺序、图示 caption、耗时、内存，并用 route-plan 记录候选 Provider 进入灰度的原因；当前已落地离线 provider 对比工具、`--suite` 固定样本输入、覆盖率、表格/图示 RAG 风险和 provenance 观测轴，route-plan 会按 provider priority 选 primary/fallback，质量门禁的 `local_provider_rerun` 也会先给出 route-plan 检查入口、候选能力要求和执行路由上下文；执行路由已通过默认关闭的 `providers.local_parser_routing` 接入 runtime，可在指定环境/profile 灰度。
4. 将候选 provider 先用于灰度、采样页、异常页和指定 profile；当前离线对比工具已支持 PDF `page_range`/part 模式，并在 IR/coverage/provider report 中保留原始页号；线上 part 复跑也已能接收 `provider_route_plan.required_capabilities`，把 quality gate 的 rerun 建议真正传到执行路由。

验收：

- 每个 provider 都输出统一 IR。
- 不同 provider 的结果可在同一报告中对比。
- 低质量页可局部复跑，不要求整份文档重跑。

### P3：RAG Knowledge Unit 重构

目标：让 RAG 入库从“按 chunk 拼文本”升级为“按知识单元入库”。

任务：

1. 从 IR/Artifact 生成 `KnowledgeUnit`。
2. Chunk builder 改为消费 `KnowledgeUnit`。
3. `index_manifest` 记录 unit、chunk、embedding、page_span、skip_reason。
4. 增加 RAG 覆盖测试。

当前进展：第一版已经从 blocks/tables 生成 KnowledgeUnit，并把 unit 覆盖关系写入 `index_manifest.rag_coverage`；运行期 `ParsedDocumentArtifact` 也已携带显式 `DocumentKnowledgeUnit`，chunker 会优先消费这些 unit，index manifest 会以 `document_knowledge_units` 策略审计 unit/chunk/embedding 覆盖。`projection=ir/coverage` 会优先复用运行期 `rag_coverage.units`，再用实际 chunk 文本回填 KnowledgeUnit 文本，使阅读页、覆盖审计和真实入库内容保持一致。表格 unit 会优先消费结构化 `cells` 渲染 Markdown，并在有标题/说明时把 caption 放到表格文本前；图示 unit 会在缺少 caption 时回退使用 `alt_text`，coverage 也会把 `alt_text` 视为可入库说明。默认 chunk builder 已先对齐 KnowledgeUnit 的 `should_index_for_rag / skip_reason` 策略，避免页眉页脚、解析工件、页码/版本单元进入实际 RAG chunks。后续可继续加强跨 block 合并和更复杂的图内文字提取策略。

验收：

- 正文、表格、图示说明都有明确入库策略；表格 cells 可渲染为 RAG 文本，图示 caption 缺失时可使用 alt text。
- 不应入库的页眉页脚、诊断文本、重复目录不会入库。
- 宿主系统可展示“哪些页进了 RAG，哪些页没有进，为什么”。

### P4：阅读页按 IR 渲染

目标：减少前端排版猜测。

任务：

1. 企业文档管理项目阅读页改为消费 `projection=ir`。
2. 标题、段落、表格、图示、页眉页脚按 `reader_policy` 渲染。
3. 质量信号进入阅读页提示和诊断面板。
4. 源页截图只作为证据，不混入正文文本。

验收：

- 阅读页无需 Markdown 补丁即可稳定展示正文和表格。
- 表格不再退化为挤在一起的纯文本。
- 图示缺文字时有明确提示和源图证据。

当前进展：`projection=reader` 已能从 IR 派生结构化 reader blocks，并过滤 hidden 页眉页脚；表格和图示 block 会绑定对应 KnowledgeUnit 与 RAG chunk，`rag_text` 使用真实入库文本，图示 caption 缺失但存在 `alt_text` 时也能进入 reader 文本和 coverage 口径。reader block 已按页级、表格级、图示级和 block 级信号回填 `quality_signal_codes`，同步导出和异步 export-job 也已支持 `dataset=reader`，便于阅读页联调、排版质量抽检和离线回归；企业文档管理前端下一步可优先消费 `reader.blocks[].table / figure / rag_text / quality_signal_codes`，再逐步替换旧 Markdown 补丁逻辑。

### P5：回归、性能与发布门禁

目标：让解析质量提升可持续。

任务：

1. 建立固定样本 benchmark。
2. 记录 provider 耗时、内存、失败率、质量信号密度。
3. 大文件使用 part / records / export 链路复跑。
4. CI 增加 IR schema、coverage、provider fallback 测试。

验收：

- 同一批样本的覆盖率和质量信号趋势可比较。
- 新 provider 失败不会影响默认 provider。
- 性能退化超过阈值时能被测试或 benchmark 捕获。

当前 `tools/parse_perf_baseline.py` 已把固定样本性能报告接入 Provider 对比口径：每个样本除 `elapsed_s / peak_kb / blocks / chunks / tables` 外，还会写入 `provider_report.comparison_report`，并在 Markdown 中展示 primary/best provider 与 provider score。

当前 `tools/provider_comparison_report.py` 已支持固定样本 suite：可直接读取 `samples / fixtures / cases` 清单，也能复用现有 `entries -> baseline -> fixtures` 回归套件；`fixture_relative_path` 可通过 `--fixture-root` 或 `PARSECORE_REGRESSION_FIXTURE_ROOT` 恢复跨机器路径，样本级 `providers / provider_ids / profile / page_range` 可覆盖全局参数。未显式指定 Provider时，工具会按 route-plan 的 `eligible_provider_ids` 顺序执行 primary/fallback，并把 excluded 候选保留为 skipped 解释；Markdown 会展示每个样本的 route primary/fallback。对 PDF 还支持 `--page-start / --page-end` 局部评估：工具会先切出 part 文件，再把 IR/coverage/provider report 页码平移回原始文档页号，适合大文件异常页、采样页和局部灰度。报告已新增 `gate_summary`，会标出 route primary 缺失、route primary 未完成、无完成 provider、失败 provider、最佳 provider 与 route primary 不一致、provider 质量 warning，以及 `provider_reading_order_warning_runs` 这类读序退化计数；同时新增 `provider_identity_summary` 与 identity drift warning，用于追踪同名 Provider 是否混入多版上游库或多版 Adapter。suite 顶层现还支持 `gate_policy.max_provider_reading_order_warning_runs`、`gate_policy.max_provider_quality_warning_runs`、`gate_policy.max_samples_best_provider_differs_from_route_primary`、`gate_policy.max_providers_with_multiple_provider_versions`、`gate_policy.max_providers_with_multiple_adapter_versions`，可把“读序 warning 只能出现 0 次”“Provider 质量 warning 不能回归放大”“route primary 与实测 best provider 的偏差不能超预算”“同名 Provider 不允许混入多版实现”这类约束直接写进样本工件。除此之外，provider admission 已可按 `max_providers_requiring_config_update`、`max_providers_with_route_mode_drift`、`max_providers_with_gate_status_drift`、`max_providers_with_gate_checks_drift`、`max_providers_with_route_ready_drift` 五类预算继续收口，让“对比结论还没回写到配置”也能进入 fail gate；仓库内置的 `provider-suite.fast/full/perf.json` 已把这五类预算固定为 `0`。默认 auto route-plan 模式下，disabled 或尚未 adapter 化的候选仍会保留 `skipped` 解释，但不再自动升级成 gate warning；只有显式指定的 Provider 被跳过，或出现非预期 skipped，才进入 `provider_runs_skipped`。同时，若 provider 在当前 suite 中仅因 `unsupported_media_type_or_extension` 被跳过，admission 汇总会保留现有准入口径，不再误报“应回写 evaluate/pending”。仓库内现有 [var/regression/provider-suite.fast.json](D:\个人文件\个人开发\解析管理中台\var\regression\provider-suite.fast.json)、[var/regression/provider-suite.full.json](D:\个人文件\个人开发\解析管理中台\var\regression\provider-suite.full.json) 和 [var/regression/provider-suite.perf.json](D:\个人文件\个人开发\解析管理中台\var\regression\provider-suite.perf.json) 三套工件：`fast/full` 分别镜像 `suite.fast.json` 与 `suite.full.json` 的主线 PDF baseline，并先约束 route-plan 主候选偏差、Provider 身份漂移和 admission drift；`perf` 继续聚焦 `sample-27-81-17` 与 `sample-cmm-32-48-21-ocr` 两个复杂版面/OCR 重样本。CLI 在 fail gate 或 provider failed 时返回非零。`tools/self_check.py` 现已提供显式 `--provider-suite / --provider-fixture-root / --provider-profile` 入口，并在 `fast/full/perf` profile 默认尝试带上对应 provider suite；provider 对比结果会作为 `provider_comparison_suite` 检查写入 self-check JSON，并额外落盘 `provider-comparison.fast/full/perf.json|md` 标准工件，把 `quality_warn / read_order_warn / route_mismatch / identity_drift / admission_update` 摘要带进自检输出，沿用非零退出码进入 CI 或夜间 Provider 质量门禁。

与此同时，文档级 `/providers` 投影里的 `comparison_report.summary` 也已补齐更适合产品消费的摘要字段：除了 `provider_count / pending_axes` 外，还会直接输出 `primary_provider_rank / primary_provider_score / primary_provider_recommendation / best_provider_score / best_provider_recommendation / best_provider_differs_from_primary / providers_with_quality_warnings / providers_with_reading_order_warning / providers_with_coverage_gaps / quality_warning_provider_ids / reading_order_warning_provider_ids / coverage_gap_provider_ids / attention_provider_ids / needs_attention / recommended_action`。这意味着接入方做质量面板、Provider 复核提示或“当前 primary 是否仍值得保留”的运营视图时，不必再逐条遍历 `rankings` 自己二次计算。当前 `/providers` 顶层还额外补了 `comparison_actions`，会把“查看 Provider 对比”和“检查当前 route-plan”这种只读动作直接整理成可渲染入口；同一份 payload 的 `quality_gate.provider_comparison` 也会带上相同摘要，并把这些动作并入 `quality_gate.action_suggestions`，方便宿主直接把 `/providers` 作为单一诊断视图消费。

## 样本与验收清单

建议样本池：

- 普通数字 PDF：验证正文、标题、页码、目录。
- 表格密集 PDF：验证 `tables/cells/header_rows/records`。
- 多栏 PDF：验证阅读顺序。
- 扫描 PDF：验证本地 OCR、bbox、confidence、低置信页信号。
- 流程图/组织图 PDF：验证 figure、caption、source_snapshot。
- DOCX 手册：验证标题层级、列表、表格、图片占位。
- Excel 台账：验证 sheet、merged cell、header、records。
- 17000 页级大 PDF：验证 part、records、export、局部复跑和内存稳定性。

最小验收指标：

| 指标 | P0 基线 | P2/P3 目标 |
| --- | ---: | ---: |
| 正文页 RAG 覆盖率 | 先统计 | >= 98% |
| 表格页 unit 覆盖率 | 先统计 | >= 95% |
| chunk 来源可追溯率 | 先统计 | 100% |
| 图示 caption 覆盖率 | 先统计 | >= 90% 或有缺失原因 |
| 页级 missing_reason 完整率 | 先统计 | 100% |
| Provider 失败隔离 | 手工验证 | 自动测试覆盖 |

## 风险与决策点

1. 许可证风险：`marker-local` 等候选必须先确认商业使用边界。
2. 模型体积：`mineru-local`、`paddleocr-local` 可能增加镜像体积和部署复杂度。
3. CPU/GPU 成本：本地模型必须按 profile 和坏页局部触发，避免默认全量跑重模型。
4. 中文英文混排：样本池必须包含中文规章、英文手册、表格和编号条款。
5. 质量阈值误伤：第一阶段只提示和报告，不直接阻断业务。
6. 兼容性：`compat/structured/full` 不破坏，`ir/coverage` 作为新增口径灰度。
7. 大文件性能：Provider 对比必须支持 page range 或 part 模式，否则不能进入大文件默认链路。

## 近期任务清单

1. 新增 `projection=coverage` 的内部数据结构和 JSON 导出。
2. 为现有 `pdf-text` 输出补齐 `provider_id / page_span / reader_policy / index_policy / quality_flags`。
3. 从现有 blocks/tables 生成第一版 `KnowledgeUnit`，并让默认 chunk builder 遵守 KnowledgeUnit 的入库/跳过策略。
4. 增加 `rag_empty_text_page / rag_table_without_unit / rag_figure_caption_missing` 质量信号。
5. 接入 `pymupdf4llm-local` 做 PDF baseline 对照。
6. 增加 Provider route-plan 和实际结果 `comparison_report`，继续形成 `docling-local / mineru-local / paddleocr-local` 离线对比报告。
7. 企业文档管理项目阅读页改为优先读取 `projection=ir`。
8. 建立固定样本 benchmark，并通过 `self_check --provider-suite` 把 `provider_comparison_report --suite` 报告纳入发布门禁。

## 结论

ParseCore 的下一步不应是继续堆阅读页补丁，也不应让 Skill 直接承担生产解析。推荐路线是：

```text
Skill 负责评估和包装
本地 Provider 负责解析
ParseCore 负责统一 IR、质量门禁和覆盖审计
阅读页和 RAG 只消费 ParseCore 的稳定契约
```

这条路线能同时解决三个问题：解析质量可比较、阅读页排版有结构依据、RAG 入库覆盖可审计，并且符合本地可控和渐进接入的产品要求。
