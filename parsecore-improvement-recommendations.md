# ParseCore 改进建议与本地验证结论

日期：2026-04-30

## 背景

本地将产品上传-解析-预览链路接入 ParseCore 后，用 `D:\app\uploads` 中典型 PDF 做了小范围验证。整体结论是 ParseCore 能显著改善页级结构、表格输出和 OCR 回退能力，但在真实生产文档上暴露出若干影响接入质量与产品判断的问题。以下建议可转给解析中台开发方作为后续迭代清单。

## 已在本地修复/优化

1. 依赖与运行环境补齐
   - `parsers` extra 中补充 `pdfplumber>=0.11,<1`，避免健康检查显示可用但运行时无法 import。
   - Docker 镜像补充 OpenCV/RapidOCR 运行依赖：`libgl1`、`libglib2.0-0`、`libsm6`、`libxext6`、`libxrender1`、`libxcb1`。
   - 验收：容器内 `pdfplumber` 可 import，RapidOCR engine 可初始化。

2. PDF 乱码识别增强
   - 除 `(cid:N)` 外，增加 `/0 /1 /i255` 这类 PDF name-map token 检测。
   - 对高密度 name-map token 输出 `pdf_name_garble`，建议动作 `retry_with_ocr`。
   - 页级 OCR fallback 也增加 `pdf_name_dense` 触发原因。

3. API payload 稳定性
   - `_project_pages` 对 title-only 页面补齐 `tables: []`，避免 `/parse/batch` 在无表格页上 `KeyError: 'tables'`。

4. OCR 后质量口径校准
   - 原先质量分和 flags 基于 raw blocks，OCR fallback 成功后仍可能保留 `cid_garble`。
   - 已增加 `reconcile_quality_with_projected_pages`，API 返回前用最终 `pages[].text` 重新校准 CID/PDF-name 乱码标记。
   - 验证样例：`25-51-06.pdf` 开启 OCR 后，返回 `score=1.0`、`flags=[]`、`recommended_action=null`，文本可读。

## 建议解析中台优先改进项

### P0：质量指标应描述最终输出，而不是中间态

现象：
ParseCore 内部 raw blocks 可能保留被 OCR 替换前的 PDF 原生乱码，导致产品侧看到“页面文本已可读，但 quality 仍是 cid_garble/retry_with_ocr”。

建议：
质量评估分两层输出：

- `raw_quality`：原始抽取质量，用于诊断 native PDF/text parser。
- `output_quality`：最终返回给业务系统的 `pages[].text` 质量，用于产品侧决策。

验收：
OCR fallback 成功后，`output_quality.recommended_action` 不应继续返回 `retry_with_ocr`；若最终文本仍有乱码，再保留告警。

### P0：OCR fallback 应提供明确的接受/拒绝原因

现象：
调用方只能从文本长度、flags 猜测 OCR 是否被采用，难以定位“触发了 OCR 但输出没变”的问题。

建议：
在响应中增加：

- `ocr_attempted_pages`
- `ocr_fallback_pages`
- `ocr_rejected_pages`
- `ocr_acceptance_reason`
- `ocr_rejection_reason`
- `native_text_token_count`
- `final_text_token_count`

验收：
对 CID/name-map 乱码 PDF，能清楚看到 OCR 被触发、是否替换原文、替换后 token 数下降多少。

### P1：质量规则覆盖更多 PDF 乱码形态

现象：
真实 PDF 不只出现 `(cid:N)`，还会出现 `/0 /1 /i255`、控制字符、字体编码错位、重复不可见 token 等。

建议：
建立 garble detector 模块，统一检测：

- CID token 密度
- PDF name-map token 密度
- 控制字符密度
- 可打印字符比例
- 单字符/短 token 异常重复
- CJK/Latin 字符分布异常

验收：
每类检测都有单元测试和典型样本；质量 flags 能稳定触发 OCR 或降级策略。

### P1：依赖健康检查要从“配置可用”升级为“实际可执行”

现象：
健康检查曾返回 `pdfplumber=true`，但容器内无法 import；OCR 也会因缺少系统库初始化失败。

建议：
`/health` 执行真实 import/初始化探测：

- `import pdfplumber`
- `import cv2`
- OCR engine lazy init 或轻量初始化
- 对缺失系统库返回明确 `reason`

验收：
健康检查失败时能直接指出缺失包/系统库，而不是在解析请求中才暴露。

### P1：响应 schema 需要更强的兼容性约束

现象：
title-only 页面缺少 `tables` 字段导致 API 500。

建议：
为 `pages[]` 建立响应模型/契约测试，所有页面类型都保证字段完整：

- `page_number`
- `page_type`
- `text`
- `tables_markdown`
- `tables`
- `artifacts`
- `confidence`

验收：
空页、标题页、纯表格页、纯图片页、OCR 失败页都不应导致 500。

### P2：性能与可观测性

建议：

- OCR 分页并发上限可配置，避免大 PDF 阻塞过久。
- 输出每阶段耗时：render、detect、recognize、postprocess、projection、quality。
- 增加 page-level hot pages，标出最慢/失败/低置信页。
- 对 OCR cache 命中率、平均耗时、失败原因做聚合指标。

验收：
对 45 页 PDF，能看出耗时集中在哪些阶段；重复解析时 OCR cache 生效。

### P2：表格双输出继续完善

建议：

- 保留 Markdown 表格，继续输出 raw cells、rows、cols、bbox、page 坐标。
- 对跨页表格、合并单元格、页眉重复行做结构归并。
- 对 Excel/PDF 表格保持统一 schema。

验收：
产品侧可同时用于预览、检索、结构化入库，不需要再解析 Markdown 表格。

## 本地验收记录

- 单元测试：`python -m unittest -v tests.test_quality`，6 个测试通过。
- 真实 PDF：`D:\app\uploads\25-51-06.pdf`
  - `enable_ocr=false`：原生抽取为 CID 乱码，触发 `retry_with_ocr`。
  - `enable_ocr=true`：最终文本可读，`quality.score=1.0`、`flags=[]`、`recommended_action=null`。
- 中文 PDF：`《维修培训机构管理手册》R5TR1含附件1.pdf`
  - 解析成功，80 页，输出可读中文，未再出现 `tables` 缺失导致的 500。
