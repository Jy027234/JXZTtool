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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.quality import (  # noqa: E402
    PageQuality,
    StructuralQualityReport,
    evaluate_blocks,
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


def _layout_signals_to_dict(layout_signals: Any) -> dict[str, Any]:
    return dataclasses.asdict(layout_signals)


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


def _run_one(runtime, *, fixture: Path, top_pages: int) -> dict[str, Any]:
    media_type = _detect_media_type(fixture)
    started = time.monotonic()
    outcome = runtime.submit(
        ParseRequest(
            doc_id=f"regression:{fixture.name}",
            file_path=str(fixture),
            media_type=media_type,
            options={"source": "regression-baseline"},
        )
    )
    elapsed_s = round(time.monotonic() - started, 3)
    quality = evaluate_blocks(outcome.blocks)
    layout_signals = evaluate_layout_signals(outcome.blocks)

    table_blocks = sum(1 for b in outcome.blocks if getattr(b.type, "value", b.type) == "table")
    paragraph_blocks = sum(
        1 for b in outcome.blocks if getattr(b.type, "value", b.type) == "paragraph"
    )

    return {
        "fixture": str(fixture),
        "fixture_name": fixture.name,
        "elapsed_s": elapsed_s,
        "block_counts": {
            "total": len(outcome.blocks),
            "paragraph": paragraph_blocks,
            "table": table_blocks,
            "chunks": len(outcome.chunks),
        },
        "quality": _report_to_dict(quality, top_pages=top_pages),
        "layout_signals": _layout_signals_to_dict(layout_signals),
    }


def _run_all(*, config: Path, fixtures: list[Path], top_pages: int) -> dict[str, Any]:
    runtime = build_runtime(config)
    _apply_runtime_overrides(runtime, argparse.Namespace(strip_headers_footers="default"))
    results = [_run_one(runtime, fixture=fx, top_pages=top_pages) for fx in fixtures]
    return {
        "config": str(config),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fixtures": results,
    }


def _cmd_save(args: argparse.Namespace) -> int:
    runtime = build_runtime(Path(args.config))
    _apply_runtime_overrides(runtime, args)
    payload = {
        "config": str(Path(args.config)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "post_process_overrides": {"strip_headers_footers": args.strip_headers_footers},
        "fixtures": [
            _run_one(runtime, fixture=Path(p), top_pages=args.top_pages)
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
            f" ocr_pages={layout.get('ocr_fallback_pages', 0)}"
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
    requested_override = args.strip_headers_footers
    if requested_override == "default":
        requested_override = baseline_override
    _apply_runtime_overrides(
        runtime,
        argparse.Namespace(strip_headers_footers=requested_override),
    )

    failures: list[str] = []
    for fixture in fixtures:
        candidate = _run_one(runtime, fixture=fixture, top_pages=args.top_pages)
        base = baseline_index.get(fixture.name)
        if base is None:
            print(f"[check] {fixture.name}: no baseline entry, skipped")
            continue
        cq = candidate["quality"]
        bq = base["quality"]
        candidate_layout = candidate.get("layout_signals", {})
        baseline_layout = base.get("layout_signals", {})
        print(
            f"[check] {fixture.name}"
            f" blocks={candidate['block_counts']['total']} (baseline {base['block_counts']['total']})"
            f" pages={cq['page_count']} (baseline {bq['page_count']})"
            f" very_short_ratio={cq['very_short_ratio']:.4f} (baseline {bq['very_short_ratio']:.4f})"
            f" layout_pages={candidate_layout.get('pages_with_layout_metadata', 0)}"
            f" (baseline {baseline_layout.get('pages_with_layout_metadata', 0)})"
            f" multi_col={candidate_layout.get('multi_column_pages', 0)}"
            f" (baseline {baseline_layout.get('multi_column_pages', 0)})"
            f" stripped_pages={candidate_layout.get('header_footer_stripped_pages', 0)}"
            f" (baseline {baseline_layout.get('header_footer_stripped_pages', 0)})"
            f" ocr_pages={candidate_layout.get('ocr_fallback_pages', 0)}"
            f" (baseline {baseline_layout.get('ocr_fallback_pages', 0)})"
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
            pdf=[],
            baseline=str(baseline_path),
            max_very_short_delta=args.max_very_short_delta,
            max_block_count_delta_pct=args.max_block_count_delta_pct,
            max_page_count_delta=args.max_page_count_delta,
            max_numeric_heavy_delta=args.max_numeric_heavy_delta,
            max_header_footer_delta=args.max_header_footer_delta,
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
