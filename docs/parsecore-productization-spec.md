# ParseCore 产品化规格说明书（SPEC）

> **本文取代 `parsecore-productization-todo.md`，后者保留为历史待办快照。**
> 状态以代码证据为准，每季度校准一次。

## 1. 文档定位与适用范围

### 1.1 目标

把 ParseCore 已具备的解析能力收束为**可接入、可解释、可回归、可灰度、可运营**的产品契约。本文是 P1-P7 产品化的单一权威源（single source of truth）。

### 1.2 与现有文档关系

| 现有文档 | 定位 | 与本文关系 |
| --- | --- | --- |
| [parsecore-product-upgrade-plan.md](parsecore-product-upgrade-plan.md) | 战略路线、纳入边界 | 本文引用其边界结论，不重复 |
| [parsecore-productization-todo.md](parsecore-productization-todo.md) | P1-P7 待办清单 | **已被本文取代**，保留为历史快照 |
| [implementation-plan.md](implementation-plan.md) | Phase 0-7 历史路线 | 本文不重复历史，只引用活动阶段 |
| [self-check-gate.md](self-check-gate.md) | 发布门禁 | 本文 P6 引用并扩展 |
| [configuration.md](configuration.md) | 配置手册 | 本文 P1/P7 引用其字段 |

### 1.3 全局边界

直接引用 [parsecore-product-upgrade-plan.md](parsecore-product-upgrade-plan.md) 的边界结论：

- **必须坚持**：ParseCore 是解析内核和契约中心；Reader/RAG/质量诊断/Provider 对比/导出均只消费统一 IR；新增字段可解释、可回归、可灰度；执行型动作遵守 `inspect → compare → execute → verify`；局部问题优先 part rerun；外部依赖有超时/版本/隔离/门禁预算。
- **明确不做**：不接入外部 OCR API 作主链；不让 Skill 写生产结果；候选 Provider 未经 suite 门禁不替换主链；不把前端补丁当解析质量提升；不破坏 `compat/structured/full` 兼容；不把宿主权限/UI/审批放进 ParseCore。

### 1.4 状态约定

| 标记 | 含义 |
| --- | --- |
| ✅ | 已实现（有代码+测试证据） |
| ⚠️ | 部分实现（核心有但需补强） |
| ❌ | 未实现 |

每条状态后跟 `(证据: 文件:行号)` 指针，指向具体代码位置。

---

## 2. 全局开发与契约标准

### 2.1 契约标准

- 所有新增对外 payload 字段必须同步考虑 JSON Schema、样例 payload、文档说明和回归测试。
- 对外字段命名使用稳定语义，优先使用 `quality_signal_codes`、`coverage_gap_unit_count`、`preferred_verify_request`。
- 同一语义在不同投影里保持一致，例如 part、quality、coverage 中的 `gap_unit_ids` 不应出现不同含义。
- 执行型 contract 必须包含 `method / endpoint / params|payload / context / auto_execute`，并标明是否推荐自动执行。(证据: `payload_schemas.py:_action_suggestion_schema`)
- 列表排序稳定，尤其是 `parts`、`parse_units`、`quality_signals`、`provider rankings`、`contracts`。
- 空值策略：未知用 `null`，无结果用空数组或空对象，不混用字符串占位。

### 2.2 兼容标准

- 新能力默认作为新增字段、新投影或新 endpoint，不改变旧投影主字段含义。
- `compat / structured / full` 的旧消费者必须继续可用。(证据: `api_routes.py:906 get_document` 支持 projection 参数)
- 配置项新增有默认值，默认行为不突然切换 provider 或启用重模型。
- 候选 Provider 可选依赖隔离到 extra 或显式安装路径。(证据: `pyproject.toml` 依赖分组 `[pymupdf4llm]`/`[docling]`)
- 老 job、老 artifact 缺少新字段时，投影层应能降级生成可解释 payload。

### 2.3 质量标准

- 每个质量问题至少有一个可定位维度：`doc_id`、`page_number`、`part_id`、`unit_id`、`provider_id` 或 `source_block_id`。(证据: `payload_schemas.py:_quality_signal_schema` 已有 page_number/block_id/table_id/figure_id)
- 每个跳过 RAG 入库的 unit 必须有 `skip_reason`。(证据: `ir.py:599 skip_reason` 字段)
- 每个推荐动作必须有 `reason_codes` 或可追溯上下文。(证据: `payload_schemas.py:_action_suggestion_schema` 已有 reason_codes)
- Quality gate 本阶段仍以 report-only 为主；如未来阻断生产流程，必须单独评审阈值和回滚策略。
- 对同一批固定样本，质量指标可跨版本比较。

### 2.4 测试标准

- 契约改动至少覆盖单元测试、API 测试和 payload schema/contract 测试。
- RAG 覆盖改动必须覆盖 KnowledgeUnit、chunk、embedding 状态和 skip reason。
- Provider 路由改动必须覆盖 route-plan、provider-suite、fallback 和候选不可用场景。
- Part rerun 改动必须覆盖单 part、批量 part、无可重跑 part、provider route plan、monitor/verify contract。
- 大文件能力至少有轻量单元/集成测试，真实大样本进入 benchmark 或 self-check。
- 文档-only 改动不强制跑全量测试，但必须保证链接、命令和接口名与现有实现一致。

### 2.5 文档与运维标准

- 面向接入方的接口说明写入 `configuration.md` 或 `user-guide.md`。
- 面向路线和阶段验收的说明写入本文。
- 面向发布门禁的说明写入 `self-check-gate.md`。
- 每个阶段完成后，更新"当前进展"（本文第 14 节）。
- 任何后台任务必须能通过 job id 追踪状态。(证据: `api_routes.py:899 get_job`)
- 任何局部 rerun 必须能给出 monitor 和 verify 入口。(证据: `api_routes.py:1632 _part_rerun_contracts`)
- 失败必须返回稳定错误结构，不只依赖日志排查。(证据: `api_responses.py:error_response` 统一结构)

### 2.6 Schema Version 演进策略【新增】

当前六类 schema 均使用 `{"const": "2026-06-..."}` 硬编码版本标识（证据: `payload_schemas.py` 各 schema 的 `schema_version` 字段）。为保障契约冻结的可持续性，定义以下规则：

| 场景 | 操作 | 兼容策略 |
| --- | --- | --- |
| 新增可选字段 | 不升 version | 旧消费者忽略未知字段；`additionalProperties` 控制严格度 |
| 新增必填字段 | 升 version | 投影层对老 job 缺字段时降级生成 `null` 或默认值，并产出 `quality_signal` 提示 |
| 重命名字段 | 禁止 | 必须保留旧字段作为兼容别名，同时新增语义字段 |
| 删除字段 | 升 version | 旧字段在投影层标记 `deprecated`，至少保留一个版本周期 |
| 改变字段语义 | 禁止 | 必须用新字段名表达新语义 |

