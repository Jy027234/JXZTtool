# 待办 5 交付报告：未发布内容明确为预览

日期：2026-09-21（同日续写：分区父文档的合并一致性、完整性与在飞重跑）
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 5
状态：完成标准逐条复核完毕；预览信号、读取身份、分区合并一致性均已补齐；第 6 节列出仍然留待后续的条目

## 1. 完成标准逐条复核

待办 5 的完成标准是：**"OLD 发布后，NEW 重跑失败，正式读取与导出仍返回 OLD，正文和版本标识一致；冲突不损伤其他文档；重复提交不产生错版"**，以及正文里的"未发布内容明确为预览，不能标注 published/pinned"。

| 完成标准 | 现状 | 证据 |
| --- | --- | --- |
| OLD 发布后 NEW 重跑失败，正式读取仍返回 OLD | 已满足 | [tests/test_read_consistency.py](../tests/test_read_consistency.py)（先发布 OLD，再让 NEW 分块失败，读取仍为 OLD 正文与身份）；`PublicationPolicyError` 分支不改动已发布 run |
| 正文与版本标识一致 | 已满足（本次补上读取/导出的身份） | `publication` 块随每个投影返回 `served_run_id` / `pinned` / `consumer_signal`；快照 `job` 现为服务版本的 job，见第 4 节；[tests/test_publication_read_identity.py](../tests/test_publication_read_identity.py) |
| 冲突不损伤其他文档 | 已满足 | 发布时发现跨文档 block/chunk ID 冲突即拒绝发布（`_colliding_artifact_ids`），不再删除他人活层行；[tests/test_run_store.py](../tests/test_run_store.py) |
| 重复提交不产生错版 | 已满足 | `publication_generation` + `expected_generation` CAS、claim fencing、派生 run 关系；同上 |
| **未发布内容明确为预览，不能标注 published/pinned** | **已补齐** | 见第 2 节（预览信号）与第 5 节（分区父文档：待复核分片不再被当成正式版本的一部分） |

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

## 5. 分区父文档：合并的读一致性、完整性与在飞重跑（本次补齐）

复核分区（超大 PDF 分片解析）时，用真实 297 页样本 `36d65cd6b61346e28e97dbaf829646de.pdf`
（计划 3 片：1–100 / 101–200 / 201–297）查出三个具体缺陷，全部落在待办 5 的完成标准上。

### 5.1 两个写入者交错，活层留下"块与分块不匹配"的一对

复现：`tests.test_asgi` 的分片重跑用例单独跑 30 次失败 3 次（未修改的 `dcecacc` 上 30 次失败 1 次，
同量级，说明是既有竞态）：

```text
SealError: chunks reference blocks that are not part of this run:
  doc-pdf-api-parts:merged:doc-pdf-api-parts-part-2:doc-pdf-api-parts-part-2-p-1, …-title
```

机制（用探针在 `_merged_partition_artifacts` 与 `refresh_partitioned_parent` 上取样确认）：
分片完成时**工作线程**会用 `changed_part_ids` 做增量刷新，而重跑请求（或超时清扫、失败刷新）
会做整体合并；每次刷新都是"先写 blocks、再写 chunks"两条语句，两个写入者交错时就会把 A 的
blocks 与 B 的 chunks 配成一对。`seal_run()` 会拒绝这种 run，但这一对已经留在活层，而且异常
逃出了 `except PublishError`，把一次重跑请求变成 500。

修正：

- **按父文档加写锁**（`_partition_artifact_lock()`，可重入，不同文档仍并行）：合并且包含
  "发布合并 run" 的整个过程都在锁内，`seal_run()` 因此只会看到静止状态；
- `publish_merged_parent_run()` 把 `SealError` 与 `PublishError` 一样处理：记录事件
  `merged_parent_run_not_sealed`、**不发布**，重跑请求仍能返回分片结果。

验证：分片重跑用例连续 **40 次全通过**（修正前 30 次 3 次失败）。

### 5.2 一片待复核，父文档却发布了缺页的正式版本

真实样本上，3 片全部解析成功、状态 `done`，但第 1 片（1–100 页，795 个块）的校验结论是
`warning`（`publish_on_warning=False`），于是它只有 `preview_only` 的暂存 run；父文档合并时
只能读到活层，于是：

