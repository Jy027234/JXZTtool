# P0-03 第 1 批交付报告：run 快照存储与原子发布（SQLite 垂直闭环）

日期：2026-09-20
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-03
设计依据：[docs/adr-p0-02-run-publication-contract.md](adr-p0-02-run-publication-contract.md)
状态：第 1 批已实施；索引/素材隔离、正式读路径固定 run、派生 run、Postgres 验收见第 6 节

## 1. 这一批解决什么

本轮复现的第二个交付问题——**失败重跑时新 Blocks 与旧 Chunks 并存**——根因是产物按 `tenant/doc` 覆盖，没有版本边界。第 1 批建立了这个边界：

```text
begin_run          分配 parse_run_id；记录源版本、claim、预期 generation
  ↓
save_run_artifacts 只写 run 隔离的 staged 表（可重复、封存后不可变）
  ↓
seal_run           校验必需产物、引用完整性，计算内容哈希并冻结清单
  ↓
record_validation  持久化校验结论 + policy 版本（不通过则永远不能发布）
  ↓
publish_run        单个 BEGIN IMMEDIATE 事务：校验全部前置条件 → 从 staged
                   重写活层 → 切换 active_run_id → 旧版本转 superseded
                   → 写入待派发发布事件
```

关键性质：**活层与 active 指针要么一起变，要么都不变。** 任何一步失败都回滚，旧版本保持完整可读，新 run 保持 `unpublished`。

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/run_store.py](../src/parsecore/run_store.py)（新增） | `SQLiteRunStore`：schema、run 生命周期、staged 产物、封存校验、发布事务、指针读取、outbox 读写、能力声明 |
| [src/parsecore/publication.py](../src/parsecore/publication.py) | 能力声明归一到契约层（`SQLITE_RUN_STORE_CAPABILITIES`）；`ARTIFACT_PROFILES` 的 artifact family 对齐实际 staged 产物 |
| [src/parsecore/stores.py](../src/parsecore/stores.py) | `blocks` / `chunks` 增加 `run_id` 列与回填迁移（旧行 = `legacy`） |
| [src/parsecore/runtime.py](../src/parsecore/runtime.py) | `run_store` 装配、`run_publication_ready`、`publication_state()`、`list_runs()`、`publish_run()` 全流程封装 |
| [src/parsecore/api_payloads.py](../src/parsecore/api_payloads.py) | `build_document_publication_payload()`：`consumer_signal` 权威字段 + 校验/policy/readiness/legacy 说明 |
| [src/parsecore/api_routes.py](../src/parsecore/api_routes.py) | `GET /v1/parse/documents/{doc_id}/publication` |
| [src/parsecore/capabilities.py](../src/parsecore/capabilities.py) | 新增 `run_publication` 能力项，暴露 store 能力与 `strict_publication_ready` |
| [src/parsecore/config.py](../src/parsecore/config.py) | `runtime.run_publication_enabled`、`runtime.run_artifact_profile` |
| [parsecore.toml](../parsecore.toml) | 新字段与注释 |
| [tests/test_run_store.py](../tests/test_run_store.py)（新增） | 30 项：正常路径、前置条件、fencing、故障注入、outbox |
| [tests/test_run_publication_runtime.py](../tests/test_run_publication_runtime.py)（新增） | 7 项：runtime 与 HTTP 语义 |
| [tests/test_run_publication_capability.py](../tests/test_run_publication_capability.py)（新增） | 5 项：能力报告与 health 面 |

## 3. 发布事务的校验项

`publish_run()` 在**同一个** `BEGIN IMMEDIATE` 事务内依次校验，任一失败即回滚：

1. `execution_status = succeeded`（含未 seal 一律拒绝）；
2. 已 `seal_run` 且存在清单；
3. claim 属于本次发布者（claim fencing —— 旧 worker 回写被拒）；
4. `source_version` 与请求中的 `expected_source_version` 一致；
5. `publication_generation` 与 `expected_generation` 一致（并发 A/B 逆序完成时旧世代冲突）；
6. run 上记录的 policy 版本允许该 `validation_status` 发布；
7. staged 行仍与封存清单的数量与内容哈希一致（防越权篡改）。