版本号格式：`YYYY-MM-{schema-name}`，升级时更新月份并保留旧版本 schema 供 diff。

---

## 3. 差距分析总览矩阵

> 状态校准：2026-06-25 完成质量评估后，P1-P6 已按“受控灰度/内部交付”口径验收；P7 仍为生产运维 hardening 阶段。详细评估见 [parsecore-productization-quality-assessment.md](parsecore-productization-quality-assessment.md)。

### 3.1 P1-P7 状态汇总

| 阶段 | 主题 | 当前状态 | 成熟度 | 验收口径 |
| --- | --- | --- | --- | --- |
| P1 | 契约冻结与宿主接入 | 已完成 | 高 | 可按受控灰度/内部交付验收 |
| P2 | RAG 入库契约 | 主链完成 | 中高 | 可按受控灰度验收，真实样本持续校准 |
| P3 | Provider 生产化治理 | 主链完成 | 中高 | 可按受控灰度验收，候选 Provider 仍需单独准入 |
| P4 | 阅读页排版与诊断 | 后端契约完成 | 高 | 可按后端契约验收，宿主前端需补视觉验收 |
| P5 | 大文件与 part 运维 | 主链完成 | 中高 | 可按功能主链验收，真实大样本与清理策略继续压测 |
| P6 | 发布门禁与质量趋势 | 基础门禁完成 | 中高 | 可按基础门禁验收，长任务进度/超时体验继续增强 |
| P7 | 可观测安全运维 | 已完成 | 高 | P7-T01 至 P7-T10 已按 hardening 口径闭环，进入真实灰度持续观测 |

### 3.2 跨阶段依赖关系

```
P1 (schema 冻结) ──┬──> P2 (KnowledgeUnit 依赖 schema)
                   ├──> P4 (reader block 依赖 schema)
                   └──> P3 (provider registry 依赖 schema)

P2 (KnowledgeUnit) ──> P4 (reader block 共享 KnowledgeUnit 来源)
P3 (provider-suite) ──> P6 (provider 指标趋势依赖 suite)
P5 (part rerun)    ──> P6 (part 指标趋势依赖 rerun)
P6 (门禁)          ──> P7 (运维指标依赖门禁定义)
```

---

## 4. P1：契约冻结与宿主接入准备【近期·具体】

### 4.1 现状与差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P1-T01 六类 schema 冻结 | ✅ | `payload_schemas.py:1828 _SCHEMAS` 字典含 6 项：document-coverage/ir/parts/providers/quality/reader |
| P1-T02 最小/复杂/异常样例 | ✅ | `payload_contract_samples.py` 已有 4 个快照：`build_sample_snapshot`、`build_complex_sample_snapshot`、`build_anomaly_sample_snapshot`、`build_part_rerun_sample_snapshot`；`tools/p1_contract_acceptance.py` 实际验证 24 个 payload 全部通过 schema |
| P1-T03 动作合同规范 | ✅ | `payload_schemas.py:1045 _action_suggestion_schema` 含 action_id/label/method/endpoint/scope/reason_codes/auto_execute/payload/params/context；`user-guide.md` 新增动作合同四阶段文档化（inspect→compare→execute→verify） |
| P1-T04 part rerun monitor/verify contract | ✅ | `api_routes.py:1632 _part_rerun_contracts` 完整实现 monitor_requests/verify_requests/preferred_verify_request/workflow |
| P1-T05 provider comparison 产品字段 | ✅ | `payload_schemas.py:1186 _provider_comparison_schema` 含 schema_version/primary_provider_id/best_provider_id/summary/rankings；`user-guide.md` 新增 provider comparison 产品字段说明 |
| P1-T06 coverage 页级/unit 级消费指南 | ✅ | `payload_schemas.py:149 _coverage_page_schema` 和 `:225 _coverage_unit_schema` 已有完整字段；`user-guide.md` 新增 coverage 消费指南（页级字段表/missing_reason 枚举/单元级字段表/消费建议） |
| P1-T07 schema diff/snapshot 测试 | ✅ | `tests/test_schema_snapshot.py` 含 7 个测试，对六类 schema 的 required/properties/x-parsecore/schema_version 做快照断言 |
| P1-T08 宿主接入顺序 | ✅ | `user-guide.md` 新增“契约接入顺序”章节，含 6 阶段 reader→quality→providers→parts→coverage→ir 接入清单和接入检查清单 |
| P1-T09 前端接入字段清单 | ✅ | `user-guide.md` 新增“前端接入字段清单”章节，含 reader/quality/coverage 三类投影字段清单 |
| P1-T10 API 错误码文档 | ✅ | `user-guide.md` 新增“API 错误码参考”章节，含 27 个错误码集中表（code/status/说明/触发场景） |

### 4.2 待办任务与影响文件

| 任务编号 | 任务 | 影响文件 | 状态 |
| --- | --- | --- | --- |
| P1-T02 | 补复杂/异常/part rerun 样例 | `src/parsecore/payload_contract_samples.py` | ✅ |
| P1-T03 | 动作合同四阶段文档化 | `docs/user-guide.md`、`docs/configuration.md` | ✅ |
| P1-T05 | provider comparison 产品字段说明 | `docs/user-guide.md` | ✅ |
| P1-T06 | coverage 消费指南 | `docs/user-guide.md` | ✅ |
| P1-T07 | 新增 schema snapshot 测试 | `tests/test_schema_snapshot.py`（新建） | ✅ |
| P1-T08 | 宿主接入顺序章节 | `docs/user-guide.md` | ✅ |
| P1-T09 | 前端接入字段清单 | `docs/user-guide.md` | ✅ |
| P1-T10 | 错误码集中表 | `docs/user-guide.md` | ✅ |

### 4.3 字段/接口变更

**P1-T02 样例补强**：在 `payload_contract_samples.py` 新增：
- `build_complex_sample_snapshot()` — 多栏 PDF + 跨页表格 + 嵌入图示 + 目录页
- `build_anomaly_sample_snapshot()` — OCR 降级页 + 低置信读序 + CID garble + 空文本页
- `build_part_rerun_sample_snapshot()` — part rerun 后 previous_part_observation + rerun_comparison

**P1-T07 schema snapshot 测试**：新建 `tests/test_schema_snapshot.py`，对六类 schema 的 `required` 字段名集合做快照断言：
```
EXPECTED_IR_REQUIRED = {"schema_version", "projection", "doc_id", "parse_run_id", ...}
assert set(schema["required"]) == EXPECTED_IR_REQUIRED
```
防止字段被无意重命名或删除。

**P1-T10 错误码集中表**：基于 `api_routes.py` 现有 `_error_response(code=...)` 调用整理，当前已有 25+ 错误码：

