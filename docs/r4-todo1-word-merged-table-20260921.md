# 待办 1 交付报告：Word 合并表格结构保真 + Docling 直映结构

日期：2026-09-21（同日续写：补 Docling 直映结构、Excel 合并表达统一、条目 `kind` 语义迁移）
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 1
状态：Word 链路、Docling 直映、Excel 合并表达、条目 `kind` 语义、区域素材落盘均已实施；剩余见第 10 节

## 1. 这一项解决什么

Word 表格的合并关系在原生提取阶段就被丢了。`_docx_table_cells()` 把 `<w:tbl>` 读成字符串二维
数组：`w:gridSpan`（跨列）和 `w:vMerge`（跨行）都不读，行内加粗/上下标/超链接也不读。下游
三个消费者各自从这堆字符串里再猜一次表格形状：

| 消费者 | 改造前 |
| --- | --- |
| `api_payloads._structured_tables()` | 从 `metadata["cells"]` 重建单元格 |
| `ir._table_rows_from_cells()` | 从投影后的 `cells` 再重建一次，自己写 Markdown |
| `pipelines._knowledge_unit_table_text()` / `_normalize_table_cells()` | 又从 item metadata 重建，另一个 Markdown 渲染器 |

结果是同一张合并表头在结构化投影、IR 文本、知识单元、导出里可能给出不同的网格，或者被压成
无法与"值恰好重复"区分的形式。本次把网格收敛到一处：解析器读原生结构，其余只做派生。

同类问题也存在于 Docling：它本来就给出层级、表格网格（含合并）、行内格式与来源，但旧路径先
导出 Markdown 再由 Markdown 重建 Blocks——Markdown 表达不了合并、下划线和上标，网格只能再猜
一次。第 5 节记录这一段的改造与实测。

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/tables.py](../src/parsecore/tables.py)（新增） | 唯一的表格网格约定：`GridCell` / `TableGrid`、`grid_from_metadata()`、`grid_from_rows()`、`grid_from_cells()`、`resolve_table_grid()`、`rows_from_table_payload()`、`render_markdown()` / `render_tsv()`；合并侧 `cell_records()`（只认描述符记录）、`apply_merge_records()`（把表外声明的合并补进网格）、`merge_descriptors()`（统一产出合并记录且不重复） |
| [src/parsecore/parsers.py](../src/parsecore/parsers.py) | `DocxParser` 读取 `w:gridSpan` / `w:vMerge` / 行内 `r:` 链接（`word/_rels/document.xml.rels`），输出 `table_cells`（起始行列、`row_span`/`col_span`、`continuation`/`origin`、行内 `runs`）、`merged_cells`、`header_values`、`source_row_col_counts`；`cells` 与 Markdown 由同一网格派生。`ExcelParser._excel_table_grid()` 把工作表合并转成同一份跨度（xlsx/xls 共用），表外合并在 `source_merged_ranges` |
| [src/parsecore/api_payloads.py](../src/parsecore/api_payloads.py) | 结构化表格优先读 `table_cells`，单元格带跨度与合并标记；`merged_cells` 一律由网格派生（不再透传解析器原值），记录表头用解析器给出的 `header_values` |
| [src/parsecore/ir.py](../src/parsecore/ir.py) | IR 表格携带同一份 `cells`，并派生出 `merged_cells` / `header_values`，Reader 因此也能显示合并 |
| [src/parsecore/pipelines.py](../src/parsecore/pipelines.py) | 表格增强、知识单元文本与摘要走同一个网格；合并关系进入知识单元 `table_summary`；条目 `kind` 表示内容形态、`semantic_role` 表示用途语义（见第 8 节） |
| [src/parsecore/runtime.py](../src/parsecore/runtime.py) | 知识单元形态判定与 `pipelines` 同一规则（只读 `kind`），不再从 `provenance` 兜底 |
| [src/parsecore/payload_schemas.py](../src/parsecore/payload_schemas.py) | `document-ir` 的表格增加可选 `merged_cells` / `header_values` |
| [tests/test_tables.py](../tests/test_tables.py)（新增） | 22 项：网格读取优先级、合并值填充、表头展开、Markdown/TSV 转义、合并记录的读取与应用 |
| [tests/test_docx_table_grid.py](../tests/test_docx_table_grid.py)（新增） | 8 项：原生提取、结构化/记录/Reader/IR/知识单元一致性、schema 校验 |
| [tests/support.py](../tests/support.py) | `build_docx_table_xml()`（`col_span` / `vmerge` / 原始 `runs`）、`build_docx_hyperlink()`、`create_docx_with_body(hyperlinks=...)` |

