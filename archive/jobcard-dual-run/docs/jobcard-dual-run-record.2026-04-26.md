# jobcard 双跑验收记录 2026-04-26

## 环境快照

| 项目 | 值 |
| --- | --- |
| 联调日期 | 2026-04-26 |
| 宿主系统 | jobcard |
| ParseCore 运行配置 | `parsecore.pgvector.fake-embedding.toml.example` |
| ParseCore API 地址 | `http://127.0.0.1:8090` |
| 数据库模式 | Postgres + pgvector |
| OCR provider | rapidocr |
| 双跑执行人 | 待补 |
| 记录文件 | `archive/jobcard-dual-run/docs/jobcard-dual-run-record.2026-04-26.md` |

## 当前范围

本记录先登记 ParseCore 侧已完成的 live 验证结果，随后追加 2026-04-26 当天执行的第一轮 jobcard direct-file 双跑结果，并进一步补充多条 store-backed 结果。当前已完成 12 条样本记录，其中 ParseCore 基线 2 条、jobcard direct-file 3 条、jobcard store-backed 成功 6 条、store-backed probe 阻塞 1 条；`documents` 与 `mgmt_documents` 两条路径都已完成首批宿主原生上传闭环验证，对 `doc-main-wheel-r16` 的 probe 仍确认被宿主上传文件缺失阻塞。

## ParseCore 侧已验证基线

| 基线项 | 当前状态 |
| --- | --- |
| 健康检查 | `GET /health` 返回 `status = ok`，且 `services.pdfplumber / python_docx / paddleocr = true` |
| DOCX live 验证 | `doc_id = live-docx-001`，`blocks = 301`，`chunks = 301`，`chunk_embeddings = 301`，`retrieval_mode = hybrid` |
| PDF live 验证 | `doc_id = live-pdf-001`，`parser = pdf-text`，`total_pages = 45`，`blocks = 46`，`chunks = 46`，`chunk_embeddings = 46`，`chunks_with_embedding = 46`，`retrieval_mode = hybrid` |

## 样本台账

