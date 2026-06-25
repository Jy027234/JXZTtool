"""Render a compact Markdown/JSON report from ParseCore self-check coverage data.

P6-T04/T05: coverage / reader metric trend gate.

Reads one or more self-check JSON reports (fast / full / perf) and compares
coverage and reader metrics across versions, flagging regressions that
exceed configurable thresholds.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "2026-06-coverage-trend-report"


COVERAGE_METRICS = (
    "text_page_coverage_ratio",
    "table_unit_coverage_ratio",
    "unit_chunk_coverage_ratio",
)

READER_METRICS = (
    "visible_block_count",
    "hidden_block_count",
    "table_block_count",
    "reading_order_warning_count",
    "reading_order_confidence_avg",
)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample_coverage(sample: Mapping[str, Any]) -> dict[str, float | None]:
    """Extract coverage metrics from a single self-check sample."""
    coverage = sample.get("coverage") or {}
    return {key: _safe_float(coverage.get(key)) for key in COVERAGE_METRICS}


def _sample_reader(sample: Mapping[str, Any]) -> dict[str, float | None]:
    """Extract reader metrics from a single self-check sample."""
    reader = sample.get("reader") or {}
    return {key: _safe_float(reader.get(key)) for key in READER_METRICS}


def _extract_samples(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull sample dicts out of a self-check JSON report."""
    samples: list[dict[str, Any]] = []
    for key in ("samples", "checks", "results"):
        raw = payload.get(key)
        if isinstance(raw, list):
            samples.extend(item for item in raw if isinstance(item, dict))
            break
    return samples


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _aggregate_metrics(
    samples: list[dict[str, Any]],
    metric_keys: tuple[str, ...],
    extractor: Any,
) -> dict[str, dict[str, float | None]]:
    """Return {metric: {min, max, avg, count}} across samples."""
    aggregates: dict[str, dict[str, float | None]] = {}
    for key in metric_keys:
        values = [v for s in samples if (v := extractor(s).get(key)) is not None]
        if values:
            aggregates[key] = {
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "count": len(values),
            }
        else:
            aggregates[key] = {"min": None, "max": None, "avg": None, "count": 0}
    return aggregates


def _compute_delta(
    baseline: dict[str, float | None],
    current: dict[str, float | None],
) -> dict[str, float | None]:
    delta: dict[str, float | None] = {}
    for key in set(baseline) | set(current):
        b = baseline.get(key)
        c = current.get(key)
        if b is not None and c is not None:
            delta[key] = round(c - b, 6)
        else:
            delta[key] = None
    return delta


def build_trend_report(
    reports: list[Mapping[str, Any]],
    *,
    coverage_threshold: float = -0.02,
    reader_threshold: float = -0.05,
) -> dict[str, Any]:
    """Build a trend report comparing the last report against all previous.

    Parameters
    ----------
    reports : list of self-check JSON payloads (oldest first).
    coverage_threshold : max allowed drop in coverage ratio (negative = tolerate small drops).
    reader_threshold : max allowed drop in reader metric ratio.

    Returns
    -------
    dict payload with schema_version, per-version aggregates, deltas, flags.
    """
    if not reports:
        return {"schema_version": SCHEMA_VERSION, "error": "no_reports"}

    version_entries: list[dict[str, Any]] = []
    for index, payload in enumerate(reports):
        samples = _extract_samples(payload)
        cov_agg = _aggregate_metrics(samples, COVERAGE_METRICS, _sample_coverage)
        rdr_agg = _aggregate_metrics(samples, READER_METRICS, _sample_reader)
        version_entries.append(
            {
                "index": index,
                "version": payload.get("version") or payload.get("schema_version") or f"report-{index}",
                "generated_at": payload.get("generated_at"),
                "sample_count": len(samples),
                "coverage": cov_agg,
                "reader": rdr_agg,
            }
        )

    flags: list[str] = []
    if len(version_entries) >= 2:
        prev = version_entries[-2]
        curr = version_entries[-1]
        for metric in COVERAGE_METRICS:
            prev_avg = (prev["coverage"].get(metric) or {}).get("avg")
            curr_avg = (curr["coverage"].get(metric) or {}).get("avg")
            if prev_avg is not None and curr_avg is not None:
                drop = curr_avg - prev_avg
                if drop < coverage_threshold:
                    flags.append(f"coverage_regression:{metric}:{drop:+.4f}")
        for metric in READER_METRICS:
            prev_avg = (prev["reader"].get(metric) or {}).get("avg")
            curr_avg = (curr["reader"].get(metric) or {}).get("avg")
            if prev_avg is not None and curr_avg is not None:
                drop = curr_avg - prev_avg
                if drop < reader_threshold:
                    flags.append(f"reader_regression:{metric}:{drop:+.4f}")

    return {
        "schema_version": SCHEMA_VERSION,
        "report_count": len(version_entries),
        "versions": version_entries,
        "flags": flags,
        "passed": len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        f"# Coverage / Reader Trend Report",
        "",
        f"- schema_version: `{payload.get('schema_version')}`",
        f"- report_count: {payload.get('report_count', 0)}",
        f"- passed: `{payload.get('passed')}`",
        f"- flags: {json.dumps(payload.get('flags') or [], ensure_ascii=False)}",
        "",
    ]
    for entry in payload.get("versions") or []:
        lines.append(f"## {entry.get('version')}")
        lines.append(f"- generated_at: `{entry.get('generated_at')}`")
        lines.append(f"- sample_count: {entry.get('sample_count', 0)}")
        lines.append("")
        lines.append("| metric | min | max | avg |")
        lines.append("| --- | ---: | ---: | ---: |")
        for metric, stats in (entry.get("coverage") or {}).items():
            lines.append(
                "| {m} | {mn} | {mx} | {a} |".format(
                    m=metric,
                    mn=_fmt(stats.get("min")),
                    mx=_fmt(stats.get("max")),
                    a=_fmt(stats.get("avg")),
                )
            )
        for metric, stats in (entry.get("reader") or {}).items():
            lines.append(
                "| {m} | {mn} | {mx} | {a} |".format(
                    m=metric,
                    mn=_fmt(stats.get("min")),
                    mx=_fmt(stats.get("max")),
                    a=_fmt(stats.get("avg")),
                )
            )
        lines.append("")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "-"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ParseCore coverage / reader trend report")
    parser.add_argument("reports", nargs="+", help="Paths to self-check JSON reports (oldest first)")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument("--out-json", help="Optional JSON summary output path")
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=-0.02,
        help="Max allowed coverage ratio drop (default: -0.02)",
    )
    parser.add_argument(
        "--reader-threshold",
        type=float,
        default=-0.05,
        help="Max allowed reader metric drop (default: -0.05)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    reports: list[dict[str, Any]] = []
    for path in args.reports:
        reports.append(json.loads(Path(path).read_text(encoding="utf-8")))

    payload = build_trend_report(
        reports,
        coverage_threshold=args.coverage_threshold,
        reader_threshold=args.reader_threshold,
    )
    md = render_markdown(payload)
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    else:
        print(md)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
