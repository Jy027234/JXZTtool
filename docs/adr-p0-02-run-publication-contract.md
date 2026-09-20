# ADR：run / 状态 / 发布指针契约冻结（P0-02）

日期：2026-09-20
状态：**已冻结（frozen）**，待 P0-03 实现
适用待办：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-02
可执行契约：`src/parsecore/publication.py`（`SCHEMA_VERSION = 2026-09-run-publication-contract-v1`）
契约样例：`/v1/parse/schemas/parse-run`、`/v1/parse/schemas/document-publication`，样例由 `payload_contract_samples.build_sample_parse_run()` / `build_sample_document_publication()` 生成并通过 JSON Schema 校验
测试：`tests/test_publication_contract.py`（31 项）、`tests/test_payload_schemas.py`、`tests/test_schema_snapshot.py`

本文只冻结词汇与接口。**P0-02 不改变任何运行行为**：没有任何运行路径读写新状态，没有 store 实现 run 协议。存储实现属于 P0-03，校验与发布准入门禁属于 P0-04。

---

## 1. 要解决的问题

核查复现的三个交付问题中，有两个的根因是**同一个身份被复用**：

1. `job_id` 同时充当调度请求身份和结果版本身份，重跑复用同一 `job_id`，导致新 Blocks 与旧 Chunks 并存（`stores.py:246/272/300` 按 tenant/doc 覆盖产物）。
2. `job.state = done` 同时被当作"执行完成"和"可以入库"的通知信号，导致占位解析、视图写入失败都能通知下游。

因此本 ADR 的核心决定是：**把执行完成、校验结论、发布状态拆成三套独立状态，并引入独立的 run 身份与发布指针。**

### 1.1 三条硬性结论

| 结论 | 含义 |
| --- | --- |
| `done` 不等于 `published` | 任何 run 的初始 `publication_status` 都是 `unpublished`，包括执行成功的 run。只有显式且通过校验的迁移才会变成 `published`。 |
| `done` 不等于 `validated` | 旧 `job.state` 的任何取值都不映射为校验通过；没有校验记录时 `validation_status = not_run`。 |
| 产物按 run 隔离 | 重试产生新的 `parse_run_id`，绝不覆盖上一次尝试的部分产物。 |

---

## 2. 身份与版本字段

| 字段 | 语义 | 生命周期 |
| --- | --- | --- |
| `job_id` | 调度请求身份；保留现有 `job.state` 作为执行阶段兼容字段 | 重试复用 |
| `parse_run_id` | 一次执行尝试的唯一身份；关联 `attempt_count` 与 claim | 每次尝试新建 |
| `source_version` | 输入文件内容哈希与大小；执行前校验或使用不可变输入副本 | 冻结后不变 |
| `base_run_id` | 重分块/重向量化/领域规则升级等派生 run 的来源 | 派生时写入 |
| `active_run_id` | tenant/doc 的正式版本指针 | 发布事务内切换 |
| `publication_generation` | 发布世代，单调递增，用于并发控制 | 每次发布 +1 |
| `version_sources` | engine / model / adapter / facts_schema / domain_package / chunker / embedding / validation_policy / 有效配置指纹 / 能力指纹 | 封存时固定 |

产物正文与 manifest 在 `seal_run` 后不可变。若人工修订改变事实内容，必须生成新的派生 run，不能修改已发布正文。

### 2.1 `parse_run_id` 兼容策略（重要）

现有正式读口径中 `parse_run_id = job_id`（`api_payloads.py:3335` 等）。该语义**不能静默替换**：

- P0-02 阶段：`PARSE_RUN_ID_ALIAS = "job_id"`，行为与今天完全一致。
- P0-03 阶段：值切换为 run 身份，切换点在 `PARSE_RUN_ID_ALIAS_SWITCH_CONTRACT_VERSION` 声明的契约版本上，通过新版 schema 暴露，旧投影保留映射，并附迁移说明与版本标识。
- 需要同时关联 job 与 run 的消费者必须分别读取 `job_id` 和 `parse_run_id` 两个字段，不能假定二者相等。
- 历史快照无法从已被覆盖的数据中还原，**不得伪造历史版本**；迁移时只能把可核对的当前遗留产物导入为 legacy snapshot，并把 `validation_status` 标为未验证或明确的历史豁免。

