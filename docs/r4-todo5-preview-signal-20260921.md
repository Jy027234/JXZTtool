# 待办 5 交付报告：未发布内容明确为预览

日期：2026-09-21
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 5
状态：完成标准逐条复核完毕，本次补齐"预览"信号；第 4 节列出仍然留待后续的条目

## 1. 完成标准逐条复核

待办 5 的完成标准是：**"OLD 发布后，NEW 重跑失败，正式读取与导出仍返回 OLD，正文和版本标识一致；冲突不损伤其他文档；重复提交不产生错版"**，以及正文里的"未发布内容明确为预览，不能标注 published/pinned"。

| 完成标准 | 现状 | 证据 |
| --- | --- | --- |
| OLD 发布后 NEW 重跑失败，正式读取仍返回 OLD | 已满足 | [tests/test_read_consistency.py](../tests/test_read_consistency.py)（先发布 OLD，再让 NEW 分块失败，读取仍为 OLD 正文与身份）；`PublicationPolicyError` 分支不改动已发布 run |
| 正文与版本标识一致 | 已满足（本次补上读取/导出的身份） | `publication` 块随每个投影返回 `served_run_id` / `pinned` / `consumer_signal`；快照 `job` 现为服务版本的 job，见第 4 节；[tests/test_publication_read_identity.py](../tests/test_publication_read_identity.py) |
| 冲突不损伤其他文档 | 已满足 | 发布时发现跨文档 block/chunk ID 冲突即拒绝发布（`_colliding_artifact_ids`），不再删除他人活层行；[tests/test_run_store.py](../tests/test_run_store.py) |
| 重复提交不产生错版 | 已满足 | `publication_generation` + `expected_generation` CAS、claim fencing、派生 run 关系；同上 |
| **未发布内容明确为预览，不能标注 published/pinned** | **本次补齐** | 见第 2 节 |

## 2. 本次补齐：预览信号

`consumer_signal` 的枚举里早就有 `preview_only`（契约与 ADR 都写了），但运行时从未产生过它：已解析但未发布的文档一律报 `not_ready`，与"什么都没解析"无法区分。

现在 `publication_state()` 明确区分三种情形，并把预览 run 指名道姓：

| 情形 | `consumer_signal` | 其他字段 |
| --- | --- | --- |
| 已发布正式版本 | `published` | `active_run_id` = 正式 run，`preview_run_id` 可为更新的未发布 run |
| 已解析但未发布（可交付的预览） | `preview_only` | `preview_run_id` + `preview{run_id, validation_status, publication_status, source_version, profile, created_at, pinned=false, is_formal_version=false}` |
| 什么都没有 | `not_ready` | `preview_run_id = null`，`preview = null` |
| 已发布但被撤回 | `withdrawn` | 保持原语义 |

判定"可交付预览"的条件：`execution_status = succeeded`、未发布、**已封存**（`sealed_at` 非空）、校验结论不是 `rejected` / `error` / `not_run`。被拒绝或从未校验的 run 没有可交付内容，仍然报 `not_ready`。

`preview` 与 `preview_run_id` 同时进入 `/v1/parse/documents/{id}/publication` 的载荷（`document-publication` schema 已同步为必需字段），因此消费方无需另建接口即可判断"这份内容是不是正式版本"。

## 3. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/run_store.py](../src/parsecore/run_store.py) | `publication_state()` 返回 `preview_run_id` 与 `preview`，并在只有预览时给出 `consumer_signal = preview_only` |
| [src/parsecore/api_payloads.py](../src/parsecore/api_payloads.py) | 发布状态载荷透传 `preview_run_id` / `preview` |
| [src/parsecore/payload_schemas.py](../src/parsecore/payload_schemas.py) | `document-publication` 增加 `preview` / `preview_run_id`（必需） |
| [src/parsecore/payload_contract_samples.py](../src/parsecore/payload_contract_samples.py) | 契约样例补上新字段 |
| [tests/test_run_publication_runtime.py](../tests/test_run_publication_runtime.py) | 新增 2 项：OLD 已发布 + NEW 未发布时正式读取仍为 OLD 且 NEW 作为预览；无任何 run 时 `not_ready` 且无预览。既有 warning 用例改为断言 `preview_only` 与预览详情 |
| [tests/test_schema_snapshot.py](../tests/test_schema_snapshot.py) | 冻结字段清单同步 |

