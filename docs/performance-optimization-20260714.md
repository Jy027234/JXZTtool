# 解析性能优化对比（2026-07-14）

## 对照样本

- 文件：`D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf`
- 规模：297 个 PDF 页面，其中 254 页产生解析内容
- 对比方式：同一文件、同一 Python 环境、同一解析配置；仅切换 `adaptive_dual_channel`
- 质量校验：页级统计、块类型/来源统计、质量门禁，以及块正文 SHA-256 和逐页内容指纹

## 性能结果

| 指标 | 现状（自适应关闭） | 优化后（自适应开启） | 变化 |
| --- | ---: | ---: | ---: |
| 端到端耗时 | 156.077 s | 147.237 s | **-5.66%** |
| 峰值内存 | 775,482.921 KB | 762,128.074 KB | **-1.72%** |
| 解析块 | 2,035 | 2,035 | 0 |
| Chunk | 2,035 | 2,035 | 0 |
| 表格 | 44 | 44 | 0 |
| 图示 | 411 | 411 | 0 |
| 版面候选页 | 全量 297 页 | 239 / 297 页 | 跳过 58 页重型扫描 |

优化后标题块会记录 `pdf_layout_strategy=adaptive-selective`、候选页数和总页数，便于线上追踪；显式请求 `dual_channel`、`layout_reading_order`，或 `table-heavy`/`scan-pdf`/`large-pdf-ledger` 配置仍回退到全量高保真路径。

## 质量结果

- 页级 `block_count`、`table_count`、`figure_count`、质量信号逐页差异：0。
- 块类型统计：`image=411`、`paragraph=1579`、`table=44`、`title=1`，前后完全一致。
- 来源统计：`native_text=1165`、`ocr_text=450`、`pdf-image=411`、`structured_table=9`，前后完全一致。
- 版面阅读顺序置信度：0.98 → 0.98。
- 质量门禁：两次均为 `accept_with_warning`，唯一告警均为测试工具未执行嵌入（`rag_chunks_not_embedded`），不是解析缺页或表格/图示丢失。
- 直接解析正文指纹：
  - 基线：`fbd4d15afcfd2241546ab7087345761c1efa5f27dd7025afdec720b0e1b7abfe`
  - 优化后：`fbd4d15afcfd2241546ab7087345761c1efa5f27dd7025afdec720b0e1b7abfe`
- 逐页内容指纹差异：0。

## 第二轮：重复解析缓存对比

第一轮的自适应版面选择仍是冷解析优化。针对重试、人工重新解析和同一 worker 内的重复请求，第二轮增加了受限的进程内 LRU 解析块缓存：产品配置开启 `parse_cache=true`、`parse_cache_max_entries=2`；库默认仍关闭。缓存键包含内容 SHA-256、文件大小、租户和请求选项，按路径/大小/`mtime_ns` 缓存指纹计算结果，避免源文件或解析参数变化时复用旧结果；第五轮移除 `doc_id`，允许同源文件在不同任务 ID 下安全复用。

本轮用同一进程、同一 PDF、同一 `doc_id` 连续提交两次完整产品链路，关闭/开启缓存各测一组；不启用 `tracemalloc`，以便观察实际运行耗时：

| 指标 | 缓存关闭 | 缓存开启 | 变化 |
| --- | ---: | ---: | ---: |
| 第一次提交 | 25.668 s | 27.319 s | 冷解析未宣称收益 |
| 第二次提交 | 28.368 s | 2.807 s | **-90.10%** |
| 解析块 / Chunk | 2,035 / 2,035 | 2,035 / 2,035 | 一致 |

两组结果的块正文指纹均为 `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06`。因此本轮收益明确限定为同一 worker 的重复解析/重试场景；首次冷解析仍以第一轮自适应版面优化结果为准。此前对 OCR 缓存快路径的交替直测中位数没有改善（基线 25.976 s、优化 27.083 s），故不把它作为冷解析性能收益，而保留其质量安全路径。

## 第三轮：表格候选页收敛

剖析显示 `pdfplumber.find_tables()` 是冷解析的主要热点。原规则把正文中普通的 `Table 602` 等词也视为表格页，导致本样本在 169 页触发表格检测；最终只有 44 页产生有效表格块。现改为仅对强表格标记（`Removal/Installation Table`、程序表行、`FIG-ITEM`、`Units per Assy`）、独立的 `PART NUMBER` 表头加列证据，或更严格的多列线索（至少 4 行三段式列分隔）触发表格检测，布局候选页和图示路径不变。

同一 PDF、同一进程、同一 Python 环境下交替重复直测（关闭/开启缓存均为冷解析，未启用 `tracemalloc`）：

| 指标 | 收敛前 | 收敛后 | 变化 |
| --- | ---: | ---: | ---: |
| 表格检测候选页 | 169 | 50 | **-70.41%** |
| 两次直测耗时 | 24.242 s / 24.181 s | 23.394 s / 24.500 s | 中位数约 **-1.09%** |
| 解析块 / 表格 / 图示 | 2,035 / 44 / 411 | 2,035 / 44 / 411 | 一致 |

收敛前后块正文指纹均为 `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06`。本轮不把约 1% 的冷解析下降夸大为稳定收益；确定性收益是减少 119 页无效表格扫描，同时保留全部 44 个有效表格页。后续应在更多表格/扫描样本上继续验证候选规则。

## 第四轮：图示/列检测复用与轻量图片计数

函数级剖析发现同一页的 `extract_words()` 会分别被列检测和图示描述调用；同时，自适应候选选择仅为判断图片数量就物化 `pypdf.page.images`。本轮改为每页复用一次 words，并优先读取 `/Resources` 下的图片 XObject 数量；遇到 Form XObject、内联图片或无法读取资源时仍回退旧路径。

