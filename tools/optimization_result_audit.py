"""Aggregate optimization evidence into a reproducible release audit.

The audit deliberately keeps two timing lanes separate:

* tracked: historical-compatible ``tracemalloc`` runs for elapsed time and
  Python peak-allocation comparison;
* latency: untracked runs for low-overhead latency stability.

Exit codes:
0 -> every release gate passed (observations may still be present)
1 -> at least one release gate failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "2026-07-optimization-result-audit"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate ParseCore optimization audit evidence")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--original-tracked", required=True)
    parser.add_argument("--prior-tracked-stability", required=True)
    parser.add_argument("--current-tracked", action="append", required=True)
    parser.add_argument("--historical-latency", required=True)
    parser.add_argument("--current-latency", action="append", required=True)
    parser.add_argument("--historical-regression", required=True)
    parser.add_argument("--current-regression", required=True)
    parser.add_argument("--self-check", required=True)
    parser.add_argument("--p1-acceptance", required=True)
    parser.add_argument(
        "--stability-policy",
        help="Optional parse-performance stability policy; enables fixed-sample H-02 budgets.",
    )
    parser.add_argument("--out-json")
    parser.add_argument("--out-md")
    return parser


def _load(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _decrease_pct(current: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return _round((baseline - current) / baseline * 100.0)


def _series(values: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    if not samples:
        return {
            "count": 0,
            "values": [],
            "mean": None,
            "p50": None,
            "median": None,
            "p95": None,
            "min": None,
            "max": None,
            "range": None,
            "population_stddev": None,
            "cv_pct": None,
        }
    mean = statistics.fmean(samples)
    deviation = statistics.pstdev(samples)
    ordered = sorted(samples)

    def percentile(ratio: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * ratio
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    p50 = percentile(0.50)
    p95 = percentile(0.95)
    return {
        "count": len(samples),
        "values": [_round(value) for value in samples],
        "mean": _round(mean),
        "p50": _round(p50),
        "median": _round(p50),
        "p95": _round(p95),
        "min": _round(min(samples)),
        "max": _round(max(samples)),
        "range": _round(max(samples) - min(samples)),
        "population_stddev": _round(deviation),
        "cv_pct": _round(deviation / mean * 100.0) if mean else 0.0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _first_result(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError(f"missing first parse result: {source}")
    return results[0]


def _parse_results(payload: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    """Return every measured result, including a multi-run stability artifact."""

    results = payload.get("results")
    if not isinstance(results, list) or not results or not all(isinstance(item, dict) for item in results):
        raise ValueError(f"missing parse results: {source}")
    return list(results)


def _first_fixture(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures or not isinstance(fixtures[0], dict):
        raise ValueError(f"missing first regression fixture: {source}")
    return fixtures[0]


def _result_signature(result: dict[str, Any]) -> dict[str, Any]:
    provider_report = result.get("provider_report") or {}
    provider_summary = provider_report.get("summary") or {}
    rag_coverage = provider_report.get("rag_coverage_quality") or {}
    quality_gate = provider_report.get("quality_gate") or {}
    fingerprint = result.get("fingerprint") or {}
    raw_blocks = int(result.get("raw_blocks") or result.get("blocks") or 0)
    content_blocks = int(
        result.get("content_blocks")
        or fingerprint.get("content_blocks")
        or rag_coverage.get("total_indexable_units")
        or result.get("chunks")
        or 0
    )
    return {
        "result_status": result.get("status"),
        "blocks": raw_blocks,
        "raw_blocks": raw_blocks,
        "content_blocks": content_blocks,
        "chunks": int(result.get("chunks") or 0),
        "tables": int(result.get("tables") or 0),
        "figures": int(result.get("figures") or fingerprint.get("figures") or provider_summary.get("total_figures") or 0),
        "pages": int(result.get("pages") or fingerprint.get("pages") or provider_summary.get("total_pages") or 0),
        "primary_provider_id": result.get("primary_provider_id"),
        "best_provider_id": result.get("best_provider_id"),
        "quality_gate": quality_gate.get("gate"),
        "quality_gate_passed": quality_gate.get("passed"),
        "quality_gate_flags": sorted(str(item) for item in quality_gate.get("flags") or []),
    }


def _all_equal(values: list[dict[str, Any]]) -> bool:
    if not values:
        return False
    first = json.dumps(values[0], sort_keys=True, ensure_ascii=False)
    return all(
        json.dumps(value, sort_keys=True, ensure_ascii=False) == first
        for value in values[1:]
    )


def _regression_summary(fixture: dict[str, Any]) -> dict[str, Any]:
    block_counts = fixture.get("block_counts") or {}
    quality = fixture.get("quality") or {}
    table_quality = fixture.get("table_quality") or {}
    structure_quality = fixture.get("structure_quality") or {}
    structure_counts = structure_quality.get("counts") or {}
    raw_blocks = int(block_counts.get("total") or 0)
    content_blocks = int(quality.get("total_blocks") or raw_blocks)
    return {
        "raw_blocks": raw_blocks,
        "content_blocks": content_blocks,
        "chunks": int(block_counts.get("chunks") or 0),
        "pages": int(quality.get("page_count") or 0),
        "tables": int(table_quality.get("table_block_count") or 0),
        "very_short_ratio": _round(float(quality.get("very_short_ratio") or 0.0), 6),
        "noise_ratio": _round(float(structure_quality.get("noise_ratio") or 0.0), 6),
        "quality_denominator_items": int(
            structure_counts.get("quality_denominator_items") or content_blocks
        ),
        "audit_artifact_items": int(
            structure_counts.get("audit_artifact_items") or max(0, raw_blocks - content_blocks)
        ),
    }


def _gate(name: str, passed: bool, summary: str) -> dict[str, Any]:
    return {"name": name, "status": "passed" if passed else "failed", "summary": summary}


def _load_stability_policy(path: str | Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    resolved = Path(path).resolve()
    payload = _load(resolved)
    return payload, str(resolved)


def _lifecycle_groups(results: Iterable[dict[str, Any]]) -> set[str]:
    groups: set[str] = set()
    for result in results:
        lifecycle = result.get("parser_lifecycle")
        if not isinstance(lifecycle, Mapping):
            groups.add("unknown")
            continue
        cache_state = result.get("cache_state")
        cache_mode = (
            cache_state.get("requested_mode")
            if isinstance(cache_state, Mapping)
            else "unknown"
        )
        groups.add(
            f"{lifecycle.get('mode') or 'unknown'}:{lifecycle.get('phase') or 'unknown'}:cache={cache_mode or 'unknown'}"
        )
    return groups


def _stage_telemetry_summary(results: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = {}
    for result in results:
        stage_timings = result.get("stage_timings")
        if not isinstance(stage_timings, Mapping):
            continue
        for name, value in stage_timings.items():
            try:
                values.setdefault(str(name), []).append(float(value))
            except (TypeError, ValueError):
                continue
    return {name: _series(series) for name, series in sorted(values.items())}


def _process_telemetry_summary(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    entries = [
        result.get("process_telemetry")
        for result in results
        if isinstance(result.get("process_telemetry"), Mapping)
        and result.get("process_telemetry", {}).get("status") == "available"
    ]
    if not entries:
        return {"status": "unavailable", "available_runs": 0}

    def values(section: str, key: str) -> list[float]:
        output: list[float] = []
        for entry in entries:
            mapping = entry.get(section)
            if not isinstance(mapping, Mapping):
                continue
            try:
                output.append(float(mapping.get(key)))
            except (TypeError, ValueError):
                continue
        return output

    return {
        "status": "available",
        "available_runs": len(entries),
        "peak_rss_bytes": _series(values("peak", "rss_bytes")),
        "peak_working_set_bytes": _series(values("peak", "working_set_bytes")),
        "cpu_total_s": _series(values("delta", "cpu_total_s")),
        "io_read_bytes": _series(values("delta", "io_read_bytes")),
        "io_write_bytes": _series(values("delta", "io_write_bytes")),
    }


def _policy_gates(
    *,
    policy: Mapping[str, Any] | None,
    sample_path: Path,
    tracked_results: list[dict[str, Any]],
    latency_results: list[dict[str, Any]],
    tracked_elapsed: Mapping[str, Any],
    tracked_memory: Mapping[str, Any],
    latency_elapsed: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if policy is None:
        return []
    gates: list[dict[str, Any]] = []
    sample_policy = policy.get("sample")
    if isinstance(sample_policy, Mapping) and sample_policy.get("sha256"):
        expected = str(sample_policy.get("sha256") or "").upper()
        actual = _sha256(sample_path) if sample_path.is_file() else None
        gates.append(_gate("stability_policy_sample_identity", actual == expected, f"sha256={actual} expected={expected}"))

    expected_fingerprint = policy.get("fingerprint")
    if isinstance(expected_fingerprint, Mapping):
        expected = {str(key): int(value) for key, value in expected_fingerprint.items()}
        signatures = [_result_signature(result) for result in tracked_results + latency_results]
        passed = bool(signatures) and all(
            all(signature.get(key) == value for key, value in expected.items())
            for signature in signatures
        )
        gates.append(
            _gate(
                "stability_policy_structural_fingerprint",
                passed,
                f"expected={expected} runs={len(signatures)}",
            )
        )

    measurement_policy = policy.get("measurement")
    all_results = tracked_results + latency_results
    if isinstance(measurement_policy, Mapping) and measurement_policy.get("cache_mode") is not None:
        expected_cache_mode = str(measurement_policy.get("cache_mode") or "").strip().lower()
        cache_modes = [
            str((result.get("cache_state") or {}).get("requested_mode") or "unknown")
            if isinstance(result.get("cache_state"), Mapping)
            else "unknown"
            for result in all_results
        ]
        gates.append(
            _gate(
                "stability_policy_measurement_cache_mode",
                bool(cache_modes) and all(mode == expected_cache_mode for mode in cache_modes),
                f"expected={expected_cache_mode} observed={cache_modes}",
            )
        )
        if measurement_policy.get("require_parse_cache_bypass"):
            parse_cache_bypassed = bool(all_results) and all(
                isinstance(result.get("cache_state"), Mapping)
                and (result["cache_state"].get("parse_cache") or {}).get("observed_states") == ["disabled"]
                and int((result["cache_state"].get("parse_cache") or {}).get("observed_hit_blocks") or 0) == 0
                for result in all_results
            )
            gates.append(
                _gate(
                    "stability_policy_measurement_parse_cache_bypass",
                    parse_cache_bypassed,
                    "all evidence reports parse-cache bypass with no full-document cache hit",
                )
            )
        if measurement_policy.get("require_ocr_cache_hit"):
            ocr_cache_warm = bool(all_results) and all(
                isinstance(result.get("cache_state"), Mapping)
                and int((result["cache_state"].get("ocr_cache") or {}).get("observed_cache_hit_blocks") or 0) > 0
                for result in all_results
            )
            gates.append(
                _gate(
                    "stability_policy_measurement_ocr_cache_warm",
                    ocr_cache_warm,
                    "all evidence observed at least one page OCR-cache hit per run",
                )
            )
        if measurement_policy.get("require_no_ocr_cache_hits"):
            ocr_cache_bypassed = bool(all_results) and all(
                isinstance(result.get("cache_state"), Mapping)
                and int((result["cache_state"].get("ocr_cache") or {}).get("observed_cache_hit_blocks") or 0) == 0
                for result in all_results
            )
            gates.append(
                _gate(
                    "stability_policy_measurement_ocr_cache_bypass",
                    ocr_cache_bypassed,
                    "all evidence observed zero page OCR-cache hits",
                )
            )

    def add_lane_gates(
        *,
        name: str,
        results: list[dict[str, Any]],
        elapsed: Mapping[str, Any],
        config: Any,
        memory: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(config, Mapping):
            return
        successful = [result for result in results if result.get("status") not in {None, "failed"}]
        minimum_runs = config.get("min_runs")
        if minimum_runs is not None:
            gates.append(_gate(f"stability_policy_{name}_minimum_runs", len(results) >= int(minimum_runs), f"runs={len(results)} minimum={minimum_runs}"))
        minimum_success = config.get("min_success_rate_pct")
        if minimum_success is not None:
            rate = len(successful) / len(results) * 100.0 if results else 0.0
            gates.append(_gate(f"stability_policy_{name}_success_rate", rate >= float(minimum_success), f"success_rate={_round(rate)}% minimum={minimum_success}%"))
        if config.get("require_uniform_lifecycle"):
            groups = _lifecycle_groups(results)
            gates.append(_gate(f"stability_policy_{name}_uniform_lifecycle", len(groups) == 1, f"groups={sorted(groups)}"))
        if config.get("max_p50_s") is not None:
            p50 = elapsed.get("p50")
            gates.append(_gate(f"stability_policy_{name}_p50", p50 is not None and float(p50) <= float(config["max_p50_s"]), f"p50={p50}s maximum={config['max_p50_s']}s"))
        if config.get("max_cv_pct") is not None:
            cv_pct = elapsed.get("cv_pct")
            gates.append(_gate(f"stability_policy_{name}_cv", cv_pct is not None and float(cv_pct) <= float(config["max_cv_pct"]), f"cv={cv_pct}% maximum={config['max_cv_pct']}%"))
        if memory is not None and config.get("max_mean_peak_kb") is not None:
            mean_peak = memory.get("mean")
            gates.append(_gate(f"stability_policy_{name}_mean_peak_kb", mean_peak is not None and float(mean_peak) <= float(config["max_mean_peak_kb"]), f"mean_peak={mean_peak}KB maximum={config['max_mean_peak_kb']}KB"))

    add_lane_gates(
        name="latency",
        results=latency_results,
        elapsed=latency_elapsed,
        config=policy.get("latency"),
    )
    add_lane_gates(
        name="tracked_memory",
        results=tracked_results,
        elapsed=tracked_elapsed,
        memory=tracked_memory,
        config=policy.get("tracked_memory"),
    )
    return gates


def build_report(
    *,
    sample: str | Path,
    original_tracked: str | Path,
    prior_tracked_stability: str | Path,
    current_tracked: list[str | Path],
    historical_latency: str | Path,
    current_latency: list[str | Path],
    historical_regression: str | Path,
    current_regression: str | Path,
    self_check: str | Path,
    p1_acceptance: str | Path,
    stability_policy: str | Path | None = None,
) -> dict[str, Any]:
    sample_path = Path(sample).resolve()
    original_payload = _load(original_tracked)
    prior_stability_payload = _load(prior_tracked_stability)
    tracked_payloads = [_load(path) for path in current_tracked]
    historical_latency_payload = _load(historical_latency)
    latency_payloads = [_load(path) for path in current_latency]
    historical_regression_payload = _load(historical_regression)
    current_regression_payload = _load(current_regression)
    self_check_payload = _load(self_check)
    p1_payload = _load(p1_acceptance)
    policy, policy_source = _load_stability_policy(stability_policy)

    original_result = _first_result(original_payload, source=str(original_tracked))
    tracked_results = [
        result
        for payload, path in zip(tracked_payloads, current_tracked, strict=True)
        for result in _parse_results(payload, source=str(path))
    ]
    latency_results = [
        result
        for payload, path in zip(latency_payloads, current_latency, strict=True)
        for result in _parse_results(payload, source=str(path))
    ]
    historical_latency_fixture = _first_fixture(
        historical_latency_payload,
        source=str(historical_latency),
    )
    historical_fixture = _first_fixture(
        historical_regression_payload,
        source=str(historical_regression),
    )
    current_fixture = _first_fixture(current_regression_payload, source=str(current_regression))

    tracked_elapsed = _series(float(result.get("elapsed_s") or 0.0) for result in tracked_results)
    tracked_memory = _series(
        float(result["peak_kb"])
        for result in tracked_results
        if result.get("peak_kb") is not None
    )
    latency_elapsed = _series(float(result.get("elapsed_s") or 0.0) for result in latency_results)
    original_elapsed = float(original_result.get("elapsed_s") or 0.0)
    original_peak_kb = float(original_result.get("peak_kb") or 0.0)
    prior_summary = prior_stability_payload.get("summary") or {}
    prior_median = float(prior_summary.get("elapsed_median_s") or 0.0)
    historical_latency_elapsed = float(historical_latency_fixture.get("elapsed_s") or 0.0)

    tracked_signatures = [_result_signature(result) for result in tracked_results]
    latency_signatures = [_result_signature(result) for result in latency_results]
    tracked_success = bool(tracked_results) and all(
        payload.get("status") == "ok" for payload in tracked_payloads
    ) and all(result.get("status") not in {None, "failed"} for result in tracked_results)
    latency_success = bool(latency_results) and all(
        payload.get("status") == "ok" for payload in latency_payloads
    ) and all(result.get("status") not in {None, "failed"} for result in latency_results)

    historical_quality = _regression_summary(historical_fixture)
    current_quality = _regression_summary(current_fixture)
    prior_quality_counts = prior_summary.get("quality_counts") or {}
    structure_stable = _all_equal(tracked_signatures) and _all_equal(latency_signatures)
    quality_preserved = (
        current_quality["content_blocks"] == historical_quality["content_blocks"]
        and current_quality["chunks"] == historical_quality["chunks"]
        and current_quality["tables"] == historical_quality["tables"]
        and current_quality["pages"] >= historical_quality["pages"]
        and current_quality["noise_ratio"] <= historical_quality["noise_ratio"]
        and all(
            signature["chunks"] == int(prior_quality_counts.get("chunks") or 0)
            and signature["tables"] == int(prior_quality_counts.get("tables") or 0)
            and signature["figures"] == int(prior_quality_counts.get("figures") or 0)
            for signature in tracked_signatures + latency_signatures
        )
    )

    self_checks = self_check_payload.get("checks") or []
    self_check_passed = (
        self_check_payload.get("status") == "ok"
        and bool(self_checks)
        and all(item.get("status") == "passed" for item in self_checks)
    )
    p1_summary = p1_payload.get("summary") or {}
    p1_passed = (
        p1_payload.get("status") == "passed"
        and int(p1_summary.get("failed_check_count") or 0) == 0
    )

    referenced_names = [
        Path(str(original_result.get("document") or original_result.get("file_name") or "")).name,
        Path(str(historical_latency_fixture.get("fixture") or "")).name,
        Path(str(historical_fixture.get("fixture") or "")).name,
        Path(str(current_fixture.get("fixture") or "")).name,
        *(Path(str(result.get("document") or result.get("file_name") or "")).name for result in tracked_results),
        *(Path(str(result.get("document") or result.get("file_name") or "")).name for result in latency_results),
    ]
    sample_identity_passed = sample_path.is_file() and all(
        name == sample_path.name for name in referenced_names
    )

    stability_policy_gates = _policy_gates(
        policy=policy,
        sample_path=sample_path,
        tracked_results=tracked_results,
        latency_results=latency_results,
        tracked_elapsed=tracked_elapsed,
        tracked_memory=tracked_memory,
        latency_elapsed=latency_elapsed,
    )

    gates = [
        _gate(
            "clean_latency_vs_historical",
            bool(latency_elapsed["median"] is not None and latency_elapsed["median"] < historical_latency_elapsed),
            f"median {latency_elapsed['median']}s vs historical {historical_latency_elapsed}s",
        ),
        _gate(
            "python_peak_memory_vs_original",
            bool(tracked_memory["mean"] is not None and tracked_memory["mean"] < original_peak_kb),
            f"mean {tracked_memory['mean']}KB vs original {original_peak_kb}KB",
        ),
        _gate(
            "clean_latency_stability",
            bool(latency_success and latency_elapsed["cv_pct"] is not None and latency_elapsed["cv_pct"] <= 5.0),
            f"runs={latency_elapsed['count']} cv={latency_elapsed['cv_pct']}% range={latency_elapsed['range']}s",
        ),
        _gate(
            "structural_determinism",
            tracked_success and latency_success and structure_stable,
            "tracked and clean-latency run signatures are internally identical",
        ),
        _gate(
            "content_quality_preserved",
            quality_preserved,
            "content blocks/chunks/tables/figures preserved; physical-page coverage increased",
        ),
        _gate(
            "self_check",
            self_check_passed,
            f"status={self_check_payload.get('status')} checks={len(self_checks)}",
        ),
        _gate(
            "p1_contract_acceptance",
            p1_passed,
            (
                f"passed={p1_summary.get('passed_check_count', 0)}/"
                f"{p1_summary.get('check_count', 0)} payloads={p1_summary.get('payload_count', 0)}"
            ),
        ),
        _gate(
            "sample_identity",
            sample_identity_passed,
            f"all evidence references {sample_path.name}",
        ),
        *stability_policy_gates,
    ]

    observations: list[dict[str, Any]] = []
    if tracked_elapsed["median"] is not None:
        observations.append(
            {
                "code": "tracked_memory_elapsed_non_sla",
                "severity": "information",
                "summary": (
                    f"tracemalloc lane median {tracked_elapsed['median']}s is retained for allocation history only; "
                    "release latency is gated exclusively by the clean-latency lane"
                ),
            }
        )
    if (
        tracked_elapsed["max"] is not None
        and tracked_elapsed["median"] is not None
        and tracked_elapsed["max"] > tracked_elapsed["median"] * 1.2
    ):
        observations.append(
            {
                "code": "tracked_memory_instrumentation_tail_outlier",
                "severity": "observation",
                "summary": (
                    f"tracemalloc lane max {tracked_elapsed['max']}s is more than 20% above "
                    f"median {tracked_elapsed['median']}s; clean latency CV is {latency_elapsed['cv_pct']}%"
                ),
            }
        )
    current_signature = latency_signatures[0] if latency_signatures else {}
    flags = current_signature.get("quality_gate_flags") or []
    if flags:
        observations.append(
            {
                "code": "non_blocking_quality_gate_flags",
                "severity": "observation",
                "summary": f"quality gate passed with flags: {', '.join(flags)}",
            }
        )
    if current_quality["audit_artifact_items"]:
        observations.append(
            {
                "code": "physical_page_audit_artifacts",
                "severity": "information",
                "summary": (
                    f"{current_quality['audit_artifact_items']} empty/non-extractable page artifacts "
                    "preserve physical-page evidence and are excluded from content-quality denominators"
                ),
            }
        )

    tracked_telemetry = {
        "stage_timings_s": _stage_telemetry_summary(tracked_results),
        "process": _process_telemetry_summary(tracked_results),
    }
    latency_telemetry = {
        "stage_timings_s": _stage_telemetry_summary(latency_results),
        "process": _process_telemetry_summary(latency_results),
    }
    retained_tail_count = sum(
        len((payload.get("stability") or {}).get("outliers") or [])
        for payload in tracked_payloads + latency_payloads
        if isinstance(payload.get("stability"), Mapping)
    )
    if retained_tail_count:
        observations.append(
            {
                "code": "stability_outlier_telemetry_retained",
                "severity": "observation",
                "summary": f"{retained_tail_count} stability outlier(s) retain stage and process telemetry in source artifacts",
            }
        )

    failed_gates = [gate for gate in gates if gate["status"] != "passed"]
    status = "failed" if failed_gates else ("passed_with_observation" if observations else "passed")
    config_value = tracked_payloads[0].get("config") if tracked_payloads else None
    config_path = Path(str(config_value)).resolve() if config_value else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_recommendation": "hold" if failed_gates else "proceed_with_tail_monitoring",
        "sample": {
            "path": str(sample_path),
            "name": sample_path.name,
            "size_bytes": sample_path.stat().st_size if sample_path.is_file() else None,
            "sha256": _sha256(sample_path) if sample_path.is_file() else None,
        },
        "configuration": {
            "path": str(config_path) if config_path else None,
            "sha256": _sha256(config_path) if config_path and config_path.is_file() else None,
        },
        "performance": {
            "tracked_lane": {
                "current_elapsed_s": tracked_elapsed,
                "current_peak_kb": tracked_memory,
                "original_elapsed_s": _round(original_elapsed),
                "original_peak_kb": _round(original_peak_kb),
                "prior_stability_median_elapsed_s": _round(prior_median),
                "elapsed_improvement_vs_original_pct": _decrease_pct(
                    float(tracked_elapsed["median"] or 0.0), original_elapsed
                ),
                "elapsed_improvement_vs_prior_stability_pct": _decrease_pct(
                    float(tracked_elapsed["median"] or 0.0), prior_median
                ),
                "peak_memory_improvement_vs_original_pct": _decrease_pct(
                    float(tracked_memory["mean"] or 0.0), original_peak_kb
                ),
            },
            "clean_latency_lane": {
                "current_elapsed_s": latency_elapsed,
                "historical_elapsed_s": _round(historical_latency_elapsed),
                "elapsed_improvement_pct": _decrease_pct(
                    float(latency_elapsed["median"] or 0.0), historical_latency_elapsed
                ),
            },
        },
        "reliability": {
            "tracked_run_count": len(tracked_results),
            "clean_latency_run_count": len(latency_results),
            "tracked_success": tracked_success,
            "clean_latency_success": latency_success,
            "structural_determinism": structure_stable,
            "signature": current_signature,
            "historical_quality": historical_quality,
            "current_quality": current_quality,
            "telemetry": {
                "tracked_lane": tracked_telemetry,
                "clean_latency_lane": latency_telemetry,
            },
            "stability_policy": {
                "source": policy_source,
                "enabled": policy is not None,
            },
            "self_check": {
                "status": self_check_payload.get("status"),
                "checks": [
                    {
                        "name": item.get("name"),
                        "status": item.get("status"),
                        "summary": item.get("summary"),
                    }
                    for item in self_checks
                ],
            },
            "p1_contract_acceptance": {
                "status": p1_payload.get("status"),
                "summary": p1_summary,
            },
        },
        "gates": gates,
        "summary": {
            "gate_count": len(gates),
            "passed_gate_count": len(gates) - len(failed_gates),
            "failed_gate_count": len(failed_gates),
            "observation_count": len(observations),
        },
        "observations": observations,
        "evidence": {
            "original_tracked": str(Path(original_tracked).resolve()),
            "prior_tracked_stability": str(Path(prior_tracked_stability).resolve()),
            "current_tracked": [str(Path(path).resolve()) for path in current_tracked],
            "historical_latency": str(Path(historical_latency).resolve()),
            "current_latency": [str(Path(path).resolve()) for path in current_latency],
            "historical_regression": str(Path(historical_regression).resolve()),
            "current_regression": str(Path(current_regression).resolve()),
            "self_check": str(Path(self_check).resolve()),
            "p1_acceptance": str(Path(p1_acceptance).resolve()),
            "stability_policy": policy_source,
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    performance = payload.get("performance") or {}
    tracked = performance.get("tracked_lane") or {}
    clean = performance.get("clean_latency_lane") or {}
    tracked_elapsed = tracked.get("current_elapsed_s") or {}
    tracked_memory = tracked.get("current_peak_kb") or {}
    clean_elapsed = clean.get("current_elapsed_s") or {}
    reliability = payload.get("reliability") or {}
    historical_quality = reliability.get("historical_quality") or {}
    current_quality = reliability.get("current_quality") or {}
    sample = payload.get("sample") or {}
    lines = [
        "# 产品优化结果审计（2026-07-15）",
        "",
        f"- 审计状态：**{payload.get('status')}**",
        f"- 发布建议：`{payload.get('release_recommendation')}`",
        f"- 生成时间：`{payload.get('generated_at')}`",
        f"- 固定样本：`{sample.get('path')}`",
        f"- 样本 SHA256：`{sample.get('sha256')}`",
        "",
        "## 性能对比",
        "",
        "| 测量通道 | 历史值 | 当前值 | 提升 | 稳定性 |",
        "| --- | ---: | ---: | ---: | --- |",
        (
            f"| 内存追踪耗时 | {tracked.get('original_elapsed_s')} s | "
            f"中位数 {tracked_elapsed.get('median')} s | "
            f"{tracked.get('elapsed_improvement_vs_original_pct')}% | "
            f"极差 {tracked_elapsed.get('range')} s，CV {tracked_elapsed.get('cv_pct')}% |"
        ),
        (
            f"| 纯延迟 | {clean.get('historical_elapsed_s')} s | "
            f"中位数 {clean_elapsed.get('median')} s | {clean.get('elapsed_improvement_pct')}% | "
            f"极差 {clean_elapsed.get('range')} s，CV {clean_elapsed.get('cv_pct')}% |"
        ),
        (
            f"| Python 峰值内存 | {tracked.get('original_peak_kb')} KB | "
            f"均值 {tracked_memory.get('mean')} KB | "
            f"{tracked.get('peak_memory_improvement_vs_original_pct')}% | "
            f"极差 {tracked_memory.get('range')} KB |"
        ),
        "",
        "## 可靠性与质量",
        "",
        "| 指标 | 历史值 | 当前值 |",
        "| --- | ---: | ---: |",
        f"| 内容块 | {historical_quality.get('content_blocks')} | {current_quality.get('content_blocks')} |",
        f"| 分块 | {historical_quality.get('chunks')} | {current_quality.get('chunks')} |",
        f"| 物理页覆盖 | {historical_quality.get('pages')} | {current_quality.get('pages')} |",
        f"| 表格 | {historical_quality.get('tables')} | {current_quality.get('tables')} |",
        f"| 噪声率 | {historical_quality.get('noise_ratio')} | {current_quality.get('noise_ratio')} |",
        f"| 审计占位项 | {historical_quality.get('audit_artifact_items')} | {current_quality.get('audit_artifact_items')} |",
        "",
        f"- 结构确定性：`{reliability.get('structural_determinism')}`",
        f"- 内存追踪通道成功：`{reliability.get('tracked_success')}`",
        f"- 纯延迟通道成功：`{reliability.get('clean_latency_success')}`",
        f"- 当前质量指纹：`{json.dumps(reliability.get('signature') or {}, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 运行遥测",
        "",
        "| 通道 | P50 | P95 | 峰值 RSS | CPU time | I/O read |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    telemetry = reliability.get("telemetry") or {}
    for label, lane_key, elapsed in (
        ("内存追踪", "tracked_lane", tracked_elapsed),
        ("纯延迟", "clean_latency_lane", clean_elapsed),
    ):
        lane_telemetry = telemetry.get(lane_key) or {}
        process = lane_telemetry.get("process") or {}
        peak_rss = (process.get("peak_rss_bytes") or {}).get("max")
        cpu_total = (process.get("cpu_total_s") or {}).get("mean")
        io_read = (process.get("io_read_bytes") or {}).get("mean")
        lines.append(
            f"| {label} | {elapsed.get('p50')} | {elapsed.get('p95')} | {peak_rss} | {cpu_total} | {io_read} |"
        )
    lines.extend(
        [
            "",
        "## 发布门禁",
        "",
        "| 门禁 | 状态 | 证据 |",
        "| --- | --- | --- |",
        ]
    )
    for gate in payload.get("gates") or []:
        lines.append(f"| {gate.get('name')} | {gate.get('status')} | {gate.get('summary')} |")
    lines.extend(["", "## 观察项", ""])
    observations = payload.get("observations") or []
    if observations:
        for item in observations:
            lines.append(
                f"- `{item.get('code')}` ({item.get('severity')}): {item.get('summary')}"
            )
    else:
        lines.append("- 无。")
    lines.extend(["", "## 证据工件", ""])
    for name, value in (payload.get("evidence") or {}).items():
        if isinstance(value, list):
            lines.append(f"- {name}: {', '.join(f'`{item}`' for item in value)}")
        else:
            lines.append(f"- {name}: `{value}`")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = build_report(
        sample=args.sample,
        original_tracked=args.original_tracked,
        prior_tracked_stability=args.prior_tracked_stability,
        current_tracked=args.current_tracked,
        historical_latency=args.historical_latency,
        current_latency=args.current_latency,
        historical_regression=args.historical_regression,
        current_regression=args.current_regression,
        self_check=args.self_check,
        p1_acceptance=args.p1_acceptance,
        stability_policy=args.stability_policy,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[optimization-result-audit] wrote {output_path}")
    else:
        print(text)
    if args.out_md:
        markdown_path = Path(args.out_md)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[optimization-result-audit] wrote {markdown_path}")
    return 0 if int((payload.get("summary") or {}).get("failed_gate_count") or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
