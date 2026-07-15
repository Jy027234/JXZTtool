"""Render compact trends from ParseCore perf self-checks or baseline reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


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


BYTES_PER_MIB = 1024 * 1024


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _compact_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _percentile(values: Iterable[float], ratio: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _numeric_summary(values: Iterable[float]) -> dict[str, int | float | None]:
    samples = [float(value) for value in values]
    return {
        "count": len(samples),
        "p50": _compact_number(_percentile(samples, 0.50)),
        "max": _compact_number(max(samples)) if samples else None,
        "sum": _compact_number(sum(samples)) if samples else None,
    }


def _unique_texts(values: Iterable[Any]) -> list[str]:
    return sorted({str(value) for value in values if value not in (None, "")})


def _unique_numbers(values: Iterable[Any]) -> list[int | float]:
    numbers = [_as_number(value) for value in values]
    return sorted(
        {
            _compact_number(number)
            for number in numbers
            if number is not None
        },
        key=float,
    )


def _report_kind(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("perf_tracking"), Mapping):
        return "self_check"
    if isinstance(payload.get("measurement"), Mapping) and isinstance(payload.get("results"), list):
        return "parse_perf_baseline"
    return "unknown"


def _baseline_results(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        result
        for result in (payload.get("results") or [])
        if isinstance(result, Mapping)
    ]


def _sample_signature(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    samples: set[tuple[str, int | float | None]] = set()
    for result in results:
        file_name = result.get("file_name") or result.get("document")
        if not isinstance(file_name, str) or not file_name:
            continue
        size_bytes = _compact_number(_as_number(result.get("size_bytes")))
        samples.add((file_name, size_bytes))
    return [
        {"file_name": file_name, "size_bytes": size_bytes}
        for file_name, size_bytes in sorted(
            samples,
            key=lambda item: (
                item[0],
                item[1] is None,
                float(item[1]) if item[1] is not None else -1.0,
            ),
        )
    ]


def _measurement_channel(payload: Mapping[str, Any]) -> dict[str, Any]:
    measurement = _as_mapping(payload.get("measurement"))
    cache = _as_mapping(measurement.get("cache"))
    lifecycle = _as_mapping(measurement.get("runtime_lifecycle"))
    track_python_memory = measurement.get("track_python_memory")
    return {
        "lane": measurement.get("lane") if isinstance(measurement.get("lane"), str) else None,
        "track_python_memory": (
            track_python_memory if isinstance(track_python_memory, bool) else None
        ),
        "elapsed_scope": (
            measurement.get("elapsed_scope")
            if isinstance(measurement.get("elapsed_scope"), str)
            else None
        ),
        "cache_mode": cache.get("mode") if isinstance(cache.get("mode"), str) else None,
        "reuse_runtime": (
            lifecycle.get("reuse_runtime")
            if isinstance(lifecycle.get("reuse_runtime"), bool)
            else None
        ),
        "warmup_runs": _compact_number(_as_number(lifecycle.get("warmup_runs"))),
        "sample_signature": _sample_signature(_baseline_results(payload)),
    }


def _channel_is_complete(channel: Mapping[str, Any]) -> bool:
    return all(
        channel.get(field) is not None
        for field in (
            "lane",
            "track_python_memory",
            "elapsed_scope",
            "cache_mode",
            "reuse_runtime",
            "warmup_runs",
        )
    ) and bool(channel.get("sample_signature"))


def _channel_key(channel: Mapping[str, Any]) -> str:
    return json.dumps(channel, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render ParseCore self-check or parse-baseline performance trends"
    )
    parser.add_argument("report", help="Path to a self-check or parse-baseline JSON report")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument("--out-json", help="Optional compact JSON summary output path")
    parser.add_argument(
        "--trend-reports",
        nargs="+",
        help="Matching self-check or parse-baseline JSON reports for multi-version analysis",
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


def _telemetry_metric(
    telemetry: Mapping[str, Any], metric_name: str, field: str
) -> int | float | None:
    metric = _as_mapping(telemetry.get(metric_name))
    return _compact_number(_as_number(metric.get(field)))


def build_process_telemetry_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate per-run psutil telemetry from a parse performance baseline."""
    results = _baseline_results(payload)
    eligible_results = [
        result for result in results if str(result.get("status") or "").lower() != "failed"
    ]
    telemetry = [
        _as_mapping(result.get("process_telemetry"))
        for result in eligible_results
        if _as_mapping(result.get("process_telemetry")).get("status") == "available"
    ]
    summary: dict[str, Any] = {
        "status": "available" if telemetry else "unavailable",
        "total_runs": len(results),
        "eligible_runs": len(eligible_results),
        "failed_runs": len(results) - len(eligible_results),
        "available_runs": len(telemetry),
        "missing_runs": len(eligible_results) - len(telemetry),
    }
    if not telemetry:
        summary["reason"] = "no_available_process_telemetry"
        return summary

    def metric_values(section: str, key: str) -> list[float]:
        values: list[float] = []
        for item in telemetry:
            value = _as_number(_as_mapping(item.get(section)).get(key))
            if value is not None:
                values.append(value)
        return values

    summary.update(
        {
            "collectors": _unique_texts(item.get("collector") for item in telemetry),
            "working_set_semantics": _unique_texts(
                item.get("working_set_semantics") for item in telemetry
            ),
            "process_scopes": _unique_texts(item.get("scope") for item in telemetry),
            "sample_intervals_ms": _unique_numbers(
                item.get("sample_interval_ms") for item in telemetry
            ),
            "peak_rss_bytes": _numeric_summary(metric_values("peak", "rss_bytes")),
            "peak_working_set_bytes": _numeric_summary(
                metric_values("peak", "working_set_bytes")
            ),
            "peak_vms_bytes": _numeric_summary(metric_values("peak", "vms_bytes")),
            "cpu_total_s": _numeric_summary(metric_values("delta", "cpu_total_s")),
            "io_read_bytes": _numeric_summary(metric_values("delta", "io_read_bytes")),
            "io_write_bytes": _numeric_summary(metric_values("delta", "io_write_bytes")),
        }
    )
    return summary


