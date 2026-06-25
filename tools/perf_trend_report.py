"""Render a compact Markdown/JSON report from a ParseCore perf self-check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PERF_COLUMNS = (
    "elapsed_s",
    "ocr_total_s",
    "call_s",
    "provider_s",
    "rec_s",
    "max_page_ocr_s",
    "peak_memory_mb",
    "throughput_mb_s",
    "part_throughput_s",
)

EXTENDED_METRICS = {
    "peak_memory_mb": "peak_memory",
    "throughput_mb_s": "file_size_mb / elapsed_s",
    "part_throughput_s": "part_count / total_elapsed_s",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render ParseCore perf trend report")
    parser.add_argument("report", help="Path to a self-check JSON report")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument("--out-json", help="Optional compact JSON summary output path")
    parser.add_argument(
        "--trend-reports",
        nargs="+",
        help="Multiple self-check JSON reports for multi-version trend analysis",
    )
    return parser


def _load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sample_metric(sample: dict[str, Any], metric_name: str) -> int | float | None:
    if metric_name == "elapsed_s":
        value = sample.get("elapsed_s")
        return value if isinstance(value, (int, float)) else None
    ocr_metric = (sample.get("ocr_metrics") or {}).get(metric_name)
    if isinstance(ocr_metric, dict):
        value = ocr_metric.get("current")
        return value if isinstance(value, (int, float)) else None
    metric = (sample.get("metrics") or {}).get(metric_name)
    if isinstance(metric, dict):
        value = metric.get("current")
        return value if isinstance(value, (int, float)) else None
    return None


def _format_metric(value: Any, *, digits: int = 3) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return ""


def _comparison_delta(sample: dict[str, Any], metric_name: str) -> int | float | None:
    metrics = sample.get("metrics") or {}
    metric = metrics.get(metric_name)
    if not isinstance(metric, dict):
        return None
    value = metric.get("delta")
    return value if isinstance(value, (int, float)) else None


def build_summary(payload: dict[str, Any]) -> dict[str, Any]:
    perf_tracking = payload.get("perf_tracking") or {}
    samples = [
        sample for sample in (perf_tracking.get("samples") or []) if isinstance(sample, dict)
    ]
    comparison = perf_tracking.get("comparison") or {}
    checks = [item for item in (payload.get("checks") or []) if isinstance(item, dict)]
    return {
        "status": payload.get("status"),
        "profile": payload.get("profile"),
        "suite": payload.get("suite"),
        "generated_at": payload.get("generated_at"),
        "regression_timeout_seconds": payload.get("regression_timeout_seconds"),
        "overview": perf_tracking.get("overview") or {"sample_count": len(samples)},
        "samples": samples,
        "comparison": comparison,
        "checks": checks,
    }


def build_trend_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a cross-version trend summary from 2+ self-check JSON reports."""
    if len(reports) < 2:
        return {"available": False, "reason": "need_at_least_2_reports"}

    versions: list[dict[str, Any]] = []
    for report in reports:
        summary = build_summary(report)
        overview = summary.get("overview") or {}
        slowest = overview.get("slowest_sample") or {}
        versions.append({
            "version": report.get("version") or report.get("generated_at") or report.get("timestamp") or "?",
            "status": summary.get("status"),
            "elapsed_s_p50": _extract_elapsed_p50(summary.get("samples") or []),
            "elapsed_s_p95": _extract_elapsed_p95(summary.get("samples") or []),
            "peak_memory_mb": overview.get("peak_memory_mb"),
            "slowest_sample": slowest.get("name"),
        })

    elapsed_values = [v["elapsed_s_p50"] for v in versions if v["elapsed_s_p50"] is not None]
    trend_direction = "stable"
    change_pct: float | None = None
    if len(elapsed_values) >= 2:
        first, last = elapsed_values[0], elapsed_values[-1]
        if first > 0:
            change_pct = round((last - first) / first * 100, 1)
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
        "elapsed_s_p50_change_pct": change_pct,
    }


def _extract_elapsed_p50(samples: list[dict[str, Any]]) -> float | None:
    values = sorted(
        float(s.get("elapsed_s"))
        for s in samples
        if isinstance(s.get("elapsed_s"), (int, float))
    )
    if not values:
        return None
    mid = len(values) // 2
    return values[mid]


def _extract_elapsed_p95(samples: list[dict[str, Any]]) -> float | None:
    values = sorted(
        float(s.get("elapsed_s"))
        for s in samples
        if isinstance(s.get("elapsed_s"), (int, float))
    )
    if not values:
        return None
    idx = max(0, int(len(values) * 0.95) - 1)
    return values[min(idx, len(values) - 1)]


