# ParseCore 阶段产品化待办（历史快照）

> **⚠️ 本文已被 [parsecore-productization-spec.md](parsecore-productization-spec.md) 取代，保留为历史待办快照。**
> 当前产品化工作的权威源为 SPEC 文档，含代码级差距分析和状态标注。
>
> 本文记录 ParseCore 在“本地 Provider + 统一 IR + quality/coverage + reader + part rerun”主路线之后，仍需要继续产品化的工作。它面向研发、测试、产品接入和运维验收，重点明确每一阶段的开发标准、交付物和验收标准。

## 1. 文档定位

本文不是新的技术路线分叉，而是对以下既有方案的执行化拆解：

- [parsecore-product-upgrade-plan.md](parsecore-product-upgrade-plan.md)：主路线、工作包和升级边界。
- [local-provider-ir-upgrade-plan.md](local-provider-ir-upgrade-plan.md)：Provider、IR、RAG 覆盖和阅读页排版质量方案。
- [configuration.md](configuration.md)：配置、接口、导出和验收命令。
- [self-check-gate.md](self-check-gate.md)：自检门禁与发布前检查。

当前默认判断：

- ParseCore 已具备 `projection=ir|coverage|reader`、`/quality`、`/providers`、`/parts`、局部 rerun、Provider 对比、payload schema 和第一版 KnowledgeUnit/RAG 覆盖审计能力。
- 后续重点不是继续寻找 Skill 直接替代解析，而是把这些能力做成稳定产品能力：可接入、可解释、可回归、可灰度、可运营。
- 本文全部待办都遵守“本地可控”边界，不包含外部 OCR API、第三方云 OCR、按量调用型识别服务。

## 2. 总体目标

ParseCore 后续产品化应达成五个结果：

1. 宿主阅读页可以稳定消费 `projection=reader`，不再依赖 Markdown 补丁猜结构。
2. RAG 入库可以按 KnowledgeUnit 审计，能解释“哪些内容进库、哪些没进、为什么”。
3. 本地 Provider 可以按 route-plan、provider-suite 和质量门禁受控灰度，不能直接无门禁替换主链。
4. 大文件、复杂 PDF、异常页可以通过 `/parts` 和局部 rerun 运维闭环，不要求整文档反复重跑。
5. 发布决策从“解析成功率”升级为“解析质量、阅读页质量、RAG 覆盖、性能稳定性、Provider 准入状态”的综合门禁。

## 3. 全局边界

### 必须坚持

- ParseCore 是解析内核和契约中心，宿主产品只消费稳定 API、schema、质量信号和动作合同。
- Reader、RAG、质量诊断、Provider 对比、导出均只消费统一 IR 或运行期 artifact，不直接消费某个 parser 的私有输出。
- 新增字段必须做到可解释、可回归、可灰度；不能只为某一个页面补临时字段。
- 所有执行型动作都应遵守 `inspect -> compare -> execute -> verify` 流程，先解释，再执行，再验收。
- 局部问题优先用 part rerun、rechunk、reembed 解决，整文档 reparse 作为更高成本动作。
- 外部依赖必须有超时、版本标识、失败隔离和门禁预算；可选依赖不能破坏默认安装。

### 明确不做

- 不接入外部 OCR API 或云端转码服务作为本轮主链能力。
- 不让 Skill 直接写生产解析结果、RAG chunks、embedding 或业务库。
- 不让候选 Provider 未经 suite 门禁就全量替换默认 provider。
- 不把前端阅读页补丁当成解析质量提升方案。
- 不破坏现有 `compat / structured / full` 投影的兼容性。
- 不把宿主系统的权限、文档库 UI、业务流程审批逻辑放进 ParseCore 仓库实现。

## 4. 全局开发标准

### 4.1 契约标准

- 所有新增对外 payload 字段必须同步考虑 JSON Schema、样例 payload、文档说明和回归测试。
- 对外字段命名使用稳定语义，避免临时 UI 命名，例如优先使用 `quality_signal_codes`、`coverage_gap_unit_count`、`preferred_verify_request`。
- 同一语义在不同投影里必须保持一致，例如 part、quality、coverage 中的 `gap_unit_ids` 不应出现不同含义。
- 执行型 contract 必须包含 `method / endpoint / params|payload / context / auto_execute`，并标明是否推荐自动执行。
- 列表排序必须稳定，尤其是 `parts`、`parse_units`、`quality_signals`、`provider rankings`、`contracts`。
- 空值策略必须稳定：未知用 `null`，无结果用空数组或空对象，不要混用字符串占位。