在代表 PDF 上，轻量图片计数与旧属性计数 297 页完全一致，候选集合仍为布局 239 页、表格 50 页、图示 180 页；图片计数微基准耗时 `1.652s → 0.114s`（约 **-93.1%**）。与第四轮前的两次冷解析对照相比：

| 指标 | 第四轮前 | 第四轮后 | 变化 |
| --- | ---: | ---: | ---: |
| 解析器直测 | 24.242 s / 24.181 s | 22.251 s / 20.966 s | 中位数约 **-10.75%** |
| 解析块 / 表格 / 图示 | 2,035 / 44 / 411 | 2,035 / 44 / 411 | 一致 |

第四轮两次正文指纹均为 `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06`。完整 `runtime.submit` 冷启动实测为 `24.987s`，状态为 `done`，2035 个块和 2035 个 Chunk；该单次链路测量用于产品冒烟，不单独作为稳定 p95 结论。

## 本次改动

1. 在 `PdfTextParser` 增加自适应版面页选择：依据表格/图示标记、多列线索、图片数量和 OCR 异常信号筛选重型页面。
2. `pdfplumber` 对非候选页仅保留原生文本抽取，跳过表格检测、图示区域和多列重排；保留文本结果以避免分段质量变化。
3. 保留显式高保真配置和重表格/扫描文档的全量回退，并增加策略与候选页可观测字段。
4. 性能基准工具从“重复构建两次 Provider 投影”改为单次投影，避免测量报告本身放大端到端耗时。
5. 增加同源重试/重解析的有界 LRU 缓存，并用文件指纹、租户和选项组成缓存键；缓存命中只绕过解析阶段，不改变后续结构化、分块、持久化和索引流程；并发 miss 通过 single-flight 合并。
6. 收敛自适应路径的表格候选页，仅对强表格信号或明确多列页调用 `find_tables()`，避免普通叙述中的表格编号触发重型扫描。
7. 复用页级 words，并在自适应路径对“仅表格、无图示”页跳过 words 物化；同时用 PDF 资源字典做图片数量快照，减少重复对象解析和图片对象物化。
8. 将运行时 document views 的持久化改为轻量 pages/lines/records 路径；对无 chunks 的持久化快照只计算必要的 RAG 质量信号，避免重复构建完整 IR/coverage 投影。

## 第五轮：同源跨 `doc_id` 解析缓存

上一轮缓存只在同一 `doc_id` 重试时命中，重复上传、任务重建或人工复制任务仍会重复执行 PDF 解析。本轮将键调整为“租户 + 内容源文件指纹（SHA-256、大小）+ 请求选项”，不再把 `doc_id` 作为缓存维度；命中后复制块元数据，并将块的 `doc_id` 与确定性块 ID 前缀重绑定到当前请求。不同租户、文件内容变化或解析选项变化仍不会共享结果，缓存容量保持 2 条 LRU。

代表 PDF 在同一进程、同一租户下使用两个不同 `doc_id` 连续解析：

| 指标 | 第一次（`fifth-source-a`） | 第二次（`fifth-source-b`） | 变化 |
| --- | ---: | ---: | ---: |
| 解析器直测 | 23.568 s | 0.009 s | **-99.96%** |
| 解析块 | 2,035 | 2,035 | 一致 |
| 表格 / 图示 | 44 / 411 | 44 / 411 | 一致 |
| 块正文 SHA-256 | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | 一致 |

完整 `runtime.submit` 链路也保持相同质量：第一次 `26.448s`，第二次 `1.259s`（**-95.24%**），两次均为 `done`，均产出 2,035 个块和 2,035 个 Chunk；第二次的首尾块 ID 已切换为 `fifth-runtime-latest-b-title` / `fifth-runtime-latest-b-p-2034`，跨页合并元数据中的旧任务 ID 残留为 0，证明缓存没有泄漏上一任务的文档标识。该收益针对同 worker 的同源重复解析，首次冷解析仍需经过第四轮的版面优化路径。

## 第六轮：表格候选与 words 工作集收敛

对当前冷解析做函数级采样后，`find_tables()` 和 `extract_words()` 仍是主要重型调用。本轮将表格候选从 50 页收敛到 44 页：封面、目录和说明页中的普通 `part number` 不再单独触发表格检测；程序表行、独立表头、FIG-ITEM/Units per Assy 等明确证据仍保留。代表 PDF 的 44 个有效表格页全部保留。

同时，自适应路径对 22 个“表格页但无图示”的页面不再物化 words；图示页和可能需要列判断的页面仍保留 words 路径。标题块新增 `pdf_layout_word_page_count=217`，可在线观测工作集。

| 性能工作量指标 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| `find_tables()` 调用页数 | 50 | 44 | **-12.0%** |
| `extract_words()` 实际调用次数 | 229 | 207 | **-9.6%** |
| 布局候选页 | 239 | 239 | 不变 |

冷解析质量保持一致：2,035 个块、44 个表格、411 个图示，正文 SHA-256 仍为 `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06`；完整链路冒烟为 `26.414s`、状态 `done`、2,035 个 Chunk。由于单次冷解析受 pdfplumber、磁盘和 SQLite 抖动影响，本轮把“减少重型调用次数”作为确定性收益，不把 23.5–25.6 秒区间内的单次端到端差异当作稳定 p95 结论。

## 第七轮：并发同源解析 single-flight

第五轮的 LRU 只能复用已完成的结果；多个请求同时到达时，仍可能各自进入冷解析。本轮在同一 parser 内按“租户 + 源文件指纹 + 解析选项”维护 in-flight Future：第一个请求负责解析，其余请求等待同一个结果，再按自己的 `doc_id` 重绑定块和合并元数据；异常也会广播给等待者，不会留下永久占用的 flight。

