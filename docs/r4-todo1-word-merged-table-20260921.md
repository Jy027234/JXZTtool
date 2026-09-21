# 待办 1 交付报告：Word 合并表格结构保真 + Docling 直映结构

日期：2026-09-21（同日续写：补 Docling 直映结构）
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 1
状态：Word 链路与 Docling 直映均已实施；剩余见第 6 节

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
| [src/parsecore/tables.py](../src/parsecore/tables.py)（新增） | 唯一的表格网格约定：`GridCell` / `TableGrid`、`grid_from_metadata()`、`grid_from_rows()`、`grid_from_cells()`、`resolve_table_grid()`、`rows_from_table_payload()`、`render_markdown()` / `render_tsv()` |
| [src/parsecore/parsers.py](../src/parsecore/parsers.py) | `DocxParser` 读取 `w:gridSpan` / `w:vMerge` / 行内 `r:` 链接（`word/_rels/document.xml.rels`），输出 `table_cells`（起始行列、`row_span`/`col_span`、`continuation`/`origin`、行内 `runs`）、`merged_cells`、`header_values`、`source_row_col_counts`；`cells` 与 Markdown 由同一网格派生 |
| [src/parsecore/api_payloads.py](../src/parsecore/api_payloads.py) | 结构化表格优先读 `table_cells`，单元格带跨度与合并标记；记录表头用解析器给出的 `header_values` |
| [src/parsecore/ir.py](../src/parsecore/ir.py) | IR 表格携带同一份 `cells`，并派生出 `merged_cells` / `header_values`，Reader 因此也能显示合并 |
| [src/parsecore/pipelines.py](../src/parsecore/pipelines.py) | 表格增强、知识单元文本与摘要走同一个网格；合并关系进入知识单元 `table_summary` |
| [src/parsecore/payload_schemas.py](../src/parsecore/payload_schemas.py) | `document-ir` 的表格增加可选 `merged_cells` / `header_values` |
| [tests/test_tables.py](../tests/test_tables.py)（新增） | 16 项：网格读取优先级、合并值填充、表头展开、Markdown/TSV 转义 |
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

## 7. 未完成（同一待办）

1. **`DocumentArtifactItem.kind` 语义迁移**：目前 `kind` 仍是语义角色、内容形态由
   `provenance["block_type"]` 表达。迁移会牵动知识单元类型判定，放在表格链路稳定后做。
2. **Excel / Markdown 逐样本扩展**：Excel 现有 `cells` + `merged_cells` 继续复用，未改；
   其原生 `merged_cells` 与 Word/PDF/Docling 的跨度表达尚未统一到同一形状。
3. **PDF 来源定位**：表块与图片块现在带 `source_region`（页号、点坐标、原点、页面旋转、页尺寸
   与 `crop_fraction`），见[待办 3 报告第 7 节](r4-todo3-pdf-catalog-table-20260921.md)；
   Word 侧仍用块/单元格标识，不填物理页码，Excel 的工作表与单元格范围沿用原有字段。
4. **Docling 的图片/公式落盘**：图片目前只保留图注与来源，未保存原图；公式只保留转写文本并标
   未核验，没有做区域图保留。
5. **PDF 后端的本机限制**：本机 `docling-parse` 缺 `pdf_resources/glyphs/standard/additional.dat`，
   真实 PDF 转换直接失败（`DocumentLoadError`），因此 Docling 直映只在 DOCX 上做了真实样本
   验证；PDF 侧沿用 `pdf-text` 路径不受影响。

## 8. 回滚

- 代码回滚：`git revert` 本次提交即可；`tables.py` 为新增模块，回滚后无引用。
- 行为回滚：如需恢复"按 `cells` 字符串自行重建网格"，可让消费者直接读 `metadata["cells"]`；
  但那样会重新引入"同一张表在 Reader/记录/IR 中网格不一致"的问题。
- 执行路径的严重度修正不建议回滚：回滚会让一份正常解析的长文档因为 1 页签名页告警而被判为
  执行失败，并使质量回归门禁无法产生结论。
