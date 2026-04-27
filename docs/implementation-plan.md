# 实施计划

## 导航

- 第 1 节「当前状态」是 Phase 0–4 的落地清单与详细历史说明，仅作为存档与可追溯证据，不再作为日常阅读入口。
- 第 2 节「Phase 0 ~ Phase 4」是已收口的历史路线图，保留章节方便回看，新工作不再在这些章节里追加。
- 第 3 节「下一阶段规划（2026-04-27）」是当前唯一的活动路线，新任务、新决策、新验收口径都写到这里，并对照 [docs/architecture.md](architecture.md)、[docs/ocr-gateway-contract.md](ocr-gateway-contract.md)、[docs/self-check-gate.md](self-check-gate.md) 维护。

总体收口判断：

- ParseCore 的「嵌入式内核 + 异步 worker + 多租户 + OCR 兜底 + LLM 增强 + pgvector + 同步/异步 API」骨架已基本完整，Phase 0–4 不再是瓶颈。
- 当前的真实瓶颈集中在三个方向：
  1. 工程组织还偏散：format/backend/pipeline/options 没有显式注册表，enrichment 没有独立 stage 抽象，chunking 没有作为一等抽象暴露出来。
  2. 解析质量长尾仍在：表格结构、复杂版面阅读顺序、扫描件 OCR 重样本超长尾，靠规则与几何启发式继续挤已边际递减。
  3. 检索/产品化还薄：还是单层 embedding + 单索引，没有「主索引 + 高精度索引 + 结构索引」分层，也没有夜间批处理与多版本重建机制。
- 下一阶段以「先稳工程结构，再有节制地引入专用模型，最后做检索与批处理升级」为优先级，避免直接跳到全文 VLM 路线。

## 1. 当前状态

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

### 已收口但仍需跟踪

- [x] 生产级 PDF 文本解析
- 收口说明：解析栈已落齐 (1) pypdf 段落切分 + 多列页面文本重排，(2) 双通道 pdfplumber 表格识别，(3) 结构项 / 内联结构项 / TOC 条目切分，(4) 表格续行合并 / HIGHLIGHTS 变更日志合并，(5) 重复页眉页脚剥离 + 极短块合并，(6) CID 乱码 / 空白页自动转图走 RapidOCR 兜底，(7) 可选 LLM 边界精修 hook（仅对低置信段落生效），(8) 所有后处理项均可通过 `[[parsers]] options.post_process` 开关。`var/regression/suite.json` 6 个样本（含 OCR 兜底 + 多版本飞行手册 + 真实 CMM/27-81-17）作为门禁基线，`tools/regression_baseline.py check-suite` 默认 5 OK + 1 slow-tagged SKIP，`--include-tag slow` 可纳入慢样本；69 项单测覆盖含 PDF 各后处理子模块。残差结构差异（jobcard 双跑 raw/展示口径 Block 对位仍有页级块数差）已不再以“向 legacy 收敛”为目标——legacy 在 TOC/表格页常压成单块，向其收敛会降质，所以保留当前更细颗粒度的切分作为生产口径。
- [x] 图片 OCR（含 PDF 坏页 OCR 兜底）
- [x] Postgres + pgvector
- 说明：`bootstrap.py` 已显式按 `database_url` scheme 路由 JobStore（`sqlite:///` → `SQLiteJobStore`，`postgresql://` / `postgres://` → 新增的 `PostgresJobStore`，`memory://`/空 → `InMemoryJobStore`，未知 scheme 直接 raise `ValueError`，避免静默降级）；`_build_index` 在 `index_mode in {pgvector, hybrid}` 且 URL 是 Postgres 时构造 `PgVectorIndex`，否则一律 `NullIndex()`。`PostgresJobStore` 与 SQLite 行为对齐（ISO 文本时间戳 + JSON 字段，`claim_next_job` 走 `FOR UPDATE SKIP LOCKED`）；`PgVectorIndex` 用 `pgvector` 扩展、`vector(dim)` 列、按 `doc_id` 维护索引。新增 `tests/test_bootstrap_routing.py`（路由分支单测）与 `tests/test_postgres_stores.py`（受 `PARSECORE_TEST_POSTGRES_URL` 环境变量门控的真实库 smoke 测）。本轮进一步把容器运行面补齐：Docker 镜像安装 `storage` extras，`docker-compose.yml` 新增 `pgvector` profile 和 `parsecore-postgres` 服务，并允许通过 `PARSECORE_RUNTIME_CONFIG` 在 `parsecore.queue.toml`、`parsecore.pgvector.toml.example`、`parsecore.pgvector.fake-embedding.toml.example`、`parsecore.remote-http.toml.example` 之间切换；因此即使没有外部 embedding key，也能本地把 `chunk_embeddings` 与 hybrid search 路径跑通。
- [x] 队列化 worker
- [~] jobcard 宿主兼容接入（历史双跑已归档）
- 说明：解析入口已接到 ParseCore，相关历史双跑记录、runbook 与辅助脚本已统一归档到 `archive/jobcard-dual-run/`；当前默认门禁切回 ParseCore 自检，不再继续扩大双跑样本池。既有历史联调结果仍保留为兼容性证据：ParseCore 的 PDF 文本提取已提升为按段落切分，并在 ParseCore 与 jobcard 两侧统一到 `pypdf`；对真实 PDF 的 raw 相似度、展示口径相似度、字段级/页面级/Block/Chunk 级差异摘要、索引命中差异摘要和 Block 对位摘要都已完成过一轮归档验证。宿主原生上传与 store-backed 样本也已经证明 `documents` 与 `mgmt_documents` 两条路径可通，但当前继续推进时应优先解决宿主上传资产保全问题，并以 `unittest`、`tools/regression_baseline.py check-suite`、运行态健康检查和最小灰度作为默认质量门禁。