代表 PDF 两路并发请求的对比：优化组共享一个 parser，控制组使用两个独立 parser（因此没有 in-flight 合并）。

| 指标 | 独立 parser 控制组 | single-flight 优化组 | 变化 |
| --- | ---: | ---: | ---: |
| 两路并发总耗时 | 46.855 s | 22.121 s | **-52.79%** |
| 实际未缓存解析次数 | 2 | 1 | **-50%** |
| 每路解析块 | 2,035 | 2,035 | 一致 |
| 两路正文 SHA-256 | 相同 | 相同 | 一致 |

优化组两路首块 ID 分别为 `seventh-concurrent-a-title`、`seventh-concurrent-b-title`，说明等待结果不会复用上一任务的文档标识。该收益针对同 worker 的并发同源请求；单路首次冷解析耗时不因 single-flight 改变。

## 第八轮：同内容跨路径缓存

实际重复上传通常会生成新的临时路径。此前基于路径的指纹无法命中这类请求；本轮增加 SHA-256 内容指纹，并用一个小型 `(路径, 大小, mtime_ns)` LRU 避免同一路径在一次解析链路中重复读文件。租户和解析选项仍参与最终缓存键，避免跨租户或跨配置复用。

代表 PDF 复制到新路径后，在同一 parser、同一租户下使用两个不同 `doc_id` 连续解析：

| 指标 | 原路径 / 首次 | 新路径 / 第二次 | 变化 |
| --- | ---: | ---: | ---: |
| 解析器耗时 | 23.108 s | 1.186 s | **-94.87%** |
| 解析块 | 2,035 | 2,035 | 一致 |
| 表格 / 图示 | 44 / 411 | 44 / 411 | 一致 |
| 正文 SHA-256 | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | 一致 |

第二次首尾块 ID 为 `eighth-path-b-title` / `eighth-path-b-p-2034`，证明跨路径命中后仍完成当前任务的 ID 重绑定。文件内容变化、租户变化或解析选项变化会产生新的指纹/键，不会误命中旧结果。

## 第九轮：缓存命中后的 document views 轻量化

第八轮已经把同内容跨路径的解析阶段降到秒级，但完整 `runtime.submit` 的缓存命中仍要重新生成 pages/lines/records。旧路径调用完整 structured projection，连带构建 coverage、IR、parse units 和质量门禁；这些结果并不直接写入 `document_views`。本轮改为只构建持久化所需的三类行，并保留页面质量信号、记录质量信号和跨块字段；当持久化快照没有 chunks/index manifest 时，RAG 缺失信号改用块级轻量判定，带 chunks 的 API fallback 仍走 canonical coverage projection。

代表 PDF、同一 worker、同一租户、不同 `doc_id` 的完整链路对比：

| 指标 | 第八轮缓存命中 | 第九轮缓存命中 | 变化 |
| --- | ---: | ---: | ---: |
| `runtime.submit` 第二次耗时 | 1.259 s | 0.992 s | **-21.20%** |
| 解析块 / 表格 / 图示 | 2,035 / 44 / 411 | 2,035 / 44 / 411 | 一致 |
| 正文 SHA-256 | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | `ec68870c688c3dede01af126cee355bb39e6a84df0a452161fc6ac89aedf8c06` | 一致 |

视图生成微基准（同一份 2,035 块快照、无 chunks/index manifest）为 `0.374 s → 0.132 s`（约 **-64.7%**）。轻量路径与 canonical structured projection 的 254 页质量信号、表格 ID 和记录结果逐项一致；新增回归测试覆盖 pages/lines/records 一致性。冷解析首轮仍受 pdfplumber、磁盘和 SQLite 抖动影响，本轮收益限定为重复解析/重试的命中链路。

## 第十轮：超大 PDF 分片计划延迟物化

原压力工具在仅验证 17,101 页分片计划时，就提前创建 343 个 PDF 分片文件；这会把“计划门禁”和“内容执行”耦合，并使计划耗时受磁盘写入影响。本轮增加 `materialize_part_files=false`：计划阶段只生成 part spec 和 job，真正执行某个 part 时才按其页段创建文件。执行路径增加幂等的 lazy materialization，既保留原有显式创建行为，也避免计划阶段产生大量临时文件。

| 指标 | 提前物化 | 延迟物化 | 变化 |
| --- | ---: | ---: | ---: |
| PDF 页数 | 17,101 | 17,101 | 0 |
| 分片数 | 343 | 343 | 0 |
| 计划耗时 | 10.926 s | 5.44 s | **-50.2%** |
| 计划错误数 | 0 | 0 | 0 |
| 计划阶段分片文件 | 343 | 0 | **-100%** |

该轮只验证计划阶段，`snapshot_blocks_min=100000` 在 `execute_parts=false` 时明确记为 skipped；真正执行分片的内容质量仍需单独运行执行门禁，不能由本表推断。

随后在生产配置下完成六批受控内容执行：首批 20 个分片、第 1–50 号、第 51–100 号、第 101–150 号、第 151–200 号和第 201–250 号分片均 0 错误；第 201–250 号分片覆盖第 10,001–12,500 页，产出 2,550 个 block 和 2,550 个 Chunk，单分片平均 6.065 s、最大 8.099 s。前六批使用旧增量刷新路径，继续观察到 SQLite 快照读放大；父索引现在改为运行时缓存最新子任务并按变更 part 更新 manifest，避免每个分片扫描整个租户 job 表。优化后的第 251–300 号分片批次覆盖第 12,501–15,000 页，0 错误，单分片平均 5.166 s、最大 5.795 s，较前一批平均耗时下降约 14.8%。该数字是受控对照，不宣称跨机器或全量线性扩展。压力工具新增 `--part-start`，支持按页段继续执行；此前未覆盖的第 251–343 号分片随后已补齐，合成 17,101 页全页聚合门禁已通过。`--parallel-parts` 默认值为 1 以保持旧行为，统一 self-check 也支持 `parallel_parts` 配置。