验证：

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 994 tests / OK (skipped=6)

.\.venv\Scripts\python.exe -m unittest tests.test_asgi
Ran 63 tests / OK    # 本模块连续 3 次全通过
```

## 4. 读取身份：正文来自哪个版本，就报哪个版本（本次补齐）

复核导出面时复现出一个具体缺陷：**已发布 OLD 之后又跑出未发布的 NEW，读取快照里的 `job` 是"最新的 job"**。

```text
OLD job: job-dfabe3c4ff62 published: published
NEW job: job-d3d02eb1003e state: done          # 未发布（warning）
snapshot job id: job-d3d02eb1003e              # ← 缺陷：正文是 OLD，身份却是 NEW
publication served_run_id: run-1ea43178f3874e23 → 该 run 的 job 是 job-dfabe3c4ff62
blocks: ['old', 'OLD content']
records parse_run_id: job-d3d02eb1003e         # 导出把 OLD 正文标成了 NEW 身份
```

读取内容来自活层（由已发布 run 拥有），但快照的 `job` 取的是最新 job；`_document_projection` 与
`_minimal_export_payload` 都用 `job.job_id` 作为 `parse_run_id`（`PARSE_RUN_ID_ALIAS`），于是导出
把 OLD 的正文标成了 NEW 的身份——正是完成标准里"正文和版本标识一致"的反面。

修正：`get_document()` 在读取上下文给出 `served_run_id` 时，用该 run 的 `job_id` 解析出**服务版本
的 job**作为快照身份；job 行不存在时（API 直接 `publish_run`、legacy 导入）用 run 记录重建一个最小
job 身份，run 记录才是权威。未发布的新 run 仍作为 `publication.preview` 出现，两个身份不再冲突。

修正后（同一场景）：

```text
snapshot job id: job-dfabe3c4ff62              # 与 served_run_id 指向的 run 一致
records parse_run_id: job-dfabe3c4ff62         # 导出同步
preview run id: run-8f0edca127b94ef6           # NEW 作为预览，身份独立
```

回归覆盖（[tests/test_read_consistency.py](../tests/test_read_consistency.py)，新增 2 项）：

- 读取快照的 `job` 是服务版本（`job-old`），`publication.served_run_id`、`pinned` 一致，导出记录
  的 `parse_run_id` 也是 `job-old`，而 NEW 出现在 `publication_state()["preview_run_id"]` 且其
  `job_id` 就是 NEW 的 job；
- 结构化投影载荷的 `parse_run_id` 同为服务版本，`latest_job_used_as_formal_version` 仍为 false。

## 5. 仍然留待后续

1. **索引层的 run 隔离**：`PgVectorIndex` 仍不能按 run 过滤，因此索引能力未计入 `SQLiteRunStore` 的能力声明；正式部署的 Postgres/pgvector 一致性与超大目录演练仍按方案留在部署事项里。
2. **已发布证据清理保护**：staged 产物的清理作业尚未实现（方案把它列为部署相关事项）。
3. **导出数据集全覆盖**：本次覆盖 records 导出与结构化投影；pages/lines/coverage/reader 四个数据集走同一 `get_document` 快照，未各自单测。

## 6. 回滚

- 代码回滚：`git revert` 本次提交；`consumer_signal` 回到"未发布一律 `not_ready`"的旧行为，`preview` 字段随之消失（schema 需一并回滚），读取身份回到"最新 job"。
- 行为回滚：不需要开关。预览只增加信息，不改变读取服务哪个 run、也不改变发布判定；读取身份修正只影响快照报告的身份字段，不改变服务的内容。