## 3. 唯一网格约定

`parsecore.tables` 决定三件事，其他模块不再重复判断：

1. **从哪个键取网格**：`table_cells`（结构化，权威）> `cells` > `cells_preview`。
2. **合并单元格在扁平行里贡献什么文本**：`rows(fill_merged=True)` 把合并值填到它覆盖的每个
   位置。扁平行是按列消费的，留空会被读成"这一列没有值"，而该行其实受合并支配。需要按跨度
   渲染时用 `rows(fill_merged=False)`。
3. **如何渲染**：`render_markdown()` 转义 `|` 并把单元格内换行写成 `<br>`；`render_tsv()` 把
   换行换成空格。Markdown 无法表达 `rowspan`/`colspan`，所以扁平文本重复合并值，而结构化
   输出保留真实跨度——这正是验收要求里"Markdown 无法表达的合并关系保留在结构化输出中"。

`TableGrid.merged_cells()` 由跨度派生，不再由解析器手工维护第二份合并列表；`header_values()`
把合并表头展开到它覆盖的每一列（跨行表头不会被重复计入），并作为记录表头的来源。

## 4. 做减法：替换、合并、删除

| 对象 | 处理 |
| --- | --- |
| 解析器私有 `_DocxCell` / `_DocxTableGrid` | 删除，改用共享 `GridCell` / `TableGrid`；合并列表由跨度派生 |
| `_docx_table_cells()` | 删除（唯一调用方已改用网格） |
| `api_payloads._rows_from_structured_cells()`、`_table_rows_from_metadata()` | 删除，合并进 `tables` |
| `ir._table_rows_from_cells()`、`_markdown_table()` | 删除，改用 `tables.resolve_table_grid()` / `render_markdown()` |
| `pipelines._normalize_table_cells()`、`_pad_rows()`、`_render_table_text()` 的本地实现 | 删除，改用 `tables`（`_render_table_text` 保留为薄封装以维持 `output_format` 语义） |
| `parsers._render_docx_table_markdown()`、`_escape_markdown_table_cell()` | 删除，改用 `tables.render_markdown()`（Word 与 Excel 出口共用） |
| 三套 Markdown 渲染器 | 合并为一套：原先 IR/知识单元的渲染不转义 `|`，单元格含竖线时会把表格拆行 |

## 5. 验证

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . 
Ran 895 tests / OK (skipped=6)

.\.venv\Scripts\python.exe -m unittest tests.test_tables tests.test_docx_table_grid
Ran 24 tests / OK

