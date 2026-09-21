# 待办 3（首批）交付报告：一种实际目录表格的列归属与跨页关系

日期：2026-09-21
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 3
状态：选定样本的表头归属、跨页关系与网格校验已实施；其余见第 6 节

## 1. 选定样本与复现

样本：`D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf`（A320 主轮 CMM，297 页，44 张表，也是快速回归主样本）。
该文档的分解图页使用"宽目录表"版式：一张表横向排满部件编号，行是拆装工序条目，列是件号与图号。

复现出的三个具体问题：

| # | 复现 | 证据（同一页、同一单元格） |
| --- | --- | --- |
| 1 | **表头列名整体倒序**：窄列里的列标题旋转 90° 排版，整页按左到右读时字符顺序被反转，列的身份丢失 | 第 85 页第 2 列表头 `REVOC EHT )a()1(.A.6 FO WERCS`，实际应为 `SCREW OF THE COVER 6.A.(1)(a)` |
| 2 | **跨页关系缺失**：同一张表分在连续多页上（每页换一批列、行标签重复），但每页都只是一张互不相关的表 | 44 个表块没有任何 `continuation_*` 字段；第 85/87 页与第 89/91/93 页是两组继续关系 |
| 3 | **记录字段名不可用**：记录以首行做字段名，于是字段名就是倒序乱码 | 修改前第 85 页的字段名形如 `REVOC EHT )a()1(.A.6 FO WERCS` |

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/parsers.py](../src/parsecore/parsers.py) | `_pdf_char_reading_frame()` / `_text_along_char_direction()`：从字符矩阵推导阅读方向并按该方向拼单元格文字；`_page_has_rotated_text()` / `_chars_in_bbox()` / `_pdf_table_cells()`：只有页面确实存在旋转文字时才逐格重读，普通页面直接沿用 `Table.extract()`；`_table_continuation_kind()` / `_link_table_continuations()`：判定并按既有契约写入跨页关系；`_TABLE_CONTINUATION_MAX_PAGE_GAP` |
| [src/parsecore/validation.py](../src/parsecore/validation.py) | 新规则 `table_grid_invalid`（`warning`）：单元格越出 `rows`/`cols` 或在同一格位重复赋值；`_table_grid_defects()` |
| [tests/test_pdf_rotated_table_text.py](../tests/test_pdf_rotated_table_text.py)（新增） | 6 项：旋转文字按自身方向读取、间距与词距、正立单元格不受影响 |
| [tests/test_pdf_table_continuation.py](../tests/test_pdf_table_continuation.py)（新增） | 14 项：列继续 / 行继续 / 无关表 / 隔一页继续 / 长间隔不连 / 分组页范围 / 中间部分双向指针 |
| [tests/test_validation.py](../tests/test_validation.py) | 4 项：网格越界、格位重叠、正常网格不报、没有格位的表不评判 |

## 3. 做了什么

### 3.1 文字归属：按字符自己的方向读单元格

字符矩阵 `(a, b, c, d, e, f)` 是文本空间到用户空间的映射（y 向上），因此：

- 字前进方向 = `(a, -b)`（换到 pdfplumber 的 y 向下坐标）；
- 下一行方向 = `(-c, d)`。

据此把单元格内的字符投影到"沿行/跨行"两个轴上，按跨行轴分行、按沿行轴排序，并按字符实际宽度判断词间距。**正立单元格返回 `None`，仍用 pdfplumber 的结果**，所以只有真正旋转的格子会被重读。

性能用页面级一次判定挡住：`_page_has_rotated_text()` 扫一遍字符矩阵，页面没有旋转文字就直接返回基础提取。实测（20×29 表）：

| 场景 | 基础提取 | 加旋转重读 | 页面判定后 |
| --- | --- | --- | --- |
| 第 85 页（含旋转表头） | 55.7 ms | 93.6 ms | 95.1 ms |
| 第 82 页（3×2，无旋转） | 1.0 ms | 2.1 ms | 1.3 ms |
| 第 269 页（2×5，无旋转） | 4.5 ms | 7.2 ms | 4.5 ms |

整份文档解析耗时：改动前 29.1 s / 26–29 s，改动后 26.0 s（同一台机器、同一选项），未出现性能退化。

### 3.2 跨页关系：复用既有契约，不新增表示

`_table_continuation_kind()` 用两个可判定的信号分类：

| 关系 | 判定 | 含义 |
| --- | --- | --- |
| `column_continuation` | 行标签（首列，去掉表头行）完全相同、行数相同、表头行不同 | 同一批条目，下一页给出**更多列**（件号/图号） |
| `row_continuation` | 表头行完全相同、行标签不同 | 同一张表继续给出**更多行** |
| 不关联 | 其余 | 是另一张表 |