## 2. Phase 0 ~ Phase 4（历史路线，已收口）

下列章节是 ParseCore 从骨架到平台化准备阶段的实际执行轨迹，不再作为活动路线维护；任何继续推进都改写到第 3 节。

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
- 说明：新增 [tools/self_check.py](../tools/self_check.py) 作为默认自检入口，统一执行单测、runtime describe smoke 和回归基线套件，并把 JSON 汇总写入 `var/self-check/latest.json`；新增 [self-check-gate.md](self-check-gate.md) 记录退出码语义与 2026-04-26 当前结论。当前快速自检已经稳定通过：`125 passed, 5 skipped`，耗时约 6 秒；默认回归套件中 `primary-default`、`primary-strip-hf`、`sample-25-51-06` 与 `sample-flight-ops-manual-r2` 均在预算内通过，`sample-27-81-17` 依 `slow` 标签默认跳过；剩余长尾风险集中在 `sample-cmm-32-48-21-ocr`，该样本在 600 秒窗口内仍未完成，现阶段应视为 OCR 重样本性能专项而不是通用可靠性失效。本轮已把页级 `layout_elapsed_s / ocr_engine_init_elapsed_s / ocr_render_elapsed_s / ocr_input_prepare_elapsed_s / ocr_engine_exec_elapsed_s / ocr_call_elapsed_s / ocr_provider_elapsed_s / ocr_provider_det_elapsed_s / ocr_provider_cls_elapsed_s / ocr_provider_rec_elapsed_s / ocr_postprocess_elapsed_s / ocr_total_elapsed_s` 纳入 block metadata 与 `layout_signals` 聚合，并通过 `tools/regression_baseline.py check --baseline var/regression/baseline.cmm-32-48-21.json` 跑出真实长尾分布：218 页里 217 页触发 OCR 兜底，初始观测累计 `ocr_total_s=381.739`，其中 `render_s=16.313`、`call_s=364.019`、`post_s=0.038`、`max_page_ocr_s=6.101`；后续验证表明单纯把 `ocr_render_resolution` 从 110 降到 96 会退化到 `ocr_total_s=413.822` / `call_s=393.781`，不是有效方向。当前已将本地 RapidOCR 的 PDF 页输入切为灰度，在相同样本上把 `ocr_total_s` 压到 `366.242`、`call_s` 压到 `350.099`，同时结构结果仍在回归预算内；进一步把 `ocr_call` 拆成 `prep + engine` 后，两次重样本现测 `prep_s=0.645-0.762`、`engine_s=372.487-439.126`、`call_s=373.138-439.896`，说明数组准备稳定低于 OCR 调用的 `0.2%`，后续优化应继续压缩引擎执行段而不是围绕 `np.array(...)` 一类表层开销打转。本轮又修正了本地 RapidOCR provider timing 的解释方式：其返回的并非单个 elapsed，而是 det/cls/rec 三段列表；此前 `provider_s` 在 rapidocr 场景下会因类型不匹配退化为 `0.0`，现已规范化为总和并分别暴露 `det_s / cls_s / rec_s`。同一重样本现测 `provider_s=397.694`，其中 `det_s=46.526`、`cls_s=12.378`、`rec_s=338.790`，说明当前长尾的主要内部瓶颈已进一步收敛到识别阶段而非检测或分类阶段。与此同时，RapidOCR 的局部调参包装层也已补齐：现在仅覆盖 `rec_* / cls_* / det_*` 参数时，ParseCore 会自动补齐缺省 `*_model_path`，不再因为上游 `UpdateParameters` 的实现细节直接抛错；实测 `rec_batch_num=12` 已可在 live 环境直接生效，无需手工补 `rec_model_path`。代表页实验则表明目前还没有安全稳定的配置级收益：`rec_batch_num=12/16` 大多更慢，`use_angle_cls=false` 虽能省掉 `cls_s` 但会让第 20 页结果从 69 条掉到 66 条，`rec_img_shape` 缩到 `288/256` 虽保持文本一致，但 `rec_s` 仅在个别页轻微波动，未形成稳健下降。本轮继续把 RapidOCR adapter 暴露的 `crop_count / cls_rotate_positive_count / cls_rotate_high_count` 接进 parser、`layout_signals` 与回归输出；同一重样本最新现测为 `ocr_total_s=428.959`、`provider_s=384.083`、`crops=9232`、`cls_180=1013`、`cls_180_hi=262`，说明 angle classifier 的高置信旋转命中密度仅约 `2.8%`，已具备继续做页级条件跳过判定的观测基础，但仍不足以支持全局关闭 `use_angle_cls`。进一步补上页级 `ocr_hot_pages / ocr_sparse_cls_pages` 摘要后，最新重样本现测为 `ocr_total_s=442.124`、`provider_s=396.569`；其中热页前五为 `p32/p1/p51/p41/p68`，而首批低命中候选页为 `p51/p116/p157/p86/p87`，这些页均有 `0/47-63` 的高置信 `180` 命中但单页 OCR 仍在 `3.805-4.785s`，已足以支撑下一轮围绕“页级条件跳过 angle classifier”的小范围实验。本轮进一步在 RapidOCR adapter 内补了默认关闭的 `parsecore_angle_cls_probe_crops / parsecore_angle_cls_probe_min_crops` 采样探针，用于验证“先抽样少量 crop，再决定是否跑剩余 `text_cls`”是否值得继续；但在重样本上的对照结论是否定的：启用 `16/48` 探针后，虽然 `cls_s` 从 `11.203` 降到 `10.166`，但整体 `ocr_total_s` 反而从 `419.906` 升到 `431.603`，且 `cls_180_hi` 从 `262` 降到 `252`。这说明当前瓶颈仍主要在 recognition 段，且采样探针会引入额外误判/分批开销，因此该能力目前只保留为默认关闭的实验开关，不应写入默认配置。结论仍然是 600 秒风险主要由 OCR 调用阶段主导，但“输入形态”比“继续降分辨率”更值得优化，而下一轮若继续压长尾，应优先围绕 recognition 段的模型/后端级实验，同时把这个探针视为已验证但暂不采纳的分支，而不是继续挤参数表面收益。
- [x] 增加 chunk-only 重算入口，避免为 embedding/索引重建另起管线
- [x] ASGI 显式派生重算路由：`rechunk` / `re-embed`（不再依赖隐式 `options.mode`）
- [x] 混合检索入口：`search` API 默认“向量优先 + 关键词回退”，并支持 `semantic_role` 过滤与角色权重排序
- [x] embedding live smoke 工具：`tools/_embedding_smoke.py`，有 key 时执行真实 provider，无 key 时显式 skip