---

## 3. 三套状态机

### 3.1 `execution_status`

`pending → running → succeeded | failed | cancelled`

- 终态不再迁移。租约过期后的恢复**新建 run**，而不是把旧 run 改回 `running`。
- 相同状态重复写入是幂等的（允许），失败→运行等回退不允许。

### 3.2 `validation_status`

`not_run → passed | warning | needs_review | rejected | error`

| 取值 | 含义 |
| --- | --- |
| `not_run` | 有产物但没有校验结论。**默认值，不能被当成通过** |
| `passed` | 全部必需校验通过 |
| `warning` | 存在可放行告警；是否发布由版本化 policy 决定 |
| `needs_review` | 需人工复核，不自动发布，也不无限自动重试 |
| `rejected` | 明确拒绝发布 |
| `error` | 校验自身异常。未知或异常一律不得折叠为 `passed` |

校验结论必须连同 `reason`、`policy_version`、`rule_version`、`warning_codes`、`metrics`、`evidence_refs` 一起持久化到 run。

### 3.3 `publication_status`

`unpublished → published | withdrawn`，`published → superseded | withdrawn`，`superseded → withdrawn`，`withdrawn` 为终态。

- 只有 `publish_run()` 事务可以写 `published`。
- 发布新世代时旧 run 在同一事务内变为 `superseded`。
- `withdrawn` 为运营动作，产物仍可读，用于诊断与审计。

### 3.4 旧 `job.state` 映射表

| 旧 `job.state` | `execution_status` | `validation_status` | `publication_status` |
| --- | --- | --- | --- |
| `pending` | `pending` | `not_run` | `unpublished` |
| `parsing` / `structuring` / `embedding` | `running` | `not_run` | `unpublished` |
| `partial` | `succeeded` | `not_run`（缺页清单另记） | `unpublished` |
| `done` | `succeeded` | `not_run` | `unpublished` |
| `failed` | `failed` | `not_run` | `unpublished` |

映射由 `legacy_execution_status()` / `execution_status_from_job_state()` 提供，未知取值抛错而不是猜测。`partial` 在执行层是成功，但**默认不可发布**；确实需要发布部分结果时必须有专门 profile 与缺页清单。

---

## 4. 必需产物与 profile

`ArtifactProfile` 把能力分为 `required` / `optional` / `disabled`：

| profile | required | disabled |
| --- | --- | --- |
| `default` | reader | — |
| `rag` | reader、rag | — |
| `records` | reader、records、export | rag |
| `assets-only` | assets | reader、records、rag |

`default` 故意保持 RAG 为 optional：**未要求向量化的文档不因 embedding 关闭被拒绝**。必需产物失败不得进入成功交付；可选产物失败必须带明确降级状态。这与 P0-01 的 `degraded_capabilities` 使用同一套语义。

---

## 5. 校验策略与 warning 放行

`ValidationPolicy`（默认 `2026-09-validation-policy-v1`）：

| 条件 | 结果 |
| --- | --- |
| `passed` 且 `publish_on_passed = true` | 可发布 |
| `warning` 且 `publish_on_warning = false`（默认） | 不可发布 |
| `warning` 且开启且全部 code 在 `warning_allowlist` | 可发布 |
| `warning` 且存在未列入白名单的 code | 不可发布 |
| `not_run` / `needs_review` / `rejected` / `error` | 不可发布 |
| profile 在 `require_durable_index_for_profiles`（默认 `rag`）且索引非持久 | 不可发布（`index_backend_not_durable`） |

策略版本随 run 持久化，因此后续策略变更**不能追溯改写历史 run 的结论**。

---

## 6. 存储能力与发布事务

P0-03 要实现的操作（`RUN_STORE_OPERATIONS`）：

