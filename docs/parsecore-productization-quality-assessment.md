# ParseCore P1-P7 产品化完成质量评估

评估日期：2026-06-25

## 1. 评估结论

综合判断：P1-P6 已达到“受控灰度/内部交付”质量，P7 已从“框架完成”提升到“基础运维能力可用、生产 hardening 继续补强”质量。整体完成度高，可以进入宿主联调和受控灰度；生产长期运行前仍建议补真实样本报告和持续运维演练。

建议评级：88 / 100

交付口径：

- 可以对外说：ParseCore 的 reader、coverage、quality、provider、parts、payload schema、provider comparison、self-check 基础门禁已经形成完整产品化主链。
- 可以对内说：P7 已补齐事件日志敏感字段脱敏、tenant/part 关联、运维面板字段说明和专项回滚口径。
- 谨慎表述：P7 仍需要生产环境持续演练，尤其是真实容量清理、provider-suite 长任务、事故回放和长期指标趋势。
- 不建议表述：已完成所有真实样本和长期生产运营验证。

## 2. 本次验证结果

已运行并通过：

```text
tests/test_payload_schemas.py tests/test_payload_contract_check.py tests/test_schema_snapshot.py tests/test_api_payloads.py tests/test_asgi.py
=> 110 passed, 42 subtests passed

tests/test_provider_comparison_report.py tests/test_provider_provenance.py tests/test_pymupdf4llm_parser.py tests/test_docling_parser.py
=> 34 passed

tests/test_parts.py tests/test_pdf_parts.py tests/test_runtime.py tests/test_exports.py tests/test_export_jobs.py
=> 124 passed

tests/test_self_check.py tests/test_parse_perf_baseline.py tests/test_coverage_trend_report.py tests/test_perf_trend_report.py tests/test_stage_timer.py tests/test_large_pdf_stress.py tests/test_config_parser_options.py
=> 65 passed, 9 subtests passed

py -m parsecore.cli payload-contract-check
=> passed, 6 schemas / 6 payloads

py -m parsecore.cli describe --config parsecore.toml
=> passed

py -m parsecore.cli self-check --config parsecore.toml --skip-regression --skip-provider-comparison
=> status=ok, 514 tests passed, 7 skipped
```

专项说明：

```text
tools/provider_comparison_report.py --suite var/regression/provider-suite.fast.json
```

该命令曾运行超过 2 分钟没有中间进度输出，因此被记录为门禁体验问题。后续已修复：`tools/provider_comparison_report.py` 新增 `--progress`，按样本向 stderr 输出进度；`tools/self_check.py` 调 provider comparison 时默认带 `--progress`。仍建议在真实样本环境重新跑 provider-suite fast/full/perf，形成可归档报告。

## 3. 阶段评分

| 阶段 | 评分 | 结论 |
| --- | ---: | --- |
| P1 契约冻结与宿主接入 | 90 | 已基本完成，schema、样例、API 和 contract check 质量较高 |
| P2 RAG 入库契约 | 84 | 主链完成度高，KnowledgeUnit/coverage 已形成闭环，但真实 embedding 与长尾 skip reason 仍需持续验证 |
| P3 Provider 生产化治理 | 82 | registry、route-plan、provider comparison、admission 基础扎实，但 provider-suite 长任务体验和重候选准入仍需补强 |
| P4 阅读页排版与诊断 | 88 | reader 投影、表格/图示/quality signal 主链较完整，仍缺真实前端视觉验收闭环 |
| P5 大文件与 part 运维 | 83 | part rerun、comparison、monitor/verify contract 和导出覆盖较好，真实大样本与清理策略需生产压测 |
| P6 发布门禁与质量趋势 | 86 | 基础 self-check、payload contract、趋势测试可用，provider comparison 已补进度输出 |
| P7 可观测、安全与运维 | 78 | 已补日志脱敏、tenant/part 事件关联、运维面板字段和专项回滚；真实生产演练仍需继续 |

## 4. 质量亮点

### 4.1 契约链路比较完整

