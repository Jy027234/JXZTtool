# 性能与稳定性优化口径

## 分层门禁

当前质量门禁按反馈速度分三层：

1. 本地快速门禁：提交前运行单测和 runtime smoke。

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/self_check.py --skip-regression
```

2. PR / 主分支 CI：`.github/workflows/parsecore-ci.yml` 自动运行快速门禁，并额外验证 base SDK import 不会加载 API 可选依赖。

3. 定时或手动 CI：运行完整回归套件和 Postgres + pgvector smoke。OCR 长尾和存储层问题不阻塞日常 PR，但会在夜间或发版前暴露。

## 上传保护

API 同步入口支持运行时文件大小保护：

```toml
[runtime]
max_upload_bytes = 52428800
```

覆盖入口：

- `POST /parse`
- `POST /v1/parse`
- `POST /parse/batch`
- `POST /v1/parse/batch`

超过限制时返回 `413 file_too_large`，响应会带 `actual_bytes / limit_bytes`，便于宿主系统记录和提示。设置为 `0` 表示关闭限制。

## OCR Benchmark

OCR 重样本不要混在普通 PR 反馈里压时长，使用专项工具观察：

```powershell
d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/ocr_benchmark.py --config parsecore.toml --pdf samples/heavy-ocr.pdf --out var/self-check/ocr-benchmark.json
```

输出包含：

- 文档总耗时、blocks/chunks 数量
- OCR attempted / fallback / failed 页数
- OCR 总耗时、最慢 OCR 页耗时
- top OCR 热页和分类旋转稀疏页
- 基础结构质量摘要与 noisy pages

发版前建议固定 1 到 3 个真实 OCR 长尾样本，保存 benchmark JSON，与上一个灰度版本对比：

- `ocr_total_elapsed_s` 是否明显放大
- `max_ocr_page_elapsed_s` 是否出现新尖峰
- `ocr_failed_pages` 是否从 0 变为非 0
- `very_short_ratio` 和 noisy pages 是否异常升高

## 建议执行顺序

1. 日常开发：快速本地门禁。
2. PR：CI 快速门禁。
3. 涉及 parser、OCR、layout、存储、索引：手动触发完整 CI。
4. 灰度前：跑 OCR benchmark 和 Postgres + pgvector smoke。
5. 灰度中：观察 `/v1/parse/metrics`、`/v1/parse/events`、`/v1/parse/indexes/metrics` 和 `/v1/parse/prometheus`。
