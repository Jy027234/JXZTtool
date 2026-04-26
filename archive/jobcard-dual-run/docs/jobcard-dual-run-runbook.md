# jobcard 双跑操作手册

## 目标

本手册用于把 jobcard 的首轮双跑收口到一套可重复执行的流程。前提是假设 ParseCore 侧已经采用当前仓库验证过的 fake-embedding + pgvector 运行形态，以便先验证解析、落库和检索链路，而不依赖外部 embedding key。

## 当前 ParseCore 侧基线

当前仓库已完成以下 live 验证，可作为 jobcard 联调前置基线：

1. `GET /health` 返回 `status = ok`，且 `services.pdfplumber / python_docx / paddleocr = true`。
2. DOCX 样本已验证可解析、可 `re-embed`、可落 `chunk_embeddings`，检索模式能进入 `hybrid`。
3. 真实 PDF 样本 `25-51-06.pdf` 已验证 `parser = pdf-text`、`total_pages = 45`、`blocks = 46`、`chunks = 46`、`chunk_embeddings = 46`、`chunks_with_embedding = 46`，检索模式为 `hybrid`。

## 阶段 0：准备 ParseCore 联调环境

1. 在本仓库根目录设置运行配置：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.fake-embedding.toml.example"
```

2. 启动 API、worker 与 Postgres：

```powershell
docker compose --profile pgvector up -d --build
```

3. 探活：

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
```

4. 若宿主联调需要 OCR 网关，同时按 [ocr-integration-checklist.md](ocr-integration-checklist.md) 完成 OCR provider 验收。

补充：若你准备跑 jobcard 自己 store 中的文档，而不是 `--file` 直跑文件，需要额外确认两件事：

1. `JOB_CARD_UPLOAD_DIR` 必须指向实际还保存着上传文件 hash 名称的目录；若宿主上传文件实际放在共享目录，可临时设置为 `D:\app\uploads` 后再运行 compare。
2. 先检查 live store 里是否真的有可用样本。2026-04-26 当前运行态已扩展为 `documents = 5`、`mgmt_documents = 2`：除 `doc-527d3fe173db` 外，已新增 `doc-parsecore-docx-store-seed`、`doc-flight-ops-r2-store-seed`、`mgmt-parsecore-docx-store-seed`、`doc-1f24155682db` 与 `mdoc-cb4a937bffe8` 五条样本；但 `doc-main-wheel-r16` 仍因缺少 `sample-main-wheel-r16.pdf` 处于阻塞状态。
3. 若 live store 样本不足，可使用仓库里的 `../tools/seed_jobcard_live_store.py` 把共享上传目录中的现有文件注册进 jobcard live store，再执行 store-backed compare。该工具会调用 jobcard 自己的 Python 环境完成写入，不依赖当前 ParseCore venv 是否安装 jobcard 依赖。
4. 若需要补“宿主原生上传”证据，可使用 `../tools/upload_jobcard_native_sample.py` 先登录 jobcard，再通过 `/api/v1/documents/upload` 或 `/api/v1/mgmt-documents/upload` 走真实上传路由生成新样本。

## 阶段 1：确定双跑样本

1. 至少准备一份 DOCX 和一份真实 PDF。
2. PDF 优先选择已知能稳定复现的技术手册样本，避免首轮就引入扫描质量过差的新变量。
3. 对每个样本固定 `doc_id`、`tenant_id`、原始文件路径和宿主文档 ID，保证结果可追溯。
4. 从 [jobcard-dual-run-record-template.md](jobcard-dual-run-record-template.md) 复制一份本轮联调记录，并在样本执行前先填好环境快照、样本标识和预期验收项。

## 阶段 2：jobcard 接线方式

可选两种形态，优先按宿主当前状态选择：

1. 内嵌模式：在 jobcard FastAPI 主应用中挂载 ParseCore 子应用，保留宿主原有上传接口，仅把后台解析入口切到 ParseCore。
2. sidecar 模式：jobcard 继续保留上传与结果回填，只把解析请求发到独立的 ParseCore 服务。

无论哪种方式，首轮双跑都要保留“旧实现输出”和“ParseCore 输出”两份结果，不要直接覆盖旧链路。

## 阶段 3：首轮双跑步骤

1. 用 jobcard 原有入口提交样本，保留旧解析结果。
2. 对同一份样本调用 ParseCore，保留 `doc_id / tenant_id / parser / total_pages / blocks / chunks / retrieval_mode`。
3. 若 jobcard 仓库已接入 `parsecore_compare.py`，优先使用它生成 JSON/Markdown 报告；若尚未接入，则至少人工比对 `plainText`、Block 数、Chunk 数和搜索命中。
4. 对每个样本记录以下最小验收指标：
   - 宿主侧字段回填是否成功
   - ParseCore 返回的 `parser` 是否符合预期
   - 文档读取是否能成功返回 `blocks` 与 `chunks`
   - `GET /v1/parse/documents/{doc_id}/search` 的 `retrieval_mode` 是否进入 `hybrid` 或至少稳定落在 `keyword-fallback`
   - Postgres 中是否存在对应的 `parse_jobs / blocks / chunks / chunk_embeddings`

当前已验证的宿主 store-backed 例子：

