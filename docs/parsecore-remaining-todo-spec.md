# ParseCore 剩余待办执行 Spec（历史执行稿）

> **状态说明（2026-06-25）：本文保留为历史执行稿，不再作为当前验收权威源。**
> 当前产品化状态以 [parsecore-productization-spec.md](parsecore-productization-spec.md) 和 [parsecore-productization-quality-assessment.md](parsecore-productization-quality-assessment.md) 为准。
> 本文中“未启动 / 待验证 / 待完善”等状态可能已经被后续代码或文档修复覆盖，阅读时只用于追溯当时的执行计划。

---

## 目录

- [P3-T05 mineru/paddleocr/marker 离线评估清单](#p3-t05-minerupaddleocrmarker-离线评估清单)
- [P3-T10 part rerun provider_route_plan 能力影响验证](#p3-t10-part-rerun-provider_route_plan-能力影响验证)
- [P5-T01 完善 part 策略决策因子](#p5-t01-完善-part-策略决策因子)
- [P5-T11 大文件导出筛选](#p5-t11-大文件导出筛选)
- [P5-T12 真实大样本 benchmark](#p5-t12-真实大样本-benchmark)
- [P6-T02 provider comparison 纳入 self-check](#p6-t02-provider-comparison-纳入-self-check)
- [P6-T07 性能趋势完善](#p6-t07-性能趋势完善)

---

## P3-T05 mineru/paddleocr/marker 离线评估清单

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ❌ 未启动 |
| 影响文件 | `docs/local-provider-ir-upgrade-plan.md`、`docs/ocr-integration-checklist.md` |
| 依赖 | 无外部依赖，纯文档 |

### 现状分析

`docs/local-provider-ir-upgrade-plan.md:121-128` 已有候选 provider 表格（pymupdf4llm-local / docling-local / mineru-local / paddleocr-local / marker-local），标注了推荐用途和接入级别。但缺少结构化评估清单——即每个候选 provider 在接入前必须完成的评测维度、评分标准和通过/不通过判定规则。

### 执行步骤

#### Step 1: 定义评估维度

在 `docs/local-provider-ir-upgrade-plan.md` 的候选 provider 表格之后，新增 `## 离线评估清单` 章节，包含以下 7 个评估维度：

| # | 维度 | 评分标准 | 通过阈值 | 数据来源 |
|---|------|----------|----------|----------|
| 1 | **许可证合规** | 商业使用是否允许 | 必须 "允许" | LICENSE 文件 / 官方声明 |
| 2 | **安装可行性** | 依赖体积、模型体积、CPU/GPU 需求 | 模型 < 2GB（CPU 模式）或 < 6GB（GPU 模式） | `pip install` 日志 + `du -sh` |
| 3 | **文本覆盖率** | text_page_coverage_ratio vs pymupdf4llm baseline | 不低于 baseline - 0.05 | `provider_comparison_report --suite` |
| 4 | **表格结构** | table_unit_coverage_ratio vs docling baseline | 不低于 baseline - 0.10 | `provider_comparison_report --suite` |
| 5 | **阅读顺序** | reading_order_confidence_avg | ≥ 0.75 | coverage projection 聚合 |
| 6 | **性能** | elapsed_s_p50 vs baseline | 不超过 baseline × 2.0 | `tools/parse_perf_baseline.py` |
| 7 | **失败隔离** | 异常输入不崩溃、错误消息清晰 | 100% 通过 | `tests/test_docling_parser.py` 模式扩展 |

#### Step 2: 定义评估样本集

每个候选 provider 必须在以下 5 类样本上完成评测：

| 样本类型 | 描述 | 页数 | 来源 |
|----------|------|------|------|
| 纯文本 PDF | 无表格/图片/扫描 | 5-10 | `var/fixtures/` |
| 表格密集 PDF | 多表/跨页表头 | 10-20 | `var/fixtures/` |
| 图文混排 PDF | 图片+caption+正文 | 10-20 | `var/fixtures/` |
| 扫描件 PDF | 图片页、低质量 OCR | 5-10 | `var/fixtures/` |
| 多栏 PDF | 学术论文/手册格式 | 10-20 | `var/fixtures/` |

#### Step 3: 定义评估结果模板

```markdown
### [provider-name] 离线评估报告

- 评估日期: YYYY-MM-DD
- 评估人: [name]
- provider_version: [version]
- adapter_version: [version]

| 维度 | 结果 | 阈值 | 通过 |
|------|------|------|------|
| 许可证合规 | 允许/不允许 | 必须"允许" | ✅/❌ |
| 安装可行性 | 模型 XGB | < 2GB/6GB | ✅/❌ |
| 文本覆盖率 | 0.XX | ≥ baseline - 0.05 | ✅/❌ |
| 表格结构 | 0.XX | ≥ baseline - 0.10 | ✅/❌ |
| 阅读顺序 | 0.XX | ≥ 0.75 | ✅/❌ |
| 性能 | X.XXs | ≤ baseline × 2.0 | ✅/❌ |
| 失败隔离 | X/X 通过 | 100% | ✅/❌ |

**综合判定**: 准入/准入(灰度)/不准入

**备注**:
- [发现的问题和限制]
```

#### Step 4: 交叉引用

在 `docs/ocr-integration-checklist.md` 中添加指向本评估清单的链接。

### 验收标准

1. `docs/local-provider-ir-upgrade-plan.md` 包含 `## 离线评估清单` 章节。
2. 7 个评估维度全部定义，每个维度有明确的量化阈值。
3. 5 类评估样本集定义完整。
4. 评估结果模板可直接复制使用。
5. `docs/ocr-integration-checklist.md` 有交叉引用。

---

## P3-T10 part rerun provider_route_plan 能力影响验证

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 待验证 |
| 影响文件 | `src/parsecore/runtime.py`、`tests/test_runtime.py`、`tests/test_asgi.py` |
| 依赖 | 无 |

### 现状分析

`runtime.py:1057 rerun_pdf_part` 已接受 `provider_route_plan` 参数，通过 `_normalize_local_provider_route_request` 归一化后传递给子 job 的 options。`test_runtime.py:2276 test_rerun_pdf_part_uses_provider_route_plan_capabilities` 已验证 required_capabilities 传递。`test_asgi.py:2040 test_pdf_parts_batch_rerun_accepts_provider_route_plan_payload` 已验证 API 层批量 rerun 传递 route_plan。

**缺失**: 未验证 rerun 后子 job 的实际 parser 是否按 route_plan 选择了正确的 provider（即 route_plan 从 API → runtime → job options → parser selection 的端到端链路完整性）。

### 执行步骤

#### Step 1: 验证现有测试覆盖

运行以下测试确认现有覆盖：
```bash
.venv/Scripts/python.exe -m pytest tests/test_runtime.py::TestRuntime::test_rerun_pdf_part_uses_provider_route_plan_capabilities tests/test_asgi.py::TestASGI::test_pdf_parts_batch_rerun_accepts_provider_route_plan_payload -v
```

#### Step 2: 补充端到端验证测试

在 `tests/test_runtime.py` 中新增测试 `test_rerun_pdf_part_provider_route_plan_preserved_in_part_observation`：

验证点：
1. 调用 `rerun_pdf_part(provider_route_plan={"required_capabilities": ["ocr", "table"]})` 后，`partition_parts_for_document` 返回的 part payload 中包含 `provider_route_plan` 字段。
2. 该字段的 `required_capabilities` 与传入值一致。
3. 子 job 的 options 中 `local_provider_route_request.required_capabilities` 被正确设置。

#### Step 3: 补充 previous_part_observation 验证

验证 `rerun_pdf_part` 返回值中 `previous_part_observation` 包含上一个 job 的 provider_ids 和 quality_signal_codes。

### 验收标准

1. `test_rerun_pdf_part_uses_provider_route_plan_capabilities` 通过。
2. `test_pdf_parts_batch_rerun_accepts_provider_route_plan_payload` 通过。
3. 新增的端到端验证测试通过，确认 route_plan 从 API 到 part observation 的完整链路。
4. 全量 pytest 无回归。

---

## P5-T01 完善 part 策略决策因子

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 待完善 |
| 影响文件 | `src/parsecore/pdf_parts.py`、`src/parsecore/runtime.py`、`tests/test_pdf_parts.py` |
| 依赖 | 无 |

### 现状分析

`pdf_parts.py:16 plan_pdf_parts` 当前决策因子：
- `total_pages`：总页数
- `target_pages_per_part`：默认 50
- `ocr_heavy_pages_per_part`：默认 10（OCR 重负载场景）
- `profile`：通过 `_is_ocr_heavy(profile, options)` 切换到 ocr_heavy 目标
- `options`：透传 profile 选项

**缺失决策因子**：
1. **历史失败信号**：同一文档前序 part 失败时，是否影响后续 part 的拆分策略。
2. **OCR 重页密度**：文档中 OCR 页的占比，影响是否使用 ocr_heavy 目标。
3. **文件大小**：大文件（如 > 100MB）可能需要更小的 part 以控制内存。

### 执行步骤

#### Step 1: 扩展 `plan_pdf_parts` 签名

在 `pdf_parts.py:plan_pdf_parts` 中新增可选参数：

```python
def plan_pdf_parts(
    doc_id: str,
    total_pages: int,
    target_pages_per_part: int | None = None,
    ocr_heavy_pages_per_part: int | None = None,
    profile: str | None = None,
    options: dict[str, Any] | None = None,
    *,
    file_size_bytes: int | None = None,           # 新增
    ocr_page_ratio: float | None = None,           # 新增
    historical_failure_rate: float | None = None,  # 新增
) -> list[dict[str, Any]]:
```

#### Step 2: 完善 `_pages_per_part` 决策逻辑

```python
def _pages_per_part(
    *,
    target_pages_per_part: int | None,
    ocr_heavy_pages_per_part: int | None,
    profile: str | None,
    options: dict[str, Any] | None,
    file_size_bytes: int | None = None,
    ocr_page_ratio: float | None = None,
    historical_failure_rate: float | None = None,
) -> int:
    target = target_pages_per_part or DEFAULT_TARGET_PAGES_PER_PART
    ocr_heavy_target = ocr_heavy_pages_per_part or DEFAULT_OCR_HEAVY_PAGES_PER_PART

    # 1. OCR 重页密度 > 0.3 时自动切换到 ocr_heavy 目标
    if ocr_page_ratio is not None and ocr_page_ratio > 0.3:
        return _positive_int(ocr_heavy_target, "invalid_pages_per_part")

    # 2. 历史失败率 > 0.2 时缩减 part 大小（减半）
    if historical_failure_rate is not None and historical_failure_rate > 0.2:
        target = max(1, target // 2)

    # 3. 文件大小 > 100MB 时缩减 part 大小
    if file_size_bytes is not None and file_size_bytes > 100 * 1024 * 1024:
        target = max(1, target // 2)

    if _is_ocr_heavy(profile=profile, options=options):
        return _positive_int(ocr_heavy_target, "invalid_pages_per_part")
    return _positive_int(target, "invalid_pages_per_part")
```

#### Step 3: 在 `start_pdf_part_jobs` 中传递新参数

在 `runtime.py:start_pdf_part_jobs` 中从 source_job 和 options 中提取 `file_size_bytes`、`ocr_page_ratio`、`historical_failure_rate`，传递给 `plan_pdf_parts`。

数据来源：
- `file_size_bytes`：`Path(source_job.file_path).stat().st_size`
- `ocr_page_ratio`：从 `options.get("ocr_page_ratio")` 读取（由前置 OCR 检测阶段写入）
- `historical_failure_rate`：从 `options.get("historical_failure_rate")` 读取（由调用方根据历史 job 统计写入）

#### Step 4: 编写测试

在 `tests/test_pdf_parts.py` 的 `PdfPartsPlanTests` 中新增测试：

| 测试名 | 验证点 |
|--------|--------|
| `test_plan_shrinks_part_size_for_large_file` | `file_size_bytes=200MB` → part 大小减半 |
| `test_plan_switches_to_ocr_heavy_when_ratio_high` | `ocr_page_ratio=0.5` → 使用 ocr_heavy 目标 |
| `test_plan_shrinks_part_size_when_failure_rate_high` | `historical_failure_rate=0.3` → part 大小减半 |
| `test_plan_ignores_new_factors_when_none` | 新参数全为 None → 行为与之前一致（向后兼容） |

### 验收标准

1. `plan_pdf_parts` 新增 3 个可选参数，默认值 None 保证向后兼容。
2. `_pages_per_part` 的 3 个新决策规则按阈值触发。
3. `start_pdf_part_jobs` 从 job 上下文提取并传递新参数。
4. 4 个新测试全部通过。
5. 全量 pytest 无回归。
6. 既有 `test_plan_returns_suggestions_without_creating_jobs` 等 dry-run 测试不受影响。

---

## P5-T11 大文件导出筛选

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 待验证 |
| 影响文件 | `src/parsecore/exports.py`、`src/parsecore/api_routes.py`、`tests/test_exports.py` |
| 依赖 | 无 |

### 现状分析

当前导出能力：
- `exports.py:export_structured_projection` 接受 `payload, dataset, format`，**无筛选参数**，直接导出全部行。
- `record_filters.py:filter_records` 支持 `query`、`table_id`、`quality_signal`、`field_filters`、`page_start`、`page_end`，但仅用于 records 端点，**未接入导出**。
- `api_routes.py` 的 records 端点已支持 `quality_signal` 和 `page_start/page_end` 查询参数，但导出端点不支持。

**缺失**：
1. `export_structured_projection` 不支持 `page_range` 和 `quality_signal` 筛选。
2. parse_units 数据集无任何筛选能力。
3. 导出端点 API 不支持查询参数筛选。

### 执行步骤

#### Step 1: 扩展 `export_structured_projection` 签名

在 `exports.py:export_structured_projection` 中新增可选筛选参数：

```python
def export_structured_projection(
    payload: dict[str, Any],
    *,
    dataset: str,
    format: str,
    as_bytes: bool = False,
    page_start: int | None = None,       # 新增
    page_end: int | None = None,         # 新增
    quality_signal: str | None = None,   # 新增
) -> dict[str, str | bytes]:
```

#### Step 2: 在 `_dataset_rows` 中应用筛选

```python
def _dataset_rows(
    payload: dict[str, Any],
    dataset: ExportDataset,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
    quality_signal: str | None = None,
) -> list[dict[str, Any]]:
    rows = payload.get(dataset)
    # ... 既有逻辑 ...

    # 新增：page range 筛选（适用于 pages, lines, records, parse_units）
    if page_start is not None or page_end is not None:
        rows = _filter_by_page_range(rows, page_start=page_start, page_end=page_end)

    # 新增：quality_signal 筛选（适用于 records, parse_units, quality_signals）
    if quality_signal is not None:
        rows = _filter_by_quality_signal(rows, quality_signal=quality_signal)

    return rows
```

#### Step 3: 实现筛选辅助函数

```python
def _filter_by_page_range(
    rows: list[dict[str, Any]],
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[dict[str, Any]]:
    """筛选包含页码字段的行。支持 page_number / page_start+page_end 两种模式。"""
    start = page_start
    end = page_end
    if start is not None and end is not None and start > end:
        raise ValueError("invalid_page_range")
    result = []
    for row in rows:
        row_page_start = row.get("page_start") or row.get("page_number")
        row_page_end = row.get("page_end") or row_page_start
        if row_page_start is None:
            result.append(row)  # 无页码字段的行不受筛选影响
            continue
        if start is not None and row_page_end < start:
            continue
        if end is not None and row_page_start > end:
            continue
        result.append(row)
    return result


def _filter_by_quality_signal(
    rows: list[dict[str, Any]],
    *,
    quality_signal: str | None = None,
) -> list[dict[str, Any]]:
    """筛选包含 quality_signal / quality_signal_codes 字段的行。"""
    if not quality_signal:
        return rows
    result = []
    for row in rows:
        codes = row.get("quality_signal_codes") or row.get("quality_signal")
        if isinstance(codes, list) and quality_signal in codes:
            result.append(row)
        elif isinstance(codes, str) and codes == quality_signal:
            result.append(row)
        elif row.get("code") == quality_signal:
            result.append(row)
    return result
```

#### Step 4: API 导出端点支持查询参数

在 `api_routes.py` 的导出端点中，从 `request.query_params` 提取 `page_start`、`page_end`、`quality_signal`，传递给 `export_structured_projection`。

#### Step 5: 编写测试

在 `tests/test_exports.py` 中新增 `ExportFilteringTests` 类：

| 测试名 | 验证点 |
|--------|--------|
| `test_export_records_filtered_by_page_range` | records 导出只包含指定页范围的行 |
| `test_export_parse_units_filtered_by_page_range` | parse_units 导出只包含指定页范围的行 |
| `test_export_records_filtered_by_quality_signal` | records 导出只包含指定 quality_signal 的行 |
| `test_export_quality_signals_dataset_unaffected_by_page_range` | quality_signals 数据集不受 page_range 筛选影响 |
| `test_export_invalid_page_range_raises_error` | page_start > page_end 时抛出 ValueError |
| `test_export_without_filter_returns_all_rows` | 不传筛选参数时返回全部行（向后兼容） |

### 验收标准

1. `export_structured_projection` 支持 `page_start`、`page_end`、`quality_signal` 三个可选筛选参数。
2. 筛选适用于 `pages`、`lines`、`records`、`parse_units` 数据集。
3. `quality_signals` 数据集不受 page_range 筛选影响（它没有页码字段）。
4. 不传筛选参数时行为与之前完全一致（向后兼容）。
5. API 导出端点支持 `?page_start=1&page_end=10&quality_signal=rag_empty_text_page` 查询参数。
6. 6 个新测试全部通过。
7. 全量 pytest 无回归。

---

## P5-T12 真实大样本 benchmark

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 待门禁化 |
| 影响文件 | `tools/large_pdf_stress.py`、`tools/self_check.py`、`docs/go-live-readiness.md` |
| 依赖 | 无 |

### 现状分析

`tools/large_pdf_stress.py` 已实现：
- 合成 PDF 生成（`--generate-pages 17000`）
- 真实 PDF 路径传入（`--pdf path`）
- part 拆分计划 + 可选执行
- rerun 验证（`--rerun-part-id`）
- JSON + Markdown 报告输出

`docs/go-live-readiness.md` 记录了 17101 页真实样本已有产物，但未作为专项 benchmark 门禁。

**缺失**：
1. 无固定基准样本路径和基准快照。
2. 无门禁判定逻辑（通过/不通过的阈值）。
3. 未接入 `self_check.py` 作为可选 check。

### 执行步骤

#### Step 1: 定义基准样本

在 `var/regression/` 下创建 `large-pdf-benchmark.config.json`：

```json
{
  "sample_name": "large-pdf-benchmark",
  "pdf_path": "var/fixtures/large-pdf-benchmark.pdf",
  "total_pages": 17101,
  "target_pages_per_part": 50,
  "profile": "large-pdf",
  "max_active_parts_per_doc": 4,
  "execute_parts": false,
  "thresholds": {
    "plan_elapsed_s_max": 10.0,
    "part_count_min": 340,
    "part_count_max": 350,
    "error_count_max": 0,
    "snapshot_blocks_min": 100000
  }
}
```

> 如果 `var/fixtures/large-pdf-benchmark.pdf` 不存在，自动回退到 `--generate-pages 17101` 合成模式。

#### Step 2: 在 `large_pdf_stress.py` 中新增门禁判定

```python
def evaluate_gate(report: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """根据 threshold 判定 benchmark 是否通过。"""
    thresholds = config.get("thresholds", {})
    checks = []
    for key, threshold in thresholds.items():
        # 解析 key: e.g. "plan_elapsed_s_max" → metric="plan_elapsed_s", op="max"
        if key.endswith("_max"):
            metric = key[:-4]
            actual = report.get(metric)
            passed = actual is not None and actual <= threshold
        elif key.endswith("_min"):
            metric = key[:-4]
            actual = report.get(metric)
            passed = actual is not None and actual >= threshold
        else:
            continue
        checks.append({
            "metric": metric,
            "actual": actual,
            "threshold": threshold,
            "operator": "max" if key.endswith("_max") else "min",
            "passed": passed,
        })
    all_passed = all(c["passed"] for c in checks)
    return {"passed": all_passed, "checks": checks}
```

#### Step 3: 接入 `self_check.py` 作为可选 check

在 `tools/self_check.py` 中新增 `--large-pdf-benchmark` 参数：
- 传入 benchmark config 路径
- 在 `main()` 中作为可选 check 执行
- 输出 gate 结果到 self-check JSON

#### Step 4: 文档更新

在 `docs/go-live-readiness.md` 的"推荐执行顺序"中新增：
```
6. 若 `--large-pdf-benchmark` 配置可用，执行大样本 benchmark 门禁。
```

### 验收标准

1. `var/regression/large-pdf-benchmark.config.json` 存在且格式正确。
2. `large_pdf_stress.py` 包含 `evaluate_gate` 函数，能根据 threshold 判定通过/不通过。
3. `self_check.py` 支持 `--large-pdf-benchmark` 可选参数。
4. 真实 PDF 不存在时自动回退到合成模式，不报错。
5. gate 判定结果包含每项 metric 的 actual/threshold/passed。
6. `docs/go-live-readiness.md` 推荐执行顺序包含大样本 benchmark 步骤。
7. 全量 pytest 无回归。

---

## P6-T02 provider comparison 纳入 self-check

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 已集成但输出需完善 |
| 影响文件 | `tools/self_check.py`、`tools/provider_comparison_report.py`、`docs/self-check-gate.md` |
| 依赖 | 无 |

### 现状分析

`tools/self_check.py` 已集成 provider comparison：
- `--provider-suite` 参数（显式传入 suite 路径）
- `_default_provider_suite_for_profile()` 自动发现 `var/regression/provider-suite.{profile}.json`
- `_run_provider_comparison_suite()` 执行子进程调用
- `_default_provider_suite_preflight()` 预检样本是否存在

**缺失**：
1. 标准 JSON 输出格式未定义——当前输出是子进程 stdout 解析，格式不稳定。
2. Markdown 报告未自动生成到 self-check 输出目录。
3. `docs/self-check-gate.md` 未文档化 provider comparison check 的使用方法和输出位置。

### 执行步骤

#### Step 1: 标准化 JSON 输出

在 `self_check.py:_run_provider_comparison_suite` 中，确保子进程输出被解析为标准格式：

```python
# 标准输出结构
{
    "name": "provider_comparison_suite",
    "status": "ok" | "degraded" | "failed" | "timeout",
    "elapsed_s": float,
    "suite_path": str,
    "sample_count": int,
    "summary": {
        "total_samples": int,
        "passed_samples": int,
        "failed_samples": int,
        "gate_policy": {...},
    },
    "output_json_path": str,  # 指向 provider-comparison.{profile}.json
    "output_md_path": str,    # 指向 provider-comparison.{profile}.md
}
```

#### Step 2: 自动生成 Markdown 报告

在 `_run_provider_comparison_suite` 执行后，自动将 `provider-comparison.{profile}.md` 复制到 self-check 输出目录。

#### Step 3: 文档化

在 `docs/self-check-gate.md` 中新增 `## Provider Comparison Check` 章节：

```markdown
## Provider Comparison Check

### 启用方式

```bash
# 显式指定 suite
.venv/Scripts/python.exe -m tools.self_check --provider-suite var/regression/provider-suite.fast.json

# 自动发现（suite 文件放在 var/regression/ 下）
.venv/Scripts/python.exe -m tools.self_check
```

### 输出位置

- JSON: `var/self-check/provider-comparison.{profile}.json`
- Markdown: `var/self-check/provider-comparison.{profile}.md`

### 判定规则

- `status=ok`：所有样本通过 gate policy
- `status=degraded`：有样本超时或降级
- `status=failed`：有样本硬失败或 gate policy 不通过
```

### 验收标准

1. `_run_provider_comparison_suite` 返回的 CheckResult 包含标准化 JSON 结构。
2. `output_json_path` 和 `output_md_path` 指向实际文件。
3. `docs/self-check-gate.md` 包含 `## Provider Comparison Check` 章节。
4. 章节包含启用方式、输出位置、判定规则。
5. 全量 pytest 无回归。

---

## P6-T07 性能趋势完善

| 属性 | 值 |
|------|-----|
| 优先级 | P2 |
| 状态 | ⚠️ 基础已有，跨版本比较需完善 |
| 影响文件 | `tools/perf_trend_report.py`、`tests/test_perf_trend_report.py` |
| 依赖 | 无 |

### 现状分析

`tools/perf_trend_report.py` 已实现：
- `PERF_COLUMNS`：6 项指标（elapsed_s, ocr_total_s, call_s, provider_s, rec_s, max_page_ocr_s）
- `build_summary()`：从 self-check JSON 提取 perf_tracking、comparison、checks
- `render_markdown()`：渲染 Markdown 报告
- `--compare-report`：两份报告对比，生成 delta 表
- "Perf Deltas Vs Previous Report" 章节

**缺失**：
1. **内存指标**：无 peak_memory_mb / avg_memory_mb。
2. **吞吐指标**：无 MB/s（解析速度）、part_throughput（part/s）、export_throughput（rows/s）。
3. **多版本趋势**：当前只支持两份报告对比（A vs B），不支持 3+ 版本的趋势线。
4. **趋势摘要**：无 "持续下降 N 个版本" 或 "累计增长 X%" 的趋势判定。

### 执行步骤

#### Step 1: 扩展 PERF_COLUMNS

```python
PERF_COLUMNS = (
    "elapsed_s",
    "ocr_total_s",
    "call_s",
    "provider_s",
    "rec_s",
    "max_page_ocr_s",
    "peak_memory_mb",      # 新增
    "throughput_mb_s",     # 新增
    "part_throughput_s",   # 新增
)

EXTENDED_METRICS = {
    "peak_memory_mb": "peak_memory",
    "throughput_mb_s": "file_size_mb / elapsed_s",
    "part_throughput_s": "part_count / total_elapsed_s",
}
```

#### Step 2: 新增多版本趋势函数

```python
def build_trend_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """从 3+ 份 self-check JSON 构建跨版本趋势摘要。"""
    if len(reports) < 2:
        return {"available": False, "reason": "need_at_least_2_reports"}

    versions = []
    for report in reports:
        summary = build_summary(report)
        versions.append({
            "version": report.get("version") or report.get("timestamp", "?"),
            "status": summary.get("status"),
            "elapsed_s_p50": summary.get("overview", {}).get("elapsed_s_p50"),
            "elapsed_s_p95": summary.get("overview", {}).get("elapsed_s_p95"),
            "peak_memory_mb": summary.get("overview", {}).get("peak_memory_mb"),
            "slowest_sample": summary.get("overview", {}).get("slowest_sample", {}).get("name"),
        })

    # 计算趋势方向
    elapsed_values = [v["elapsed_s_p50"] for v in versions if v["elapsed_s_p50"] is not None]
    trend_direction = "stable"
    if len(elapsed_values) >= 2:
        first, last = elapsed_values[0], elapsed_values[-1]
        if last > first * 1.1:
            trend_direction = "regressing"
        elif last < first * 0.9:
            trend_direction = "improving"

    return {
        "available": True,
        "version_count": len(versions),
        "versions": versions,
        "trend_direction": trend_direction,
        "elapsed_s_p50_first": elapsed_values[0] if elapsed_values else None,
        "elapsed_s_p50_last": elapsed_values[-1] if elapsed_values else None,
        "elapsed_s_p50_change_pct": (
            ((elapsed_values[-1] - elapsed_values[0]) / elapsed_values[0] * 100)
            if len(elapsed_values) >= 2 and elapsed_values[0] > 0
            else None
        ),
    }
```

#### Step 3: 渲染多版本趋势表

在 `render_markdown()` 中新增 `## Multi-Version Trend` 章节（当传入 3+ 份报告时渲染）：

```markdown
## Multi-Version Trend

| version | status | elapsed_s_p50 | elapsed_s_p95 | peak_memory_mb | trend |
|---------|--------|---------------|---------------|----------------|-------|
| v1.0 | ok | 2.5 | 5.0 | 120 | - |
| v1.1 | ok | 2.3 | 4.8 | 115 | improving |
| v1.2 | ok | 2.8 | 5.5 | 130 | regressing |

- trend_direction: **regressing**
- elapsed_s_p50 change: +12.0%
```

#### Step 4: 新增 CLI 参数

```python
parser.add_argument(
    "--trend-reports",
    nargs="+",
    help="Multiple self-check JSON reports for multi-version trend analysis",
)
```

#### Step 5: 编写测试

在 `tests/test_perf_trend_report.py` 中新增：

| 测试名 | 验证点 |
|--------|--------|
| `test_build_trend_summary_with_3_versions` | 3 份报告生成趋势摘要，trend_direction 正确 |
| `test_build_trend_summary_with_1_report_returns_unavailable` | 1 份报告返回 available=False |
| `test_build_trend_summary_detects_regression` | elapsed_s 持续上升 → trend_direction="regressing" |
| `test_build_trend_summary_detects_improvement` | elapsed_s 持续下降 → trend_direction="improving" |
| `test_render_markdown_includes_trend_section_when_3plus_reports` | Markdown 包含 Multi-Version Trend 章节 |

### 验收标准

1. `PERF_COLUMNS` 扩展为 9 项（新增 peak_memory_mb, throughput_mb_s, part_throughput_s）。
2. `build_trend_summary` 支持 3+ 份报告的跨版本趋势分析。
3. `render_markdown` 在传入 3+ 份报告时渲染 `## Multi-Version Trend` 章节。
4. 趋势判定包含 trend_direction（stable/improving/regressing）和 change_pct。
5. CLI 支持 `--trend-reports` 参数。
6. 5 个新测试全部通过。
7. 全量 pytest 无回归。
8. 既有 `--compare-report` 两份对比功能不受影响（向后兼容）。