## 3. 下一阶段规划（2026-04-27）

本节是当前唯一活动路线。核心原则如下：

- 保持 ParseCore 的主链为 deterministic-first：常规 PDF/DOCX/图片解析继续走稳定后端，不把全文 VLM 当默认主解析器。
- 优先补工程组织，再补专用模型：先把 format/backend/pipeline/options、enrichment stage、chunking、索引层组织好，再引入局部专用模型。
- 专用模型只解决高价值长尾：优先表格结构、复杂版面阅读顺序、OCR 重样本；不做“为了先进而先进”的整链路模型化。
- 检索层从单索引升级为多索引：主索引保吞吐和成本，高精度索引保高价值文档，结构索引用于 SOP/工卡/合规比对。
- 批处理与重建必须成为内建能力：允许夜间重算 chunk、embedding、索引与质量报表，而不是把重建当临时脚本。

当前优先级排序：

1. Phase 5 已完成并收口，后续不再回到“先补工程组织”的准备态。
2. Phase 6 现在是唯一活动阶段，优先挑一个高价值长尾切片进入受控实验。
3. Phase 7 仍然排在 Phase 6 之后，避免在专用能力尚未定型前过早铺开多索引与批处理复杂度。

### 3.1 当前明确不做

- 不把 ParseCore 改造成全文多模态 VLM 优先架构。
- 不在当前阶段引入过重的全量统一 token/doctags 体系。
- 不为了追平 legacy 双跑数值继续堆 compare-only 窄规则。
- 不先做复杂规则引擎，再考虑索引与批处理；规则只服务确定性的结构修补和业务约束。