def _process_telemetry_channel(
    payload: Mapping[str, Any], telemetry: Mapping[str, Any]
) -> dict[str, Any]:
    measurement = _as_mapping(payload.get("measurement"))
    declared = _as_mapping(measurement.get("process_telemetry"))
    return {
        **_measurement_channel(payload),
        "declared_sample_interval_ms": _compact_number(
            _as_number(declared.get("sample_interval_ms"))
        ),
        "observed_sample_intervals_ms": list(telemetry.get("sample_intervals_ms") or []),
        "collectors": list(telemetry.get("collectors") or []),
        "working_set_semantics": list(telemetry.get("working_set_semantics") or []),
        "process_scopes": list(telemetry.get("process_scopes") or []),
    }


def _process_telemetry_channel_is_complete(channel: Mapping[str, Any]) -> bool:
    return _channel_is_complete(channel) and all(
        channel.get(field)
        for field in (
            "declared_sample_interval_ms",
            "observed_sample_intervals_ms",
            "collectors",
            "working_set_semantics",
            "process_scopes",
        )
    )


def build_summary(payload: dict[str, Any]) -> dict[str, Any]:
    report_kind = _report_kind(payload)
    if report_kind == "parse_perf_baseline":
        baseline_summary = dict(_as_mapping(payload.get("summary")))
        stability = _as_mapping(payload.get("stability"))
        results = _baseline_results(payload)
        baseline_summary.setdefault("sample_count", len(results))
        return {
            "report_kind": report_kind,
            "status": payload.get("status"),
            "profile": _as_mapping(payload.get("measurement")).get("lane"),
            "suite": payload.get("config"),
            "generated_at": payload.get("generated_at"),
            "regression_timeout_seconds": None,
            "overview": baseline_summary,
            "baseline_summary": baseline_summary,
            "samples": results,
            "comparison": {},
            "checks": [
                item for item in (stability.get("gates") or []) if isinstance(item, Mapping)
            ],
            "measurement": dict(_as_mapping(payload.get("measurement"))),
            "process_telemetry": build_process_telemetry_summary(payload),
            "stage_timings_s": stability.get("stage_timings_s") or {},
        }

    perf_tracking = payload.get("perf_tracking") or {}
    samples = [
        sample for sample in (perf_tracking.get("samples") or []) if isinstance(sample, dict)
    ]
    comparison = perf_tracking.get("comparison") or {}
    checks = [item for item in (payload.get("checks") or []) if isinstance(item, dict)]
    return {
        "report_kind": report_kind,
        "status": payload.get("status"),
        "profile": payload.get("profile"),
        "suite": payload.get("suite"),
        "generated_at": payload.get("generated_at"),
        "regression_timeout_seconds": payload.get("regression_timeout_seconds"),
        "overview": perf_tracking.get("overview") or {"sample_count": len(samples)},
        "samples": samples,
        "comparison": comparison,
        "checks": checks,
        "measurement": {},
        "process_telemetry": {"status": "unavailable", "reason": "not_a_baseline_report"},
    }


