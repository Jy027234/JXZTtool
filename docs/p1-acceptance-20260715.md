# ParseCore P1 契约冻结与宿主接入验收

- 生成时间：2026-07-15（本地验收批次）
- 状态：**passed**
- 检查：8/8 通过
- 样例：24 个 payload（4 组）

## 验收项

| 检查 | 状态 | 说明 |
| --- | --- | --- |
| `schema_registry_and_json_schema` | passed | 6 frozen schemas in 2026-06-payload-schema-registry |
| `sample_payloads_all_variants` | passed | 24 payloads across 4 sample variants |
| `legacy_projection_compatibility` | passed | 6 compat/structured/full projections retained |
| `ir_structure_and_quality_signals` | passed | IR pages=3 blocks=10 tables=2 figures=1 units=10 anomaly_signals=3 |
| `reader_ir_traceability` | passed | 8 reader blocks trace to 8 IR blocks |
| `coverage_page_unit_consistency` | passed | pages=3 units=10 indexable=8 gaps=0 |
| `action_contract_workflow` | passed | workflow=inspect→compare→execute→verify actions=4 |
| `part_rerun_monitor_verify_contract` | passed | 1/1 parts expose previous observation and comparison |

## 交付边界

本批次验证 schema registry、最小/复杂/异常/part-rerun 样例、旧 projection 兼容、IR→Reader 可追溯、coverage 页/单元一致性，以及 inspect→compare→execute→verify 动作合同。

Provider 候选准入、远程 embedding/RAG 和真实生产宿主视觉验收仍属于 P2/P3/P7 或外部环境门禁，不在本批次自动放行。