### 4.2 兼容标准

- 新能力默认作为新增字段、新投影或新 endpoint 提供，不应改变旧投影主字段含义。
- `compat / structured / full` 的旧消费者必须继续可用。
- 配置项新增必须有默认值，且默认行为不应让生产解析链路突然切换 provider 或启用重模型。
- 候选 Provider 的可选依赖必须隔离到 extra 或显式安装路径中。
- 老 job、老 artifact 缺少新字段时，投影层应能降级生成可解释 payload。

### 4.3 质量标准

- 每个质量问题都应至少有一个可定位维度：`doc_id`、`page_number`、`part_id`、`unit_id`、`provider_id` 或 `source_block_id`。
- 每个跳过 RAG 入库的 unit 必须有 `skip_reason`。
- 每个推荐动作必须有 `reason_codes` 或可追溯上下文，避免前端只能展示“建议重跑”。
- Quality gate 本阶段仍以 report-only 为主；如果未来阻断生产流程，必须单独评审阈值和回滚策略。
- 对同一批固定样本，质量指标必须可跨版本比较。

### 4.4 测试标准

- 契约改动至少覆盖单元测试、API 测试和 payload schema/contract 测试。
- RAG 覆盖改动必须覆盖 KnowledgeUnit、chunk、embedding 状态和 skip reason。
- Provider 路由改动必须覆盖 route-plan、provider-suite、fallback 和候选不可用场景。
- Part rerun 改动必须覆盖单 part、批量 part、无可重跑 part、provider route plan、monitor/verify contract。
- 大文件能力必须至少有轻量单元/集成测试，真实大样本进入 benchmark 或 self-check，而不是塞进普通单测。
- 文档-only 改动不强制跑全量测试，但必须保证链接、命令和接口名与现有实现一致。

### 4.5 文档标准

- 面向接入方的接口说明写入 `configuration.md` 或 `user-guide.md`。
- 面向路线和阶段验收的说明写入本文或 `parsecore-product-upgrade-plan.md`。
- 面向发布门禁的说明写入 `self-check-gate.md`。
- 每个阶段完成后，必须更新“当前进展”和“下一阶段待办”，避免文档停留在计划态。

### 4.6 运维标准

- 任何后台任务必须能通过 job id 追踪状态。
- 任何局部 rerun 必须能给出 monitor 和 verify 入口。
- Provider 选择必须能解释 route primary、fallback、excluded 和 admission 状态。
- 失败必须返回稳定错误结构，不能只依赖日志排查。
- 生产配置不得泄露密钥、文件路径越界或把临时文件永久留存。

## 5. 总体验收标准

以下条件满足后，才认为 ParseCore 后续产品化进入“可受控灰度”：

- 固定样本集可跑通 `self-check fast/full/perf`，并产出 provider comparison、coverage、quality 和 perf 工件。
- `payload-contract-check` 能验证当前冻结 payload schema 与样例一致。
- `/quality` 可以作为默认诊断入口，并能下钻到 `/providers`、`/parts`、`/coverage`。
- `/parts/rerun` 和 `/parts/{part_id}/rerun` 的响应可以直接驱动 `monitor -> verify`。
- `projection=reader` 能支撑宿主阅读页首版结构化渲染，不要求前端继续猜表格和图示。
- RAG 入库链路能从 chunk 反查 KnowledgeUnit、page、block/table/figure 和 skip reason。
- 候选 Provider 是否进入 route 有 suite 结果、admission 建议和配置回写依据。
- 复杂样本上的质量退化、读序 warning、Provider identity drift 和 admission drift 能进入门禁预算。

## 6. 阶段路线总览

| 阶段 | 主题 | 主要对象 | 推荐优先级 |
| --- | --- | --- | --- |
| P1 | 契约冻结与宿主接入准备 | reader / quality / coverage / providers / parts | 最高 |
| P2 | RAG 入库契约产品化 | KnowledgeUnit / chunks / embedding / index_manifest | 最高 |
| P3 | 本地 Provider 生产化治理 | provider registry / route-plan / suite / admission | 高 |
| P4 | 阅读页排版与诊断闭环 | reader blocks / table / figure / quality hints | 高 |
| P5 | 大文件与 part 运维产品化 | part plan / rerun / cancel / merge / export | 中高 |
| P6 | 发布门禁与质量趋势 | self-check / schema / perf / provider-suite | 中高 |
| P7 | 可观测、安全与运维交付 | metrics / logs / auth / cleanup / audit | 中 |