def _report_version(report: Mapping[str, Any]) -> Any:
    return report.get("version") or report.get("generated_at") or report.get("timestamp") or "?"


def _latency_trend_direction(first: float, last: float) -> str:
    if last > first * 1.1:
        return "regressing"
    if last < first * 0.9:
        return "improving"
    return "stable"


def _change_pct(first: float, last: float) -> float | None:
    return round((last - first) / first * 100, 1) if first > 0 else None


def _stage_timing_p50s(payload: Mapping[str, Any]) -> dict[str, int | float]:
    stability = _as_mapping(payload.get("stability"))
    timings = _as_mapping(stability.get("stage_timings_s"))
    result: dict[str, int | float] = {}
    for stage, raw_summary in timings.items():
        if not isinstance(stage, str) or not stage:
            continue
        value = _as_number(_as_mapping(raw_summary).get("p50"))
        if value is not None:
            result[stage] = _compact_number(value) or 0
    return dict(sorted(result.items()))


def _baseline_trend_version(payload: Mapping[str, Any]) -> dict[str, Any]:
    summary = build_summary(dict(payload))
    baseline_summary = _as_mapping(summary.get("baseline_summary"))
    telemetry = _as_mapping(summary.get("process_telemetry"))
    peak_kb = _as_number(baseline_summary.get("max_peak_kb"))
    return {
        "version": _report_version(payload),
        "status": summary.get("status"),
        "lane": _measurement_channel(payload).get("lane"),
        "track_python_memory": _measurement_channel(payload).get("track_python_memory"),
        "cache_mode": _measurement_channel(payload).get("cache_mode"),
        "elapsed_s_p50": _compact_number(_as_number(baseline_summary.get("p50_elapsed_s"))),
        "elapsed_s_p95": _compact_number(_as_number(baseline_summary.get("p95_elapsed_s"))),
        "peak_python_allocation_mb": _compact_number(peak_kb / 1024.0)
        if peak_kb is not None
        else None,
        "peak_rss_bytes_p50": _telemetry_metric(telemetry, "peak_rss_bytes", "p50"),
        "peak_rss_bytes_max": _telemetry_metric(telemetry, "peak_rss_bytes", "max"),
        "peak_working_set_bytes_max": _telemetry_metric(
            telemetry, "peak_working_set_bytes", "max"
        ),
        "peak_vms_bytes_max": _telemetry_metric(telemetry, "peak_vms_bytes", "max"),
        "cpu_total_s_sum": _telemetry_metric(telemetry, "cpu_total_s", "sum"),
        "io_read_bytes_sum": _telemetry_metric(telemetry, "io_read_bytes", "sum"),
        "io_write_bytes_sum": _telemetry_metric(telemetry, "io_write_bytes", "sum"),
        "stage_timings_p50_s": _stage_timing_p50s(payload),
        "measurement_channel": _measurement_channel(payload),
        "process_telemetry_channel": _process_telemetry_channel(payload, telemetry),
        "process_telemetry": telemetry,
    }