| 错误码 | HTTP 状态 | 场景 |
| --- | --- | --- |
| document_not_found | 404 | 文档不存在 |
| job_not_found | 404 | 任务不存在 |
| part_not_found | 404 | Part 不存在 |
| schema_not_found | 404 | Schema 不存在 |
| export_not_found | 404 | 导出文件不存在 |
| invalid_projection | 400 | 不支持的投影 |
| invalid_part_state | 400 | 无效 part 状态过滤 |
| missing_doc_id | 400 | 缺少 doc_id |
| missing_file_path | 400 | 缺少 file_path |
| file_required | 400 | 缺少上传文件 |
| empty_file | 400 | 空文件 |
| quota_exceeded | 429 | 配额超限 |
| too_many_inflight_jobs | 429 | 并发任务超限 |
| document_too_large_for_sync | 413 | 文档过大不适合同步解析 |
| upload_bridge_unauthorized | 401 | 上传桥接鉴权失败 |
| file_path_not_allowed | 403 | 文件路径越界 |
| ... | ... | 完整列表见 `api_routes.py` |

### 4.4 测试要求

| 测试文件 | 覆盖场景 |
| --- | --- |
| `tests/test_schema_snapshot.py`（新建） | 六类 schema required 字段名快照 |
| `tests/test_payload_schemas.py` | 新增样例 payload 符合 schema |
| `tests/test_payload_contract_check.py` | 新增样例通过 contract check |
| `tests/test_api_payloads.py` | 复杂/异常样例 API 投影 |

### 4.5 验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_payload_schemas.py tests/test_payload_contract_check.py tests/test_schema_snapshot.py -q
py -m pytest tests/test_api_payloads.py tests/test_asgi.py -q
py -m parsecore.cli payload-contract-check
py -m parsecore.cli p1-contract-acceptance --out var/self-check/p1-contract-acceptance-YYYYMMDD.json
```

### 4.6 退出条件

P1 完成后，宿主产品不需要阅读 ParseCore 源码即可完成第一版 reader、quality、coverage、providers、parts 接入。具体判定：
- `GET /v1/parse/schemas` 列出所有冻结 schema，名称与文档一致。
- `payload-contract-check` 在本地通过，覆盖 reader、coverage、quality、providers、parts。
- schema snapshot 测试通过，字段重命名会被捕获。
- `user-guide.md` 包含接入顺序、字段清单、错误码表。

### 4.7 2026-07-15 P1 执行证据

本轮执行新增 `p1-contract-acceptance` 门禁，并修正 part-rerun 样例使用运行时真实边界 `partition_parts`，确保 `previous_part_observation`、`rerun_comparison` 和 monitor/verify 诊断不会被通用 fallback 丢弃。

- 结果：8/8 检查通过，6 个冻结 schema、4 组样例、24 个 payload。
- 兼容性：复杂/异常样例的 `compat / structured / full` 共 6 个旧 projection 通过。
- IR/Reader：3 页、10 blocks、2 tables、1 figure、10 KnowledgeUnit；8 个 reader blocks 均可回溯到 IR source block。
- Coverage：10 units、8 个可入库 units，页/单元计数一致，coverage gap=0。
- Action contract：`inspect → compare → execute → verify` 四阶段全部 ready。
- Part rerun：1/1 part 暴露 previous observation + comparison，状态为 `improved`。

机器可读报告：`var/self-check/p1-contract-acceptance-20260715.json`；可读报告：[p1-acceptance-20260715.md](p1-acceptance-20260715.md)。

---

## 5. P2：RAG 入库契约产品化【近期·具体】

### 5.1 现状与差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P2-T01 KnowledgeUnit 字段固化 | ✅ | `payload_schemas.py:742 _knowledge_unit_schema` required 含 unit_id/doc_id/source_block_ids/source_table_ids/page_span/text/unit_type/semantic_role/should_index_for_rag/skip_reason/quality_flags/chunk_ids |
| P2-T02 unit↔chunk 反查 | ✅ | unit 有 `chunk_ids`（`ir.py:601`）；coverage page 有 `unit_ids`+`chunk_ids`（`ir.py:313,322`）；reader block 有 `source_unit_ids`+`rag_chunk_ids`（`test_api_payloads.py:780,781`）；chunk→unit 通过 block_ids→coverage page→unit_ids 查找 |
| P2-T03 embedding 状态字段 | ✅ | `payload_schemas.py` 三个 unit schema（knowledge/coverage/reader）均新增 `embedding_model`/`embedding_state`/`embedding_error_category` 字段，`ir.py:_unit_embedding_state` 根据 should_index/chunk_ids/embedded 计算状态 |
| P2-T04 index_manifest.rag_coverage.units | ✅ | `_knowledge_units_from_runtime_manifest()`（`ir.py:608`）从 manifest 读取 units；`test_api_payloads.py:772` 验证 rag_manifest.units 闭环 |
| P2-T05 skip_reason 枚举 | ✅ | `ir.py:_skip_reason()` 扩展到 10 类：新增 `toc_duplicate`/`diagnostic_text`/`low_confidence_ocr`/`figure_caption_missing` |
| P2-T06 表格 unit RAG 文本来自 cells | ✅ | `_fallback_block_unit_text()`（`ir.py:820`）对 table 调用 `_render_ir_table_text()` 渲染 cells+caption；`test_runtime.py:597` 验证 cells 进入 unit.text |
| P2-T07 图示 unit RAG 文本来自 caption | ✅ | `_fallback_block_unit_text()`（`ir.py:826`）对 image 拼接 text+alt_text；`test_runtime.py:636` 验证 caption/alt_text 进入 unit.text |
| P2-T08 rechunk_document | ✅ | `runtime.py:2152 rechunk_latest` + API 端点 `POST /v1/parse/documents/{doc_id}/rechunk` |
| P2-T09 reembed_document | ✅ | `runtime.py:2160 reembed_latest` + API 端点 `POST /v1/parse/documents/{doc_id}/re-embed` |
| P2-T10 dataset 联动验收样例 | ✅ | `tests/test_exports.py` 新增 `DatasetLinkageValidationTests`（4个测试），验证 coverage unit_ids ↔ parse_units、reader quality_signal_codes ⊆ quality_signals、跨 dataset 页码一致性 |
| P2-T11 RAG 侧接入说明 | ✅ | `user-guide.md` 新增“RAG 侧接入说明”章节，含 KnowledgeUnit 数据流/核心字段/Embedding 状态机/接入检查清单/典型接入代码 |
| P2-T12 质量信号固定 | ✅ | `tests/test_quality.py` 新增 `RagCoverageQualitySignalTests`（7个测试），锁定 5 个 rag_* flag 名称与 gate 映射（accept/manual_review/accept_with_warning） |

### 5.2 待办任务与影响文件

| 任务编号 | 任务 | 影响文件 | 状态 |
| --- | --- | --- | --- |
| P2-T03 | 补 embedding 状态字段 | `src/parsecore/payload_schemas.py`、`src/parsecore/ir.py`、`src/parsecore/api_payloads.py` | ✅ |
| P2-T05 | 扩展 skip_reason 枚举 | `src/parsecore/ir.py` | ✅ |
| P2-T06 | 验证表格 unit RAG 文本 | `src/parsecore/ir.py` | ✅ |
| P2-T07 | 验证图示 unit RAG 文本 | `src/parsecore/ir.py` | ✅ |
| P2-T10 | dataset 联动验收样例 | `tests/test_exports.py` | ✅ |
| P2-T11 | RAG 侧接入说明 | `docs/user-guide.md` | ✅ |
| P2-T12 | 固定 rag_* 质量信号 | `src/parsecore/ir.py`、`tests/test_quality.py` | ✅ |

### 5.3 字段/接口变更

**P2-T03 embedding 状态字段**（新增 3 字段）：

| 字段名 | 类型 | 位置 | 说明 |
| --- | --- | --- | --- |
| `embedding_model` | string (nullable) | `_knowledge_unit_schema` properties | 使用的 embedding 模型标识 |
| `embedding_state` | string | `_knowledge_unit_schema` properties + required | 枚举：`pending`/`embedded`/`failed`/`skipped` |
| `embedding_error_category` | string (nullable) | `_knowledge_unit_schema` properties | 失败类别：`model_unavailable`/`quota_exceeded`/`timeout`/`invalid_dimension`/`unknown` |

影响代码路径：
- `payload_schemas.py:_knowledge_unit_schema` — 新增 3 字段到 properties，`embedding_state` 加入 required
- `payload_schemas.py:_coverage_unit_schema` — 同步新增
- `ir.py` coverage unit 构建逻辑 — 从 `index_manifest` 或 chunk metadata 提取 embedding 状态
- `pipelines.py` — embedding 流程产出状态
- `runtime.py` — reembed 后更新状态

**P2-T05 skip_reason 枚举扩展**（从 3 类扩展到 10 类）：

| skip_reason | 触发条件 | 现有 |
| --- | --- | --- |
| `empty_text` | 文本为空 | ✅ 已有 |
| `semantic_role:header_footer` | 页眉页脚 | ✅ 已有（通过 _SKIP_INDEX_ROLES） |
| `semantic_role:parse_artifact` | 解析工件 | ✅ 已有 |
| `semantic_role:page_ref_cell` | 页码引用 | ✅ 已有 |
| `semantic_role:version_cell` | 版本单元 | ✅ 已有 |
| `index_policy_skip` | 索引策略跳过 | ✅ 已有 |
| `toc_duplicate` | 目录重复项 | ❌ 新增 |
| `diagnostic_text` | 诊断文本 | ❌ 新增 |
| `low_confidence_ocr` | 低置信 OCR | ❌ 新增 |
| `figure_caption_missing` | 图示缺说明 | ❌ 新增 |

影响代码路径：
- `ir.py:_skip_reason()` — 扩展判断逻辑
- `ir.py:_SKIP_INDEX_ROLES` — 可能扩展角色集合
- `pipelines.py` — KnowledgeUnit 构建时传递 skip_reason
- `payload_schemas.py:_knowledge_unit_schema` — skip_reason 字段文档补充枚举值

**P2-T12 质量信号固定**：

| 质量信号 code | 触发条件 |
| --- | --- |
| `rag_empty_text_page` | 正文页无 indexable unit |
| `rag_units_without_chunks` | indexable unit 无 chunk |
| `rag_chunks_not_embedded` | chunk 未 embedding |
| `rag_table_without_unit` | 表格无对应 KnowledgeUnit |
| `rag_figure_caption_missing` | 图示缺 caption |

### 5.4 测试要求

| 测试文件 | 覆盖场景 |
| --- | --- |
| `tests/test_payload_schemas.py` | skip_reason 枚举穷举、embedding_state 枚举穷举 |
| `tests/test_api_payloads.py` | embedding 失败降级场景、skip_reason 展示 |
| `tests/test_exports.py` | dataset=coverage/reader/parse_units 联动 |
| `tests/test_runtime.py` | rechunk 后 coverage 反映改善、reembed 后 embedding_state 更新 |

### 5.5 验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_api_payloads.py tests/test_exports.py tests/test_export_jobs.py -q
py -m pytest tests/test_parts.py tests/test_runtime.py -q
py -m parsecore.cli self-check --config parsecore.toml --profile fast
```

