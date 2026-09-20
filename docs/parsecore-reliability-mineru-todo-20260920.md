# ParseCore 现状核查与可靠交付、MinerU 接入待办

日期：2026-09-20  
核查基线：`main @ 14cc7f2`，包版本 `0.1.0`；开始核查时工作区干净。  
状态：方案已形成，以下研发待办尚未实施。

本次依据用户提供的 MinerU / JXZTtool 对比方案，核查当前源码、配置、历史规格与测试，并进行临时 SQLite 环境下的故障注入。本文作为**本轮改造的优先级与验收入口**；既有 `parsecore-productization-spec.md` 继续说明已交付接口，不应将其中的历史“完成”状态解释为本文新增可靠性约束已经完成。

## 1. 判断与执行顺序

保留 ParseCore 的作业管理、part 恢复、领域语义、字段证据及下游交付能力；将 MinerU 作为独立部署、可替换、可评测的解析 Provider。

当前首要问题是**正式结果的可信交付**。本地已复现占位解析被标记完成、视图写入失败仍为 DONE、失败重跑混读新旧结果三个问题。单纯接入更强的解析器无法解决这些问题。

推荐顺序：

1. **P0：收紧真实执行与发布约束。** 明确必需能力，建立不可变产物快照、持久化校验结论和统一发布入口。
2. **P1：接入 MinerU 并收敛事实模型。** 独立推理环境，保留结构与素材，先旁路双跑，再按场景准入；同时明确并发配置。
3. **P2：领域能力产品化。** 将已有 CAAC v1/v2、手册语义、证据校验整理为独立版本的领域包。

调整原方案的一点：**最小发布回归门禁提前到 P0**。真实样本正确性评测在 P1 准入前完成，不能等领域包整理结束再补。

## 2. 现状核查：已有能力与实际缺口

下表源码位置均对应上述提交。标注“复现”表示本次本地观察；“源码”表示控制流或数据结构核查；不代表已经在生产环境发生事故。

| 领域 | 已有能力与证据 | 当前缺口及判断 | 去向 |
| --- | --- | --- | --- |
| 解析器接入 | `contracts.py:9` 有 `ParserAdapter`；`parsers.py:5924` 实现 7 类工厂分支 | 未知名称落到 `StubParser`，返回固定正文；本次复现任务 `done`。默认配置没有启用 MinerU，不能据此说默认 PDF 都是假解析 | P0-01 |
| 必需能力 | `bootstrap.py:46` 集中组装依赖 | embedding/rerank 配置异常转 Null；`_build_index()` 可将 `pgvector + SQLite` 转为 `NullIndex`，本次复现。NullIndex 有内存辅助行为，但不等于承诺的持久化向量后端 | P0-01 |
| Provider 治理 | `config.py` 已有 registry、route-plan、`route_mode/gate_status/gate_checks`；`runtime.py:3816` 已接执行路由 | `parsecore.toml:192` 中 MinerU 为 `disabled/evaluate/pending`，工厂没有实现。应补实现与实际可用性检查，复用治理框架 | P1-01～04 |
| 质量判断 | `quality.py`、`ir.py`、`api_payloads.py:2111` 已有质量、覆盖和复跑建议 | `config.py:580` 明确 `enforcement=report_only`；质量投影返回 `blocking=False`；`runtime.py:779` 的质量观测晚于索引与 DONE。当前通用链路不是发布硬门禁 | P0-04 |
| 完成时序 | `runtime.py:747` 起保存 chunks、更新 index，再标记 DONE | `runtime.py:769` 才持久化视图；`_persist_document_views():1026` 捕获异常并返回 False，调用方未据此阻止完成。本次复现 `done + claim 已清除 + pages=0` | P0-03～04 |
| 版本一致性 | 已有 source hash、配置指纹、IR schema、知识单元 diff、输出 `parse_run_id` | `api_payloads.py:3335` 的 run 标识取 job_id；`stores.py:246/272/300` 按 tenant/doc 覆盖产物；`runtime.py:1165` 取最新 job 与当前产物。本次复现新 Blocks 与旧 Chunks 并存 | P0-02～03 |
| 作业与恢复 | job attempt、claim_token、租约、重试、死信、取消、part 及重分块/重向量化已经存在 | 写入前 claim 检查与实际产物写入不在同一原子操作；尚缺文档级发布世代约束。并发旧 worker 回写是待故障测试验证的风险，不能把现有 claim 当完整版本锁 | P0-03、P0-05 |
| 并发 | `asgi.py:37` 的 BackgroundParseRunner 用 ThreadPoolExecutor | `worker.py:25/37` 顺序 drain；`max_workers` 在独立 worker 里是每轮处理上限。不是全项目没有并发，而是两种运行模式语义不同 | P1-05 |
| 文档事实 | `pipelines.py:113/125/140` 已有 ArtifactItem、KnowledgeUnit、ParsedDocumentArtifact；IR 已有版本 | `ir.py:35` 仍从多来源重新组装投影；底层 BlockType 仅 title/paragraph/table/image。应升级现有 artifact 为持久化事实源，避免另外建立第四套独立事实模型 | P1-02 |
| 领域提取 | `catalog_contracts.py` 已有 CAAC v1/v2、提取器版本和 fieldEvidence；专用运行可恢复 | 本地校验明确只查轻量不变量，权威 Schema 在 CAACresource；`catalog_runs.py:54` 起有特定目录页段，同时也已有章节检测。应整理为版本化配置与包，不是重新实现目录提取 | P2-01～02 |
| 测试与评测 | 有 provider comparison、gold evaluation、stability、license audit、self-check 等工具；50 页已批准 AI 辅助标注 | 这些标注明确不是独立人工 gold；本次 92 项相关测试通过，但未阻止上述三种交付问题 | P0-05、P1-04 |
| CI | `.github/workflows/parsecore-ci.yml` 分 fast/full/perf | 缺样本时 fast 降为 smoke、full 可跳过；另外 full 样本探测未跳过 disabled 条目，会把目录当 JSON 读取，本次复现目录读取异常 | P0-06 |
| 访问边界 | tenant_id 已贯穿多项存储与运行接口，`api_support.py:20` 有共享 API key | 该中间件没有身份到租户的绑定。需要确认可信宿主注入边界；本次未证明越权漏洞。若对外开放多租户服务，应在开放前完成鉴权验收 | P0-07（条件前置） |
| 文档 | 已有大量规格、验收和部署资料 | `architecture.md` 仍写同步首版/未来队列，README 等存在历史路线。须区分已交付规格、本轮待办、历史验收 | P2-03 |

