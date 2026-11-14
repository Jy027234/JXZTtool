# ParseCore 发版说明

## 版本定位

当前发版建议命名为：`0.1.0 可交付灰度版`。

发布日期口径：`2026-05-10`

交付口径：本版可用于宿主产品的本地或集成试运行；这不表示已完成受控生产灰度验收。SQLite 本地正式发布已有实现和验证，Postgres/pgvector 示例限存储与索引联调，正式发布及生产部署仍未验证。中小文档可按接入方流程试运行；1 万页以上大 PDF 建议使用异步 job、part 拆分、records 查询与导出包链路。

## 本版能力范围

本版提供以下稳定能力：

- 嵌入式 SDK、ASGI API、CLI、queue-worker 四种使用形态。
- Word、Excel、PDF、图片/OCR 场景的解析入口。
- `profile=auto` 自动解析策略，支持 `default / large-pdf / large-pdf-catalog / large-pdf-ledger / table-heavy / ocr-heavy / excel-ledger / scan-pdf`。
- `projection=compat|structured|full` 文档结果读取。
- `pages / lines / records` document views 持久化。
- records 分页查询，支持 `limit / offset / query / page_start / page_end / quality_signal / field.*`。
- 同步数据集导出与导出包文件，支持 `jsonl / csv / tsv / sqlite / xlsx`。
- 大 PDF 页段规划、part 子 job、父文档 partial 读模型、单 part/批量复跑、尚未运行 part 取消。
- 质量信号、parse units、metrics、events、Prometheus、索引指标和灰度基线快照。
- API key 鉴权、同步上传大小保护、异步上传桥接、quota、worker 失败退避、软超时和 claim token 写回保护。

## 关键优化

本轮交付前已完成以下产品化优化：

- PDF part 文件批量生成，避免每个 part 反复打开同一大 PDF。
- `large-pdf-catalog` / `large-pdf-ledger` 默认 fast text path，适合目录、清单、台账型超大 PDF。
- 导出包文件写入磁盘；`POST /export-jobs` 当前仍在请求内生成并落盘，不能视作后台任务已经异步执行。
- records API 查询从主文档快照中拆出，默认不再全量加载 `pages / lines / records` views。
- SQLite/Postgres/InMemory store 均支持 records 分页查询和游标式过滤扫描。
- OCR adapter 诊断输出、API 可选依赖、重复 helper、ASGI monolith 等早期质量问题已完成主要收口或规划隔离。

## 最近记录的本地验证结果

2026-11-14 最终本地 fast 发版门禁为 `ok / verified`。pytest 8.4.2 下有 1,214 项通过、6 项跳过。8 个 schema、8 个 payload、真实样本的 3 种配置回归与 6 次局部 Provider 执行通过。[验收范围与报告](parsecore-reliability-mineru-todo-20260920.md#12-p1-至-p3-总验收)

2026-05-10 历史大 PDF 样本只读抽检：

```text
pages=17,101
lines=1,249,000
records=454,985
catalog.sqlite=约 615 MB
records 关键字查询样例：PMA0013，返回 2 条，约 0.36s
```

CI 的 Postgres/pgvector 集成 smoke 仅在定时或手动触发的 workflow 中运行。生产与 Postgres/pgvector 正式发布验收待开展。

## 交付建议

推荐交付分级：

- 中小 Word/PDF/Excel：可用于本地或宿主集成试运行；生产试运行须另行完成目标环境验证。
- Excel 与表格类文档：可用于本地或宿主集成试运行，并保留业务抽检。
- 超大 PDF：必须优先走异步 job、part 拆分和导出包，不建议同步 HTTP 直跑。
- 扫描件和复杂跨页表格：可在本地/集成试运行中使用，但应依赖 `quality_signals` 做异常定位和局部复跑。

## 已知限制

以下内容不阻塞本版交付，但应进入后续版本：

- `POST /v1/parse/documents/{doc_id}/export-jobs` 在请求内生成并写入导出文件后返回 `202`，records 导出可能全量读取。生成完成后，`/download` 分块传输文件。传输验证范围见[当前开发与证据入口](parsecore-reliability-mineru-todo-20260920.md)。
- 复杂 PDF 表格的 profile 自动切换仍需更多真实样本训练规则。
- Parquet、异常页截图包、raw cells trace 包尚未作为正式导出能力落地。
- OCR 重样本仍需要独立性能基线和专项优化，不应混入普通文档 SLA。
- 多产品大规模共用前，应由宿主侧补充权限、租户隔离、容量、备份和审计策略。

## 发版前检查

发版前建议执行：

```powershell
python -m pytest -q
git diff --check
python -m parsecore.cli describe --config parsecore.toml
```

仓库根目录的 fast release acceptance 沿用 [自检门禁](self-check-gate.md) 中的默认 fast suite 与 `--require-verified-corpus` 标志，可运行 `python -m parsecore.cli self-check --require-verified-corpus`。Postgres/pgvector 示例启动命令只用于存储与索引联调，不作为生产验收。

Postgres/pgvector 存储与索引联调可执行以下命令。

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
```

上面是 Postgres/pgvector 本地联调示例。该后端缺少正式版本原子发布和 run 隔离，尚未验证生产部署；启用发布时，内置索引也不支持 `rag` 产物 profile。

## 回滚口径

满足以下任一条件时暂停灰度或回滚：

- 默认回归或 smoke 出现硬失败。
- `/health`、`/v1/runtime` 或主解析 API 不可用。
- 新版本导致中小文档主路径稳定失败。
- 大 PDF part 队列持续 dead-letter，且无法通过单 part 复跑恢复。
- 宿主侧观测到权限、租户隔离、容量或审计风险。

保守回滚方式见 [gray-deployment.md](gray-deployment.md)。

## 质量指标变化记录

每次发版须记录以下质量指标的变化，以建立跨版本可追溯的质量基线。

### 记录格式

每个版本在发版说明中追加如下表格：

```markdown
### 质量指标快照（vX.Y.Z）

| 指标 | 上版值 | 本版值 | 变化 | 备注 |
| --- | ---: | ---: | --- | --- |
| text_page_coverage_ratio | 0.98 | 0.99 | +0.01 | 修复了表格页文本遗漏 |
| table_unit_coverage_ratio | 0.92 | 0.95 | +0.03 | 新增 table merge 逻辑 |
| unit_chunk_coverage_ratio | 0.97 | 0.98 | +0.01 | chunk 策略微调 |
| reading_order_confidence_avg | 0.82 | 0.85 | +0.03 | 引入 layout provider |
| visible_block_count (avg) | 42 | 44 | +2 | 新增图示 block 识别 |
| hidden_block_count (avg) | 3 | 2 | -1 | header/footer 过滤优化 |
| quality_gate accept_rate | 85% | 90% | +5% | — |
| quality_gate manual_review_rate | 10% | 6% | -4% | — |
```

### 指标采集方法

- **coverage 指标**：从 `projection=coverage` 的 `coverage.summary` 字段提取。
- **reader 指标**：从 `projection=reader` 的 `reader_summary` 字段提取。
- **quality_gate 指标**：从 `quality_gate.gate` 字段统计 accept / accept_with_warning / manual_review 分布。
- **provider comparison 指标**：从 `tools/provider_comparison_report.py` 输出的 `gate_summary` 提取。

### 变化判定规则

- coverage 类指标下降 > 0.02 须标注原因并评估是否阻塞发版。
- reading_order_confidence_avg 下降 > 0.05 须排查 layout provider 变更。
- manual_review_rate 上升 > 5% 须排查新增 quality_signal 是否引入误报。
- 所有变化均须关联到具体的 PR 或 commit。
