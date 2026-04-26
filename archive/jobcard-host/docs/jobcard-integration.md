# jobcard 接入建议

## 当前判断

jobcard 当前是 Docker 化的 FastAPI 后端加前端一体工程，更适合先接入 ParseCore 作为后端模块，再逐步拆成 sidecar 或独立服务。

完整的宿主替换步骤见 [jobcard-replacement-checklist.md](jobcard-replacement-checklist.md)，历史双跑资料见 [../../jobcard-dual-run/README.md](../../jobcard-dual-run/README.md)，OCR 接线检查见 [../../../docs/ocr-integration-checklist.md](../../../docs/ocr-integration-checklist.md)，OCR HTTP 契约见 [../../../docs/ocr-gateway-contract.md](../../../docs/ocr-gateway-contract.md)。

## 推荐路径

### Step 1: 后端内嵌

- 在 jobcard 后端中引入 parsecore 包
- 保持原有上传接口不变
- 把 ParseCore 子应用挂到内部前缀，例如 `/internal/parsecore`
- 文档路由继续保留现有接口，对内改为调用 ParseCore 任务提交和状态查询
- 暂时使用同一数据库，减少迁移成本

### Step 2: 兼容性验证（历史双跑已归档）

- 先跑 ParseCore 自检门禁，确认解析、落库和检索链路稳定
- 再选少量宿主真实样本做兼容性复验
- 历史双跑记录与辅助脚本仅在需要回看宿主差异时使用

### Step 3: worker 外拆

- 将解析任务转为后台队列执行
- API 只负责创建 job 和查询结果
- 大文件和 OCR 任务单独扩容

## OCR 接线建议

- 若 jobcard 部署环境允许直接安装本地 OCR 依赖，可保留 `providers.ocr.provider = "rapidocr"`，由 ParseCore 在本地完成图片 OCR 和 PDF 坏页回退。
- 若 jobcard 已有统一 OCR 网关，建议把 ParseCore 切到 `providers.ocr.provider = "remote-http"`，这样 `image-ocr` parser 和 PDF 坏页回退会共用同一个宿主 OCR 通道，而不需要在 ParseCore 容器里重复维护 OCR 模型。
- `remote-http` 的最小配置是 `base_url`；若宿主网关要求鉴权，可再加 `api_key_env`。`options.endpoint_path` 与 `options.headers` 用于传输层，其余 `options` 会透传给宿主 OCR 网关。
- ParseCore 对宿主 OCR 网关的请求体是 `image_base64 / mime_type / file_name? / options?`；响应体至少需要返回 `result` 列表，列表项包含 `bbox / text / confidence`。
- 健康检查里的兼容字段名仍然是 `services.paddleocr`，但在 ParseCore 内部它代表“当前 OCR provider 可用”，不再限定为某一个具体 OCR 引擎。

## 不建议的做法

- 一开始就把 ParseCore 做成独立中台并强推 jobcard 改造
- 一开始就上复杂多租户控制台
- 用 LLM 替代结构化主解析

## 首批需要补的接口

- 创建解析任务
- 查询解析任务状态
- 获取文档 Block / Chunk
- 触发重解析
- 查询索引构建状态

## 现在已经具备的升级抓手

当前 Starter Kit 已提供两类直接可用的接入能力：

- ASGI 子应用：可通过 `mount_into_fastapi(app, ...)` 直接挂到 jobcard 的 FastAPI 主应用。
- jobcard 补丁适配：`build_jobcard_document_patch()` 会把 ParseOutcome 转成现有文档路由更容易消费的补丁结构。

当前实际进度：

- 历史双跑记录、runbook 和辅助脚本已统一归档到 [../archive/jobcard-dual-run/README.md](../archive/jobcard-dual-run/README.md)。