### 5.6 退出条件

P2 完成后，RAG 侧不再需要从 reader 文本、Markdown 或 parser 输出猜 chunk 来源。具体判定：
- 正文页 `text_page_coverage_ratio >= 0.98`，达不到时有页级原因。
- 表格页 `table_unit_coverage_ratio >= 0.95`，达不到时指出缺失表格或 unit。
- `unit_chunk_coverage_ratio >= 0.98`，未 chunk 的 unit 有原因。
- 页眉页脚、页码、解析诊断文本、截图路径不进入 RAG 正文。
- 宿主可展示"哪些页进了 RAG、哪些 unit 没进、为什么没进"。
- 重建 chunks 或 embeddings 后，coverage 反映改善或仍有缺口。

---

## 6. P3：本地 Provider 生产化治理【中期·具体+框架】

### 6.1 已实现能力确认

以下能力已完整实现，附测试指针：

| 能力 | 证据 |
| --- | --- |
| provider registry 字段固化 | ✅ `payload_schemas.py:429 _local_provider_registry_entry_schema` |
| provider provenance | ✅ `payload_schemas.py:384 _provenance_schema` 含 provider_id/provider_version/adapter_version |
| route-plan 排除原因 | ✅ `runtime.py:360 provider_route_plan` 返回 excluded_provider_ids |
| **identity drift 门禁** | ✅ `tests/test_provider_comparison_report.py:264 test_gate_summary_warns_on_provider_identity_drift`、`:304 test_gate_summary_fails_when_provider_identity_drift_budget_is_exceeded`，gate_policy 含 `max_providers_with_multiple_provider_versions`/`max_providers_with_multiple_adapter_versions` |
| **route primary 偏差预算** | ✅ `tests/test_provider_comparison_report.py:231 test_gate_summary_fails_when_best_provider_mismatch_budget_is_exceeded`，gate_policy 含 `max_samples_best_provider_differs_from_route_primary` |
| **admission drift 五类预算** | ✅ `tests/test_provider_comparison_report.py:336` gate_policy 含 `max_providers_with_multiple_provider_versions`/`max_providers_with_multiple_adapter_versions` 等 |
| gate policy 十类预算 | ✅ `tools/provider_comparison_report.py` 实现十类 gate_policy：reading_order_warning/quality_warning/route_primary_mismatch/provider_version_drift/adapter_version_drift/五类 admission_drift |