.\.venv\Scripts\python.exe tools/self_check.py --profile fast --out var/self-check-fast.json
unit_tests 895 passed | runtime_describe passed | payload_contracts passed (schemas=8)
corpus_manifest verified 3/3 | provider_comparison_suite passed (gate=accept)
regression_suite failed —— 既有漂移，见下
```

关键用例（`tests/test_docx_table_grid.py`）：

| 场景 | 结果 |
| --- | --- |
| `w:gridSpan` 表头占两列，第二列是 continuation 而不是第二个值 | 通过 |
| `w:vMerge` 续行归属起始单元格（`row_span=2`，续行 `origin=1.0`） | 通过 |
| 行内加粗 / 上标 / 超链接（链接目标取自 `.rels`）保留在单元格 `runs` | 通过 |
| `cells` 扁平行、Markdown、`table_cells` 出自同一网格 | 通过 |
| 结构化投影单元格带 `row_span`/`col_span`/`continuation`/`origin`，并触发 `table_merged_cells` | 通过 |
| 记录表头取合并表头展开值（`Part` / `Part_2` / `Qty`），不再落到 `col_2` | 通过 |
| Reader 与 IR 的知识单元引用同一份表格文本与单元格跨度 | 通过 |
| `document-ir` / `document-reader` schema 校验通过 | 通过 |
| Markdown 单元格含 `|` 或换行时不破坏表格 | 通过（`tests/test_tables.py`） |

### 5.1 顺带修掉的一个执行路径缺陷

回归门禁跑不起来：真实 297 页样本 `36d65cd6b61346e28e97dbaf829646de.pdf` 的校验结论是
`warning`（`suspect_signature_pages`，1 页），而默认策略 `publish_on_warning=False`，
于是 `publish_run` 抛 `PublishError`。执行路径原先只把 `needs_review` 当作"解析成功但未发布"，
`warning` 落到了硬失败路径：任务 `failed`、产物不可读、质量评估无法进行。

这是在 `b18a525` 的代码上同样复现的（已用未修改源码 A/B 验证），不是本次表格改动引入的。
按"严重度必须单调"的原则修正：

- `run_store.PublicationPolicyError`（`code=run_not_publishable_by_policy`）单独表示"策略要求人工
  决定"，与其他发布故障区分；
- 执行路径对 `warning` / `needs_review` 一律返回"未发布预览"（任务 `done`、`not_ready`、旧正式版本
  不变），只有 `rejected` / `error` 以及冲突、丢锁等真实故障继续走失败路径；
- 显式 `publish_run()` 的 `reason` 因此从 `run_publish_failed` 变为更准确的
  `run_not_publishable_by_policy`（[docs/p0-03](p0-03-run-store-batch1-20260920.md) 已同步）。

修复后门禁可以跑完并给出漂移结论（原先直接中断）：

```text
[check] REGRESSION DETECTED:
  - 36d65cd6b61346e28e97dbaf829646de.pdf: numeric_heavy_total grew by 38 (budget +2)
  - 36d65cd6b61346e28e97dbaf829646de.pdf: suspected_header_footer_total grew by 39 (budget +2)
  - 36d65cd6b61346e28e97dbaf829646de.pdf: total_blocks drifted by +6.88% (budget ±5.00%)
  - 36d65cd6b61346e28e97dbaf829646de.pdf: heading_body_binding_rate dropped by 0.0909 (budget +0.0500)
  表格指标未变：table_ready=1.0000 (baseline 1.0000) table_cells=44 (baseline 44)