- 已在 jobcard 主应用内挂载 ParseCore 子应用。
- 已把普通文档库和管理文库的旧后台解析入口切到 ParseCore。
- 已保留旧后台解析逻辑作为 ParseCore bridge 不可用时的回退路径。
- 已补 3 个 jobcard 集成功能测试，覆盖子应用健康检查、普通文档解析和管理文库解析。
- 在接线过程中额外修复了 jobcard JSON 存储模式下 `store.mutate()` / `save()` 的重入锁死锁问题。
- 已在 jobcard backend 增加 `parsecore_compare.py`，可对 store 中现有文档记录做“旧解析 vs ParseCore”双跑并输出 JSON/Markdown 报告，也可直接对指定文件路径做双跑。
- 已完成真实 PDF 复验与中台修正：ParseCore 的 PDF 文本提取已改为按段落切分，并在 ParseCore 与 jobcard 两侧统一安装 `pypdf`；对 `D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf` 的最新直接文件双跑结果为相似度 0.9626，ParseCore 输出 1401 个 block / 1401 个 chunk，报告输出为 `jobcard/backend/data/parsecore_dual_run_report_direct_file.json` 和 `.md`。
- 针对这类超长文档，双跑工具已把整体相似度计算切到按页加权聚合，避免全文级 `SequenceMatcher` 在真实样本上长时间卡住，同时保留页内字符级差异判断。
- 旧基线可作为回归对照：早期按页单块版本对同一 PDF 仅输出 255 个 block / 255 个 chunk，虽然全文相似度更高，但结构粒度不足，难以支撑后续 Block/Chunk 级差异分析。
- 双跑报告现已包含字段级差异摘要、页面级差异摘要、页面块密度排行、Block 长度统计、Chunk 长度统计、索引命中差异摘要、第一版 Block 对位差异摘要、展示口径差异摘要和展示口径 Block 对位摘要；索引部分当前以 compare 层关键词命中探针表达 legacy embedding 状态、ParseCore chunk 可索引性和命中对比，可直接定位“文本接近但索引准备度或检索命中存在偏差”的问题，而不再只依赖全文相似度；Block 对位部分已经从顺序硬对齐升级为页内动态规划匹配，用于减少插入/缺失块引起的整页连锁错配，并输出最差块对列表；展示口径部分当前对 legacy 与 ParseCore 两侧页面统一做重复页眉页脚去重，用于更贴近 jobcard 入库和前端显示时的可见文本；展示口径 Block 对位则进一步在去重后的页面文本上按统一切段对位，用于识别“文本看起来更接近，但结构切段仍不一致”的情况。
- 对 `D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf` 的最新直接文件双跑报告中，索引摘要显示 legacy 侧无 embedding 元数据、ParseCore 侧可索引 chunk 数为 1401，关键词命中探针为 4 比 4，作为后续接入正式索引链路前的稳定基线。
- 同一份真实 PDF 的 raw Block 对位摘要在升级为页内动态规划匹配后，legacy 侧仍可拆出 885 个块、ParseCore 侧为 1400 个内容块，但平均对位相似度已从 0.3392 提升到 0.5419，且仍有 215 页存在块数差异。这说明顺序错配已经显著减少，但结构切分粒度差异依旧存在，可作为下一轮块匹配优化后的新观察基线。
- 同一份真实 PDF 的展示口径摘要当前显示：raw 相似度为 0.9626，而去重重复页眉页脚后的展示相似度提升到 0.9641；legacy/ParseCore 分别有 246/247 页发生去重，长度差也从 -4862 收敛到 -1356。这说明你指出的“入库或前端展示可能去掉重复页眉页脚”确实会影响人工感知差异，后续看双跑结果时应同时参考 raw 和 display 两套口径。
- 同一份真实 PDF 的展示口径 Block 对位摘要当前显示：尽管去重页眉页脚后全文展示相似度上升到 0.9641，但按统一切段并升级为页内动态规划匹配后的块对位平均相似度也只提升到 0.2927，且仍有 221 页存在块数差异。这说明去掉展示噪声和修复顺序错配后，剩余问题仍主要来自结构切段策略本身；后续如果要把 Block 对位做成更可解释的质量指标，需要继续增强块匹配策略，或回到解析器侧继续收敛切段边界。

## 建议替换顺序

### 文档库

普通文档库这一步已经完成首轮接线，当前状态是：

1. 上传完成后仍保持原接口。
2. 点击解析时优先调用 ParseCore。
3. ParseCore 输出会回填到原文档记录的 `parsedTextContent`、`parseStatus` 和 `parsecore` 字段。
4. 若 ParseCore bridge 不可用，则回退到旧解析逻辑。

### 管理文库

管理文库也已经完成同样的首轮接线，当前与普通文档库保持同样策略。

## 下一步重点

1. 把 ParseCore 自检门禁固定为 `unittest`、`tools/regression_baseline.py check-suite`、`GET /health` 和最小解析 smoke。
2. 宿主侧只保留少量真实样本的灰度复验，不再默认继续扩大双跑样本池。
3. 若宿主出现兼容性问题，再回到 [../archive/jobcard-dual-run/README.md](../archive/jobcard-dual-run/README.md) 中的记录和脚本复现旧场景。
4. 在此基础上继续收敛 PDF 生产级策略，并评估部署时是继续走 inline，还是切换到 `queue-worker` 模式。

如果要回看当前仓库已经完成的 jobcard 历史联调路径，优先按 [../archive/jobcard-dual-run/README.md](../archive/jobcard-dual-run/README.md) 进入；当前 OCR provider 的独立验收则继续按 [ocr-integration-checklist.md](ocr-integration-checklist.md) 补齐。

## 最小集成示例

```python
from parsecore.jobcard import mount_into_fastapi

mount_into_fastapi(app, config_path="parsecore.toml", prefix="/internal/parsecore")
```

当 ParseCore 完成解析后，可把结果映射为 jobcard 文档补丁：

```python
from parsecore.jobcard import build_jobcard_document_patch

patch = build_jobcard_document_patch(outcome)
# 然后把 patch 写回 jobcard 自身的文档 store
```