写入的字段全部是既有契约字段，Reader/IR/记录已经在读：`continuation_group_id`、`table_group_id`、`is_continuation`、`continues_from`、`continues_to`、`continuation_kind`、`page_span`。物理表仍是每页一张（阅读仍按页给网格），只是每张表现在说明了自己属于哪一组、接在谁后面、怎么接。

样本实测：识别出 **12 组** 跨页表——8 组 `column_continuation`（第 85–167 页范围内的宽目录表）与 4 组 `row_continuation`（第 269–297 页的长表）。组内 `page_span` 覆盖整组，例如 `probe-cont:tblgrp:p89 → [89, 93]`。

### 3.3 校验：网格不越界、不非法重叠

新规则 `table_grid_invalid` 在发布校验阶段检查每个表块的结构化单元格：格位越出声明的 `rows`/`cols`，或两个单元格占用同一格位 → `warning`，并把 `table_grid_defects` 计入指标。只输出扁平字符串行的解析器不在此列——它们没有可判错的格位。

## 4. 验证

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 956 tests / OK (skipped=6)

.\.venv\Scripts\python.exe -m unittest tests.test_pdf_rotated_table_text tests.test_pdf_table_continuation tests.test_validation
Ran 46 tests / OK
```

真实样本端到端（第 85 页表块 → 结构化投影 → 记录）：

| 项 | 修改前 | 修改后 |
| --- | --- | --- |
| 表头单元格 | `REVOC EHT )a()1(.A.6 FO WERCS` | `SCREW OF THE COVER 6.A.(1)(a)` |
| `continuation_group_id` / `page_span` | 无 | `probe-records:tblgrp:p85` / `[85, 87]` |
| 记录字段名 | 倒序乱码 | `SCREW OF THE COVER 6.A.(1)(a)`、`LOCKING RING "C135" 6.A.(2)` … |
| 表块数 / 表指标 | 44；`table_ready=1.0000 table_cells=44` | 44；`table_ready=1.0000 table_cells=44`（未变） |

回归基线（`tools/regression_baseline.py --config parsecore.toml check --baseline var/regression/baseline.json`）与改动前**完全一致**：

```text
blocks=2221 (baseline 2078) pages=297 toc_recog=0.1745 (0.1687) chapter_cov=0.1273 (0.0426)
table_ready=1.0000 table_cells=44 heading_bind=0.9091 (1.0000)
REGRESSION DETECTED: numeric_heavy_total +38 / suspected_header_footer_total +39 /
                     total_blocks +6.88% / heading_body_binding_rate -0.0909
```

也就是说：本轮改动修好的是**单元格内容与跨页关系**，而既有门禁的这四项漂移是文档级的块统计，不随本次改动变化；阈值与基线未做任何放宽，仍如实退出 1。

## 5. 与待办 1 约定的一致性

PDF 表仍走同一份产物约定：`metadata["cells"]`（扁平行）→ `parsecore.tables.grid_from_metadata()` → 结构化单元格（`table_cells`）→ Reader / 记录 / IR。本次没有新增并列的表格表示或状态：跨页关系用的是 IR/结构化投影早已读取的字段，网格校验用的是同一批格位。

## 6. 本批未完成

1. **跨页合并成一张逻辑表**：现在保留每页物理表 + 组关系，没有把 4 页拼成一张 19×112 的大表。合并需要在结构化层按 `continuation_group_id` 拼接列，并决定记录以哪一页为准；本轮先保证关系可判定、可追溯。
2. **错列的自动推断**：只修了"列名读不出来"这一类。列边界本身错位（文字被分到相邻列）需要按线条/对齐重算网格，样本中未复现出该类错误，故未动。
3. **单位、脚注、多级表头**：样本表头是"描述 + 图号"两行式，没有独立单位行/脚注；未实现专门的表头层级与脚注归属。
4. **困难区域的模型判断**：方案允许"只有困难区域才让模型判断列边界或片段归属"，本轮未启用，也没有新增视觉输入。
5. **未参与调试的验收页**：本轮验收集中在第 82–103、145–167、269–297 页；其余表页只做了整体指标对照，未逐页检查。
6. **续行文本重复**：`column_continuation` 下同一行标签会在每页各出现一次（这是版式事实），消费方需要结合 `continuation_group_id` 与页码判断，文档未在记录层做去重。

## 7. 回滚

- 代码回滚：`git revert` 本次提交；旋转读取与跨页关联都是纯增量函数，回滚后回到"表头倒序、表间无关系"的旧行为。
- 行为回滚：不需要开关——`_page_has_rotated_text()` 为假时完全走原路径；跨页关联只写元数据，不改变块内容与顺序。
