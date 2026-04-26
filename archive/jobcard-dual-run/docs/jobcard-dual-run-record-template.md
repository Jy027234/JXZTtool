# jobcard 双跑验收记录模板

## 使用方式

1. 每启动一轮新的 jobcard 联调，先复制本模板为一份新的记录文件。
2. 样本执行前先填写“环境快照”和“样本台账”。
3. 每跑完一个样本，立即填写“结果记录”和“异常与结论”，不要等整批结束后再回忆补录。
4. 若出现需要回滚的信号，把现象、影响范围和对应的事件或指标一起填入“回滚观察点”。

## 环境快照

| 项目 | 值 |
| --- | --- |
| 联调日期 |  |
| 宿主系统 | jobcard |
| ParseCore 运行配置 | `parsecore.pgvector.fake-embedding.toml.example` |
| ParseCore API 地址 | `http://127.0.0.1:8090` |
| 数据库模式 | Postgres + pgvector |
| OCR provider | rapidocr / remote-http / 未启用 |
| 双跑执行人 |  |
| 记录文件 |  |

## ParseCore 侧已验证基线

| 基线项 | 当前状态 |
| --- | --- |
| 健康检查 | `GET /health` 返回 `status = ok`，且 `services.pdfplumber / python_docx / paddleocr = true` |
| DOCX 基线 | 已完成 live 验证，可解析、可 `re-embed`、可进入 `hybrid` |
| PDF 基线 | `doc_id = live-pdf-001`，`parser = pdf-text`，`total_pages = 45`，`blocks = 46`，`chunks = 46`，`chunk_embeddings = 46`，`chunks_with_embedding = 46`，`retrieval_mode = hybrid` |

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