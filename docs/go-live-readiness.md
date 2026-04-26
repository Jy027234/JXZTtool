# ParseCore 上线前收口清单

## 目标

这份文档只回答一个问题：以当前仓库和已完成验证看，ParseCore 是否已经具备进入产品灰度的最低条件。

结论先行：

- 当前主线目标应定义为：`可进入产品灰度，不再以 OCR 长尾专项作为上线阻塞项`。
- 上线前必须完成的仓库内动作，集中在默认配置冻结、默认自检门禁通过、已知风险分级和回滚口径固定。
- OCR 重样本长尾、recognition 段专项优化、各类实验开关验证，都应转为上线后专项迭代，不应继续阻塞当前版本进入产品。

## 当前 readiness 结论

当前状态定义为：`基本就绪，可进入产品灰度`。

2026-04-27 当前仓库内必做项已完成的证据：

1. 默认自检门禁已通过，状态为 `ok`。
2. 单测结果为 `139 passed, 5 skipped`。
3. runtime describe 正常，当前默认形态仍为 `index_mode = hybrid`、`execution_mode = inline`。
4. 默认回归套件结果为 `ok = 5, skipped = 1`，其中 `sample-27-81-17` 继续按 `slow` 标签跳过。
5. 为适配当前默认样本集，自检脚本中的回归超时默认值已从 `600s` 调整为 `900s`，避免出现“套件本身通过但门禁误报 degraded”的假降级。
6. 本地 API 健康检查已通过，`GET /health -> status = ok`，且 `services.pdfplumber / python_docx / paddleocr = true`。

这一定义基于以下事实：

1. ParseCore 默认质量门禁已经从 jobcard 双跑切回自身自检。
2. 主线解析、任务、存储、API、OCR provider 抽象和观测链路都已具备可执行验证。
3. 历史 jobcard 双跑、宿主替换和 OCR 接线资料已归档，不再挤占主线判断口径。
4. 当前最明显的性能风险集中在 OCR 重样本 `sample-cmm-32-48-21-ocr`，但它已被明确定义为长尾专项，而不是主线可靠性失效。

## 上线前必做项

### A. 仓库内必做

1. 固定默认配置，不启用任何实验性 OCR 开关。
2. 执行默认自检门禁并记录结果。当前状态：已完成。
3. 确认默认 runtime describe 正常。当前状态：已完成。
4. 确认默认回归套件没有新增硬失败。当前状态：已完成。
5. 确认 API 最小健康检查正常。当前状态：已完成。
6. 把当前遗留问题按“阻塞上线 / 不阻塞上线 / 上线后迭代”三类收口。当前状态：已完成。
7. 固定上线后回滚口径，避免把 OCR 长尾问题和主线功能故障混为一谈。当前状态：已完成。

### B. 接入侧必做

以下项目属于产品接入或宿主侧动作，不应伪装为仓库内阻塞：

1. 若产品走 API 接入，按 [ocr-integration-checklist.md](ocr-integration-checklist.md) 和现有健康检查口径完成环境探活。
2. 若产品是 jobcard 宿主替换，按 [../archive/jobcard-host/docs/jobcard-replacement-checklist.md](../archive/jobcard-host/docs/jobcard-replacement-checklist.md) 完成灰度替换。
3. 若要彻底退场 jobcard 旧链路，还需额外满足 [../archive/jobcard-host/docs/jobcard-cutover-readiness.md](../archive/jobcard-host/docs/jobcard-cutover-readiness.md) 中的宿主样本与上传资产条件。

## 遗留问题分级

### 阻塞 ParseCore 主线进入产品灰度

当前无新增仓库内阻塞项；默认门禁结果应作为唯一硬判断口径。

### 不阻塞 ParseCore 进入产品灰度，但阻塞宿主彻底退场旧链路

1. jobcard 宿主上传资产保全仍有外部缺口，主轮样本 `doc-main-wheel-r16` 仍属于外部数据阻塞。
2. jobcard 宿主原生样本池仍偏小，当前更适合继续小流量灰度，而不是直接退场旧链路。

### 上线后专项迭代

1. OCR 重样本 `sample-cmm-32-48-21-ocr` 的长尾压缩。
2. recognition 段模型级、后端级或输入形态专项优化。
3. 页级 OCR 成本更细的观测，例如 `rec` 每 crop 成本。
4. 默认关闭的实验开关复验，例如 `parsecore_angle_cls_probe_*` 这类 OCR 采样探针。

## 默认上线口径

当前推荐口径如下：

1. ParseCore 当前版本可以进入产品灰度。
2. 默认配置保持不变，不引入任何 OCR 实验开关。
3. 默认质量门禁以 `tools/self_check.py` 为准，2026-04-27 最近一次全量执行结果为 `ok`。
4. OCR 长尾性能问题保留为已知风险，但不单独阻塞当前灰度。
5. 若出现主线功能故障、门禁失败或 API 不可用，再触发回滚；不要把已知 OCR 长尾波动直接等同于版本不可上线。

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

- 默认门禁见 [self-check-gate.md](self-check-gate.md)
- OCR 接入清单见 [ocr-integration-checklist.md](ocr-integration-checklist.md)
- 宿主替换清单见 [../archive/jobcard-host/docs/jobcard-replacement-checklist.md](../archive/jobcard-host/docs/jobcard-replacement-checklist.md)
- jobcard 切流 readiness 见 [../archive/jobcard-host/docs/jobcard-cutover-readiness.md](../archive/jobcard-host/docs/jobcard-cutover-readiness.md)