```

这四项漂移在未修改的 `14cc7f2` 上同样存在，属于本轮五项待办之外的既有问题；阈值与基线
未做任何放宽，门禁仍然如实退出 1。

## 6. Docling 直映结构（本次补齐）

Docling 的转换结果本来就是结构对象：条目带层级（`iterate_items()` 的 `level`）、表格带网格与
合并跨度（`TableItem.data.table_cells` 的 `start_row_offset_col/idx`、`row_span`、`col_span`、
`column_header`）、行内格式（`formatting.bold/italic/underline/script`、`hyperlink`）以及来源
（`prov[].page_no/bbox`）。旧路径把这些导出成 Markdown 再重建，Markdown 表达不了的部分直接
丢失，网格只能再猜一次。

现在 `DoclingParser.parse()` 优先走 `_build_docling_provider_blocks()`：

| 结构 | 映射 |
| --- | --- |
| `title` / `section_header` | `TITLE` 块（`heading_level` + `is_section_heading` + `semantic_role`） |
| `table` | `TABLE` 块：`table_cells`（行列 + 跨度 + continuation/origin）、`cells`、`header_rows`、`merged_cells`、`header_values` 全部经 `parsecore.tables` 派生，Markdown 正文也由同一网格渲染 |
| `picture` | `IMAGE` 块，带图注（`caption_text()`）与 `alt_text` |
| `page_header` / `page_footer` | 段落块 + `semantic_role = header_footer`（页眉页脚保留类别） |
| `formula` / `code` | 段落块，原文保留并标 `formula_unverified`（语法正确不等于内容已核验） |
| `text` | 段落块，`formatting` / `hyperlink` 转为 `runs` |
| 无 `prov`（Word 无物理页） | `page = 1` 且 `page_kind = "logical"`，不冒充页码证据 |

判据是"转换结果是否提供结构对象"：只有 Markdown 的结果（或测试替身）继续走
`_build_markdown_provider_blocks()`；TextParser / PyMuPDF4LLM 的 Markdown 路径保持不变。

真实对照（`python-docx` 生成的含合并表格 DOCX，本机 docling 2.x 离线转换 0.1 s）：

| 项 | 走 Markdown（改动前） | 直映结构（改动后） |
| --- | --- | --- |
| 表格跨度 | 无（`table_cells` 不存在） | 表头 `Part` `col_span=2`（第 2 列标为 continuation），`Bolt` `row_span=2` |
| `merged_cells` | 无 | `col_span` + `row_span` 两条，带 origin |
| `header_rows` | 无 | 1 |
| 标题层级 | `heading_level=2`，无 `is_section_heading` | `heading_level=2` + `is_section_heading=true` |
| 行内格式（加粗 + 下划线） | 丢失 | `runs = [{"text": "WARNING: do not reuse", "bold": true, "underline": true}]` |
| Blocks 映射耗时 | 35 ms | **1 ms** |

验证：

```text
.\.venv\Scripts\python.exe -m unittest tests.test_docling_structure
Ran 9 tests / OK          # 含一次真实 docling 转换（本机可用，未跳过）

.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 980 tests / OK (skipped=6)
```

真实转换用例会在本机导入真实的 `docling*`，而 Windows 下 docling 加载器会把 `docling*` 模块
清空并从 ASCII junction 重新导入；那会破坏其它用例安装的模块替身（本轮确实先撞上
`test_provider_comparison_report` 的两个失败），因此该用例结束时会把自己引入的 `docling*` /
`docling_parse*` 模块移除，恢复进程原状。

## 7. Excel 合并表达统一（本次补齐）

Excel 是本轮之前唯一没有走共享网格的原生表格解析器：它把合并读成**范围标签列表**
（`merged_cells: ["A1:B1"]`），而 Word/PDF/Docling 读成**跨度记录**。同一件事两种形状，
`document-ir` 的表格 schema（`merged_cells: items: object`）只接受后者——Excel 的合并一旦
进入 IR 就是不合规数据，下游判断合并前得先问"这是哪个解析器出的表"。

现在 Excel 走同一份网格：

| 结构 | 改造前 | 改造后 |
| --- | --- | --- |
| 表内合并 | `merged_cells: ["A1:B1", ...]`（范围标签） | `table_cells`（跨度 + `continuation`/`origin`）+ `merged_cells`（跨度记录，字段与 Word 完全一致） |
| 表外合并（表格上方的标题行） | 混在同一个 `merged_cells` 里，读起来像本表的单元格 | `source_merged_ranges`（来源范围事实，不再是本表的单元格） |
| `cells` / Markdown 正文 | 各自按合并值填充一次 | 由 `grid.rows()` 派生，与 `table_cells` 同一张网格 |
| 超大表的元数据降级 | `cells` 降级为 `cells_preview`，`merged_cells` 仍报全表 | 不变：合并是全表事实，`cells_truncated` 已说明单元格是部分 |

落点：

| 位置 | 职责 |
| --- | --- |
| `parsers._excel_table_grid()` | 范围内合并 → 跨度；范围外合并 → `source_merged_ranges`（xlsx 与 xls 两条路径共用） |
| `tables.apply_merge_records()` | "只有扁平行 + 合并记录"的表（降级预览）在投影时补出跨度，使发布的单元格与发布的合并描述同一张网格 |
| `tables.merge_descriptors()` | 统一产出合并记录：网格跨度优先，网格装不下的声明合并仍然如实报出，同一条合并不重复 |
| `tables.cell_records()` | 只认记录是描述符；历史范围标签不会被当成长度未知的跨度 |

做减法：`api_payloads` 不再原样透传解析器的 `merged_cells`（那是"两种形状都能穿过"的入口），
改为只认记录、其余从网格派生；解析器侧那份手工维护的合并列表也随 `TableGrid` 一起消失。

真实样本（`D:\app\uploads`，用 `ExcelParser` 直接解析后再走 `_structured_tables()` 投影）：

| 样本 | 表数 | 带合并表 | 声明的合并 = 投影的合并 | `table_cells` |
| --- | ---: | ---: | --- | --- |
| 金鹿维修管理系统升级开发计划控制表-0421.xlsx | 7 | 6 | 4 / 8 / 7 / 21 / 15 / 2，逐表一致 | 有（未降级表） |
| 发展战略思考.xlsx | 2 | 1 | 13 = 13 | 有 |
| ARJ21主轮IPC.xls | 1 | 0 | 0 = 0 | — |

- 三个样本的标题行合并（`A1:H1`、`A6:H6`、`A43:H43`）都落在 `source_merged_ranges`，没有再混进
  表内合并；表内合并的续格数与跨度一致（例如 `A2:H5` 的 4 条 `col_span` 展开出 12 个续格，
  `A7:I11` 的 8 条 `row_span` 展开出 23 个续格）。
- 把 `max_metadata_cells` 压到 6 复现降级路径：7 张表仍各自报出全部合并（4/8/7/21/15/2/0），
  只有落在预览行内的跨度被补进网格——合并是来源事实，不因单元格被截断而消失。

验证：

```text
.\.venv\Scripts\python.exe -m unittest tests.test_tables tests.test_excel_parser tests.test_api_payloads tests.test_docx_table_grid tests.test_docling_structure
Ran 79 tests / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 1005 tests / OK (skipped=6)

