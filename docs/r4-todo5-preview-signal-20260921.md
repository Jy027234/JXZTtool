# 待办 5 交付报告：未发布内容明确为预览

日期：2026-09-21
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 5
状态：完成标准逐条复核完毕，本次补齐"预览"信号；第 4 节列出仍然留待后续的条目

## 1. 完成标准逐条复核

待办 5 的完成标准是：**"OLD 发布后，NEW 重跑失败，正式读取与导出仍返回 OLD，正文和版本标识一致；冲突不损伤其他文档；重复提交不产生错版"**，以及正文里的"未发布内容明确为预览，不能标注 published/pinned"。

| 完成标准 | 现状 | 证据 |
| --- | --- | --- |
| OLD 发布后 NEW 重跑失败，正式读取仍返回 OLD | 已满足 | [tests/test_read_consistency.py](../tests/test_read_consistency.py)（先发布 OLD，再让 NEW 分块失败，读取仍为 OLD 正文与身份）；`PublicationPolicyError` 分支不改动已发布 run |
| 正文与版本标识一致 | 已满足 | `publication` 块随每个投影返回 `served_run_id` / `pinned` / `consumer_signal`，`latest_job_used_as_formal_version` 恒为 false；[tests/test_publication_read_identity.py](../tests/test_publication_read_identity.py) |
| 冲突不损伤其他文档 | 已满足 | 发布时发现跨文档 block/chunk ID 冲突即拒绝发布（`_colliding_artifact_ids`），不再删除他人活层行；[tests/test_run_store.py](../tests/test_run_store.py) |
| 重复提交不产生错版 | 已满足 | `publication_generation` + `expected_generation` CAS、claim fencing、派生 run 关系；同上 |
| **未发布内容明确为预览，不能标注 published/pinned** | **本次补齐** | 见下 |

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
Ran 992 tests / OK (skipped=6)

.\.venv\Scripts\python.exe -m unittest tests.test_asgi
Ran 63 tests / OK    # 本模块连续 3 次全通过
```

## 4. 仍然留待后续

1. **失败重跑后正式读取的导出面**：读取（documents/reader/coverage/ir）已核对；导出任务（`export_jobs`）走同一批活层行与 `publication` 上下文，但没有单独的"OLD 已发布 + NEW 失败 + 导出仍为 OLD"用例，属于待补的回归覆盖。
2. **索引层的 run 隔离**：`PgVectorIndex` 仍不能按 run 过滤，因此索引能力未计入 `SQLiteRunStore` 的能力声明；正式部署的 Postgres/pgvector 一致性与超大目录演练仍按方案留在部署事项里。
3. **已发布证据清理保护**：staged 产物的清理作业尚未实现（方案把它列为部署相关事项）。

## 5. 回滚

- 代码回滚：`git revert` 本次提交；`consumer_signal` 回到"未发布一律 `not_ready`"的旧行为，`preview` 字段随之消失（schema 需一并回滚）。
- 行为回滚：不需要开关。预览只增加信息，不改变读取服务哪个 run、也不改变发布判定。