通过后才：删除并重写该 `tenant/doc` 的活层 → `publication_generation + 1` → active 指针指向本 run → 旧 active run 转 `superseded` → 写入 `run_published` 事件。

## 4. 验证

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 完整序列发布并切换指针 | `test_full_sequence_publishes_and_switches_the_pointer` | 通过 |
| 二次发布取代旧版本 | `test_second_publication_supersedes_the_first` | 通过 |
| 未发布文档返回 `not_ready` | `test_publication_state_is_not_ready_before_any_publication` | 通过 |
| 失败 run 不能 seal 也不能发布 | `test_execution_must_succeed_before_publishing` | 通过 |
| 未校验（`not_run`）阻止发布 | `test_validation_not_run_blocks_publication` | 通过 |
| `needs_review`/`rejected`/`error` 阻止发布 | `test_needs_review_and_rejected_never_publish` | 通过 |
| warning 默认不放行 | `test_warning_blocked_by_default_policy` | 通过 |
| legacy 导入 run 永不发布 | `test_legacy_imported_runs_may_never_be_published` | 通过 |
| 缺必需产物阻止封存 | `test_missing_required_artifact_blocks_sealing` | 通过 |
| 断裂引用阻止封存 | `test_chunk_referencing_an_unknown_block_blocks_sealing` | 通过 |
| 封存后产物不可变 | `test_artifacts_are_immutable_after_sealing` | 通过 |
| 陈旧 generation 被拒且旧版本完整 | `test_stale_generation_is_rejected_and_the_old_version_survives` | 通过 |
| 陈旧源版本被拒 | `test_stale_source_version_is_rejected` | 通过 |
| claim fencing 拒绝失效 worker | `test_claim_fencing_rejects_a_writer_that_lost_its_claim` | 通过 |
| 取消后的旧 worker 不能发布 | `test_cancelled_run_cannot_be_published_by_a_late_worker` | 通过 |
| seal 前进程退出：旧版本完整 | `test_failure_before_sealing_leaves_the_previous_version_complete` | 通过 |
| **发布事务中途失败整体回滚** | `test_failure_during_the_publish_transaction_rolls_everything_back` | 通过 |
| staged 被篡改则拒绝发布 | `test_tampered_staging_blocks_publication` | 通过 |
| 失败 run 不影响已发布版本 | `test_a_failed_run_does_not_disturb_the_published_version` | 通过 |
| 重试产生新 run、不复用旧产物 | `test_a_retry_creates_a_new_run_and_does_not_reuse_the_old_artifacts` | 通过 |
| 回滚可从 active run 重建活层 | `test_restore_rebuilds_the_live_tier_from_the_active_run` | 通过 |
| 无 active run 时回滚是空操作 | `test_restore_is_a_no_op_without_an_active_run` | 通过 |
| staged 被篡改时拒绝回滚 | `test_restore_refuses_to_rebuild_from_tampered_staging` | 通过 |
| 发布写入待派发事件、派发幂等 | `test_publication_writes_a_pending_event` | 通过 |
| 回滚的发布不留下发布事件 | `test_a_rolled_back_publish_leaves_no_run_published_event` | 通过 |
| HTTP 端点 `not_ready` / `published` | `tests/test_run_publication_runtime.py` | 通过 |
| 端点响应匹配冻结 schema | `test_publication_payload_matches_the_frozen_schema` | 通过 |
| 能力报告与 health 一致 | `tests/test_run_publication_capability.py` | 通过 |

命令与结果：

```text
.\.venv\Scripts\python.exe -m unittest tests.test_run_store tests.test_run_publication_runtime tests.test_run_publication_capability -q
Ran 45 tests in ~8s / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
Ran 770 tests / OK (skipped=6)
```

## 5. 兼容性说明