.\.venv\Scripts\python.exe tools/excel_sample_quality.py --sample-dir D:/app/uploads
documents 3 | total_tables 10 | documents_with_issues 0
ARJ21主轮IPC.xls                          1 表 | 表内合并 0 | 来源范围合并 0
发展战略思考.xlsx                          2 表 | 表内合并 1 | 来源范围合并 0
金鹿维修管理系统升级开发计划控制表-0421.xlsx  7 表（6 张有标题） | 表内合并 6 | 来源范围合并 5
```

样本质量工具因此拆成两个计数：`merged_cell_tables`（本表网格里的合并）与
`source_merged_range_tables`（来源范围里的合并，例如表格上方的标题行），不再把两者混为一谈。

新增用例（`tests/test_tables.py`）：声明合并在网格上落地、网格装不下的合并仍被报出、范围标签
不当成描述符、声明的合并不覆盖已记录的跨度、同一条合并不重复。

## 8. 产物条目的 `kind` 语义迁移（本次补齐）

`DocumentArtifactItem.kind` 原来存的是语义角色（`kind=semantic_role`），内容形态另存在
`provenance["block_type"]`，`metadata["item_kind"]` 又抄了一份角色——同一个"它是什么"有三个字段，
下游只能写三重兜底（`block_type == "table" or kind == "table" or semantic_role == "table"`）。
一份含列表的警告因此无法表达"内容是段落、用途是警告"。

现在一个字段一个含义：

| 字段 | 含义 |
| --- | --- |
| `item.kind` | 内容形态（`BlockType`：`title` / `paragraph` / `table` / `image`） |
| `item.semantic_role` | 用途语义（`warning` / `body_section` / `lep_entry` / `parse_artifact` …） |
| `item.provenance` | 来源与顺序（`parser`、`position`），不再复制形态与角色 |
| `item.metadata["kind"]` | 块元数据原样保留，与条目 `kind` 同形 |

做减法（删除的重复字段与分支）：

| 对象 | 处理 |
| --- | --- |
| `provenance["block_type"]` | 与新的 `kind` 同值 → 删除 |
| `metadata["item_kind"]` | 与 `semantic_role` 同值且全仓无读取方 → 删除 |
| 知识单元元数据 `block_type` | 与 `kind` 同值 → 删除，知识单元只留 `kind` + 自身 `semantic_role` |
| `pipelines._item_block_type()`、`pipelines._knowledge_unit_type()`、`runtime._runtime_unit_type()` | 三重兜底改为只读 `kind`（形态），角色只参与角色判断 |
| 角色取值链里的 `or item.kind` | 删除：形态不能当角色用（否则没有角色的表格块会得到角色 `table`） |
| `metadata["summary"]["item_kinds"]` | 名为 kind、实为角色计数 → 改名 `semantic_roles`（全仓无其它读取方） |

顺带修正（原语义把问题掩盖了，不是搬家的副作用）：`TableStructureStage` 以前按角色选表
（`item.kind != "table"` 实际是角色判断），`semantic_role` 更窄的表——Word 有效页清单
（`lep_entry`）、Excel 目录表——不会被表格结构增强；现在按内容形态选择，这些表同样渲染并进入
知识单元。

验证：

```text
.\.venv\Scripts\python.exe -m unittest tests.test_quality tests.test_runtime tests.test_docx_manual_structure tests.test_audit_placeholders
Ran 85 tests / OK