### Phase 5：解析工程重构与能力收口（已完成）

目标：把现在“能跑”的能力整理成“可扩、可组合、可缓存、可服务化”的稳定结构。这一阶段不追求大幅提高模型精度，主要解决工程组织问题。

#### 任务清单

- [x] 建立显式的 format -> backend -> pipeline -> options 注册表
- [x] 把 OCR、表格、图片说明、公式、边界精修收口为 enrichment stage
- [x] 抽象规范解析产物层，位于 Block/Chunk 之上、业务投影之下
- [x] 把 chunking 提升为独立一等抽象，直接消费规范解析产物
- [x] 增加 pipeline warmup / cache 复用机制，按 pipeline class + options hash 复用重型资源
- [x] 为 parser、pipeline、stage 增加统一 capability 声明与策略校验
- [x] 把同步 batch、异步 job、rechunk、re-embed 统一到同一组 pipeline 入口，避免分叉逻辑

#### 落地说明（2026-04-27）

- 新增 `src/parsecore/pipelines.py`，把 format/backend/pipeline/options 注册、stage 声明、规范解析产物、artifact-backed chunking、pipeline cache 收到同一处。
- `build_runtime` 现已在启动时根据 `settings.parsers` 显式构建 pipeline registry，并在 bootstrap 阶段完成 warmup；`runtime.describe()` 也开始回传 `pipelines` 与 `pipeline_cache` 视图。
- `ParseRuntime` 不再把 parser 选择、derived task、chunking 视为彼此独立的散件：常规 parse、`/parse/batch`、`rechunk`、`re-embed` 现统一经由 pipeline registry 解析和能力校验。
- 规范解析产物层当前最小落地为 `ParsedDocumentArtifact + DocumentArtifactItem`，承接 Block 之上的 typed item、metadata、provenance 和 summary，为 Phase 6/7 的结构索引与批处理打底。
- chunking 现通过 `ArtifactBackedChunker` 消费规范解析产物，而不是直接让 runtime 裸调 `ChunkBuilder`；因此后续新增 typed item 或 stage 时不必再改 runtime 主流程。
- stage 目前分 runtime stage 与 parser-backed stage 两类：runtime stage 已落 provenance / summary，parser-backed stage 已显式声明 pdf 表格、OCR fallback、boundary refinement 与 image OCR 等现有能力，先解决“能力可声明、可验证、可缓存”，再逐步把 parser 内部逻辑外移。
- 本轮回归：`tests.test_runtime`、`tests.test_asgi`、`tests.test_bootstrap_routing` 与全量 `unittest discover -s tests` 均通过；当前结果为 `141 passed, 5 skipped`。