## 第十一轮：P0 只读质量审计工件

新增固定样本清单和 `tools/p0_quality_audit.py`，对普通 PDF、表格 PDF、多栏 PDF、OCR PDF、图文手册、DOCX、Excel 运行 `projection=full`，并额外生成页级 `coverage_report.jsonl`。按当前代码最新 r4 工件实测 7/7 样本完成、31 个页级记录、746/746 个 chunk 可追溯到 block/page、缺口原因完整率 100%；2 个空白页未产出 block，pypdf 探针已明确记为 `page_without_extractable_content`。工具另提供只读 `--embedding-provider fake` 复核，7/7 样本 embedding coverage 通过，未改生产配置。

## 第十二轮：分片父索引增量缓存与全页覆盖

生产批次观察到父文档每完成一个 part 都会扫描租户 job 表并重建完整 manifest，数据库历史任务增多后出现读放大。本轮在 inline 运行时维护 parent-scoped 最新子任务缓存，并让增量 manifest 只更新 changed part 的 block/chunk 数量、chunk IDs 和状态；公开查询、取消和批量重跑仍强制刷新，queue-worker 模式关闭该缓存以避免跨 worker 状态陈旧。运行时回归 59 项通过。

受控对照（同一生产配置、串行、延迟物化）：第 201–250 号分片平均 6.065 s、最大 8.099 s；缓存路径下第 251–300 号分片平均 5.166 s、最大 5.795 s，平均耗时下降约 **14.8%**，且两批均为 0 错误。随后第 301–343 号分片 43/43 成功，平均 4.570 s。7 份非重叠报告聚合为 17,101/17,101 页、343/343 分片、17,444 block/chunk，页段无缺口和重叠；聚合工件为 `var/self-check/p0-large-pdf-stress-full-coverage-20260714.json`。

## 第十三轮：候选 Provider 实际对标

在项目 `.venv` 安装 `pymupdf4llm 0.3.4` 和项目声明的 `docling 2.113.0` 后，使用同一组 PDF 页段对比 `pdf-text / pymupdf4llm-local / docling-local / mineru-local`。`pdf-text` 与 `pymupdf4llm-local` 的历史窗口均已完成；Docling 先因未安装依赖失败，补齐依赖后又暴露了中文安装路径下原生 glyph 资源兼容问题，现已由适配器的 ASCII junction/copy fallback 修复。修复后的 3 个真实 PDF 固定 1–5 页窗口共 9/9 provider runs 完成，窗口 gate 为 `accept_with_warning`；`docling-local` 的结构输出可用，但单窗口耗时约 `24.786–74.468 s`，明显高于 `pdf-text` / `pymupdf4llm-local`。一次 254 页整文档长跑在约 15 分钟门限内未完成且工作集达到约 2.5 GB，因此 Docling 仍保持 `evaluate/pending`，不自动修改默认 route；MinerU 保持 skipped。窗口报告为 `var/self-check/provider-comparison.pages-1-5-r16.json`。
补充表格/OCR 样本 10 页双跑：表格 PDF `pdf-text 44.517 s / 5 tables`，`pymupdf4llm-local 12.957 s / 24 tables`；CMM/OCR PDF `pdf-text 23.701 s / 6 tables`，`pymupdf4llm-local 8.659 s / 24 tables`。表格计数差异说明候选 Provider 的结构结果仍需人工 gold 校验，不能仅按耗时切换。报告为 `var/self-check/provider-comparison-candidates-table-ocr-venv-20260714.json`。

## 第十四轮：embedding 链路与空白页诊断

P0 审计新增显式的本地 fake embedding 覆盖开关，避免把默认关闭 embedding 造成的 `chunks_not_embedded` 与解析缺口混为一谈。对同一 7 类真实样本重跑后，7/7 完成、632 个 chunk 全部完成 embedding，chunk→block→page 追溯仍为 100%；普通 PDF 第 4 页和多栏手册第 2 页由 pypdf 确认无文本/图片，标记为 `page_without_extractable_content`。该开关只替换本次审计进程内的 embedding provider，不改变 `parsecore.toml`、默认 route 或索引配置。工件分别为 `var/self-check/p0-quality-audit-20260714-r3/summary.json` 和 `var/self-check/p0-quality-audit-20260714-fake-r2/summary.json`。

## 第十五轮：典型文档当前版本复测

对同一份 `D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf` 重新执行 `tools/parse_perf_baseline.py`，与同口径现状工件比较：

| 指标 | 现状复测 | 当前版本 | 变化 |
| --- | ---: | ---: | ---: |
| 端到端耗时 | 148.144 s | 132.323 s | **-10.68%** |
| 峰值内存 | 775,464.208 KB | 713,724.026 KB | **-7.96%** |
| 解析块 / Chunk | 2,035 / 2,035 | 2,035 / 2,035 | 一致 |
| 表格 / 图示 | 44 / 411 | 44 / 411 | 一致 |
| 内容页 / 阅读顺序置信度 | 254 / 0.98 | 254 / 0.98 | 一致 |

当前版本工件为 `var/self-check/optimization-current-20260714.json`；质量投影仍为 `accept_with_warning`，唯一告警是默认未启用真实 embedding 的 `rag_chunks_not_embedded`，不构成解析正文、表格或图示退化。此前首轮 `adaptive_dual_channel` 对照与本轮重复复测属于不同采样窗口，发布判定以同口径工件和质量指纹为准。

## 第十六轮：gold 证据包与候选准入阻断