| 样本名 | 宿主文档 ID | doc_id | tenant_id | 文件类型 | 文件路径 | 执行方式 | 预期重点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ParseCore live DOCX 基线 | 待补 | live-docx-001 | tenant-live | DOCX | 待补 | direct-file / API | 回填、读取、re-embed、hybrid |
| ParseCore live PDF 基线 | 待补 | live-pdf-001 | tenant-live | PDF | `D:/app/uploads/25-51-06.pdf` | direct-file / API | 页数、search、pgvector、hybrid |
| jobcard 首轮 DOCX | n/a（direct-file） | file-0de3a3beccb248fbb065085e416de43f | default | DOCX | `D:/app/uploads/0de3a3beccb248fbb065085e416de43f.docx` | direct-file | legacy 对照、文本相似度、块粒度 |
| jobcard 首轮 PDF（25-51-06） | n/a（direct-file） | file-25-51-06 | default | PDF | `D:/app/uploads/25-51-06.pdf` | direct-file | legacy 对照、PDF 可读性、块数 |
| jobcard 首轮 PDF（flight-ops） | n/a（direct-file） | file-飞行运行手册第二版R2(2021-04-06） | default | PDF | `D:/app/uploads/飞行运行手册第二版R2(2021-04-06）.pdf` | direct-file | legacy 对照、页数、块数、可索引 chunk |
| jobcard store-backed PDF（线束） | doc-527d3fe173db | doc-527d3fe173db | default | PDF | `D:/app/uploads/36d65cd6b61346e28e97dbaf829646de.pdf` | store-backed | 宿主视角、live store、页数、可索引 chunk |
| jobcard store-backed PDF probe（主机轮组件） | doc-main-wheel-r16 | doc-main-wheel-r16 | default | PDF | 缺失：`sample-main-wheel-r16.pdf` | store-backed | 验证 live store 第二样本是否可执行 |
| jobcard store-backed DOCX seed | doc-parsecore-docx-store-seed | doc-parsecore-docx-store-seed | default | DOCX | `D:/app/uploads/0de3a3beccb248fbb065085e416de43f.docx` | store-backed | 宿主视角、DOCX、块粒度、probe |
| jobcard store-backed PDF seed（flight-ops） | doc-flight-ops-r2-store-seed | doc-flight-ops-r2-store-seed | default | PDF | `D:/app/uploads/飞行运行手册第二版R2(2021-04-06）.pdf` | store-backed | 宿主视角、可复现 PDF 样本池 |
| jobcard store-backed 管理文库 DOCX seed | mgmt-parsecore-docx-store-seed | mgmt-parsecore-docx-store-seed | default | DOCX | `D:/app/uploads/0de3a3beccb248fbb065085e416de43f.docx` | store-backed / mgmt_documents | 管理文库路径、DOCX、probe |
| jobcard 原生上传 PDF（flight-ops） | doc-1f24155682db | doc-1f24155682db | default | PDF | `D:/app/uploads/d91b7faf82c94a6f8dfd5b3e232e750f.pdf` | store-backed / native-upload | 宿主原生上传、documents 路径闭环 |
| jobcard 原生上传管理文库 DOCX | mdoc-cb4a937bffe8 | mdoc-cb4a937bffe8 | default | DOCX | `D:/app/uploads/7286e5b22db043e886c3f2ef608767f0.docx` | store-backed / native-upload / mgmt_documents | 宿主原生上传、管理文库闭环 |

## 结果记录

| 样本名 | legacy 已执行 | ParseCore 已执行 | parser | total_pages | blocks | chunks | retrieval_mode | 宿主字段回填 | chunk_embeddings 已落库 | 双跑报告路径 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ParseCore live DOCX 基线 | 否 | 是 | docx | n/a | 301 | 301 | hybrid | n/a | 是 | n/a | 通过 |
| ParseCore live PDF 基线 | 否 | 是 | pdf-text | 45 | 46 | 46 | hybrid | n/a | 是 | n/a | 通过 |
| jobcard 首轮 DOCX | 是 | 是 | docx | 1 | 640 | 640 | n/a（compare report 不含该字段） | n/a（direct-file） | n/a（报告显示可索引 chunk = 640） | `jobcard/backend/data/parsecore_dual_run_report_first_batch_2026-04-26.{json,md}` | 待复验 |
| jobcard 首轮 PDF（25-51-06） | 是 | 是 | pdf-text | 45 | 46 | 46 | n/a（compare report 不含该字段） | n/a（direct-file） | n/a（报告显示可索引 chunk = 46） | `jobcard/backend/data/parsecore_dual_run_report_first_batch_2026-04-26.{json,md}` | 待复验 |
| jobcard 首轮 PDF（flight-ops） | 是 | 是 | pdf-text | 573（legacy 574） | 2145 | 2145 | n/a（compare report 不含该字段） | n/a（direct-file） | n/a（报告显示可索引 chunk = 2145） | `jobcard/backend/data/parsecore_dual_run_report_flight_ops_2026-04-26.{json,md}` | 通过 |
| jobcard store-backed PDF（线束） | 是 | 是 | pdf-text | 254（legacy 总页 297） | 2099 | 2099 | n/a（compare report 不含该字段） | 是（store 文档视角） | n/a（报告显示可索引 chunk = 2099） | `jobcard/backend/data/parsecore_dual_run_report_store_doc_527d3fe173db_2026-04-26.{json,md}` | 通过 |
| jobcard store-backed PDF probe（主机轮组件） | 否 | 否（未进入对比） | n/a | n/a | n/a | n/a | n/a | 否 | 否 | `jobcard/backend/data/parsecore_dual_run_report_store_doc_main_wheel_r16_probe_2026-04-26.{json,md}` | 阻塞 |
| jobcard store-backed DOCX seed | 是 | 是 | docx | 1 | 640 | 640 | n/a（compare report 不含该字段） | 是（store 文档视角） | n/a（报告显示可索引 chunk = 640） | `jobcard/backend/data/parsecore_dual_run_report_store_docx_seed_2026-04-26.{json,md}` | 通过 |
| jobcard store-backed PDF seed（flight-ops） | 是 | 是 | pdf-text | 573（legacy 574） | 2145 | 2145 | n/a（compare report 不含该字段） | 是（store 文档视角） | n/a（报告显示可索引 chunk = 2145） | `jobcard/backend/data/parsecore_dual_run_report_store_flight_ops_seed_2026-04-26.{json,md}` | 通过 |
| jobcard store-backed 管理文库 DOCX seed | 是 | 是 | docx | 1 | 640 | 640 | n/a（compare report 不含该字段） | 是（mgmt_documents 视角） | n/a（报告显示可索引 chunk = 640） | `jobcard/backend/data/parsecore_dual_run_report_mgmt_docx_seed_2026-04-26.{json,md}` | 通过 |
| jobcard 原生上传 PDF（flight-ops） | 是 | 是 | pdf-text | 573（legacy 574） | 2145 | 2145 | n/a（compare report 不含该字段） | 是（documents 原生上传视角） | n/a（报告显示可索引 chunk = 2145） | `jobcard/backend/data/parsecore_dual_run_report_native_documents_upload_2026-04-26.{json,md}` | 通过 |
| jobcard 原生上传管理文库 DOCX | 是 | 是 | docx | 1 | 640 | 640 | n/a（compare report 不含该字段） | 是（mgmt_documents 原生上传视角） | n/a（报告显示可索引 chunk = 640） | `jobcard/backend/data/parsecore_dual_run_report_native_mgmt_upload_2026-04-26.{json,md}` | 通过 |

## 异常与结论

| 样本名 | 现象 | 初步定位 | 对应证据 | 处理动作 | 当前状态 |
| --- | --- | --- | --- | --- | --- |
| ParseCore live PDF 基线 | 无异常；检索模式进入 `hybrid`，并已确认 `chunk_embeddings = 46` | n/a | API 返回 + Postgres SQL 计数 | 无需处理 | 已验证 |
| jobcard 首轮 DOCX | `text_similarity = 0.8212`，但 legacy 仅 53 个块，ParseCore 为 640 个块 | legacy 与 ParseCore 的 DOCX 切段粒度差异大，当前 direct-file 双跑更适合先看文本一致性而非块级对齐 | `parsecore_dual_run_report_first_batch_2026-04-26.json` 中 DOCX 条目 | 保留为 DOCX 首轮样本；后续补 store 模式或宿主回填视角复验 | 待复验 |
| jobcard 首轮 PDF（25-51-06） | `text_similarity = 0.2324`，legacy 与 ParseCore 的 plain text 预览都呈 CID 编码样式，不能作为可读性验收样本 | 样本文本编码或提取质量问题，不是双跑命令失败 | `parsecore_dual_run_report_first_batch_2026-04-26.json` 中 PDF 条目 | 追加运行 flight-ops PDF 作为首轮可读 PDF 样本 | 已绕行 |
| jobcard 首轮 PDF（flight-ops） | `text_similarity = 0.8890`，legacy/ParseCore 页数为 574/573，ParseCore 可索引 chunk = 2145，probe 命中 5 比 5 | 可作为当前首轮 PDF 文本样本，后续继续扩样本面并观察页级差异 | `parsecore_dual_run_report_flight_ops_2026-04-26.json` | 保留为当前首轮代表性 PDF 双跑结果 | 已验证 |
| jobcard store-backed PDF（线束） | `text_similarity = 0.9580`，legacy/ParseCore 非空页数同为 254，ParseCore `chunk_count = 2099`，probe 命中 4 比 4 | 真正的宿主视角双跑已跑通；`legacy_total_pages = 297` 与 `parsecore_total_pages = 254` 的差值主要来自 legacy 空白页统计方式 | `parsecore_dual_run_report_store_doc_527d3fe173db_2026-04-26.json` | 保留为当前第一条 store-backed 基线；继续补更多 live store 文档 | 已验证 |
| jobcard store-backed PDF probe（主机轮组件） | compare probe 返回 `missing_files = 1`，`file_path = null`，未进入 legacy / ParseCore 对比 | 当前 live store 的 `doc-main-wheel-r16` 进一步确认来自 `jobcard/backend/store.py` 的占位 seed，`file.size = 0`；另一个静态 store 样本 `doc-3db0059c4e7f` 虽指向真实 `C20195162` / `R16 TR32-7` PDF，但其上传文件 `ea79cf24213f481080a26674690af96c.pdf` 也不在当前机器上，因此本地无法恢复主轮 PDF 资产 | `parsecore_dual_run_report_store_doc_main_wheel_r16_probe_2026-04-26.json` | 将其定义为外部数据阻塞；下一步只能从老系统导出源 PDF，或重新通过宿主原生上传补一份真实主轮 CMM PDF | 已确认阻塞 |
| jobcard store-backed DOCX seed | `text_similarity = 0.8212`，legacy/ParseCore 页数同为 1，ParseCore `chunk_count = 640`，probe 命中 4 比 4 | 通过 `../tools/seed_jobcard_live_store.py` 补出第一条 store-backed DOCX 样本，证明宿主视角 DOCX 路径可执行；但 legacy/ParseCore 块粒度差异仍与 direct-file 一致 | `parsecore_dual_run_report_store_docx_seed_2026-04-26.json` | 保留为当前第一条 store-backed DOCX 基线；后续继续观察宿主原生上传路径 | 已验证 |
| jobcard store-backed PDF seed（flight-ops） | `text_similarity = 0.8890`，legacy/ParseCore 页数为 574/573，ParseCore `chunk_count = 2145`，probe 命中 5 比 5 | 通过 seed 工具补出第二条可复现 store-backed PDF 样本，说明当前 documents 维度已不再受单样本约束 | `parsecore_dual_run_report_store_flight_ops_seed_2026-04-26.json` | 保留为当前第二条 store-backed PDF 基线 | 已验证 |
| jobcard store-backed 管理文库 DOCX seed | `text_similarity = 0.8212`，legacy/ParseCore 页数同为 1，ParseCore `chunk_count = 640`，probe 命中 5 比 5 | 通过 seed 工具补出第一条 `mgmt_documents` 样本，证明管理文库路径本身可执行；但该样本仍不代表宿主原生上传闭环已完成验证 | `parsecore_dual_run_report_mgmt_docx_seed_2026-04-26.json` | 保留为当前第一条管理文库 store-backed 基线；后续补宿主原生上传样本 | 已验证 |
| jobcard 原生上传 PDF（flight-ops） | `text_similarity = 0.8890`，legacy/ParseCore 页数为 574/573，ParseCore `chunk_count = 2145`，probe 命中 5 比 5 | 通过 `../tools/upload_jobcard_native_sample.py` 先登录宿主，再走 `/api/v1/documents/upload` 生成新的 UUID 上传文件并写入 live store，说明宿主原生上传 `documents` 闭环已被验证 | `parsecore_dual_run_report_native_documents_upload_2026-04-26.json` | 保留为当前第一条原生上传 documents 基线 | 已验证 |
| jobcard 原生上传管理文库 DOCX | `text_similarity = 0.8212`，legacy/ParseCore 页数同为 1，ParseCore `chunk_count = 640`，probe 命中 5 比 5 | 通过同一原生上传工具走 `/api/v1/mgmt-documents/upload` 生成新的管理文库 live 样本，说明 `mgmt_documents` 原生上传闭环也已被验证 | `parsecore_dual_run_report_native_mgmt_upload_2026-04-26.json` | 保留为当前第一条原生上传管理文库基线 | 已验证 |

## 回滚观察点

| 日期 | 信号 | 影响范围 | 证据 | 是否触发回滚 |
| --- | --- | --- | --- | --- |
| 2026-04-26 | 未观察到 `services.paddleocr = false`、embedding skipped 异常增长或检索退化 | 当前 ParseCore 本地联调基线 | live PDF / DOCX 验证结果 | 否 |
| 2026-04-26 | jobcard 首轮 direct-file 双跑未出现 compare 脚本执行失败或 ParseCore bridge 不可用 | 当前首轮 DOCX / PDF 样本 | `parsecore_dual_run_report_first_batch_2026-04-26.*` 与 `parsecore_dual_run_report_flight_ops_2026-04-26.*` | 否 |
| 2026-04-26 | store-backed compare 需要显式把 `JOB_CARD_UPLOAD_DIR` 指到真实上传目录；否则即使 store 有文档也会解析不到文件 | 当前 live store 文档 | `parsecore_dual_run_report_store_doc_527d3fe173db_2026-04-26.*` 的成功运行条件 | 否 |
| 2026-04-26 | live store 第二个样本 `doc-main-wheel-r16` 因缺少 `sample-main-wheel-r16.pdf` 无法进入对比 | 当前 live store 扩样本范围 | `parsecore_dual_run_report_store_doc_main_wheel_r16_probe_2026-04-26.*` | 否 |
| 2026-04-26 | `../tools/seed_jobcard_live_store.py` 已验证可把共享上传目录中的现有文件注册为 live store 样本，并通过 jobcard 自己的 Python 环境完成写入 | 当前 documents 维度扩样本 | `parsecore_dual_run_report_store_docx_seed_2026-04-26.*` 与 `parsecore_dual_run_report_store_flight_ops_seed_2026-04-26.*` | 否 |
| 2026-04-26 | `mgmt_documents` 路径已通过 `mgmt-parsecore-docx-store-seed` 完成第一条 store-backed 双跑，但该样本来自 seed 工具补样 | 当前管理文库路径 | `parsecore_dual_run_report_mgmt_docx_seed_2026-04-26.*` | 否 |
| 2026-04-26 | `../tools/upload_jobcard_native_sample.py` 已验证可经宿主登录态与真实上传路由生成新的 UUID 上传文件，并在 `documents` 与 `mgmt_documents` 两条路径完成对比 | 当前宿主原生上传闭环 | `parsecore_dual_run_report_native_documents_upload_2026-04-26.*` 与 `parsecore_dual_run_report_native_mgmt_upload_2026-04-26.*` | 否 |

## 本轮结论

- 样本总数：12（其中 ParseCore 侧基线 2，jobcard direct-file 双跑 3，jobcard store-backed 成功 6，store-backed probe 阻塞 1）
- 通过数：9
- 待复验数：2
- 外部阻塞数：1
- 失败数：0
- 是否允许扩大样本面：是（`documents` 与 `mgmt_documents` 两条路径都已可继续灰度验证）
- 下一步动作：继续扩宿主原生上传样本面；对 `doc-main-wheel-r16` 不再按“找回本地旧文件”处理，而是改走老系统 PDF 导出或宿主原生重新上传，并同步修复宿主上传资产保全问题

## 关联文档

- 操作手册见 [jobcard-dual-run-runbook.md](jobcard-dual-run-runbook.md)
- 记录模板见 [jobcard-dual-run-record-template.md](jobcard-dual-run-record-template.md)
- 宿主替换总清单见 [../../jobcard-host/docs/jobcard-replacement-checklist.md](../../jobcard-host/docs/jobcard-replacement-checklist.md)