### 6.2 剩余待办

| 任务编号 | 任务 | 影响文件/模块 | 状态 |
| --- | --- | --- | --- |
| P3-T03 | pymupdf4llm-local 安装 extra 文档化 | `docs/configuration.md` | ✅ |
| P3-T04 | docling-local 失败隔离测试 | `tests/test_docling_parser.py` | ✅ |
| P3-T05 | mineru/paddleocr/marker 离线评估清单 | `docs/local-provider-ir-upgrade-plan.md` | ✅ |
| P3-T07 | provider-suite 覆盖度扩展 | `tools/provider_comparison_report.py`、`var/regression/` | ✅ |
| P3-T08 | recommended_admission 配置回写建议 | `tools/provider_comparison_report.py` | ✅ |
| P3-T09 | 灰度 fallback 测试 | `tests/test_provider_comparison_report.py` | ✅ |
| **P3-T10** | ✅ | `test_runtime.py:2276 test_rerun_pdf_part_uses_provider_route_plan_capabilities` 验证端到端链路：route_plan → job options → parser selection → provider_ids → partition_parts.provider_route_plan |

### 6.3 准入流程框架

```
候选 Provider
    │
    ▼
[评测态] ── provider-suite fast/full/perf
    │         样本/许可证/性能/可观测性 四类门禁
    ▼
[gate pending] ── gate_status = "pending"
    │
    ▼
[gate passed] ── gate_status = "passed", route_ready = true
    │         recommended_admission 产出配置回写建议
    ▼
[route] ── 进入 route-plan primary 或 fallback
           identity drift / 偏差预算 持续监控
```

**准入四类门禁维度**：
1. **样本门禁**：provider-suite fast/full/perf 覆盖普通 PDF、表格 PDF、多栏 PDF、图示 PDF、DOCX
2. **许可证门禁**：开源/商业/内部许可证明确标注
3. **性能门禁**：耗时/峰值内存/MB/s 在预算内
4. **可观测性门禁**：provider_id/version/adapter_version 可追溯，elapsed/memory/reading_order_confidence 可采集

**P3-T05 离线评估清单框架**（mineru-local/paddleocr-local/marker-local）：

| 评估项 | 维度 | 进入默认 route |
| --- | --- | --- |
| 安装成本 | 依赖体积/安装时间/GPU 需求 | 否（仅离线评估） |
| 样本覆盖 | 普通/表格/多栏/图示/DOCX/大样本 | 否 |
| 质量基线 | coverage ratio/reading order warning/quality warning | 否 |
| 性能基线 | 耗时/内存/MB/s | 否 |
| 许可证 | 开源协议/商业限制 | 否 |

以上三个候选 Provider 只做离线评估清单，不进入默认生产 route。

### 6.4 验收标准

- `/v1/parse/providers/route-plan` 能解释 primary、fallback、excluded 和 admission。(✅ 已实现)
- `/providers.comparison_report.summary` 能给出 best provider、primary provider、偏差、warning 和推荐动作。(✅ 已实现)
- provider-suite 能对候选 provider 输出 completed/skipped/failed，且 skipped 原因清晰。(⚠️ 需扩展覆盖度)
- route primary 与 best provider 偏差不超过 suite gate 预算。(✅ 已实现)
- provider identity drift 和 admission drift 在 fast/full/perf 中可被捕获。(✅ 已实现)
- 开启本地路由灰度后，未满足 gate 的候选不会进入实际执行 route。(⚠️ 需补齐测试)
- 复杂 part rerun 可以按 `layout`、`tables`、`figures` 等能力要求重算 provider。(⚠️ 需验证)

