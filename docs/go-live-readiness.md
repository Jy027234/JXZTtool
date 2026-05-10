# ParseCore 上线前收口清单

## 目标

这份文档只回答一个问题：以当前仓库和已完成验证看，ParseCore 是否已经具备交付给宿主产品试运行和进入受控生产灰度的最低条件。

结论先行：

- 当前主线目标定义为：`0.1.0 可交付灰度版`。
- 交付结论：`可以交付试运行，可进入受控生产灰度`。
- 上线前必须完成的仓库内动作，集中在测试通过、配置冻结、使用说明交付、已知风险分级和回滚口径固定。
- OCR 重样本长尾、复杂 PDF 表格 profile 自动切换、Parquet/截图/trace 包等增强项，应转为上线后专项迭代，不阻塞当前版本交付。

## 当前 readiness 结论

当前状态定义为：`可交付试运行，可进入受控生产灰度`。

2026-05-10 当前仓库内必做项已完成的证据：

1. 全量测试通过：`322 passed, 5 skipped`。
2. `git diff --check` 通过。
3. runtime/API/worker/导出/records/document views 主链路已有测试覆盖。
4. 17,101 页真实 PDF 样本已有完整解析产物：`pages=17,101`、`lines=1,249,000`、`records=454,985`。
5. records 查询已从主文档快照中拆出，默认不再全量加载大 views。
6. 异步导出包直接写文件，支持 `jsonl/csv/tsv/sqlite/xlsx`。
7. 配置手册、发版说明和使用说明已补齐。

这一定义基于以下事实：

1. ParseCore 默认质量门禁已经完全收口到自身自检。
2. 主线解析、任务、存储、API、OCR provider 抽象和观测链路都已具备可执行验证。
3. 历史单一宿主联调与 OCR 接线资料已归档，不再挤占主线判断口径。
4. 当前剩余风险集中在 OCR 重样本、复杂跨页表格和超大 records HTTP streaming 增强，但这些已被定义为上线后专项，而不是主线可靠性失效。

## 上线前必做项

### A. 仓库内必做

1. 固定默认配置，不启用未验证的实验开关。
2. 执行全量测试并记录结果。当前状态：已完成。
3. 执行 `git diff --check`。当前状态：已完成。
4. 确认默认 runtime describe 正常。当前状态：建议发版当天再执行一次。
5. 确认 API 最小健康检查正常。当前状态：建议发版当天再执行一次。
6. 把当前遗留问题按“阻塞上线 / 不阻塞上线 / 上线后迭代”三类收口。当前状态：已完成。
7. 固定上线后回滚口径，避免把 OCR 长尾或复杂样本专项和主线功能故障混为一谈。当前状态：已完成。
8. 将 [release-notes.md](release-notes.md) 与 [user-guide.md](user-guide.md) 一并交付给接入方。当前状态：已完成。

### B. 接入侧必做

以下项目属于产品接入或宿主侧动作，不应伪装为仓库内阻塞：

1. 若产品走 API 接入，按 [ocr-integration-checklist.md](ocr-integration-checklist.md) 和现有健康检查口径完成环境探活。
2. 若环境对外暴露 ParseCore HTTP 接口，显式配置 `runtime.api_key_env`，并验证 `/health` 可匿名、其余接口需要 `x-api-key` 或 `Authorization: Bearer`。
3. 若产品依赖 `remote-http` OCR 网关，补齐网关契约验证、失败回滚与监控口径。

## 遗留问题分级

### 阻塞 ParseCore 主线进入产品灰度

当前无新增仓库内阻塞项；全量测试、健康检查和真实样本抽检结果应作为硬判断口径。

### 当前无单一宿主退场类阻塞项

单一宿主切换计划已经终止；本仓库不再维护任何“单一宿主退场旧链路”的前置条件或 readiness 清单。

### 上线后专项迭代

1. OCR 重样本长尾压缩。
2. 复杂 PDF 表格 profile 自动切换和列错位恢复增强。
3. records HTTP 响应进一步 streaming 化。
4. Parquet、异常页截图包、raw cells trace 包。
5. 页级 OCR 成本更细的观测，例如 `rec` 每 crop 成本。

## 默认上线口径

当前推荐口径如下：

1. ParseCore 当前版本可以交付试运行，并进入受控生产灰度。
2. 默认配置保持不变，不引入未验证实验开关。
3. 发版前至少跑全量 `pytest -q`、`git diff --check`、runtime describe 和 API health。
4. 大 PDF 必须优先走异步 job、part 拆分和导出包，不把同步 HTTP 请求作为主链路。
5. OCR 长尾性能问题保留为已知风险，但不单独阻塞当前灰度。
6. 若出现主线功能故障、门禁失败或 API 不可用，再触发回滚；不要把已知复杂样本长尾波动直接等同于版本不可上线。

## 回滚触发条件

满足以下任一条件时，应暂停灰度或回滚到上一稳定版本：

1. 默认自检门禁出现硬失败。
2. runtime describe 或健康检查显示主解析能力异常。
3. API 主路径出现稳定复现的错误响应或功能缺失。
4. 默认回归样本出现结构性退化，而非单次长尾抖动。
5. 接入侧观测到 OCR 失败页持续增加，且影响已进入灰度的真实产品样本。

## 推荐执行顺序

1. 先执行一次默认自检门禁并记录结果。
2. 确认当前版本保持默认配置，无实验项污染。
3. 把当前遗留问题和回滚口径同步到产品接入方。
4. 进入小流量灰度。
5. 把 OCR 长尾专项转为上线后任务，不再阻塞主线。

## 关联文档

- 发版说明见 [release-notes.md](release-notes.md)
- 使用说明见 [user-guide.md](user-guide.md)
- 默认门禁见 [self-check-gate.md](self-check-gate.md)
- OCR 接入清单见 [ocr-integration-checklist.md](ocr-integration-checklist.md)