`payload-contract-check` 已验证 6 类 schema 和 6 类样例 payload，覆盖 `document-coverage / document-ir / document-parts / document-providers / document-quality / document-reader`。这说明 P1 的契约冻结不是只写文档，而是已有工具化门禁。

### 4.2 API 和运行期回归覆盖强

代表性 API、runtime、parts、exports、provider、self-check、trend 测试合计通过 333 个 pytest 用例，再加 self-check 内部 514 个 unittest。对当前变更规模来说，测试基线比较扎实。

### 4.3 Part rerun 已形成产品闭环

单 part 和批量 rerun 已具备 previous observation、rerun comparison、monitor_requests、verify_requests、preferred_verify_request 和 workflow。宿主产品可以直接按 `monitor -> verify` 接入，不必自己拼 follow-up 跳转。

### 4.4 Provider 路线方向正确

Provider registry 默认仍保持保守：内置主链 route-ready，候选 provider 保持 evaluate/pending，不会在默认配置下全量替换生产解析。这个设计符合“本地 Provider 可灰度，不冒进替换主链”的产品原则。

### 4.5 Reader/RAG/coverage 已同源化

reader、coverage、parse_units、parts 都开始共享 KnowledgeUnit 和 coverage gap 语义。排版质量、RAG 入库覆盖和局部复跑不再是三套互不相干的诊断链路。

## 5. 主要问题

### 5.1 文档状态曾存在冲突（已修复）

`parsecore-productization-todo.md` 仍保留大量未勾选项；`parsecore-productization-spec.md` 声明取代 todo，但它自身前后也存在状态不一致。例如 P7 在汇总矩阵中仍有未实现项，后续又出现部分“已完成”描述；`parsecore-remaining-todo-spec.md` 还保留若干待办状态。

影响：接入方或测试人员会困惑到底以哪份文档作为验收来源。

修复：`parsecore-productization-spec.md` 已校准 P1-P7 状态，`parsecore-remaining-todo-spec.md` 已标记为历史执行稿，`parsecore-productization-todo.md` 保持历史快照口径，README 也补充了评估报告入口。

### 5.2 P7 仍需生产 hardening

已修复：

- `JobEventLogger / ParseStageTimer` 已支持 `tenant_id` 和 `part_id` 事件关联。
- 事件日志写入前已脱敏 `api_key / token / authorization / secret / password` 等敏感字段。
- `configuration.md` 已补最小运维面板字段说明。
- `gray-deployment.md` 已补 local parser routing、Provider 配置、profile、reader、part rerun 专项回滚口径。

仍需生产演练：

- quality/provider 运行指标要在真实环境确认看板聚合效果。
- provider failure category 和工件保留调度仍建议继续细化。
- 临时目录隔离、容量清理、事故回放需要灰度期验证。

影响：受控灰度可以推进，但生产长期运行和事故排查风险偏高。

### 5.3 Provider suite 长任务体验曾不足（已补进度）

provider-suite fast 单独执行时曾超过 2 分钟没有中间输出。本次修复已让 provider comparison 支持 `--progress`，self-check 自动打开进度。

影响：CI 上可能表现为“看似卡死”；人工验收时难以判断是正常长任务、样本缺失、依赖卡住还是实际失败。

后续建议：

- provider-suite 增加更短的 smoke 模式，专门用于 PR 快速门禁。
- 超时时尽量输出当前样本、provider、阶段和已完成样本数。

### 5.4 真实样本验收仍需区分“测试通过”和“质量达标”

当前单测和 contract 门禁非常好，但复杂 PDF、扫描件、跨页表格、17000 页级大样本仍需要真实样本报告支撑最终质量判定。

影响：代码质量可验收，但业务效果还需要固定样本指标证明。

建议：把真实样本报告纳入 release notes，每次记录 coverage、reader、provider、performance 指标变化。

## 6. 阶段细评

### P1：契约冻结与宿主接入

完成质量：高。

证据：