```text
begin_run              # 冻结输入版本，分配 job + run，记录预期 generation
save_run_artifacts     # 在 run 隔离空间写入 blocks/chunks/views/素材/manifest
seal_run               # 校验数量/引用/哈希后封存，产物转为不可变
record_validation      # 持久化校验结论与证据
get_run                # 按 run 读取（含未封存）
get_active_run         # 读取 tenant/doc 的已发布 run 与世代
publish_run            # 单事务内校验并切换 active 指针
list_runs              # 运行诊断
record_publication_event  # 发布事件与待派发通知（outbox）
```

能力键（`RUN_STORE_CAPABILITY_KEYS`）与严格发布门槛（`STRICT_PUBLICATION_REQUIRED_CAPABILITIES`）：

```text
run_isolated_artifacts        必需
staged_artifacts              必需
atomic_active_pointer         必需
compare_and_set_active_pointer 必需
claim_fencing                 必需
source_version_check          可选强化
publication_generations       可选强化
validation_persistence        可选强化
publication_outbox            可选强化
```

**今天 SQLite / Postgres / memory 三个后端的 `strict_publication_ready` 都是 `false`**，由 `run_store_capabilities()` 显式报告。P0-03 补齐后逐个打开；在补齐之前，任何后端都不得声称能原子发布。

`latest_job` 刻意**不在**能力列表里：读最新 job 仅是运行诊断，不得用于决定发布什么。

### 6.1 发布事务（P0-03 实现口径）

```text
BEGIN
  SELECT run            -- FOR UPDATE / BEGIN IMMEDIATE
  校验: execution_status = succeeded
  校验: validation_status 允许发布（按 run 上记录的 policy 版本）
  校验: 必需产物已 seal 且清单/哈希一致
  校验: claim 属于本次发布者（claim fencing）
  校验: source_version 与请求中的 expected_source_version 一致
  校验: publication_generation 与请求中的 expected_generation 一致
  UPDATE document_publication
     SET active_run_id = :run, publication_generation = generation + 1
   UPDATE run SET publication_status = 'published'
   UPDATE 旧 active run SET publication_status = 'superseded'
  INSERT publication_event（待异步派发）
COMMIT
```

仅在写入产物**之前**调用 `_ensure_active_claim()` 不能作为原子防护；上述检查必须与指针切换处于同一事务。

### 6.2 并行作业政策

同一 `tenant/doc` 有两个作业时的政策是 **fenced expected generation**，明确**不是**"最后完成者无条件覆盖"：

| 情形 | 结果 |
| --- | --- |
| A、B 并发，B 先发布世代 7 | A 发布时 `expected_generation = 6` ≠ 7 → 冲突，A 保持 `unpublished` |
| A 的 `source_version` 与最新已发布版本的输入不一致 | 不覆盖；`publication_rejected_stale` |
| A 在取消/超时后由旧 worker 回写 | claim fencing 失败，不得发布 |
| 需要重新处理 | 生成新的派生 run（`base_run_id` 指向被替代的 run） |

`CONCURRENT_PUBLICATION_POLICY` 把这些取值固化为可机读常量，并显式记录 `last_writer_wins = false`、`latest_job_used_for_publication = false`。

---

## 7. 下线通知与消费方迁移

旧调用方可能把 `DONE` 或 `ProductAdapter.after_parse` 当成可以入库的通知。迁移必须**同时**调整消费条件：

| 项 | 旧口径 | 新口径 |
| --- | --- | --- |
| 正式结果信号 | `job.state = done` / `after_parse`（**已废弃且不充分**） | `publication_status = published` |
| 正式读取 | 直接读最新产物 | 只返回完整已发布版本 |
| 尚无正式版本 | 无明确响应 | 明确 `not_ready` 响应 |
| 需要执行结果 | 与正式结果混用 | 走独立诊断入口 |
| `partial` 结果 | 按正式结果消费 | 迁移到显式预览口径 |

