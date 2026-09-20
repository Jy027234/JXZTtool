# P0-01 交付报告：配置与能力必须真实

日期：2026-09-20
基线：`main @ 14cc7f2`（本文提交前）
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) P0-01
状态：已实施，回归通过；已复核的既有失败见第 5 节

## 1. 解决的问题

核查文档复现的三个交付问题中，P0-01 负责其中两个的**可见性**，并消除了配置层的静默降级：

| 原问题 | 原行为 | 现行为 |
| --- | --- | --- |
| 未实现解析器 | 未知 `[[parsers]]` 名称静默落到 `StubParser`，任务 `done`，正文是固定占位文本 | 启动即拒绝（`unknown_parser_name`）；仅当 `project.allow_placeholder_parsers = true` 时才用占位解析器，且结果被显式标记为非正式 |
| 索引配置不匹配 | `index.mode = "pgvector"` + SQLite 静默返回 `NullIndex` | 启动即拒绝（`index_backend_mismatch`）；`hybrid`/`null`/`memory` 的内存索引仍可用，但报告为非持久（degraded） |
| 能力状态不可见 | 只有 `/health` 的模块可用性布尔值；embedding/rerank 初始化失败被静默替换为 Null 实现 | `/health`、`/v1/runtime`、`runtime.capability_report()` 输出完整能力报告：required/optional/disabled、`degraded_capabilities`、`accepting_jobs`、`formal_parse_ready`、`placeholder_parsers`、`mock_capabilities` |

未解决的相邻问题（有意留给后续工作包）：占位结果与降级运行仍会进入 `done`，完成时序与质量准入属于 P0-03/P0-04；文档级原子发布属于 P0-02/P0-03。

## 2. 代码变更

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/capabilities.py](../src/parsecore/capabilities.py)（新增） | 实现注册表（parser/index/llm/embedding/rerank/ocr/数据库 scheme）、启动期配置校验、稳定错误码 `ERROR_CODES`、能力报告 `build_capability_report`、能力指纹 |
| [src/parsecore/config.py](../src/parsecore/config.py) | 新增 `project.strictness`、`project.allow_placeholder_parsers`、`runtime.allow_mock_providers`、`runtime.capability_optional`；`load_settings()` 末尾调用能力校验 |
| [src/parsecore/bootstrap.py](../src/parsecore/bootstrap.py) | `_build_index()` 拒绝未知 mode 与 pgvector/非 PG 组合；`_build_parsers()` 按开关拒绝或显式使用占位解析器；`build_runtime()` 在 `database_url_override` 后重新校验；embedding/rerank 注入路径保持可被宿主 patch |
| [src/parsecore/parsers.py](../src/parsecore/parsers.py) | `build_parser()` 对未知名称抛 `KeyError`（不再返回 Stub）；新增 `build_parser_or_placeholder()` 供已显式开关的调用方使用；补 `docling`/`pymupdf4llm`/`ocr` 别名 |
| [src/parsecore/stubs.py](../src/parsecore/stubs.py) | `StubParser.is_placeholder = True`；`NullIndex`/`NullEmbeddingProvider`/`NullRerankProvider` 标注 `is_degraded` + `degradation_reason` |
| [src/parsecore/runtime.py](../src/parsecore/runtime.py) | 新增 `capability_report()`、`placeholder_parser_names()` 与占位结果记录（写入 job options、事件日志、`ParseOutcome.placeholder_result`）；`describe()` 暴露能力摘要与指纹 |
| [src/parsecore/models.py](../src/parsecore/models.py) | `ParseOutcome.placeholder_result` |
| [src/parsecore/api_health.py](../src/parsecore/api_health.py) | `health_capabilities()`；OCR 探活按能力缓存（TTL 30s），注入探针不缓存；`clear_probe_cache()` |
| [src/parsecore/api_routes.py](../src/parsecore/api_routes.py) | `/health` 增加 `capabilities` 与 `delivery_status`；`status` 仍为存活语义 |
| [src/parsecore/asgi.py](../src/parsecore/asgi.py) | 注入 `health_capabilities` |
| [src/parsecore/api_payloads.py](../src/parsecore/api_payloads.py) | 占位结果附加到 `/parse` 与 `/parse/batch` 成功响应 |
| [src/parsecore/cli.py](../src/parsecore/cli.py) | 配置被拒时输出 JSON（`code`/`field`/`message`）并返回退出码 `2`，不打印 traceback |
| [tools/provider_comparison_report.py](../tools/provider_comparison_report.py) | 只有评估配置显式开启占位时才使用占位解析器 |
| [parsecore.toml](../parsecore.toml)、[parsecore.pgvector.fake-embedding.toml.example](../parsecore.pgvector.fake-embedding.toml.example) | 显式写出新字段；fake-embedding 模板显式 `allow_mock_providers = true` |
| [docs/configuration.md](configuration.md) | 新增字段说明与错误码表 |