## 7. P1：契约冻结与宿主接入准备

目标：把现有新增投影和诊断接口固定为宿主可接的产品契约，减少“看代码猜 payload”的接入成本。

### 7.1 待办

- [ ] P1-T01 冻结 `document-ir`、`document-reader`、`document-coverage`、`document-quality`、`document-providers`、`document-parts` 六类 schema 的必填字段、可选字段和空值策略。
- [ ] P1-T02 为每类 schema 增加最小样例、复杂样例和异常样例，覆盖普通 PDF、表格 PDF、图示 PDF、part rerun 后结果。
- [ ] P1-T03 将 `/quality.attention_summary.contracts` 的 request 结构整理为稳定动作合同规范，明确 inspect、compare、execute、verify 各阶段字段。
- [ ] P1-T04 将 `/parts/rerun` 和 `/parts/{part_id}/rerun` 的 monitor/verify contract 写入接口示例，并补齐失败、无提交、部分提交场景说明。
- [ ] P1-T05 为 `/providers.comparison_report.summary` 形成产品字段说明，明确哪些字段用于红点、排序、风险提示和 Provider 复核。
- [ ] P1-T06 为 `/coverage` 输出补充“页级 coverage”和“unit 级 coverage”的消费指南，避免宿主只看页号不看 unit。
- [ ] P1-T07 增加 schema diff 或 contract snapshot 测试，防止字段被无意重命名或删除。
- [ ] P1-T08 在 `user-guide.md` 增加宿主推荐接入顺序：先 reader，再 quality，最后 providers/parts/coverage 下钻。
- [ ] P1-T09 输出一份“宿主前端接入字段清单”，区分必接字段、推荐字段和调试字段。
- [ ] P1-T10 明确 API 错误结构和常见错误码，例如 document not found、invalid projection、part not found、invalid part rerun。

### 7.2 开发标准

- Schema 中冻结的字段不能随意改名；必须变更时要保留兼容字段或升级 schema version。
- 所有 contract endpoint 必须使用 ParseCore 内部真实 endpoint，不写伪路径。
- 产品字段必须避免要求宿主二次聚合，例如 `attention_part_ids`、`gap_unit_ids` 应由 ParseCore 直接给出。
- 示例 payload 必须来自测试或 contract sample，不手写无法复现的字段。
- 接入说明必须区分“可直接渲染”和“仅供诊断/调试”字段。

### 7.3 验收标准

- `GET /v1/parse/schemas` 能列出所有冻结 schema，且 schema 名称与文档一致。
- `payload-contract-check` 能在本地通过，并覆盖 reader、coverage、quality、providers、parts。
- API 测试覆盖 `/quality`、`/coverage`、`/providers`、`/parts`、`projection=reader`。
- 任意一个 warning 文档可通过 `/quality` 找到默认推荐入口，并下钻到对应 part/provider/coverage。
- 局部 rerun 后，响应体提供 job monitor 请求和至少一个 verify 请求。