- 下游可用通知绑定到**发布事件**；执行完成事件只用于观测。
- 发布事务同时写入待通知事件，再异步重试派发；按 `tenant/doc/run/event` 幂等消费，避免通知失败反过来触发重跑解析，也避免"已发布却永久漏通知"。
- 这些可见性变化须按契约版本灰度，**不能只增加字段后继续原样通知下游**。
- `document-publication` 契约里的 `consumer_signal`（`published` / `not_ready` / `preview_only` / `withdrawn`）就是给消费方判定的权威字段。

### 7.1 文档发布与 CAAC 业务公开的区别

两者是不同层次的语义，不能互相替代：

| 概念 | 载体 | 取值 | 谁决定 |
| --- | --- | --- | --- |
| 文档发布 | `publication_status` / `active_run_id` | `unpublished` / `published` / `superseded` / `withdrawn` | ParseCore 的发布事务与校验策略 |
| CAAC 记录业务公开 | 领域记录 `quality.status` | `candidate` / `internal_preview` / `public` / `rejected`（`catalog_contracts.py:66`） | 领域包与业务复核流程 |

文档 `published` 只表示"这份事实快照是该 tenant/doc 的当前正式版本"，**不表示**其中的领域记录可以对外公开。领域记录仍由 `quality.status` 单独控制；P2-02 负责把两者映射到统一的交付政策。

---

## 8. SQLite / Postgres 迁移设计（P0-03/P0-05 执行）

原则：**新增表/列 + 回填 + 双读校验 + 按租户/profile 切换**，不原地改写既有列。

### 8.1 新增表

```sql
-- 两库通用（Postgres 用 BIGSERIAL / JSONB，SQLite 用 INTEGER PRIMARY KEY / TEXT）
CREATE TABLE parse_runs (
    parse_run_id            TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL,
    tenant_id               TEXT NOT NULL,
    doc_id                  TEXT NOT NULL,
    base_run_id             TEXT,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    execution_status        TEXT NOT NULL,
    validation_status       TEXT NOT NULL DEFAULT 'not_run',
    publication_status      TEXT NOT NULL DEFAULT 'unpublished',
    artifact_profile        TEXT NOT NULL DEFAULT 'default',
    source_version          TEXT,
    source_bytes            INTEGER,
    version_sources         TEXT,        -- Postgres: JSONB
    validation_report       TEXT,        -- Postgres: JSONB
    sealed_at               TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX idx_parse_runs_doc ON parse_runs(tenant_id, doc_id, created_at DESC);

CREATE TABLE document_publication (
    tenant_id               TEXT NOT NULL,
    doc_id                  TEXT NOT NULL,
    active_run_id           TEXT,
    publication_generation  INTEGER NOT NULL DEFAULT 0,
    publication_status      TEXT NOT NULL DEFAULT 'unpublished',
    source_version          TEXT,
    profile                 TEXT NOT NULL DEFAULT 'default',
    policy_version          TEXT,
    published_at            TEXT,
    withdrawn_reason        TEXT,
    PRIMARY KEY (tenant_id, doc_id)
);

CREATE TABLE publication_events (
    event_id                TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    doc_id                  TEXT NOT NULL,
    parse_run_id            TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    publication_generation  INTEGER,
    payload                 TEXT,        -- Postgres: JSONB
    dispatched_at           TEXT,
    created_at              TEXT NOT NULL,
    UNIQUE (tenant_id, doc_id, parse_run_id, event_type)
);
```

### 8.2 既有表按 run 扩展

`blocks` / `chunks` / document views（pages/lines/records）/ 素材与索引层都需要 `parse_run_id` 列，而不只是给 Blocks 加字段。索引主层、高精层与 structure entries 都要纳入。

- SQLite：`ALTER TABLE ... ADD COLUMN parse_run_id TEXT`，回填为对应 job 的首次 run，然后对 `(tenant_id, doc_id, parse_run_id)` 建索引。
- Postgres：同上；`pgvector` 表额外需要 `parse_run_id` 参与查询条件，未按 run 过滤的索引实现**不得参加严格发布**。

### 8.3 回填与双读

