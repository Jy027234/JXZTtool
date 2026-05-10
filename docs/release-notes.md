# ParseCore 发版说明

## 版本定位

当前发版建议命名为：`0.1.0 可交付灰度版`。

发布日期口径：`2026-05-10`

交付结论：可以交付给宿主产品试运行，并可进入受控生产灰度。中小文档和 Excel/Word/PDF 常规解析可直接使用；1 万页以上大 PDF 建议使用异步 job、part 拆分、records 查询与 SQLite/JSONL 导出链路。

## 本版能力范围

本版提供以下稳定能力：

- 嵌入式 SDK、ASGI API、CLI、queue-worker 四种使用形态。
- Word、Excel、PDF、图片/OCR 场景的解析入口。
- `profile=auto` 自动解析策略，支持 `default / large-pdf / large-pdf-catalog / large-pdf-ledger / table-heavy / ocr-heavy / excel-ledger / scan-pdf`。
- `projection=compat|structured|full` 文档结果读取。
- `pages / lines / records` document views 持久化。
- records 分页查询，支持 `limit / offset / query / page_start / page_end / quality_signal / field.*`。
- 同步导出与异步导出包，支持 `jsonl / csv / tsv / sqlite / xlsx`。
- 大 PDF 页段规划、part 子 job、父文档 partial 读模型、单 part/批量复跑、尚未运行 part 取消。
- 质量信号、parse units、metrics、events、Prometheus、索引指标和灰度基线快照。
- API key 鉴权、同步上传大小保护、异步上传桥接、quota、worker 失败退避、软超时和 claim token 写回保护。

## 关键优化

本轮交付前已完成以下产品化优化：

- PDF part 文件批量生成，避免每个 part 反复打开同一大 PDF。
- `large-pdf-catalog` / `large-pdf-ledger` 默认 fast text path，适合目录、清单、台账型超大 PDF。
- 异步导出直接写文件，避免先把大结果集整体序列化为 bytes。
- records API 查询从主文档快照中拆出，默认不再全量加载 `pages / lines / records` views。
- SQLite/Postgres/InMemory store 均支持 records 分页查询和游标式过滤扫描。
- OCR adapter 诊断输出、API 可选依赖、重复 helper、ASGI monolith 等早期质量问题已完成主要收口或规划隔离。

## 验证结果

最近一次仓库级验证：

```text
pytest: 322 passed, 5 skipped
git diff --check: passed
```

真实大 PDF 样本只读抽检：

```text
pages=17,101
lines=1,249,000
records=454,985
catalog.sqlite=约 615 MB
records 关键字查询样例：PMA0013，返回 2 条，约 0.36s
```

## 交付建议

推荐交付分级：

- 中小 Word/PDF/Excel：可直接生产试运行。
- Excel 与表格类文档：可交付使用，建议保留业务抽检。
- 超大 PDF：必须优先走异步 job、part 拆分和导出包，不建议同步 HTTP 直跑。
- 扫描件和复杂跨页表格：可用，但应依赖 `quality_signals` 做异常定位和局部复跑。

## 已知限制

以下内容不阻塞本版交付，但应进入后续版本：

- records 同步 HTTP 导出仍会在响应阶段形成完整内容，后续可进一步改为真正 streaming response。
- 复杂 PDF 表格的 profile 自动切换仍需更多真实样本训练规则。
- Parquet、异常页截图包、raw cells trace 包尚未作为正式导出能力落地。
- OCR 重样本仍需要独立性能基线和专项优化，不应混入普通文档 SLA。
- 多产品大规模共用前，应由宿主侧补充权限、租户隔离、容量、备份和审计策略。

## 发版前检查

发版前建议执行：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m pytest -q
git diff --check
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe -m parsecore.cli describe --config parsecore.toml
```

灰度/生产配置还应执行：

```powershell
$env:PARSECORE_RUNTIME_CONFIG = "./parsecore.pgvector.toml.example"
docker compose --profile pgvector up -d --build
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/runtime
```

## 回滚口径

满足以下任一条件时暂停灰度或回滚：

- 默认回归或 smoke 出现硬失败。
- `/health`、`/v1/runtime` 或主解析 API 不可用。
- 新版本导致中小文档主路径稳定失败。
- 大 PDF part 队列持续 dead-letter，且无法通过单 part 复跑恢复。
- 宿主侧观测到权限、租户隔离、容量或审计风险。

保守回滚方式见 [gray-deployment.md](gray-deployment.md)。
