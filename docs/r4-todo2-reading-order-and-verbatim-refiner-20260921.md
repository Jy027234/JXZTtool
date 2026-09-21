# 待办 2（首批）交付报告：阅读顺序按区域排列，段落增强保留原文

日期：2026-09-21
待办来源：[docs/parsecore-reliability-mineru-todo-20260920.md](parsecore-reliability-mineru-todo-20260920.md) 待办 2
状态：两项主体已实施（模型只给关系 + 双栏按区域排布）；后续见第 6 节

## 1. 这一项解决什么

本轮修掉两处"模型/裁剪替程序改内容"的地方：

1. **段落增强让模型重写正文**。旧协议把整段原文交给模型，要求它返回"切分后的字符串数组"，再用
   85% 长度保护判断是否接受。R2 探针已经证明这不够：`AB-123 torque 25` 被接受为
   `AB-124 torque 95`——数字和料号被改写，长度只差几个字符，保护根本没触发。
2. **双栏页面按固定竖带裁剪**。旧 `_extract_text_by_columns()` 把页面按页宽等分成两刀，
   再对每条带 `crop().extract_text()`。跨栏的行被从中间切开，真实样本（飞行运行手册 R2
   第 17 页，页宽 595"有效页清单"）的结果是：

   ```text
   修改前（竖带裁剪）                          修改后（区域排列）
   飞行                                       飞行运行手册 0
   0.5 有效页清单                             0.5 有效页清单
   *-状态符号：N-新、R-修改、A-                *-状态符号：N-新、R-修改、A-增加、D-取消
   编号 章节 * 日期                            编号 章节 * 日期 编号 章节 * 日期
   封面 N 2020/10/23                          封面 N 2020/10/23
   0.1-1 第 0 章 N 2020/10/23                  0.1-1 第 0 章 N 2020/10/23 0.2-1 第 0 章 R 2020/11/09
   ...                                        ...
   行运行手册 0                                （不再出现）
   -增加、D-取消
   3
   3 0.2-1 第 0 章 R 2020/11/09
   ```

   标题被拆成"飞行"+"行运行手册"，脚注被截断，右侧行与左侧行脱节并混入零散片段。

## 2. 交付物

| 文件 | 变更 |
| --- | --- |
| [src/parsecore/llm.py](../src/parsecore/llm.py) | 新协议：程序按原文自带边界编号切分片段，模型只回 `{"groups": [[1,2],[3]], "order": [0,1]}`；结果由原文片段拼装，含未知/越界/丢段/重复/空组/顺序非法校验；`rejections` / `last_rejection` 记录拒绝原因 |
| [src/parsecore/parsers.py](../src/parsecore/parsers.py) | `_extract_text_by_columns()` 改为按行判定区域（跨栏行 / 左栏 / 右栏）后排序；新增 `_PageTextLine`、`_page_text_lines()`、`_line_spans_columns()`、`_line_column()`、`_order_page_lines_by_region()`；删除整页裁剪分支 |
| [tests/test_llm_boundary_refiner.py](../tests/test_llm_boundary_refiner.py)（新增） | 29 项：逐字保真、协议校验、拒绝原因、请求独立预算、缓存、解析器接线 |
| [tests/test_pdf_multicolumn_reading_order.py](../tests/test_pdf_multicolumn_reading_order.py)（新增） | 8 项：跨栏标题不被切开、整行表格行保持位置、页中标题分段、排除表格区域、单栏与无行几何的回退 |
| [tools/_a4_real_pdf.py](../tools/_a4_real_pdf.py) | 冒烟脚本改读按请求的 refiner 状态并打印拒绝原因 |

## 3. 段落增强：模型只能移动边界

| 环节 | 现在的规则 |
| --- | --- |
| 片段切分 | 只取原文自带边界：换行、句末标点后的编号/字母项、`NOTE` / `WARNING` / `CAUTION` / `注：` / `步骤 N`。因此模型不可能切在数字、料号或单位中间 |
| 发给模型 | `id| 片段文本` 列表 + 固定 JSON 结构说明，明确"不要重写任何文字" |
| 接受前校验 | 编号必须 1..N 各出现一次（未知、越界、丢段、重复、空组全部拒绝）；`order` 必须是组序号的排列；最终文本按片段多重集复核 |
| 结果拼装 | 由原文片段 `"".join(...)` 生成，只做空白规整；不做任何模型文本回填 |
| 失败处理 | 保留原段落，并按原因计数（`invalid_response` / `unknown_fragment` / `duplicate_fragment` / `missing_fragment` / `empty_group` / `invalid_order` / `no_change` / `no_legal_boundary` / `too_many_fragments` / `call_budget_exhausted` / `request_failed`） |
| 成本 | 没有合法边界的段落（单句、含料号的短句）**不发起调用**，简单文档保持快速路径；调用上限、重试与并发仍沿用现有配置，状态按请求隔离 |

被删除的旧路径：字符串数组协议 `_parse_json_string_array()`、85% 长度放行判断、以及"接受模型重写文本"的整条分支。