#### 设计要求

- 新抽象必须兼容现有 ParseRequest / ParseJob / Block / Chunk / ParseOutcome，不破坏外部 API。
- 规范解析产物层只补 typed item、provenance、layout signals、enrichment result，不替代现有 Block/Chunk 存储。
- stage 必须支持显式开关、失败降级和 trace/event 观测，保持当前的安全退化语义。

#### 验收标准

- [x] 新增一种 parser backend 或 enrichment stage 时，不需要修改 runtime 主流程分支。
- [x] 同一文档的 `parse`、`batch`、`rechunk`、`re-embed` 共享统一 pipeline 定义。
- [x] worker 热启动与重复任务的初始化开销可通过缓存观测到显著下降。
- [x] 现有自检、回归基线、OCR 网关契约测试全部保持通过。

### Phase 6：局部专用模型与长尾质量治理

目标：只在规则/几何启发式边际收益开始递减的局部问题上引入专用模型，优先解决真正影响下游价值的长尾结构错误。

#### 专用模型引入门槛

以下条件至少满足 3 条，才允许进入默认路线候选：

- 某类结构错误占严重质量问题的 30% 以上。
- 规则与几何策略连续两轮优化后，关键指标提升低于 10%。
- 问题已明确影响检索、抽取、QA 或人工校验成本。
- 能作为独立 stage 或可选 pipeline 接入，而不是破坏主链契约。

#### 优先级

- [~] P1 表格结构专用能力：跨页表、无框表、复杂单元格结构
- [~] P1 复杂版面阅读顺序/布局能力：多栏、混排、图文穿插、目录/清单型页面
- [ ] P2 OCR 长尾专项：围绕 recognition 段而不是继续挤渲染分辨率参数
- [ ] P2 公式/符号能力：仅在技术手册、学术或维修文档样本占比明显上升时引入
- [ ] P3 页级/区域级 VLM fallback：仅对疑难页、高价值页、扫描件或结构失真页启用

#### 落地说明（2026-04-27，第一批）