### 7.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_payload_schemas.py tests/test_payload_contract_check.py -q
py -m pytest tests/test_api_payloads.py tests/test_asgi.py -q
py -m parsecore.cli payload-contract-check
```

### 7.5 退出条件

P1 完成后，宿主产品不需要阅读 ParseCore 源码即可完成第一版 reader、quality、coverage、providers、parts 接入。

## 8. P2：RAG 入库契约产品化

目标：把 RAG 从“按 chunk 拼文本”产品化为“按 KnowledgeUnit 入库、按 coverage 审计、按 skip reason 解释”。

### 8.1 待办

- [ ] P2-T01 固化 `KnowledgeUnit` 字段：`unit_id`、`unit_type`、`text`、`page_span`、`source_block_ids`、`source_table_ids`、`source_figure_ids`、`should_index_for_rag`、`skip_reason`。
- [ ] P2-T02 固化 unit 到 chunk 的映射：每个 chunk 必须能反查 `unit_ids`，每个 indexable unit 必须能反查 `chunk_ids` 或解释未 chunk 原因。
- [ ] P2-T03 固化 chunk 到 embedding 的状态：`embedded`、`embedding_model`、`embedding_state`、`embedding_error_category`。
- [ ] P2-T04 扩展 `index_manifest.rag_coverage.units[]`，确保 unit、chunk、embedding、page、skip reason 形成闭环。
- [ ] P2-T05 建立 skip reason 枚举：页眉页脚、页码、目录重复项、空文本、解析工件、诊断文本、低置信 OCR、图示缺说明等。
- [ ] P2-T06 表格 unit 的 RAG 文本必须优先来自结构化 cells，并保留 caption/header 信息。
- [ ] P2-T07 图示 unit 的 RAG 文本必须优先来自 caption/alt text/邻近说明；缺失时产生质量信号，不把截图路径当正文。
- [ ] P2-T08 `rechunk_document` 必须能基于 KnowledgeUnit 重建 chunk，并保留覆盖审计结果。
- [ ] P2-T09 `reembed_document` 必须能更新 embedding 状态，并在 coverage 中反映 unembedded unit/chunk。
- [ ] P2-T10 增加 `dataset=coverage`、`dataset=parse_units`、`dataset=reader` 的联动验收样例，方便离线审计。
- [ ] P2-T11 为 RAG 侧提供“只消费 KnowledgeUnit/coverage，不猜 parser 输出”的接入说明。
- [ ] P2-T12 固定质量信号和 coverage 指标：`rag_empty_text_page`、`rag_units_without_chunks`、`rag_chunks_not_embedded`、`rag_table_without_unit`、`rag_figure_caption_missing`。

### 8.2 开发标准

- Chunk builder 不应直接面对 parser 私有输出，应优先消费运行期 `DocumentKnowledgeUnit`。
- `unit_id` 必须稳定可复现，不能因为非内容性字段变化就大面积漂移。
- `skip_reason` 不允许为空字符串；非入库内容必须解释。
- 表格、图示、正文的 unit 类型必须明确，不能全部退化为 paragraph。
- Coverage 统计必须同时支持文档级、页级、part 级和 unit 级。
- RAG 相关质量信号必须能回到具体 unit 或 page。

### 8.3 验收标准

- 正文页 `text_page_coverage_ratio >= 0.98`，达不到时必须有页级原因。
- 表格页 `table_unit_coverage_ratio >= 0.95`，达不到时必须指出缺失表格或 unit。
- `unit_chunk_coverage_ratio >= 0.98`，未 chunk 的 unit 必须有原因。
- Chunk 来源可追溯率为 `100%`。
- 页眉页脚、页码、解析诊断文本、截图路径不得进入 RAG 正文。
- 宿主可展示“哪些页进了 RAG、哪些 unit 没进、为什么没进”。
- 重建 chunks 或 embeddings 后，coverage 能反映改善、无变化或仍有缺口。

### 8.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_api_payloads.py tests/test_exports.py tests/test_export_jobs.py -q
py -m pytest tests/test_parts.py tests/test_runtime.py -q
py -m parsecore.cli self-check --config parsecore.toml --profile fast
```

### 8.5 退出条件

P2 完成后，RAG 侧不再需要从 reader 文本、Markdown 或 parser 输出里猜 chunk 来源；所有入库和未入库内容都有可审计依据。

## 9. P3：本地 Provider 生产化治理

目标：让候选本地解析能力可以被评估、准入、灰度、回滚，而不是靠临时脚本或人工判断进入生产链路。

### 9.1 待办

- [ ] P3-T01 固化 provider registry 字段：`id`、`enabled`、`priority`、`media_types`、`extensions`、`profiles`、`capabilities`、`route_mode`、`gate_status`、`gate_checks`。
- [ ] P3-T02 为 `pdf-text`、`docx-native`、`excel-native`、`image-ocr` 补齐 provider provenance：`provider_id`、`provider_version`、`adapter_version`。
- [ ] P3-T03 将 `pymupdf4llm-local` 保持为轻量 PDF baseline provider，明确安装 extra、跳过原因和不支持场景。
- [ ] P3-T04 将 `docling-local` 保持为 PDF/DOCX 统一结构对照 provider，补齐失败隔离和可选依赖测试。
- [ ] P3-T05 对 `mineru-local`、`paddleocr-local`、`marker-local` 只做离线评估清单，不进入默认生产 route。
- [ ] P3-T06 完善 route-plan 排除原因：未安装、禁用、gate pending、gate failed、不支持媒体类型、不满足 capability。
- [ ] P3-T07 完善 provider-suite fast/full/perf，覆盖普通 PDF、表格 PDF、多栏 PDF、图示 PDF、DOCX 和复杂大样本页段。
- [ ] P3-T08 将 provider comparison 的 `recommended_admission` 转化为配置回写建议，但不自动修改生产配置。
- [ ] P3-T09 对 `providers.local_parser_routing.enabled = true` 的灰度环境补齐测试，确认 fallback_to_default 生效。
- [ ] P3-T10 part rerun 的 `provider_route_plan.required_capabilities` 必须能影响实际 provider 选择，并在结果中可追溯。
- [ ] P3-T11 建立 provider identity drift 门禁，防止同名 provider 混入多版上游库或 adapter。
- [ ] P3-T12 建立 route primary 与实测 best provider 偏差预算，防止 route 配置长期偏离质量结果。

