# ParseCore 自检门禁

## 目的

当前默认质量门禁已经从 jobcard 双跑切回 ParseCore 自身验证。

这套门禁用于回答三个问题：

1. 代码改动后，基础可靠性是否仍然成立。
2. 默认运行时是否还能正常装配。
3. 解析质量和长尾性能是否出现明显退化。

## 默认入口

快速自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --skip-regression
```

全量自检：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py
```

输出会同时打印到终端，并写入 [var/self-check/latest.json](../var/self-check/latest.json)。

## 覆盖范围

`tools/self_check.py` 当前串联三类检查：

1. 单测：`unittest discover -s tests -p "test_*.py"`
2. 运行时 smoke：`parsecore.cli describe --config parsecore.toml`
3. 回归基线：`tools/regression_baseline.py check-suite --suite var/regression/suite.json`

## 退出码语义

1. `0`：全部通过
2. `1`：至少一项必需检查失败
3. `2`：没有硬失败，但存在退化或超时

## 2026-04-26 当前结论

### 可靠性

- 快速自检已通过。
- 单测结果：`125 passed, 5 skipped`。
- 单测耗时约 `6.0s` 到 `6.6s`。
- 默认 runtime describe 正常，当前形态为 `index_mode = hybrid`、`execution_mode = inline`。

### 解析质量

已确认以下基线样本在预算内通过：

1. `primary-default`
2. `primary-strip-hf`
3. `sample-25-51-06`
4. `sample-flight-ops-manual-r2`

`sample-27-81-17` 依照 `suite.json` 默认策略以 `slow` 标签跳过，不计入默认门禁失败。

### 性能风险

- 当前最明显的长尾风险集中在 `sample-cmm-32-48-21-ocr`。
- 该 OCR 样本在回归套件中持续运行超过 `600s`，未在默认超时窗口内完成。
- 这说明常规样本和非长尾 PDF 基线已经稳定，但 OCR 重样本仍需要单独优化或拆分更细的门禁策略。

## 当前建议

1. 日常改动默认跑快速自检。
2. 涉及 PDF 后处理、OCR、分页重排时跑全量自检。
3. 若全量自检返回 `degraded`，优先检查是否卡在 `sample-cmm-32-48-21-ocr`。
4. 若要继续收敛长尾性能，应把 OCR 重样本单独拆成专项 smoke 或阶段性预算。