- 已把表格结构能力从单纯的 parser-backed 声明升级成真正的 runtime enrichment stage：`table-structure` 现作为可选 stage 挂在 pipeline 上，和 `table-detection` 分离。
- `table-structure` 支持独立开关 `enrichment.table_structure.enabled`，并可调 `header_rows`、`output_format`；默认关闭，用于受控实验而不是直接进入主链。
- stage 当前直接消费规范解析产物中的 table item 与 `cells` 元数据，把表格渲染为稳定 markdown/tsv，并将结果写回 item metadata 与下游 chunk 文本，便于后续检索和结构索引复用。
- 失败语义按 degrade 处理：stage 异常不会阻断主解析链，runtime 只在 artifact metadata 中记录 `failed_runtime_stages`。
- 已补 focused 回归覆盖：pipeline 描述、stage 启停、markdown 渲染、request 级禁用覆盖注册配置，确保这是“可独立开关、可单测、可回退”的真实能力，而不是仅在文档中声明。
- 已落一组真实表格专项 baseline：`var/regression/baseline.table-structure.primary.json` 基于主样本 `36d65cd6b61346e28e97dbaf829646de.pdf`，当前记录到 `table_ready=1.0000`、`table_cells=44`，并已接入默认 `suite.json` 作为 `primary-table-structure` 门禁项。
- 已将复杂版面阅读顺序从 `dual_channel` 的隐式副作用拆成独立 parser-backed stage：`layout-reading-order` 现在有单独配置 `post_process.layout_reading_order`，也支持 request 级覆盖 `post_process.layout_reading_order` / `enrichment.layout_reading_order.enabled`。
- parser 现在会把 `layout_reading_order_applied` 与 `layout_reading_order_strategy` 写入 block metadata；`tools/regression_baseline.py` 也新增 `layout_quality` 指标与 drift 门槛，并已将 `baseline.27-81-17.json` 重存为原生携带 `layout_quality` 的 slow layout baseline，继续沿用 `suite.json` 路径而不是另起一套脚手架。
- slow layout 样本已完成真实验证：最新 `baseline.27-81-17.json` 的生成结果为 `multi_col=2 / layout_ro_pages=2`，此前单独 `check` 也已 `OK`，说明布局阅读顺序 stage 在既有多栏样本上已命中并稳定落在预算内；长耗时问题仍主要来自 OCR，而不是布局阶段本身。
- 目录/清单页细化已补：`_split_toc_entries` 现支持 `A-1/B-10`、`2-3`、`IV/VI` 等非纯数字页码终结符，降低 TOC/LEP 页在页码样式变化时的漏切分概率。
- 正文-表格混排页顺序已细化：PDF parser 现在按 `table.bbox.top` 与段落数量估算锚点，将表格块与段落块交错输出，替代此前“每页先出全部 table 再出 paragraph”的固定顺序，降低 mixed page 的阅读顺序偏差。
- 图文穿插页细化已补：新增 `merge_figure_captions`（默认开启）后处理，仅在 `Figure/Fig./Illustration` 标签独立成段时与下一段说明合并，避免图注被切碎后并入错误上下文。
- 规范解析产物已显式补齐 `item.semantic_role` 与 `structure_tags`，并把 `semantic_role` 写入 provenance；`item.kind` 继续保留为兼容字段，后续结构索引可直接消费 `semantic_role + structure_tags` 而不需回推 block metadata。
- pipeline 可观测字段已补：每次 artifact 都会记录 `pipeline_name`、`options_hash`、`cache_key`、`cache_hit/miss`、cache 计数快照与 `active/skipped/failed_runtime_stages`，并补了缓存命中行为回归测试。
- 本轮验证结果：`tests.test_pdf_parser_figure_caption`、`tests.test_pdf_parser_toc_split`、`tests.test_pdf_parser_options`、`tests.test_regression_baseline`、`tests.test_runtime` 与全量 `unittest discover -s tests` 均通过；当前结果为 `159 passed, 5 skipped`。真实 baseline `check` 已验证 `baseline.json` 与 `baseline.table-structure.primary.json` 均在预算内通过；`sample-27-81-17` 继续作为 slow layout 样本保留在同一 suite 路径中，`sample-cmm-32-48-21-ocr` 仍保留为既有 OCR 长尾样本，未在本轮观察窗口内收口。

#### 具体策略

- 表格、布局、OCR、公式都先作为可选 stage 接入，不进入默认主链。
- 默认策略继续是 backend text + deterministic parse；命中条件后再把局部页或局部区域升级到专用能力。
- OCR 性能治理优先走模型/后端实验、页级策略和批处理调度，不继续围绕 `ocr_render_resolution`、`rec_batch_num` 这类已验证低收益参数反复试错。
- 任何专用模型上线前，都要补最小样本集、质量门槛与失败降级语义。