## 4. 双栏页面：先分区域，再定顺序

`_extract_text_by_columns()` 现在按行工作（pdfplumber 的 `extract_text_lines()`），判定规则：

| 行类型 | 判定 | 位置 |
| --- | --- | --- |
| 跨栏行 | 行宽 ≥ 55% 页宽，或同时覆盖页宽 48% 之前与 52% 之后 | 属页面流：先结束当前栏块，再按 y 位置输出 |
| 左栏行 | `x1 ≤ 48%` 页宽（否则按中心点） | 累积到左栏块 |
| 右栏行 | `x0 ≥ 52%` 页宽 | 累积到右栏块 |
| 表格 / 图片 | 由既有 `bboxes` 排除后不再参与 | 保持原有跳过语义 |

输出顺序 = 页面流行 → 左栏块 → 右栏块 → 下一条跨栏行 → …… 同一栏的文字不会被邻栏打断，跨栏的标题、重复表头和整行数据不会被切开。段落合并仍留在原有的下游切分里，本次不新增合并启发式。

## 5. 验证

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
Ran 932 tests / OK (skipped=6)

.\.venv\Scripts\python.exe -m unittest tests.test_llm_boundary_refiner tests.test_pdf_multicolumn_reading_order
Ran 37 tests / OK
```

### 5.1 真实样本前后对比（飞行运行手册 R2 第 17–21 页）

样本：`D:\app\uploads\飞行运行手册第二版R2(2021-04-06）.pdf` 第 17–21 页（`_estimate_column_count`
判定为双栏，第 17–21 页连续 5 页双栏），选项 `layout_reading_order = true`，同一进程内先跑旧竖带
实现再跑新实现，各 3 次：

| 指标 | 修改前（竖带裁剪） | 修改后（区域排列） |
| --- | --- | --- |
| 页文本抽取耗时 | 178 ms/页 | **6 ms/页** |
| 端到端 5 页解析 | 2.8 / 3.8 / 3.7 s | 3.8 / 3.7 / 3.7 s |
| 拆断的标题"飞行"独立块 | 有 | 无 |
| 脚注被截断为 `-增加、D-取消` | 有 | 无 |
| 第 17 页正文顺序 | 左带（缺右值）+ 右带（零散片段） | 页面流 + 完整行，标题/脚注完整 |
| 块数 / 中位长度 | 16 / 25.5 | 18 / 31.0 |

抽取本身快了约 30 倍：旧实现每页要做两次 `crop().extract_text()`（各自重新解析页面对象），新实现
复用已解析字符，只做一次行几何读取。端到端耗时差异在重复测量里落在噪声范围内（首轮 2.8 s 是
冷启动，随后两者都是 3.7–3.8 s），无未解释的性能退化。

**一处需要说明的指标变化**：该 5 页样本上 `very_short_ratio` 从 0.0000 升到 0.3333。原因是块粒度
变细——同样的文字现在拆成"飞行运行手册 0"、"0.5 有效页清单"、"*-状态符号…"、"修订日期…"等独立
块，而不是被塞进两个栏块里，其中 4 个短块被判为 very short。内容没有丢失或增加。样本只有 5 页且
都是清单页，不足以据此判断整体指标趋势；正规样本的趋势仍以回归门禁为准（该 PDF 不在现有
suite 中）。

### 5.2 回归门禁

`tools/self_check.py --profile fast` 的结论与待办 1 交付时一致：`unit_tests 932 passed`、
`runtime_describe` / `payload_contracts` / `corpus_manifest 3/3` / `provider_comparison_suite`
通过，`regression_suite` 仍因既有漂移退出 1（`numeric_heavy_total +38`、`suspected_header_footer_total +39`、
`total_blocks +6.88%`、`heading_body_binding_rate -0.0909`）。这些漂移在改动前就存在，阈值未放宽；
双栏路径默认关闭（`layout_reading_order` 非默认选项），因此对默认配置的解析结果没有影响。

## 6. 未完成（同一待办）

1. **栏内段落合并**：本次只改顺序，不新增合并规则；栏内多行仍按原样交给下游切分。
2. **区域类型细分**：目前只区分"跨栏 / 左栏 / 右栏"。图注、脚注、注释块的独立类型与栏内表格
   （无框线表格被当作多行文本）尚未细分。
3. **视觉证据**：待办 2 提到的"局部图像 + 片段编号"输入未接入；当前客户端仍是纯文本接口，不改
   装成能恢复已丢失布局。
4. **更多样本**：本轮只验证了飞行运行手册第 17–21 页（清单页）与 36d65cd6 第 17 页；正文型双栏
   页面（连续段落跨栏）尚未取到样本，`_order_page_lines_by_region()` 的该分支只有合成用例覆盖。

## 7. 回滚

- 代码回滚：`git revert` 本次提交。
- 行为回滚：refiner 可用 `boundary_refiner=None` 关闭（`[providers.llm] enabled = false` 时
  `build_llm_client` 返回 `None`，bootstrap 不注入）；双栏排序只在 `layout_reading_order = true`
  时生效，默认关闭。