### 9.2 开发标准

- 每个 provider 必须输出统一 IR 或明确 skipped/failed，不能输出私有结构给下游消费。
- Provider 失败不得影响默认 provider；fallback 必须可解释。
- 可选 provider 不得在默认安装、默认配置下引入重依赖或启动失败。
- Provider 对比工具不得调用外部 OCR API。
- Provider 入 route 必须同时满足样本、许可证、性能、可观测性四类门禁。
- Provider 改动必须更新配置文档和 provider-suite 说明。

### 9.3 验收标准

- `/v1/parse/providers/route-plan` 能解释 primary、fallback、excluded 和 admission。
- `/providers.comparison_report.summary` 能给出 best provider、primary provider、偏差、warning 和推荐动作。
- provider-suite 能对候选 provider 输出 completed/skipped/failed，且 skipped 原因清晰。
- route primary 与 best provider 偏差不超过 suite gate 预算。
- provider identity drift 和 admission drift 在 fast/full/perf 中可被捕获。
- 开启本地路由灰度后，未满足 gate 的候选不会进入实际执行 route。
- 复杂 part rerun 可以按 `layout`、`tables`、`figures` 等能力要求重算 provider。

### 9.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_provider_comparison_report.py tests/test_provider_provenance.py -q
py -m pytest tests/test_pymupdf4llm_parser.py tests/test_docling_parser.py -q
py tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/provider-suite.fast.json --out-json var/self-check/provider-comparison.fast.json --out-md var/self-check/provider-comparison.fast.md
py tools/self_check.py --config parsecore.toml --profile fast --provider-suite var/regression/provider-suite.fast.json
```

### 9.5 退出条件

P3 完成后，新增 provider 是否能用于生产不再靠主观判断，而由 provider-suite、route-plan、admission 和质量趋势共同决定。

## 10. P4：阅读页排版与诊断闭环

目标：让阅读页从“渲染一段 parser Markdown”升级为“渲染结构化 reader blocks，并能解释排版质量风险”。

### 10.1 待办

- [ ] P4-T01 固化 reader block 类型：heading、paragraph、list、table、figure、page_break、artifact。
- [ ] P4-T02 固化 `reader_policy`：visible、hidden、diagnostic、evidence-only。
- [ ] P4-T03 表格 reader block 必须带结构化 table、cells、header、caption 和 source ids。
- [ ] P4-T04 图示 reader block 必须带 figure metadata、caption/alt text、source ids 和缺失说明。
- [ ] P4-T05 reader block 必须带 `quality_signal_codes`，让前端能在局部展示低置信读序、表格风险、RAG 覆盖缺口。
- [ ] P4-T06 reader page 必须统计 visible/hidden/diagnostic block count，避免隐藏内容无迹可查。
- [ ] P4-T07 读序低置信页必须能从 reader 下钻到 coverage/quality/provider 诊断。
- [ ] P4-T08 同步导出 `dataset=reader` 应覆盖 reader blocks、table、figure、quality signals。
- [ ] P4-T09 为宿主阅读页提供最小渲染协议：按 page 分组、按 block 顺序渲染、hidden 不进正文、diagnostic 进提示层。
- [ ] P4-T10 建立视觉抽检样本清单，覆盖多栏、表格、标题层级、图示、页眉页脚、目录重复项。

### 10.2 开发标准

- 阅读页所需结构应由 ParseCore 输出，不能要求前端用正则猜表格、标题和分隔线。
- `bbox`、`page_number`、`source_*_ids` 应尽量保留，便于未来做源页证据高亮。
- 表格不得在 reader 中退化为挤在一起的纯文本；即便 RAG 文本是 Markdown，reader 也应保留结构化 table。
- 图示缺 caption 是质量问题，不应静默丢失。
- Reader 输出应与 RAG coverage 共享同一套 KnowledgeUnit 来源。

### 10.3 验收标准

- 宿主阅读页可以默认只消费 `projection=reader` 渲染正文、表格和图示。
- 表格样本中，表格 block 保留 cells/header/caption，不退化为普通 paragraph。
- 多栏样本中，读序低置信能产生 signal，并可在 quality/provider 中定位。
- 页眉页脚、页码、解析工件默认不进入正文 blocks。
- 图示缺说明时，reader 和 coverage 都能给出质量提示。
- `dataset=reader` 可以用于离线排版质量抽检。

### 10.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_api_payloads.py tests/test_exports.py tests/test_export_jobs.py -q
py -m pytest tests/test_asgi.py -q
```