为避免“自动指标看起来更快”直接触发路由切换，本轮生成了 50 页、覆盖 5 个 PDF 的只读 evidence packet（PNG、pypdf 文本探针、SHA-256 和 manifest），并完成 50 页 `pdf-text` / `pymupdf4llm-local` 双跑。候选 50/50 完成，基线 49/50 完成；候选已完成页平均 1.132 s，基线已完成页平均 3.096 s，但当前 gold corpus 为 `approved=0 / pending=50`，候选全部命中 `provider_license_not_approved` hard veto，准入建议为 `remain_shadow_only`。因此性能优势只作为候选观测，不改变默认 `pdf-text` route。

按文档类型汇总的已完成页平均耗时如下；结构数量同时变化，必须由人工 gold 解释，不能把耗时差直接当作质量提升：

| 文档类型 | `pdf-text` | `pymupdf4llm-local` | 候选变化 | 结构观察 |
| --- | ---: | ---: | ---: | --- |
| 图文手册 | 0.87 s | 0.43 s | -50.6% | blocks 21 → 217 |
| 多栏 PDF | 2.80 s | 0.29 s | -89.6% | blocks 45 → 70 |
| 普通 PDF | 1.00 s（9 页） | 3.48 s | +248% | 基线 1 页未完成，候选存在长尾 |
| 扫描/OCR | 4.68 s | 0.70 s | -85.0% | blocks 223 → 48，tables 2 → 10 |
| 表格密集 | 5.92 s | 0.77 s | -87.0% | blocks 213 → 312，tables 1 → 8 |

证据和评测工件分别为 `output/pdf/provider-gold-review-20260714/manifest.json` 与 `var/self-check/provider-gold-pending-full-20260714.json`。

证据目录现在同时包含 `RISK_REVIEW.md`，将前 20 个优先复核页与原始截图、文本探针关联，缩短人工 gold 标注定位时间；该索引只读，不会自动批准页面。

本轮同时把待审核风险摘要写入 `gold_evaluation.risk_summary`，自动列出每个文档的平均/p95/最大耗时、结构总量和优先复核页。当前最高风险页为普通 PDF 第 165 页：候选 `37.039 s`、基线 `5.397 s`（+586.3%），blocks `13/8`、tables `2/1`；普通 PDF 第 100 页的基线 provider 未完成。该摘要只用于人工复核排序，不把 pending 页面当成 gold 结论。

针对第 165 页长尾做了本地 `pymupdf4llm 0.3.4` 参数探针：默认单页约 `4.989 s`；`ignore_graphics=true` 或 `graphics_limit≤500` 可降到约 `0.10–0.13 s`，但 Markdown 表格分隔符从 `630` 变为 `0`，会直接丢失表格结构，因此没有把这个“快路径”写入默认配置。`graphics_limit≥1000` 又回到约 `3.9–4.0 s`，说明下一轮应优先研究按页识别图形/表格密度的受控 fallback，而不是全局关闭 graphics。

随后用临时 ParseCore 配置做普通 PDF 10 页复跑：默认候选平均 `3.982 s`、最大 `37.039 s`，`ignore_graphics=true` 后平均 `0.276 s`、最大 `1.837 s`，第 165 页降到 `0.292 s`；但候选表格总数从 `3` 降为 `0`。这证明“按页受控 fallback”有性能价值，但全局 tuning 不满足质量门禁。调参工件为 `var/self-check/provider-gold-tuned-ignore-graphics-ordinary-20260714.json`。

同时修正了 Windows 下第三方 PDF 句柄延迟释放导致的临时 part 清理竞态：评测结果已生成时不再因 cleanup 阶段的 `WinError 32` 将整批报告判为失败，后续仍保持 best-effort 清理。

## 第十九轮：默认路径最终回归

在受控参数透传和 cleanup 容错落地后，使用同一份 `D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf` 做默认 `pdf-text` 路径复测：

| 指标 | 上轮当前版本 | 本轮复测 | 变化 |
| --- | ---: | ---: | ---: |
| 端到端耗时 | 132.323 s | 121.366 s | -8.28% |
| 峰值内存 | 713,724.026 KB | 713,721.034 KB | 基本不变 |
| blocks / chunks | 2,035 / 2,035 | 2,035 / 2,035 | 一致 |
| tables | 44 | 44 | 一致 |

相对最初现状基线 148.144 s，本轮累计下降约 **18.1%**；质量指纹未变化。由于这是单次冷运行，暂把它作为“无回归且有改善”的证据，不立即把 121.366 s 写成新的稳定 SLA 基线；后续应重复 3 次取中位数。

## 第二十轮：默认路径三次稳定性复测

针对上一轮的单次冷运行限制，在同一份典型 PDF、同一默认 `pdf-text` 配置下连续复测 3 次（r4–r6），三次均 `status=ok` 且 `failed_documents=0`：

| 指标 | r4 | r5 | r6 | 三次摘要 |
| --- | ---: | ---: | ---: | ---: |
| 端到端耗时 | 136.926 s | 120.755 s | 135.035 s | 中位数 135.035 s；范围 16.171 s |
| 峰值内存 | 715,961.538 KB | 715,962.811 KB | 715,962.703 KB | 均值 715,962.351 KB |
| blocks / chunks | 2,035 / 2,035 | 2,035 / 2,035 | 2,035 / 2,035 | 三次一致 |
| tables | 44 | 44 | 44 | 三次一致 |
| figures | 411 | 411 | 411 | 三次一致 |