| | 修正前 | 修正后 |
| --- | --- | --- |
| 父文档 `consumer_signal` | `published` | `preview_only` |
| 父文档 `active_run_id` / 代数 | `run-4b70bfb00e23416a` / 2 | `null` / 0 |
| 父文档 `formal_version_available` | true | false |
| 父文档活层页数 | 197 / 297（第 1 片整片消失） | 197 / 297（内容不变，但明确是预览） |

即：**父文档曾把"少了 100 页"的合并结果当成正式版本发布**。修正后父文档仍然合并可用内容
（不隐藏、不丢内容），但把缺口交给 run 校验：`partition_parts_awaiting_review`（warning）让
默认策略拒绝发布正式版本，父文档保持 `preview_only` 并给出 `preview_run_id`；`warning_codes`
现在也会随 run 记录保存，预览因此可定位到具体规则（本次一并补上 `policy_version` /
`rule_version` / `warning_codes` 的落库）。

### 5.3 重跑在飞时，父文档会丢掉那一片

`_merged_partition_artifacts()` 只认"每片最新的 job"（`_latest_pdf_part_jobs()`），而重跑一开始
这一片的最新 job 就是 `pending`，于是整体合并只拿得到其余分片：

| 场景（真实样本） | 修正前 | 修正后 |
| --- | --- | --- |
| 重跑 `part-2` 后（尚未执行）父文档 | 只剩其余分片，且发布新的正式版本 | 块/页数不变（1987 块 / 197 页，`part-2` 仍是上一次完成的内容） |
| 重跑执行完 | `published`，代数 2 | 仍按缺口判定（`part-1` 待复核 → `preview_only`，代数 0） |

修正：`_latest_done_part_jobs()` 按分片取**最近一次完成**的 job 参与合并；整体合并与"待复核分片"
判定都用它，另外把计划里的分片（`part_specs`）纳入判定——从未完成过的分片同样算缺口
（`part_not_finished`），不会因为"还没有 job"而悄悄少页。

### 5.4 交付物与验证

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/runtime.py](../src/parsecore/runtime.py) | 父文档写锁；`_latest_done_part_jobs()`；`_parts_awaiting_review()`（待复核 / 未完成两类缺口）；`publish_merged_parent_run(parts_awaiting_review=…)` 把缺口作为 warning 质检信号送入校验；`SealError` 按"未发布"处理；run 记录补 `policy_version` / `rule_version` / `warning_codes`；新增 `publish_reviewed_run()` |
| [src/parsecore/run_store.py](../src/parsecore/run_store.py) | `publish_run(reviewed=…)`：对 `warning` / `needs_review` 的人工决定例外，决定写入 `run_published` 事件；`rejected` / `error` / `not_run` 仍拒 |
| [src/parsecore/api_routes.py](../src/parsecore/api_routes.py) | `POST /v1/parse/documents/{doc_id}/publication/publish`（缺 reason/reviewer → 400；无可发布预览 → 404；策略拒绝 → 409） |
| [docs/configuration.md](configuration.md) | 运维章节补上复核发布入口与语义 |
| [tests/test_runtime.py](../tests/test_runtime.py) | 新增 3 项：两个刷新不交错的确定性用例（去掉锁即失败）、`SealError` 时父文档不发布、分片待复核时父文档保持预览并给出规则码；新增 2 项：重跑在飞时父文档保留该片上次完成的内容、复核发布分片后父文档重算 |
| [tests/test_run_publication_runtime.py](../tests/test_run_publication_runtime.py) | 新增 3 项：复核发布把预览 run 变成正式版本（结论仍是 warning）、无决定的复核被策略拒绝、复核发布接口端到端（含 schema 校验） |

验证：

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 1013 tests / OK (skipped=6)

分片重跑用例连续 40 次：0 失败（修正前 30 次 3 次失败；未修改基线 30 次 1 次失败）
```

真实样本前后对比（`36d65cd6b61346e28e97dbaf829646de.pdf`，3 片，本机 inline）：

```text
解析 + 合并 6.8 s（三片：1-100 / 101-200 / 201-297）
分片发布状态：part-1 preview_only(warning) | part-2 published(passed) | part-3 published(passed)
父文档：preview_only，formal_version_available=false，1987 块 / 1964 分块，197 页（= 101-297）
分块引用悬空块：0（合并前后、重跑前后均为 0）
重跑 part-1 并执行 5.9 s：父文档仍 preview_only、代数 0、内容不变
```

第 1 片的 `warning` 是内容本身的结论（该片 100 页里触发了校验规则）。为避免"待复核"变成死路，
本次补齐了复核后的发布入口（第 5.5 节），并在真实样本上跑通了完整链路。

### 5.6 回归门禁状态（本轮未放宽任何阈值）

```text
.\.venv\Scripts\python.exe tools\self_check.py --profile fast --out var\self-check-fast.json
unit_tests        1013 tests passed
runtime_describe  passed
payload_contracts schemas=8 payloads=8 failed_schemas=0 failed_payloads=0
corpus_manifest   3/3 entries verified
regression_suite  failed —— 四项既有漂移，与本轮改动无关（数值与改动前一致）：
  numeric_heavy_total +44（预算 +2）
  suspected_header_footer_total +45（预算 +2）
  total_blocks +9.96%（预算 ±5%）
  heading_body_binding_rate -0.0784（预算 +0.05）
