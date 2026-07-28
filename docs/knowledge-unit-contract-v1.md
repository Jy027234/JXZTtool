# ParseCore KnowledgeUnit 通用结构契约 v1

> 契约版本：`2026-07-knowledge-unit-v1`<br>
> IR 版本：`2026-07-ir`<br>
> Reader 版本：`2026-07-reader`<br>
> Coverage 版本：`2026-07-coverage`
> Diff 版本：`2026-07-knowledge-unit-diff-v1`

## 1. 能力边界

ParseCore 负责把 PDF、Office、图片和文本转换为可复用的解析事实：页面、块、表格、图、章节层级、通用语义角色、KnowledgeUnit、来源跨度、连续关系、处理覆盖和解析质量信号。

ParseCore 不负责法规义务真假判断、公司事实认定、领域 ontology、Wiki Page/Claim 编译与审批、宿主权限、合规裁决或默认问答路由。这些能力必须留在宿主产品的领域模块中。

## 2. 源完整性

`ir`、`reader` 和 `coverage` 均输出 `source_integrity`：

- `status`：`verified` 或显式退化状态；
- `hash_algorithm` 与 `source_hash`：当前实现为 SHA-256；
- `source_size_bytes`、`source_mtime_ns`；
- `parser_schema_version`；
- `parser_config_fingerprint`；
- `provider_fingerprint`。

运行时在任务创建时流式计算源文件 SHA-256，并把结果随 ParseJob 和 index manifest 持久化。无法读取源文件时必须输出 `missing`，不得使用正文 hash 冒充源文件 hash。

## 3. 稳定指纹

IR 对 block、table、figure、section 和 KnowledgeUnit 输出稳定指纹：

| 对象 | 字段 | 主要输入 |
| --- | --- | --- |
| Block | `stable_block_id`、`block_fingerprint` | 文档、源版本、角色、规范化文本、页跨度、bbox、阅读顺序 |
| Table | `stable_table_id`、`table_fingerprint` | 源版本、caption、cells、页跨度、bbox |
| Figure | `stable_figure_id`、`figure_fingerprint` | 源版本、类型、caption、alt text、页跨度、bbox |
| Section | `section_id` | 父章节、编号、标题、层级、来源 block 与页跨度 |
| KnowledgeUnit | `stable_unit_id`、`unit_fingerprint` | 源版本、内容指纹、结构指纹、来源对象指纹 |

KnowledgeUnit 同时输出：

- `content_fingerprint`：规范化文本、unit type 和 semantic role；
- `structure_fingerprint`：章节路径、编号、类型、角色和来源位置；
- `source_version_key`：源文件版本标识。

随机顺序号和临时 block ID 不作为 KnowledgeUnit 稳定性的唯一依据。

## 4. 层级和通用角色

KnowledgeUnit 固定输出：

- `section_id`、`parent_section_id`；
- `section_no`、`section_title`、`section_level`、`title_path`；
- `list_level`、`list_marker`、`list_parent_unit_id`。

首版通用结构角色包含：`title`、`body_section`、`appendix`、`clause`、`definition`、`list_item`、`procedure`、`procedure_step`、`note`、`warning`、`caution`、`table` 和 `image`/`figure_caption`。角色只描述结构，不表达法规强制性、适用性或合规结论。

## 5. 来源跨度

Block、Table、Figure 和 KnowledgeUnit 均输出 `source_span`：

```json
{
  "page_start": 1,
  "page_end": 1,
  "source_block_ids": ["..."],
  "source_table_ids": [],
  "bbox": [10, 20, 100, 40],
  "bbox_page": 1,
  "precision": "region",
  "degraded_reason": ""
}
```

无 bbox 或对象跨页时，`precision=page`、`bbox=null`，并显式给出 `degraded_reason`。宿主不得把页级定位伪装为区域级定位。

## 6. 连续关系

KnowledgeUnit 的 `continuity` 固定包含：

- `kind`：`none`、`spans_pages` 或 `continues`；
- `group_id`；
- `continues_from_unit_id`、`continues_to_unit_id`；
- `reason`、`confidence`。

关系来源包括显式 continuation、同组相邻 unit、分页断句、跨页单元，以及相邻页面共享表格标识。声明了 continuation 但无法解析时，coverage 输出质量信号，不静默猜测。

## 7. Coverage 全量账本

`coverage.units` 枚举所有 KnowledgeUnit，而不是只枚举成功生成 chunk 的对象。每个 unit 固定输出：

- `processing_status`：`pending / processed / skipped / failed / reviewed`；
- `processing_reason`；
- chunk、embedding、coverage 和缺失原因；
- unit 级质量信号。

`coverage.summary` 输出 `total_unit_count`、`accounted_unit_count`、`unaccounted_unit_count` 和各处理状态计数。`unaccounted_unit_count` 非零时，宿主不得把抽取运行标记为完整成功。

## 8. ParseRun diff

`knowledge_unit_diff` 为新旧 ParseRun 输出一对一映射：

- `unchanged`：unit 指纹相同，或跨源版本时内容和结构均相同；
- `relocated`：内容相同但结构位置变化；
- `changed`：结构锚点相同但内容变化；
- `added`、`removed`；
- `unknown`：重复内容或结构导致匹配歧义，必须人工或宿主规则复核。

首次解析输出 baseline diff，所有当前 unit 均为 `added`。算法版本、前后 ParseRun、计数、置信度和原因都必须随结果保存。

## 9. 结构质量信号

首版固定信号包含：

- `structure_unit_identity_invalid`；
- `structure_section_parent_missing`；
- `structure_title_path_missing`；
- `structure_heading_level_jump`；
- `structure_section_number_jump`；
- `structure_list_parent_missing`；
- `structure_semantic_unit_empty`；
- `structure_cross_page_continuity_missing`；
- `structure_cross_page_table_break`。

这些信号只说明解析风险，不能替代宿主审批或法规判断。

## 10. 兼容与发布

- 契约新增采用 schema version 演进；旧版本消费者可以继续读取原字段。
- 宿主在启用 P0 全量抽取前，应对 `ir` 和 `coverage` 执行强类型校验。
- 相同源文件和配置重复解析必须得到稳定指纹，并在 diff 中归类为 `unchanged`。
- 任何 Wiki 或领域编译失败都不得反向破坏 ParseCore 原始解析结果。
