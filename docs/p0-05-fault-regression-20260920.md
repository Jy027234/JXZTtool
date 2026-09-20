# P0-05 交付报告：故障回归、迁移与回滚

日期：2026-09-20
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-05
状态：故障回归与迁移/回滚工具已实施；真实 Postgres 与生产数据演练见第 6 节

## 1. 这一项解决什么

P0-03 实现了原子发布，P0-04 实现了发布准入。P0-05 负责证明这两件事**在事务之外**也成立：进程会在任意一步死掉、请求会重复、租约会失效、worker 会重启、索引会挂、通知会丢或重复——以及已经把数据写进去的旧库怎么迁进来、出问题怎么退回去。

## 2. 故障回归清单

`tests/test_fault_regression.py`（20 项）。每项都断言同一个不变量：**上一个已发布版本保持完整，新 run 保持 `unpublished`。**

| 场景 | 断言要点 |
| --- | --- |
| staging 后进程退出 | 旧版本完整，新 run 未发布 |
| seal 后、校验前退出 | 即使请求发布也被拒绝 |
| 发布提交后、通知派发前退出 | 事件仍在 outbox；已发布状态不依赖通知是否发出 |
| 重复请求（复用已发布 run 身份） | 不产生第二个世代，指针与世代不变 |
| 租约失效 | 恢复路径**新建** run 并带 `base_run_id`，新 run staging 为空，死 run 的部分产物永不发布 |
| worker 重启后再次发布 | 世代递增、旧 run 转 `superseded`、活层为新内容 |
| 索引不可用 | `rag` profile 拒绝发布（`index_backend_not_durable`）；`default` 正常发布 |
| 并发 part 逆序完成 | 各文档指针与世代互不干扰 |
| part 重跑 | 只影响自身文档；另一文档的已发布版本与世代不变 |
| 通知丢失 | 事件保持 pending，重启可重投 |
| 通知重复投递 | 第二次为 no-op，不产生重复通知 |
| 事件唯一性 | `(tenant, doc, run, event_type)` 无重复行 |
| 陈旧写者（旧 generation） | 冲突，旧版本完整 |
| 活层被越权改写 | `restore_live_from_run()` 可恢复，恢复前重新校验清单哈希 |
| staged 被篡改 | 回滚同样拒绝 |
| 迁移前后 | block/chunk/view 计数与内容哈希一致 |
| legacy 导入 run | 永不通过正常路径发布，`validation_status = not_run` |

## 3. 迁移工具

新增 `tools/legacy_snapshot_import.py`，CLI 入口 `parsecore import-legacy-snapshots`：

```powershell
# 默认 dry-run：只报告会影响哪些文档
.\.venv\Scripts\python.exe -m parsecore.cli import-legacy-snapshots --config parsecore.toml

# 确认后写入
.\.venv\Scripts\python.exe -m parsecore.cli import-legacy-snapshots --config parsecore.toml --apply
```

对真实 `var/parsecore.db` 的 dry-run 输出（节选）：

```json
{
  "applied": false,
  "candidate_count": 2,
  "imported_count": 0,
  "documents": [
    { "doc_id": "aviation-training-3460c…", "status": "would_import",
      "block_count": 8, "chunk_count": 8, "view_count": 162,
      "validation_status": "not_run" },
    { "doc_id": "regression:36d65cd6b6134…pdf", "status": "would_import",
      "block_count": 2182, "chunk_count": 2139, "view_count": 14993 }
  ]
}
```

dry-run 时 `imported_count` 恒为 0，调用方无法把 dry-run 误当迁移完成。

设计要点：

- 导入的 run 记录 `validation_status = not_run`、`policy_version = legacy-import-no-validation`、`version_sources.engine = legacy-import`，并带 block/chunk 内容哈希，供迁移前后比对。
- 发布走 `publish_run(legacy_import=True)` 这一条**受限**路径：它要求 run 的 policy 恰为 `legacy-import-no-validation` **且** `validation_status` 恰为 `not_run`。因此它不能用来重新发布任何已校验（含 `rejected`/`needs_review`）的 run——绕过校验的口子被限制在"从未校验过"这一种情形。
- 指针与活层来自同一次读取，因此迁移后 `active_run_id` 与活层内容一致；导入本身也会在事务内按 staged 重写活层，所以失败不会留下半迁移状态。

