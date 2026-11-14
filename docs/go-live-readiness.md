# ParseCore 上线前收口清单

## 目标

这份文档区分仓库内本地验收与目标环境上线验收。现有本地结果可用于宿主集成试运行判断，但不构成生产部署或生产灰度验收。

结论先行：

- 当前版本定位为：`0.1.0 可交付灰度版`；版本定位不等于已获生产准入。
- 本地 SQLite 正式版本发布已有实现；生产部署以及 Postgres/pgvector 正式发布仍未验证。
- 2026-11-14 本地 fast 发版门禁通过。
- 上线前仍须在目标宿主环境完成配置、健康检查、真实样本/流量、持久化、权限、备份和回滚验证。
- OCR 重样本长尾、复杂 PDF 表格 profile 自动切换、Parquet/截图/trace 包等属于后续迭代候选；其处理不改变当前本地验收结论，也不能替代目标环境生产验收。

## 当前 readiness 结论

当前状态：已有本地验收证据，可用于集成试运行评估；生产 readiness 尚未建立。

当前可确认的是本地验收状态，不是生产 readiness：

1. 2026-11-14 最终本地 fast 发版门禁结果为 `ok / verified`，1,214 项测试通过、6 项跳过。
2. SQLite 正式发布路径已在本地 run/预览/发布切换链路中验证。
3. Postgres/pgvector 当前限存储与索引联调；原子发布、待审 run 读取和 run 隔离仍未具备/未验证。
4. 生产部署、真实宿主权限/容量/备份及目标环境健康检查没有本仓库证据。