### 10.5 退出条件

P4 完成后，阅读页排版问题的主修复路径应回到 IR/reader/provider，而不是继续在前端添加 Markdown 补丁。

## 11. P5：大文件与 part 运维产品化

目标：让大 PDF、复杂页段、局部异常可以通过 part 化运行和运维闭环稳定处理。

### 11.1 待办

- [ ] P5-T01 固化大文件默认 part 策略：按页数、文件大小、profile、历史失败信号决定是否建议 part 化。
- [ ] P5-T02 完善 `/parts/plan` 的 dry-run 能力，允许只看拆分建议而不立即创建子 job。
- [ ] P5-T03 固化 part 状态机：planned、queued、running、done、warning、failed、cancelled、expired。
- [ ] P5-T04 完善 active part 限流、claim token、软超时、取消和失败重试规则。
- [ ] P5-T05 局部 rerun 必须保留 previous observation，并产出 rerun comparison。
- [ ] P5-T06 批量 rerun 必须支持 failed_only、state、part_ids、profile、provider_route_plan。
- [ ] P5-T07 rerun 响应必须提供 monitor_requests、verify_requests、preferred_verify_request 和 workflow。
- [ ] P5-T08 part 结果合并必须保证父文档 pages/lines/records/chunks/index rows 局部替换，不污染其他页段。
- [ ] P5-T09 part 级 coverage、provider、quality signal 必须能回填到父文档 projection。
- [ ] P5-T10 增加 part 清理策略，避免临时 part PDF 和旧 job artifact 无限增长。
- [ ] P5-T11 大文件导出必须支持 coverage、reader、parse_units、records，并允许筛选页段或质量信号。
- [ ] P5-T12 建立真实大样本 benchmark，至少覆盖 1000+ 页和代表性 17000 页级场景。

### 11.2 开发标准

- Part job 必须使用独立 part doc id，不覆盖父文档原始结果。
- 父文档状态必须能表达 partial、warning、failed 等复合状态。
- 任何 part rerun 都必须能追踪 provider_route_plan、selected_provider_id 和 rerun_comparison。
- 批量 rerun 不能重复提交已经 rerun 且未改善的 warning part，除非用户显式指定。
- 大文件测试不应让普通 CI 变慢，真实重样本进入专项 benchmark。

### 11.3 验收标准

- 单 part rerun 后，父文档对应页段被替换，其他页段不变。
- 批量 rerun 能跳过不符合条件的 part，并说明 skipped reason。
- rerun 后可以通过 monitor contract 追踪 job，通过 verify contract 回到 `/parts`、`/quality`、`/coverage`。
- part summary 能汇总 warning、failed、provider changed、rerun status、coverage gap。
- 大文件 part 模式下内存峰值和单次执行时间进入可控范围。
- cancel、timeout、worker restart 不会造成父文档状态永久悬挂。