## 3. 契约与语义

### 3.1 稳定错误码

`CapabilityConfigurationError` 同时继承 `RuntimeError` 与 `ValueError`（兼容既有调用方按值错误处理的代码），并携带 `code` / `field`。

| 错误码 | 触发条件 |
| --- | --- |
| `unknown_parser_name` | 未知 `[[parsers]]` 名称且未开启占位 |
| `unknown_index_mode` | `index.mode` 未知 |
| `index_backend_mismatch` | `pgvector` + 非 PostgreSQL |
| `unknown_database_scheme` | 非 sqlite/postgresql/memory |
| `unknown_llm_provider` / `unknown_embedding_provider` / `unknown_rerank_provider` / `unknown_ocr_provider` | provider 名称未实现 |
| `mock_providers_not_allowed` | 启用 mock provider 但未开启 `runtime.allow_mock_providers` |
| `unknown_capability_name` | `runtime.capability_optional` 含未知能力 |
| `invalid_strictness_mode` | `project.strictness` 不是 `strict`/`lenient` |

### 3.2 能力分级与就绪语义

- `required`：启用后不可用即 `accepting_jobs = false`（例如显式启用的 embedding 缺少凭据）。
- `optional`：默认 `rerank`、`ocr`、`index`，可用 `runtime.capability_optional` 扩展（含 `parser:<name>`）；不可用时进入 `degraded_capabilities`，不阻塞交付。
- `disabled`：未启用，报告为 `disabled`，不计为故障；`embedding`/`llm` 关闭属于此类，非 RAG profile 不受影响。
- 占位解析器：阻塞 `formal_parse_ready`，不阻塞 `accepting_jobs`（显式开发开关）。
- `/health.status` 保持 `"ok"`（进程存活，避免被 supervisor 重启），`delivery_status` 取 `ok` / `degraded` / `not_ready`。

## 4. 验证

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 全量单元测试 | `.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q` | `Ran 668 tests / OK (skipped=6)`（P0-01 新增 26 项） |
| 新增测试 | `python -m unittest tests.test_capabilities -q` | `Ran 26 tests / OK` |
| 核查文档指定子集 | `python -m unittest tests.test_runtime tests.test_bootstrap_routing tests.test_worker tests.test_ir tests.test_provider_provenance tests.test_config_parser_options tests.test_provider_failures tests.test_p0_release_readiness -q` | `Ran 92 tests / OK` |
| 配置模板 | 逐一 `load_settings()` 全部 `parsecore*.toml` / `*.example` | 10/10 OK |
| 自检门禁（跳过已知回归项） | `python -m parsecore.cli self-check --profile fast --skip-regression --skip-payload-contracts --out var/self-check/p0-01-fast-noregress.json` | `status ok`：`unit_tests` / `runtime_describe` / `provider_comparison_suite` 全部通过 |
| CLI 拒绝路径 | `python -m parsecore.cli describe --config <未知解析器配置>` | 退出码 `2`，stderr 输出 `unknown_parser_name` JSON |

占位探针复现结果：未知名称 + 显式开关的配置可以完成解析，但 `outcome.placeholder_result`、job options、事件 `completed_with_placeholder_result`、`/health.capabilities.placeholder_parsers` 同时标记，且 `formal_parse_ready = false`。

## 5. 已知未通过项（与本项无关）

`tools/regression_baseline.py check-suite --suite var/regression/suite.fast.json` 在 fast 三个样本上失败：

```text
REGRESSION DETECTED:
  - 36d65cd6b61346e28e97dbaf829646de.pdf: numeric_heavy_total grew by 38 (budget +2)
  - 36d65cd6b61346e28e97dbaf829646de.pdf: suspected_header_footer_total grew by 39 (budget +2)
```

已在 `git worktree` 中的未修改 HEAD（`14cc7f2`）上复现**完全相同**的差值（+38 / +39 与 +34 / +34），因此该回归先于本次改动存在，属于 P0-06 范畴（基线/样本与发布门禁修复），不是 P0-01 的引入问题。本轮未修改任何解析算法。

## 6. 回滚

- 代码回滚：`git revert` 本次提交即可；新配置字段均为带默认值的可选字段，旧 TOML 无需修改。
- 兼容性影响（需在升级说明中标注）：
  1. 未知解析器名称从“静默可用”变为**启动失败**；如需临时兼容，可设 `project.strictness = "lenient"` 与 `project.allow_placeholder_parsers = true`，但 `/health` 会持续报告未就绪。
  2. `index.mode = "pgvector"` + SQLite 从“静默内存索引”变为**启动失败**，必须修配置。
  3. mock provider 需显式 `runtime.allow_mock_providers = true`（仅联调环境）。
- 未改变任何对外响应的既有字段；`/health`、`/v1/runtime`、`/parse` 均为新增字段。
