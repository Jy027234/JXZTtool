# 实施计划

## 当前状态

### 已完成

- [x] 核心模型：ParseRequest、ParseJob、Block、Chunk、ParseOutcome
- [x] 状态机：pending -> parsing -> structuring -> embedding -> done / failed
- [x] SQLite 持久化 JobStore
- [x] 查询接口：job 查询、文档快照读取、重跑
- [x] ASGI API：创建任务、查询任务、获取文档、触发重解析
- [x] 可切换执行模式：inline / queue-worker
- [x] 独立 worker 入口与队列消费逻辑
- [x] jobcard 补丁适配器与挂载助手
- [x] 真实 DOCX 解析与文本解析
- [x] 本地容器运行骨架
- [x] 基础自动化测试
- [x] jobcard 主应用挂载 ParseCore 子应用
- [x] jobcard 文档库解析入口切到 ParseCore
- [x] jobcard 管理文库解析入口切到 ParseCore
- [x] jobcard 集成功能测试
- [x] 修复 jobcard JSON 模式 store.mutate/save 死锁
- [x] jobcard 双跑比较脚本与报告生成
- [x] jobcard 直接文件双跑入口
- [x] PDF 文本按段落切分与 pypdf 环境对齐
- [x] 页面级与 Block/Chunk 级差异摘要
- [x] 字段级差异摘要
- [x] 索引命中差异摘要（关键词命中探针）
- [x] Block 对位差异摘要（按页顺序对位）
- [x] 展示口径差异摘要（重复页眉页脚去重）
- [x] 展示口径 Block 对位摘要（去重后按统一切段对位）
- [x] 双方结构质量独立指标（不再假设 legacy 为 ground truth）
- [x] 块数差异页面人工判读样本（gap_page_samples）
- [x] 多引擎 A/B 评估（legacy-pdfplumber vs ParseCore-pypdf vs local-pdfplumber vs PyMuPDF 尝试）
- [x] ParseCore PDF 后处理：重复页眉页脚剥离 + 极短块合并
- [x] 后处理 min_length 灵敏度扫描
- [x] PDF 后处理通过 `[[parsers]] options.post_process` 开关化
- [x] PDF 后处理结构项切分（`(a)`/`(1)`/`NOTE:`/`WARNING:` 等标记按行切段）
- [x] PDF 后处理内联结构项切分（长段内的 `(5)`/`(a)`/`(1)` 标记二次切段）
- [x] jobcard compare 层切分逻辑对齐 ParseCore 后处理栈（`_split_legacy_page_blocks` 走 split_pdf_page_text + split_structural_items + merge_short_blocks）
- [x] PDF 后处理 TOC 条目切分（基于 dot-leader + 页号/`Not applicable` 终结符的 `_split_toc_entries`，支持多行合包与同行多条目）
- [x] PDF 表格续行合并（仅在 `Index Name P/N or Type Manufacturer` 表头页触发 `_merge_table_continuations`）
- [x] jobcard compare 层 LEP 巨块切分（`LIST OF EFFECTIVE PAGES` 页按“日期后跟 Title Case 标题”边界细分 legacy mega-block）
- [x] PDF HIGHLIGHTS 变更日志条目合并（仅在 `HIGHLIGHTS` + `CHAPTER/Section/Page Description of Change` 页触发 `_merge_highlights_entries`）
- [x] jobcard compare 层 legacy mega-block 专项切分（tools/torque table body、vendor code list、equipment table inline body）
- [x] Block metadata `semantic_role` 收口（title/paragraph/table + toc/highlights/note/warning/caution/LEP）
- [x] Chunk 透传 `semantic_role` 并持久化到 SQLite/Postgres
- [x] 可选 embedding provider（OpenAI-compatible）接入 `STRUCTURING -> EMBEDDING -> index` 状态机
- 说明：除 `openai-compatible` 外，现已补一条本地验证用 `fake` provider，直接复用现有 `FakeEmbeddingProvider` 产出确定性 1536 维向量，用于在无外部 key 的环境里跑通 `re-embed`、pgvector 落库和 hybrid search。
- [x] embedding 失败降级为 `embedding_skipped` 事件，不中断主解析作业
- [x] 结构重算模式 `options.mode = "rerun_chunks_only"`（复用已存 blocks 重算 chunk/embedding/index）
- [x] embedding 覆盖率质量指标与 regression baseline 接线（`embedded_chunk_ratio` / `mean_embedding_dim_norm`）
- [x] README 能力边界声明（明确 ParseCore 不承载 RAG/合规比对/宿主业务规则）
- [x] jobcard 双跑资料与辅助脚本归档
- [x] 默认自检门禁脚本与结论文档