三次稳定性证据汇总为 [`optimization-current-20260714-stability.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\optimization-current-20260714-stability.json)，单次工件为 r4、r5、r6 JSON/Markdown。中位数相对最初 148.144 s 基线下降 **8.85%**，但相对上一轮当前值 132.323 s 高 **2.05%**；耗时区间波动较大，因此只把 135.035 s 作为当前稳定性观察值，不把它写成硬 SLA，也不以此触发 Provider 路由切换。质量计数三次完全一致，默认路径无解析错误。

## 第二十一轮：回归基线与当前质量指纹同步

快速 self-check 首次复跑发现旧 regression baseline 仍记录 1,624 blocks，而当前已确认的默认路径质量指纹为 2,035 blocks；该差异来自此前 P0 结构拆分后的基线未同步，不是本轮审核工具运行错误。为避免门禁长期误报，先将旧的三份 baseline 备份到 [`regression-baseline-before-refresh-20260714`](D:\个人文件\个人开发\解析管理中台\var\self-check\regression-baseline-before-refresh-20260714)，再按当前代码和同一典型 PDF 重生成：

- `var/regression/baseline.json`：默认路径，2,035 blocks、44 tables。
- `var/regression/baseline.table-structure.primary.json`：表格结构 profile，2,035 blocks、44 tables。
- `var/regression/baseline.strip_hf.json`：去页眉页脚 profile，2,002 blocks、44 tables、219 个 stripped pages。

同步后 [`p0-self-check-20260714-r11.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r11.json) 已通过：unit tests `541 passed / 5 skipped`、payload contracts、runtime describe 和 fast regression suite `3/3` 全部通过。随后新增候选复用、Provider failure category、parser lifecycle warm-state、self-check 参数透传、解析缓存 telemetry、稳定性门禁和许可证证据测试，最新 [`p0-self-check-20260714-r24.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r24.json) 为 `548 passed / 5 skipped`，独立 pytest 当前为 `564 passed, 5 skipped, 51 subtests passed`。这次只同步了已验证的质量指纹，不放宽 block/table/结构预算。

## 第二十二轮：用户授权的 AI-assisted gold 核验

用户明确要求“由 Codex 检查批准，不等待人工”后，使用新增的 `tools/ai_gold_review.py` 对 evidence packet 的 50 个页面执行受控 AI-assisted review。工具逐页校验 PNG/文本探针存在性和 SHA-256，核对 `pdf-text` 基线块位置、Provider provenance、页级文本/图片探针，并对确认空白页 `ordinary-pdf p100` 写入空白页期望；同时记录 8 个代表性/高风险页面的视觉 spot-check。审核者写为 `Codex (AI-assisted, user-authorized)`，审计范围显式为 `ai_assisted_review_not_human_gold`，没有修改 Provider 许可证、默认 route 或 `approved_provider_ids`。

状态校验工件 [`provider-gold-review-status-20260714-ai-r2.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-review-status-20260714-ai-r2.json) 为 `status=ready`：`50 approved / 0 pending / 0 rejected / 0 errors`。此前 [`provider-gold-ai-approved-20260714.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-approved-20260714.json) 使用了性能探针，现保留为历史对照；最新默认关闭 `tracemalloc` 的真实耗时复评为 [`provider-gold-ai-approved-20260714-r2.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-ai-approved-20260714-r2.json)：

| 指标 | `pdf-text` 基线 | `pymupdf4llm-local` 候选 | 解释 |
| --- | ---: | ---: | --- |
| 完成页 | 49/50 | 50/50 | 基线无法抽取确认空白页 p100；候选产生 1 个合成标题，已用 `mustNotBeHeading` 约束 |
| 已完成页平均耗时 | 2.027 s | 0.272 s | 候选约 -86.6%，但不是质量通过 |
| p95 / 最大耗时 | 5.316 / 6.331 s | 0.561 / 5.818 s | 普通 PDF p165 为最高长尾，候选约 5.818 s |
| 总 blocks / tables / figures | 570 / 7 / 34 | 780 / 23 / 0 | 结构差异明显，尤其 figure 计数和表格拆分 |
| 准入建议 | route primary | `remain_shadow_only` | 候选命中许可证 hard veto，并有关键 token/表格锚点/结构差异风险 |

因此本轮完成“页面批准、真实耗时复测和对比证据收口”，不把速度优势写成质量提升，也不切换默认路由。最高风险页为普通 PDF p165（候选 5.818 s vs 基线 2.567 s，+126.6%，blocks 13/8、tables 2/1）；扫描/OCR 页面通常更快但 figures 由 20 降为 0，表格计数也改变。对比工具现默认不启用 `tracemalloc`，避免 native PDF 解析被 Python 内存追踪放大；如需旧的 Python 峰值内存指标，可显式追加 `--track-python-memory`，但该模式不用于真实耗时 SLA。若治理仍要求独立人工 gold，AI-assisted 批准不能替代人工签字；若接受用户授权的受控 AI 评审，则当前 50 页离线候选入口已 ready，但候选仍需许可证、结构质量和连续 3 次稳定运行门禁。

## 第二十三轮：Provider suite 入口与 Windows 编码修复

