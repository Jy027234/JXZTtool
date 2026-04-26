# jobcard 宿主资料归档

本目录收纳 ParseCore 面向 jobcard 宿主的接线、切流和替换资料。

这些文档仍保留历史和兼容性价值，但已经从主产品 `docs/` 顶层移出，避免继续把通用产品资料和单一宿主资料混在一起。

## 内容

- `docs/jobcard-integration.md`：jobcard 接线策略与宿主嵌入背景
- `docs/jobcard-cutover-readiness.md`：切流 readiness 结论与阻塞项
- `docs/jobcard-replacement-checklist.md`：宿主替换、灰度与回滚清单
- `../jobcard-dual-run/README.md`：更细的历史双跑记录、runbook 和辅助脚本

## 当前使用原则

1. 主产品默认看 `docs/self-check-gate.md` 和主 README 中的自检入口。
2. 只有在处理 jobcard 宿主接入或历史兼容性问题时，才进入本目录。
3. 若要回放历史双跑过程，再继续进入 `archive/jobcard-dual-run/`。