### 未完成

- [x] 生产级 PDF 文本解析
- 收口说明：解析栈已落齐 (1) pypdf 段落切分 + 多列页面文本重排，(2) 双通道 pdfplumber 表格识别，(3) 结构项 / 内联结构项 / TOC 条目切分，(4) 表格续行合并 / HIGHLIGHTS 变更日志合并，(5) 重复页眉页脚剥离 + 极短块合并，(6) CID 乱码 / 空白页自动转图走 RapidOCR 兜底，(7) 可选 LLM 边界精修 hook（仅对低置信段落生效），(8) 所有后处理项均可通过 `[[parsers]] options.post_process` 开关。`var/regression/suite.json` 6 个样本（含 OCR 兜底 + 多版本飞行手册 + 真实 CMM/27-81-17）作为门禁基线，`tools/regression_baseline.py check-suite` 默认 5 OK + 1 slow-tagged SKIP，`--include-tag slow` 可纳入慢样本；69 项单测覆盖含 PDF 各后处理子模块。残差结构差异（jobcard 双跑 raw/展示口径 Block 对位仍有页级块数差）已不再以“向 legacy 收敛”为目标——legacy 在 TOC/表格页常压成单块，向其收敛会降质，所以保留当前更细颗粒度的切分作为生产口径。
- [x] 图片 OCR（含 PDF 坏页 OCR 兜底）
- [x] Postgres + pgvector
- 说明：`bootstrap.py` 已显式按 `database_url` scheme 路由 JobStore（`sqlite:///` → `SQLiteJobStore`，`postgresql://` / `postgres://` → 新增的 `PostgresJobStore`，`memory://`/空 → `InMemoryJobStore`，未知 scheme 直接 raise `ValueError`，避免静默降级）；`_build_index` 在 `index_mode in {pgvector, hybrid}` 且 URL 是 Postgres 时构造 `PgVectorIndex`，否则一律 `NullIndex()`。`PostgresJobStore` 与 SQLite 行为对齐（ISO 文本时间戳 + JSON 字段，`claim_next_job` 走 `FOR UPDATE SKIP LOCKED`）；`PgVectorIndex` 用 `pgvector` 扩展、`vector(dim)` 列、按 `doc_id` 维护索引。新增 `tests/test_bootstrap_routing.py`（路由分支单测）与 `tests/test_postgres_stores.py`（受 `PARSECORE_TEST_POSTGRES_URL` 环境变量门控的真实库 smoke 测）。本轮进一步把容器运行面补齐：Docker 镜像安装 `storage` extras，`docker-compose.yml` 新增 `pgvector` profile 和 `parsecore-postgres` 服务，并允许通过 `PARSECORE_RUNTIME_CONFIG` 在 `parsecore.queue.toml`、`parsecore.pgvector.toml.example`、`parsecore.pgvector.fake-embedding.toml.example`、`parsecore.remote-http.toml.example` 之间切换；因此即使没有外部 embedding key，也能本地把 `chunk_embeddings` 与 hybrid search 路径跑通。
- [x] 队列化 worker
- [~] jobcard 宿主兼容接入（历史双跑已归档）
- 说明：解析入口已接到 ParseCore，相关历史双跑记录、runbook 与辅助脚本已统一归档到 `archive/jobcard-dual-run/`；当前默认门禁切回 ParseCore 自检，不再继续扩大双跑样本池。既有历史联调结果仍保留为兼容性证据：ParseCore 的 PDF 文本提取已提升为按段落切分，并在 ParseCore 与 jobcard 两侧统一到 `pypdf`；对真实 PDF 的 raw 相似度、展示口径相似度、字段级/页面级/Block/Chunk 级差异摘要、索引命中差异摘要和 Block 对位摘要都已完成过一轮归档验证。宿主原生上传与 store-backed 样本也已经证明 `documents` 与 `mgmt_documents` 两条路径可通，但当前继续推进时应优先解决宿主上传资产保全问题，并以 `unittest`、`tools/regression_baseline.py check-suite`、运行态健康检查和最小灰度作为默认质量门禁。

## Phase 0: 定稿骨架

