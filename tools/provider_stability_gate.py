"""Validate repeated Provider comparison reports for P0 stability evidence.

The stability gate deliberately separates deterministic quality structure from
elapsed time.  A candidate is stable when its page/sample signatures remain
unchanged across the required number of reports.  Differences from the
baseline Provider are reported as warnings because they require gold review;
they are not silently treated as a performance failure.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2026-07-provider-stability-gate"


def _load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"report must be a JSON object: {source}")
    # Gold evaluation artifacts wrap the provider comparison under
    # ``comparison``; accept that shape so the same stability gate can audit
    # the full approved-page runs without copying large JSON files.
    comparison = payload.get("comparison")
    if "samples" not in payload and isinstance(comparison, dict):
        return comparison
    return payload


def _provider_entry(sample: Mapping[str, Any], provider_id: str) -> Mapping[str, Any] | None:
    providers = sample.get("providers") or []
    if not isinstance(providers, list):
        return None
    for provider in providers:
        if isinstance(provider, Mapping) and str(provider.get("provider_id") or "") == provider_id:
            return provider
    return None


def _sample_name(sample: Mapping[str, Any]) -> str:
    return str(sample.get("sample_name") or sample.get("name") or "")


def _quality_signature(provider: Mapping[str, Any]) -> dict[str, Any]:
    """Return only deterministic quality fields used for stability checks."""

    ir = provider.get("ir_summary") if isinstance(provider.get("ir_summary"), Mapping) else {}
    coverage = (
        provider.get("coverage_summary")
        if isinstance(provider.get("coverage_summary"), Mapping)
        else {}
    )
    rag = (
        provider.get("rag_coverage_quality")
        if isinstance(provider.get("rag_coverage_quality"), Mapping)
        else {}
    )
    return {
        "status": str(provider.get("status") or ""),
        "blocks": int(provider.get("blocks") or 0),
        "chunks": int(provider.get("chunks") or 0),
        "tables": int(provider.get("tables") or 0),
        "pages": int(ir.get("pages") or 0),
        "knowledge_units": int(ir.get("knowledge_units") or 0),
        "text_page_coverage_ratio": float(coverage.get("text_page_coverage_ratio") or 0),
        "table_unit_coverage_ratio": float(coverage.get("table_unit_coverage_ratio") or 0),
        "unit_chunk_coverage_ratio": float(coverage.get("unit_chunk_coverage_ratio") or 0),
        "pages_with_coverage_gaps": int(coverage.get("pages_with_coverage_gaps") or 0),
        "pages_missing_chunks": int(coverage.get("pages_missing_chunks") or 0),
        "pages_chunks_not_embedded": int(coverage.get("pages_chunks_not_embedded") or 0),
        "embedded_unit_count": int(coverage.get("embedded_unit_count") or 0),
        "unembedded_unit_count": int(coverage.get("unembedded_unit_count") or 0),
        "rag_score": float(rag.get("score") or 0),
        "rag_gate": str(rag.get("gate") or ""),
        "rag_flags": sorted(str(item) for item in (rag.get("flags") or [])),
    }


def _run_rows(report: Mapping[str, Any], provider_id: str) -> dict[str, dict[str, Any]]:
    samples = report.get("samples") or []
    if not isinstance(samples, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        name = _sample_name(sample)
        if not name:
            continue
        provider = _provider_entry(sample, provider_id)
        if provider is None:
            rows[name] = {"status": "missing", "signature": None, "elapsed_s": None}
            continue
        elapsed = provider.get("elapsed_s")
        rows[name] = {
            "status": str(provider.get("status") or ""),
            "signature": _quality_signature(provider),
            "elapsed_s": float(elapsed) if elapsed is not None else None,
        }
    return rows


def _baseline_differences(
    report: Mapping[str, Any],
    *,
    provider_id: str,
    baseline_provider_id: str,
) -> list[str]:
    differences: list[str] = []
    samples = report.get("samples") or []
    if not isinstance(samples, list):
        return differences
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        name = _sample_name(sample)
        candidate = _provider_entry(sample, provider_id)
        baseline = _provider_entry(sample, baseline_provider_id)
        if not name or candidate is None or baseline is None:
            continue
        if _quality_signature(candidate) != _quality_signature(baseline):
            differences.append(name)
    return differences


def evaluate_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    provider_id: str,
    baseline_provider_id: str | None = None,
    minimum_stable_runs: int = 3,
) -> dict[str, Any]:
    """Evaluate repeated reports without changing route/admission config."""

    required = max(1, int(minimum_stable_runs))
    errors: list[str] = []
    warnings: list[str] = []
    report_rows = [_run_rows(report, provider_id) for report in reports]
    if len(report_rows) < required:
        errors.append("insufficient_stable_runs")

    sample_sets = [set(rows) for rows in report_rows]
    if sample_sets:
        expected_samples = sample_sets[0]
        for index, names in enumerate(sample_sets[1:], start=2):
            if names != expected_samples:
                errors.append(f"sample_set_changed_run_{index}")
    else:
        expected_samples = set()
        errors.append("no_reports")

    signatures_stable = True
    per_sample: dict[str, dict[str, Any]] = {}
    for sample_name in sorted(expected_samples):
        entries = [rows.get(sample_name) for rows in report_rows]
        statuses = [str(entry.get("status") or "missing") for entry in entries if entry]
        signatures = [entry.get("signature") for entry in entries if entry]
        if any(status != "done" for status in statuses):
            errors.append(f"provider_run_not_done:{sample_name}")
        if len(set(json.dumps(signature, sort_keys=True) for signature in signatures)) > 1:
            signatures_stable = False
            errors.append(f"quality_signature_changed:{sample_name}")
        elapsed = [entry.get("elapsed_s") for entry in entries if entry and entry.get("elapsed_s") is not None]
        per_sample[sample_name] = {
            "statuses": statuses,
            "quality_signature": signatures[0] if signatures else None,
            "quality_signature_stable": len(signatures) == len(report_rows)
            and len(set(json.dumps(signature, sort_keys=True) for signature in signatures)) <= 1,
            "elapsed_s": {
                "runs": elapsed,
                "min": min(elapsed) if elapsed else None,
                "max": max(elapsed) if elapsed else None,
                "median": statistics.median(elapsed) if elapsed else None,
            },
        }

    baseline_differences: list[dict[str, Any]] = []
    if baseline_provider_id:
        for index, report in enumerate(reports, start=1):
            changed = _baseline_differences(
                report,
                provider_id=provider_id,
                baseline_provider_id=baseline_provider_id,
            )
            if changed:
                warnings.append(f"candidate_structure_differs_from_baseline_run_{index}")
            baseline_differences.append({"run": index, "sample_names": changed})

    passed = not errors
    if baseline_differences and any(item["sample_names"] for item in baseline_differences):
        gate = "accept_with_warning" if passed else "fail"
    else:
        gate = "accept" if passed else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if passed else "failed",
        "gate": gate,
        "provider_id": provider_id,
        "baseline_provider_id": baseline_provider_id,
        "required_stable_runs": required,
        "observed_stable_runs": len(reports),
        "sample_count": len(expected_samples),
        "quality_signature_stable": signatures_stable and passed,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "samples": per_sample,
        "baseline_differences": baseline_differences,
    }


def render_markdown(result: Mapping[str, Any], report_paths: Sequence[str | Path]) -> str:
    lines = [
        "# Provider stability gate",
        "",
        f"- provider: `{result.get('provider_id')}`",
        f"- gate: `{result.get('gate')}`",
        f"- status: `{result.get('status')}`",
        f"- stable runs: `{result.get('observed_stable_runs')}/{result.get('required_stable_runs')}`",
        f"- quality signature stable: `{result.get('quality_signature_stable')}`",
        "",
        "## Reports",
        "",
    ]
    lines.extend(f"- `{Path(path).as_posix()}`" for path in report_paths)
    lines.extend(["", "## Samples", "", "| sample | stable | median s | min s | max s |", "| --- | --- | ---: | ---: | ---: |"])
    for name, sample in (result.get("samples") or {}).items():
        elapsed = sample.get("elapsed_s") or {}
        lines.append(
            f"| {name} | {sample.get('quality_signature_stable')} | "
            f"{elapsed.get('median') or '-'} | {elapsed.get('min') or '-'} | {elapsed.get('max') or '-'} |"
        )
    if result.get("errors"):
        lines.extend(["", "## Errors", "", *[f"- `{item}`" for item in result["errors"]]])
    if result.get("warnings"):
        lines.extend(["", "## Warnings", "", *[f"- `{item}`" for item in result["warnings"]]])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate repeated Provider comparison reports")
    parser.add_argument("--report", action="append", required=True, help="Provider comparison JSON; repeat at least three times")
    parser.add_argument("--provider", required=True, help="Candidate Provider id")
    parser.add_argument("--baseline-provider", default=None)
    parser.add_argument("--minimum-stable-runs", type=int, default=3)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", default=None)
    args = parser.parse_args(argv)

    try:
        reports = [_load_json(path) for path in args.report]
        result = evaluate_reports(
            reports,
            provider_id=args.provider,
            baseline_provider_id=args.baseline_provider,
            minimum_stable_runs=args.minimum_stable_runs,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"provider stability gate failed: {exc}", file=sys.stderr)
        return 2

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(result, args.report), encoding="utf-8")
    print(json.dumps({"status": result["status"], "gate": result["gate"], "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
