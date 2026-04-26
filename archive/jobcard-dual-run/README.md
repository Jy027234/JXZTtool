# jobcard 双跑归档

本目录收纳已经完成的 jobcard 双跑记录、操作手册和辅助脚本。

这些材料保留为历史兼容性证据，但不再作为当前主线推进目标。当前默认质量门禁以 ParseCore 自检为主，仅在需要复现旧宿主兼容性问题时回看本目录内容。

## 内容

- `docs/jobcard-dual-run-runbook.md`：首轮 jobcard 双跑操作手册
- `docs/jobcard-dual-run-record-template.md`：双跑记录模板
- `docs/jobcard-dual-run-record.2026-04-26.md`：2026-04-26 历史联调记录
- `tools/seed_jobcard_live_store.py`：补 live store 样本的历史辅助脚本
- `tools/upload_jobcard_native_sample.py`：走宿主原生上传闭环的历史辅助脚本

## 当前使用原则

1. 默认先跑 ParseCore 自检：单测、回归基线、健康检查和最小运行时 smoke。
2. 只有在需要回放 jobcard 历史样本或定位宿主兼容性问题时，才使用这里的文档和脚本。