包含 Provider 对比的 fast self-check 首次复跑发现两个入口问题：直接执行 `tools/self_check.py` 时仓库根目录未加入 `sys.path`，且 Windows 默认 GBK stdout 无法输出 PDF 中的 `\uf02d` 字符。现已分别修复 self-check 根目录导入和 comparison CLI 的 UTF-8 stdout；轻量中文 Markdown 样本直接 stdout 验证通过。随后安装项目声明的 Docling extra，并在 `DoclingParser` 增加 Windows 中文安装路径兼容：原生扩展通过临时 ASCII junction（受限环境退回复制）读取 glyph 资源。修复后的 3 个真实 PDF 1–5 页窗口报告 [`provider-comparison.pages-1-5-r16.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-comparison.pages-1-5-r16.json) 为 9/9 完成、gate `accept_with_warning`；完整 254 页 fast suite 首样本约 15 分钟仍未完成，未把长跑中断当作通过，默认 route 继续保持 `pdf-text`。

## 第二十四轮：Docling 依赖与中文路径兼容收口

按项目 `pyproject.toml` 的 `docling>=2.18,<3` 安装了 Docling 2.113.0、Docling Parse 7.8.0 和 CPU Torch 2.13.0。首次真实探针发现 Windows 原生扩展在中文虚拟环境路径下误报 `pdf_resources/glyphs/standard/additional.dat` 不存在；文件实际存在，问题来自原生扩展资源路径兼容。现在 `DoclingParser` 在加载转换器前检查安装路径，必要时建立临时 ASCII junction，junction 不可用时退回一次性复制，并将 ASCII 目录置于 `sys.path` 首位。

修复验证：

- `WP08-07事件调查的管理.pdf`：`pdf-text` 与 `docling-local` 均完成，Docling 输出 168 blocks / 2 tables，未再出现资源文件错误；最新回归工件为 `var/self-check/provider-docling-probe-r17-final.json`。
- 三份真实 PDF 的第 1–5 页窗口：`pdf-text`、`pymupdf4llm-local`、`docling-local` 共 9/9 完成，gate=`accept_with_warning`；Docling 窗口耗时分别约 24.786 s、74.468 s、29.084 s，结构数量与另外两个 provider 不同。
- 254 页典型 PDF 的整文档探针：首样本在约 15 分钟内未完成，工作集约 2.5 GB；该次按资源门限停止，没有生成半成品报告，也没有改写 route/admission。

因此本轮闭合了“依赖缺失/中文路径导致无法运行”的环境阻塞，但没有闭合 Docling 的整文档性能和结构质量准入。候选仍为 `evaluate/pending`，生产默认仍为 `pdf-text`；后续应优先研究页级模型复用、OCR/版面分层和进程内存上限，再做完整 suite。

同轮增加了 Docling 的显式 `fast-text` 候选配置（`do_ocr=false`、`do_table_structure=false`、`force_backend_text=true`）。在第 1–5 页受控探针中，普通 PDF 从 30.387 s 降到 6.381 s，blocks/tables/figures 保持 40/0/0；表格 PDF 从 84.063 s 降到 9.519 s，但 tables 从 1 变为 0。该结果支持“无表格/OCR 信号页才走快路径”的后续页级 fallback，不支持全局切换。详细工件为 `var/self-check/docling-pipeline-profile-probe-r18.json`。

为降低 Docling 在分片复跑场景中的冷启动成本，`DoclingParser` 新增显式
`reuse_converter` 候选开关，并在仓库候选配置中打开；它只在同一 parser 实例的长生命周期
worker 内复用 `DocumentConverter`，默认库行为仍为每次 parse 新建。当前尚未把这项
优化写成收益 SLA：需要在同一 worker 上完成 cold/warm 双口径、工作集和并发安全复测后，
才能进入 provider 准入结论。补做的 10 页 bounded probe 中，cold=`29.883 s`、
warm=`13.299 s`（观察到 `-55.5%`），blocks/tables/figures 均为 `168/2/0`，结构指纹
完全一致；工件为 `var/self-check/docling-reuse-probe-r19.json`。该结果只证明同一
parser 实例的热复用候选可行，不代表全量 SLA 或并发安全已批准。

同时，Docling 首块 provenance 现在记录 `converter_reuse_enabled` 和
`converter_cache_hit`，后续 provider comparison 可以把冷启动成本单独拆出，避免把
“首次加载模型/布局资源”误判为稳态解析吞吐。

Provider comparison 的失败结果也完成分类：依赖未安装/Provider 不可用、超时、输入文件、
权限、媒体类型不支持和一般解析失败分别落入 `failure_category`，并在 gate summary 的
`failure_categories` 中计数。这样 Provider suite 失败时可以先判断是环境阻塞还是性能/质量
回归，不会因为缺少可选依赖而误改默认路由。

对启用了 Docling converter 复用的候选，单次 Provider run 还会输出
`converter_cache_state=cold|warm`，suite summary 会汇总 cache hits/misses；当前对比工具
仍按样本新建 parser，因此正式 warm 命中需要使用同一长生命周期 worker 另行验证。

现已提供 `--reuse-parser-instances` 候选测量开关：在同一 suite 内按 Provider 复用一个 parser
实例，可以直接观察 cold→warm 的 converter cache 命中；默认仍为 `new_per_run`，已有冷启动
基线工件和历史性能数字保持原口径，不与 warm-state 结果混比。

本轮进一步把 `pdf-text` 的同源重解析缓存纳入对比观测：启用 `post_process.parse_cache` 后，
首块 provenance 会写入 `parse_cache_state=cold|warm`，Provider suite summary 统计
`parse_cache.observations / hits / misses`。缓存只复用同一来源指纹和请求选项的 Block 模板，
返回时仍重新绑定当前 `doc_id / block_id`，不改变正文结构；默认库行为保持关闭。

## 下一阶段建议

- OCR：该样本仍有大量 OCR 异常页，下一步应做 OCR 批处理/并发与模型热复用，并继续用正文指纹和表格/图示计数做门禁。
- 索引：生产环境启用异步批量 embedding；当前基准故意使用空 embedding，质量报告因此提示 `rag_chunks_not_embedded`。
- Provider 对标：将 PaddleOCR、MinerU、Docling、Marker 以 sidecar 方式接入同一份样本集，按覆盖率、表格结构、阅读顺序、耗时和内存统一评分后再启用路由。
- 监控：保留 `pdf_layout_strategy`、候选页数、阶段耗时、p95/p99 和质量信号，建立每次版本升级的自动回归阈值。

## 第二十五轮：真实本地 embedding/RAG coverage 验收

为关闭“只能用 fake embedding 证明 coverage”的证据缺口，本轮新增可选的
`sentence-transformers-local`/`transformers-local` embedding provider。模型按显式配置
加载，支持 `local_files_only`、CPU/auto device、batch、最大 token 长度和 L2 归一化；默认
生产配置仍关闭 embedding，不会联网或改变路由。

使用 `D:\app\uploads` 的 7 类真实样本和本地 `all-MiniLM-L6-v2`（384 维）复跑：

| 指标 | 结果 |
| --- | ---: |
| 样本 / 页 | 7/7；31 页 |
| blocks / chunks | 1,919 / 746 |
| embedded chunks | 746/746（100%） |
| chunk→block→page 追溯 | 746/746（100%） |
| 质量审计 gate | `passed` |
| embedding smoke | 5/5 embedded；384 维；向量范数均值 1.0；语义 search 命中 |

审计工件为 [`p0-quality-audit-20260714-local-embedding/summary.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-quality-audit-20260714-local-embedding\summary.json)，配置副本为 [`p0-local-embedding.toml`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-local-embedding.toml)。这证明本地解析→chunk→真实 embedding→coverage/search 链路可运行；仍不等同于远程 embedding 网关、索引写入、召回/重排和线上业务命中率验收。