```

表格指标未变：`table_ready=1.0000`、`table_cells=44`、`blocks=2285`。四项漂移在本轮之前的
提交上同样存在（见[待办 1 报告第 5.1 节](r4-todo1-word-merged-table-20260921.md)），属于本轮五项
待办之外的既有问题；门禁的阈值与基线未做任何放宽，仍然如实退出 1。

### 5.5 复核后的发布入口（本次补齐）

预览 run 原本只能靠重跑或改配置才有机会进入正式版本；分区父文档加上缺口判定后，这会把
"一片待复核"变成整篇都没有正式版本。因此补一个显式的人工决定入口：

| 入口 | 说明 |
| --- | --- |
| `ParseRuntime.publish_reviewed_run(doc_id, parse_run_id=None, reason=..., reviewer=...)` | 把指定（默认取预览）run 发布为正式版本；分片发布会顺带刷新其父文档 |
| `POST /v1/parse/documents/{doc_id}/publication/publish` | 同上，body `{"reason": "...", "reviewer": "...", "parse_run_id": "..."}`；缺 reason 与 reviewer 时返回 400 `review_decision_required` |
| `SQLiteRunStore.publish_run(reviewed={...})` | 唯一的第二条策略例外（另一条是 `legacy_import`）：只对 `warning` / `needs_review` 生效，`rejected` / `error` / `not_run` 仍然拒绝 |

原则：**发布不等于改写结论**。被复核发布的 run 仍然记录 `validation_status = warning`，决定
（reason / reviewer / 时间）写进 `run_published` 事件的 payload 与接口返回的
`diagnostics.published_after_review`，因此"为什么一个非 passed 的 run 成了正式版本"是可追溯的。

真实样本上的完整链路（`36d65cd6b61346e28e97dbaf829646de.pdf`，3 片）：

```text
分片全部解析完成     父文档 preview_only，代数 0，1987 块 / 197 页（1 片待复核）
复核发布 part-1      分片 published（validation 仍是 warning），父文档自动重算
                     → 2782 块 / 2739 块分块 / 297 页（1..297），悬空引用 0，仍 preview_only
                     （父文档自己的校验结论同样是 warning：它现在含有需要复核的内容）
复核发布父文档        published，代数 1，formal_version_available=true，
                     served_run_id=run-620a91c65ece4b15，pinned=true