### 11.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_parts.py tests/test_runtime.py tests/test_asgi.py -q
py tools/large_pdf_stress.py --config parsecore.toml
```

### 11.5 退出条件

P5 完成后，大文件问题应主要通过 part 诊断、局部 rerun、导出和质量验证闭环处理，而不是依赖整文档重复解析。

## 12. P6：发布门禁与质量趋势

目标：让 ParseCore 发布从“测试通过”升级为“质量趋势、Provider 准入、RAG 覆盖、性能稳定”共同通过。

### 12.1 待办

- [ ] P6-T01 固化 `self-check fast/full/perf` 三档门禁输入、输出路径和失败语义。
- [ ] P6-T02 将 provider comparison suite 纳入 self-check 标准输出，并产出 JSON/Markdown 工件。
- [ ] P6-T03 将 payload contract check 纳入发布前必跑命令。
- [ ] P6-T04 建立 coverage 指标趋势：text page coverage、table unit coverage、unit chunk coverage、embedding coverage。
- [ ] P6-T05 建立 reader 指标趋势：visible block count、hidden block count、table block count、reading order warning。
- [ ] P6-T06 建立 provider 指标趋势：best/primary 偏差、quality warning provider、reading order warning provider、identity drift。
- [ ] P6-T07 建立性能趋势：耗时、峰值内存、MB/s、part throughput、export throughput。
- [ ] P6-T08 让 gate policy 支持按样本、provider、profile 配置预算。
- [ ] P6-T09 在 release notes 中记录质量指标变化，不只记录功能变化。
- [ ] P6-T10 定义回滚触发条件：schema 破坏、质量大幅退化、provider drift、性能超预算、关键样本失败。

### 12.2 开发标准

- 门禁输出必须机器可读，不能只有 Markdown。
- 指标必须可跨版本比较，字段名和单位保持稳定。
- 新增样本必须说明覆盖目的和进入 fast/full/perf 哪一档。
- 质量 warning 增加必须有解释，不允许用提高预算掩盖退化。
- 发布文档必须记录未达标项、风险分级和回滚口径。

### 12.3 验收标准

- `self-check fast` 可作为普通发布前必跑门禁。
- `self-check full/perf` 可作为夜间或候选版本门禁。
- Provider suite 失败能导致非零退出码，并指出失败样本和 provider。
- payload schema 破坏能在 contract check 阶段被捕获。
- 发布说明包含 reader、coverage、provider、performance 的关键变化。

### 12.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m parsecore.cli payload-contract-check
py tools/self_check.py --config parsecore.toml --profile fast --provider-suite var/regression/provider-suite.fast.json
py tools/parse_perf_baseline.py --config parsecore.toml --sample-dir D:/app/uploads --extensions .pdf,.docx,.xls,.xlsx,.xlsm --out-json var/self-check/parse-perf-baseline.json --out-md var/self-check/parse-perf-baseline.md
```

### 12.5 退出条件

P6 完成后，每次发布都能回答：质量有没有变好、RAG 覆盖有没有退化、Provider 路由是否合理、性能是否仍在预算内。

## 13. P7：可观测、安全与运维交付

目标：让 ParseCore 进入真实灰度和生产时可排障、可审计、可回滚。

### 13.1 待办

- [ ] P7-T01 统一 request id、job id、doc id、part id、tenant id 在日志和事件中的关联。
- [ ] P7-T02 增加解析阶段耗时指标：upload、parse、normalize、chunk、embed、export、rerun。
- [ ] P7-T03 增加质量指标：quality gate、coverage ratio、provider warning、part warning、rerun outcome。
- [ ] P7-T04 增加 provider 指标：provider id、route status、fallback reason、elapsed、memory、failure category。
- [ ] P7-T05 增加错误分类：invalid input、unsupported media、parser failed、provider unavailable、quota exceeded、timeout、storage failed。
- [ ] P7-T06 强化 API key、桥接 key、敏感配置的注入和日志脱敏。
- [ ] P7-T07 强化文件路径校验、临时目录隔离、上传文件名净化和过期清理。
- [ ] P7-T08 为 part 文件、export 包、provider comparison 工件建立保留期和清理策略。
- [ ] P7-T09 提供最小运维面板字段说明，方便宿主或运维系统接看板。
- [ ] P7-T10 建立灰度回滚手册：关闭 local parser routing、回退 provider 配置、关闭候选 profile、降级 reader 接入。

### 13.2 开发标准

- 日志不输出密钥、完整敏感路径或原文业务内容。
- 指标标签不能无限膨胀，例如不要把文件名全文作为高基数标签。
- 清理任务必须先有 dry-run 或可审计记录。
- 安全校验失败必须返回稳定错误码，不应抛出内部堆栈给宿主。
- 运维事件必须能关联到具体 job/doc/part/provider。

### 13.3 验收标准

- 任意一次解析失败可以通过 job id 定位阶段、错误类别和主要原因。
- 任意一次 provider fallback 可以解释为什么选中或排除候选。
- 临时上传、part 文件、export 工件不会无限保留。
- API key 和敏感配置不会进入日志。
- 灰度出问题时，可以通过配置关闭本地路由并回到默认 provider。