- **既有读路径未改动**。发布事务在事务内重写活层（`blocks` / `chunks` / `document_views`），所以 `get_document`、检索、records、导出仍然读同一批表。这是本批次的刻意取舍：先验证发布事务的原子性，再在第二批把正式读路径固定到 run。
- `blocks` / `chunks` 新增 `run_id`，历史行回填为 `'legacy'`；它只用于审计，不参与既有查询条件。
- `/health` 的 `status` 语义不变；新增的 `run_publication` 能力项为 optional，不与交付阻塞混淆。
- **memory:// 与 Postgres 后端仍然 `strict_publication_ready = false`**，此时 `publish_run()` 返回 `published: false, reason: run_store_not_ready`，端点报 `not_ready`。这符合 P0-01 的"能力必须真实"：宁可说不能发布，也不做非原子发布。
- 发布失败返回稳定的错误码（`run_publish_failed` / `publication_rejected_stale` / `run_seal_failed` / `run_not_sealable`），调用方可直接分支。

## 6. 本批未完成（第 2 批范围）

第 2 批已部分落地（2026-09-20，同一报告续写）：

| 原未完成项 | 现状 |
| --- | --- |
| 正式读路径固定 run | **部分完成**：`runtime.publication_read_context()` 把"本次读取服务了哪个 run"随快照传给所有投影，`compat`/`structured`/`full`/`ir`/`coverage`/`reader` 六个投影都带 `publication` 块（`pinned` / `served_run_id` / `consumer_signal` / `latest_job_used_as_formal_version` 恒为 false）。HTTP 面 `GET /documents/{id}`、`?projection=reader`、`/coverage` 与 `/publication` 的 `served_run_id` 一致。**尚未**把读取硬性限制为"必须有已发布 run"——嵌入式流程默认不发布，硬限制会破坏所有既有读取，因此当前是"如实声明"而不是"强制固定"。 |
| 通知异步派发消费端 | **已完成**，见第 7 节。 |
| 索引三层与素材/manifest 的 run 隔离 | 未完成。`PgVectorIndex` 仍不能按 run 过滤，因此 `publication_outbox` 之外的索引能力未计入 `SQLiteRunStore` 能力声明。 |
| 派生 run（full / parent-part / rechunk / reembed） | 部分完成：`begin_run(base_run_id=...)` 与 `publish_run` 支持派生关系，租约恢复路径已使用；rechunk/reembed 的正式派生 run 尚未接入。 |
| 真实 Postgres/pgvector 验收、旧数据回填脚本 | 回填脚本已完成（P0-05，`import-legacy-snapshots`）；Postgres 验收未完成。 |

新增测试：`tests/test_publication_read_identity.py`（6 项），断言未发布时 `served_run_id` 为空且 `consumer_signal = not_ready`、发布后为已发布 run、六个投影一致、HTTP 面一致、新发布切换服务身份。

## 7. 通知派发（P0-05 收口）

`SQLiteRunStore.dispatch_pending_publication_events(handler)` 与 `parsecore dispatch-publication-events`：先投递后标记，失败保持 pending 并记录 `publication_event_dispatch_failed`；`publication_outbox` 已计入能力声明。详见 [docs/p0-05-fault-regression-20260920.md](p0-05-fault-regression-20260920.md) 第 6 节。

## 8. 仍未完成

1. **索引三层与素材/manifest 的 run 隔离**：`PgVectorIndex` 尚不能按 run 过滤，因此该实现不能参加严格发布（能力声明已如实反映）。
2. **读取硬性固定 run**：见第 6 节；当前只保证身份一致与可声明。
3. **派生 run 的完整接入**：rechunk / reembed 的正式派生 run。
4. **真实 Postgres/pgvector 验收**与**生产数据回填演练**。
5. **真实 Postgres/pgvector 验收**与**旧数据回填脚本**：见 ADR 第 8 节设计。

在这些完成之前，不应宣称已经保证端到端原子交付。

## 9. 回滚

- 代码回滚：`git revert` 本批次提交；`run_id` 列与 run 表为增量，旧程序不读它们。
- 运行期回滚：`runtime.run_publication_enabled = false` 即回到本批次之前的行为，`/publication` 端点报 `not_ready` 并说明原因。
- 数据回滚：`SQLiteRunStore.restore_live_from_run(doc_id=..., tenant_id=...)` 会用当前 active run 的**已封存** staged 产物重建活层，并在重建前重新校验清单哈希；没有 active run 时返回 `no_active_run` 而不做任何写入。它不切换指针，因此可作为"活层被误改"的恢复动作。
