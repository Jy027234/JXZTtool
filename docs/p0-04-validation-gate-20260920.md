# P0-04 交付报告：质量规则接入发布准入

日期：2026-09-20
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-04
设计依据：[docs/adr-p0-02-run-publication-contract.md](adr-p0-02-run-publication-contract.md) 第 5 节
状态：已实施；覆盖/表格阈值规则与"同 run 多投影结论一致"见第 6 节

## 1. 这一项解决什么

P0-03 建立了"只有显式发布才成为正式版本"，但发布的前提条件当时由调用方给出：`publish_run(validation_status="passed")`。P0-04 把判定权移到共享规则上，并修掉了核查文档复现的第三个问题——**视图写入失败仍为 DONE**。

三个复现问题到此全部被约束：

| 复现问题 | 现在的约束 |
| --- | --- |
| 占位解析被标记完成 | P0-01 拒绝未知解析器 / 显式标记占位；P0-04 的 `placeholder_result` 规则直接 `rejected` |
| 视图写入失败仍为 DONE | 必需产物持久化失败即任务失败，且检查发生在 `DONE` 之前 |
| 失败重跑混读新旧结果 | P0-03 的 run 隔离与原子发布 |

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/validation.py](../src/parsecore/validation.py)（新增） | 发布准入规则与 `ValidationReport`；`validate_run()`（内存产物）与 `validate_staged_run()`（已封存 staged 产物） |
| [src/parsecore/publication.py](../src/parsecore/publication.py) | `ValidationStatus` 新增 `info`（不参与门禁的观察项）及其迁移；`may_publish` 对 `info` 的放行语义 |
| [src/parsecore/runtime.py](../src/parsecore/runtime.py) | `publish_run()` 默认执行共享规则；`_validation_options()` 取 `[quality_gate]` 阈值；`_artifact_requirement()`；`_assert_required_view_delivery()`；必需产物持久化提前到 `DONE` 之前 |
| [tests/test_validation.py](../tests/test_validation.py)（新增） | 23 项规则单测，覆盖三个复现问题的规则映射与优先级 |
| [tests/test_required_artifacts.py](../tests/test_required_artifacts.py)（新增） | 10 项：必需/可选产物的交付语义与运行时接线 |

## 3. 规则与阈值

| 规则 | 触发条件 | 结论 |
| --- | --- | --- |
| `unknown_artifact_profile` | profile 名未知 | `error` |
| `required_artifact_missing:<artifact>` | profile 要求的产物为空 | `rejected` |
| `empty_document` | 没有任何 block | `rejected` |
| `placeholder_result` | 结果来自占位解析器 | `rejected` |
| `cid_garble` | 文本中仍有 `(cid:N)` | `rejected` |
| `quality_signal:<code>`（severity=error） | 调用方传入的 error 级质量信号（如断裂来源引用） | `rejected` |
| `ocr_failed_pages` | 有页 OCR 失败 | `needs_review` |
| `low_reading_order_confidence` | 最差页低于 `min_reading_order_confidence`（默认 0.75） | `warning` |
| `suspect_signature_pages` | 签名页占比异常 | `warning` |
| `quality_signal:<code>`（其他 severity） | 未知 severity 一律降级为告警 | `warning` |
| `optional_artifacts_empty` | 可选产物为空 | `info`（不参与门禁） |

结论优先级：`error` > `rejected` > `needs_review` > `warning` > `passed`。`info` 只进入 `metrics.informational_rules`，不改变结论——否则一个未启用 embedding 的部署会因为 RAG 为空而被推上 warning 策略。

`error` 的含义被严格限定为"校验自身失败"（未知 profile、校验异常），与"校验判定不可接受"分开存放在 `errors` / `rejections` 两个字段，避免把校验故障误读成内容问题，也避免把内容问题误读成校验故障。

## 4. 顺序修正（本次最重要的行为变更）

原顺序：`persist index → 标记 DONE（清除 claim）→ 持久化视图（失败只记日志）`。

后果（已在本地复现）：任务已经是 `done`、claim 已清除；此时必需视图写入失败，异常进入失败处理路径，`mark_dead_letter` 因 claim 不匹配抛 `stale_claim`，调用方看到的是 `stale_claim`，而任务状态仍是 `done`——真实错误被掩盖，且无法重试。

现顺序：`persist index → 持久化必需产物并校验 → 标记 DONE`。失败时任务走正常失败路径（inline 立即 `failed` 且 `failure_reason` 为真实原因；队列模式按 `max_attempts` 重排队），任务不进入 `done`。

## 5. 验证

```text
.\.venv\Scripts\python.exe -m unittest tests.test_validation tests.test_required_artifacts tests.test_run_store tests.test_run_publication_runtime tests.test_publication_contract -q
Ran 100 tests / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
Ran 813 tests / OK (skipped=6)
```

关键用例：

| 场景 | 结果 |
| --- | --- |
| 健康 run 默认发布成功并持久化校验报告 | 通过 |
| 占位结果被规则拒绝（`rejection_codes` 含 `placeholder_result`） | 通过 |
| error 级质量信号（断裂来源引用）拒绝发布 | 通过 |
| 缺必需产物 / CID 乱码 / 空文档拒绝发布 | 通过 |
| OCR 失败页 → `needs_review`，不自动发布 | 通过 |
| 未知 severity 降级为告警而不是被丢弃或升级为拒绝 | 通过 |
| 可选产物为空 → `info`，正常部署仍 `passed` | 通过 |
| 阈值来自 `[quality_gate]` 配置并可收紧 | 通过 |
| 必需视图写入失败 → 任务 `failed` 且 `failure_reason` 为真实原因 | 通过 |
| 可选/禁用视图写入失败 → 任务仍 `done`，产生降级事件 | 通过 |

## 6. 未完成

1. **覆盖/表格阈值规则**：`[quality_gate]` 的 `min_text_page_coverage`、`min_table_unit_coverage`、`min_unit_chunk_coverage` 已进入 `_validation_options()` 并传给规则，但规则本身尚未使用覆盖率指标（需要 coverage 投影输入，属 P0-03 第 2 批的按 run 读取）。
2. **同 run 多投影结论一致**：验收要求"状态 API、Reader 与 RAG 对同 run 的结论一致"。当前校验结论持久化在 run 上，但 Reader/RAG/状态 API 尚未强制读取同一 run（正式读路径固定 run 是 P0-03 第 2 批）。
3. **warning 策略版本化配置**：`ValidationPolicy` 支持白名单但尚未从 TOML 读取，仍使用默认策略。

## 7. 回滚

- 代码回滚：`git revert` 本次提交。
- 行为回滚：`runtime.publish_run(validation_status=..., validate=False)` 或直接调用 `run_store.record_validation()`，即可绕过共享规则（宿主自定义判定路径）。
- 注意：顺序修正本身不应回滚——回滚它会把"真实错误被 `stale_claim` 掩盖且任务仍为 done"重新引入。