1. 为每个历史 job 生成一个 legacy run：`execution_status` 取旧 state 映射，`validation_status = not_run`，`publication_status = unpublished`。
2. 生成一条 legacy 发布记录，`active_run_id` 指向该 legacy run，`publication_generation = 1`，`policy_version = "legacy-import-no-validation"`，并在 run 上标明历史豁免。
   - **不得**把回填的 legacy run 标为 `passed`。
3. 观察期内正式读路径双读新旧存储并比对文档计数、哈希、检索与导出结果。
4. 按 tenant / profile 逐步切换到按 run 读取；迁移观察期不删旧快照。

### 8.4 回滚

- 停止新路由与新发布，切回上一个完整 active run。
- 迁移观察期保留全部旧快照；清理 staged 与过期产物时必须保护 active run、回滚保留版本、在途导出以及被父/派生 manifest 引用的快照。
- 保留能读取新存储的兼容版本作为回滚版本；旧程序若不理解 run 表，**不能直接写入新版数据**。
- 第 1 批已提供回滚原语 `SQLiteRunStore.restore_live_from_run(doc_id, tenant_id)`：用 active run 的**已封存** staged 产物重建活层，重建前重新校验清单数量与内容哈希，且**不切换指针**；没有 active run 时返回 `no_active_run` 且不写入任何数据。staged 被篡改时拒绝重建（`SealError`）。

---

## 9. 失败场景清单（P0-05 转为回归）

> 实施状态（P0-03 第 1 批）：下表中 SQLite 可覆盖的行已转为 `tests/test_run_store.py` 的自动化回归；索引三层、素材隔离、派生 run、通知派发与 Postgres 相关行仍待第 2 批。

| 场景 | 期望结果 |
| --- | --- |
| blocks 写入后进程退出 | 旧正式版本不受影响，新 run 保持 `unpublished` |
| seal 前进程退出 | 无 active 指针变化，staged 产物可清理 |
| 发布事务前进程退出 | active 指针不变 |
| 发布事务已提交、通知未派发 | 事件仍在 outbox，重试派发；不重跑解析 |
| 通知重复投递 | 按 `tenant/doc/run/event` 幂等 |
| 并发 A/B 逆序完成 | 世代较新者发布，较旧者冲突 |
| 取消/超时后旧 worker 回写 | claim fencing 拒绝写入与发布 |
| worker 重启 | 恢复租约后新建 run，不覆盖旧产物 |
| 索引不可用 | 按 profile 判定：`rag` profile 不可发布，`default` 降级可见 |
| 分页/导出跨发布 | 游标与导出任务携带固定 run，不混版 |
| part 复跑 | 生成新父 manifest，旧 run 仍完整可读 |
| 缺页的 `partial` | 默认不可发布；专门 profile + 缺页清单才可显式预览 |

---

## 10. 验收对照

| P0-02 验收项 | 交付物 |
| --- | --- |
| 状态迁移表可评审 | 本文第 3 节 + `legal_transitions()` + `tests/test_publication_contract.py::StateMachineTests` |
| 契约样例 | `parse-run` / `document-publication` 两个 JSON Schema 与样例，纳入 `payload-contract-check` 与 P1 契约验收（schema 6→8，payload 24→32） |
| SQLite/Postgres 迁移设计 | 本文第 8 节 |
| 兼容说明 | 本文第 2.1 与第 7 节 + `contract_payload()["consumer_migration"]` |
| 失败场景 | 本文第 9 节，P0-05 转回归 |
| 不以"只添加三个 JSON 字段"算完成 | 本项不新增运行时字段；vocabulary、状态机、策略、存储能力与迁移路径均已冻结，行为改动留给 P0-03/P0-04 |

## 11. 变更控制

契约状态为 `frozen`。后续工作包如需扩展：

- 允许：新增可选字段、新增 profile、新增能力键、新增 `validation_policy` 版本；
- 不允许：改变既有字段含义、复用已废弃取值、让 `done` 重新等价于 `published`；
- 破坏性变更必须提升 `SCHEMA_VERSION` 并附迁移说明。