def build_stage_timing_trend(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare stage P50 timings inside one strictly identical measurement channel."""
    if len(reports) < 2:
        return {"available": False, "reason": "need_at_least_2_reports"}
    if any(_report_kind(report) != "parse_perf_baseline" for report in reports):
        return {
            "available": False,
            "reason": "requires_parse_perf_baseline_reports",
        }

    versions = [_baseline_trend_version(report) for report in reports]
    result: dict[str, Any] = {
        "available": False,
        "observation_only": True,
        "version_count": len(versions),
        "versions": [
            {
                "version": version.get("version"),
                "stage_timings_p50_s": version.get("stage_timings_p50_s") or {},
            }
            for version in versions
        ],
    }
    channels = [_as_mapping(version.get("measurement_channel")) for version in versions]
    if any(not _channel_is_complete(channel) for channel in channels):
        result["reason"] = "incomplete_measurement_channel"
        return result
    if len({_channel_key(channel) for channel in channels}) != 1:
        result["reason"] = "incompatible_measurement_channels"
        return result

    stage_maps = [
        _as_mapping(version.get("stage_timings_p50_s")) for version in versions
    ]
    if any(not stage_map for stage_map in stage_maps):
        result["reason"] = "stage_timings_unavailable"
        return result
    stage_sets = [set(stage_map) for stage_map in stage_maps]
    common_stages = sorted(set.intersection(*stage_sets))
    if not common_stages:
        result["reason"] = "no_common_stage_timings"
        return result

    all_stages = sorted(set.union(*stage_sets))
    comparisons: dict[str, dict[str, Any]] = {}
    for stage in common_stages:
        first = _as_number(stage_maps[0].get(stage))
        last = _as_number(stage_maps[-1].get(stage))
        if first is None or last is None:
            continue
        comparisons[stage] = {
            "p50_s_first": _compact_number(first),
            "p50_s_last": _compact_number(last),
            "change_pct": _change_pct(first, last),
            "direction": _latency_trend_direction(first, last),
        }
    if not comparisons:
        result["reason"] = "common_stage_values_unavailable"
        return result

    missing_by_version = {
        str(version.get("version")): sorted(set(all_stages) - stage_sets[index])
        for index, version in enumerate(versions)
        if set(all_stages) - stage_sets[index]
    }
    return {
        **result,
        "available": True,
        "measurement_channel": channels[0],
        "comparison_scope": "common_stages",
        "stage_set_consistent": len({tuple(sorted(items)) for items in stage_sets}) == 1,
        "common_stage_count": len(comparisons),
        "stages": comparisons,
        "missing_stages_by_version": missing_by_version,
    }


def build_process_telemetry_trend(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare RSS only inside an identical baseline measurement channel.

    RSS values are intentionally observation-only: the function never maps them to a
    release decision or an alert threshold.
    """
    if len(reports) < 2:
        return {"available": False, "reason": "need_at_least_2_reports"}
    if any(_report_kind(report) != "parse_perf_baseline" for report in reports):
        return {
            "available": False,
            "reason": "requires_parse_perf_baseline_reports",
        }

    versions = [_baseline_trend_version(report) for report in reports]
    result: dict[str, Any] = {
        "available": False,
        "observation_only": True,
        "version_count": len(versions),
        "versions": versions,
    }
    if any(
        _as_mapping(version.get("process_telemetry")).get("status") != "available"
        for version in versions
    ):
        result["reason"] = "process_telemetry_unavailable"
        return result

    channels = [
        _as_mapping(version.get("process_telemetry_channel")) for version in versions
    ]
    if any(not _process_telemetry_channel_is_complete(channel) for channel in channels):
        result["reason"] = "incomplete_process_telemetry_channel"
        return result
    if len({_channel_key(channel) for channel in channels}) != 1:
        result["reason"] = "incompatible_process_telemetry_channels"
        return result

    rss_values = [_as_number(version.get("peak_rss_bytes_max")) for version in versions]
    if any(value is None for value in rss_values):
        result["reason"] = "peak_rss_unavailable"
        return result
    first, last = float(rss_values[0]), float(rss_values[-1])
    direction = "unchanged"
    if last > first:
        direction = "increased"
    elif last < first:
        direction = "decreased"
    return {
        **result,
        "available": True,
        "measurement_channel": channels[0],
        "peak_rss_bytes_max_first": _compact_number(first),
        "peak_rss_bytes_max_last": _compact_number(last),
        "peak_rss_bytes_max_change_pct": _change_pct(first, last),
        "peak_rss_direction": direction,
    }


def _build_baseline_trend(reports: list[dict[str, Any]]) -> dict[str, Any]:
    versions = [_baseline_trend_version(report) for report in reports]
    telemetry_trend = build_process_telemetry_trend(reports)
    stage_timing_trend = build_stage_timing_trend(reports)
    result: dict[str, Any] = {
        "available": False,
        "source_kind": "parse_perf_baseline",
        "version_count": len(versions),
        "versions": versions,
        "process_telemetry": telemetry_trend,
        "stage_timings": stage_timing_trend,
    }
    channels = [_as_mapping(version.get("measurement_channel")) for version in versions]
    if any(not _channel_is_complete(channel) for channel in channels):
        result["reason"] = "incomplete_measurement_channel"
        return result
    if len({_channel_key(channel) for channel in channels}) != 1:
        result["reason"] = "incompatible_measurement_channels"
        return result

    elapsed_values = [_as_number(version.get("elapsed_s_p50")) for version in versions]
    if any(value is None for value in elapsed_values):
        result["reason"] = "elapsed_s_p50_unavailable"
        return result
    first, last = float(elapsed_values[0]), float(elapsed_values[-1])
    return {
        **result,
        "available": True,
        "measurement_channel": channels[0],
        "trend_direction": _latency_trend_direction(first, last),
        "elapsed_s_p50_first": _compact_number(first),
        "elapsed_s_p50_last": _compact_number(last),
        "elapsed_s_p50_change_pct": _change_pct(first, last),
    }


def build_trend_summary(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a cross-version trend summary from compatible performance reports."""
    if len(reports) < 2:
        return {"available": False, "reason": "need_at_least_2_reports"}

    report_kinds = {_report_kind(report) for report in reports}
    if report_kinds == {"parse_perf_baseline"}:
        return _build_baseline_trend(reports)
    if "parse_perf_baseline" in report_kinds:
        return {
            "available": False,
            "reason": "mixed_report_kinds",
            "report_kinds": sorted(report_kinds),
        }

    versions: list[dict[str, Any]] = []
    for report in reports:
        summary = build_summary(report)
        overview = summary.get("overview") or {}
        slowest = overview.get("slowest_sample") or {}
        versions.append({
            "version": _report_version(report),
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
        trend_direction = _latency_trend_direction(first, last)

    return {
        "available": True,
        "source_kind": "self_check",
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


def _format_mib(value: Any) -> str:
    number = _as_number(value)
    return "n/a" if number is None else f"{number / BYTES_PER_MIB:.1f}"


def _format_channel(channel: Mapping[str, Any]) -> str:
    samples = [
        str(item.get("file_name"))
        for item in (channel.get("sample_signature") or [])
        if isinstance(item, Mapping) and item.get("file_name")
    ]
    return "; ".join(
        (
            f"lane={channel.get('lane')}",
            f"track_python_memory={channel.get('track_python_memory')}",
            f"cache_mode={channel.get('cache_mode')}",
            f"reuse_runtime={channel.get('reuse_runtime')}",
            f"warmup_runs={channel.get('warmup_runs')}",
            f"samples={','.join(samples) or 'unknown'}",
        )
    )


def _render_process_telemetry(telemetry: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Process Telemetry (Observation Only)",
        "",
        f"- status: `{telemetry.get('status')}`",
        f"- measured runs: {telemetry.get('available_runs', 0)}/{telemetry.get('eligible_runs', 0)}",
    ]
    if telemetry.get("status") != "available":
        lines.extend(
            [
                f"- reason: `{telemetry.get('reason', 'no_available_process_telemetry')}`",
                "- RSS is unavailable for this report; no RSS trend is calculated.",
                "",
            ]
        )
        return lines

    lines.extend(
        [
            f"- collector: `{', '.join(telemetry.get('collectors') or [])}`",
            f"- working_set_semantics: `{', '.join(telemetry.get('working_set_semantics') or [])}`",
            f"- sample_interval_ms: `{', '.join(str(value) for value in (telemetry.get('sample_intervals_ms') or []))}`",
            "",
            "| observed runs | RSS P50 MiB | RSS max MiB | working set max MiB | VMS max MiB | CPU total s (sum) | IO read MiB (sum) | IO write MiB (sum) |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            "| {runs} | {rss_p50} | {rss_max} | {working_set_max} | {vms_max} | {cpu_total} | {io_read} | {io_write} |".format(
                runs=telemetry.get("available_runs", 0),
                rss_p50=_format_mib(_telemetry_metric(telemetry, "peak_rss_bytes", "p50")),
                rss_max=_format_mib(_telemetry_metric(telemetry, "peak_rss_bytes", "max")),
                working_set_max=_format_mib(
                    _telemetry_metric(telemetry, "peak_working_set_bytes", "max")
                ),
                vms_max=_format_mib(_telemetry_metric(telemetry, "peak_vms_bytes", "max")),
                cpu_total=_format_metric(
                    _telemetry_metric(telemetry, "cpu_total_s", "sum")
                ),
                io_read=_format_mib(_telemetry_metric(telemetry, "io_read_bytes", "sum")),
                io_write=_format_mib(_telemetry_metric(telemetry, "io_write_bytes", "sum")),
            ),
            "",
            "- RSS is an observation channel only: it does not create an alert, change a release decision, or replace the Python allocation budget.",
            "",
        ]
    )
    return lines


def _render_current_stage_timings(stage_timings: Mapping[str, Any]) -> list[str]:
    lines = ["## Stage Timings", ""]
    rows: list[tuple[str, Mapping[str, Any]]] = [
        (str(stage), _as_mapping(summary))
        for stage, summary in sorted(stage_timings.items())
        if isinstance(stage, str) and isinstance(summary, Mapping)
    ]
    if not rows:
        return lines + ["- unavailable: `stage_timings_unavailable`", ""]
    lines.extend(
        [
            "| stage | runs | P50 s | P95 s | max s | CV % |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for stage, summary in rows:
        lines.append(
            "| {stage} | {count} | {p50} | {p95} | {maximum} | {cv} |".format(
                stage=stage,
                count=summary.get("count", 0),
                p50=_format_metric(summary.get("p50")),
                p95=_format_metric(summary.get("p95")),
                maximum=_format_metric(summary.get("max")),
                cv=_format_metric(summary.get("cv_pct")),
            )
        )
    lines.extend(["", "- Stage timings are observations; they do not add a release threshold.", ""])
    return lines


def _render_stage_timing_trend(trend: Mapping[str, Any]) -> list[str]:
    lines = ["### Stage Timing Trend", ""]
    if not trend.get("available"):
        return lines + [
            f"- comparison: unavailable (`{trend.get('reason', 'unknown')}`).",
            "- Stage timings from nonmatching measurement channels are never aggregated.",
            "",
        ]
    lines.extend(
        [
            "| stage | first P50 s | last P50 s | change | direction |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for stage, comparison in sorted(_as_mapping(trend.get("stages")).items()):
        item = _as_mapping(comparison)
        change = item.get("change_pct")
        change_label = f"{float(change):+.1f}%" if isinstance(change, (int, float)) else "n/a"
        lines.append(
            "| {stage} | {first} | {last} | {change} | {direction} |".format(
                stage=stage,
                first=_format_metric(item.get("p50_s_first")),
                last=_format_metric(item.get("p50_s_last")),
                change=change_label,
                direction=item.get("direction", "stable"),
            )
        )
    missing = _as_mapping(trend.get("missing_stages_by_version"))
    lines.extend(
        [
            "",
            f"- compared common stages: {trend.get('common_stage_count', 0)}; stage_set_consistent: `{trend.get('stage_set_consistent')}`.",
            f"- missing_stages_by_version: `{json.dumps(missing, ensure_ascii=False, sort_keys=True)}`.",
            "- Stage timing changes are observation-only and do not change the release decision.",
            "",
        ]
    )
    return lines


def _render_baseline_trend(trend_reports: list[dict[str, Any]]) -> list[str]:
    trend = build_trend_summary(trend_reports)
    lines = ["## Multi-Version Trend", ""]
    versions = trend.get("versions") or []
    if versions:
        lines.extend(
            [
                "| version | status | lane | Python memory tracking | cache mode | elapsed P50 s | elapsed P95 s | RSS max MiB |",
                "| --- | --- | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for version in versions:
            lines.append(
                "| {version} | {status} | {lane} | {tracking} | {cache_mode} | {p50} | {p95} | {rss_max} |".format(
                    version=version.get("version", "?"),
                    status=version.get("status", "?"),
                    lane=version.get("lane", "?"),
                    tracking=version.get("track_python_memory", "?"),
                    cache_mode=version.get("cache_mode", "?"),
                    p50=_format_metric(version.get("elapsed_s_p50"), digits=3),
                    p95=_format_metric(version.get("elapsed_s_p95"), digits=3),
                    rss_max=_format_mib(version.get("peak_rss_bytes_max")),
                )
            )
        lines.append("")

    lines.extend(_render_stage_timing_trend(_as_mapping(trend.get("stage_timings"))))

    if not trend.get("available"):
        lines.extend(
            [
                f"- comparison: unavailable (`{trend.get('reason', 'unknown')}`).",
                "- Nonmatching measurement channels remain separate observations; no aggregate latency or RSS trend is calculated.",
                "",
            ]
        )
        return lines

    change = trend.get("elapsed_s_p50_change_pct")
    change_label = f"{change:+.1f}%" if change is not None else "n/a"
    lines.extend(
        [
            f"- measurement_channel: `{_format_channel(_as_mapping(trend.get('measurement_channel')))}`",
            f"- latency trend_direction: **{trend.get('trend_direction', 'stable')}**",
            f"- elapsed_s_p50 change: {change_label}",
        ]
    )
    telemetry_trend = _as_mapping(trend.get("process_telemetry"))
    if telemetry_trend.get("available"):
        rss_change = telemetry_trend.get("peak_rss_bytes_max_change_pct")
        rss_change_label = f"{rss_change:+.1f}%" if rss_change is not None else "n/a"
        lines.extend(
            [
                f"- RSS max observation: **{telemetry_trend.get('peak_rss_direction')}** ({rss_change_label})",
                "- RSS observation does not imply a release regression or an alert threshold.",
            ]
        )
    else:
        lines.append(
            f"- RSS trend: unavailable (`{telemetry_trend.get('reason', 'unknown')}`)."
        )
    lines.append("")
    return lines


def _render_baseline_markdown(
    summary: Mapping[str, Any], *, trend_reports: list[dict[str, Any]] | None
) -> str:
    measurement = _as_mapping(summary.get("measurement"))
    cache = _as_mapping(measurement.get("cache"))
    overview = _as_mapping(summary.get("baseline_summary"))
    lines: list[str] = [
        "# ParseCore Parse Performance Trend",
        "",
        f"- status: **{summary.get('status')}**",
        f"- generated_at: {summary.get('generated_at')}",
        f"- lane: `{measurement.get('lane', 'unknown')}`",
        f"- track_python_memory: `{measurement.get('track_python_memory')}`",
        f"- elapsed_scope: `{measurement.get('elapsed_scope', 'unknown')}`",
        f"- cache_mode: `{cache.get('mode', 'unknown')}`",
        "",
        "## Current Measurement",
        "",
        "| samples | elapsed P50 s | elapsed P95 s | elapsed max s | Python allocation max KB |",
        "| ---: | ---: | ---: | ---: | ---: |",
        "| {samples} | {p50} | {p95} | {maximum} | {peak_kb} |".format(
            samples=overview.get("documents", overview.get("sample_count", 0)),
            p50=_format_metric(overview.get("p50_elapsed_s"), digits=3),
            p95=_format_metric(overview.get("p95_elapsed_s"), digits=3),
            maximum=_format_metric(overview.get("max_elapsed_s"), digits=3),
            peak_kb=_format_metric(overview.get("max_peak_kb"), digits=3),
        ),
        "",
    ]
    lines.extend(_render_current_stage_timings(_as_mapping(summary.get("stage_timings_s"))))
    lines.extend(_render_process_telemetry(_as_mapping(summary.get("process_telemetry"))))
    if trend_reports and len(trend_reports) >= 2:
        lines.extend(_render_baseline_trend(trend_reports))
    return "\n".join(lines) + "\n"


def render_markdown(payload: dict[str, Any], *, trend_reports: list[dict[str, Any]] | None = None) -> str:
    summary = build_summary(payload)
    if summary.get("report_kind") == "parse_perf_baseline":
        return _render_baseline_markdown(summary, trend_reports=trend_reports)
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