### 本次实测证据

相关单测命令：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_runtime tests.test_bootstrap_routing tests.test_worker tests.test_ir tests.test_provider_provenance tests.test_config_parser_options tests.test_provider_failures tests.test_p0_release_readiness -q
```

结果：**92 tests / 13.087s / OK**。这是选定测试的结果，不是全量测试、完整回归或 MinerU 推理结果。

在临时配置、临时 SQLite 库、合成 DOCX 和临时日志中执行以下探针，未连接生产库：

| 探针 | 操作 | 观察结果 |
| --- | --- | --- |
| 未实现解析器 | 将测试 `[[parsers]]` 中的 `docx-native` 换为 `mineru-local`，提交合成文件 | 实例 `StubParser`；`state=done`；含固定占位正文 |
| 视图写入故障 | 对 `job_store.save_document_views` 注入 RuntimeError | 写入尝试 1 次；`state=done`；claim 已清除；持久化 pages 为 0 |
| 失败重跑 | 同 doc_id 先成功保存 OLD 内容；新文件含 NEW 内容；对第二次运行的 `_load_chunks_for_request` 注入异常，max_attempts=1 | 最新 job 为 failed；读取 Blocks 含 NEW，Chunks 含 OLD |
| 索引配置不匹配 | `_build_index('sqlite:///:memory:', 'pgvector')` | 返回 `NullIndex`，未报配置不兼容 |
| full 样本探测 | 按 CI 当前循环读取 `suite.full.json` 的 entry.baseline | 遇到 disabled 的 `multi-column-pdf`，空 baseline 指向目录；Windows 报 PermissionError。Linux 的具体错误类型未实测 |

按有效 baseline 条目盘点本机样本引用：fast **3/3** 可找到；full **5/5** 可找到，另有 **3 个 disabled 条目**；perf **1/2** 可找到，缺 `sample-27-81-17`。这是文件存在性检查，引用数不等于不同文档数，也不表示样本通过质量验收或 CI runner 拥有相同文件。

未执行：完整 self-check、真实 PostgreSQL 故障演练、全量真实文档回归、GPU/模型推理、MinerU 同样本性能比较、线上权限测试。

## 3. 目标约束与数据契约

### 3.1 正式结果的唯一发布入口

```text
冻结输入版本 / 分配 job 与 run
  → Provider 原始结果及素材
  → 统一文档事实快照
  → 领域标注 / Chunks / Reader / Records / Export 所需产物
  → 暂存索引 + 完整性与质量校验
  → 原子切换 tenant/doc 的 active_run_id
  → 下游按该 run 读取和检索

