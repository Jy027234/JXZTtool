"""Regression baseline runner (Phase C3).

Replaces the legacy-vs-ParseCore compare flow with a self-contained baseline
report that depends only on ParseCore's own structural self-check
(``parsecore.quality``) and runtime metrics. Layout-side signals are also
serialized as informational fields, but they do not participate in failure
budgets.

Usage::

    # 1. Establish baseline on a fixture set, write JSON snapshot.
    python tools/regression_baseline.py save \\
        --pdf D:\\app\\uploads\\36d65cd6b61346e28e97dbaf829646de.pdf \\
        --out var/regression/baseline.json

    # 2. After a code change, re-run on the same fixtures and diff.
    python tools/regression_baseline.py check \\
        --pdf D:\\app\\uploads\\36d65cd6b61346e28e97dbaf829646de.pdf \\
        --baseline var/regression/baseline.json

    # 3. Run the fixed regression batch.
    python tools/regression_baseline.py check-suite \
        --suite var/regression/suite.json

The "check" mode exits non-zero when any guarded metric drifts beyond the
configured budget, so it is suitable as a CI gate.

Guard budgets (tunable via CLI):
    --max-very-short-delta        absolute delta on very_short_ratio
    --max-block-count-delta-pct   relative drift on total_blocks (e.g. 0.05)
    --max-page-count-delta        absolute delta on page_count
    --max-numeric-heavy-delta     absolute delta on numeric_heavy_total
    --max-header-footer-delta     absolute delta on suspected_header_footer_total
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.quality import (  # noqa: E402
    PageQuality,
    StructuralQualityReport,
    evaluate_blocks,
    evaluate_chunk_embeddings,
    evaluate_layout_signals,
)


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _detect_media_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix)


def _report_to_dict(report: StructuralQualityReport, *, top_pages: int) -> dict[str, Any]:
    noisy = [dataclasses.asdict(page) for page in report.noisy_pages(top=top_pages)]
    return {
        "total_blocks": report.total_blocks,
        "page_count": report.page_count,
        "median_block_length": report.median_block_length,
        "very_short_ratio": report.very_short_ratio,
        "suspected_header_footer_total": report.suspected_header_footer_total,
        "numeric_heavy_total": report.numeric_heavy_total,
        "long_block_total": report.long_block_total,
        "noisy_pages": noisy,
    }


def _layout_signals_to_dict(layout_signals: Any, *, top_pages: int) -> dict[str, Any]:
    payload = dataclasses.asdict(layout_signals)
    payload.pop("ocr_page_signals", None)
    payload["ocr_hot_pages"] = [
        dataclasses.asdict(page) for page in layout_signals.ocr_hot_pages(top=top_pages)
    ]
    payload["ocr_sparse_cls_pages"] = [
        dataclasses.asdict(page)
        for page in layout_signals.ocr_sparse_cls_pages(top=top_pages)
    ]
    return payload


def _format_ocr_page_signal(page: dict[str, Any]) -> str:
    crop_count = int(page.get("ocr_provider_crop_count", 0) or 0)
    rotate_high_count = int(page.get("ocr_provider_cls_rotate_high_count", 0) or 0)
    return (
        f"p{int(page.get('page_number', 0))}"
        f":total={float(page.get('ocr_total_elapsed_s', 0.0)):.3f}s"
        f",engine={float(page.get('ocr_engine_exec_elapsed_s', 0.0)):.3f}s"
        f",cls={float(page.get('ocr_provider_cls_elapsed_s', 0.0)):.3f}s"
        f",crops={crop_count}"
        f",hi={rotate_high_count}/{crop_count}"
        f"({float(page.get('cls_rotate_high_ratio', 0.0)):.1%})"
    )


def _embedding_quality_to_dict(embedding_quality: Any) -> dict[str, Any]:
    return dataclasses.asdict(embedding_quality)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_table_cells(raw_cells: Any) -> list[list[str]]:
    if not isinstance(raw_cells, list):
        return []
    rows: list[list[str]] = []
    for row in raw_cells:
        if not isinstance(row, list):
            continue
        normalized_row = ["" if cell is None else str(cell).strip() for cell in row]
        if any(cell for cell in normalized_row):
            rows.append(normalized_row)
    return rows


def _looks_like_markdown_table(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    if not all(line.startswith("|") and line.endswith("|") for line in lines[:2]):
        return False
    separator = lines[1].replace("|", " ").replace(":", " ").replace("-", " ").strip()
    return separator == ""


def _looks_like_tsv_table(text: str) -> bool:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return all("\t" in line for line in lines[:2])


def _lookup_option_path(options: dict[str, Any], path: str) -> Any:
    current: Any = options
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _merge_nested_options(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested_options(dict(merged[key]), value)
            continue
        merged[key] = value
    return merged


def _request_option_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {"source": "regression-baseline"}
    post_process_options: dict[str, Any] = {}
    if getattr(args, "enable_layout_reading_order", None) is not None:
        post_process_options["layout_reading_order"] = bool(args.enable_layout_reading_order)
    table_options: dict[str, Any] = {}
    if getattr(args, "enable_table_structure", None) is not None:
        table_options["enabled"] = bool(args.enable_table_structure)
    if getattr(args, "table_output_format", None) is not None:
        table_options["output_format"] = str(args.table_output_format)
    if getattr(args, "table_header_rows", None) is not None:
        table_options["header_rows"] = max(1, int(args.table_header_rows))
    if post_process_options:
        overrides["post_process"] = post_process_options
    if table_options:
        overrides["enrichment"] = {"table_structure": table_options}
    return overrides


def _layout_reading_order_quality_to_dict(
    *,
    blocks: Sequence[Any],
    request_options: dict[str, Any],
) -> dict[str, Any]:
    multi_column_pages: set[int] = set()
    applied_pages: set[int] = set()
    applied_blocks = 0
    metadata_present = False
    strategy_counts: dict[str, int] = {}

    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            continue
        page_number = _safe_int(metadata.get("page"))
        if _safe_int(metadata.get("column_count_hint")) and int(metadata.get("column_count_hint", 1)) > 1 and page_number is not None:
            multi_column_pages.add(page_number)
        if "layout_reading_order_applied" in metadata:
            metadata_present = True
        if not metadata.get("layout_reading_order_applied"):
            continue
        applied_blocks += 1
        if page_number is not None:
            applied_pages.add(page_number)
        strategy = str(metadata.get("layout_reading_order_strategy") or "unknown")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    enabled_override = _lookup_option_path(request_options, "post_process.layout_reading_order")
    if enabled_override is None:
        enabled_override = _lookup_option_path(request_options, "enrichment.layout_reading_order.enabled")
    enabled = bool(enabled_override) if enabled_override is not None else metadata_present
    applied_page_ratio = (
        round(len(applied_pages) / len(multi_column_pages), 4)
        if multi_column_pages
        else 0.0
    )
    return {
        "enabled": enabled,
        "multi_column_pages": len(multi_column_pages),
        "applied_pages": len(applied_pages),
        "applied_blocks": applied_blocks,
        "applied_page_ratio": applied_page_ratio,
        "strategy_counts": strategy_counts,
    }


def _layout_quality_for_check(payload: dict[str, Any]) -> dict[str, Any]:
    existing = payload.get("layout_quality")
    if isinstance(existing, dict) and existing:
        return existing
    layout_signals = payload.get("layout_signals") or {}
    multi_column_pages = int(layout_signals.get("multi_column_pages", 0) or 0)
    enabled = multi_column_pages > 0
    applied_pages = multi_column_pages if enabled else 0
    applied_page_ratio = 1.0 if enabled and multi_column_pages > 0 else 0.0
    return {
        "enabled": enabled,
        "multi_column_pages": multi_column_pages,
        "applied_pages": applied_pages,
        "applied_blocks": 0,
        "applied_page_ratio": applied_page_ratio,
        "strategy_counts": {},
    }


def _table_quality_to_dict(
    *,
    blocks: Sequence[Any],
    chunks: Sequence[Any],
    request_options: dict[str, Any],
) -> dict[str, Any]:
    table_blocks = [
        block
        for block in blocks
        if getattr(getattr(block, "type", None), "value", getattr(block, "type", None)) == "table"
    ]
    table_chunks = [
        chunk
        for chunk in chunks
        if str(getattr(chunk, "semantic_role", "")).strip().lower() == "table"
    ]

    table_blocks_with_cells = 0
    total_rows = 0
    total_cols = 0
    for block in table_blocks:
        cells = _normalize_table_cells(getattr(block, "metadata", {}).get("cells"))
        if not cells:
            continue
        table_blocks_with_cells += 1
        total_rows += len(cells)
        total_cols += max((len(row) for row in cells), default=0)

    output_format = str(
        _lookup_option_path(request_options, "enrichment.table_structure.output_format") or "markdown"
    ).lower()
    enabled = bool(_lookup_option_path(request_options, "enrichment.table_structure.enabled"))
    rendered_ready_chunks = 0
    markdown_ready_chunks = 0
    tsv_ready_chunks = 0
    for chunk in table_chunks:
        text = str(getattr(chunk, "text", "") or "")
        markdown_ready = _looks_like_markdown_table(text)
        tsv_ready = _looks_like_tsv_table(text)
        if markdown_ready:
            markdown_ready_chunks += 1
        if tsv_ready:
            tsv_ready_chunks += 1
        if output_format == "tsv":
            rendered_ready_chunks += int(tsv_ready)
        else:
            rendered_ready_chunks += int(markdown_ready)

    table_chunk_count = len(table_chunks)
    rendered_ready_ratio = (
        round(rendered_ready_chunks / table_chunk_count, 4)
        if table_chunk_count > 0
        else 0.0
    )
    return {
        "enabled": enabled,
        "output_format": output_format,
        "table_block_count": len(table_blocks),
        "table_blocks_with_cells": table_blocks_with_cells,
        "table_chunk_count": table_chunk_count,
        "rendered_ready_chunks": rendered_ready_chunks,
        "rendered_ready_ratio": rendered_ready_ratio,
        "markdown_ready_chunks": markdown_ready_chunks,
        "tsv_ready_chunks": tsv_ready_chunks,
        "total_rows": total_rows,
        "total_cols": total_cols,
        "mean_rows_per_table": round(total_rows / table_blocks_with_cells, 3) if table_blocks_with_cells else 0.0,
        "mean_cols_per_table": round(total_cols / table_blocks_with_cells, 3) if table_blocks_with_cells else 0.0,
    }


def _apply_runtime_overrides(runtime: Any, args: argparse.Namespace) -> None:
    strip_mode = args.strip_headers_footers
    if strip_mode == "default":
        return
    target = strip_mode == "on"
    for parser in getattr(runtime, "parsers", ()):
        if getattr(parser, "name", None) != "pdf-text":
            continue
        if hasattr(parser, "_strip_hf_enabled"):
            parser._strip_hf_enabled = target


def _run_one(runtime, *, fixture: Path, top_pages: int, request_options: dict[str, Any]) -> dict[str, Any]:
    media_type = _detect_media_type(fixture)
    started = time.monotonic()
    outcome = runtime.submit(
        ParseRequest(
            doc_id=f"regression:{fixture.name}",
            file_path=str(fixture),
            media_type=media_type,
            options=request_options,
        )
    )
    elapsed_s = round(time.monotonic() - started, 3)
    quality = evaluate_blocks(outcome.blocks)
    layout_signals = evaluate_layout_signals(outcome.blocks)
    embedding_quality = evaluate_chunk_embeddings(outcome.chunks)

    table_blocks = sum(1 for b in outcome.blocks if getattr(b.type, "value", b.type) == "table")
    paragraph_blocks = sum(
        1 for b in outcome.blocks if getattr(b.type, "value", b.type) == "paragraph"
    )

    return {
        "fixture": str(fixture),
        "fixture_name": fixture.name,
        "elapsed_s": elapsed_s,
        "request_options": request_options,
        "block_counts": {
            "total": len(outcome.blocks),
            "paragraph": paragraph_blocks,
            "table": table_blocks,
            "chunks": len(outcome.chunks),
        },
        "quality": _report_to_dict(quality, top_pages=top_pages),
        "layout_signals": _layout_signals_to_dict(layout_signals, top_pages=top_pages),
        "layout_quality": _layout_reading_order_quality_to_dict(
            blocks=outcome.blocks,
            request_options=request_options,
        ),
        "embedding_quality": _embedding_quality_to_dict(embedding_quality),
        "table_quality": _table_quality_to_dict(
            blocks=outcome.blocks,
            chunks=outcome.chunks,
            request_options=request_options,
        ),
    }


def _run_all(*, config: Path, fixtures: list[Path], top_pages: int) -> dict[str, Any]:
    runtime = build_runtime(config)
    _apply_runtime_overrides(runtime, argparse.Namespace(strip_headers_footers="default"))
    results = [
        _run_one(
            runtime,
            fixture=fx,
            top_pages=top_pages,
            request_options={"source": "regression-baseline"},
        )
        for fx in fixtures
    ]
    return {
        "config": str(config),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fixtures": results,
    }


def _cmd_save(args: argparse.Namespace) -> int:
    runtime = build_runtime(Path(args.config))
    _apply_runtime_overrides(runtime, args)
    request_option_overrides = _request_option_overrides_from_args(args)
    payload = {
        "config": str(Path(args.config)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "post_process_overrides": {"strip_headers_footers": args.strip_headers_footers},
        "request_option_overrides": request_option_overrides,
        "fixtures": [
            _run_one(runtime, fixture=Path(p), top_pages=args.top_pages, request_options=request_option_overrides)
            for p in args.pdf
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for fixture in payload["fixtures"]:
        q = fixture["quality"]
        layout = fixture["layout_signals"]
        print(
            f"[save] {fixture['fixture_name']}"
            f" blocks={fixture['block_counts']['total']}"
            f" pages={q['page_count']}"
            f" very_short_ratio={q['very_short_ratio']:.4f}"
            f" layout_pages={layout['pages_with_layout_metadata']}"
            f" multi_col={layout['multi_column_pages']}"
            f" stripped_pages={layout['header_footer_stripped_pages']}"
            f" ocr_attempted={layout.get('ocr_attempted_pages', 0)}"
            f" ocr_pages={layout.get('ocr_fallback_pages', 0)}"
            f" ocr_failed={layout.get('ocr_failed_pages', 0)}"
            f" layout_ro_pages={fixture['layout_quality']['applied_pages']}"
            f" embedded_ratio={fixture['embedding_quality']['embedded_chunk_ratio']:.4f}"
            f" table_ready={fixture['table_quality']['rendered_ready_ratio']:.4f}"
            f" table_cells={fixture['table_quality']['table_blocks_with_cells']}"
            f" elapsed={fixture['elapsed_s']}s"
        )
    print(f"[save] wrote {out}")
    return 0


def _check_drift(
    *,
    name: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    args: argparse.Namespace,
) -> list[str]:
    failures: list[str] = []
    bq = baseline["quality"]
    cq = candidate["quality"]
    be = baseline.get("embedding_quality") or {}
    ce = candidate.get("embedding_quality") or {}

    # Absolute deltas.
    for key, budget_attr in (
        ("very_short_ratio", "max_very_short_delta"),
        ("page_count", "max_page_count_delta"),
        ("numeric_heavy_total", "max_numeric_heavy_delta"),
        ("suspected_header_footer_total", "max_header_footer_delta"),
    ):
        budget = getattr(args, budget_attr)
        delta = cq[key] - bq[key]
        # Drift in either direction counts; numeric_heavy / header_footer are
        # informational, so we only fail on increases (worse noise).
        if key in ("numeric_heavy_total", "suspected_header_footer_total"):
            if delta > budget:
                failures.append(
                    f"{name}: {key} grew by {delta} (budget +{budget})"
                )
        elif key == "very_short_ratio":
            if abs(delta) > budget:
                failures.append(
                    f"{name}: {key} drifted by {delta:+.4f} (budget ±{budget})"
                )
        else:  # page_count
            if abs(delta) > budget:
                failures.append(
                    f"{name}: {key} drifted by {delta:+d} (budget ±{budget})"
                )

    # Relative drift on total_blocks.
    base_blocks = max(1, baseline["block_counts"]["total"])
    delta_pct = (candidate["block_counts"]["total"] - base_blocks) / base_blocks
    if abs(delta_pct) > args.max_block_count_delta_pct:
        failures.append(
            f"{name}: total_blocks drifted by {delta_pct:+.2%}"
            f" (budget ±{args.max_block_count_delta_pct:.2%})"
        )

    embedded_ratio_drop = float(be.get("embedded_chunk_ratio", 0.0)) - float(
        ce.get("embedded_chunk_ratio", 0.0)
    )
    if embedded_ratio_drop > args.max_embedded_chunk_ratio_drop:
        failures.append(
            f"{name}: embedded_chunk_ratio dropped by {embedded_ratio_drop:.4f}"
            f" (budget +{args.max_embedded_chunk_ratio_drop:.4f})"
        )

    bl = _layout_quality_for_check(baseline)
    cl = _layout_quality_for_check(candidate)
    if bl.get("enabled") or cl.get("enabled"):
        applied_pages_drop = int(bl.get("applied_pages", 0)) - int(cl.get("applied_pages", 0))
        if applied_pages_drop > args.max_layout_reading_order_pages_drop:
            failures.append(
                f"{name}: layout_reading_order applied_pages dropped by {applied_pages_drop}"
                f" (budget +{args.max_layout_reading_order_pages_drop})"
            )
        applied_ratio_drop = float(bl.get("applied_page_ratio", 0.0)) - float(
            cl.get("applied_page_ratio", 0.0)
        )
        if applied_ratio_drop > args.max_layout_reading_order_page_ratio_drop:
            failures.append(
                f"{name}: layout_reading_order applied_page_ratio dropped by {applied_ratio_drop:.4f}"
                f" (budget +{args.max_layout_reading_order_page_ratio_drop:.4f})"
            )

    bt = baseline.get("table_quality") or {}
    ct = candidate.get("table_quality") or {}
    if bt.get("enabled") or ct.get("enabled"):
        rendered_ratio_drop = float(bt.get("rendered_ready_ratio", 0.0)) - float(
            ct.get("rendered_ready_ratio", 0.0)
        )
        if rendered_ratio_drop > args.max_table_rendered_ready_ratio_drop:
            failures.append(
                f"{name}: table rendered_ready_ratio dropped by {rendered_ratio_drop:.4f}"
                f" (budget +{args.max_table_rendered_ready_ratio_drop:.4f})"
            )
        cells_drop = int(bt.get("table_blocks_with_cells", 0)) - int(
            ct.get("table_blocks_with_cells", 0)
        )
        if cells_drop > args.max_table_blocks_with_cells_drop:
            failures.append(
                f"{name}: table_blocks_with_cells dropped by {cells_drop}"
                f" (budget +{args.max_table_blocks_with_cells_drop})"
            )
    return failures


def _cmd_check(args: argparse.Namespace) -> int:
    baseline_payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    baseline_index = {fx["fixture_name"]: fx for fx in baseline_payload["fixtures"]}

    fixtures = [Path(p) for p in args.pdf] or [Path(fx["fixture"]) for fx in baseline_payload["fixtures"]]
    runtime = build_runtime(Path(args.config))
    baseline_override = str(
        (baseline_payload.get("post_process_overrides") or {}).get(
            "strip_headers_footers", "default"
        )
    )
    baseline_request_overrides = dict(
        baseline_payload.get("request_option_overrides") or {"source": "regression-baseline"}
    )
    requested_override = args.strip_headers_footers
    if requested_override == "default":
        requested_override = baseline_override
    requested_request_overrides = _request_option_overrides_from_args(args)
    effective_request_overrides = _merge_nested_options(
        baseline_request_overrides,
        requested_request_overrides,
    )
    _apply_runtime_overrides(
        runtime,
        argparse.Namespace(strip_headers_footers=requested_override),
    )

    failures: list[str] = []
    for fixture in fixtures:
        candidate = _run_one(
            runtime,
            fixture=fixture,
            top_pages=args.top_pages,
            request_options=effective_request_overrides,
        )
        base = baseline_index.get(fixture.name)
        if base is None:
            print(f"[check] {fixture.name}: no baseline entry, skipped")
            continue
        cq = candidate["quality"]
        bq = base["quality"]
        candidate_layout = candidate.get("layout_signals", {})
        baseline_layout = base.get("layout_signals", {})
        candidate_layout_quality = _layout_quality_for_check(candidate)
        baseline_layout_quality = _layout_quality_for_check(base)
        candidate_embedding = candidate.get("embedding_quality", {})
        baseline_embedding = base.get("embedding_quality", {})
        candidate_table = candidate.get("table_quality", {})
        baseline_table = base.get("table_quality", {})
        print(
            f"[check] {fixture.name}"
            f" blocks={candidate['block_counts']['total']} (baseline {base['block_counts']['total']})"
            f" pages={cq['page_count']} (baseline {bq['page_count']})"
            f" very_short_ratio={cq['very_short_ratio']:.4f} (baseline {bq['very_short_ratio']:.4f})"
            f" layout_pages={candidate_layout.get('pages_with_layout_metadata', 0)}"
            f" (baseline {baseline_layout.get('pages_with_layout_metadata', 0)})"
            f" multi_col={candidate_layout.get('multi_column_pages', 0)}"
            f" (baseline {baseline_layout.get('multi_column_pages', 0)})"
            f" layout_ro_pages={candidate_layout_quality.get('applied_pages', 0)}"
            f" (baseline {baseline_layout_quality.get('applied_pages', 0)})"
            f" stripped_pages={candidate_layout.get('header_footer_stripped_pages', 0)}"
            f" (baseline {baseline_layout.get('header_footer_stripped_pages', 0)})"
            f" ocr_pages={candidate_layout.get('ocr_fallback_pages', 0)}"
            f" (baseline {baseline_layout.get('ocr_fallback_pages', 0)})"
            f" embedded_ratio={candidate_embedding.get('embedded_chunk_ratio', 0.0):.4f}"
            f" (baseline {baseline_embedding.get('embedded_chunk_ratio', 0.0):.4f})"
            f" table_ready={candidate_table.get('rendered_ready_ratio', 0.0):.4f}"
            f" (baseline {baseline_table.get('rendered_ready_ratio', 0.0):.4f})"
            f" table_cells={candidate_table.get('table_blocks_with_cells', 0)}"
            f" (baseline {baseline_table.get('table_blocks_with_cells', 0)})"
        )
        if (
            candidate_layout.get("ocr_attempted_pages", 0)
            or baseline_layout.get("ocr_attempted_pages", 0)
            or candidate_layout.get("ocr_total_elapsed_s", 0.0)
            or baseline_layout.get("ocr_total_elapsed_s", 0.0)
        ):
            print(
                f"[check][ocr] {fixture.name}"
                f" layout_s={candidate_layout.get('layout_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('layout_elapsed_s', 0.0):.3f})"
                f" ocr_total_s={candidate_layout.get('ocr_total_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_total_elapsed_s', 0.0):.3f})"
                f" render_s={candidate_layout.get('ocr_render_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_render_elapsed_s', 0.0):.3f})"
                f" prep_s={candidate_layout.get('ocr_input_prepare_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_input_prepare_elapsed_s', 0.0):.3f})"
                f" engine_s={candidate_layout.get('ocr_engine_exec_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_engine_exec_elapsed_s', 0.0):.3f})"
                f" call_s={candidate_layout.get('ocr_call_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_call_elapsed_s', 0.0):.3f})"
                f" provider_s={candidate_layout.get('ocr_provider_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_provider_elapsed_s', 0.0):.3f})"
                f" det_s={candidate_layout.get('ocr_provider_det_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_provider_det_elapsed_s', 0.0):.3f})"
                f" cls_s={candidate_layout.get('ocr_provider_cls_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_provider_cls_elapsed_s', 0.0):.3f})"
                f" rec_s={candidate_layout.get('ocr_provider_rec_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_provider_rec_elapsed_s', 0.0):.3f})"
                f" crops={int(candidate_layout.get('ocr_provider_crop_count', 0))}"
                f" (baseline {int(baseline_layout.get('ocr_provider_crop_count', 0))})"
                f" cls_180={int(candidate_layout.get('ocr_provider_cls_rotate_positive_count', 0))}"
                f" (baseline {int(baseline_layout.get('ocr_provider_cls_rotate_positive_count', 0))})"
                f" cls_180_hi={int(candidate_layout.get('ocr_provider_cls_rotate_high_count', 0))}"
                f" (baseline {int(baseline_layout.get('ocr_provider_cls_rotate_high_count', 0))})"
                f" post_s={candidate_layout.get('ocr_postprocess_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('ocr_postprocess_elapsed_s', 0.0):.3f})"
                f" max_page_ocr_s={candidate_layout.get('max_ocr_page_elapsed_s', 0.0):.3f}"
                f" (baseline {baseline_layout.get('max_ocr_page_elapsed_s', 0.0):.3f})"
            )
            hot_pages = list(candidate_layout.get("ocr_hot_pages") or [])[: min(args.top_pages, 5)]
            if hot_pages:
                print(
                    f"[check][ocr-hot-pages] {fixture.name} "
                    + "; ".join(_format_ocr_page_signal(page) for page in hot_pages)
                )
            sparse_pages = list(candidate_layout.get("ocr_sparse_cls_pages") or [])[: min(args.top_pages, 5)]
            if sparse_pages:
                print(
                    f"[check][ocr-sparse-cls] {fixture.name} "
                    + "; ".join(_format_ocr_page_signal(page) for page in sparse_pages)
                )
        failures.extend(
            _check_drift(name=fixture.name, baseline=base, candidate=candidate, args=args)
        )

    if failures:
        print("[check] REGRESSION DETECTED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("[check] OK: all metrics within budget")
    return 0


def _cmd_check_suite(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite)
    suite_payload = json.loads(suite_path.read_text(encoding="utf-8"))
    entries = list(suite_payload.get("entries") or [])
    if not entries:
        print(f"[suite] no entries in {suite_path}")
        return 1

    include_tags = {tag.strip() for tag in (args.include_tags or []) if tag.strip()}
    failures: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_baseline = str(entry.get("baseline") or "").strip()
        if not raw_baseline:
            continue
        entry_tags = {str(t) for t in (entry.get("tags") or [])}
        # Skip entries tagged "slow" unless explicitly included via --include-tag slow.
        if "slow" in entry_tags and "slow" not in include_tags:
            label = str(entry.get("name") or raw_baseline)
            print(f"[suite] SKIP (slow): {label}")
            continue
        baseline_path = _resolve_path(suite_path.parent, raw_baseline)
        label = str(entry.get("name") or baseline_path.name)
        print(f"[suite] {label}: {baseline_path}")
        check_args = argparse.Namespace(
            config=args.config,
            top_pages=args.top_pages,
            strip_headers_footers=args.strip_headers_footers,
            enable_layout_reading_order=args.enable_layout_reading_order,
            enable_table_structure=args.enable_table_structure,
            table_output_format=args.table_output_format,
            table_header_rows=args.table_header_rows,
            pdf=[],
            baseline=str(baseline_path),
            max_very_short_delta=args.max_very_short_delta,
            max_block_count_delta_pct=args.max_block_count_delta_pct,
            max_page_count_delta=args.max_page_count_delta,
            max_numeric_heavy_delta=args.max_numeric_heavy_delta,
            max_header_footer_delta=args.max_header_footer_delta,
            max_embedded_chunk_ratio_drop=args.max_embedded_chunk_ratio_drop,
            max_layout_reading_order_pages_drop=args.max_layout_reading_order_pages_drop,
            max_layout_reading_order_page_ratio_drop=args.max_layout_reading_order_page_ratio_drop,
            max_table_rendered_ready_ratio_drop=args.max_table_rendered_ready_ratio_drop,
            max_table_blocks_with_cells_drop=args.max_table_blocks_with_cells_drop,
        )
        if _cmd_check(check_args) != 0:
            failures.append(label)

    if failures:
        print(f"[suite] FAILED: {', '.join(failures)}")
        return 1
    print("[suite] OK: all baselines passed")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ParseCore regression baseline runner")
    parser.add_argument(
        "--config",
        default=str(ROOT / "parsecore.toml"),
        help="ParseCore config file (default: %(default)s)",
    )
    parser.add_argument("--top-pages", type=int, default=10, help="Top-N noisy pages to record")
    parser.add_argument(
        "--strip-headers-footers",
        choices=("default", "on", "off"),
        default="default",
        help="Override pdf-text strip_headers_footers for this run only",
    )
    parser.add_argument(
        "--max-embedded-chunk-ratio-drop",
        type=float,
        default=0.05,
        help="Maximum allowed drop in embedded chunk ratio vs baseline",
    )
    parser.add_argument(
        "--enable-layout-reading-order",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable layout reading-order rebuilding for this run",
    )
    parser.add_argument(
        "--max-layout-reading-order-pages-drop",
        type=int,
        default=0,
        help="Maximum allowed drop in pages where layout reading-order was applied",
    )
    parser.add_argument(
        "--max-layout-reading-order-page-ratio-drop",
        type=float,
        default=0.05,
        help="Maximum allowed drop in layout reading-order applied page ratio",
    )
    parser.add_argument(
        "--enable-table-structure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable the table-structure enrichment stage for this run",
    )
    parser.add_argument(
        "--table-output-format",
        choices=("markdown", "tsv"),
        default=None,
        help="Rendered output format for the table-structure stage",
    )
    parser.add_argument(
        "--table-header-rows",
        type=int,
        default=None,
        help="Header row count passed to the table-structure stage",
    )
    parser.add_argument(
        "--max-table-rendered-ready-ratio-drop",
        type=float,
        default=0.05,
        help="Maximum allowed drop in table rendered_ready_ratio vs baseline",
    )
    parser.add_argument(
        "--max-table-blocks-with-cells-drop",
        type=int,
        default=0,
        help="Maximum allowed drop in table blocks carrying cells metadata vs baseline",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    save = sub.add_parser("save", help="Run fixtures and save baseline JSON")
    save.add_argument("--pdf", action="append", required=True, help="Fixture path (repeatable)")
    save.add_argument("--out", required=True, help="Baseline output JSON path")
    save.set_defaults(func=_cmd_save)

    check = sub.add_parser("check", help="Re-run fixtures and diff against baseline")
    check.add_argument("--pdf", action="append", default=[], help="Fixture path (repeatable, default = baseline list)")
    check.add_argument("--baseline", required=True, help="Baseline JSON path written by `save`")
    check.add_argument("--max-very-short-delta", type=float, default=0.01)
    check.add_argument("--max-block-count-delta-pct", type=float, default=0.05)
    check.add_argument("--max-page-count-delta", type=int, default=0)
    check.add_argument("--max-numeric-heavy-delta", type=int, default=2)
    check.add_argument("--max-header-footer-delta", type=int, default=2)
    check.set_defaults(func=_cmd_check)

    check_suite = sub.add_parser("check-suite", help="Run a fixed batch of baseline checks")
    check_suite.add_argument(
        "--suite",
        default=str(ROOT / "var" / "regression" / "suite.json"),
        help="Suite JSON path listing baseline files (default: %(default)s)",
    )
    check_suite.add_argument("--max-very-short-delta", type=float, default=0.01)
    check_suite.add_argument("--max-block-count-delta-pct", type=float, default=0.05)
    check_suite.add_argument("--max-page-count-delta", type=int, default=0)
    check_suite.add_argument("--max-numeric-heavy-delta", type=int, default=2)
    check_suite.add_argument("--max-header-footer-delta", type=int, default=2)
    check_suite.add_argument(
        "--include-tag",
        action="append",
        dest="include_tags",
        default=[],
        help="Include entries with this tag (default skips 'slow'-tagged entries)",
    )
    check_suite.set_defaults(func=_cmd_check_suite)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