目标：把可复用边界先定下来。

- [x] 确认核心模型：ParseRequest、ParseJob、Block、Chunk、ParseOutcome
- [x] 确认状态机：pending -> parsing -> structuring -> embedding -> done / failed
- [x] 确认扩展点：Parser、Index、Translation、ProductAdapter、JobStore
- [x] 确认 jobcard 的首个接入点

## Phase 1: 跑通单机闭环

目标：在单进程内跑通最小解析流程。

- [x] 接入 DOCX / PDF / 图片三类解析器
- 说明：DOCX 已完成；PDF 文本解析已收口（详见“生产级 PDF 文本解析”小节）；图片 OCR 已接入生产实现
- [x] 生成 Block / Chunk
- [x] 将结果落到本地 SQLite 基线存储
- [x] 提供查询文档、查询 job、读取 Block / Chunk 的 API

验收标准：

- [x] 能提交解析任务
- [x] 能看到状态流转
- [x] 能读取解析结果
- [x] 能对指定文档做增量重跑

## Phase 2: 接入 jobcard

目标：替换或并行现有技术文档解析能力。

- [x] 为 jobcard 增加 ProductAdapter
- [x] 对接现有文档上传与任务入口
- [x] 保留历史兼容性证据并归档双跑资料
- 说明：jobcard 的 `/documents/{id}/parse` 与 `/mgmt-documents/{id}/parse` 已切到 ParseCore，旧逻辑仍作为 bridge 不可用时的回退路径；既有 `parsecore_compare.py` 与历史双跑报告继续保留，但已从主线资料中移出，统一归档到 `archive/jobcard-dual-run/`，仅在需要复现旧宿主兼容性问题时使用。
- [~] 记录字段差异、Block 差异和索引命中差异
- 说明：字段差异摘要、索引命中差异摘要、第一版 Block 对位差异摘要、展示口径差异摘要和展示口径 Block 对位摘要已落地。展示口径部分当前通过重复页眉页脚去重，补充一条更接近 jobcard 入库/前端展示的对比视角；展示口径 Block 对位则在去重后按统一切段对位，用于识别“展示文本更近，但结构切段仍不一致”的情况；索引部分当前包含 legacy embedding 状态、chunk/resource 计数、ParseCore 可索引 chunk 覆盖率，以及沿用现有关键词检索口径的命中探针；Block 对位部分现已从顺序对位升级为页内动态规划匹配，优先吸收插入/缺失块导致的连锁错配，后续再视需要增强为更丰富的块匹配策略

验收标准：

- [x] 业务接口不破坏现有前端
- [x] 新解析流程可独立重跑
- [~] 差异可追踪
- 说明：当前已把 ParseCore 原始 job/blocks/chunks 回填到 jobcard 文档记录的 `parsecore` 字段，并可通过 jobcard `parsecore_compare.py` 生成系统化差异报告；当前真实 PDF 基线已更新为 1401 blocks / 1401 chunks、raw 相似度 0.9626、展示口径相似度 0.9641，报告已包含字段级、页面级差异摘要、Block/Chunk 统计、索引命中差异摘要、第一版 Block 对位差异摘要、展示口径差异摘要和展示口径 Block 对位摘要。其中 raw Block 对位在升级为智能匹配后平均相似度已提升到 0.5419，但仍有 215 页存在块数差异；展示口径 Block 对位也从 0.1579 提升到 0.2927，但仍有 221 页存在块数差异。这说明顺序错配已明显缓解，但真正的结构切段差异依旧存在；下一步重点转为扩大样本面并视需要增强块匹配策略

## Phase 3: 拆 Worker 与内部 API

目标：从嵌入式骨架走向可外拔服务。

- [x] 将解析执行迁移到队列 worker
- [x] 产品后端仅保留任务提交和查询
- 说明：当前已具备 queue-worker 模式与独立 worker；默认开发配置仍保留 inline 以便单机调试
- [x] 加入失败重试与死信策略
- [x] 加入基础观测与审计日志

验收标准：

- 大文件解析不阻塞主业务请求
- 任务失败可重试
- 可以独立扩容 worker

## Phase 4: 平台化准备

目标：为多产品复用做准备，但不提前过度建设。

