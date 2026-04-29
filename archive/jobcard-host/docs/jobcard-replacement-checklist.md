# jobcard 替换清单

## 目标

历史注记：本清单记录的是 2026-04-26 前后的宿主替换思路。2026-04-29 起，主线源码已删除当时用于宿主接线的 helper；下文“后端内嵌”方案只作为历史接线证据，不再代表当前 mainline 仍可直接照抄执行。

本清单用于把 ParseCore 按“先兼容、后切流”的方式嵌入 jobcard 或同类宿主系统。

当前默认质量门禁以 ParseCore 自检为主；历史 jobcard 双跑记录、runbook 和辅助脚本已归档到 [../../jobcard-dual-run/README.md](../../jobcard-dual-run/README.md)。

## 阶段 0：前置决定

1. 确认执行模式：小流量选 `inline`，稳定后优先切 `queue-worker`。
2. 确认 OCR 策略：本地 `rapidocr` 还是宿主统一 `remote-http` 网关。
3. 确认当前阶段是否继续使用 SQLite；若只是替换解析链路，可以先不动存储；若要一并验证持久化与检索，直接从 [../../../parsecore.pgvector.toml.example](../../../parsecore.pgvector.toml.example) 起步。

## 阶段 1：配置准备

1. 以 [../../../parsecore.remote-http.toml.example](../../../parsecore.remote-http.toml.example) 为模板生成宿主环境配置。
2. 若需要 Postgres + pgvector，本地/联调环境改用 [../../../parsecore.pgvector.toml.example](../../../parsecore.pgvector.toml.example)。
3. 若走 `remote-http`，注入 `PARSECORE_OCR_API_KEY`。
4. 将宿主租户、环境标识等放进 `providers.ocr.options.headers`。
5. 保留 `POST /parse`、`POST /parse/batch`、`GET /health` 这些兼容入口，降低宿主改造面。

## 阶段 2：部署形态

### 方案 A：后端内嵌

适用于宿主本身是 FastAPI/Starlette：

历史接线示意：把 ParseCore 子应用挂到宿主内部前缀，例如 `/internal/parsecore`，并让宿主保留上传/查询接口，只把后台解析入口切到 ParseCore。

适合先验证接口兼容和数据结构。

### 方案 B：sidecar 或独立解析服务

适用于需要把 worker、OCR、索引扩容分开的场景：

1. 复用仓库现有 `parsecore-api` 与 `parsecore-worker`。
2. 用宿主侧网关或 service discovery 暴露 ParseCore。
3. 宿主只保留任务提交、查询和结果回填逻辑。

## 阶段 3：切流前检查

1. `GET /health` 中 `pdfplumber / python_docx / paddleocr` 符合预期。
2. 用真实样本跑一次上传解析和 batch 解析。
3. `/v1/parse/events` 可以看到 `ocr_attempted / ocr_failed`。
4. `/v1/parse/prometheus` 可以看到 OCR 摘要计数。
5. 文档补丁字段能正确回填宿主记录。
6. 若要回看宿主 store 现有文档的历史兼容性验证，必须确认 `JOB_CARD_UPLOAD_DIR` 指向真实上传目录，且 live store 引用的上传文件仍实际存在；否则只能退回 direct-file 样本验证。

## 阶段 4：灰度替换

1. 先选一小批文档做灰度复验。
2. 以 `plainText`、Block 数、Chunk 数、搜索命中和宿主字段回填为主要观察项。
3. 把 OCR 失败页与 `layout_signals.ocr_failed_pages` 纳入验收。
4. 达标后再扩大租户或业务范围；只有出现兼容性问题时再回看历史双跑资料。

当时已知 jobcard 运行态边界：`doc-527d3fe173db`、`doc-1f24155682db` 与 `mdoc-cb4a937bffe8` 已在 `JOB_CARD_UPLOAD_DIR = D:\app\uploads` 下完成 store-backed 双跑，其中后两条已明确来自宿主原生上传接口；`doc-main-wheel-r16` 已进一步确认是 `jobcard/backend/store.py` 中 `size = 0` 的占位 seed，不是当时机器上的真实上传资产，而静态 store 里另一条相关样本 `doc-3db0059c4e7f` 所指向的 PDF 也已缺失，因此当时主轮 CMM 样本仍属于外部数据阻塞。

2026-04-26 的最新推进：已通过归档工具 `archive/jobcard-dual-run/tools/seed_jobcard_live_store.py` 向 live store 补入 `doc-parsecore-docx-store-seed`、`doc-flight-ops-r2-store-seed` 与 `mgmt-parsecore-docx-store-seed` 三条样本，并通过 `archive/jobcard-dual-run/tools/upload_jobcard_native_sample.py` 走宿主原生上传接口新增 `doc-1f24155682db`、`mdoc-cb4a937bffe8` 两条样本。当时运行态已变为 `documents = 5`、`mgmt_documents = 2`；因此两条宿主文库路径都已具备继续灰度验证的原生样本基础，阶段 5 条件 7 已被满足。对主轮 CMM 样本的额外排查结果也已经明确：当时机器上无可恢复的本地 PDF 资产，后续若要补这条样本，必须从老系统数据库导出 `document_source_files` 中的真实 PDF，或通过宿主原生上传重新补样。

## 阶段 5：切换完成条件

以下条件同时满足时，再认为旧解析链路可以退场：

1. 兼容接口已经稳定服务真实流量。
2. OCR 网关的失败页与失败事件在可接受范围内。
3. 宿主查询、重跑、检索链路都已切到 ParseCore 结果。
4. 观测面可以独立定位 quota、inflight、OCR、embedding 四类问题。
5. 至少有一批可持续复现的宿主 store-backed 样本，不依赖临时补路径或缺失上传文件。
6. `documents` 与 `mgmt_documents` 两条宿主文库路径都至少有 1 条 store-backed 历史兼容样本。
7. 至少各有 1 条样本来自宿主原生上传流程，而不是人工 seed。

## 建议的回滚触发条件

1. `services.paddleocr` 持续为 `false`。
2. `/v1/parse/events?event_type=ocr_failed` 出现持续增长的失败页。
3. `parse_ocr_failed_total`、`parse_embedding_skipped_total` 明显异常增长。
4. 宿主出现无法接受的结构化字段缺失或检索退化。
5. live store 引用的上传文件大面积缺失，导致历史兼容样本或宿主回填验证无法持续复现。

## 关联文档

- 当前切流状态汇总见 [jobcard-cutover-readiness.md](jobcard-cutover-readiness.md)
- 详细接线背景见 [jobcard-integration.md](jobcard-integration.md)
- 历史双跑归档见 [../../jobcard-dual-run/README.md](../../jobcard-dual-run/README.md)
- OCR 接线清单见 [../../../docs/ocr-integration-checklist.md](../../../docs/ocr-integration-checklist.md)
- OCR HTTP 契约见 [../../../docs/ocr-gateway-contract.md](../../../docs/ocr-gateway-contract.md)