.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 1006 tests / OK (skipped=6)
```

用例：

| 场景 | 结果 |
| --- | --- |
| `semantic_role=lep_entry` 的表格仍按表处理（单元类型 `table`、被 table-structure 增强） | 通过（`test_a_narrower_semantic_role_does_not_hide_the_content_form`） |
| 条目 `kind` 是块形态、`provenance` 无 `block_type`、`metadata` 无 `item_kind` | 通过（`test_pipeline_registry_warmup_and_artifact_chunking_are_active`） |
| 审计占位条目表达为"形态 `paragraph` + 角色 `parse_artifact`" | 通过（`tests/test_quality.py`） |

### 8.1 记录一个既有竞态（待办 5 处理，本次未改）

`publish_merged_parent_run()` 用 `seal_run()` 校验"分块引用的块都属于本次运行"，偶发失败：

```text
SealError: chunks reference blocks that are not part of this run:
  doc-pdf-api-parts:merged:doc-pdf-api-parts-part-2:doc-pdf-api-parts-part-2-p-1, …-title
```

在**未修改**的 `dcecacc` 上同样复现（本机 30 次单测 1 次失败；带本次改动 30 次 3 次失败，
两者同量级），机制在 `_merged_partition_artifacts()`：它先按子任务读 `get_blocks()`、再读
`get_chunks()`，两次读取之间子任务（inline worker）可能改写自己的产物，于是合并出的分块引用了
新块 id、而合并块集合来自旧快照；`block_id_map` 未命中的 id 被原样保留，落库后与块集合不一致。
这是"重跑合并"的读一致性问题（待办 5 的"失败重跑不能覆盖旧正式结果 / 重复提交不产生错版"），
本轮只记录现象与机制，修复放在待办 5。

## 9. 区域素材落盘（本次补齐：从"可重建定位"到"已保存素材"）

待办 1 的口径是"图片、公式、脚注遵循**先保留，后理解**：在本轮处理到的内容中保存原始素材或可重建
定位"。此前只做到了后半句：表块与图片块带 `source_region`（页号、点坐标、原点、旋转、页尺寸、
`crop_fraction`），消费方可以自己裁剪，但**素材本身没有落盘**。

现在补上前半句：`src/parsecore/assets.py` 按区域渲染 PNG 并登记摘要。

| 项 | 行为 |
| --- | --- |
| 开关 | `[runtime] region_assets = true`（默认 **false**）；`region_asset_resolution`（默认 144 dpi）。未开启时不渲染、不写文件、块元数据不变 |
| 存储边界 | 只写 `local://` 对象存储（与上传、导出一致的边界），路径 `<store>/regions/<doc_id>/<block_id>.png`；非本地存储时记录 `region_assets_skipped` 并原样返回块 |
| 渲染 | 优先 PyMuPDF（`get_pixmap(clip=...)`），无 fitz 时回退 pdfplumber `crop().to_image()` |
| 区域框 | 超出页面的框**按页裁剪**（不是补白）：素材是页面上真实存在的那一部分，描述里记的就是裁剪后的框 |
| 登记 | 块元数据 `asset` = `{schema_version, kind, page_number, bbox, crop_fraction, page_width/page_height, rotation_degrees, path, sha256, bytes, width, height, resolution, verified: false}`；`region_assets_manifest()` 给出整份摘要（条数、总字节、逐条摘要） |
| 核验状态 | `verified: false` 是刻意的：素材已保存，但"这张图是否等于来源区域"没有做视觉核对，不冒充已验证 |
| 失败语义 | 素材是可选产物：渲染/写盘失败只记事件（`region_assets_failed` / `region_assets_skipped`），不让解析失败，定位信息照常发布 |