- 6 类 payload schema 已通过 contract check。
- 2026-07-15 新增并执行 `p1-contract-acceptance`：4 组样例共 24 个 payload 全部通过 schema，复杂/异常样例的旧 projection 兼容检查通过。
- IR→Reader source block 可追溯、coverage 页/单元计数一致、动作合同四阶段和 part-rerun comparison 均通过自动化门禁。
- API 测试和 schema snapshot 测试通过。
- `describe` 能暴露 payload schema registry。

扣分点：

- 文档仍有多个状态源，需统一权威说明。

验收建议：可以按完成验收。

### P2：RAG 入库契约

完成质量：中高。

证据：

- KnowledgeUnit、rag_coverage、coverage projection、reader/rag_text 已形成同源关系。
- 测试覆盖 API payload、exports、runtime 和 parts。

扣分点：

- embedding 状态字段和真实 embedding 链路仍需用固定样本持续验证。
- skip_reason 枚举虽有扩展，但真实复杂样本上要继续观察误入库和漏入库。

验收建议：可以按主链完成验收，后续以真实样本指标继续加固。

### P3：Provider 生产化治理

完成质量：中高。

证据：

- provider registry、route-plan、provider provenance、provider comparison 测试通过。
- 默认配置没有让候选 provider 直接进入生产 route。

扣分点：

- provider-suite fast 的人工执行体验不够好，本次未拿到完整运行结果。
- mineru/paddleocr/marker 等重候选更多还是评估清单，不是生产可用 provider。

验收建议：基础治理完成；候选 provider 进入生产前仍需单独准入验收。

### P4：阅读页排版与诊断

完成质量：高。

证据：

- reader projection、reader schema、reader export 和 quality signal 回填均有测试覆盖。
- user guide 已补最小渲染协议和抽检建议。

扣分点：

- 未在本次评估中验证真实前端视觉结果。
- 多栏、复杂表格和图示样本仍应做截图级人工验收。

验收建议：后端契约可以验收；宿主前端接入后需补视觉验收。

### P5：大文件与 part 运维

完成质量：中高。

证据：

- part/runtime/export 回归 124 个测试通过。
- rerun contract、previous observation、comparison、批量 rerun、导出筛选都有覆盖。

扣分点：

- 真实大样本 benchmark 本次未执行。
- part/export/provider 工件保留期在文档中仍有状态冲突，需要确认最终实现和生产配置。

验收建议：功能主链可以验收；生产前补真实大样本和清理策略验收。

### P6：发布门禁与质量趋势

完成质量：中高。

证据：

- 基础 self-check 通过，实际跑了 514 个 unittest。
- payload contract、coverage trend、perf trend、large-pdf stress 相关测试通过。

扣分点：

- provider-suite 长任务缺少进度输出。
- 本次未完成 provider-suite fast 实跑报告。

验收建议：基础门禁可用；发布门禁体验需要继续收口。

### P7：可观测、安全与运维

完成质量：中高。

证据：

- 已有 API key 配置、统一错误响应、阶段耗时、事件脱敏、tenant/part 关联和 metrics 基础。
- `describe`、self-check、运维面板字段说明和专项回滚文档可提供灰度运营支撑。

扣分点：

- 生产排障、清理调度、provider failure category 和真实看板效果仍需继续演练。

验收建议：可按受控灰度运维基础验收；生产长期运行前继续作为 hardening 专项。

## 7. 最终建议

建议把当前状态定义为：

```text
P1-P6：产品化主链完成，达到受控灰度质量。
P7：基础运维能力可用，生产 hardening 继续推进。
整体：可以进入宿主联调和受控灰度，但真实样本和长期生产运营仍需持续验证。
```

下一步优先修复：

1. 用真实样本重新跑 provider-suite fast/full/perf 和 large-pdf benchmark，形成 release note 可引用的质量报告。
2. 为 provider-suite 增加 PR 级 smoke 模式和更细的超时上下文。
3. 继续补齐 provider failure category、工件保留调度和容量清理演练。
4. 宿主前端接入 reader 后，做一次截图级视觉验收，确认复杂表格、图示、多栏读序表现。