#### 验收标准

- [x] 至少 1 个局部专用能力进入受控实验，并有独立配置开关。
- 长尾样本集有明确前后对比，不只看整体平均分。
- 新能力失败时文档仍能落回 deterministic 路径，不阻断主解析作业。
- `tools/self_check.py`、相关 regression baseline 与新增专项样本集可以稳定复现结果。

### Phase 7：多索引、批处理与产品化增强

目标：把“解析结果能看”升级成“解析结果能被持续运营、重建、检索和对比”，支撑知识库、工卡匹配、合规比对等场景。

#### 任务清单

- [x] 建立多索引分层：主索引、高精度索引、结构索引
- [x] 定义 chunk/version/index version 关系，支持多版本重建与灰度切换
- [x] 增加夜间批处理入口：重算 chunk、重做 embedding、增量刷新索引与质量报表
- [x] 把 `semantic_role`、结构标签、业务标签纳入索引 schema 与搜索排序
- [x] 为 SOP/工卡/合规比对补结构索引与任务级检索接口
- [x] 增加索引构建与索引切换观测：覆盖率、成本、耗时、回滚状态
- [x] 预留 small/large embedding 双层策略，但默认只强制 small 层上线

#### 落地说明（2026-04-27，第一批）

- Phase 7 已正式启动，当前先落“主索引 + 结构索引”的最小骨架，而不是一次性铺开完整多索引运营链路。
- runtime 在每次 parse/rechunk/re-embed 后都会产出 `index_manifest`，明确记录 `pipeline_name`、`options_hash`、`index_version` 与各层索引清单；当前默认至少包含 `primary`（chunk）与 `structure`（typed-item）两层。
- `primary` 层继续承接现有 chunk upsert；`structure` 层则开始消费规范解析产物中的 typed item，当前会把 `item_id / semantic_role / structure_tags / page_number / text` 送入 index adapter，为 Phase 7 后续结构检索打底。
- `NullIndex` 与 `PgVectorIndex` 都已兼容新的 manifest / structure 写入契约：前者用于本地开发与测试保留索引快照，后者已新增 `structure_index_entries` 与 `index_manifests` 存储骨架。
- 文档快照接口也已开始返回 `index_manifest`，因此同一条产品链路里已经可以看到“当前文档有哪些索引层、版本是什么、结构层覆盖了多少条 typed item”。
- 结构检索与任务检索入口已上线：API 现支持 `structure-search` 与 `tasks/search`，可直接基于 `semantic_role + structure_tags` 检索 typed item，而不必退回全文 chunk 检索。
- 索引构建观测已补：runtime 新增 `index_metrics`，可聚合租户维度的 layer 覆盖、item 数量、semantic role 覆盖与 index version 分布；文档接口继续返回单文档 `index_manifest`。
- 夜间批处理入口已补：CLI 新增 `parsecore batch-reindex`，支持按租户、文档和时间窗口批量重跑 chunk/index，并可选附带 embedding 重建。
- embedding 双层策略已预留：manifest 现显式记录 `embedding_tiers`，默认 `small`，同时接受 `index.embedding_tiers = ["small", "large"]` 的上层配置输入；当前仍只强制 `small` 层上线，不默认启用高精度层。
- 高精度层现已具备可执行入口：当请求 options 启用 `index.embedding_tiers = ["small", "large"]` 时，manifest 会产出 `high_precision` 层并统计候选 chunk；检索接口支持 `index_layer=high_precision` 做层级过滤，便于在不影响默认主链的前提下对关键内容做更窄范围召回。
- 高精度层已进一步从“运行时筛选”升级为“独立持久化索引路径”：index adapter 新增 layer chunk 读取能力，`high_precision` 候选 chunk id 会写入 manifest 并由索引层独立存储，检索 `index_layer=high_precision` 时优先走索引层读取，避免只依赖内存态/请求态筛选。
- 索引观测已补 high_precision 细粒度指标：`index_metrics` 除 layer count/item 之外，新增 `high_precision.documents/document_coverage/items/item_ratio_vs_primary`，可直接衡量高精度层覆盖规模与相对成本。
- 检索效果观测已接入：`index_metrics` 新增 `search_effectiveness`（按 `primary/high_precision` 统计 `queries/hit_rate/avg_hits/max_hits/zero_hit_queries`），并在 `high_precision` 汇总里补 `query_count/query_hit_rate/query_avg_hits`，形成覆盖-成本-效果闭环。
- 检索效果观测现已具备重启延续性：查询效果事件会写入 JobStore（SQLite/Postgres 均支持），`index_metrics` 优先从持久层聚合读取，不再只依赖进程内缓存，runtime 重建后指标可持续回放。
- 观测接口已补趋势桶：`index_metrics.search_effectiveness_trends` 现提供 `1h/6h/24h` 按层命中趋势快照，可直接给 dashboard 做近期走势展示而不需二次聚合。
- 趋势窗口已升级为可配置：`/v1/parse/indexes/metrics` 支持重复参数 `trend_window_hours`（如 `2,12`），runtime 会按传入窗口输出对应趋势桶并保留默认回退值，避免 dashboard 被固定窗口绑死。
- 本轮验证结果：`tests.test_runtime`、`tests.test_asgi`、`tests.test_bootstrap_routing` 与全量 `unittest discover -s tests` 均通过；当前结果为 `167 passed, 5 skipped`。