1. `doc-527d3fe173db`：`text_similarity = 0.958`，legacy/ParseCore 非空页数 254/254，ParseCore `chunk_count = 2099`，probe 命中 4 比 4。
2. `doc-parsecore-docx-store-seed`：`text_similarity = 0.8212`，legacy/ParseCore 页数 1/1，ParseCore `chunk_count = 640`，probe 命中 4 比 4。
3. `doc-flight-ops-r2-store-seed`：`text_similarity = 0.8890`，legacy/ParseCore 页数 574/573，ParseCore `chunk_count = 2145`，probe 命中 5 比 5。
4. `mgmt-parsecore-docx-store-seed`：`text_similarity = 0.8212`，legacy/ParseCore 页数 1/1，ParseCore `chunk_count = 640`，probe 命中 5 比 5。
5. `doc-1f24155682db`：通过宿主原生 `/api/v1/documents/upload` 生成，`text_similarity = 0.8890`，legacy/ParseCore 页数 574/573，ParseCore `chunk_count = 2145`，probe 命中 5 比 5。
6. `mdoc-cb4a937bffe8`：通过宿主原生 `/api/v1/mgmt-documents/upload` 生成，`text_similarity = 0.8212`，legacy/ParseCore 页数 1/1，ParseCore `chunk_count = 640`，probe 命中 5 比 5。

这说明只要宿主上传文件仍在，`documents` 与 `mgmt_documents` 两条 store-backed 路径本身都是可跑通的，且宿主原生上传闭环也已拿到首批证据；当原生 live store 样本不足时，仍可以先用 seed 工具补齐可复现样本，再继续灰度验证。

## 阶段 4：建议验收顺序

1. 先验收 DOCX：确认宿主回填、文档读取和 `re-embed` 路径正常。
2. 再验收 PDF：确认页数、结构块数量、OCR 事件和检索模式稳定。
3. 最后再扩大样本面：把双跑从单文件扩大到一批真实文档，并开始看字段级、页面级和块级差异。

## 阶段 5：异常定位

1. jobcard 可提交但 ParseCore 无文档结果：优先检查 `tenant_id` 是否传对，以及宿主是否正确回填 `doc_id`。
2. 文档已解析但 `retrieval_mode = keyword-fallback`：优先检查 `chunk_embeddings` 是否写入，以及当前配置是否仍是 `provider = "fake"` 或真实 provider 是否可用。
3. OCR 相关问题：转到 [ocr-integration-checklist.md](ocr-integration-checklist.md) 看 provider 可用性、失败页事件和 Prometheus 指标。
4. 结构差异明显但文本相近：优先查看 jobcard 的双跑报告，不要直接把 legacy 当作唯一 ground truth。

## 阶段 6：切流前最低门槛

1. 样本集上的宿主字段回填稳定。
2. ParseCore 文档读取、重跑、检索链路可独立工作。
3. 至少一份 DOCX 和一份 PDF 完成完整双跑，且结果可追踪。
4. OCR、embedding、pgvector 三条辅助链路的失败信号都可从事件或指标面定位。

截至 2026-04-26，`doc-1f24155682db` 与 `mdoc-cb4a937bffe8` 已分别满足 `documents` / `mgmt_documents` 的首条宿主原生上传样本要求。

## 关联文档

- 宿主替换总清单见 [../../jobcard-host/docs/jobcard-replacement-checklist.md](../../jobcard-host/docs/jobcard-replacement-checklist.md)
- jobcard 接线背景见 [../../jobcard-host/docs/jobcard-integration.md](../../jobcard-host/docs/jobcard-integration.md)
- OCR 接入清单见 [ocr-integration-checklist.md](ocr-integration-checklist.md)
- 联调记录模板见 [jobcard-dual-run-record-template.md](jobcard-dual-run-record-template.md)

## 样本台账

| 样本名 | 宿主文档 ID | doc_id | tenant_id | 文件类型 | 文件路径 | 执行方式 | 预期重点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 示例-DOCX |  |  |  | DOCX |  | store / direct-file | 回填、读取、re-embed |
| 示例-PDF |  |  |  | PDF |  | store / direct-file | 页数、OCR、search、pgvector |

## 结果记录

| 样本名 | legacy 已执行 | ParseCore 已执行 | parser | total_pages | blocks | chunks | retrieval_mode | 宿主字段回填 | chunk_embeddings 已落库 | 双跑报告路径 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  | 是 / 否 | 是 / 否 |  |  |  |  | hybrid / keyword-fallback / n/a | 是 / 否 | 是 / 否 |  | 通过 / 待确认 / 失败 |

## 异常与结论

| 样本名 | 现象 | 初步定位 | 对应证据 | 处理动作 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
|  |  | tenant / OCR / embedding / pgvector / 宿主回填 / 结构差异 | 事件 / Prometheus / SQL / 报告路径 |  | 打开 / 已处理 / 待复验 |

## 回滚观察点

| 日期 | 信号 | 影响范围 | 证据 | 是否触发回滚 |
| --- | --- | --- | --- | --- |
|  | `services.paddleocr = false` / `parse_embedding_skipped_total` 增长 / 回填失败 / 检索退化 |  |  | 是 / 否 |

## 本轮结论

- 样本总数：
- 通过数：
- 待复验数：
- 失败数：
- 是否允许扩大样本面：是 / 否
- 下一步动作：

## 关联文档

- 操作手册见 [jobcard-dual-run-runbook.md](jobcard-dual-run-runbook.md)
- 宿主替换总清单见 [../../jobcard-host/docs/jobcard-replacement-checklist.md](../../jobcard-host/docs/jobcard-replacement-checklist.md)
- OCR 接入清单见 [ocr-integration-checklist.md](ocr-integration-checklist.md)