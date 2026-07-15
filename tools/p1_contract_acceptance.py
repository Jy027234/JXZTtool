"""Run the P1 contract-freeze and host-integration acceptance gate.

The regular ``payload-contract-check`` command intentionally stays small and
fast: it validates one representative payload for each frozen schema.  P1
also promises complex, anomaly, and part-rerun fixtures, plus a stable bridge
from the new Parse IR to the reader and legacy projections.  This tool makes
those promises executable without requiring a running API or external
provider.

Exit codes:
0 -> all P1 checks passed
1 -> at least one P1 check failed
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from time import perf_counter
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from parsecore.api_payloads import (
    _document_projection,
    _document_quality_projection,
)
from parsecore.payload_contract_samples import (
    build_anomaly_payload_contract_samples,
    build_anomaly_sample_snapshot,
    build_complex_payload_contract_samples,
    build_complex_sample_snapshot,
    build_part_rerun_payload_contract_samples,
    build_payload_contract_samples,
)
from parsecore.payload_schemas import (
    PAYLOAD_SCHEMA_REGISTRY_VERSION,
    payload_schema,
    payload_schema_names,
    payload_schema_registry,
)


ACCEPTANCE_SCHEMA_VERSION = "2026-07-p1-contract-acceptance"
EXPECTED_SCHEMA_NAMES = (
    "document-coverage",
    "document-ir",
    "document-parts",
    "document-providers",
    "document-quality",
    "document-reader",
)

_SAMPLE_BUILDERS: tuple[tuple[str, Callable[[], dict[str, dict[str, Any]]]], ...] = (
    ("minimal", build_payload_contract_samples),
    ("complex", build_complex_payload_contract_samples),
    ("anomaly", build_anomaly_payload_contract_samples),
    ("part-rerun", build_part_rerun_payload_contract_samples),
)


def _error_details(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, ValidationError):
        details["path"] = [str(item) for item in exc.absolute_path]
        details["validator"] = exc.validator
    return details


def _check(
    checks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    name: str,
    passed: bool,
    *,
    summary: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    result: dict[str, Any] = {
        "name": name,
        "status": "passed" if passed else "failed",
        "summary": summary,
    }
    if details:
        result["details"] = dict(details)
    checks.append(result)
    if not passed:
        failures.append(result)


def _validate_sample_sets(
    checks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    variant_results: list[dict[str, Any]] = []
    total_payloads = 0
    total_failures = 0

    for variant_name, builder in _SAMPLE_BUILDERS:
        variant_failures: list[dict[str, Any]] = []
        try:
            payloads = builder()
        except Exception as exc:  # pragma: no cover - defensive acceptance surface
            variant_results.append(
                {
                    "variant": variant_name,
                    "status": "failed",
                    "payload_count": 0,
                    "failures": [{"error": str(exc)}],
                }
            )
            total_failures += 1
            continue

        for schema_name in EXPECTED_SCHEMA_NAMES:
            payload = payloads.get(schema_name)
            if not isinstance(payload, dict):
                variant_failures.append(
                    {"schema": schema_name, "error": "sample payload missing"}
                )
                continue
            try:
                Draft202012Validator(payload_schema(schema_name)).validate(payload)
            except Exception as exc:  # pragma: no cover - exercised on contract drift
                variant_failures.append(
                    {"schema": schema_name, **_error_details(exc)}
                )

        total_payloads += len(payloads)
        total_failures += len(variant_failures)
        variant_results.append(
            {
                "variant": variant_name,
                "status": "passed" if not variant_failures else "failed",
                "payload_count": len(payloads),
                "failures": variant_failures,
            }
        )

    _check(
        checks,
        failures,
        "sample_payloads_all_variants",
        total_failures == 0 and total_payloads == len(_SAMPLE_BUILDERS) * len(EXPECTED_SCHEMA_NAMES),
        summary=f"{total_payloads} payloads across {len(_SAMPLE_BUILDERS)} sample variants",
        details={
            "expected_payload_count": len(_SAMPLE_BUILDERS) * len(EXPECTED_SCHEMA_NAMES),
            "actual_payload_count": total_payloads,
            "failed_payload_count": total_failures,
            "variants": variant_results,
        },
    )
    return {
        "variant_count": len(_SAMPLE_BUILDERS),
        "payload_count": total_payloads,
        "failed_payload_count": total_failures,
        "variants": variant_results,
    }


def _run_projection_checks(
    checks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    complex_snapshot = build_complex_sample_snapshot()
    anomaly_snapshot = build_anomaly_sample_snapshot()

    legacy_count = 0
    legacy_failures: list[dict[str, Any]] = []
    for variant_name, snapshot in (
        ("complex", complex_snapshot),
        ("anomaly", anomaly_snapshot),
    ):
        for projection in ("compat", "structured", "full"):
            try:
                payload = _document_projection(snapshot, projection=projection)
                required = {"projection", "doc_id", "pages"}
                missing = sorted(required - set(payload))
                if payload.get("projection") != projection or not payload.get("pages") or missing:
                    raise ValueError(
                        f"projection={projection} missing={missing} pages={len(payload.get('pages') or [])}"
                    )
                if projection == "full" and (
                    not isinstance(payload.get("blocks"), list)
                    or not isinstance(payload.get("chunks"), list)
                ):
                    raise ValueError("full projection did not retain blocks/chunks")
            except Exception as exc:  # pragma: no cover - exercised on compatibility drift
                legacy_failures.append(
                    {"variant": variant_name, "projection": projection, **_error_details(exc)}
                )
            legacy_count += 1

    _check(
        checks,
        failures,
        "legacy_projection_compatibility",
        not legacy_failures,
        summary=f"{legacy_count} compat/structured/full projections retained",
        details={"projection_count": legacy_count, "failures": legacy_failures},
    )

    ir = _document_projection(complex_snapshot, projection="ir")
    reader = _document_projection(complex_snapshot, projection="reader")
    coverage = _document_projection(complex_snapshot, projection="coverage")
    anomaly_ir = _document_projection(anomaly_snapshot, projection="ir")
    anomaly_quality = _document_quality_projection(anomaly_snapshot)

    ir_counts = {
        "pages": len(ir.get("pages") or []),
        "blocks": len(ir.get("blocks") or []),
        "tables": len(ir.get("tables") or []),
        "figures": len(ir.get("figures") or []),
        "knowledge_units": len(ir.get("knowledge_units") or []),
        "quality_signals": len(anomaly_ir.get("quality_signals") or []),
    }
    _check(
        checks,
        failures,
        "ir_structure_and_quality_signals",
        all(ir_counts[key] > 0 for key in ("pages", "blocks", "tables", "figures", "knowledge_units", "quality_signals")),
        summary=(
            f"IR pages={ir_counts['pages']} blocks={ir_counts['blocks']} "
            f"tables={ir_counts['tables']} figures={ir_counts['figures']} "
            f"units={ir_counts['knowledge_units']} anomaly_signals={ir_counts['quality_signals']}"
        ),
        details=ir_counts,
    )

    ir_block_ids = {
        str(block.get("block_id"))
        for block in ir.get("blocks", [])
        if isinstance(block, Mapping) and str(block.get("block_id") or "")
    }
    reader_blocks = [block for block in reader.get("blocks", []) if isinstance(block, Mapping)]
    reader_source_block_ids = {
        str(block_id)
        for block in reader_blocks
        for block_id in (block.get("source_block_ids") or [])
        if str(block_id)
    }
    reader_traceable = bool(reader_blocks) and all(
        block.get("source_unit_ids")
        and block.get("source_block_ids")
        and isinstance(block.get("rag_text"), str)
        for block in reader_blocks
    )
    _check(
        checks,
        failures,
        "reader_ir_traceability",
        reader_traceable and reader_source_block_ids <= ir_block_ids,
        summary=f"{len(reader_blocks)} reader blocks trace to {len(reader_source_block_ids)} IR blocks",
        details={
            "reader_block_count": len(reader_blocks),
            "reader_source_block_count": len(reader_source_block_ids),
            "orphan_source_block_ids": sorted(reader_source_block_ids - ir_block_ids),
        },
    )

    coverage_payload = coverage.get("coverage") if isinstance(coverage.get("coverage"), Mapping) else {}
    coverage_summary = coverage_payload.get("summary") if isinstance(coverage_payload, Mapping) else {}
    coverage_pages = coverage_payload.get("pages") if isinstance(coverage_payload, Mapping) else []
    coverage_units = coverage_payload.get("units") if isinstance(coverage_payload, Mapping) else []
    indexable_unit_count = sum(
        1
        for unit in coverage_units or []
        if isinstance(unit, Mapping) and bool(unit.get("should_index_for_rag"))
    )
    coverage_consistent = (
        isinstance(coverage_summary, Mapping)
        and len(coverage_pages or []) == coverage_summary.get("total_pages")
        and indexable_unit_count == coverage_summary.get("total_indexable_units")
        and coverage_summary.get("pages_with_coverage_gaps") == 0
    )
    _check(
        checks,
        failures,
        "coverage_page_unit_consistency",
        coverage_consistent,
        summary=(
            f"pages={len(coverage_pages or [])} units={len(coverage_units or [])} "
            f"indexable={indexable_unit_count} gaps={coverage_summary.get('pages_with_coverage_gaps') if isinstance(coverage_summary, Mapping) else None}"
        ),
        details={
            "page_count": len(coverage_pages or []),
            "unit_count": len(coverage_units or []),
            "indexable_unit_count": indexable_unit_count,
            "summary": dict(coverage_summary) if isinstance(coverage_summary, Mapping) else {},
        },
    )

    workflow = (
        anomaly_quality.get("attention_summary", {}).get("contracts", {}).get("workflow", {})
        if isinstance(anomaly_quality.get("attention_summary"), Mapping)
        else {}
    )
    phases = [
        str(phase.get("phase"))
        for phase in workflow.get("phases", [])
        if isinstance(phase, Mapping)
    ]
    workflow_ready = phases == ["inspect", "compare", "execute", "verify"] and all(
        bool(phase.get("ready"))
        for phase in workflow.get("phases", [])
        if isinstance(phase, Mapping)
    )
    action_contracts = anomaly_quality.get("attention_summary", {}).get("contracts", {})
    recommended_requests = action_contracts.get("recommended_requests", []) if isinstance(action_contracts, Mapping) else []
    action_fields_ok = all(
        all(field in action for field in ("action_id", "request", "auto_execute"))
        and isinstance(action.get("request"), Mapping)
        and action["request"].get("method")
        and action["request"].get("endpoint")
        for action in recommended_requests
        if isinstance(action, Mapping)
    )
    _check(
        checks,
        failures,
        "action_contract_workflow",
        workflow_ready and action_fields_ok,
        summary=f"workflow={'→'.join(phases) or 'none'} actions={len(recommended_requests)}",
        details={
            "phases": phases,
            "all_phases_ready": workflow_ready,
            "recommended_request_count": len(recommended_requests),
            "action_fields_ok": action_fields_ok,
        },
    )

    return {
        "legacy_projection_count": legacy_count,
        "ir_counts": ir_counts,
        "reader_block_count": len(reader_blocks),
        "coverage_page_count": len(coverage_pages or []),
        "coverage_unit_count": len(coverage_units or []),
        "workflow_phases": phases,
    }


def _run_part_rerun_check(
    checks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = build_part_rerun_payload_contract_samples()["document-parts"]
    parts = [part for part in payload.get("parts", []) if isinstance(part, Mapping)]
    compared = [
        part
        for part in parts
        if isinstance(part.get("previous_part_observation"), Mapping)
        and isinstance(part.get("rerun_comparison"), Mapping)
        and bool((part.get("diagnostics") or {}).get("rerun_compared"))
    ]
    comparison = compared[0].get("rerun_comparison") if compared else {}
    rerun_ok = bool(compared) and str((comparison or {}).get("status") or "") == "improved"
    _check(
        checks,
        failures,
        "part_rerun_monitor_verify_contract",
        rerun_ok,
        summary=f"{len(compared)}/{len(parts)} parts expose previous observation and comparison",
        details={
            "part_count": len(parts),
            "compared_part_count": len(compared),
            "comparison_status": (comparison or {}).get("status"),
            "improvement_axes": list((comparison or {}).get("improvement_axes") or []),
        },
    )
    return {
        "part_count": len(parts),
        "compared_part_count": len(compared),
        "comparison_status": (comparison or {}).get("status"),
    }


def run_acceptance() -> dict[str, Any]:
    started = perf_counter()
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    registry = payload_schema_registry()
    registry_failures: list[dict[str, Any]] = []
    if tuple(payload_schema_names()) != EXPECTED_SCHEMA_NAMES:
        registry_failures.append(
            {
                "error": "schema names drifted",
                "expected": list(EXPECTED_SCHEMA_NAMES),
                "actual": list(payload_schema_names()),
            }
        )
    for name in EXPECTED_SCHEMA_NAMES:
        try:
            Draft202012Validator.check_schema(payload_schema(name))
        except Exception as exc:  # pragma: no cover - exercised on schema drift
            registry_failures.append({"schema": name, **_error_details(exc)})
    _check(
        checks,
        failures,
        "schema_registry_and_json_schema",
        not registry_failures and registry.get("summary", {}).get("total") == len(EXPECTED_SCHEMA_NAMES),
        summary=f"{len(EXPECTED_SCHEMA_NAMES)} frozen schemas in {PAYLOAD_SCHEMA_REGISTRY_VERSION}",
        details={"registry_failures": registry_failures, "registry": registry},
    )

    sample_summary = _validate_sample_sets(checks, failures)
    projection_summary = _run_projection_checks(checks, failures)
    rerun_summary = _run_part_rerun_check(checks, failures)

    return {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "status": "passed" if not failures else "failed",
        "summary": {
            "check_count": len(checks),
            "passed_check_count": sum(1 for check in checks if check["status"] == "passed"),
            "failed_check_count": len(failures),
            "schema_count": len(EXPECTED_SCHEMA_NAMES),
            "sample_variant_count": sample_summary["variant_count"],
            "payload_count": sample_summary["payload_count"],
            "legacy_projection_count": projection_summary["legacy_projection_count"],
            "part_count": rerun_summary["part_count"],
        },
        "registry_schema_version": PAYLOAD_SCHEMA_REGISTRY_VERSION,
        "sample_sets": sample_summary,
        "projections": projection_summary,
        "checks": checks,
        "failures": failures,
        "duration_s": round(perf_counter() - started, 3),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        "# ParseCore P1 契约冻结与宿主接入验收",
        "",
        f"- 生成时间：{date.today().isoformat()}（本地验收批次）",
        f"- 状态：**{report.get('status', 'unknown')}**",
        f"- 检查：{summary.get('passed_check_count', 0)}/{summary.get('check_count', 0)} 通过",
        f"- 样例：{summary.get('payload_count', 0)} 个 payload（{summary.get('sample_variant_count', 0)} 组）",
        "",
        "## 验收项",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    for check in report.get("checks", []):
        if not isinstance(check, Mapping):
            continue
        lines.append(
            f"| `{check.get('name', '')}` | {check.get('status', '')} | {check.get('summary', '')} |"
        )
    lines.extend(
        [
            "",
            "## 交付边界",
            "",
            "本批次验证 schema registry、最小/复杂/异常/part-rerun 样例、旧 projection 兼容、IR→Reader 可追溯、coverage 页/单元一致性，以及 inspect→compare→execute→verify 动作合同。",
            "",
            "Provider 候选准入、远程 embedding/RAG 和真实生产宿主视觉验收仍属于 P2/P3/P7 或外部环境门禁，不在本批次自动放行。",
            "",
        ]
    )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ParseCore P1 contract acceptance gate")
    parser.add_argument("--out", default=None, help="optional JSON report path")
    parser.add_argument("--markdown-out", default=None, help="optional Markdown report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_acceptance()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    if args.markdown_out:
        markdown_path = Path(args.markdown_out)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(report), encoding="utf-8")

    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(text.encode("utf-8", errors="replace"))
        stdout_buffer.write(b"\n")
        stdout_buffer.flush()
    else:
        print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