def render_markdown(payload: dict[str, Any], *, trend_reports: list[dict[str, Any]] | None = None) -> str:
    summary = build_summary(payload)
    lines: list[str] = [
        "# ParseCore Perf Gate",
        "",
        f"- status: **{summary.get('status')}**",
        f"- profile: `{summary.get('profile')}`",
        f"- suite: `{summary.get('suite')}`",
        f"- generated_at: {summary.get('generated_at')}",
    ]
    timeout_s = summary.get("regression_timeout_seconds")
    if timeout_s:
        lines.append(f"- regression_timeout_s: {timeout_s}")
    lines.append("")

    samples = summary["samples"]
    overview = summary["overview"]
    if samples:
        lines.extend(
            [
                "## Perf Samples",
                "",
                "| sample | elapsed_s | ocr_total_s | call_s | provider_s | rec_s | max_page_ocr_s | peak_memory_mb | throughput_mb_s | part_throughput_s |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for sample in samples:
            values = {
                metric: _format_metric(
                    _sample_metric(sample, metric),
                    digits=1 if metric == "elapsed_s" else 3,
                )
                for metric in PERF_COLUMNS
            }
            lines.append(
                "| {name} | {elapsed_s} | {ocr_total_s} | {call_s} | {provider_s} | {rec_s} | {max_page_ocr_s} | {peak_memory_mb} | {throughput_mb_s} | {part_throughput_s} |".format(
                    name=sample.get("name", "?"),
                    **values,
                )
            )
        slowest = overview.get("slowest_sample") or {}
        hottest = overview.get("highest_ocr_total_sample") or {}
        if slowest:
            lines.extend(
                [
                    "",
                    f"- slowest_sample: `{slowest.get('name')}` ({_format_metric(slowest.get('elapsed_s'), digits=1)}s)",
                ]
            )
        if hottest:
            lines.append(
                f"- highest_ocr_total_sample: `{hottest.get('name')}` ({_format_metric(hottest.get('ocr_total_s'))}s)"
            )
        lines.append("")

    comparison = summary["comparison"]
    if isinstance(comparison, dict) and comparison.get("available"):
        lines.extend(
            [
                "## Perf Deltas Vs Previous Report",
                "",
                "| sample | delta_elapsed_s | delta_ocr_total_s | delta_call_s | delta_provider_s | delta_rec_s | delta_max_page_ocr_s | delta_peak_memory_mb | delta_throughput_mb_s | delta_part_throughput_s |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for sample in comparison.get("samples", []) or []:
            values = {
                metric: _format_metric(
                    _comparison_delta(sample, metric),
                    digits=1 if metric == "elapsed_s" else 3,
                )
                for metric in PERF_COLUMNS
            }
            lines.append(
                "| {name} | {elapsed_s} | {ocr_total_s} | {call_s} | {provider_s} | {rec_s} | {max_page_ocr_s} | {peak_memory_mb} | {throughput_mb_s} | {part_throughput_s} |".format(
                    name=sample.get("name", "?"),
                    **values,
                )
            )
        lines.extend(["", f"- compare_report: `{comparison.get('compare_report')}`", ""])
    elif isinstance(comparison, dict) and comparison:
        lines.extend(
            [
                "## Perf Comparison",
                "",
                f"- compare_report: `{comparison.get('compare_report')}`",
                f"- status: unavailable ({comparison.get('reason', 'no comparable samples')})",
                "",
            ]
        )

    lines.extend(
        [
            "## Checks",
            "",
            "| name | status | elapsed_s | summary |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in summary["checks"]:
        name = item.get("name", "?")
        status = item.get("status", "?")
        elapsed = item.get("elapsed_s", "")
        item_summary = str(item.get("summary") or "").replace("|", "\\|")
        lines.append(f"| {name} | {status} | {elapsed} | {item_summary} |")

    structure = None
    for item in summary["checks"]:
        details = item.get("details") or {}
        if details.get("structure_metrics"):
            structure = details["structure_metrics"]
            break
    if structure:
        lines.extend(["", "## Structure Metrics", "", "| metric | value |", "| --- | ---: |"])
        for key in ("toc_recog", "chapter_cov", "noise_ratio", "heading_bind", "evidence_bind"):
            if key in structure:
                lines.append(f"| {key} | {float(structure[key]):.4f} |")

    # P6-T07: multi-version trend section
    if trend_reports and len(trend_reports) >= 2:
        trend = build_trend_summary(trend_reports)
        if trend.get("available"):
            lines.extend(
                [
                    "",
                    "## Multi-Version Trend",
                    "",
                    "| version | status | elapsed_s_p50 | elapsed_s_p95 | peak_memory_mb | trend |",
                    "| --- | --- | ---: | ---: | ---: | --- |",
                ]
            )
            for version in trend["versions"]:
                lines.append(
                    "| {version} | {status} | {p50} | {p95} | {mem} | - |".format(
                        version=version.get("version", "?"),
                        status=version.get("status", "?"),
                        p50=_format_metric(version.get("elapsed_s_p50"), digits=1),
                        p95=_format_metric(version.get("elapsed_s_p95"), digits=1),
                        mem=_format_metric(version.get("peak_memory_mb"), digits=1),
                    )
                )
            change = trend.get("elapsed_s_p50_change_pct")
            change_label = f"{change:+.1f}%" if change is not None else "n/a"
            lines.extend(
                [
                    "",
                    f"- trend_direction: **{trend.get('trend_direction', 'stable')}**",
                    f"- elapsed_s_p50 change: {change_label}",
                    "",
                ]
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = _load_report(args.report)
    trend_payload: list[dict[str, Any]] | None = None
    if args.trend_reports:
        trend_payload = [payload] + [_load_report(p) for p in args.trend_reports]
    markdown = render_markdown(payload, trend_reports=trend_payload)
    if args.out_md:
        Path(args.out_md).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    if args.out_json:
        summary = build_summary(payload)
        if trend_payload and len(trend_payload) >= 2:
            summary["trend"] = build_trend_summary(trend_payload)
        Path(args.out_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