#### 索引策略

- 主索引：低成本、高吞吐，覆盖全部常规 chunk，作为默认检索入口。
- 高精度索引：当前仅保留 tier/manifest 预留位，不做全量默认；待真实业务命中集明确后，再决定 large tier 的启用策略与覆盖范围。
- 结构索引：面向步骤、表格、工卡项、合规条款等 typed item，优先服务比对与定位，而不是通用 RAG。

#### 验收标准

- 文档解析后可在同一条产品链路里区分“文本检索命中”和“结构检索命中”。
- 索引重建不要求重跑全文解析，至少可复用规范解析产物或 Block/Chunk 层结果。
- 支持按租户、索引版本、时间窗口观察覆盖率、失败率、重建时长和查询命中质量。
- 对知识库/RAG 侧，能明确区分实时路径与夜间批处理路径。

### 3.2 建议执行顺序

1. 先用 Phase 5 新增的 registry/artifact/chunking 骨架承接一个真实高价值长尾切片，避免继续只做框架整理。
2. 当前建议 Phase 6 第一优先是表格结构，其次是复杂版面阅读顺序；OCR 长尾继续按专项优化，不与表格/布局实验混在一起。
3. 只有当至少一个局部专用能力收口后，再进入 Phase 7 的多索引与批处理，把 typed item 与版本化索引真正用起来。

### 3.3 下一批落地建议

若按一到两个迭代推进，建议第一批只做以下事项：

- [x] 选 1 组表格长尾样本，建立专项回归与是否引入表格专用能力的决策门槛
- [x] 把表格能力从当前 parser-backed stage 提升为真正可独立开关、可单测、可回退的 enrichment stage
- [x] 为复杂版面阅读顺序准备独立样本集和指标，不与表格专项共用同一评测口径
- [x] 把规范解析产物里的 typed item 与 `semantic_role` 显式接进后续结构索引设计，避免 Phase 7 再回头改数据形状
- [x] 补 pipeline 级可观测字段，至少能看到每次命中的 pipeline name、options hash、cache hit/miss 与 active stages

这一批做完后，再决定是否进入表格专用模型实验，而不是现在直接跳到整页 VLM 或全文模型方案。