### 13.4 建议验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_asgi.py tests/test_config_parser_options.py tests/test_self_check.py -q
py -m parsecore.cli describe --config parsecore.toml
```

### 13.5 退出条件

P7 完成后，ParseCore 可以作为长期运行服务被监控和运维，而不是只作为离线解析工具使用。

## 14. 跨阶段任务优先队列

建议近期按下面顺序启动：

1. P1-T01 到 P1-T04：先冻结 schema 和动作合同，降低宿主接入成本。
2. P2-T01 到 P2-T05：固化 KnowledgeUnit 和 skip reason，避免 RAG 链路继续猜来源。
3. P4-T01 到 P4-T05：把 reader 渲染字段稳定下来，支撑阅读页替换 Markdown 补丁。
4. P3-T07 到 P3-T12：让 provider-suite 和 admission 成为候选 Provider 准入依据。
5. P5-T05 到 P5-T09：把 part rerun 从“能执行”推进到“能证明改善或未改善”。
6. P6-T01 到 P6-T04：把 self-check、payload contract、coverage 指标固定成发布门禁。
7. P7-T01 到 P7-T05：补齐生产排障最小可观测链路。

## 15. Definition of Ready

进入开发前，每个任务至少满足：

- 已明确影响的 endpoint、projection、schema 或配置项。
- 已明确是否影响旧投影兼容。
- 已明确需要新增或更新的测试文件。
- 已明确是否需要更新 `configuration.md`、`user-guide.md`、`self-check-gate.md` 或本文。
- 已明确样本来源：单元构造样本、固定 fixture、真实大样本或离线 benchmark。
- 已明确是否涉及可选依赖、许可证、部署成本或性能预算。

## 16. Definition of Done

任务完成时至少满足：

- 代码、测试、文档同步更新。
- 新字段有 schema 或样例覆盖。
- 新行为有正向、异常、降级场景测试。
- 对外 API 的错误结构稳定。
- 如涉及 Provider，provider provenance、route-plan、comparison 或 suite 至少覆盖一项。
- 如涉及 RAG，KnowledgeUnit、chunk、embedding、coverage 至少形成一条可追溯链。
- 如涉及 reader，table/figure/quality signal 至少有一个结构化样例。
- 如涉及 part，monitor/verify contract 和 rerun comparison 至少覆盖一个测试。
- 本地验证命令已记录，无法运行的命令必须说明原因。

## 17. 阶段验收矩阵

| 验收项 | P1 | P2 | P3 | P4 | P5 | P6 | P7 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Payload schema 冻结 | 必须 | 关联 | 关联 | 关联 | 关联 | 必须 | 关联 |
| API 回归 | 必须 | 必须 | 必须 | 必须 | 必须 | 必须 | 必须 |
| RAG coverage | 关联 | 必须 | 关联 | 关联 | 关联 | 必须 | 关联 |
| Provider suite | 关联 | 关联 | 必须 | 关联 | 关联 | 必须 | 关联 |
| Reader 渲染样例 | 关联 | 关联 | 关联 | 必须 | 关联 | 关联 | 可选 |
| Part rerun 闭环 | 关联 | 关联 | 关联 | 关联 | 必须 | 关联 | 关联 |
| 性能/内存基线 | 可选 | 关联 | 必须 | 可选 | 必须 | 必须 | 关联 |
| 文档更新 | 必须 | 必须 | 必须 | 必须 | 必须 | 必须 | 必须 |
| 运维/回滚说明 | 可选 | 可选 | 关联 | 可选 | 关联 | 必须 | 必须 |

## 18. 推荐发布检查清单

每次准备把 ParseCore 产品化能力推给宿主前，至少检查：

- [ ] `README.md` 文档导航指向最新计划和用户指南。
- [ ] `configuration.md` 包含新增 endpoint、配置项、导出 dataset 和验收命令。
- [ ] `release-notes.md` 记录新增能力、质量指标变化和已知限制。
- [ ] `payload-contract-check` 通过。
- [ ] API 回归通过。
- [ ] Provider suite fast 通过。
- [ ] 关键样本 coverage 指标未退化，或退化有原因和回滚计划。
- [ ] 可选 provider 的依赖、许可证和部署成本已说明。
- [ ] local parser routing 默认状态符合发布策略。
- [ ] 外部 OCR API 未被纳入主链或默认配置。

## 19. 当前建议结论

下一步最值得优先推进的是 P1、P2、P4 的组合：先冻结宿主可接的 reader/quality/coverage/parts 契约，再把 RAG coverage 和阅读页结构做成稳定消费面。P3 的本地 Provider 灰度可以并行推进，但必须由 provider-suite 和 admission 控制节奏。P5、P6、P7 则是把能力带入长期运营的稳定性工作。

换句话说，后续产品化的核心不是“再找一个更强解析器”，而是让 ParseCore 对解析结果负责到底：能解释结构、能审计入库、能定位缺口、能局部修复、能证明质量是否改善。