失败或需复核的 run → 诊断/预览；已发布旧版本继续可用
```

“原子发布”指**可见性切换原子化**，不要求文件系统、数据库和外部索引参与一个分布式事务。索引和素材先写入 run 隔离空间，确认完整后在权威库事务中切换指针；所有正式查询必须按指针筛选，直接访问未筛选索引不能绕过发布约束。

### 3.2 三类状态与版本字段（建议契约，P0-02 冻结）

| 对象/字段 | 建议语义 |
| --- | --- |
| `job_id` | 调度请求身份；保留现有 job.state 作为执行阶段兼容字段 |
| `parse_run_id` | 一次执行尝试的唯一身份；重试产生新 run，避免复用 job_id 覆盖上次部分产物；关联 attempt_count 与 claim |
| `source_version` | 输入文件内容哈希与大小；执行前校验或使用不可变输入副本，避免提交后原路径内容改变 |
| `execution_status` | `pending/running/succeeded/failed/cancelled`；描述执行，不表示可供正式使用 |
| `validation_status` | `not_run/passed/warning/needs_review/rejected/error`；保存原因、指标、证据及规则版本；未知或校验异常不能默认为通过 |
| `publication_status` | `unpublished/published/superseded/withdrawn`；与当前 active 指针在同一事务维护 |
| `active_run_id` / `publication_generation` | tenant/doc 的正式版本指针与并发控制世代；不同 job 的旧执行不能因晚完成而覆盖更新发布决策 |
| `required_artifacts` / `capabilities` | 按 profile 声明 Reader、Records、RAG、素材等交付要求；未要求向量化的文档不因 embedding 关闭被拒绝 |
| 版本来源 | engine、model、adapter、facts_schema、domain_package、chunker、embedding、validation_policy 与有效配置指纹 |
| `base_run_id` | 重分块、重向量化、领域规则升级等派生产物的来源，保证复用关系可追溯 |

产物正文及 manifest 在封存后不可变；执行状态和发布指针通过受控状态迁移更新。若人工修订改变事实内容，应生成新派生 run，而非修改已发布正文。

每次读取先固定一个 run；分页游标和异步导出任务携带该 run，避免跨页或导出过程中自动切到新版。父 PDF 用 manifest 固定各 part 的 run、原始页段、重叠上下文与缺页情况；单 part 复跑产生新的父 manifest。`partial` 可显式预览，完整文档正式发布默认要求所有必需页段通过；确需发布部分结果时必须有专门 profile 和缺页清单。

已有 API `parse_run_id=job_id` 的语义不能静默替换：新增内部 run 身份后，先通过新版本 schema 暴露，旧投影保留兼容映射；生产读口径切换应有迁移说明及版本标识。历史快照无法从已被覆盖的数据中还原，不得伪造历史版本。

旧调用方可能把 DONE 或 `ProductAdapter.after_parse` 当成可以入库的通知。迁移必须同时调整消费条件：新接口以 `publication_status=published` 为准；旧正式结果接口只返回完整已发布版本，尚无正式版本时给出明确未就绪响应，另设诊断入口读取执行结果。原有 partial 消费方应迁移到显式预览口径；这些可见性变化须按契约版本灰度，不能只增加字段后继续原样通知下游。

## 4. 待办工作包

以下均为 `[ ] 待实施`。每项交付时同时附代码/配置变更、测试报告及回滚说明；任务编号用于跟踪，旧规格中的 P1–P7 编号不与这里的优先级对应。

### P0：真实执行、质量准入与发布一致性

#### [x] P0-01 配置与能力必须真实（小）

状态：**已实施（2026-09-20）**。交付报告见 [docs/p0-01-capability-truthfulness-20260920.md](p0-01-capability-truthfulness-20260920.md)。

- 修改位置：`config.py`、`bootstrap.py`、`parsers.py`、`api_health.py`、配置模板及 bootstrap 测试。
- 新增明确的生产严格模式与 mock 开关；不要把当前 `project.mode=embedded-sdk` 当生产/开发开关。未知 parser/index/provider 名称在启动时拒绝，mock 仅显式启用并标识为非正式结果。
- 将能力分为 required、optional、disabled：required 配置/依赖/初始化失败应阻止 readiness 或任务；optional 降级须在 health、运行记录和响应暴露 `degraded_capabilities/reason`；disabled 不误报故障。保留非 RAG profile 合法关闭 embedding 的能力。
- 校验配置、实际注册实现和运行可用性一致；探活按能力缓存/限时，区分存活与可接任务，避免每次 `/health` 加载重模型。
- 验收：未知解析器、缺必需凭据、索引后端不匹配均给出稳定错误码；显式 optional rerank 降级仍能交付但可见；上节占位成功探针不能产生正式结果。
- 依赖：无。建议作为第一个修复提交。

实施说明（与本节原始措辞的差异）：

- 新增 `src/parsecore/capabilities.py` 作为实现注册表、稳定错误码与能力报告的单一来源；`strictness` 默认 `strict`。
- `project.allow_placeholder_parsers`（默认 `false`）取代原先“未知名称静默落到 `StubParser`”的行为；开启后占位结果在 job options、`ParseOutcome.placeholder_result` 与 API payload 中显式标记，`/health` 报 `formal_parse_ready = false`。
- `index.mode = "pgvector"` 配非 PostgreSQL 的 `database_url` 现在直接拒绝（`index_backend_mismatch`），不再返回 `NullIndex`。
- 本次未改动完成时序与发布准入（占位结果仍会走到 `done`），该问题属于 P0-03/P0-04；P0-01 只保证占位与降级**可见且不可伪装为正式结果**。

#### [x] P0-02 冻结 run、发布与兼容契约（小）

状态：**已实施（2026-09-20）**。设计决策见 [docs/adr-p0-02-run-publication-contract.md](adr-p0-02-run-publication-contract.md)，可执行契约见 `src/parsecore/publication.py`，契约样例见 `/v1/parse/schemas/parse-run` 与 `/v1/parse/schemas/document-publication`。

- 修改位置：`models.py`、`contracts.py`、`payload_schemas.py`、`payload_contract_samples.py`；新增一份状态/存储设计决策。
- 冻结第 3 节字段、合法迁移、必需产物、warning 放行策略、旧 state 与新状态的映射；文档发布和 CAAC `quality.status=public` 的业务公开含义分别定义。
- 定义 `begin_run/save_run_artifacts/seal_run/get_active_run/publish_run` 等存储能力；明确 active 指针 CAS、claim fencing 和 latest-job 查询仅用于运行诊断。
- 明确两个作业同 doc 并行时的政策：以发布世代和预期源版本拒绝过时发布，不采用“最后完成者无条件覆盖”。
- 验收：状态迁移表、契约样例、SQLite/Postgres 迁移设计、兼容说明和失败场景全部可评审；不以只添加三个 JSON 字段算完成。
- 依赖：无，可与 P0-01 同期设计。

实施说明：

- 新增 `src/parsecore/publication.py`：三套状态机（`execution_status` / `validation_status` / `publication_status`）、合法迁移表、旧 `job.state` 映射、`ArtifactProfile`、`ValidationPolicy`、`RunStoreCapabilities` 与并行发布政策，全部以可机读常量冻结。
- 三条硬性语义：`done` 不等于 `published`；`done` 不等于 `validated`（无校验记录即 `not_run`）；重试产生新 run，不覆盖上次尝试的部分产物。
- 新增两个契约 schema 与样例（注册表 6→8，P1 契约验收 payload 24→32）；`parse_run_id` 的别名切换点写入契约（`PARSE_RUN_ID_ALIAS`），避免静默改变既有读口径。
- 明确记录当前 SQLite / Postgres / memory 三个后端的 `strict_publication_ready` 均为 `false`，`latest_job` 不参与发布决策。
- 本项**不改变运行时行为**：没有运行路径读写新状态，没有 store 实现 run 协议。存储与原子发布属于 P0-03，校验与发布门禁属于 P0-04。

#### [~] P0-03 实现快照存储与原子发布（大）

状态：**第 1 批已实施（2026-09-20）**，其余待实施。交付报告见 [docs/p0-03-run-store-batch1-20260920.md](p0-03-run-store-batch1-20260920.md)。

- 修改位置：`stores.py`、`stubs.py`、`contracts.py`、`runtime.py`、`exports.py`、`export_jobs.py`、检索与 records 读取路径。
- Blocks、Chunks、document views、素材、索引和 manifest 按 `tenant/doc/run` 隔离；索引主层、高精层及 structure entries 均纳入，不能只给 Blocks 增加 run 字段。
- 先保存完整 staged 产物并校验数量/引用/哈希，再以 DB 事务检查 run 状态、claim 所属、源版本和预期 generation，切换 active 指针。仅在写入前调用 `_ensure_active_claim()` 不足以作为原子防护。
- 所有 Reader/Records/RAG/Export 正式读取固定已发布 run；预览按明确的 run 读取。若暂时不能让某个索引实现按 run 过滤，该实现不能参加严格发布。
- 将下游可用通知绑定到发布事件，保留执行完成事件用于观测。发布事务同时保存待通知事件，再异步重试派发；按 tenant/doc/run/event 幂等消费，避免通知失败反过来重跑解析或已发布却永久漏通知。
- 覆盖 full、parent/part、part rerun、rechunk、reembed，派生 run 可复用不可变基础快照；部分失败不得覆盖旧版。
- 验收：在 blocks、chunks、views、embedding、index、seal、publish 前后分别注入故障，旧正式版本始终完整；并发 A/B 完成逆序、取消/超时后的旧 worker 不得发布；分页与导出不混版。先验证 SQLite，再以真实 Postgres/pgvector 验收。
- 依赖：P0-02。该项是 P0 的主要工程量，不能用移动 DONE 一行代替。

第 1 批已完成（SQLite 垂直闭环）：

- 新增 `src/parsecore/run_store.py`：staged 产物表 + `parse_runs` / `document_publication` / `publication_events`；`begin_run → save_run_artifacts → seal_run → record_validation → publish_run`，发布事务以 `BEGIN IMMEDIATE` 原子完成状态、策略、claim、源版本、世代、清单校验与 active 指针切换。
- 发布事务内从 staged 重写活层（blocks/chunks/document_views），因此**既有读路径无需改动**；`blocks`/`chunks` 增加 `run_id`（旧行回填 `legacy`）用于审计。
- 新增只读端点 `/v1/parse/documents/{doc_id}/publication`；未发布时返回明确 `not_ready`，`/health` 与 `/v1/runtime` 的 `run_publication` 能力项说明当前后端能否原子发布。
- 未完成（第 2 批）：索引三层与素材/manifest 的 run 隔离、Reader/Records/RAG/Export 正式读路径固定 run、full/parent-part/rechunk/reembed 派生 run 的接入、通知异步派发消费端、真实 Postgres/pgvector 验收、旧数据回填脚本。

#### [ ] P0-04 将质量规则接入发布准入（中）

- 修改位置：`runtime.py`、`quality.py`、`ir.py`、`api_payloads.py`、quality gate 配置。
- 从现有 quality/coverage 规则抽取共享校验服务，使运行期和查询投影使用相同结果；校验报告持久化到 run。
- 在必需产物生成后、正式发布前校验结构完整性、引用、覆盖、关键领域字段与 profile 要求。索引可提前写入隔离空间，但尚不可被正式查询命中。
- 把 `_persist_document_views()` 的“失败仅记日志”改为按 required_artifacts 处理：必需视图失败不得进入成功交付；可选视图失败有明确降级状态。质量遥测故障仍可单独非阻塞，不能与质量判定故障混用。
- `needs_review/rejected/error` 不自动发布；warning 是否可发布由版本化 policy 决定。执行成功且需复核不强行记作 parser 执行失败，也不无限自动重试。
- 验收：占位、缺素材、缺页、断裂来源引用、校验异常被阻止发布；warning 策略可复现；状态 API、Reader 与 RAG 对同 run 的结论一致。
- 依赖：P0-01～03；规则提取可在存储开发期间推进。

#### [ ] P0-05 补故障回归、迁移及回滚演练（中）

- 修改位置：`tests/test_runtime.py`、`tests/test_parts.py`、`tests/test_postgres_stores.py`、API/exports 测试、部署文档。
- 将本次故障注入转为长期回归，覆盖进程在发布前后退出、重复请求、取消、租约失效、worker 重启、索引不可用、并发 part 更新及发布通知丢失/重复；验证幂等与旧 claim 不可回写。
- 采用新增表/列、回填、双读校验、按租户/profile 切换的迁移流程；只把可核对的当前遗留产物导入为 legacy snapshot，validation 标识为未验证或明确的历史豁免，不能自动伪装通过。
- 回滚通过停止新路由/新发布并切回上一个完整 active run；迁移观察期不删旧快照。明确旧程序若不理解 run 表不能直接写入新版数据；保留能读取新存储的兼容版本作为回滚版本。
- 清理 staged 和过期产物时保护 active run、回滚保留版本、在途导出及被父/派生 manifest 引用的快照。
- 验收：迁移前后文档计数、哈希、检索与导出一致；演练报告列明恢复点、恢复时间及无法恢复的既有历史数据；P0 三个核心故障探针全部通过新约束。
- 依赖：P0-03～04。

#### [~] P0-06 修复 CI 样本探测并建立真正的发布门禁（中）

状态：**前半已实施（2026-09-20）**（复用 corpus 校验 + 发布验收状态区分）；"可随仓库分发的最小脱敏核心样本"与"最终门禁纳入 P0-05 故障回归"仍待实施。

- 修改位置：`.github/workflows/parsecore-ci.yml`、`tools/self_check.py`、`tools/regression_baseline.py`、`var/regression`。
- 抽取复用的 corpus manifest 校验，统一正确处理 disabled、空 baseline、非文件路径、缺样本、空 fixture 集和哈希漂移；修复当前 full 探测目录读取错误，避免 fast/full/perf 三段复制逻辑漂移。
- 日常 PR 可按轻量套件运行；新增明确的 release 验收入口并纳入实际发版流程。发布要求核心样本齐全、必需用例执行和报告完整；缺样本/被跳过应阻止发版并显示未验证，不能沿用 smoke 成功代表质量通过。
- 建立可随仓库分发的最小脱敏核心样本；受限真实样本用内部工件存储按 manifest 拉取。补齐当前缺失 perf 样本或有依据地更新基线，不把删除难样本当通过。
- 验收：刻意移除核心样本、使 suite 为空或 baseline 路径错误时 release 非零退出；合法 disabled 不触发目录读取；发布报告记录 commit、配置、样本哈希、实际运行/跳过数与报告路径。
- 依赖：样本盘点可立即开始；最终门禁纳入 P0-05 故障回归。

实施说明：

- 新增 `tools/corpus_manifest.py` 作为唯一一份 suite → baseline → fixture 解析/校验实现与 CLI；CI 中三段重复的内联 Python 探测改为调用同一命令（保留旧的 `available=` 输出，新增 `verified` / `status` / `reason` / 各类计数与报告路径）。
- `tools/self_check.py` 新增 `corpus_manifest` 检查与 `verification` 报告块（含 commit、配置指纹、样本哈希、实际运行/跳过数、报告路径），以及 `--require-verified-corpus` 发布验收模式；`--skip-regression` 现在写入显式 `regression_suite` 检查（`status=skipped`、`reason=regression_skipped_by_flag`），不再静默降级。
- 新增独立 `release` job；fast lane 语义不变（PR 仍是轻量门禁），但 smoke fallback 打印 UNVERIFIED 警告并写入 job summary。
- 实测：fast **3/3** 条目 verified；full **5/5** verified 且 **3 个 disabled 条目**被正确跳过（其空/目录 baseline 不再被读取，即原先 Windows `PermissionError` 的场景）；perf **1/2**，缺 `sample-27-81-17.pdf`。既有 baseline 未记录哈希 → `hash_unrecorded_fixtures` 为非阻塞 warning，`save` 起开始记录哈希。
- 发布门禁实测非零退出（`EXIT=1`，`reason=regression_suite_failed`），并把既有真实回归如实暴露：`numeric_heavy_total +38`、`suspected_header_footer_total +39`、`total_blocks +6.88%`、`heading_body_binding_rate -0.0909`。未修改任何 baseline 或阈值，未删除/禁用难样本。
- 未完成：可随仓库分发的最小脱敏核心样本；perf 缺样本补齐；P0-05 故障回归纳入发布门禁。

#### [ ] P0-07 明确宿主身份与租户边界（条件前置，中）

- 修改位置：`api_support.py`、`api_routes.py`、宿主注入协议及鉴权测试。
- 嵌入/可信内网模式：记录谁认证用户、谁注入 tenant，禁止把外部请求任意 tenant 参数当可信身份。
- 独立多租户模式：由认证上下文解析 tenant，覆盖 job 查询/取消、文档与原件、parts/rerun、records/search、导出创建/下载及诊断接口；资源授权以持久化所属租户为准。
- 验收：租户 A 不能通过 B 的 doc/job/export/run ID 或请求参数越界；内部 worker 和宿主代理的授权路径可审计。
- 依赖：P0-02；对外多租户开放前完成。若部署仅为受信单宿主，可先定边界，不为本轮另建账号系统。

### P1：MinerU 接入、事实模型收敛与并发

#### [ ] P1-01 固定 MinerU 版本及独立运行环境（小）

- 交付：固定 release/完整 commit、镜像与模型标识、依赖锁文件、资源规格、能力探测结果和许可记录。
- 优先自托管内网服务；推理环境与 ParseCore 分离，ParseCore 端优先轻量 HTTP 客户端，不因使用官方 SDK 又引入整套模型依赖。
- 上游本次查阅的 `torch` extra 为 `transformers>=5.10.1,<6`，本仓库 local-embedding 为 `>=4.40,<5`，这两组依赖不能在同一环境满足；此判断只针对上述组件组合。[上游依赖声明](https://github.com/opendatalab/MinerU/blob/master/pyproject.toml)
- 上游许可有附加条件。接入时归档实际固定版本 LICENSE，确认自身部署/对外服务方式及模型、依赖的许可适用性；不把独立 HTTP 部署当许可豁免。[上游许可](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)
- 验收：隔离服务可完成选定档位的合成文件解析和重启恢复；关闭 MinerU 不影响现有原生路径启动；资源和未支持能力可见。
- 依赖：无，可做独立技术验证；正式路由仍保持关闭。

#### [ ] P1-02 将现有 Artifact 收敛为持久化事实源（大）

- 修改位置：`pipelines.py`、`models.py`、`ir.py`、`api_payloads.py`、`payload_schemas.py`；新增事实模型模块可按职责命名。
- 复用 ParsedDocumentArtifact / ArtifactItem / KnowledgeUnit，增加版本化强类型页面、块、表格单元格、图/公式/素材及来源关系；未知类型保留原始类型和扩展字段，不能悄悄变成普通段落。
- 固定页码基准、坐标单位/原点/旋转、阅读顺序、跨页与父子关系、表格合并单元格、图注、公式、行内元素、缺失能力及诊断。保留原始 Provider 工件便于回放。
- Reader、Records、RAG、Export 仅投影同一快照；语义推断和原文提取分字段，并携带 source 引用。旧 Block/Chunk 接口由兼容投影继续提供。
- 验收：同 run 多投影的文本来源、页码与引用一致；未知/缺失字段可解释；同一原始结果可离线重复生成各视图；新的 Renderer 不再自行修复文档事实。
- 依赖：P0-02～03；第一版最小 facts 契约应与存储设计共同冻结，完整结构扩展在本项完成。

#### [ ] P1-03 实现结构化 MinerU Adapter（中至大）

- 修改位置：新增 `providers/mineru_http.py`（建议），对接 `parsers.py` 工厂、Provider 配置、provenance 与 failure 分类。
- 复用配置中 `mineru-local` 的候选身份，另设 transport/endpoint 表示独立本地或内网服务；如最终改名为 `mineru-http`，提供配置迁移，避免两个 ID 指向同一实现却分别准入。
- 接入路径：capability discovery → 上传受控输入 → 创建远端解析任务 → 有界轮询 → 下载结构化结果与素材 → 校验 → 映射事实快照。保留本地 job/run 与远端执行 ID，重试先恢复已有任务状态；不假设上游创建接口天然幂等。[官方 HTTP 流程](https://opendatalab.github.io/MinerU/usage/http_api/)
- 主消费 MiddleJson/结构化工件和素材包，不能只接 Markdown。当前官方说明中，命令 `mineru parse --json` 的响应不是 MiddleJson；中间协议的 `page_idx` 从 0 起，CLI 页范围从 1 起；适配器必须显式映射、测试首尾页及 part 页偏移。[官方结果契约](https://opendatalab.github.io/MinerU/reference/output_files/)
- 协议 schema、引擎版本不兼容、缺必需素材、页范围不完整、超时、取消、OOM、网络断开、下载失败分别保留错误码与 retryable；缺 bbox/confidence 不伪造。保护上传/下载凭据，素材解包校验路径与大小；设置服务级并发与输入上限。
- 验收：固定结构样例通过映射契约测试；真实服务通过扫描/表格/多栏样本；远端重启后不重复发布、不丢执行映射；默认路由继续维持原 Provider。
- 依赖：P0-01～04、P1-01～02。mock 协议开发可提前，真实影子结果必须写独立 run/工件空间。

#### [ ] P1-04 用现有工具完成隔离双跑与正确性评测（中）

- 复用 `provider_comparison_report.py`、`provider_gold_evaluation.py`、`provider_stability_gate.py`、`provider_license_audit.py`，补事实映射和运行资源指标，避免新建第二套评测平台。
- 相同冻结输入、相同页段和明确档位双跑；shadow run 不更新 active 指针、不写正式索引、不参与正式配额结果统计。占用资源单独设预算，防止影响正式队列。
- 复用现有 50 页 AI 辅助标注做初始诊断；关键数字/料号、表格行关系、警告与步骤应对照原文件独立复核，不能只以 pdf-text 的输出作为正确答案。划分调参集与验收集。
- 覆盖第 5 节样本矩阵；补推理进程/服务的 RSS、GPU 显存、冷启动/稳态数据，不能把客户端 tracemalloc 当总内存。
- 验收：报告可关联 input hash、run、engine/model/adapter 版本、规则版本与原始证据；至少 3 次稳定性复测；错误、未执行、缺证据均不计为质量通过。
- 依赖：P0-06、P1-03；样本与标注整理可提前。

#### [ ] P1-05 统一并发与资源限制（中）

- 修改位置：`worker.py`、`asgi.py`、`config.py`、`runtime.py`、worker 部署配置。
- 分开定义部署进程数、进程内 `job_concurrency`、`claim_batch_size`、`max_active_parts_per_doc`、Provider 级容量；旧 max_workers 按执行模式明确映射并给出弃用说明，不静默改变吞吐。
- queue-worker 第一版可选择多进程，每进程单作业；确需线程池时先验证 parser/model 实例线程安全和 GPU 共享方式。跨进程 part/配额限制必须通过共享存储保证，不能只依赖进程内计数。
- 明确 job 总截止时间与 worker 活性租约的区别；长推理的心跳、续租、取消、进程退出清理及重复领取分别验证。
- 验收：2 个 worker 能完成不同任务而不重复发布；单文档 part 和 Provider 容量限制在多进程下有效；worker 被终止后可恢复；记录实测并发时间线与资源峰值。
- 依赖：P0-03、P0-05；在扩大 MinerU 负载前完成。

#### [ ] P1-06 按 profile 灰度与决定是否晋升（小）

- 复用现有 route_mode、gate_status、gate_checks；按租户/profile/文档白名单控制，禁止全局将 PDF 默认切到 MinerU。
- 准入只覆盖通过的样本类别；保留原生 DOCX/Excel/文本/简单 PDF。受控视觉解析可用，但禁止无证据改写和不可追溯替换。
- 失败回退只按明确策略执行，每次换引擎留独立运行记录，最终结果暴露 actual provider、fallback reason 和验证结论。
- 验收：第 5 节硬门槛全部满足；灰度中零混版/零未验证发布；演练关闭新 Provider 并继续使用上一个已发布版本。评测不能证明收益时继续 shadow，允许只晋升部分场景。
- 依赖：P0 全部适用项、P1-04～05。

### P2：领域包与维护成本

#### [ ] P2-01 将 CAAC 和手册语义整理为版本化领域包（中）

- 修改位置：`catalog_contracts.py`、`catalog_runs.py`、`catalog_extractors/`、`pipelines.py`、领域 TOML。
- 包描述记录输入 facts 版本、领域包/规则/输出 Schema 版本、提取器、校验器、测试集和迁移策略；沿用 CAAC v1/v2，不重置既有版本历史。
- 把特定年度页段抽为带 source/version 约束的配置；复用现有章节检测，新版目录不匹配时明确报错或进入复核。
- 验收：同一事实快照分别运行旧/新领域包，无须重新 OCR；比较字段与证据 diff；升级领域规则不改变其他 profile 输出。
- 依赖：P1-02。

#### [ ] P2-02 收紧领域 Schema、证据与发布策略（中）

- 与 CAACresource 固定权威 JSON Schema 的版本/哈希或分发包，防止只靠本地轻量形状检查。
- 建立字段值到原页/块/表格单元格的证据校验及关键字段人工 gold；区分结构覆盖、内容正确和业务公开政策。
- 将业务状态 candidate/internal_preview/public/rejected 映射到通用校验与交付政策，保留各自含义；批量提取可部分复核，但正式导出必须明确包含哪些记录状态。
- 验收：关键字段错值、有值无证据、证据指向其他 run/源版本、跨页错行均能被拦截或标记复核；规则升级支持离线回放。
- 依赖：P0-04、P2-01；P1 准入需要的核心正确性标注不等待本项整体完成。

#### [ ] P2-03 按职责拆分模块并校准文档（中）

- 随前述功能移动工厂/Provider、事实归一化、领域 enrich、投影与校验，逐步降低 `parsers.py/api_payloads.py/ir.py/runtime.py` 的职责交叉；不单独启动全仓大重写。
- 更新 architecture、configuration、user-guide、release-notes 与 readiness，明确 Starlette、inline/queue-worker、report-only/发布门禁、partial preview 和正式版本语义。
- 验收：README 只有一个本轮待办入口；每个完成项指向对应实现与报告；历史文档有适用日期，不把“主链完成”扩写成 MinerU 生产可用。
- 依赖：跟随各阶段持续完成，最后统一收口。

## 5. 样本矩阵与准入判据

准确率/性能数字要先冻结基线与预算。以下是建议的验收口径，不是本次已经测出的收益，也不构成全部真实资料的准确率保证。

| 样本组 | 主要验证 | 产物 |
| --- | --- | --- |
| 简单文字 PDF + 原生 DOCX/Excel/文本 | 原生路线不退化、表格和标题契约兼容 | 内容/结构基线及兼容报告 |
| 多栏图文、跨栏标题、图注 | 阅读顺序、对象关系、页码坐标、素材引用 | 原页对照与结构映射报告 |
| 扫描件、低清/旋转页、数字料号 | OCR 关键字符正确率、漏页、失败诊断 | 字段 exact-match、错误类型与证据 |
| 跨页/合并单元格表格 | 单元格、行列关系、表头重复与跨页归属 | cell/row/relation 指标，不能只数表格数量 |
| 航空手册 | warning/caution 与 procedure_step 关联、编号和步骤完整性 | 关键事实逐项核对及缺漏清单 |
| 超大目录 | 完整页范围、内存/磁盘、任务恢复、part 上下文去重、记录分页与导出 | 小窗口 → 多 part → 全量三个阶段的报告 |

建议先验收约束：

- **交付硬门槛**：占位发布 0；必需产物缺失发布 0；新旧 run 混读 0；失效 claim 发布 0；缺核心样本或协议不支持时不得通过 release/provider 准入。
- **来源硬门槛**：全部交付对象有真实 source/run 引用；所选页段完整性检查通过；缺坐标时明确 unavailable，不能用伪造坐标满足 100%。声明支持的坐标需通过原页对照。
- **关键业务事实**：独立核对的验收集中，数字/料号/批准号及关键警告/步骤错误或遗漏为 0；覆盖范围仅限验收集。出现关键错误先修复或收窄可准入 profile，不能靠其他页面平均分抵消。
- **整体质量**：按类别比较关键 token、表格结构/关系、阅读顺序与证据完整性；默认要求不劣于基线，并在目标复杂文档类别有事先约定的收益。复用已有 gold 工具的分数门槛时，应核对它是否覆盖本轮关键事实。
- **资源与恢复**：固定机器和并发，分别报告冷启动/稳态 p50、p95、峰值 RSS/显存、失败率、恢复耗时；目标预算在首次小样本测量后冻结。超出内存或恢复预算不得直接扩大到万页。

## 6. 依赖、里程碑与工作量

关键路径：`P0-02 → P0-03 → P0-04 → P0-05 → 正式发布准入`；配置收紧与 CI 修复可先落地。MinerU 版本验证、协议样例、样本准备可独立推进，影子结果进入中台前须使用隔离 run。

| 里程碑 | 范围 | 出口条件 | 规划量级 |
| --- | --- | --- | --- |
| M0：风险确认与契约 | P0-01/02、P0-06 初步修复；P1-01 调研 | 配置真实、契约可评审、CI 探测可用 | 约 1 周 |
| M1：可靠交付闭环 | P0-03～06；适用时 P0-07 | 三类本地故障被回归约束，SQLite/Postgres 发布及回滚通过 | 约 2～3 周 |
| M2：MinerU 影子接入 | P1-01～03、P1-05 | 独立服务、完整结构映射、影子隔离与容量限制 | 约 2～3 周 |
| M3：评测与受控晋升 | P1-04/06 | 样本、许可、质量、资源、恢复及回滚证据齐全 | 约 1～2 周 |
| M4：领域包收口 | P2-01～03 | 领域版本独立、证据验证和文档一致 | 约 1～2 周 |

估算假设：1 名熟悉仓库的后端开发，测试/领域人员可参与验收，数据库与推理资源可获得；各阶段可有样本准备重叠，但表内合计约 **7～11 人周量级**，不是工期承诺。完整快照迁移、生产权限、上游部署或样本获取的未知项应在 M0 后重新估算；不应把原子发布压成一两天的小修。

首批建议拆为四个可评审交付：

1. 配置严格性及显式降级（P0-01）。
2. CI corpus 探测修复、发布验收状态区分（P0-06 前半）。
3. run / 状态 / 发布指针 ADR 与契约样例（P0-02）。
4. 快照存储和读取的第一个垂直闭环，再扩到索引、导出及 part（P0-03）；在全部正式读路径完成前，不宣称已经保证原子交付。

## 7. 本轮决策与待验证事项

- 默认保持现有原生路径，MinerU 先自托管隔离运行、只做候选；ParseCore 管来源版本与发布，不引入第二套文档权威库。官方提供不创建文档库的解析入口以及自托管 API，可支持这一边界。[官方 SDK 说明](https://opendatalab.github.io/MinerU/usage/sdk_api/)
- 本次访问了 MinerU 官方主分支资料及文档；未成功读取输入报告的缩写 commit 页面，未据此宣称已复核其精确 `4.0.4/25b16c40d18f…` 基线。P1-01 必须重新固定完整版本，再录制协议工件；在线文档不能替代锁定版本的契约测试。
- 尚需在实施起步时确认：实际部署是单宿主还是对外多租户；首个验收类别优先 CAAC 还是航空手册；可用 GPU/内存、吞吐目标与样本获取方式。默认先以可信内网、复杂 PDF/手册影子评测推进，这些信息不阻塞 P0 本地修复。
- 本轮只完成现状分析和待办制定；生产代码、默认路由及依赖未修改，也未将任何 Provider 标记为已准入。