本轮回归：独立 pytest `565 passed, 5 skipped, 51 subtests passed`，`compileall` 通过；最新快速 self-check [`p0-self-check-20260714-r25.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-self-check-20260714-r25.json) 状态 `ok`，内置 unit `549 passed, skipped=5`，payload 6/6，runtime describe 通过，fast regression suite 3/3。

当前仍不切换默认 Provider route。默认 `pdf-text` 的 P0 本地验收已由 `p0-core-readiness-20260715.json` 收口；`docling-local` 50 页全 Gold 评估证明其平均质量 `53.021`、平均耗时 `11.444 s`、最大 `234.983 s`，不具备替代价值。剩余生产扩展门禁集中在候选 Provider route promotion 的许可证/结构质量决策，以及远程 embedding/RAG 线上链路的真实凭证与命中率验收。

## 第二十六轮：真实上传目录批量审计与纯扫描页降级（2026-07-15）

本轮把 `D:\app\uploads` 去重后的 28 份真实源文档纳入同一份可恢复清单，审计工具增加
`--sample-id`、`--resume` 与 `--rerun-sample-id`，按样本批次执行并保留已完成的 full projection/page coverage。
当前工件 [`p0-upload-full-audit-20260715-local-embedding/summary.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-upload-full-audit-20260715-local-embedding\summary.json)
显示：

| 指标 | 当前结果 |
| --- | ---: |
| 去重真实源文档 | 28 |
| 完成样本 | 28/28 |
| 请求 PDF 页 / coverage 页 | 2,131 / 2,139 |
| blocks / chunks | 19,458 / 18,218 |
| 本地 embedding / chunk 追溯 | 18,218/18,218 / 100% |
| 无可提取内容页 | 64 个真实上传 PDF 页（视觉核验：63 纯白、1 页仅矢量横线；现均有显式 `empty_page` 工件） |
| 审计 gate | `passed` |

纯扫描页此前会因 native text 为空而直接抛出 `No extractable text found in PDF`。现在只有确认存在
页面图像时才触发 `native_text_empty` OCR；OCR 无结果时保留不可索引的页工件，并记录
`ocr_attempted / ocr_failed / empty_page`，既不把空页伪造为正文，也不丢失页级审计证据。另修复
跨页段落合并后的 `page_span` 传播：原先 386 个 `parser_page_not_emitted` 已归零，长文 574/574
页均有 coverage 记录，仅第 2 页无可提取内容。新增行为、IR coverage 跨页展开与审计断点续跑单测
均通过（55 tests）。64 个真实上传页的 30 DPI 视觉证据见 [`p0-empty-page-evidence-20260715-r2/manifest.json`](D:\个人文件\个人开发\解析管理中台\output\pdf\p0-empty-page-evidence-20260715-r2\manifest.json)：63 页纯白，1 页只有矢量横线；证据包另外包含 2 个既有低清 OCR fixture 页，共 66 页、渲染错误 0。按用户授权生成的 [p0-empty-page-review-20260715.json](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-empty-page-review-20260715.json) 已将真实上传的 64 页全部判为 `approved_non_indexable`。候选 Provider 的最新连续稳定性见 [`p0-candidate-gold-stability-gate-20260715-r2.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-candidate-gold-stability-gate-20260715-r2.json)：r7/r8/r9 均 50/50 完成，quality signature 稳定，门禁 `accept_with_warning`，但许可证/结构差异仍禁止自动切换 route。新增的 [`provider-gold-docling-20260715-r1.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\provider-gold-docling-20260715-r1.json) 显示 Docling 全 Gold 平均质量 53.021、平均耗时 11.444 s、最大 234.983 s，不能替代候选。新增 [`local-rag-acceptance-20260715.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\local-rag-acceptance-20260715.json) 显示本地 Transformer 6/6 embedded、hit@3=1.0。最终 readiness 分 scope：[`p0-core-readiness-20260715.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-core-readiness-20260715.json) 本地 5/5、默认 P0 路径 `release_ready=true`；[`p0-release-readiness-20260715.json`](D:\个人文件\个人开发\解析管理中台\var\self-check\p0-release-readiness-20260715.json) 保留生产 scope 的 2 个外部 open checks。加入本地 RAG acceptance 与 readiness scope 单测后的全量回归为 `582 passed, 5 skipped, 51 subtests passed`；`passed` 只表示审计记录与追溯闭环，不等于这些页具备正文质量。