```

即"复核分片 → 复核整篇"两次决定就能得到完整的 297 页正式版本；在此之前父文档不会用缺页的
合并结果冒充正式版本。

### 5.7 导出数据集带上所服务的版本标识（本次补齐）

补 pages / lines / coverage / reader 四个数据集的导出回归时，发现一个具体缺陷：**这些数据集导出的
文件里没有任何版本标识**。records 的行自带 `parse_run_id`，而 pages / lines 的行来自
`document_views` 持久化视图（本身不含标识），coverage / reader 的行也不重复信封字段；导出的文件
只有行、没有信封，于是"这份导出属于哪个 run"无法回答——正是完成标准里"正文和版本标识一致"。

修正：导出页在写出前，用该次读取所服务的版本（`payload["doc_id"]` / `payload["parse_run_id"]`）
给每个行补上 `doc_id` / `parse_run_id`（行已有标识时不覆盖）。回归用例
`tests/test_read_consistency.py::test_every_export_dataset_states_the_version_it_served` 在
"OLD 已发布 + NEW 预览"场景下逐个数据集导出，断言每行报的都是 `job-old`。

## 6. staged 产物清理作业（本次补齐）

第 5 节留下的"已发布证据清理保护"在此补齐。此前每个 run 的 staging 行（blocks/chunks/views）与区域
素材都永久保留，没有任何回收路径。

| 对象 | 处理 |
| --- | --- |
| 文档当前 `active_run_id` | 永不回收：`restore_live_from_run()` 回滚要读它的 staging 行 |
| 文档当前 `preview_run_id` | 永不回收：复核后仍可能发布 |
| 其它 run 超过保留期 | 回收：**先删素材文件、再删 staging 行**（失败时留下"描述指向已删文件"，而不是无人知晓的文件） |
| 活层（live tier） | **永不动**：它属于已发布 run；清理后 `restore_live_from_run()` 仍可回滚 |
| 默认保留期 | `[runtime] run_artifact_retention_seconds`（默认 2592000 = 30 天） |

入口 `cleanup_run_artifacts(tenant_id=…, doc_id=…, retention_seconds=…, dry_run=True)`（默认 dry-run），
返回 `{scanned_runs, protected_runs, eligible_runs, deleted_files, freed_bytes, missing_files,
deleted_staged_rows{blocks,chunks,views,assets}, runs, errors}`；真实删除才写 `run_artifacts_collected`。
它取代了上一轮只清素材的 `cleanup_region_assets()`（同名能力是其子集，保留一个入口）。

真实存储实测（297 页目录图册 run + 一个新区块，455 素材/份）：

```text
只有 1 个已发布 run 时：scanned 2，eligible 1（旧的未发布 run），protected = 当前正式 run
发布第二个版本后 dry-run：eligible 2、would delete 910 个文件
execute：deleted_files 455、missing_files 455（上一轮已删过的文件如实计缺）、freed 15,025,614 字节
         deleted_staged_rows = {blocks: 3732, chunks: 3646, views: 22588, assets: 910}
正式 run 的活层：restore_live_from_run() 仍返回 restored=true，磁盘剩 1 个 PNG（新版本的素材）
```

用例（[tests/test_region_assets.py](../tests/test_region_assets.py) 新增 1 项）：清理后旧 run 的
`{blocks,chunks,views,assets}` 全为 0、正式 run 的计数不变、活层块列表不变、`restore_live_from_run()`
仍可回滚；dry-run 的 `deleted_staged_rows` 全为 0。全量 **1052 项 OK (skipped=6)**。

### 6.1 运维入口与索引 run 隔离判定（本次补齐）

- **CLI**：parsecore.cli cleanup-run-artifacts --config … [--tenant-id] [--doc-id]
  [--retention-seconds] [--execute] [--out]，默认 dry-run，--execute 才删除；出错时退出码 1。
- **索引 run 隔离**：durable_index 不再由 isinstance(index, PgVectorIndex) 决定，而是读索引自己的
  声明 supports_run_isolation（PgVectorIndex = False：行按文档存放，一次 upsert 覆盖整篇，无法回滚到
  某个已发布 run；NullIndex = False）。因此要求持久索引的 
ag profile 在默认索引上会被拒绝
  （
un_not_publishable_by_policy / index_backend_not_durable），而不是发布一个索引层永远回不去的版本；
  一旦某个后端能按 run 隔离，它自己声明即可，运行时不必知道它的类。用例
  	ests/test_run_publication_capability.py::IndexRunIsolationTests（2 项）覆盖声明与拒绝语义。

## 7. 仍然留待后续

1. **索引层的 run 隔离**：`PgVectorIndex` 仍不能按 run 过滤，因此索引能力未计入 `SQLiteRunStore` 的能力声明；正式部署的 Postgres/pgvector 一致性与超大目录演练仍按方案留在部署事项里。
2. **已发布证据清理保护**：staged 产物的清理作业尚未实现（方案把它列为部署相关事项）。
3. **复核入口的权限与批量操作**：`publish 复核` 目前只做"必须给出决定"的校验，未接入宿主鉴权勾子，也没有"一次复核整篇（含所有待复核分片）"的批量入口；宿主可循环调用分片入口再发布父文档。

## 8. 回滚

- 代码回滚：`git revert` 本次提交；`consumer_signal` 回到"未发布一律 `not_ready`"的旧行为，`preview` 字段随之消失（schema 需一并回滚），读取身份回到"最新 job"。
- 行为回滚：不需要开关。预览只增加信息，不改变读取服务哪个 run、也不改变发布判定；读取身份修正只影响快照报告的身份字段，不改变服务的内容。
- 分区修正的回滚代价：回滚写锁会恢复"块/分块交错 + 500"的竞态（30 次约 3 次），回滚缺口判定会恢复"缺页的正式版本"与"在飞重跑丢片"。