### 6.5 验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_provider_comparison_report.py tests/test_provider_provenance.py -q
py -m pytest tests/test_pymupdf4llm_parser.py tests/test_docling_parser.py -q
py tools/provider_comparison_report.py --config parsecore.toml --suite var/regression/provider-suite.fast.json --out-json var/self-check/provider-comparison.fast.json --out-md var/self-check/provider-comparison.fast.md
py tools/self_check.py --config parsecore.toml --profile fast --provider-suite var/regression/provider-suite.fast.json
```

---

## 7. P4：阅读页排版与诊断闭环【近期·具体】

### 7.1 现状与差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P4-T01 reader block 类型 | ✅ | `payload_schemas.py:868 _reader_block_schema` required 含 type/display_kind/reader_policy |
| P4-T02 reader_policy | ✅ | `ir.py:1406 _reader_policy` 返回 inline/hidden/table/source_snapshot |
| P4-T03 表格 reader block | ✅ | `_reader_block_schema` properties 含 table（generic_object） |
| P4-T04 图示 reader block | ✅ | `_reader_block_schema` properties 含 figure（generic_object） |
| P4-T05 quality_signal_codes | ✅ | `_reader_block_schema` required 含 quality_signal_codes |
| P4-T06 visible/hidden block count | ✅ | `payload_schemas.py:825 _reader_page_schema` required 含 reader_block_count/hidden_block_count |
| P4-T07 读序低置信下钻 | ✅ | `tests/test_api_payloads.py` 新增 `ReadingOrderConfidenceDrillDownTests`（8个测试），验证 reader_page → quality_signal_codes → coverage_missing_reason → provider_ids → provider_provenance 完整下钻链 |
| P4-T08 dataset=reader 导出 | ✅ | `exports.py:16 EXPORT_DATASETS` 含 reader；`api_routes.py:1040` export_document 处理 reader dataset |
| P4-T09 最小渲染协议 | ✅ | `user-guide.md` 新增“Reader 最小渲染协议”章节，含渲染流程/质量提示与诊断层/字段映射参考 |
| P4-T10 视觉抽检样本清单 | ✅ | `user-guide.md` 新增“视觉抽检样本清单”章节，含样本矩阵/抽检流程/通过标准 |

### 7.2 待办任务与影响文件

| 任务编号 | 任务 | 影响文件 | 状态 |
| --- | --- | --- | --- |
| P4-T07 | 读序低置信下钻路径验证 | `src/parsecore/ir.py`、`src/parsecore/api_payloads.py`、`tests/test_api_payloads.py` | ✅ |
| P4-T09 | 最小渲染协议文档 | `docs/user-guide.md` | ✅ |
| P4-T10 | 视觉抽检样本清单 | `docs/user-guide.md`、`var/fixtures/` | ✅ |

### 7.3 字段/接口变更

**P4-T09 最小渲染协议**（文档输出，无代码变更）：

```
宿主阅读页最小渲染协议：
1. 按 page_number 分组 reader blocks
2. 按 reading_order 排序 page 内 blocks
3. reader_policy = "hidden" 的 block 不进正文（页眉页脚/页码/解析工件）
4. reader_policy = "table" 的 block 渲染结构化表格（使用 table.cells/header/caption）
5. reader_policy = "source_snapshot" 的 block 渲染图示（使用 figure.caption/alt_text）
6. reader_policy = "inline" 的 block 渲染段落文本
7. quality_signal_codes 非空的 block 在局部展示质量提示
8. diagnostic 类信息进入提示层，不混入正文
```

**P4-T10 视觉抽检样本清单**：

| 样本类型 | 覆盖目的 | 来源 |
| --- | --- | --- |
| 多栏 PDF | 读序低置信检测 | 固定 fixture |
| 表格 PDF | 表格 block 结构化渲染 | 固定 fixture |
| 标题层级 PDF | heading block 类型验证 | 固定 fixture |
| 图示 PDF | figure block caption/alt_text | 固定 fixture |
| 页眉页脚 PDF | hidden block 过滤 | 固定 fixture |
| 目录重复项 PDF | skip_reason: toc_duplicate | 固定 fixture |

### 7.4 测试要求

| 测试文件 | 覆盖场景 |
| --- | --- |
| `tests/test_api_payloads.py` | reader 投影 table/figure/hidden block 结构 |
| `tests/test_exports.py` | dataset=reader 导出包含 reader blocks |
| `tests/test_asgi.py` | reader 端点返回稳定结构 |

### 7.5 验证命令

```powershell
$env:PYTHONPATH='src'
py -m pytest tests/test_api_payloads.py tests/test_exports.py tests/test_export_jobs.py -q
py -m pytest tests/test_asgi.py -q
```

### 7.6 退出条件

P4 完成后，阅读页排版问题的主修复路径回到 IR/reader/provider，而不是继续在前端添加 Markdown 补丁。具体判定：
- 宿主阅读页可默认只消费 `projection=reader` 渲染正文、表格和图示。
- 表格 block 保留 cells/header/caption，不退化为普通 paragraph。
- 多栏样本中读序低置信能产生 signal，并可在 quality/provider 中定位。
- 页眉页脚、页码、解析工件默认不进入正文 blocks。
- 图示缺说明时，reader 和 coverage 都能给出质量提示。
- `dataset=reader` 可用于离线排版质量抽检。

---

## 8. P5：大文件与 part 运维产品化【远期·框架】

### 8.1 目标

让大 PDF、复杂页段、局部异常可以通过 part 化运行和运维闭环稳定处理，不依赖整文档反复重跑。

### 8.2 现状差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P5-T01 默认 part 策略 | ✅ | `pdf_parts.py:16 plan_pdf_parts` 新增 3 个决策因子：`file_size_bytes`(>100MB 减半)、`ocr_page_ratio`(>0.3 切换 ocr_heavy)、`historical_failure_rate`(>0.2 减半)；7 个新测试覆盖正向/边界/兼容性 |
| P5-T02 /parts/plan dry-run | ✅ | `pdf_parts.py:16 plan_pdf_parts` 纯函数返回建议不创建 job；`test_pdf_parts.py` 新增 3 个测试验证 dry-run/幂等/无依赖 |
| P5-T03 part 状态机 | ✅ | `parts.py:8 PART_STATE_FILTERS` 9 种状态：pending/parsing/structuring/embedding/done/failed/cancelled/warning/partial |
| P5-T04 限流/claim/超时/取消 | ✅ | `runtime.py:705 claim_job` + `recover_timed_out_jobs` + `cancel_pdf_part` + `_schedule_retry` 完整实现；`test_runtime.py` 已有超时恢复/取消/claim 保护测试 |
| P5-T05 previous_part_observation | ✅ | `runtime.py:1078 _current_part_observation` 已实现 |
| P5-T06 批量 rerun 过滤 | ✅ | `runtime.py:1106 rerun_pdf_parts` 支持 failed_only/state/part_ids/profile/provider_route_plan |
| P5-T07 rerun monitor/verify/workflow | ✅ | `api_routes.py:1632 _part_rerun_contracts` 完整实现 |
| P5-T08 part 结果合并 | ✅ | `contracts.py:135 replace_document_views_by_prefix` + `:146 replace_blocks_by_prefix` + `:155 replace_chunks_by_prefix` |
| P5-T09 part 级 coverage 回填 | ✅ | `runtime.py:1410 refresh_partitioned_parent` + `replace_blocks_by_prefix` + `replace_chunks_by_prefix` + `replace_document_views_by_prefix` 完整实现；`test_runtime.py:2092` 已有 prefix 替换测试 |
| P5-T10 part 清理策略 | ✅ | `config.py` 新增 `part_artifact_retention_seconds`/`export_artifact_retention_seconds`；`parts.py` 新增 `cleanup_artifacts()` 支持 dry-run |
| P5-T11 大文件导出筛选 | ✅ | `exports.py:export_structured_projection` 支持 `page_start`/`page_end`/`quality_signal` 三个筛选参数；`_filter_by_page_range` + `_filter_by_quality_signal` 辅助函数；8 个新测试覆盖 records/parse_units/quality_signals/reader 数据集 |
| P5-T12 真实大样本 benchmark | ✅ | `tools/large_pdf_stress.py` 新增 `evaluate_gate` 函数支持 threshold 门禁判定；`var/regression/large-pdf-benchmark.config.json` 基准配置；`tools/self_check.py` 支持 `--large-pdf-benchmark` 可选参数；6 个新测试覆盖门禁通过/失败/空阈值/缺失指标场景 |

### 8.3 框架性待办

| 任务编号 | 任务 | 影响模块 | 方向 |
| --- | --- | --- | --- |
| P5-T01 | 完善 part 策略决策因子 | `runtime.py` | 增加历史失败信号、OCR 重页密度作为 part 化建议输入 ✅ |
| P5-T02 | dry-run 能力验证 | `tests/test_pdf_parts.py` | 确认 plan 端点不创建子 job 时只返回拆分建议 ✅ |
| P5-T04 | 软超时与失败重试 | `runtime.py`、`worker.py` | 定义软超时阈值、取消后父文档状态不悬挂 ✅ |
| P5-T09 | part 级 coverage 回填 | `ir.py`、`api_payloads.py` | 验证 part rerun 后父文档 coverage projection 局部更新 ✅ |
| P5-T10 | part 清理策略 | `runtime.py`、`config.py` | 定义 part PDF/export 包/comparison 工件保留期，支持 dry-run 清理 ✅ |
| P5-T11 | 大文件导出筛选 | `exports.py`、`api_routes.py` | 支持按页段、质量信号筛选 parse_units/records ✅ |
| P5-T12 | 真实大样本 benchmark | `tools/large_pdf_stress.py` | 复用 17100 页真实样本作为专项门禁，非另造 |

### 8.4 验收方向

- 单 part rerun 后，父文档对应页段被替换，其他页段不变。
- 批量 rerun 能跳过不符合条件的 part，并说明 skipped reason。
- rerun 后可通过 monitor contract 追踪 job，通过 verify contract 回到 `/parts`、`/quality`、`/coverage`。
- part summary 能汇总 warning、failed、provider changed、rerun status、coverage gap。
- 大文件 part 模式下内存峰值和单次执行时间进入可控范围。
- cancel、timeout、worker restart 不造成父文档状态永久悬挂。
- 临时 part PDF 和旧 job artifact 不会无限保留。

---

## 9. P6：发布门禁与质量趋势【远期·框架】

### 9.1 目标

让 ParseCore 发布从"测试通过"升级为"质量趋势、Provider 准入、RAG 覆盖、性能稳定"共同通过。

### 9.2 现状差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P6-T01 self-check 三档 | ✅ | `tools/self_check.py` fast/full/perf，含超时配置（fast=900s/full=4200s/perf=4200s） |
| P6-T02 provider comparison 纳入 self-check | ✅ | `tools/self_check.py` 支持 `--provider-suite` 可选参数；CheckResult 包含标准化 JSON 结构（name/sample_count/output_json_path/output_md_path）；`docs/self-check-gate.md` 新增 Provider Comparison Check 章节 |
| P6-T03 payload-contract-check 必跑 | ✅ | `src/parsecore/cli.py:45` 已有 `payload-contract-check` 子命令 |
| P6-T04 coverage 指标趋势 | ✅ | `tools/coverage_trend_report.py` 新增 coverage 趋势门禁（text_page_coverage_ratio/table_unit_coverage_ratio/unit_chunk_coverage_ratio） |
| P6-T05 reader 指标趋势 | ✅ | `tools/coverage_trend_report.py` 新增 reader 趋势门禁（visible/hidden/table block count/reading_order_confidence） |
| P6-T06 provider 指标趋势 | ✅ | 十类 gate_policy 已实现（identity_drift/best_provider_mismatch/admission_drift/reading_order_warning/quality_warning 等），证据：`tests/test_provider_comparison_report.py:848 test_build_report_exposes_suite_gate_policy` |
| P6-T07 性能趋势 | ✅ | `tools/perf_trend_report.py` 支持整体耗时、进程遥测和阶段 P50 的跨版本趋势；阶段/RSS 只在严格同通道时比较并保持 observation-only，缺失阶段显式列出 |
| P6-T08 gate policy 按样本/provider/profile | ✅ | provider 维度已有十类预算；样本/profile 维度需扩展 |
| P6-T09 release notes 质量指标 | ✅ | `docs/release-notes.md` 新增“质量指标变化记录”章节，含记录格式/采集方法/变化判定规则 |
| P6-T10 回滚触发条件 | ✅ | `docs/go-live-readiness.md:85` 新增“回滚条件量化阈值”表（7 项指标）和“回滚判定流程”（4 步） |

### 9.3 框架性待办

| 任务编号 | 任务 | 影响模块 | 方向 |
| --- | --- | --- | --- |
| P6-T04 | coverage 指标趋势门禁 | `tools/coverage_trend_report.py` | text_page_coverage_ratio/table_unit_coverage_ratio/unit_chunk_coverage_ratio 跨版本趋势 ✅ |
| P6-T05 | reader 指标趋势门禁 | `tools/coverage_trend_report.py` | visible block count/hidden block count/table block count/reading order warning 跨版本趋势 ✅ |
| P6-T07 | 性能趋势完善 | `tools/perf_trend_report.py` | 耗时/峰值内存/MB/s/part throughput/export throughput 跨版本比较 |
| P6-T09 | release notes 质量规范 | `docs/release-notes.md` | 每次发布记录 reader/coverage/provider/performance 关键变化 ✅ |
| P6-T10 | 回滚触发条件质量化 | `docs/go-live-readiness.md`、`docs/self-check-gate.md` | 定义 schema 破坏/质量退化率/provider drift/性能超预算/关键样本失败的量化阈值 ✅ |

### 9.4 验收方向

- `self-check fast` 可作为普通发布前必跑门禁。
- `self-check full/perf` 可作为夜间或候选版本门禁。
- Provider suite 失败能导致非零退出码，并指出失败样本和 provider。
- payload schema 破坏能在 contract check 阶段被捕获。
- 发布说明包含 reader、coverage、provider、performance 的关键变化。
- coverage/reader 指标退化有趋势报告和回滚口径。

---

## 10. P7：可观测、安全与运维交付【远期·框架】

### 10.1 目标

让 ParseCore 进入真实灰度和生产时可排障、可审计、可回滚。

### 10.2 现状差距

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P7-T01 request/job/doc/part/tenant id 关联 | ✅ | `src/parsecore/events.py` 的 `JobEventLogger / ParseStageTimer` 已支持 `job_id / doc_id / part_id / tenant_id` |
| P7-T02 解析阶段耗时指标 | ✅ | `events.py` 新增 `ParseStageTimer`，`api_responses.py` 新增 `PARSE_STAGES` 元组（upload/parse/normalize/chunk/embed/export/rerun） |
| P7-T03 质量指标 | ✅ | 完成态解析从当前 blocks/index manifest 写入轻量脱敏 `document_quality` 事件；固定 gate/flag 计数及 quality/coverage/embedding/provider warning 全局摘要由 Prometheus 暴露，分片父文档只在 DONE 后观测 |
| P7-T04 provider 指标 | ✅ | `provider_failures.py` 统一固定失败类别；embedding 入库、查询向量化与 rerank 终态失败写入脱敏事件，并由 `parse_provider_failure_total{provider_type,provider_id,failure_category}` 聚合；Provider comparison 复用同一分类器 |
| P7-T05 错误分类 | ✅ | `api_responses.py` 新增 `ERROR_CATEGORIES`（9 类）和 `error_category_for_code()` 映射函数 |
| P7-T06 API key/日志脱敏 | ✅ | `src/parsecore/events.py` 写入事件前会脱敏 api_key/token/authorization/secret/password 等敏感字段 |
| P7-T07 文件路径校验/临时目录隔离 | ✅ | `private_files.py` 统一根目录边界、独占写入、安全扩展名和 POSIX 0700/0600；同步上传、桥接暂存、导出与 PDF part 已接入，路径逃逸和清理有回归覆盖 |
| P7-T08 工件保留期 | ✅ | `config.py` 已有 staged/part/export retention 配置；`parsecore cleanup-provider-comparison-artifacts` 使用 `provider_comparison_artifact_retention_seconds`，默认 dry-run 且仅清理 self-check 生成的 Provider comparison 报告 |
| P7-T09 运维面板字段说明 | ✅ | `docs/configuration.md` 已补最小运维面板字段说明 |
| P7-T10 灰度回滚手册 | ✅ | `docs/gray-deployment.md` 已补 local parser routing、provider 配置、候选 profile、reader 降级专项回滚口径 |

### 10.3 框架性待办

| 任务编号 | 任务 | 影响模块 | 方向 |
| --- | --- | --- | --- |
| P7-T01 | id 关联完善 | `events.py`、`runtime.py` | part_id/tenant_id 在日志和事件中关联 ✅ |
| P7-T02 | 解析阶段耗时指标 | `runtime.py`、`events.py` | upload→parse→normalize→chunk→embed→export→rerun 各阶段耗时采集 ✅ |
| P7-T03 | 质量指标系统化 | `runtime.py`、`api_routes.py` | 完成态 `document_quality` 事件、固定 gate/flag 计数、质量与 coverage 全局摘要；report-only 语义和既有阈值保持不变 ✅ |
| P7-T04 | Provider 失败类别与聚合 | `provider_failures.py`、`runtime.py`、`provider_comparison_report.py` | 固定低基数类别、终态失败事件、Prometheus 聚合、fallback 语义不变 ✅ |
| P7-T05 | 错误分类系统化 | `api_responses.py`、`runtime.py` | 定义错误类别枚举，稳定错误码 ✅ |
| P7-T06 | 日志脱敏强化 | `events.py`、`api_support.py` | API key/token/authorization/secret/password 不进事件日志 ✅ |
| P7-T07 | 临时目录隔离强化 | `private_files.py`、`api_routes.py`、`export_jobs.py`、`pdf_parts.py` | 上传文件名/扩展名净化、受控根目录、独占创建、私有权限与同步清理 ✅ |
| P7-T08 | 工件保留期 | `parts.py`、`config.py`、`cli.py` | part 文件/export 包/comparison 工件保留期和清理策略，支持 dry-run；comparison 仅匹配 self-check 输出 ✅ |
| P7-T09 | 运维面板字段 | `docs/configuration.md` | 最小运维面板字段说明 ✅ |
| P7-T10 | 专项回滚手册 | `docs/gray-deployment.md` | local parser routing 关闭/provider 配置回退/候选 profile 关闭/reader 降级接入 ✅ |

### 10.4 验收方向

- 任意一次解析失败可通过 job id 定位阶段、错误类别和主要原因。
- 任意一次 provider fallback 可解释为什么选中或排除候选。
- 临时上传、part 文件、export 工件不会无限保留。
- API key 和敏感配置不进入日志。
- 灰度出问题时，可通过配置关闭本地路由并回到默认 provider。

---

## 11. 跨阶段任务优先队列

建议近期按下面顺序启动：

1. **P1-T07 + P1-T02**：先建 schema snapshot 测试和补样例，降低契约被无意破坏的风险。 ✅
2. **P1-T08 + P1-T09 + P1-T10**：输出宿主接入顺序、字段清单、错误码表，降低接入成本。 ✅
3. **P2-T03 + P2-T05**：补 embedding 状态字段和扩展 skip_reason 枚举，避免 RAG 链路继续猜来源。 ✅
4. **P4-T09 + P4-T10**：输出最小渲染协议和视觉抽检样本清单，支撑阅读页替换 Markdown 补丁。 ✅
5. **P3-T07 + P3-T09**：扩展 provider-suite 覆盖度和灰度 fallback 测试。 ✅
6. **P5-T10 + P5-T12**：建立 part 清理策略和真实大样本 benchmark。 ✅（P5-T10）
7. **P6-T04 + P6-T05**：建立 coverage/reader 指标趋势门禁。 ✅
8. **P7-T02 + P7-T05**：补齐解析阶段耗时和错误分类。 ✅

---

## 12. Definition of Ready / Definition of Done

### 12.1 Definition of Ready

进入开发前，每个任务至少满足：

- 已明确影响的 endpoint、projection、schema 或配置项。
- 已明确是否影响旧投影兼容。
- 已明确需要新增或更新的测试文件。
- 已明确是否需要更新 `configuration.md`、`user-guide.md`、`self-check-gate.md` 或本文。
- 已明确样本来源：单元构造样本、固定 fixture、真实大样本或离线 benchmark。
- 已明确是否涉及可选依赖、许可证、部署成本或性能预算。

### 12.2 Definition of Done

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

---

## 13. 阶段验收矩阵

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

---

## 14. 当前进展与下一批次（活动章节）

> 本节随迭代更新，非活动章节保持稳定。

### 14.1 当前状态（截至 SPEC 创建日）

| 阶段 | 成熟度 | 近期可启动任务 |
| --- | --- | --- |
| P1 | 高（schema/样例/接入文档已补齐） | 持续维护 schema snapshot 和 payload contract |
| P2 | 中高（KnowledgeUnit/RAG coverage 主链已完成） | 真实样本持续校准 skip_reason 与 embedding 状态 |
| P3 | 中高（gate_policy/provider suite 主链已完成） | 扩展真实候选 Provider 准入和 provider-suite smoke 模式 |
| P4 | 高（reader block 与渲染协议已补齐） | 宿主前端截图级视觉验收 |
| P5 | 中高（part 核心与清理基础已完成） | 真实大样本 benchmark 和容量清理演练 |
| P6 | 中高（self-check/趋势/进度输出已补齐） | 长任务超时上下文和 PR 级 smoke 门禁 |
| P7 | 高（P7-T01 至 P7-T10 已闭环：阶段耗时、质量与 Provider 指标、API 运维演练、错误分类、事件脱敏、临时目录隔离、保留期与回滚文档均已补齐） | 真实灰度持续抓取与趋势观察；不新增无依据阈值 |

### 14.2 下一批次建议

第一批（已完成）：
1. P1-T07 新增 `tests/test_schema_snapshot.py`
2. P1-T02 补 `payload_contract_samples.py` 复杂/异常/part rerun 样例
3. P1-T08/T09/T10 更新 `user-guide.md` 接入顺序、字段清单、错误码表
4. P2-T03 补 embedding 状态字段（`embedding_model`/`embedding_state`/`embedding_error_category`）
5. P2-T05 扩展 skip_reason 枚举
6. P4-T09 输出最小渲染协议文档

第二批（已完成）：
7. P3-T07 扩展 provider-suite 样本覆盖度 ✅
8. P3-T09 补齐灰度 fallback 测试 ✅
9. P4-T10 建立视觉抽检样本清单 ✅
10. P2-T11 输出 RAG 侧接入说明 ✅

第三批（已完成/持续演练）：
11. P5-T10 part 清理策略 ✅
12. P6-T04/T05 coverage/reader 指标趋势门禁 ✅
13. P7-T02/T05 解析阶段耗时和错误分类 ✅
14. P7-T01/T06 事件 tenant/part 关联与敏感字段脱敏 ✅
15. P7-T09/T10 运维面板字段与专项回滚手册 ✅