17,101 页 PDF 的产物数据来自 2026-05-10 历史抽检。[本轮总验收](parsecore-reliability-mineru-todo-20260920.md#12-p1-至-p3-总验收)

本地验收结论基于以下事实：

1. ParseCore 默认质量门禁已经完全收口到自身自检。
2. 主线解析、任务、存储、API、OCR provider 抽象和观测链路都已具备可执行验证。
3. 历史单一宿主联调与 OCR 接线资料已归档，不再挤占主线判断口径。
4. 当前剩余风险集中在 OCR 重样本、复杂跨页表格和超大 records HTTP streaming 增强，但这些已被定义为上线后专项，而不是主线可靠性失效。

## 上线前必做项

### A. 仓库内必做

1. 固定默认配置，不启用未验证的实验开关。
2. 运行 `python -m parsecore.cli self-check --require-verified-corpus` 并记录结果，其中包含根目录 pytest 测试。
3. 执行 `git diff --check`；本次交付前检查此次实际变更。
4. 确认默认 runtime describe 正常，并保存目标配置的输出。
5. 在目标环境确认 API 健康检查、鉴权、存储可用性和回滚方式。
6. 按“生产阻塞 / 生产前置 / 上线后迭代”分类遗留事项；当前生产/pgvector 发布验证仍缺。
7. 固定目标环境的回滚口径，并在部署演练中验证。
8. 将 [release-notes.md](release-notes.md) 与 [user-guide.md](user-guide.md) 一并交付给接入方。

### B. 接入侧必做

以下项目属于产品接入或宿主侧动作，不应伪装为仓库内阻塞：

1. 若产品走 API 接入，按 [ocr-integration-checklist.md](ocr-integration-checklist.md) 和现有健康检查口径完成环境探活。
2. 若环境对外暴露 ParseCore HTTP 接口，显式配置 `runtime.api_key_env`，并验证 `/health` 可匿名、其余接口需要 `x-api-key` 或 `Authorization: Bearer`。
3. 若产品依赖 `remote-http` OCR 网关，补齐网关契约验证、失败回滚与监控口径。

## 遗留问题分级

### 生产灰度 readiness

目前没有证据可以给出“生产灰度已就绪”的结论。Postgres/pgvector 正式发布与 run 隔离缺口、目标环境验证及生产部署检查应在对应范围完成后再判断；本地 fast/release 验收不能替代这些检查。

### 当前无单一宿主退场类阻塞项

单一宿主切换计划已经终止；本仓库不再维护任何“单一宿主退场旧链路”的前置条件或 readiness 清单。

### 后续专项迭代

1. OCR 重样本长尾压缩。
2. 复杂 PDF 表格 profile 自动切换和列错位恢复增强。
3. 大导出包生成的后台执行与分页读取可按宿主需求推进。已生成文件的分块下载验收见[当前开发与证据入口](parsecore-reliability-mineru-todo-20260920.md)。
4. Parquet、异常页截图包、raw cells trace 包。
5. 页级 OCR 成本更细的观测，例如 `rec` 每 crop 成本。

## 默认上线口径

当前推荐口径如下：

1. ParseCore 当前版本可用于本地或宿主集成试运行；是否进入生产灰度取决于目标环境的单独验收，当前尚未验证。
2. 默认配置保持不变，不引入未验证实验开关。
3. 发版候选至少运行根目录 `python -m pytest -q`、`git diff --check`、runtime describe、API health 与 `--require-verified-corpus` fast 发布验收；仍需补目标环境验证。
4. 大 PDF 必须优先走异步 job 和 part 拆分。导出包 POST 当前在请求内生成并落盘后返回 `202`，不等于后台生成；records 可能被全量读取。
5. OCR 长尾性能问题保留为已知风险；是否适用于目标灰度仍须结合目标环境和真实样本判断。
6. 若出现主线功能故障、门禁失败或 API 不可用，再触发回滚；不要把已知复杂样本长尾波动直接等同于版本不可上线。

## 回滚触发条件

满足以下任一条件时，应暂停灰度或回滚到上一稳定版本：

1. 默认自检门禁出现硬失败。
2. runtime describe 或健康检查显示主解析能力异常。
3. API 主路径出现稳定复现的错误响应或功能缺失。
4. 默认回归样本出现结构性退化，而非单次长尾抖动。
5. 接入侧观测到 OCR 失败页持续增加，且影响已进入灰度的真实产品样本。

### 回滚条件量化阈值

| # | 条件 | 量化指标 | 触发阈值 | 采集方法 |
|---|------|----------|----------|----------|
| 1 | **Schema 破坏** | `schema_version` 一致性 | 任一 projection 的 `schema_version` 与上一稳定版本不一致 | `tools/self_check.py` 对比 `baseline.schema_version` |
| 2 | **质量退化** | coverage 类指标下降率 | `coverage.unit_coverage_avg` 或 `coverage.chunk_coverage_avg` 下降 > 0.02 | `tools/parse_perf_baseline.py` 对比基线 |
| 3 | **Provider Drift** | provider 输出差异率 | `provider_comparison_report` 显示任一 provider 的 `blocks_diff_rate` > 0.1 | `tools/provider_comparison_report.py` |
| 4 | **性能超预算** | 解析耗时增长率 | `elapsed_s_p50` 增长 > 20% 或 `elapsed_s_p95` 增长 > 50% | `tools/parse_perf_baseline.py` 对比基线 |
| 5 | **关键样本失败** | regression baseline 失败率 | `tools/regression_baseline.py` 失败样本数 > 0 | `tools/regression_baseline.py` |
| 6 | **读序低置信** | `reading_order_confidence_avg` 下降率 | 下降 > 0.05 | coverage projection 聚合 |
| 7 | **门禁硬失败** | `gate_failed` 计数 | `runtime_describe` 中 `gate_failed` > 0 | `parsecore health` 或 `/health` API |

### 回滚判定流程

1. **采集基线**：发版前执行 `tools/self_check.py`、`tools/parse_perf_baseline.py`、`tools/regression_baseline.py`，记录基线数据。
2. **灰度监控**：灰度期间每 4 小时执行一次上述采集脚本，与基线对比。
3. **触发判定**：任一指标超过阈值时，暂停灰度并排查根因。
4. **回滚执行**：若根因无法在 2 小时内修复，回滚到上一稳定版本并通知接入方。

## 推荐执行顺序

1. 先执行一次默认自检门禁并记录结果。
2. 确认当前版本保持默认配置，无实验项污染。
3. 把当前遗留问题和回滚口径同步到产品接入方。
4. 完成目标环境验收后，再评估是否进入小流量灰度。
5. 按灰度中的真实 OCR 长尾样本安排专项，不把已有本地验收当作这些样本的结论。
6. 若 `--large-pdf-benchmark` 配置可用，执行大样本 benchmark 门禁：`.venv/Scripts/python.exe -m tools.self_check --large-pdf-benchmark var/regression/large-pdf-benchmark.config.json`。

## 关联文档

- 发版说明见 [release-notes.md](release-notes.md)
- 使用说明见 [user-guide.md](user-guide.md)
- 默认门禁见 [self-check-gate.md](self-check-gate.md)
- OCR 接入清单见 [ocr-integration-checklist.md](ocr-integration-checklist.md)