真实样本（`36d65cd6b61346e28e97dbaf829646de.pdf`，`dual_channel`，同一台机器）：

| | 关闭（默认） | 开启 |
| --- | --- | --- |
| 解析耗时 | 26.4 s | 42.0 s（+15.6 s，+59%） |
| blocks / tables / figures | 1866 / 44 / 411 | 1866 / 44 / 411（不变） |
| 素材 | 0 | **455**（`pdf_figure` 411 + `pdf_table` 44），15,025,614 字节（约 33 KB/张） |
| 抽样核对 | — | 第 3 页 `pdf_figure` 412×96、25,438 字节，`sha256` 与磁盘文件一致 |

即：一份 297 页目录图册的素材约 15 MB、约 16 秒，块与表格指标完全不变；这也是默认关闭的理由——
不看不用的图不必先渲染。

用例（[tests/test_region_assets.py](../tests/test_region_assets.py)，新增 6 项）：表/图区域各写出一张
可校验的 PNG（摘要、字节数、尺寸、来源框与 `crop_fraction` 都在描述里，无区域的块不被改动）、
来源缺失与倒置框不写任何文件、超出页面的框按页裁剪、`local://` 之外的存储不写、运行时开关关闭时
一次渲染都不发生、没有区域的一次完整解析不受影响。

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 1036 tests / OK (skipped=6)
```

## 10. 未完成（同一待办）

1. **Markdown 表源的相互扩展**：Excel 已统一到跨度表达；由 Markdown 重建的表格
   （`_markdown_table_cells()`，TextParser / PyMuPDF4LLM / Docling 的降级路径）本身没有合并
   信息，只能给出扁平行，不会有 `table_cells`——这是格式能力差异，不是待补的分支。
2. **素材进入产物族（assets）**：文件与摘要已就绪，但 run store 的 `assets` 产物族（staging 面 +
   manifest 校验）仍未实现，因此 `assets-only` 这类要求 assets 的 profile 还不能封存；当前素材只
   通过块元数据 `asset` 引用，不参与发布判定。
3. **Word/Excel 的区域素材**：`source_region` 目前只有 PDF 侧产生；Word 用段落/表格标识、Excel 用
   工作表与单元格范围，都没有像素框，因此本功能对它们不适用（也没有伪造框）。
4. **Docling 的图片/公式落盘**：Docling 直映路径的图片仍只保留图注与来源，公式只保留转写文本并
   标未核验。
5. **PDF 后端的本机限制**：本机 `docling-parse` 缺 `pdf_resources/glyphs/standard/additional.dat`，
   真实 PDF 转换直接失败（`DocumentLoadError`），因此 Docling 直映只在 DOCX 上做了真实样本
   验证；PDF 侧沿用 `pdf-text` 路径不受影响。

## 11. 回滚

- 代码回滚：`git revert` 本次提交即可；`tables.py` 为新增模块，回滚后无引用。
- 行为回滚：如需恢复"按 `cells` 字符串自行重建网格"，可让消费者直接读 `metadata["cells"]`；
  但那样会重新引入"同一张表在 Reader/记录/IR 中网格不一致"的问题。
- 执行路径的严重度修正不建议回滚：回滚会让一份正常解析的长文档因为 1 页签名页告警而被判为
  执行失败，并使质量回归门禁无法产生结论。