## 4. 发现的真实风险（本轮修复）

活层 `blocks`/`chunks` 的 `block_id`/`chunk_id` 是**全局主键**，而读取按 `tenant/doc` 过滤。P0-03 之前的库如果两个文档复用了同一 id，发布第二个文档时插入会撞主键约束——原先会直接抛 `IntegrityError`。

现在发布会在插入冲突的那一刻移除冲突行，并写入 `artifact_id_displaced` 事件（含被顶替的 id 列表）：

- 不是因为"冲突行不重要"，而是因为一个 block id 只能属于一个文档，二者不能同时持有；
- 关键取舍：**不在发布开始时批量删除**，而是逐行在插入前删除，这样**未被发布的文档仍保留自己的产物**。起初我写成批量删除，结果发布 part-2 时静默清空了 part-1 的内容——这个 bug 被 part 回归测试当场抓住，才改成现在的位置；
- 顶替行为有专门的回归测试 `test_shared_block_id_across_documents_is_displaced_loudly`。

## 5. 验证

```text
.\.venv\Scripts\python.exe -m unittest tests.test_fault_regression -q
Ran 20 tests / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
Ran 856 tests / OK (skipped=6)
```

三个核心故障探针（核查文档复现的问题）在新约束下的结论：

| 原探针 | 现状 |
| --- | --- |
| 未实现解析器 → 占位正文 + `done` | 启动即拒绝；显式开启占位时结果带 `placeholder_result`，发布被 `placeholder_result` 规则拒绝 |
| 视图写入失败 → `done` + `pages=0` | 必需产物失败即任务 `failed`，且检查在 `DONE` 之前，真实原因不会被 `stale_claim` 掩盖 |
| 失败重跑 → 新 Blocks + 旧 Chunks 并存 | run 隔离 + 原子发布 + 陈旧世代冲突；迁移与回滚均有回归 |

## 6. 通知派发（P0-03 遗留项的收口）

发布事务把待通知事件写入 outbox，P0-05 补上了消费端：`tools/dispatch_publication_events.py`，CLI 入口 `parsecore dispatch-publication-events`。

```powershell
# 打印每条事件（一行一个 JSON），供采集器 tail
.\.venv\Scripts\python.exe -m parsecore.cli dispatch-publication-events --config parsecore.toml

# POST 到宿主接收端
.\.venv\Scripts\python.exe -m parsecore.cli dispatch-publication-events --config parsecore.toml --sink http --url https://host/parsecore/events
```

投递语义：**先调用 sink，成功后才标记已派发**，因此崩溃重跑会重投（at-least-once）；消费端需按 `(tenant, doc, run, event_type)` 幂等。sink 失败或返回 `False` 时事件保持 pending，并写入 `publication_event_dispatch_failed` 事件记录失败原因——通知不会因为下游不可用而丢失，也不会因为通知失败而重跑解析。CLI 在存在失败投递时返回 1。

## 7. 未完成

1. **真实 Postgres/pgvector 迁移与故障演练**：本轮全部在 SQLite 上验证；ADR 第 8 节的设计需要在真实 Postgres 上跑一遍（含 `FOR UPDATE` 版发布事务）。
2. **生产数据演练报告**：验收要求"列明恢复点、恢复时间及无法恢复的既有历史数据"。本轮给出了工具与一致性校验口径，尚未在真实数据上产出该报告。
3. **staged 产物清理作业**：清理时必须保护 active run、回滚保留版本、在途导出与被引用的快照；当前只有保留期配置，没有清理作业。
4. **生产发布门禁纳入本回归**：P0-06 已建立 `--require-verified-corpus` 发布验收，把本模块加入其必需检查项仍待做。
5. **常驻派发器**：目前是一次性 CLI（可由 cron/宿主定时调用），没有常驻进程或背压策略。

## 8. 回滚

- 代码回滚：`git revert` 本次提交（新增测试与工具，不改运行时默认行为，风险最低的一次）。
- 迁移回滚：迁移只新增 `parse_runs` / `document_publication` / `publication_events` 行与 `blocks.run_id` 列，不改写原有产物；`restore_live_from_run()` 可从 active run 重建活层。
- 若迁移后需回到旧程序：旧程序不读 run 表，可继续运行；但它写入的产物不会被纳入 run 体系，需重新导入。