- [x] 抽象多产品配置中的 provider 层（LLM / embedding 分离配置）
- [x] 加入租户与配额字段
- 说明：`ParseRequest`/`ParseJob` 已新增 `tenant_id`、`quota_key`、`quota_units` 字段（默认 `default/default/1`）；ASGI `POST /v1/parse/jobs` 支持透传并做 `quota_units >= 1` 校验；SQLite/Postgres `parse_jobs` 新增三列及迁移逻辑，重跑与派生任务（retry/reparse/rechunk/re-embed）均保留租户与配额上下文。`GET /v1/parse/jobs` 已支持 `tenant_id`/`quota_key` 过滤，`GET /v1/parse/quotas/usage` 已提供租户与配额维度聚合视图；文档读取/搜索/重跑接口已按 `tenant_id` 做硬隔离（默认 `default`），跨租户访问返回 `document_not_found`。本轮进一步完成 `blocks/chunks` 与 pgvector upsert 的 `tenant_id + doc_id` 物理分区，避免同 `doc_id` 跨租户覆盖。
- 说明：在以上多租户隔离能力基础上，`parse_jobs` 已补齐复合索引（`tenant_id + doc_id + created_at`、`state + created_at`），用于优化多租户最新任务查询与 worker claim 队列扫描。
- 说明：新增 `GET /v1/parse/metrics` 轻量观测接口，支持按租户查看失败率、活跃任务数与耗时分位（p50/p90/p99），用于运行态快速健康检查。
- 说明：新增 `GET /v1/parse/dashboard` 聚合接口，单请求返回租户级 usage、metrics 与 recent jobs，减少前端并发请求。
- 说明：`metrics/dashboard` 新增 `since_hours` 时间窗口参数，可按最近 N 小时任务计算失败率与耗时分位，便于实时运维观测。
- 说明：`PostgresJobStore` 与 `PgVectorIndex` 均已接入 `psycopg_pool` 连接池优先策略（无池依赖时自动回退单连接模式），降低高并发下建连开销；`GET /v1/parse/quotas/usage` 同步支持 `since_hours` 时间窗口，补齐观测接口口径一致性。
- 说明：inline 执行模式已增加 inflight 背压阈值（`runtime.max_inflight_jobs`，超限返回 429），并将 embedding 阶段改为分批重试 + 局部降级，避免单批失败导致整文 embedding 全量失效。
- 说明：新增配额硬限能力（`runtime.quota_enforce` / `quota_window_hours` / `quota_default_limit_units` / `quota_limits`），提交与重跑在超限时返回 `429 quota_exceeded`，用于平台级资源保护。
- [x] 提供企业产品兼容的同步 batch 解析入口
- 说明：新增 `POST /parse/batch`（parser-service 兼容根路径）与 `POST /v1/parse/batch`（版本化入口），统一接收 `file_base64 + file_name` 并同步返回 `success / total_pages / pages[] / parser_used / error`，用于对接现有 Node 产品的 parser-service 客户端；页级投影会复用 ParseCore block 元数据输出 `page_number / page_type / text / tables_markdown / confidence`，其中 `toc_entry/lep_entry` 自动归类为 `toc`，便于以最小改动嵌入现有产品。
- [x] 提供 parser-service 兼容的 multipart 上传解析入口
- 说明：新增 `POST /parse` 与 `POST /v1/parse`，支持 multipart `file` 上传并返回 `file_name / mime_type / total_pages / pages / metadata`；其中 `metadata.parser` 与 PDF 场景下的 `metadata.ocr_enabled` 与企业产品现有 `ParseResult` 契约保持对齐。为支撑该入口，`api` 依赖已显式补入 `python-multipart`。本轮进一步把 `enable_ocr` 接成 request 级开关：显式 `true` 可为该请求打开 PDF OCR 回退，显式 `false` 可覆盖配置默认值关闭 OCR 回退。
- [x] 提供 parser-service 兼容的健康检查入口
- 说明：`GET /health` 已升级为 `status / version / services` 结构，其中 `services` 会结合当前注册 parser 与实际 OCR provider 可用性返回 `pdfplumber / python_docx / paddleocr` 能力矩阵；兼容字段名保留 `paddleocr`，在 ParseCore 内部实际映射到 OCR provider 能力探测，便于宿主产品在切换 ParseCore 前复用既有健康检查与能力探测逻辑。本轮同时补齐 `providers.ocr` 抽象，并内置 `rapidocr` 与 `remote-http` 两种 provider，可在保留 `image-ocr` parser 注册的前提下按环境显式启停或切换 OCR provider。另已把 PDF OCR 失败路径显式化：坏页触发 OCR 但 provider 失败时，会在 block metadata 与 `layout_signals` 中保留 `ocr_attempt_reason / ocr_error_reason / ocr_failed_pages` 等信号，便于双跑和回归时直接诊断远程 OCR 问题。
- [x] 固化 `remote-http` OCR 网关契约并补可执行校验
- 说明：新增 [docs/ocr-gateway-contract.md](ocr-gateway-contract.md) 固定请求/响应、鉴权、失败语义与验收清单，并补 `tests.test_ocr.OcrProviderTests.test_remote_http_provider_matches_gateway_contract_over_real_http` 作为真实 HTTP contract test；同时补 [docs/ocr-integration-checklist.md](ocr-integration-checklist.md)，把宿主接线前的配置、探活、事件与 Prometheus 验收步骤收口为可执行清单。
- [x] 提供 jobcard 宿主替换与部署清单
- 说明：新增 [../archive/jobcard-host/docs/jobcard-replacement-checklist.md](../archive/jobcard-host/docs/jobcard-replacement-checklist.md) 与 [parsecore.remote-http.toml.example](../parsecore.remote-http.toml.example) 作为宿主部署模板，收口配置准备、切流检查、灰度替换与回滚条件；同时把历史联调操作手册归档到 [../archive/jobcard-dual-run/docs/jobcard-dual-run-runbook.md](../archive/jobcard-dual-run/docs/jobcard-dual-run-runbook.md)，避免主线文档继续围绕双跑展开。
- [x] 补齐宿主系统对接所需的 trace 与统一错误契约
- 说明：ASGI 关键接口已统一回传 `x-trace-id`，错误体收口为 `error / code / message / trace_id / detail?`；同时 `quota_exceeded` 与 `too_many_inflight_jobs` 等提交期观测事件会记录 `trace_id`，便于把宿主请求、ParseCore API 错误和 observability 事件串成一条排障链路。当前 observability 还额外纳入了 OCR 摘要事件：PDF 坏页触发 OCR 后，会按文档汇总 `ocr_attempted / ocr_fallback / ocr_failed` 进入 `/v1/parse/events`，并把对应页数暴露到 `/v1/parse/prometheus`。
- [x] 补评测集、基准集和质量报表
- 说明：新增 [tools/self_check.py](../tools/self_check.py) 作为默认自检入口，统一执行单测、runtime describe smoke 和回归基线套件，并把 JSON 汇总写入 `var/self-check/latest.json`；新增 [self-check-gate.md](self-check-gate.md) 记录退出码语义与 2026-04-26 当前结论。当前快速自检已经稳定通过：`125 passed, 5 skipped`，耗时约 6 秒；默认回归套件中 `primary-default`、`primary-strip-hf`、`sample-25-51-06` 与 `sample-flight-ops-manual-r2` 均在预算内通过，`sample-27-81-17` 依 `slow` 标签默认跳过；剩余长尾风险集中在 `sample-cmm-32-48-21-ocr`，该样本在 600 秒窗口内仍未完成，现阶段应视为 OCR 重样本性能专项而不是通用可靠性失效。本轮已把页级 `layout_elapsed_s / ocr_engine_init_elapsed_s / ocr_render_elapsed_s / ocr_call_elapsed_s / ocr_provider_elapsed_s / ocr_postprocess_elapsed_s / ocr_total_elapsed_s` 纳入 block metadata 与 `layout_signals` 聚合，并通过 `tools/regression_baseline.py check --baseline var/regression/baseline.cmm-32-48-21.json` 跑出真实长尾分布：218 页里 217 页触发 OCR 兜底，累计 `ocr_total_s=381.739`，其中 `render_s=16.313`、`call_s=364.019`、`post_s=0.038`、`max_page_ocr_s=6.101`；结论是 600 秒风险主要由 OCR 调用阶段主导，而不是版面提取或后处理。
- [x] 增加 chunk-only 重算入口，避免为 embedding/索引重建另起管线
- [x] ASGI 显式派生重算路由：`rechunk` / `re-embed`（不再依赖隐式 `options.mode`）
- [x] 混合检索入口：`search` API 默认“向量优先 + 关键词回退”，并支持 `semantic_role` 过滤与角色权重排序
- [x] embedding live smoke 工具：`tools/_embedding_smoke.py`，有 key 时执行真实 provider，无 key 时显式 skip
