from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .ir import build_coverage_projection, build_ir_projection, rag_coverage_quality_payload
from .models import Block, BlockType, ParseOutcome
from .ocr_trace import build_ocr_decision_trace, ocr_decision_trace_payload
from .parts import document_parts_projection
from .profiles import resolve_parse_profile
from .record_filters import collect_record_page
from .quality import ParseQualitySummary, evaluate_parse_quality, evaluate_projected_parse_quality


_ARTIFACT_SEMANTIC_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
    "image",
}
_TEXT_RECORD_PROFILES = {"large-pdf-catalog", "large-pdf-ledger"}
_TEXT_RECORD_START_PATTERN = re.compile(r"^\s*(?P<row>\d{1,8})\s+(?P<body>.+?)\s*$")
_TEXT_RECORD_HEADER_PATTERN = re.compile(r"(?:序号|证件编号|项目编号|持证人|最新批准日期|批准日期)")
_TEXT_RECORD_DATE_PATTERN = re.compile(r"\b(19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b")
_TEXT_RECORD_ANY_DATE_PATTERN = re.compile(r"\b(19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b")
_TEXT_RECORD_CERT_PATTERN = re.compile(
    r"\b(?:TC|STC|PMA|MDA|CTSOA|VTC|VSTC|VDA|TDA|TSOA)[A-Z0-9-]*\b",
    re.IGNORECASE,
)
_DATE_FIELD_HINTS = ("date", "日期", "批准日期", "latest", "有效期")
_IDENTIFIER_FIELD_HINTS = ("certificate", "project", "编号", "证件", "项目", "no", "number")

# Increment when the shape of pages[] or top-level fields changes in a
# backwards-incompatible way.  Consumers can gate on this string.
PAYLOAD_SCHEMA_VERSION = "2026-04"
DOCUMENT_SCHEMA_VERSION = "2026-06"
PROVIDER_USAGE_SCHEMA_VERSION = "2026-06-provider-usage"
PROVIDER_COMPARISON_SCHEMA_VERSION = "2026-06-provider-comparison"
QUALITY_GATE_SCHEMA_VERSION = "2026-06-quality-gate"
READER_SCHEMA_VERSION = "2026-07-reader"
PART_RERUN_COMPARISON_SCHEMA_VERSION = "2026-06-part-rerun-comparison"
DEFAULT_READING_ORDER_CONFIDENCE_THRESHOLD = 0.75


def _to_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_payload(item) for item in value]
    return value


def _quality_payload(qs: ParseQualitySummary) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "score": qs.score,
        "flags": sorted(qs.flags),
        "warnings": list(qs.warnings),
        "total_cid_tokens": qs.total_cid_tokens,
    }
    if qs.total_pdf_name_tokens:
        payload["total_pdf_name_tokens"] = qs.total_pdf_name_tokens
    if qs.recommended_action:
        payload["recommended_action"] = qs.recommended_action
    if qs.ocr_failed_pages:
        payload["ocr_failed_pages"] = qs.ocr_failed_pages
    if qs.suspect_signature_pages:
        payload["suspect_signature_pages"] = qs.suspect_signature_pages
    return payload


def _batch_success_response(outcome: ParseOutcome) -> dict[str, Any]:
    pages = _project_pages(outcome.blocks)
    raw_qs = evaluate_parse_quality(outcome.blocks)
    output_qs = evaluate_projected_parse_quality(pages)
    ocr_trace = build_ocr_decision_trace(outcome.blocks)
    trace_payload = ocr_decision_trace_payload(ocr_trace)
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "success": True,
        "total_pages": len(pages),
        "pages": pages,
        "parser_used": _infer_parser_used(outcome.blocks),
        "quality": _quality_payload(output_qs),
        "raw_quality": _quality_payload(raw_qs),
        "output_quality": _quality_payload(output_qs),
        "ocr_decision_trace": trace_payload,
        "error": None,
    }


def _parse_success_response(
    outcome: ParseOutcome,
    *,
    file_name: str,
    mime_type: str | None,
    enable_ocr: bool,
) -> dict[str, Any]:
    pages = _project_pages(outcome.blocks)
    parser_used = _infer_parser_used(outcome.blocks)
    raw_qs = evaluate_parse_quality(outcome.blocks)
    output_qs = evaluate_projected_parse_quality(pages)
    ocr_trace = build_ocr_decision_trace(outcome.blocks)
    metadata: dict[str, Any] = {
        "parser": parser_used,
        "schema_version": PAYLOAD_SCHEMA_VERSION,
    }
    if (mime_type or "").lower() == "application/pdf":
        metadata["ocr_enabled"] = enable_ocr
        # C1: expose aggregated stage timings for PDF.
        timings = _aggregate_stage_timings(outcome.blocks)
        if timings:
            metadata["stage_timings"] = timings
        # A3: expose the effective OCR strategy from the title block.
        ocr_strategy = _read_first_metadata(outcome.blocks, "ocr_strategy")
        if ocr_strategy:
            metadata["ocr_strategy"] = ocr_strategy
        trace_payload = ocr_decision_trace_payload(ocr_trace)
        if trace_payload.get("ocr_attempted_pages", 0) > 0:
            metadata["ocr_decision_trace"] = trace_payload
    # B3: expose fidelity_profile when it was set by the caller.
    fidelity_profile = _read_first_metadata(outcome.blocks, "fidelity_profile")
    if fidelity_profile:
        metadata["fidelity_profile"] = fidelity_profile
    return {
        "file_name": file_name,
        "mime_type": mime_type or "application/octet-stream",
        "total_pages": len(pages),
        "pages": pages,
        "metadata": metadata,
        "quality": _quality_payload(output_qs),
        "raw_quality": _quality_payload(raw_qs),
        "output_quality": _quality_payload(output_qs),
    }


def _document_projection(snapshot: dict[str, Any], *, projection: str = "full") -> dict[str, Any]:
    normalized_projection = str(projection or "full").strip().lower()
    if normalized_projection not in {"compat", "structured", "full", "ir", "coverage", "reader"}:
        raise ValueError("invalid_projection")

    blocks = tuple(snapshot.get("blocks") or ())
    chunks = tuple(snapshot.get("chunks") or ())
    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", ""))
    pages = _project_pages(blocks)
    raw_qs = evaluate_parse_quality(blocks)
    output_qs = evaluate_projected_parse_quality(pages)
    ocr_trace = build_ocr_decision_trace(blocks)
    trace_payload = ocr_decision_trace_payload(ocr_trace)
    quality_gate_config = _quality_gate_config(snapshot.get("quality_gate"))

    if normalized_projection == "compat":
        return {
            "schema_version": PAYLOAD_SCHEMA_VERSION,
            "projection": "compat",
            "doc_id": doc_id,
            "job": _to_payload(job),
            "total_pages": len(pages),
            "pages": pages,
            "parser_used": _infer_parser_used(blocks),
            "quality": _quality_payload(output_qs),
            "raw_quality": _quality_payload(raw_qs),
            "output_quality": _quality_payload(output_qs),
            "ocr_decision_trace": trace_payload,
            "error": None,
        }

    tables = _structured_tables(blocks, doc_id=doc_id)
    profile_resolution = _profile_resolution_for_document(job=job, pages=pages, tables=tables)
    profile = str(profile_resolution["resolved_profile"])
    quality_signals = _quality_signals(
        pages=pages,
        tables=tables,
        blocks=blocks,
        reading_order_confidence_threshold=float(
            quality_gate_config["thresholds"]["min_reading_order_confidence"]
        ),
    )
    records = _structured_records(
        blocks=blocks,
        tables=tables,
        quality_signals=quality_signals,
        profile=profile,
        doc_id=doc_id,
    )
    quality_signals.extend(_record_quality_signals(records))
    quality_payload = _quality_payload(output_qs)
    raw_quality_payload = _quality_payload(raw_qs)
    output_quality_payload = _quality_payload(output_qs)
    parse_run_id = str(getattr(job, "job_id", "") or "")
    state = _state_value(getattr(job, "state", None))
    local_provider_routing = _local_provider_routing_decision(snapshot)
    if normalized_projection in {"ir", "reader"}:
        ir_payload = build_ir_projection(
            snapshot=snapshot,
            doc_id=doc_id,
            parse_run_id=parse_run_id,
            profile=profile,
            profile_resolution=profile_resolution,
            state=state,
            blocks=blocks,
            chunks=chunks,
            pages=pages,
            tables=tables,
            quality=quality_payload,
            raw_quality=raw_quality_payload,
            output_quality=output_quality_payload,
            quality_signals=quality_signals,
            ocr_decision_trace=trace_payload,
        )
        part_quality_signals = list(quality_signals)
        part_quality_signals.extend(
            dict(signal)
            for signal in ir_payload.get("coverage_quality_signals", [])
            if isinstance(signal, Mapping)
        )
        parse_units = _parse_units(
            snapshot=snapshot,
            pages=pages,
            tables=tables,
            quality_signals=part_quality_signals,
            coverage_pages=(ir_payload.get("coverage") or {}).get("pages") or [],
            coverage_units=(ir_payload.get("coverage") or {}).get("units") or [],
        )
        ir_payload["quality_gate"] = _quality_gate_projection(
            snapshot=snapshot,
            coverage_summary=(ir_payload.get("coverage") or {}).get("summary") or {},
            rag_coverage_quality=ir_payload.get("rag_coverage_quality") or {},
            parse_units=parse_units,
            pages=pages,
        )
        if normalized_projection == "reader":
            return _document_reader_projection(ir_payload)
        return ir_payload
    if normalized_projection == "coverage":
        coverage_payload = build_coverage_projection(
            snapshot=snapshot,
            doc_id=doc_id,
            parse_run_id=parse_run_id,
            profile=profile,
            state=state,
            blocks=blocks,
            chunks=chunks,
            pages=pages,
            tables=tables,
            quality_signals=quality_signals,
        )
        part_quality_signals = list(quality_signals)
        part_quality_signals.extend(
            dict(signal)
            for signal in coverage_payload.get("quality_signals", [])
            if isinstance(signal, Mapping)
        )
        parse_units = _parse_units(
            snapshot=snapshot,
            pages=pages,
            tables=tables,
            quality_signals=part_quality_signals,
            coverage_pages=(coverage_payload.get("coverage") or {}).get("pages") or [],
            coverage_units=(coverage_payload.get("coverage") or {}).get("units") or [],
        )
        coverage_payload["quality_gate"] = _quality_gate_projection(
            snapshot=snapshot,
            coverage_summary=(coverage_payload.get("coverage") or {}).get("summary") or {},
            rag_coverage_quality=coverage_payload.get("rag_coverage_quality") or {},
            parse_units=parse_units,
            pages=pages,
        )
        return coverage_payload

    coverage_payload = build_coverage_projection(
        snapshot=snapshot,
        doc_id=doc_id,
        parse_run_id=parse_run_id,
        profile=profile,
        state=state,
        blocks=blocks,
        chunks=chunks,
        pages=pages,
        tables=tables,
        quality_signals=quality_signals,
    )
    coverage_quality_signals = [
        dict(signal)
        for signal in coverage_payload.get("quality_signals", [])
        if isinstance(signal, Mapping)
    ]
    quality_signals.extend(coverage_quality_signals)
    structured_pages = _structured_pages(
        pages=pages,
        tables=tables,
        quality_signals=quality_signals,
    )
    parse_units = _parse_units(
        snapshot=snapshot,
        pages=pages,
        tables=tables,
        quality_signals=quality_signals,
        coverage_pages=(coverage_payload.get("coverage") or {}).get("pages") or [],
        coverage_units=(coverage_payload.get("coverage") or {}).get("units") or [],
    )

    payload: dict[str, Any] = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": normalized_projection,
        "doc_id": doc_id,
        "parse_run_id": parse_run_id,
        "profile": profile,
        "profile_resolution": profile_resolution,
        "local_provider_routing": local_provider_routing,
        "state": state,
        "compat_pages": pages,
        "pages": structured_pages,
        "tables": tables,
        "records_summary": _records_summary(records),
        "quality": quality_payload,
        "raw_quality": raw_quality_payload,
        "output_quality": output_quality_payload,
        "quality_signals": quality_signals,
        "quality_summary": _quality_signal_summary(quality_signals),
        "coverage_summary": coverage_payload["coverage"]["summary"],
        "rag_coverage_quality": coverage_payload["rag_coverage_quality"],
        "quality_gate": _quality_gate_projection(
            snapshot=snapshot,
            coverage_summary=coverage_payload["coverage"]["summary"],
            rag_coverage_quality=coverage_payload["rag_coverage_quality"],
            parse_units=parse_units,
            pages=pages,
        ),
        "ocr_decision_trace": trace_payload,
        "parse_units": parse_units,
        "index_manifest": coverage_payload["index_manifest"],
    }
    if normalized_projection == "full":
        payload["job"] = _to_payload(job)
        payload["blocks"] = _to_payload(blocks)
        payload["chunks"] = _to_payload(chunks)
    return payload


def _document_quality_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    structured = _document_projection(snapshot, projection="structured")
    providers = _document_providers_projection(snapshot)
    parts = document_parts_projection(structured)
    provider_diagnostics = _quality_provider_diagnostics(providers)
    parts_diagnostics = _quality_parts_diagnostics(parts)
    attention_summary = _quality_attention_summary(
        doc_id=str(structured["doc_id"]),
        quality_gate=structured["quality_gate"],
        provider_diagnostics=provider_diagnostics,
        parts_diagnostics=parts_diagnostics,
        coverage_summary=structured["coverage_summary"],
        quality_summary=structured["quality_summary"],
    )
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": "quality",
        "doc_id": structured["doc_id"],
        "parse_run_id": structured["parse_run_id"],
        "profile": structured["profile"],
        "profile_resolution": structured["profile_resolution"],
        "local_provider_routing": structured.get("local_provider_routing"),
        "state": structured["state"],
        "quality": structured["quality"],
        "raw_quality": structured["raw_quality"],
        "output_quality": structured["output_quality"],
        "quality_signals": structured["quality_signals"],
        "quality_summary": structured["quality_summary"],
        "coverage_summary": structured["coverage_summary"],
        "rag_coverage_quality": structured["rag_coverage_quality"],
        "quality_gate": structured["quality_gate"],
        "ocr_decision_trace": structured["ocr_decision_trace"],
        "parse_units": structured["parse_units"],
        "provider_diagnostics": provider_diagnostics,
        "parts_diagnostics": parts_diagnostics,
        "attention_summary": attention_summary,
    }


def _quality_provider_diagnostics(providers: Mapping[str, Any]) -> dict[str, Any]:
    comparison_report = providers.get("comparison_report")
    if not isinstance(comparison_report, Mapping):
        comparison_report = {}
    return {
        "summary": _to_payload(dict(providers.get("summary") or {})),
        "comparison_report": {
            "primary_provider_id": str(comparison_report.get("primary_provider_id") or "") or None,
            "best_provider_id": str(comparison_report.get("best_provider_id") or "") or None,
            "summary": _to_payload(dict(comparison_report.get("summary") or {})),
        },
        "comparison_actions": _to_payload(list(providers.get("comparison_actions") or [])),
    }


def _quality_parts_diagnostics(parts: Mapping[str, Any]) -> dict[str, Any]:
    raw_parts = parts.get("parts")
    items = [item for item in raw_parts if isinstance(item, Mapping)] if isinstance(raw_parts, list) else []
    attention_parts: list[dict[str, Any]] = []
    actions: list[Mapping[str, Any]] = []
    for item in items:
        diagnostics = item.get("diagnostics")
        diagnostics_payload = diagnostics if isinstance(diagnostics, Mapping) else {}
        state = str(item.get("state") or "")
        coverage_gap_count = _safe_int(item.get("coverage_gap_count"), default=0)
        coverage_gap_unit_count = _safe_int(item.get("coverage_gap_unit_count"), default=0)
        if (
            state not in {"warning", "failed"}
            and not bool(diagnostics_payload.get("rerun_compared"))
            and coverage_gap_count <= 0
            and coverage_gap_unit_count <= 0
        ):
            continue
        coverage_summary = item.get("coverage_summary")
        coverage_summary_payload = coverage_summary if isinstance(coverage_summary, Mapping) else {}
        rerun_comparison = item.get("rerun_comparison")
        rerun_comparison_payload = rerun_comparison if isinstance(rerun_comparison, Mapping) else {}
        attention_parts.append(
            {
                "part_id": str(item.get("part_id") or ""),
                "state": state,
                "page_range": _to_payload(item.get("page_range") or {}),
                "quality_signal_codes": [str(code) for code in item.get("quality_signal_codes", []) if str(code)],
                "coverage_gap_count": coverage_gap_count,
                "coverage_gap_unit_count": coverage_gap_unit_count,
                "gap_unit_ids": [
                    str(unit_id)
                    for unit_id in coverage_summary_payload.get("gap_unit_ids", [])
                    if str(unit_id)
                ],
                "unembedded_unit_count": _safe_int(coverage_summary_payload.get("unembedded_unit_count"), default=0),
                "selected_provider_id": str(item.get("selected_provider_id") or "") or None,
                "recommended_focus": str(diagnostics_payload.get("recommended_focus") or "") or None,
                "rerun_status": str(diagnostics_payload.get("rerun_status") or "") or None,
                "gap_unit_count_delta": (
                    _safe_int(rerun_comparison_payload.get("gap_unit_count_delta"), default=0)
                    if "gap_unit_count_delta" in rerun_comparison_payload
                    else None
                ),
                "gap_unit_ids_added": [
                    str(unit_id)
                    for unit_id in rerun_comparison_payload.get("gap_unit_ids_added", [])
                    if str(unit_id)
                ],
                "gap_unit_ids_removed": [
                    str(unit_id)
                    for unit_id in rerun_comparison_payload.get("gap_unit_ids_removed", [])
                    if str(unit_id)
                ],
                "provider_changed": bool(diagnostics_payload.get("provider_changed")),
                "action_suggestions": _to_payload(list(item.get("action_suggestions") or [])),
            }
        )
        actions.extend(
            action
            for action in (item.get("action_suggestions") or [])
            if isinstance(action, Mapping)
        )
    return {
        "part_summary": _to_payload(dict(parts.get("part_summary") or {})),
        "attention_parts": attention_parts,
        "actions": _merge_action_suggestions(actions, ()),
    }


def _quality_attention_summary(
    *,
    doc_id: str,
    quality_gate: Mapping[str, Any],
    provider_diagnostics: Mapping[str, Any],
    parts_diagnostics: Mapping[str, Any],
    coverage_summary: Mapping[str, Any],
    quality_summary: Mapping[str, Any],
) -> dict[str, Any]:
    quality_actions = _mapping_list(quality_gate.get("action_suggestions"))
    provider_actions = _mapping_list(provider_diagnostics.get("comparison_actions"))
    part_actions = _mapping_list(parts_diagnostics.get("actions"))
    provider_summary = provider_diagnostics.get("comparison_report")
    if not isinstance(provider_summary, Mapping):
        provider_summary = {}
    provider_summary_payload = provider_summary.get("summary")
    if not isinstance(provider_summary_payload, Mapping):
        provider_summary_payload = {}
    attention_parts = _mapping_list(parts_diagnostics.get("attention_parts"))
    part_focuses = {
        str(item.get("recommended_focus") or "").strip()
        for item in attention_parts
        if str(item.get("recommended_focus") or "").strip()
    }
    quality_action_ids = {
        str(action.get("action_id") or "").strip()
        for action in quality_actions
        if str(action.get("action_id") or "").strip()
    }
    quality_gate_action = str(quality_gate.get("recommended_action") or "").strip() or None
    quality_gate_gate = str(quality_gate.get("gate") or "").strip().lower()
    quality_gate_attention = quality_gate_gate not in {"", "accept", "disabled"}
    provider_attention = bool(provider_summary_payload.get("needs_attention"))
    provider_route_mismatch = bool(provider_summary_payload.get("best_provider_differs_from_primary"))
    part_attention_count = len(attention_parts)
    part_direct_attention = bool(
        part_focuses & {"local_provider_rerun", "provider_route_plan", "parse_ir", "coverage_gaps"}
    )
    part_batch_rerun_available = "rerun_warning_parts" in quality_action_ids

    direct_document_actions = {"rechunk_document", "reembed_document"}
    recommended_focus: str | None = None
    ordered_actions: list[dict[str, Any]] = []
    if quality_gate_action in direct_document_actions:
        recommended_focus = "quality_gate"
        ordered_actions = _merge_action_suggestions(quality_actions, part_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, provider_actions)
    elif quality_gate_action == "local_provider_rerun" and part_batch_rerun_available:
        recommended_focus = "parts"
        ordered_actions = _merge_action_suggestions(quality_actions, part_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, provider_actions)
    elif provider_route_mismatch:
        recommended_focus = "providers"
        ordered_actions = _merge_action_suggestions(provider_actions, part_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, quality_actions)
    elif quality_gate_action == "local_provider_rerun" and part_direct_attention:
        recommended_focus = "parts"
        ordered_actions = _merge_action_suggestions(part_actions, provider_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, quality_actions)
    elif part_attention_count > 0 and (part_direct_attention or not provider_attention):
        recommended_focus = "parts"
        ordered_actions = _merge_action_suggestions(part_actions, quality_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, provider_actions)
    elif provider_attention:
        recommended_focus = "providers"
        ordered_actions = _merge_action_suggestions(provider_actions, quality_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, part_actions)
    elif quality_gate_attention:
        recommended_focus = "quality_gate"
        ordered_actions = _merge_action_suggestions(quality_actions, provider_actions)
        ordered_actions = _merge_action_suggestions(ordered_actions, part_actions)

    recommended_action = str(ordered_actions[0].get("action_id") or "").strip() if ordered_actions else quality_gate_action
    if not recommended_action:
        recommended_action = None

    if recommended_focus == "providers":
        recommended_entrypoint = f"/v1/parse/documents/{doc_id}/providers"
    elif recommended_focus == "parts":
        recommended_entrypoint = f"/v1/parse/documents/{doc_id}/parts"
    elif recommended_focus == "quality_gate":
        recommended_entrypoint = f"/v1/parse/documents/{doc_id}/quality"
    else:
        recommended_entrypoint = None

    entrypoints = _quality_diagnostic_entrypoints(
        doc_id=doc_id,
        recommended_focus=recommended_focus,
        quality_gate_attention=quality_gate_attention,
        provider_attention=provider_attention,
        part_attention_count=part_attention_count,
        provider_diagnostics=provider_diagnostics,
        parts_diagnostics=parts_diagnostics,
        coverage_summary=coverage_summary,
        quality_summary=quality_summary,
    )
    return {
        "needs_attention": bool(quality_gate_attention or provider_attention or part_attention_count > 0),
        "quality_gate_attention": quality_gate_attention,
        "provider_attention": provider_attention,
        "part_attention_count": part_attention_count,
        "recommended_focus": recommended_focus,
        "recommended_action": recommended_action,
        "recommended_entrypoint": recommended_entrypoint,
        "recommended_actions": ordered_actions,
        "entrypoints": entrypoints,
        "contracts": _quality_attention_contracts(
            doc_id=doc_id,
            recommended_focus=recommended_focus,
            recommended_actions=ordered_actions,
            entrypoints=entrypoints,
            parts_diagnostics=parts_diagnostics,
        ),
        "attention_sources": {
            "quality_gate": quality_gate_attention,
            "providers": provider_attention,
            "parts": part_attention_count,
        },
    }


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _quality_diagnostic_entrypoints(
    *,
    doc_id: str,
    recommended_focus: str | None,
    quality_gate_attention: bool,
    provider_attention: bool,
    part_attention_count: int,
    provider_diagnostics: Mapping[str, Any],
    parts_diagnostics: Mapping[str, Any],
    coverage_summary: Mapping[str, Any],
    quality_summary: Mapping[str, Any],
) -> dict[str, Any]:
    provider_summary = provider_diagnostics.get("comparison_report")
    if not isinstance(provider_summary, Mapping):
        provider_summary = {}
    provider_summary_payload = provider_summary.get("summary")
    if not isinstance(provider_summary_payload, Mapping):
        provider_summary_payload = {}
    provider_attention_count = len(
        [item for item in provider_summary_payload.get("attention_provider_ids", []) if str(item)]
    )
    coverage_gap_pages = _safe_int(coverage_summary.get("pages_with_coverage_gaps"), default=0)
    quality_signal_total = _safe_int(quality_summary.get("total"), default=0)
    attention_parts = _mapping_list(parts_diagnostics.get("attention_parts"))
    attention_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if str(item.get("part_id") or "").strip()
    ]
    rerun_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if any(
            str(action.get("action_id") or "") in {"rerun_part"}
            for action in _mapping_list(item.get("action_suggestions"))
        )
    ]
    provider_changed_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if bool(item.get("provider_changed"))
    ]
    coverage_gap_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if _safe_int(item.get("coverage_gap_count"), default=0) > 0
    ]
    coverage_gap_unit_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if _safe_int(item.get("coverage_gap_unit_count"), default=0) > 0
    ]
    rerun_gap_unit_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if _safe_int(item.get("gap_unit_count_delta"), default=0) != 0
    ]
    unembedded_part_ids = [
        str(item.get("part_id") or "")
        for item in attention_parts
        if _safe_int(item.get("unembedded_unit_count"), default=0) > 0
    ]
    gap_unit_ids = sorted(
        {
            str(unit_id)
            for item in attention_parts
            for unit_id in item.get("gap_unit_ids", [])
            if str(unit_id)
        }
    )
    coverage_gap_page_numbers = sorted(
        {
            _safe_int(page.get("page_number"), default=0)
            for page in ((coverage_summary.get("gap_pages") or []) if isinstance(coverage_summary.get("gap_pages"), list) else [])
            if isinstance(page, Mapping) and _safe_int(page.get("page_number"), default=0) > 0
        }
    )
    return {
        "quality": {
            "endpoint": f"/v1/parse/documents/{doc_id}/quality",
            "state": "attention" if quality_gate_attention else "ok",
            "recommended": recommended_focus == "quality_gate",
            "attention_count": quality_signal_total,
            "context": {
                "quality_signal_total": quality_signal_total,
                "coverage_gap_pages": coverage_gap_pages,
            },
        },
        "providers": {
            "endpoint": f"/v1/parse/documents/{doc_id}/providers",
            "state": "attention" if provider_attention else "ok",
            "recommended": recommended_focus == "providers",
            "attention_count": provider_attention_count,
            "context": {
                "primary_provider_id": str(provider_summary.get("primary_provider_id") or "") or None,
                "best_provider_id": str(provider_summary.get("best_provider_id") or "") or None,
                "attention_provider_ids": [
                    str(item)
                    for item in provider_summary_payload.get("attention_provider_ids", [])
                    if str(item)
                ],
            },
        },
        "parts": {
            "endpoint": f"/v1/parse/documents/{doc_id}/parts",
            "state": "attention" if part_attention_count > 0 else "ok",
            "recommended": recommended_focus == "parts",
            "attention_count": part_attention_count,
            "params": {"state": "warning|failed"} if part_attention_count > 0 else {},
            "context": {
                "attention_part_ids": attention_part_ids,
                "rerun_part_ids": rerun_part_ids,
                "provider_changed_part_ids": provider_changed_part_ids,
                "coverage_gap_part_ids": coverage_gap_part_ids,
                "coverage_gap_unit_part_ids": coverage_gap_unit_part_ids,
                "rerun_gap_unit_part_ids": rerun_gap_unit_part_ids,
                "unembedded_part_ids": unembedded_part_ids,
                "gap_unit_ids": gap_unit_ids,
            },
        },
        "coverage": {
            "endpoint": f"/v1/parse/documents/{doc_id}/coverage",
            "state": "attention" if coverage_gap_pages > 0 else "ok",
            "recommended": recommended_focus == "coverage",
            "attention_count": coverage_gap_pages,
            "context": {
                "gap_page_numbers": coverage_gap_page_numbers,
                "gap_unit_ids": [
                    str(item)
                    for item in coverage_summary.get("gap_unit_ids", [])
                    if str(item)
                ],
                "pages_missing_rag_units": _safe_int(coverage_summary.get("pages_missing_rag_units"), default=0),
                "pages_missing_chunks": _safe_int(coverage_summary.get("pages_missing_chunks"), default=0),
                "pages_chunks_not_embedded": _safe_int(
                    coverage_summary.get("pages_chunks_not_embedded"), default=0
                ),
                "skipped_unit_count": _safe_int(coverage_summary.get("skipped_unit_count"), default=0),
                "unembedded_unit_count": _safe_int(coverage_summary.get("unembedded_unit_count"), default=0),
            },
        },
    }


def _quality_attention_contracts(
    *,
    doc_id: str,
    recommended_focus: str | None,
    recommended_actions: Sequence[Mapping[str, Any]],
    entrypoints: Mapping[str, Any],
    parts_diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    recommended_requests = [
        contract
        for index, action in enumerate(recommended_actions, start=1)
        if (contract := _action_request_contract(action, contract_id=f"recommended:{index}")) is not None
    ]
    entrypoint_requests = _quality_entrypoint_contracts(entrypoints)
    parts_batch_rerun_requests = _quality_parts_batch_rerun_contracts(
        doc_id=doc_id,
        parts_diagnostics=parts_diagnostics,
    )
    recommended_requests = _quality_merge_batch_rerun_request_contexts(
        recommended_requests=recommended_requests,
        parts_batch_rerun_requests=parts_batch_rerun_requests,
    )
    default_request = recommended_requests[0] if recommended_requests else None
    inspect_requests = _merge_request_contracts(
        [contract for contract in recommended_requests if str(((contract.get("request") or {}).get("method") or "")).upper() == "GET"],
        [
            contract
            for view, descriptor in entrypoint_requests.items()
            if (contract := _entrypoint_request_contract(view=view, descriptor=descriptor)) is not None
        ],
        dedupe_on_action=True,
    )
    execute_requests = _quality_execute_contracts(
        recommended_requests=[
            contract
            for contract in recommended_requests
            if str(((contract.get("request") or {}).get("method") or "")).upper() == "POST"
        ],
        parts_batch_rerun_requests=parts_batch_rerun_requests,
    )
    workflow = _quality_contract_workflow(
        recommended_focus=recommended_focus,
        recommended_requests=recommended_requests,
        inspect_requests=inspect_requests,
        execute_requests=execute_requests,
    )
    return {
        "default_request": default_request,
        "recommended_requests": recommended_requests,
        "entrypoint_requests": entrypoint_requests,
        "parts_batch_rerun_requests": parts_batch_rerun_requests,
        "inspect_requests": inspect_requests,
        "execute_requests": execute_requests,
        "preferred_execute_request": execute_requests[0] if execute_requests else None,
        "workflow": workflow,
    }


def _quality_entrypoint_contracts(entrypoints: Mapping[str, Any]) -> dict[str, Any]:
    contracts: dict[str, Any] = {}
    for key, entry in entrypoints.items():
        if not isinstance(entry, Mapping):
            continue
        endpoint = str(entry.get("endpoint") or "").strip()
        if not endpoint:
            continue
        request: dict[str, Any] = {
            "method": "GET",
            "endpoint": endpoint,
        }
        params = entry.get("params")
        if isinstance(params, Mapping) and params:
            request["params"] = _to_payload(dict(params))
        contracts[str(key)] = {
            "contract_id": f"entrypoint:{key}",
            "action_id": f"open_{key}",
            "view": str(key),
            "request": request,
            "state": str(entry.get("state") or "ok"),
            "recommended": bool(entry.get("recommended")),
            "attention_count": _safe_int(entry.get("attention_count"), default=0),
            "context": _to_payload(dict(entry.get("context") or {})) if isinstance(entry.get("context"), Mapping) else {},
        }
    return contracts


def _entrypoint_request_contract(
    *,
    view: str,
    descriptor: Mapping[str, Any],
) -> dict[str, Any] | None:
    request = descriptor.get("request")
    if not isinstance(request, Mapping):
        return None
    method = str(request.get("method") or "").strip()
    endpoint = str(request.get("endpoint") or "").strip()
    if not method or not endpoint:
        return None
    contract: dict[str, Any] = {
        "contract_id": f"entrypoint:{view}",
        "action_id": f"open_{view}",
        "scope": "document_view",
        "reason_codes": [],
        "request": _to_payload(dict(request)),
        "auto_execute": False,
        "view": view,
        "recommended": bool(descriptor.get("recommended")),
        "attention_count": _safe_int(descriptor.get("attention_count"), default=0),
        "state": str(descriptor.get("state") or "ok"),
    }
    context = descriptor.get("context")
    if isinstance(context, Mapping) and context:
        contract["context"] = _to_payload(dict(context))
    return contract


def _quality_parts_batch_rerun_contracts(
    *,
    doc_id: str,
    parts_diagnostics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    attention_parts = _mapping_list(parts_diagnostics.get("attention_parts"))
    grouped: dict[Any, dict[str, Any]] = {}
    for item in attention_parts:
        part_id = str(item.get("part_id") or "").strip()
        if not part_id:
            continue
        for action in _mapping_list(item.get("action_suggestions")):
            if str(action.get("action_id") or "") != "rerun_part":
                continue
            payload = action.get("payload")
            payload_mapping = dict(payload) if isinstance(payload, Mapping) else {}
            fingerprint = _contract_fingerprint(payload_mapping)
            group = grouped.setdefault(
                fingerprint,
                {
                    "part_ids": [],
                    "reason_codes": [],
                    "payload": _to_payload(payload_mapping),
                    "required_capabilities": [],
                    "attention_parts": [],
                    "coverage_gap_part_ids": [],
                    "coverage_gap_unit_part_ids": [],
                    "unembedded_part_ids": [],
                    "gap_unit_ids": [],
                },
            )
            group["part_ids"].append(part_id)
            group["reason_codes"].extend(str(code) for code in action.get("reason_codes", []) if str(code))
            part_payload = _quality_attention_part_contract_payload(item)
            group["attention_parts"].append(part_payload)
            if _safe_int(part_payload.get("coverage_gap_count"), default=0) > 0:
                group["coverage_gap_part_ids"].append(part_id)
            if _safe_int(part_payload.get("coverage_gap_unit_count"), default=0) > 0:
                group["coverage_gap_unit_part_ids"].append(part_id)
            if _safe_int(part_payload.get("unembedded_unit_count"), default=0) > 0:
                group["unembedded_part_ids"].append(part_id)
            group["gap_unit_ids"].extend(
                str(unit_id)
                for unit_id in part_payload.get("gap_unit_ids", [])
                if str(unit_id)
            )
            provider_route_plan = payload_mapping.get("provider_route_plan")
            if isinstance(provider_route_plan, Mapping):
                group["required_capabilities"].extend(
                    str(capability)
                    for capability in provider_route_plan.get("required_capabilities", [])
                    if str(capability)
                )

    contracts: list[dict[str, Any]] = []
    for index, group in enumerate(grouped.values(), start=1):
        payload: dict[str, Any] = {"part_ids": list(group["part_ids"]), "failed_only": False}
        if isinstance(group.get("payload"), Mapping):
            payload.update(dict(group["payload"]))
        contracts.append(
            {
                "contract_id": f"parts-batch-rerun:{index}",
                "action_id": "rerun_attention_parts",
                "scope": "parts",
                "reason_codes": list(dict.fromkeys(group["reason_codes"])),
                "target_count": len(group["part_ids"]),
                "part_ids": list(group["part_ids"]),
                "required_capabilities": list(dict.fromkeys(group["required_capabilities"])),
                "request": {
                    "method": "POST",
                    "endpoint": f"/v1/parse/documents/{doc_id}/parts/rerun",
                    "payload": payload,
                },
                "auto_execute": False,
                "context": {
                    "attention_part_ids": list(group["part_ids"]),
                    "coverage_gap_part_ids": list(dict.fromkeys(group["coverage_gap_part_ids"])),
                    "coverage_gap_unit_part_ids": list(dict.fromkeys(group["coverage_gap_unit_part_ids"])),
                    "unembedded_part_ids": list(dict.fromkeys(group["unembedded_part_ids"])),
                    "gap_unit_ids": sorted({str(unit_id) for unit_id in group["gap_unit_ids"] if str(unit_id)}),
                    "attention_parts": _to_payload(list(group["attention_parts"])),
                },
            }
        )
    return contracts


def _quality_merge_batch_rerun_request_contexts(
    *,
    recommended_requests: Sequence[Mapping[str, Any]],
    parts_batch_rerun_requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    batch_by_request: dict[Any, Mapping[str, Any]] = {}
    for contract in parts_batch_rerun_requests:
        request = contract.get("request")
        if not isinstance(request, Mapping):
            continue
        fingerprint = _quality_batch_rerun_match_key(request)
        batch_by_request[fingerprint] = contract

    merged: list[dict[str, Any]] = []
    for contract in recommended_requests:
        payload = _to_payload(dict(contract))
        request = payload.get("request")
        if not isinstance(request, Mapping):
            merged.append(payload)
            continue
        fingerprint = _quality_batch_rerun_match_key(request)
        batch_contract = batch_by_request.get(fingerprint)
        if not isinstance(batch_contract, Mapping):
            merged.append(payload)
            continue
        batch_context = batch_contract.get("context")
        current_context = payload.get("context")
        if isinstance(batch_context, Mapping):
            merged_context = dict(batch_context)
            if isinstance(current_context, Mapping):
                merged_context.update(dict(current_context))
            payload["context"] = _to_payload(merged_context)
        for key in ("target_count", "part_ids", "required_capabilities"):
            if key not in payload and key in batch_contract:
                payload[key] = _to_payload(batch_contract.get(key))
        merged.append(payload)
    return merged


def _quality_batch_rerun_match_key(request: Mapping[str, Any]) -> Any:
    payload = request.get("payload")
    part_ids = []
    if isinstance(payload, Mapping):
        part_ids = sorted(str(part_id) for part_id in payload.get("part_ids", []) if str(part_id))
    return _contract_fingerprint(
        {
            "method": str(request.get("method") or ""),
            "endpoint": _request_endpoint({"request": request}),
            "part_ids": part_ids,
        }
    )


def _quality_contract_workflow(
    *,
    recommended_focus: str | None,
    recommended_requests: Sequence[Mapping[str, Any]],
    inspect_requests: Sequence[Mapping[str, Any]],
    execute_requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    recommended_focus = str(recommended_focus or "").strip() or None
    inspect_contracts = [item for item in inspect_requests if isinstance(item, Mapping)]
    execute_contracts = [item for item in execute_requests if isinstance(item, Mapping)]
    recommended_contracts = [item for item in recommended_requests if isinstance(item, Mapping)]

    preferred_inspect = _preferred_inspect_contract(
        recommended_focus=recommended_focus,
        inspect_requests=inspect_contracts,
        recommended_requests=recommended_contracts,
    )
    preferred_compare = _preferred_compare_contract(
        recommended_focus=recommended_focus,
        inspect_requests=inspect_contracts,
    )
    preferred_execute = execute_contracts[0] if execute_contracts else None
    preferred_verify = _preferred_verify_contract(
        recommended_focus=recommended_focus,
        inspect_requests=inspect_contracts,
    )

    phases: list[dict[str, Any]] = []
    if preferred_inspect or inspect_contracts:
        phases.append(
            _workflow_phase(
                phase="inspect",
                label="Inspect attention",
                preferred=preferred_inspect,
                contracts=inspect_contracts,
            )
        )
    compare_contracts = [
        contract
        for contract in inspect_contracts
        if str(contract.get("action_id") or "") in {"inspect_provider_comparison", "inspect_provider_route_plan"}
    ]
    if preferred_compare or compare_contracts:
        phases.append(
            _workflow_phase(
                phase="compare",
                label="Compare providers",
                preferred=preferred_compare,
                contracts=compare_contracts,
            )
        )
    if preferred_execute or execute_contracts:
        phases.append(
            _workflow_phase(
                phase="execute",
                label="Apply recommended fix",
                preferred=preferred_execute,
                contracts=execute_contracts,
            )
        )
    if preferred_verify:
        phases.append(
            _workflow_phase(
                phase="verify",
                label="Verify outcome",
                preferred=preferred_verify,
                contracts=[preferred_verify],
            )
        )

    return {
        "default_phase": phases[0]["phase"] if phases else None,
        "phases": phases,
    }


def _workflow_phase(
    *,
    phase: str,
    label: str,
    preferred: Mapping[str, Any] | None,
    contracts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    contract_ids = [
        str(contract.get("contract_id") or "")
        for contract in contracts
        if str(contract.get("contract_id") or "")
    ]
    preferred_contract_id = str(preferred.get("contract_id") or "") if isinstance(preferred, Mapping) else ""
    return {
        "phase": phase,
        "label": label,
        "preferred_contract_id": preferred_contract_id or None,
        "contract_ids": list(dict.fromkeys(contract_ids)),
        "ready": bool(preferred_contract_id or contract_ids),
    }


def _preferred_inspect_contract(
    *,
    recommended_focus: str | None,
    inspect_requests: Sequence[Mapping[str, Any]],
    recommended_requests: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if recommended_focus:
        target_view = _attention_focus_view(recommended_focus)
        target_action = f"open_{target_view}" if target_view else ""
        for contract in inspect_requests:
            if str(contract.get("action_id") or "") == target_action:
                return contract
    for contract in recommended_requests:
        if str(((contract.get("request") or {}).get("method") or "")).upper() == "GET":
            return contract
    return inspect_requests[0] if inspect_requests else None


def _attention_focus_view(recommended_focus: str | None) -> str | None:
    focus = str(recommended_focus or "").strip()
    if not focus:
        return None
    return {
        "quality_gate": "quality",
        "quality_review": "quality",
        "providers": "providers",
        "provider_route_plan": "providers",
        "parts": "parts",
        "local_provider_rerun": "parts",
        "coverage": "coverage",
        "coverage_gaps": "coverage",
    }.get(focus, focus)


def _preferred_compare_contract(
    *,
    recommended_focus: str | None = None,
    inspect_requests: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    preferred_action_ids = ("inspect_provider_comparison", "inspect_provider_route_plan")
    if _attention_focus_view(recommended_focus) == "parts":
        preferred_action_ids = ("inspect_provider_route_plan", "inspect_provider_comparison")
    for action_id in preferred_action_ids:
        for contract in inspect_requests:
            if str(contract.get("action_id") or "") == action_id:
                return contract
    return None


def _preferred_verify_contract(
    *,
    recommended_focus: str | None,
    inspect_requests: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    action_ids = ["open_quality"]
    if recommended_focus:
        action_ids.append(f"open_{recommended_focus}")
    for action_id in action_ids:
        if not action_id:
            continue
        for contract in inspect_requests:
            if str(contract.get("action_id") or "") == action_id:
                return contract
    return inspect_requests[0] if inspect_requests else None


def _quality_execute_contracts(
    *,
    recommended_requests: Sequence[Mapping[str, Any]],
    parts_batch_rerun_requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    has_recommended_batch_rerun = any(
        _request_endpoint(contract) == "/parts/rerun"
        for contract in recommended_requests
    )
    batch_contracts = [] if has_recommended_batch_rerun else list(parts_batch_rerun_requests)
    covered_part_ids = set()
    for contract in [*recommended_requests, *batch_contracts]:
        request = contract.get("request")
        if not isinstance(request, Mapping):
            continue
        if not str(request.get("endpoint") or "").endswith("/parts/rerun"):
            continue
        payload = request.get("payload")
        if isinstance(payload, Mapping):
            covered_part_ids.update(
                str(part_id)
                for part_id in payload.get("part_ids", [])
                if str(part_id)
            )

    curated_primary: list[Mapping[str, Any]] = []
    for contract in recommended_requests:
        action_id = str(contract.get("action_id") or "")
        if action_id == "rerun_part":
            request = contract.get("request")
            endpoint = str((request or {}).get("endpoint") or "")
            part_id = _part_id_from_rerun_endpoint(endpoint)
            if part_id and part_id in covered_part_ids:
                continue
        curated_primary.append(contract)

    return _merge_request_contracts(curated_primary, batch_contracts)


def _merge_request_contracts(
    primary: Sequence[Mapping[str, Any]],
    secondary: Sequence[Mapping[str, Any]],
    *,
    dedupe_on_action: bool = False,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[Any] = set()

    def add(contract: Mapping[str, Any]) -> None:
        request = contract.get("request")
        if not isinstance(request, Mapping):
            return
        fingerprint = _contract_fingerprint(
            {
                "action_id": str(contract.get("action_id") or "") if dedupe_on_action else None,
                "method": str(request.get("method") or ""),
                "endpoint": str(request.get("endpoint") or ""),
                "params": request.get("params"),
                "payload": request.get("payload"),
            }
        )
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        merged.append(_to_payload(dict(contract)))

    for contract in primary:
        if isinstance(contract, Mapping):
            add(contract)
    for contract in secondary:
        if isinstance(contract, Mapping):
            add(contract)
    return merged


def _request_endpoint(contract: Mapping[str, Any]) -> str:
    request = contract.get("request")
    if not isinstance(request, Mapping):
        return ""
    endpoint = str(request.get("endpoint") or "").strip()
    if endpoint.endswith("/parts/rerun"):
        return "/parts/rerun"
    return endpoint


def _part_id_from_rerun_endpoint(endpoint: str) -> str | None:
    marker = "/parts/"
    suffix = "/rerun"
    if marker not in endpoint or not endpoint.endswith(suffix):
        return None
    part_id = endpoint.split(marker, 1)[1][:-len(suffix)]
    part_id = part_id.strip("/")
    return part_id or None


def _action_request_contract(
    action: Mapping[str, Any],
    *,
    contract_id: str,
) -> dict[str, Any] | None:
    action_id = str(action.get("action_id") or "").strip()
    method = str(action.get("method") or "").strip()
    endpoint = str(action.get("endpoint") or "").strip()
    if not action_id or not method or not endpoint:
        return None
    request: dict[str, Any] = {
        "method": method,
        "endpoint": endpoint,
    }
    params = action.get("params")
    if isinstance(params, Mapping) and params:
        request["params"] = _to_payload(dict(params))
    payload = action.get("payload")
    if isinstance(payload, Mapping) and payload:
        request["payload"] = _to_payload(dict(payload))
    contract = {
        "contract_id": contract_id,
        "action_id": action_id,
        "scope": str(action.get("scope") or ""),
        "reason_codes": [str(code) for code in action.get("reason_codes", []) if str(code)],
        "request": request,
        "auto_execute": bool(action.get("auto_execute")),
    }
    context = action.get("context")
    if isinstance(context, Mapping) and context:
        contract["context"] = _to_payload(dict(context))
    return contract


def _contract_fingerprint(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple((str(key), _contract_fingerprint(item)) for key, item in sorted(value.items(), key=lambda pair: str(pair[0])))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_contract_fingerprint(item) for item in value)
    return value


def _document_reader_projection(ir: Mapping[str, Any]) -> dict[str, Any]:
    ir_blocks = [dict(block) for block in ir.get("blocks", []) if isinstance(block, Mapping)]
    ir_tables = [dict(table) for table in ir.get("tables", []) if isinstance(table, Mapping)]
    ir_figures = [dict(figure) for figure in ir.get("figures", []) if isinstance(figure, Mapping)]
    ir_units = [dict(unit) for unit in ir.get("knowledge_units", []) if isinstance(unit, Mapping)]
    ir_pages_by_number = {
        _safe_int(page.get("page_number"), default=1): dict(page)
        for page in ir.get("pages", [])
        if isinstance(page, Mapping)
    }
    coverage_pages = [
        dict(page)
        for page in ((ir.get("coverage") or {}).get("pages") or [])
        if isinstance(page, Mapping)
    ]
    tables_by_block = {str(table.get("block_id") or ""): table for table in ir_tables if table.get("block_id")}
    figures_by_block = {str(figure.get("block_id") or ""): figure for figure in ir_figures if figure.get("block_id")}
    units_by_block = _reader_units_by_source(ir_units, "source_block_ids")
    units_by_table = _reader_units_by_source(ir_units, "source_table_ids")
    used_table_ids: set[str] = set()
    used_figure_ids: set[str] = set()
    hidden_counts_by_page: Counter[int] = Counter()
    reader_blocks: list[dict[str, Any]] = []

    sorted_blocks = sorted(
        ir_blocks,
        key=lambda block: (
            _safe_int(block.get("page_number"), default=1),
            _safe_int(block.get("reading_order"), default=0),
            str(block.get("block_id") or ""),
        ),
    )
    for block in sorted_blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        reader_policy = str(block.get("reader_policy") or "inline")
        block_id = str(block.get("block_id") or "")
        if reader_policy == "hidden":
            hidden_counts_by_page[page_number] += 1
            continue
        table = tables_by_block.get(block_id)
        figure = figures_by_block.get(block_id)
        units = _reader_units_for_block(
            block=block,
            table=table,
            units_by_block=units_by_block,
            units_by_table=units_by_table,
        )
        if reader_policy == "table" or table:
            reader_blocks.append(
                _reader_table_block(
                    block=block,
                    table=table,
                    knowledge_units=units,
                    index=len(reader_blocks) + 1,
                )
            )
            if table and table.get("table_id"):
                used_table_ids.add(str(table.get("table_id")))
            continue
        if reader_policy == "source_snapshot" or figure or str(block.get("display_kind") or "") == "figure":
            reader_blocks.append(
                _reader_figure_block(
                    block=block,
                    figure=figure,
                    knowledge_units=units,
                    index=len(reader_blocks) + 1,
                )
            )
            if figure and figure.get("figure_id"):
                used_figure_ids.add(str(figure.get("figure_id")))
            continue
        reader_blocks.append(_reader_text_block(block=block, knowledge_units=units, index=len(reader_blocks) + 1))

    for table in ir_tables:
        table_id = str(table.get("table_id") or "")
        if table_id and table_id not in used_table_ids:
            units = _reader_unique_units(
                list(units_by_table.get(table_id, ()))
                + list(units_by_block.get(str(table.get("block_id") or ""), ()))
            )
            reader_blocks.append(
                _reader_orphan_table_block(table=table, knowledge_units=units, index=len(reader_blocks) + 1)
            )
            used_table_ids.add(table_id)
    for figure in ir_figures:
        figure_id = str(figure.get("figure_id") or "")
        if figure_id and figure_id not in used_figure_ids:
            figure_units = _reader_unique_units(units_by_block.get(str(figure.get("block_id") or ""), ()))
            reader_blocks.append(
                _reader_orphan_figure_block(
                    figure=figure,
                    knowledge_units=figure_units,
                    index=len(reader_blocks) + 1,
                )
            )
            used_figure_ids.add(figure_id)

    block_ids_by_page: dict[int, list[str]] = {}
    type_counts: Counter[str] = Counter()
    for block in reader_blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        block_ids_by_page.setdefault(page_number, []).append(str(block.get("reader_block_id") or ""))
        type_counts[str(block.get("type") or "unknown")] += 1

    coverage_by_page = {
        _safe_int(page.get("page_number"), default=1): page
        for page in coverage_pages
    }
    page_numbers = sorted(
        {
            _safe_int(page.get("page_number"), default=1)
            for page in ir.get("pages", [])
            if isinstance(page, Mapping)
        }
        | set(block_ids_by_page)
        | set(coverage_by_page)
        | set(hidden_counts_by_page)
    )
    pages: list[dict[str, Any]] = []
    for page_number in page_numbers:
        ir_page = ir_pages_by_number.get(page_number, {})
        coverage_page = coverage_by_page.get(page_number, {})
        quality_signal_codes = [
            str(code)
            for code in coverage_page.get("quality_signal_codes", []) or []
            if str(code)
        ]
        pages.append(
            {
                "page_id": str(ir_page.get("page_id") or f"p{page_number:04d}"),
                "page_number": page_number,
                "page_type": str(ir_page.get("page_type") or "body"),
                "width": ir_page.get("width"),
                "height": ir_page.get("height"),
                "rotation": _safe_int(ir_page.get("rotation"), default=0),
                "source_kind": str(ir_page.get("source_kind") or "unknown"),
                "block_ids": _string_list_payload(ir_page.get("block_ids")),
                "reader_block_ids": block_ids_by_page.get(page_number, []),
                "reader_block_count": len(block_ids_by_page.get(page_number, [])),
                "hidden_block_count": hidden_counts_by_page.get(page_number, 0),
                "quality_flags": _string_list_payload(ir_page.get("quality_flags")),
                "reading_order_confidence": ir_page.get("reading_order_confidence"),
                "quality_signal_codes": list(dict.fromkeys(quality_signal_codes)),
                "coverage_missing_reason": coverage_page.get("missing_reason"),
                "provider_ids": _string_list_payload(coverage_page.get("provider_ids")),
            }
        )

    quality_signals = [
        dict(signal)
        for signal in list(ir.get("quality_signals") or []) + list(ir.get("coverage_quality_signals") or [])
        if isinstance(signal, Mapping)
    ]
    _annotate_reader_block_quality_signals(
        reader_blocks,
        coverage_by_page=coverage_by_page,
        quality_signals=quality_signals,
    )
    return {
        "schema_version": READER_SCHEMA_VERSION,
        "projection": "reader",
        "doc_id": ir.get("doc_id"),
        "parse_run_id": ir.get("parse_run_id"),
        "source_integrity": ir.get("source_integrity"),
        "knowledge_unit_diff": ir.get("knowledge_unit_diff"),
        "profile": ir.get("profile"),
        "profile_resolution": ir.get("profile_resolution"),
        "local_provider_routing": ir.get("local_provider_routing"),
        "state": ir.get("state"),
        "pages": pages,
        "blocks": reader_blocks,
        "reader_summary": {
            "page_count": len(pages),
            "block_count": len(reader_blocks),
            "hidden_block_count": sum(hidden_counts_by_page.values()),
            "by_type": dict(sorted(type_counts.items())),
            "table_blocks": type_counts.get("table", 0),
            "figure_blocks": type_counts.get("figure", 0),
            "pages_with_quality_signals": sum(1 for page in pages if page["quality_signal_codes"]),
        },
        "quality_signals": quality_signals,
        "quality_gate": ir.get("quality_gate"),
        "rag_coverage_quality": ir.get("rag_coverage_quality"),
        "index_manifest": ir.get("index_manifest"),
    }


def _reader_text_block(
    *,
    block: Mapping[str, Any],
    knowledge_units: Sequence[Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    block_type = str(block.get("type") or "paragraph")
    rag_text = _reader_rag_text(knowledge_units)
    return _reader_base_block(
        block=block,
        index=index,
        block_type="title" if block_type == "title" else "text",
        display_kind=str(block.get("display_kind") or "text"),
        text=_normalize_reader_text(rag_text or str(block.get("text") or "")),
        source_table_ids=[],
        source_figure_ids=[],
        knowledge_units=knowledge_units,
    )


_READER_STRUCTURAL_LINE_PATTERN = re.compile(
    r"^(?:[-*•·−–—]|\d+[.)、])\s+"
    r"|^[（(]\s*(?:\d+|[a-z]{1,4})\s*[)）]\s+"
    r"|^\d{1,2}(?:\.\d+){1,5}\s+\S"
    r"|^(?:SECTION|SUBPART|APPENDIX|MODULE|Article|Appendix|AMC\d*|GM\d*|"
    r"ED\s+Decision|Regulation\s+\(EU\)|[A-Z]{1,6}(?:\.[A-Z0-9]+){1,4}\s+)\b",
    re.IGNORECASE,
)


def _normalize_reader_text(text: str) -> str:
    """Keep regulatory/list boundaries while joining wrapped continuation lines."""
    lines = [line.strip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    if len(lines) <= 1:
        return lines[0] if lines else ""

    paragraphs: list[str] = []
    current: list[str] = []
    current_is_structural = False

    def flush() -> None:
        nonlocal current, current_is_structural
        if not current:
            return
        joined = current[0]
        for line in current[1:]:
            if re.search(r"[A-Za-z]-$", joined) and re.match(r"^[a-z]", line):
                joined = f"{joined[:-1]}{line}"
            else:
                joined = f"{joined} {line}"
        paragraphs.append(joined)
        current = []
        current_is_structural = False

    for line in lines:
        structural = bool(_READER_STRUCTURAL_LINE_PATTERN.search(line))
        if structural:
            flush()
            current.append(line)
            current_is_structural = True
            continue
        previous_line = current[-1] if current else ""
        if current_is_structural and re.search(r"[。！？.!?；;]$", previous_line) and re.match(r"^[A-Z][a-z]", line):
            flush()
        current.append(line)
        if not current_is_structural and re.search(r"[。！？.!?；;:]$", line):
            flush()
    flush()
    return "\n\n".join(paragraphs)


def _reader_table_block(
    *,
    block: Mapping[str, Any],
    table: Mapping[str, Any] | None,
    knowledge_units: Sequence[Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    source_table_ids = [str(table.get("table_id"))] if table and table.get("table_id") else []
    rag_text = _reader_rag_text(knowledge_units)
    payload = _reader_base_block(
        block=block,
        index=index,
        block_type="table",
        display_kind="table",
        text=str((table or {}).get("caption") or rag_text or block.get("text") or ""),
        source_table_ids=source_table_ids,
        source_figure_ids=[],
        knowledge_units=knowledge_units,
    )
    if table:
        payload["table"] = dict(table)
    return payload


def _reader_figure_block(
    *,
    block: Mapping[str, Any],
    figure: Mapping[str, Any] | None,
    knowledge_units: Sequence[Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    source_figure_ids = [str(figure.get("figure_id"))] if figure and figure.get("figure_id") else []
    rag_text = _reader_rag_text(knowledge_units)
    payload = _reader_base_block(
        block=block,
        index=index,
        block_type="figure",
        display_kind="figure",
        text=str((figure or {}).get("caption") or rag_text or (figure or {}).get("alt_text") or block.get("text") or ""),
        source_table_ids=[],
        source_figure_ids=source_figure_ids,
        knowledge_units=knowledge_units,
    )
    if figure:
        payload["figure"] = dict(figure)
    return payload


def _reader_orphan_table_block(
    *,
    table: Mapping[str, Any],
    knowledge_units: Sequence[Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    page_number = _safe_int(table.get("page_number"), default=1)
    table_id = str(table.get("table_id") or "")
    unit_payloads = _reader_unit_payloads(knowledge_units)
    rag_text = _reader_rag_text(knowledge_units)
    return {
        "reader_block_id": f"reader:{index:06d}",
        "page_number": page_number,
        "page_span": [page_number, page_number],
        "type": "table",
        "display_kind": "table",
        "reader_policy": "table",
        "semantic_role": str(table.get("semantic_role") or "table"),
        "index_policy": str(table.get("index_policy") or "index_table_summary_and_cells"),
        "text": str(table.get("caption") or rag_text or ""),
        "rag_text": rag_text,
        "source_unit_ids": [str(unit.get("unit_id")) for unit in unit_payloads if unit.get("unit_id")],
        "source_block_ids": [str(table.get("block_id"))] if table.get("block_id") else [],
        "source_table_ids": [table_id] if table_id else [],
        "source_figure_ids": [],
        "rag_chunk_ids": _reader_rag_chunk_ids(unit_payloads),
        "should_index_for_rag": any(bool(unit.get("should_index_for_rag")) for unit in unit_payloads),
        "knowledge_units": unit_payloads,
        "bbox": table.get("bbox"),
        "reading_order": index,
        "source_kind": str(table.get("source_kind") or "structured_table"),
        "confidence": _optional_payload_float(table.get("confidence")),
        "alt_text": "",
        "quality_flags": list(table.get("quality_flags") or []),
        "provenance": dict(table.get("provenance") or {}),
        "table": dict(table),
    }


def _reader_orphan_figure_block(
    *,
    figure: Mapping[str, Any],
    knowledge_units: Sequence[Mapping[str, Any]],
    index: int,
) -> dict[str, Any]:
    page_number = _safe_int(figure.get("page_number"), default=1)
    figure_id = str(figure.get("figure_id") or "")
    unit_payloads = _reader_unit_payloads(knowledge_units)
    rag_text = _reader_rag_text(unit_payloads)
    return {
        "reader_block_id": f"reader:{index:06d}",
        "page_number": page_number,
        "page_span": [page_number, page_number],
        "type": "figure",
        "display_kind": "figure",
        "reader_policy": "source_snapshot",
        "semantic_role": str(figure.get("semantic_role") or "image"),
        "index_policy": str(figure.get("index_policy") or "index_caption_only"),
        "text": str(figure.get("caption") or rag_text or figure.get("alt_text") or ""),
        "rag_text": rag_text,
        "source_unit_ids": [str(unit.get("unit_id")) for unit in unit_payloads if unit.get("unit_id")],
        "source_block_ids": [str(figure.get("block_id"))] if figure.get("block_id") else [],
        "source_table_ids": [],
        "source_figure_ids": [figure_id] if figure_id else [],
        "rag_chunk_ids": _reader_rag_chunk_ids(unit_payloads),
        "should_index_for_rag": any(bool(unit.get("should_index_for_rag")) for unit in unit_payloads),
        "knowledge_units": unit_payloads,
        "bbox": figure.get("bbox"),
        "reading_order": index,
        "source_kind": str(figure.get("source_kind") or "pdf_image"),
        "confidence": _optional_payload_float(figure.get("confidence")),
        "alt_text": str(figure.get("alt_text") or ""),
        "quality_flags": list(figure.get("quality_flags") or []),
        "provenance": dict(figure.get("provenance") or {}),
        "figure": dict(figure),
    }


def _reader_base_block(
    *,
    block: Mapping[str, Any],
    index: int,
    block_type: str,
    display_kind: str,
    text: str,
    source_table_ids: list[str],
    source_figure_ids: list[str],
    knowledge_units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    unit_payloads = _reader_unit_payloads(knowledge_units)
    rag_text = _reader_rag_text(unit_payloads)
    payload = {
        "reader_block_id": f"reader:{index:06d}",
        "page_number": _safe_int(block.get("page_number"), default=1),
        "page_span": list(_page_span_from_payload(block.get("page_span"), fallback_page=_safe_int(block.get("page_number"), default=1))),
        "type": block_type,
        "display_kind": display_kind,
        "reader_policy": str(block.get("reader_policy") or "inline"),
        "semantic_role": str(block.get("semantic_role") or ""),
        "index_policy": str(block.get("index_policy") or ""),
        "text": text,
        "rag_text": rag_text,
        "source_unit_ids": [str(unit.get("unit_id")) for unit in unit_payloads if unit.get("unit_id")],
        "source_block_ids": [str(block.get("block_id"))] if block.get("block_id") else [],
        "source_table_ids": source_table_ids,
        "source_figure_ids": source_figure_ids,
        "rag_chunk_ids": _reader_rag_chunk_ids(unit_payloads),
        "should_index_for_rag": any(bool(unit.get("should_index_for_rag")) for unit in unit_payloads),
        "knowledge_units": unit_payloads,
        "bbox": block.get("bbox"),
        "reading_order": _safe_int(block.get("reading_order"), default=index),
        "source_kind": str(block.get("source_kind") or ""),
        "confidence": _optional_payload_float(block.get("confidence")),
        "alt_text": str(block.get("alt_text") or ""),
        "quality_flags": list(block.get("quality_flags") or []),
        "provenance": dict(block.get("provenance") or {}),
    }
    for region_field in ("lines", "words"):
        raw_regions = block.get(region_field)
        if not isinstance(raw_regions, Sequence) or isinstance(
            raw_regions,
            (str, bytes, bytearray),
        ):
            continue
        regions = [
            dict(region)
            for region in raw_regions
            if isinstance(region, Mapping)
        ]
        if regions:
            payload[region_field] = regions
    return payload


def _reader_units_by_source(
    units: Sequence[Mapping[str, Any]],
    source_key: str,
) -> dict[str, list[Mapping[str, Any]]]:
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for unit in units:
        for source_id in _string_list_payload(unit.get(source_key)):
            by_source.setdefault(source_id, []).append(unit)
    return by_source


def _reader_units_for_block(
    *,
    block: Mapping[str, Any],
    table: Mapping[str, Any] | None,
    units_by_block: Mapping[str, Sequence[Mapping[str, Any]]],
    units_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    units: list[Mapping[str, Any]] = []
    block_id = str(block.get("block_id") or "")
    if block_id:
        units.extend(units_by_block.get(block_id, ()))
    table_id = str((table or {}).get("table_id") or "")
    if table_id:
        units.extend(units_by_table.get(table_id, ()))
    return _reader_unique_units(units)


def _reader_unique_units(units: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        key = unit_id or repr(sorted(unit.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(unit)
    return unique


def _reader_unit_payloads(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for unit in _reader_unique_units(units):
        payloads.append(
            {
                "unit_id": str(unit.get("unit_id") or ""),
                "stable_unit_id": str(unit.get("stable_unit_id") or ""),
                "unit_contract_version": str(unit.get("unit_contract_version") or ""),
                "unit_fingerprint": str(unit.get("unit_fingerprint") or ""),
                "content_fingerprint": str(unit.get("content_fingerprint") or ""),
                "structure_fingerprint": str(unit.get("structure_fingerprint") or ""),
                "source_version_key": str(unit.get("source_version_key") or ""),
                "source_span": dict(unit.get("source_span") or {}),
                "list_level": _safe_int(unit.get("list_level"), default=0),
                "list_marker": str(unit.get("list_marker") or ""),
                "list_parent_unit_id": str(unit.get("list_parent_unit_id") or ""),
                "continuity_required": bool(unit.get("continuity_required")),
                "unit_type": str(unit.get("unit_type") or ""),
                "semantic_role": str(unit.get("semantic_role") or ""),
                "section_id": str(unit.get("section_id") or ""),
                "parent_section_id": str(unit.get("parent_section_id") or ""),
                "section_no": str(unit.get("section_no") or ""),
                "section_title": str(unit.get("section_title") or ""),
                "section_level": _safe_int(unit.get("section_level"), default=0),
                "title_path": _string_list_payload(unit.get("title_path")),
                "continuity": dict(unit.get("continuity") or {}),
                "page_span": list(_page_span_from_payload(unit.get("page_span"), fallback_page=1)),
                "text": str(unit.get("text") or ""),
                "source_item_ids": _string_list_payload(unit.get("source_item_ids")),
                "source_block_ids": _string_list_payload(unit.get("source_block_ids")),
                "source_table_ids": _string_list_payload(unit.get("source_table_ids")),
                "should_index_for_rag": bool(unit.get("should_index_for_rag")),
                "skip_reason": unit.get("skip_reason"),
                "quality_flags": _string_list_payload(unit.get("quality_flags")),
                "chunk_ids": _string_list_payload(unit.get("chunk_ids")),
                "chunk_count": _safe_int(unit.get("chunk_count"), default=len(_string_list_payload(unit.get("chunk_ids")))),
                "embedded_chunk_count": _safe_int(unit.get("embedded_chunk_count"), default=0),
                "embedded": bool(unit.get("embedded")),
                "embedding_model": unit.get("embedding_model"),
                "embedding_state": str(unit.get("embedding_state") or "pending"),
                "embedding_error_category": unit.get("embedding_error_category"),
                "coverage_state": str(unit.get("coverage_state") or ""),
                "processing_status": str(unit.get("processing_status") or ""),
                "processing_reason": str(unit.get("processing_reason") or ""),
                "missing_reason": unit.get("missing_reason"),
                "quality_signal_codes": _string_list_payload(unit.get("quality_signal_codes")),
            }
        )
    return payloads


def _reader_rag_text(units: Sequence[Mapping[str, Any]]) -> str:
    parts = [
        str(unit.get("text") or "").strip()
        for unit in units
        if bool(unit.get("should_index_for_rag")) and str(unit.get("text") or "").strip()
    ]
    return "\n\n".join(dict.fromkeys(parts)).strip()


def _reader_rag_chunk_ids(units: Sequence[Mapping[str, Any]]) -> list[str]:
    chunk_ids: list[str] = []
    for unit in units:
        chunk_ids.extend(_string_list_payload(unit.get("chunk_ids")))
    return sorted(dict.fromkeys(chunk_ids))


def _annotate_reader_block_quality_signals(
    blocks: Sequence[dict[str, Any]],
    *,
    coverage_by_page: Mapping[int, Mapping[str, Any]],
    quality_signals: Sequence[Mapping[str, Any]],
) -> None:
    signal_index = _reader_quality_signal_index(
        coverage_by_page=coverage_by_page,
        quality_signals=quality_signals,
    )
    for block in blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        codes: list[str] = []
        codes.extend(_string_list_payload(block.get("quality_flags")))
        codes.extend(signal_index["by_page"].get(page_number, ()))
        for table_id in _string_list_payload(block.get("source_table_ids")):
            codes.extend(signal_index["by_table"].get(table_id, ()))
        for figure_id in _string_list_payload(block.get("source_figure_ids")):
            codes.extend(signal_index["by_figure"].get(figure_id, ()))
        for block_id in _string_list_payload(block.get("source_block_ids")):
            codes.extend(signal_index["by_block"].get(block_id, ()))
        block["quality_signal_codes"] = list(dict.fromkeys(code for code in codes if code))


def _reader_quality_signal_index(
    *,
    coverage_by_page: Mapping[int, Mapping[str, Any]],
    quality_signals: Sequence[Mapping[str, Any]],
) -> dict[str, dict[Any, list[str]]]:
    by_page: dict[int, list[str]] = {}
    by_table: dict[str, list[str]] = {}
    by_figure: dict[str, list[str]] = {}
    by_block: dict[str, list[str]] = {}
    for page_number, page in coverage_by_page.items():
        page_codes = _string_list_payload(page.get("quality_signal_codes"))
        table_ids = _string_list_payload(page.get("table_ids_without_units"))
        figure_ids = _string_list_payload(page.get("figure_ids_missing_caption"))
        for code in page_codes:
            if code == "rag_table_without_unit" and table_ids:
                for table_id in table_ids:
                    by_table.setdefault(table_id, []).append(code)
                continue
            if code == "rag_figure_caption_missing" and figure_ids:
                for figure_id in figure_ids:
                    by_figure.setdefault(figure_id, []).append(code)
                continue
            by_page.setdefault(int(page_number), []).append(code)

    for signal in quality_signals:
        code = str(signal.get("code") or "").strip()
        if not code:
            continue
        table_id = str(signal.get("table_id") or "").strip()
        figure_id = str(signal.get("figure_id") or "").strip()
        block_id = str(signal.get("block_id") or "").strip()
        page_number = signal.get("page_number")
        detail = signal.get("detail") if isinstance(signal.get("detail"), Mapping) else {}
        detail_table_ids = _string_list_payload(detail.get("table_ids"))
        detail_figure_ids = _string_list_payload(detail.get("figure_ids"))
        detail_block_ids = _string_list_payload(detail.get("block_ids"))
        if table_id:
            by_table.setdefault(table_id, []).append(code)
        if figure_id:
            by_figure.setdefault(figure_id, []).append(code)
        if block_id:
            by_block.setdefault(block_id, []).append(code)
        for detail_table_id in detail_table_ids:
            by_table.setdefault(detail_table_id, []).append(code)
        for detail_figure_id in detail_figure_ids:
            by_figure.setdefault(detail_figure_id, []).append(code)
        for detail_block_id in detail_block_ids:
            by_block.setdefault(detail_block_id, []).append(code)
        has_target = bool(table_id or figure_id or block_id or detail_table_ids or detail_figure_ids or detail_block_ids)
        if not has_target and page_number is not None:
            by_page.setdefault(_safe_int(page_number, default=1), []).append(code)

    return {
        "by_page": {key: list(dict.fromkeys(value)) for key, value in by_page.items()},
        "by_table": {key: list(dict.fromkeys(value)) for key, value in by_table.items()},
        "by_figure": {key: list(dict.fromkeys(value)) for key, value in by_figure.items()},
        "by_block": {key: list(dict.fromkeys(value)) for key, value in by_block.items()},
    }


def _string_list_payload(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _page_span_from_payload(value: Any, *, fallback_page: int) -> tuple[int, int]:
    if isinstance(value, Mapping):
        start = _safe_int(value.get("start", value.get("page_start")), default=fallback_page)
        end = _safe_int(value.get("end", value.get("page_end")), default=start)
        return (min(start, end), max(start, end))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _safe_int(value[0], default=fallback_page)
        end = _safe_int(value[1], default=start)
        return (min(start, end), max(start, end))
    return (fallback_page, fallback_page)


def _document_providers_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    ir = _document_projection(snapshot, projection="ir")
    blocks = [dict(block) for block in ir.get("blocks", []) if isinstance(block, Mapping)]
    tables = [dict(table) for table in ir.get("tables", []) if isinstance(table, Mapping)]
    figures = [dict(figure) for figure in ir.get("figures", []) if isinstance(figure, Mapping)]
    coverage_pages = [
        dict(page)
        for page in ((ir.get("coverage") or {}).get("pages") or [])
        if isinstance(page, Mapping)
    ]

    provider_entries: dict[str, dict[str, Any]] = {}
    block_provider_by_id: dict[str, str] = {}
    for block in blocks:
        provider_id = _provider_id_from_payload(block)
        if not provider_id:
            continue
        block_id = str(block.get("block_id") or "")
        if block_id:
            block_provider_by_id[block_id] = provider_id
        entry = _provider_usage_entry(provider_entries, provider_id)
        provenance = block.get("provenance") if isinstance(block.get("provenance"), Mapping) else {}
        if provenance and not entry["provider_version"]:
            entry["provider_version"] = str(provenance.get("provider_version") or "")
        if provenance and not entry["adapter_version"]:
            entry["adapter_version"] = str(provenance.get("adapter_version") or "")
        page_number = _safe_int(block.get("page_number"), default=1)
        if provenance:
            _provider_usage_add_observability(entry, provenance=provenance, page_number=page_number)
        entry["_page_numbers"].add(page_number)
        entry["block_count"] += 1
        entry["_block_types"][str(block.get("type") or "unknown")] += 1
        entry["_source_kinds"][str(block.get("source_kind") or "unknown")] += 1
        entry["_reader_policies"][str(block.get("reader_policy") or "unknown")] += 1
        entry["_index_policies"][str(block.get("index_policy") or "unknown")] += 1

    for table in tables:
        provider_id = _provider_id_from_payload(table) or block_provider_by_id.get(str(table.get("block_id") or ""))
        if not provider_id:
            continue
        entry = _provider_usage_entry(provider_entries, provider_id)
        entry["table_count"] += 1
        entry["_page_numbers"].add(_safe_int(table.get("page_number"), default=1))

    for figure in figures:
        provider_id = _provider_id_from_payload(figure) or block_provider_by_id.get(str(figure.get("block_id") or ""))
        if not provider_id:
            continue
        entry = _provider_usage_entry(provider_entries, provider_id)
        entry["figure_count"] += 1
        entry["_page_numbers"].add(_safe_int(figure.get("page_number"), default=1))

    coverage_by_page = {
        _safe_int(page.get("page_number"), default=1): page
        for page in coverage_pages
    }
    for page in coverage_pages:
        provider_ids = [str(provider_id) for provider_id in page.get("provider_ids", []) if str(provider_id)]
        for provider_id in provider_ids:
            entry = _provider_usage_entry(provider_entries, provider_id)
            entry["coverage_page_count"] += 1
            missing_reason = page.get("missing_reason")
            if missing_reason:
                entry["coverage_gap_count"] += 1
                entry["_missing_reasons"][str(missing_reason)] += 1
            for code in page.get("quality_signal_codes", []) or []:
                if str(code).startswith("rag_"):
                    entry["_quality_signal_codes"].add(str(code))

    page_provider_ids: dict[int, set[str]] = {}
    page_block_counts: Counter[int] = Counter()
    for block in blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        provider_id = _provider_id_from_payload(block)
        if provider_id:
            page_provider_ids.setdefault(page_number, set()).add(provider_id)
        page_block_counts[page_number] += 1
    table_counts_by_page = Counter(_safe_int(table.get("page_number"), default=1) for table in tables)
    figure_counts_by_page = Counter(_safe_int(figure.get("page_number"), default=1) for figure in figures)

    page_numbers = sorted(
        set(page_provider_ids)
        | set(table_counts_by_page)
        | set(figure_counts_by_page)
        | set(coverage_by_page)
        | {
            _safe_int(page.get("page_number"), default=1)
            for page in ir.get("pages", [])
            if isinstance(page, Mapping)
        }
    )
    pages: list[dict[str, Any]] = []
    for page_number in page_numbers:
        coverage_page = coverage_by_page.get(page_number, {})
        provider_ids = sorted(page_provider_ids.get(page_number, set()) | set(coverage_page.get("provider_ids", []) or []))
        quality_signal_codes = [
            str(code)
            for code in coverage_page.get("quality_signal_codes", []) or []
            if str(code)
        ]
        pages.append(
            {
                "page_number": page_number,
                "provider_ids": provider_ids,
                "block_count": page_block_counts.get(page_number, 0),
                "table_count": table_counts_by_page.get(page_number, 0),
                "figure_count": figure_counts_by_page.get(page_number, 0),
                "coverage_missing_reason": coverage_page.get("missing_reason"),
                "quality_signal_codes": list(dict.fromkeys(quality_signal_codes)),
            }
        )

    providers = [_finalize_provider_usage(entry) for entry in provider_entries.values()]
    providers.sort(key=lambda item: (-_safe_int(item.get("block_count"), default=0), str(item.get("provider_id") or "")))
    primary_provider_id = str(providers[0]["provider_id"]) if providers else None
    comparison_report = _provider_comparison_report(
        providers=providers,
        pages=pages,
        primary_provider_id=primary_provider_id,
    )
    comparison_actions = _provider_comparison_actions(
        snapshot=snapshot,
        comparison_report=comparison_report,
    )
    return {
        "schema_version": PROVIDER_USAGE_SCHEMA_VERSION,
        "projection": "providers",
        "doc_id": ir.get("doc_id"),
        "parse_run_id": ir.get("parse_run_id"),
        "profile": ir.get("profile"),
        "profile_resolution": ir.get("profile_resolution"),
        "local_provider_routing": ir.get("local_provider_routing"),
        "state": ir.get("state"),
        "provider_registry": ir.get("provider_registry") or {},
        "summary": {
            "provider_count": len(providers),
            "primary_provider_id": primary_provider_id,
            "total_blocks": len(blocks),
            "total_tables": len(tables),
            "total_figures": len(figures),
            "total_pages": len(pages),
            "pages_with_multiple_providers": sum(1 for page in pages if len(page["provider_ids"]) > 1),
            "pages_with_coverage_gaps": sum(1 for page in pages if page.get("coverage_missing_reason")),
        },
        "providers": providers,
        "pages": pages,
        "comparison_report": comparison_report,
        "comparison_actions": comparison_actions,
        "rag_coverage_quality": ir.get("rag_coverage_quality"),
        "quality_gate": _provider_quality_gate_payload(
            quality_gate=ir.get("quality_gate"),
            comparison_report=comparison_report,
            comparison_actions=comparison_actions,
        ),
    }


def _quality_gate_projection(
    *,
    snapshot: Mapping[str, Any],
    coverage_summary: Mapping[str, Any],
    rag_coverage_quality: Mapping[str, Any],
    parse_units: Sequence[Mapping[str, Any]] = (),
    pages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    doc_id = str(snapshot.get("doc_id") or getattr(snapshot.get("job"), "doc_id", "") or "")
    config = _quality_gate_config(snapshot.get("quality_gate"))
    thresholds = config["thresholds"]
    reading_order_confidence = _document_reading_order_confidence(pages)
    actions = config["actions"]
    observed = {
        "text_page_coverage_ratio": _float_value(coverage_summary.get("text_page_coverage_ratio"), default=1.0),
        "table_unit_coverage_ratio": _float_value(coverage_summary.get("table_unit_coverage_ratio"), default=1.0),
        "unit_chunk_coverage_ratio": _float_value(coverage_summary.get("unit_chunk_coverage_ratio"), default=1.0),
        "reading_order_confidence": reading_order_confidence,
        "pages_with_coverage_gaps": _safe_int(coverage_summary.get("pages_with_coverage_gaps"), default=0),
        "pages_missing_rag_units": _safe_int(coverage_summary.get("pages_missing_rag_units"), default=0),
        "pages_missing_chunks": _safe_int(coverage_summary.get("pages_missing_chunks"), default=0),
        "pages_chunks_not_embedded": _safe_int(coverage_summary.get("pages_chunks_not_embedded"), default=0),
        "pages_table_without_units": _safe_int(coverage_summary.get("pages_table_without_units"), default=0),
        "pages_figure_caption_missing": _safe_int(coverage_summary.get("pages_figure_caption_missing"), default=0),
    }
    if not config["enabled"]:
        return {
            "schema_version": QUALITY_GATE_SCHEMA_VERSION,
            "enabled": False,
            "gate": "disabled",
            "passed": True,
            "blocking": False,
            "enforcement": config["enforcement"],
            "recommended_action": None,
            "flags": [],
            "warnings": [],
            "thresholds": thresholds,
            "observed": observed,
            "actions": actions,
            "action_suggestions": [],
        }

    threshold_flags: list[str] = []
    threshold_warnings: list[str] = []
    if observed["text_page_coverage_ratio"] < thresholds["min_text_page_coverage"]:
        threshold_flags.append("text_page_coverage_below_threshold")
        threshold_warnings.append("Text-page RAG coverage is below the configured threshold")
    if observed["table_unit_coverage_ratio"] < thresholds["min_table_unit_coverage"]:
        threshold_flags.append("table_unit_coverage_below_threshold")
        threshold_warnings.append("Table-unit coverage is below the configured threshold")
    if observed["unit_chunk_coverage_ratio"] < thresholds["min_unit_chunk_coverage"]:
        threshold_flags.append("unit_chunk_coverage_below_threshold")
        threshold_warnings.append("Knowledge-unit chunk coverage is below the configured threshold")
    if (
        observed["reading_order_confidence"] is not None
        and observed["reading_order_confidence"] < thresholds["min_reading_order_confidence"]
    ):
        threshold_flags.append("reading_order_confidence_below_threshold")
        threshold_warnings.append("Reading-order confidence is below the configured threshold")

    rag_flags = [str(flag) for flag in rag_coverage_quality.get("flags", []) if str(flag)]
    rag_warnings = [str(warning) for warning in rag_coverage_quality.get("warnings", []) if str(warning)]
    flags = list(dict.fromkeys(threshold_flags + rag_flags))
    warnings = list(dict.fromkeys(threshold_warnings + rag_warnings))
    rag_gate = str(rag_coverage_quality.get("gate") or "accept")
    rag_action = rag_coverage_quality.get("recommended_action")

    gate = "accept"
    recommended_action: str | None = None
    if "rag_empty_text_page" in flags or rag_gate == "manual_review":
        gate = "manual_review" if actions["allow_manual_review"] else "accept_with_warning"
        recommended_action = str(rag_action or "review_parse_ir")
    elif "rag_table_without_unit" in flags:
        gate = "local_rerun" if actions["allow_local_rerun"] else "accept_with_warning"
        recommended_action = str(rag_action or "local_provider_rerun")
    elif "reading_order_confidence_below_threshold" in flags:
        gate = "local_rerun" if actions["allow_local_rerun"] else "accept_with_warning"
        recommended_action = str(rag_action or "local_provider_rerun")
    elif any(flag in flags for flag in ("text_page_coverage_below_threshold", "table_unit_coverage_below_threshold")):
        gate = "local_rerun" if actions["allow_local_rerun"] else "accept_with_warning"
        recommended_action = str(rag_action or "local_provider_rerun")
    elif "rag_figure_caption_missing" in flags:
        gate = "accept_with_warning"
        recommended_action = str(rag_action or "review_parse_ir")
    elif "unit_chunk_coverage_below_threshold" in flags or "rag_units_without_chunks" in flags:
        gate = "accept_with_warning"
        recommended_action = str(rag_action or "rechunk_document")
    elif "rag_chunks_not_embedded" in flags:
        gate = "accept_with_warning"
        recommended_action = str(rag_action or "reembed_document")
    elif flags or rag_gate == "accept_with_warning":
        gate = "accept_with_warning"
        recommended_action = str(rag_action) if rag_action else None

    suggestions = _quality_action_suggestions(
        doc_id=doc_id,
        gate=gate,
        recommended_action=recommended_action,
        flags=flags,
        snapshot=snapshot,
        parse_units=parse_units,
    )
    return {
        "schema_version": QUALITY_GATE_SCHEMA_VERSION,
        "enabled": True,
        "gate": gate,
        "passed": gate in {"accept", "accept_with_warning"},
        "blocking": False,
        "enforcement": config["enforcement"],
        "recommended_action": recommended_action,
        "flags": flags,
        "warnings": warnings,
        "thresholds": thresholds,
        "observed": observed,
        "actions": actions,
        "action_suggestions": suggestions,
    }


def _quality_gate_config(value: Any) -> dict[str, Any]:
    default = {
        "enabled": True,
        "thresholds": {
            "min_text_page_coverage": 0.98,
            "min_table_unit_coverage": 0.95,
            "min_unit_chunk_coverage": 0.98,
            "min_reading_order_confidence": DEFAULT_READING_ORDER_CONFIDENCE_THRESHOLD,
        },
        "actions": {
            "allow_local_rerun": True,
            "allow_manual_review": True,
        },
        "enforcement": "report_only",
    }
    if not isinstance(value, Mapping):
        return default
    thresholds = value.get("thresholds") if isinstance(value.get("thresholds"), Mapping) else {}
    actions = value.get("actions") if isinstance(value.get("actions"), Mapping) else {}
    return {
        "enabled": bool(value.get("enabled", default["enabled"])),
        "thresholds": {
            "min_text_page_coverage": _float_value(
                thresholds.get("min_text_page_coverage"),
                default=default["thresholds"]["min_text_page_coverage"],
            ),
            "min_table_unit_coverage": _float_value(
                thresholds.get("min_table_unit_coverage"),
                default=default["thresholds"]["min_table_unit_coverage"],
            ),
            "min_unit_chunk_coverage": _float_value(
                thresholds.get("min_unit_chunk_coverage"),
                default=default["thresholds"]["min_unit_chunk_coverage"],
            ),
            "min_reading_order_confidence": _float_value(
                thresholds.get("min_reading_order_confidence"),
                default=default["thresholds"]["min_reading_order_confidence"],
            ),
        },
        "actions": {
            "allow_local_rerun": bool(actions.get("allow_local_rerun", default["actions"]["allow_local_rerun"])),
            "allow_manual_review": bool(actions.get("allow_manual_review", default["actions"]["allow_manual_review"])),
        },
        "enforcement": str(value.get("enforcement") or default["enforcement"]),
    }


def _float_value(value: Any, *, default: float) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _document_reading_order_confidence(pages: Sequence[Mapping[str, Any]]) -> float | None:
    values: list[float] = []
    for page in pages:
        confidence = _optional_payload_float(page.get("reading_order_confidence"))
        if confidence is None:
            continue
        values.append(max(0.0, min(1.0, confidence)))
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _quality_action_suggestions(
    *,
    doc_id: str,
    gate: str,
    recommended_action: str | None,
    flags: Sequence[str],
    snapshot: Mapping[str, Any],
    parse_units: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    if not doc_id or gate in {"accept", "disabled"}:
        return []
    action = str(recommended_action or "").strip()
    reason_codes = list(dict.fromkeys(str(flag) for flag in flags if str(flag)))
    suggestions: list[dict[str, Any]] = []

    if action == "rechunk_document":
        suggestions.append(
            _action_suggestion(
                action_id="rechunk_document",
                label="Rebuild chunks",
                method="POST",
                endpoint=f"/v1/parse/documents/{doc_id}/rechunk",
                scope="document",
                reason_codes=reason_codes,
            )
        )
    elif action == "reembed_document":
        suggestions.append(
            _action_suggestion(
                action_id="reembed_document",
                label="Rebuild embeddings",
                method="POST",
                endpoint=f"/v1/parse/documents/{doc_id}/re-embed",
                scope="document",
                reason_codes=reason_codes,
            )
        )
    elif action == "review_parse_ir":
        suggestions.append(
            _action_suggestion(
                action_id="review_parse_ir",
                label="Review Parse IR",
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}",
                scope="document",
                reason_codes=reason_codes,
                params={"projection": "ir"},
            )
        )
    elif action == "local_provider_rerun":
        partition_parts = [part for part in parse_units if isinstance(part, Mapping)]
        route_plan_params = _quality_local_provider_route_plan_params(snapshot=snapshot, reason_codes=reason_codes)
        routing_context = _quality_local_provider_routing_context(snapshot=snapshot)
        rerun_candidates = _quality_local_provider_rerun_candidates(partition_parts)
        if rerun_candidates["eligible_part_ids"]:
            suggestions.append(
                _action_suggestion(
                    action_id="rerun_warning_parts",
                    label="Rerun warning parts",
                    method="POST",
                    endpoint=f"/v1/parse/documents/{doc_id}/parts/rerun",
                    scope="parts",
                    reason_codes=reason_codes,
                    payload={
                        "part_ids": rerun_candidates["eligible_part_ids"],
                        "failed_only": False,
                        "state": ["warning"],
                        "provider_route_plan": route_plan_params,
                    },
                    context={
                        "local_provider_routing": routing_context,
                        "rerun_candidates": rerun_candidates,
                    },
                )
            )
        suggestions.append(
            _action_suggestion(
                action_id="inspect_provider_route_plan",
                label="Inspect local provider route plan",
                method="GET",
                endpoint="/v1/parse/providers/route-plan",
                scope="provider_route",
                reason_codes=reason_codes,
                params=route_plan_params,
                context={
                    "local_provider_routing": routing_context,
                    "rerun_candidates": rerun_candidates,
                },
            )
        )
        suggestions.append(
            _action_suggestion(
                action_id="reparse_document",
                label="Reparse document",
                method="POST",
                endpoint=f"/v1/parse/documents/{doc_id}/reparse",
                scope="document",
                reason_codes=reason_codes,
                payload={"provider_route_plan": route_plan_params},
                context={
                    "local_provider_routing": routing_context,
                    "rerun_candidates": rerun_candidates,
                },
            )
        )
    elif action:
        suggestions.append(
            _action_suggestion(
                action_id=action,
                label=action.replace("_", " ").title(),
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}/quality",
                scope="document",
                reason_codes=reason_codes,
            )
        )

    if reason_codes and not any(item["action_id"] == "review_quality" for item in suggestions):
        suggestions.append(
            _action_suggestion(
                action_id="review_quality",
                label="Review quality report",
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}/quality",
                scope="document",
                reason_codes=reason_codes,
            )
        )
    return suggestions


def _quality_local_provider_route_plan_params(
    *,
    snapshot: Mapping[str, Any],
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    job = snapshot.get("job")
    options = getattr(job, "options", {}) or {}
    if not isinstance(options, Mapping):
        options = {}
    file_path = str(getattr(job, "file_path", "") or snapshot.get("file_path") or "").strip()
    file_name = str(getattr(job, "file_name", "") or snapshot.get("file_name") or "").strip()
    if not file_name and file_path:
        file_name = Path(file_path).name
    params: dict[str, Any] = {
        "include_disabled": True,
        "required_capabilities": _quality_required_provider_capabilities(reason_codes),
    }
    media_type = str(getattr(job, "media_type", "") or snapshot.get("media_type") or "").strip()
    if media_type:
        params["media_type"] = media_type
    if file_name:
        params["file_name"] = file_name
    elif file_path:
        suffix = Path(file_path).suffix
        if suffix:
            params["extension"] = suffix
    profile = str(options.get("profile") or snapshot.get("profile") or "").strip()
    if profile:
        params["profile"] = profile
    return params


def _quality_local_provider_routing_context(*, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    registry = snapshot.get("provider_registry")
    routing_config: dict[str, Any] = {}
    if isinstance(registry, Mapping):
        routing = registry.get("routing")
        if isinstance(routing, Mapping):
            routing_config = _to_payload(dict(routing))

    routing_enabled = bool(routing_config.get("enabled", False))
    context: dict[str, Any] = {
        "routing_enabled": routing_enabled,
        "execution_mode": "configured_route_plan" if routing_enabled else "inspect_only",
        "enable_config_path": "providers.local_parser_routing.enabled",
    }
    if routing_config:
        context["routing_config"] = routing_config

    current_decision = _local_provider_routing_decision(snapshot)
    if current_decision:
        context["current_decision"] = current_decision

    if not routing_enabled:
        context["requires_configuration"] = ["providers.local_parser_routing.enabled"]
    return context


def _quality_local_provider_rerun_candidates(
    parse_units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible_part_ids: list[str] = []
    eligible_parts: list[dict[str, Any]] = []
    skipped_parts: list[dict[str, Any]] = []
    for unit in parse_units:
        part_id = str(unit.get("part_id") or unit.get("parse_unit_id") or "").strip()
        if not part_id:
            continue
        candidate_payload = _quality_rerun_candidate_part_payload(unit)
        if not bool(unit.get("rerun_supported")):
            skipped_parts.append({**candidate_payload, "reason": "rerun_not_supported"})
            continue
        quality_signal_count = _safe_int(unit.get("quality_signal_count"), default=0)
        state = _state_value(unit.get("state"))
        if state not in {"warning", "failed"} and quality_signal_count <= 0:
            skipped_parts.append({**candidate_payload, "reason": "not_warning"})
            continue
        rerun_comparison = unit.get("rerun_comparison")
        if isinstance(rerun_comparison, Mapping):
            status = str(rerun_comparison.get("status") or "already_rerun").strip() or "already_rerun"
            skipped_parts.append(
                {
                    **candidate_payload,
                    "reason": f"already_rerun:{status}",
                    "rerun_comparison_status": status,
                    "previous_job_id": rerun_comparison.get("previous_job_id"),
                    "current_job_id": rerun_comparison.get("current_job_id"),
                }
            )
            continue
        eligible_part_ids.append(part_id)
        eligible_parts.append(candidate_payload)
    return {
        "eligible_part_ids": eligible_part_ids,
        "eligible_parts": eligible_parts,
        "eligible_count": len(eligible_part_ids),
        "coverage_gap_part_ids": [
            str(item.get("part_id") or "")
            for item in eligible_parts
            if _safe_int(item.get("coverage_gap_count"), default=0) > 0
        ],
        "coverage_gap_unit_part_ids": [
            str(item.get("part_id") or "")
            for item in eligible_parts
            if _safe_int(item.get("coverage_gap_unit_count"), default=0) > 0
        ],
        "unembedded_part_ids": [
            str(item.get("part_id") or "")
            for item in eligible_parts
            if _safe_int(item.get("unembedded_unit_count"), default=0) > 0
        ],
        "gap_unit_ids": sorted(
            {
                str(unit_id)
                for item in eligible_parts
                for unit_id in item.get("gap_unit_ids", [])
                if str(unit_id)
            }
        ),
        "skipped_parts": skipped_parts,
    }


def _quality_rerun_candidate_part_payload(unit: Mapping[str, Any]) -> dict[str, Any]:
    coverage_summary = unit.get("coverage_summary")
    coverage_summary_payload = coverage_summary if isinstance(coverage_summary, Mapping) else {}
    return {
        "part_id": str(unit.get("part_id") or unit.get("parse_unit_id") or "").strip(),
        "page_range": {
            "start": _safe_int(unit.get("page_start"), default=0),
            "end": _safe_int(unit.get("page_end"), default=0),
        },
        "quality_signal_codes": [
            str(code)
            for code in unit.get("quality_signal_codes", [])
            if str(code)
        ],
        "coverage_gap_count": _safe_int(coverage_summary_payload.get("pages_with_coverage_gaps"), default=0),
        "coverage_gap_unit_count": len(_string_list_payload(coverage_summary_payload.get("gap_unit_ids"))),
        "gap_unit_ids": _string_list_payload(coverage_summary_payload.get("gap_unit_ids")),
        "unembedded_unit_count": _safe_int(coverage_summary_payload.get("unembedded_unit_count"), default=0),
        "selected_provider_id": _part_observation_selected_provider_id(unit),
    }


def _quality_attention_part_contract_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "part_id": str(item.get("part_id") or "").strip(),
        "page_range": _to_payload(dict(item.get("page_range") or {})) if isinstance(item.get("page_range"), Mapping) else {},
        "quality_signal_codes": [
            str(code)
            for code in item.get("quality_signal_codes", [])
            if str(code)
        ],
        "coverage_gap_count": _safe_int(item.get("coverage_gap_count"), default=0),
        "coverage_gap_unit_count": _safe_int(item.get("coverage_gap_unit_count"), default=0),
        "gap_unit_ids": [
            str(unit_id)
            for unit_id in item.get("gap_unit_ids", [])
            if str(unit_id)
        ],
        "unembedded_unit_count": _safe_int(item.get("unembedded_unit_count"), default=0),
        "selected_provider_id": str(item.get("selected_provider_id") or "") or None,
        "recommended_focus": str(item.get("recommended_focus") or "") or None,
        "rerun_status": str(item.get("rerun_status") or "") or None,
        "gap_unit_count_delta": (
            _safe_int(item.get("gap_unit_count_delta"), default=0)
            if "gap_unit_count_delta" in item
            else None
        ),
    }


def _quality_required_provider_capabilities(reason_codes: Sequence[str]) -> list[str]:
    capabilities: list[str] = []
    reasons = {str(code) for code in reason_codes if str(code)}
    if reasons & {"rag_table_without_unit", "table_unit_coverage_below_threshold"}:
        capabilities.append("tables")
    if reasons & {"rag_figure_caption_missing"}:
        capabilities.extend(["layout", "figures"])
    if reasons & {"reading_order_low_confidence", "reading_order_confidence_below_threshold"}:
        capabilities.append("layout")
    if reasons & {"rag_empty_text_page", "text_page_coverage_below_threshold"}:
        capabilities.extend(["native-text", "local-ocr-fallback"])
    return list(dict.fromkeys(capabilities))


def _action_suggestion(
    *,
    action_id: str,
    label: str,
    method: str,
    endpoint: str,
    scope: str,
    reason_codes: Sequence[str],
    payload: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    suggestion: dict[str, Any] = {
        "action_id": action_id,
        "label": label,
        "method": method,
        "endpoint": endpoint,
        "scope": scope,
        "reason_codes": list(reason_codes),
        "auto_execute": False,
    }
    if payload:
        suggestion["payload"] = dict(payload)
    if params:
        suggestion["params"] = dict(params)
    if context:
        suggestion["context"] = dict(context)
    return suggestion


def _local_provider_routing_decision(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    job = snapshot.get("job")
    options = getattr(job, "options", {}) if job is not None else {}
    if not isinstance(options, Mapping):
        return None
    routing = options.get("local_provider_routing")
    if not isinstance(routing, Mapping):
        return None
    return _normalize_local_provider_routing_payload(routing)


def _normalize_local_provider_requested_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    requested = value if isinstance(value, Mapping) else {}
    required_capabilities = [
        str(item).strip()
        for item in (requested.get("required_capabilities") or [])
        if str(item).strip()
    ]
    return {
        "media_type": str(requested.get("media_type") or "").strip() or None,
        "extension": str(requested.get("extension") or "").strip() or None,
        "file_name": str(requested.get("file_name") or "").strip() or None,
        "profile": str(requested.get("profile") or "").strip() or None,
        "required_capabilities": required_capabilities,
        "include_disabled": bool(requested.get("include_disabled", False)),
    }


def _normalize_local_provider_routing_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = _to_payload(dict(value))
    selected_provider_id = str(payload.get("selected_provider_id") or "").strip() or None
    primary_provider_id = str(payload.get("primary_provider_id") or "").strip() or selected_provider_id
    eligible_provider_ids = [
        str(item).strip()
        for item in (payload.get("eligible_provider_ids") or [])
        if str(item).strip()
    ]
    if not eligible_provider_ids and selected_provider_id:
        eligible_provider_ids = [selected_provider_id]
    fallback_provider_ids = [
        str(item).strip()
        for item in (payload.get("fallback_provider_ids") or [])
        if str(item).strip()
    ]
    excluded_provider_ids = [
        str(item).strip()
        for item in (payload.get("excluded_provider_ids") or [])
        if str(item).strip()
    ]
    selected_route_role = str(payload.get("selected_route_role") or "").strip() or None
    if selected_route_role is None and selected_provider_id and selected_provider_id == primary_provider_id:
        selected_route_role = "primary"
    route_status = str(payload.get("route_status") or "").strip()
    if not route_status:
        route_status = "selected" if selected_provider_id else "no_eligible_provider"
    return {
        "schema_version": str(payload.get("schema_version") or "2026-06-local-provider-routing-decision"),
        "enabled": bool(payload.get("enabled", True)),
        "routing_policy": str(payload.get("routing_policy") or "").strip() or "priority_desc_then_id",
        "route_status": route_status,
        "selected_provider_id": selected_provider_id,
        "selected_route_role": selected_route_role,
        "primary_provider_id": primary_provider_id,
        "fallback_provider_ids": fallback_provider_ids,
        "eligible_provider_ids": eligible_provider_ids,
        "excluded_provider_ids": excluded_provider_ids,
        "fallback_to_default": bool(payload.get("fallback_to_default", True)),
        "requested": _normalize_local_provider_requested_payload(payload.get("requested")),
    }


def _provider_usage_entry(entries: dict[str, dict[str, Any]], provider_id: str) -> dict[str, Any]:
    return entries.setdefault(
        provider_id,
        {
            "provider_id": provider_id,
            "provider_version": "",
            "adapter_version": "",
            "block_count": 0,
            "table_count": 0,
            "figure_count": 0,
            "coverage_page_count": 0,
            "coverage_gap_count": 0,
            "_page_numbers": set(),
            "_block_types": Counter(),
            "_source_kinds": Counter(),
            "_reader_policies": Counter(),
            "_index_policies": Counter(),
            "_missing_reasons": Counter(),
            "_quality_signal_codes": set(),
            "_provider_elapsed_s_by_page": {},
            "_provider_memory_mb_by_page": {},
            "_reading_order_confidence_by_page": {},
        },
    )


def _provider_id_from_payload(payload: Mapping[str, Any]) -> str:
    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping):
        provider_id = str(provenance.get("provider_id") or "").strip()
        if provider_id:
            return provider_id
    return str(payload.get("provider_id") or payload.get("source_parser") or "").strip()


def _provider_usage_add_observability(
    entry: dict[str, Any],
    *,
    provenance: Mapping[str, Any],
    page_number: int,
) -> None:
    _set_page_max(
        entry["_provider_elapsed_s_by_page"],
        page_number,
        _optional_payload_float(provenance.get("provider_elapsed_s")),
    )
    _set_page_max(
        entry["_provider_memory_mb_by_page"],
        page_number,
        _optional_payload_float(provenance.get("provider_memory_mb")),
    )
    reading_order_confidence = _optional_payload_float(provenance.get("reading_order_confidence"))
    if reading_order_confidence is not None:
        reading_order_confidence = max(0.0, min(1.0, reading_order_confidence))
    _set_page_max(
        entry["_reading_order_confidence_by_page"],
        page_number,
        reading_order_confidence,
    )


def _set_page_max(values_by_page: dict[int, float], page_number: int, value: float | None) -> None:
    if value is None:
        return
    normalized = max(0.0, float(value))
    current = values_by_page.get(page_number)
    if current is None or normalized > current:
        values_by_page[page_number] = normalized


def _finalize_provider_usage(entry: dict[str, Any]) -> dict[str, Any]:
    page_numbers = sorted(int(page_number) for page_number in entry.pop("_page_numbers", set()))
    block_types = _counter_payload(entry.pop("_block_types", Counter()))
    source_kinds = _counter_payload(entry.pop("_source_kinds", Counter()))
    reader_policies = _counter_payload(entry.pop("_reader_policies", Counter()))
    index_policies = _counter_payload(entry.pop("_index_policies", Counter()))
    missing_reasons = _counter_payload(entry.pop("_missing_reasons", Counter()))
    quality_signal_codes = sorted(str(code) for code in entry.pop("_quality_signal_codes", set()) if str(code))
    elapsed_by_page = entry.pop("_provider_elapsed_s_by_page", {})
    memory_by_page = entry.pop("_provider_memory_mb_by_page", {})
    reading_order_by_page = entry.pop("_reading_order_confidence_by_page", {})
    elapsed_values = [float(value) for value in elapsed_by_page.values() if value is not None]
    memory_values = [float(value) for value in memory_by_page.values() if value is not None]
    reading_order_values = [float(value) for value in reading_order_by_page.values() if value is not None]
    finalized = dict(entry)
    finalized["page_numbers"] = page_numbers
    finalized["page_count"] = len(page_numbers)
    finalized["block_types"] = block_types
    finalized["source_kinds"] = source_kinds
    finalized["reader_policies"] = reader_policies
    finalized["index_policies"] = index_policies
    finalized["coverage_missing_reasons"] = missing_reasons
    finalized["quality_signal_codes"] = quality_signal_codes
    finalized["provider_elapsed_s"] = round(sum(elapsed_values), 6) if elapsed_values else None
    finalized["provider_elapsed_page_count"] = len(elapsed_values)
    finalized["provider_memory_mb"] = round(max(memory_values), 4) if memory_values else None
    finalized["provider_memory_page_count"] = len(memory_values)
    finalized["reading_order_confidence"] = (
        round(sum(reading_order_values) / len(reading_order_values), 4)
        if reading_order_values
        else None
    )
    finalized["reading_order_confidence_page_count"] = len(reading_order_values)
    return finalized


def _provider_comparison_report(
    *,
    providers: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    primary_provider_id: str | None,
) -> dict[str, Any]:
    rankings = [
        _provider_comparison_entry(provider)
        for provider in providers
    ]
    rankings.sort(
        key=lambda item: (
            -_float_value(item.get("score"), default=0.0),
            _safe_int((item.get("metrics") or {}).get("coverage_gap_count"), default=0),
            -_safe_int((item.get("metrics") or {}).get("table_count"), default=0),
            -_safe_int((item.get("metrics") or {}).get("block_count"), default=0),
            str(item.get("provider_id") or ""),
        )
    )
    for index, item in enumerate(rankings, start=1):
        item["rank"] = index
    best_provider_id = str(rankings[0]["provider_id"]) if rankings else None
    primary_ranking = next(
        (item for item in rankings if str(item.get("provider_id") or "") == str(primary_provider_id or "")),
        None,
    )
    best_ranking = rankings[0] if rankings else None
    best_provider_differs_from_primary = bool(
        best_provider_id and primary_provider_id and str(best_provider_id) != str(primary_provider_id)
    )
    quality_warning_provider_ids = [
        str(item.get("provider_id") or "")
        for item in rankings
        if _provider_comparison_has_quality_warning(item) and str(item.get("provider_id") or "")
    ]
    reading_order_warning_provider_ids = [
        str(item.get("provider_id") or "")
        for item in rankings
        if _provider_comparison_has_reading_order_warning(item) and str(item.get("provider_id") or "")
    ]
    coverage_gap_provider_ids = [
        str(item.get("provider_id") or "")
        for item in rankings
        if _safe_int((item.get("metrics") or {}).get("coverage_gap_count"), default=0) > 0
        and str(item.get("provider_id") or "")
    ]
    attention_provider_ids = list(
        dict.fromkeys(
            [
                *([str(primary_provider_id)] if best_provider_differs_from_primary and primary_provider_id else []),
                *([str(best_provider_id)] if best_provider_differs_from_primary and best_provider_id else []),
                *quality_warning_provider_ids,
                *reading_order_warning_provider_ids,
                *coverage_gap_provider_ids,
            ]
        )
    )
    if best_provider_differs_from_primary:
        recommended_action = "inspect_provider_route_plan"
    elif attention_provider_ids:
        recommended_action = "inspect_provider_comparison"
    else:
        recommended_action = "keep_current_primary"
    return {
        "schema_version": PROVIDER_COMPARISON_SCHEMA_VERSION,
        "primary_provider_id": primary_provider_id,
        "best_provider_id": best_provider_id,
        "summary": {
            "provider_count": len(rankings),
            "comparable_provider_count": len([item for item in rankings if item["metrics"]["page_count"] > 0]),
            "pages_with_multiple_providers": sum(1 for page in pages if len(page.get("provider_ids") or []) > 1),
            "primary_provider_rank": _safe_int(primary_ranking.get("rank"), default=None) if primary_ranking else None,
            "primary_provider_score": _optional_payload_float(primary_ranking.get("score")) if primary_ranking else None,
            "primary_provider_recommendation": (
                str(primary_ranking.get("recommendation") or "") if primary_ranking else None
            ),
            "best_provider_score": _optional_payload_float(best_ranking.get("score")) if best_ranking else None,
            "best_provider_recommendation": (
                str(best_ranking.get("recommendation") or "") if best_ranking else None
            ),
            "best_provider_differs_from_primary": best_provider_differs_from_primary,
            "providers_with_quality_warnings": len(quality_warning_provider_ids),
            "providers_with_reading_order_warning": len(reading_order_warning_provider_ids),
            "providers_with_coverage_gaps": len(coverage_gap_provider_ids),
            "quality_warning_provider_ids": quality_warning_provider_ids,
            "reading_order_warning_provider_ids": reading_order_warning_provider_ids,
            "coverage_gap_provider_ids": coverage_gap_provider_ids,
            "attention_provider_ids": attention_provider_ids,
            "needs_attention": bool(attention_provider_ids),
            "recommended_action": recommended_action,
            "pending_axes": _provider_comparison_pending_axes(rankings),
        },
        "rankings": rankings,
    }


def _provider_comparison_entry(
    provider: Mapping[str, Any],
) -> dict[str, Any]:
    provider_id = str(provider.get("provider_id") or "")
    page_count = _safe_int(provider.get("page_count"), default=0)
    coverage_page_count = _safe_int(provider.get("coverage_page_count"), default=0)
    coverage_gap_count = _safe_int(provider.get("coverage_gap_count"), default=0)
    block_count = _safe_int(provider.get("block_count"), default=0)
    table_count = _safe_int(provider.get("table_count"), default=0)
    figure_count = _safe_int(provider.get("figure_count"), default=0)
    provider_elapsed_s = _optional_payload_float(provider.get("provider_elapsed_s"))
    provider_memory_mb = _optional_payload_float(provider.get("provider_memory_mb"))
    reading_order_confidence = _optional_payload_float(provider.get("reading_order_confidence"))
    quality_signal_codes = [str(code) for code in provider.get("quality_signal_codes", []) if str(code)]
    signal_set = set(quality_signal_codes)
    coverage_gap_ratio = _ratio_float(coverage_page_count - coverage_gap_count, coverage_page_count, default=1.0)
    table_gap_count = 1 if "rag_table_without_unit" in signal_set else 0
    figure_caption_gap_count = 1 if "rag_figure_caption_missing" in signal_set else 0
    chunk_gap_count = 1 if "rag_units_without_chunks" in signal_set else 0
    embedding_gap_count = 1 if "rag_chunks_not_embedded" in signal_set else 0
    empty_text_gap_count = 1 if "rag_empty_text_page" in signal_set else 0
    score = _provider_comparison_score(
        coverage_gap_count=coverage_gap_count,
        coverage_page_count=coverage_page_count,
        table_gap_count=table_gap_count,
        figure_caption_gap_count=figure_caption_gap_count,
        chunk_gap_count=chunk_gap_count,
        embedding_gap_count=embedding_gap_count,
        empty_text_gap_count=empty_text_gap_count,
        reading_order_confidence=reading_order_confidence,
    )
    axes = {
        "text_coverage": {
            "status": "gap" if empty_text_gap_count else ("warning" if coverage_gap_count else "ok"),
            "coverage_page_count": coverage_page_count,
            "coverage_gap_count": coverage_gap_count,
            "coverage_ratio": coverage_gap_ratio,
        },
        "table_structure": {
            "status": "gap" if table_gap_count else "ok",
            "table_count": table_count,
            "rag_table_without_unit": bool(table_gap_count),
        },
        "figure_caption": {
            "status": "gap" if figure_caption_gap_count else "ok",
            "figure_count": figure_count,
            "rag_figure_caption_missing": bool(figure_caption_gap_count),
        },
        "rag_chunking": {
            "status": "gap" if chunk_gap_count or embedding_gap_count else "ok",
            "rag_units_without_chunks": bool(chunk_gap_count),
            "rag_chunks_not_embedded": bool(embedding_gap_count),
        },
        "reading_order": {
            "status": _reading_order_axis_status(reading_order_confidence),
            "reading_order_confidence": reading_order_confidence,
            "observed_page_count": _safe_int(provider.get("reading_order_confidence_page_count"), default=0),
            "threshold": 0.75,
            **(
                {"reason": "reading_order_confidence_not_emitted"}
                if reading_order_confidence is None
                else {}
            ),
        },
        "performance": {
            "status": "observed" if provider_elapsed_s is not None else "not_observed",
            "provider_elapsed_s": provider_elapsed_s,
            "observed_page_count": _safe_int(provider.get("provider_elapsed_page_count"), default=0),
            **(
                {"reason": "provider_elapsed_s_not_emitted"}
                if provider_elapsed_s is None
                else {}
            ),
        },
        "memory": {
            "status": "observed" if provider_memory_mb is not None else "not_observed",
            "provider_memory_mb": provider_memory_mb,
            "observed_page_count": _safe_int(provider.get("provider_memory_page_count"), default=0),
            **(
                {"reason": "provider_memory_mb_not_emitted"}
                if provider_memory_mb is None
                else {}
            ),
        },
    }
    return {
        "provider_id": provider_id,
        "rank": 0,
        "score": score,
        "recommendation": _provider_comparison_recommendation(signal_set, coverage_gap_count=coverage_gap_count),
        "quality_signal_codes": quality_signal_codes,
        "metrics": {
            "page_count": page_count,
            "coverage_page_count": coverage_page_count,
            "coverage_gap_count": coverage_gap_count,
            "coverage_ratio": coverage_gap_ratio,
            "block_count": block_count,
            "table_count": table_count,
            "figure_count": figure_count,
            "provider_elapsed_s": provider_elapsed_s,
            "provider_memory_mb": provider_memory_mb,
            "reading_order_confidence": reading_order_confidence,
            "quality_signal_count": len(quality_signal_codes),
            "table_gap_count": table_gap_count,
            "figure_caption_gap_count": figure_caption_gap_count,
            "chunk_gap_count": chunk_gap_count,
            "embedding_gap_count": embedding_gap_count,
            "empty_text_gap_count": empty_text_gap_count,
        },
        "axes": axes,
    }


def _provider_comparison_score(
    *,
    coverage_gap_count: int,
    coverage_page_count: int,
    table_gap_count: int,
    figure_caption_gap_count: int,
    chunk_gap_count: int,
    embedding_gap_count: int,
    empty_text_gap_count: int,
    reading_order_confidence: float | None,
) -> float:
    denominator = max(coverage_page_count, 1)
    penalty = min(0.5, coverage_gap_count / denominator * 0.5)
    penalty += min(0.2, empty_text_gap_count * 0.2)
    penalty += min(0.15, table_gap_count * 0.15)
    penalty += min(0.1, figure_caption_gap_count * 0.1)
    penalty += min(0.1, chunk_gap_count * 0.1)
    penalty += min(0.05, embedding_gap_count * 0.05)
    if reading_order_confidence is not None and reading_order_confidence < 0.75:
        penalty += min(0.1, (0.75 - reading_order_confidence) * 0.4)
    return round(max(0.0, 1.0 - penalty), 4)


def _provider_comparison_pending_axes(rankings: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rankings:
        return ["reading_order_confidence", "elapsed_s", "memory_mb"]
    pending: list[str] = []
    if not any((item.get("metrics") or {}).get("reading_order_confidence") is not None for item in rankings):
        pending.append("reading_order_confidence")
    if not any((item.get("metrics") or {}).get("provider_elapsed_s") is not None for item in rankings):
        pending.append("elapsed_s")
    if not any((item.get("metrics") or {}).get("provider_memory_mb") is not None for item in rankings):
        pending.append("memory_mb")
    return pending


def _provider_comparison_has_reading_order_warning(entry: Mapping[str, Any]) -> bool:
    axes = entry.get("axes")
    if not isinstance(axes, Mapping):
        return False
    reading_order = axes.get("reading_order")
    if not isinstance(reading_order, Mapping):
        return False
    return str(reading_order.get("status") or "") == "warning"


def _provider_comparison_has_quality_warning(entry: Mapping[str, Any]) -> bool:
    axes = entry.get("axes")
    if not isinstance(axes, Mapping):
        return False
    for key in ("text_coverage", "table_structure", "figure_caption", "rag_chunking"):
        axis = axes.get(key)
        if isinstance(axis, Mapping) and str(axis.get("status") or "") in {"warning", "gap"}:
            return True
    return _provider_comparison_has_reading_order_warning(entry)


def _provider_comparison_actions(
    *,
    snapshot: Mapping[str, Any],
    comparison_report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    doc_id = str(snapshot.get("doc_id") or getattr(snapshot.get("job"), "doc_id", "") or "")
    if not doc_id:
        return []
    summary = comparison_report.get("summary")
    if not isinstance(summary, Mapping) or not bool(summary.get("needs_attention")):
        return []

    primary_provider_id = str(comparison_report.get("primary_provider_id") or "")
    best_provider_id = str(comparison_report.get("best_provider_id") or "")
    reason_codes: list[str] = []
    if bool(summary.get("best_provider_differs_from_primary")):
        reason_codes.append("best_provider_differs_from_primary")
    if _safe_int(summary.get("providers_with_quality_warnings"), default=0) > 0:
        reason_codes.append("provider_quality_warnings")
    if _safe_int(summary.get("providers_with_reading_order_warning"), default=0) > 0:
        reason_codes.append("provider_reading_order_warnings")
    if _safe_int(summary.get("providers_with_coverage_gaps"), default=0) > 0:
        reason_codes.append("provider_coverage_gaps")

    comparison_context = {
        "comparison_summary": _to_payload(dict(summary)),
        "primary_provider_id": primary_provider_id or None,
        "best_provider_id": best_provider_id or None,
    }
    suggestions = [
        _action_suggestion(
            action_id="inspect_provider_comparison",
            label="Review provider comparison",
            method="GET",
            endpoint=f"/v1/parse/documents/{doc_id}/providers",
            scope="provider_comparison",
            reason_codes=reason_codes,
            context=comparison_context,
        )
    ]
    if bool(summary.get("best_provider_differs_from_primary")):
        suggestions.append(
            _action_suggestion(
                action_id="inspect_provider_route_plan",
                label="Inspect local provider route plan",
                method="GET",
                endpoint="/v1/parse/providers/route-plan",
                scope="provider_route",
                reason_codes=reason_codes,
                params=_quality_local_provider_route_plan_params(snapshot=snapshot, reason_codes=()),
                context={
                    **comparison_context,
                    "local_provider_routing": _quality_local_provider_routing_context(snapshot=snapshot),
                },
            )
        )
    return suggestions


def _provider_quality_gate_payload(
    *,
    quality_gate: Any,
    comparison_report: Mapping[str, Any],
    comparison_actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(quality_gate, Mapping):
        return {}

    payload = _to_payload(dict(quality_gate))
    summary = comparison_report.get("summary")
    if isinstance(summary, Mapping):
        payload["provider_comparison"] = {
            "primary_provider_id": str(comparison_report.get("primary_provider_id") or "") or None,
            "best_provider_id": str(comparison_report.get("best_provider_id") or "") or None,
            "summary": _to_payload(dict(summary)),
            "actions": _merge_action_suggestions((), comparison_actions),
        }

    existing_actions = payload.get("action_suggestions")
    if isinstance(existing_actions, Sequence) and not isinstance(existing_actions, (str, bytes, bytearray)):
        payload["action_suggestions"] = _merge_action_suggestions(existing_actions, comparison_actions)
    elif comparison_actions:
        payload["action_suggestions"] = _merge_action_suggestions((), comparison_actions)
    return payload


def _merge_action_suggestions(
    primary: Sequence[Mapping[str, Any]],
    secondary: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    deferred_review_quality: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(action: Mapping[str, Any]) -> None:
        action_id = str(action.get("action_id") or "")
        method = str(action.get("method") or "")
        endpoint = str(action.get("endpoint") or "")
        key = (action_id, method, endpoint)
        if not action_id or key in seen:
            return
        item = {str(name): _to_payload(value) for name, value in action.items()}
        seen.add(key)
        if action_id == "review_quality":
            deferred_review_quality.append(item)
            return
        merged.append(item)

    for action in primary:
        if isinstance(action, Mapping):
            add(action)
    for action in secondary:
        if isinstance(action, Mapping):
            add(action)
    merged.extend(deferred_review_quality)
    return merged


def _reading_order_axis_status(reading_order_confidence: float | None) -> str:
    if reading_order_confidence is None:
        return "not_observed"
    if reading_order_confidence < 0.75:
        return "warning"
    return "ok"


def _provider_comparison_recommendation(
    signal_set: set[str],
    *,
    coverage_gap_count: int,
) -> str:
    if "rag_empty_text_page" in signal_set:
        return "review_parse_ir_or_local_rerun"
    if "rag_table_without_unit" in signal_set:
        return "compare_table_provider_or_local_rerun"
    if "rag_figure_caption_missing" in signal_set:
        return "review_figure_caption"
    if "rag_units_without_chunks" in signal_set:
        return "rechunk_before_provider_change"
    if "rag_chunks_not_embedded" in signal_set:
        return "reembed_before_provider_change"
    if coverage_gap_count:
        return "inspect_coverage_gaps"
    return "keep_candidate"


def _ratio_float(numerator: int, denominator: int, *, default: float = 0.0) -> float:
    if denominator <= 0:
        return float(default)
    return round(max(0.0, min(1.0, numerator / denominator)), 4)


def _optional_payload_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _counter_payload(counter: Counter[str]) -> dict[str, int]:
    return {
        str(key): int(value)
        for key, value in sorted(counter.items(), key=lambda item: str(item[0]))
        if str(key)
    }


def _document_records_projection(
    snapshot: dict[str, Any],
    *,
    limit: int | None = 100,
    offset: int = 0,
    query: str | None = None,
    table_id: str | None = None,
    quality_signal: str | None = None,
    field_filters: Mapping[str, Any] | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    persisted_records = _persisted_records_from_snapshot(snapshot)
    records = persisted_records if persisted_records is not None else _records_from_snapshot(snapshot)
    result = collect_record_page(
        records,
        limit=limit,
        offset=offset,
        query=query,
        table_id=table_id,
        quality_signal=quality_signal,
        field_filters=field_filters,
        page_start=page_start,
        page_end=page_end,
    )
    return _document_records_response(
        snapshot,
        total=int(result["total"]),
        limit=result["limit"],
        offset=int(result["offset"]),
        items=result["items"],
    )


def _document_records_response(
    snapshot: dict[str, Any],
    *,
    total: int,
    limit: int | None,
    offset: int,
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    job = snapshot.get("job")
    profile_resolution = _profile_resolution_for_document(job=job, pages=[], tables=[])
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": "records",
        "doc_id": str(snapshot.get("doc_id") or getattr(job, "doc_id", "") or ""),
        "parse_run_id": str(getattr(job, "job_id", "") or ""),
        "profile": str(profile_resolution["resolved_profile"]),
        "profile_resolution": profile_resolution,
        "state": _state_value(getattr(job, "state", None)),
        "total": int(total),
        "limit": limit,
        "offset": max(0, int(offset or 0)),
        "items": [dict(item) for item in items],
    }


def _document_view_rows(
    snapshot: dict[str, Any],
    *,
    view_types: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    requested = _requested_document_view_types(view_types)
    persisted = _persisted_document_view_rows(snapshot)
    if all(view_type in persisted for view_type in requested):
        return {view_type: persisted[view_type] for view_type in requested}

    # Persisted views only need the page/line/record rows.  Building the full
    # structured projection here also constructs coverage, IR, parse units and
    # quality-gate payloads that are not stored in ``document_views``.  Keep
    # this path deliberately narrow so cache-hit reruns do not pay for the
    # complete read-model projection a second time.
    blocks = tuple(snapshot.get("blocks") or ())
    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", "") or "")
    parse_run_id = str(getattr(job, "job_id", "") or "")
    projected_pages = _project_pages(blocks)
    tables = _structured_tables(blocks, doc_id=doc_id)
    quality_gate_config = _quality_gate_config(snapshot.get("quality_gate"))
    quality_signals = _quality_signals(
        pages=projected_pages,
        tables=tables,
        blocks=blocks,
        reading_order_confidence_threshold=float(
            quality_gate_config["thresholds"]["min_reading_order_confidence"]
        ),
    )
    profile_resolution = _profile_resolution_for_document(
        job=job,
        pages=projected_pages,
        tables=tables,
    )
    computed_records = _records_from_snapshot(
        snapshot,
        pages=projected_pages,
        tables=tables,
        profile_resolution=profile_resolution,
        quality_signals=quality_signals,
    )
    quality_signals.extend(_record_quality_signals(computed_records))
    chunks = tuple(snapshot.get("chunks") or ())
    if not chunks and snapshot.get("index_manifest") is None:
        # The runtime persistence snapshot intentionally contains no chunks or
        # index manifest.  In that case coverage can only be missing-chunk /
        # missing-unit signals, which are derived directly from the blocks and
        # tables without constructing the complete IR coverage payload.
        quality_signals.extend(
            _lightweight_view_coverage_signals(
                pages=projected_pages,
                tables=tables,
                blocks=blocks,
            )
        )
    else:
        coverage_payload = build_coverage_projection(
            snapshot=snapshot,
            doc_id=doc_id,
            parse_run_id=parse_run_id,
            profile=str(profile_resolution["resolved_profile"]),
            state=_state_value(getattr(job, "state", None)),
            blocks=blocks,
            chunks=chunks,
            pages=projected_pages,
            tables=tables,
            quality_signals=quality_signals,
        )
        quality_signals.extend(
            dict(signal)
            for signal in coverage_payload.get("quality_signals", [])
            if isinstance(signal, Mapping)
        )
    # ``_structured_pages`` is intentionally built after coverage signals are
    # merged: persisted page rows expose the same quality codes as the
    # structured API projection.
    structured_pages = _structured_pages(
        pages=projected_pages,
        tables=tables,
        quality_signals=quality_signals,
    )
    result: dict[str, list[dict[str, Any]]] = {}
    if "pages" in requested:
        page_rows = persisted.get("pages")
        if page_rows is None:
            page_rows = [dict(page) for page in structured_pages if isinstance(page, dict)]
        result["pages"] = page_rows
    if "lines" in requested:
        lines = persisted.get("lines")
        if lines is None:
            lines = _structured_lines_from_blocks(
                blocks,
                doc_id=doc_id,
                parse_run_id=parse_run_id,
            )
        result["lines"] = lines
    if "records" in requested:
        records = persisted.get("records")
        if records is None:
            records = [
                dict(record)
                for record in computed_records
            ]
        result["records"] = records
    return result


def _lightweight_view_coverage_signals(
    *,
    pages: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
    blocks: Sequence[Block],
) -> list[dict[str, Any]]:
    """Derive persisted-page coverage flags without rebuilding the full IR.

    This is intentionally limited to the no-chunks/no-manifest persistence
    snapshot.  API fallbacks with real chunks continue through the canonical
    coverage projection above.
    """
    blocks_by_page: dict[int, list[Block]] = {}
    blocks_by_id: dict[str, Block] = {}
    for block in blocks:
        page_number = _safe_int((block.metadata or {}).get("page"), default=1)
        blocks_by_page.setdefault(page_number, []).append(block)
        blocks_by_id[str(block.block_id)] = block

    indexable_table_ids: set[str] = set()
    for table in tables:
        block_id = str(table.get("block_id") or "")
        block = blocks_by_id.get(block_id)
        if block is not None and _view_block_is_indexable(block):
            table_id = str(table.get("table_id") or "")
            if table_id:
                indexable_table_ids.add(table_id)

    missing_table_ids_by_page: dict[int, list[str]] = {}
    for table in tables:
        table_id = str(table.get("table_id") or "")
        if not table_id or table_id in indexable_table_ids:
            continue
        page_number = _safe_int(table.get("page_number"), default=1)
        missing_table_ids_by_page.setdefault(page_number, []).append(table_id)

    missing_figure_pages: set[int] = set()
    for block in blocks:
        metadata = block.metadata or {}
        role = str(metadata.get("semantic_role") or "").strip().lower()
        if block.type != BlockType.IMAGE and role != "image":
            continue
        if not str(block.content or "").strip() and not str(metadata.get("alt_text") or "").strip():
            missing_figure_pages.add(_safe_int(metadata.get("page"), default=1))

    signals: list[dict[str, Any]] = []
    page_numbers = sorted(
        {
            _safe_int(page.get("page_number"), default=1)
            for page in pages
            if isinstance(page, Mapping)
        }
        | set(blocks_by_page)
    )
    for page_number in page_numbers:
        page_blocks = blocks_by_page.get(page_number, [])
        parsed_text_chars = sum(
            len(str(block.content or "").strip())
            for block in page_blocks
            if _view_block_is_indexable(block)
        )
        indexable_unit_count = sum(1 for block in page_blocks if _view_block_is_indexable(block))
        if parsed_text_chars > 0 and indexable_unit_count == 0:
            signals.append(
                {
                    "code": "rag_empty_text_page",
                    "severity": "warning",
                    "message": "Page has parsed text but no indexable RAG unit",
                    "page_number": page_number,
                }
            )
        elif indexable_unit_count > 0:
            signals.append(
                {
                    "code": "rag_units_without_chunks",
                    "severity": "warning",
                    "message": "Page has indexable RAG units but no chunks",
                    "page_number": page_number,
                }
            )
        table_ids = missing_table_ids_by_page.get(page_number, [])
        if table_ids:
            signals.append(
                {
                    "code": "rag_table_without_unit",
                    "severity": "warning",
                    "message": "Page has structured tables without indexable RAG units",
                    "page_number": page_number,
                    "detail": {"table_ids": table_ids},
                }
            )
        if page_number in missing_figure_pages:
            signals.append(
                {
                    "code": "rag_figure_caption_missing",
                    "severity": "warning",
                    "message": "Page has figures without captions for RAG",
                    "page_number": page_number,
                }
            )
    return signals


def _view_block_is_indexable(block: Block) -> bool:
    metadata = block.metadata or {}
    role = str(metadata.get("semantic_role") or "").strip().lower()
    text = str(block.content or "").strip()
    if role in {"header_footer", "parse_artifact", "version_cell", "page_ref_cell"}:
        return False
    if block.type == BlockType.TABLE:
        return bool(text or metadata.get("cells"))
    if block.type == BlockType.IMAGE or role == "image":
        return bool(text or str(metadata.get("alt_text") or "").strip())
    return bool(text)


def _requested_document_view_types(view_types: Iterable[str] | None) -> tuple[str, ...]:
    if view_types is None:
        return ("pages", "lines", "records")
    requested: list[str] = []
    for view_type in view_types:
        normalized = str(view_type or "").strip().lower()
        if normalized not in {"pages", "lines", "records"} or normalized in requested:
            continue
        requested.append(normalized)
    return tuple(requested) or ("pages", "lines", "records")


def _persisted_document_view_rows(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    document_views = snapshot.get("document_views")
    if not isinstance(document_views, dict):
        return {}
    persisted: dict[str, list[dict[str, Any]]] = {}
    for view_type in ("pages", "lines", "records"):
        rows = document_views.get(view_type)
        if not isinstance(rows, (list, tuple)):
            continue
        persisted[view_type] = [dict(row) for row in rows if isinstance(row, dict)]
    return persisted


def _persisted_records_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]] | None:
    persisted = _persisted_document_view_rows(snapshot)
    if "records" in persisted:
        return persisted["records"]
    document_views = snapshot.get("document_views")
    if not isinstance(document_views, dict):
        return None
    if any(document_views.get(key) for key in ("pages", "lines")):
        return []
    return None


def _structured_lines_from_blocks(
    blocks: tuple[Block, ...],
    *,
    doc_id: str,
    parse_run_id: str,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for block_index, block in enumerate(blocks, start=1):
        metadata = block.metadata or {}
        role = str(metadata.get("semantic_role") or "paragraph")
        if block.type == BlockType.TITLE or role in _ARTIFACT_SEMANTIC_ROLES:
            continue
        page_number = _safe_int(metadata.get("page"), default=1)
        parser = str(metadata.get("parser") or "")
        source_lines = [
            dict(line)
            for line in metadata.get("lines", ()) or ()
            if isinstance(line, Mapping)
        ]
        source_line_count_before = len(lines)
        for line_index, source_line in enumerate(source_lines, start=1):
            text = " ".join(str(source_line.get("text") or "").split())
            if not text:
                continue
            source_page_number = _safe_int(
                source_line.get("page_number"),
                default=page_number,
            )
            line_payload: dict[str, Any] = {
                "line_id": f"{block.block_id}:line:{line_index}",
                "doc_id": doc_id or block.doc_id,
                "parse_run_id": parse_run_id,
                "block_id": block.block_id,
                "block_type": block.type.value,
                "block_index": block_index,
                "line_index": line_index,
                "page_number": source_page_number,
                "page_start": source_page_number,
                "page_end": source_page_number,
                "semantic_role": role,
                "source_parser": parser,
                "text": text,
                "normalized_text": _normalize_record_text(text),
            }
            source_line_id = str(source_line.get("line_id") or "").strip()
            if source_line_id:
                line_payload["source_line_id"] = source_line_id
            source_line_index = _safe_int(source_line.get("line_index"), default=0)
            if source_line_index > 0:
                line_payload["source_line_index"] = source_line_index
            for index_field in ("paragraph_index", "paragraph_line_index", "column_index"):
                index_value = _safe_int(source_line.get(index_field), default=0)
                include_zero_column = (
                    index_field == "column_index"
                    and index_value == 0
                    and index_field in source_line
                )
                if index_value > 0 or include_zero_column:
                    line_payload[index_field] = index_value
            bbox = source_line.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    line_payload["bbox"] = [float(value) for value in bbox]
                except (TypeError, ValueError):
                    pass
            for number_field in ("page_width", "page_height", "confidence"):
                number_value = source_line.get(number_field)
                if isinstance(number_value, (int, float)):
                    line_payload[number_field] = float(number_value)
            source_kind = str(source_line.get("source_kind") or "").strip()
            if source_kind:
                line_payload["source_kind"] = source_kind
            lines.append(line_payload)
        if len(lines) > source_line_count_before:
            continue
        for line_index, text in enumerate(_block_lines(block.content), start=1):
            lines.append(
                {
                    "line_id": f"{block.block_id}:line:{line_index}",
                    "doc_id": doc_id or block.doc_id,
                    "parse_run_id": parse_run_id,
                    "block_id": block.block_id,
                    "block_type": block.type.value,
                    "block_index": block_index,
                    "line_index": line_index,
                    "page_number": page_number,
                    "page_start": page_number,
                    "page_end": page_number,
                    "semantic_role": role,
                    "source_parser": parser,
                    "text": text,
                    "normalized_text": _normalize_record_text(text),
                }
            )
    return lines


def _block_lines(text: str) -> list[str]:
    lines = [
        " ".join(raw_line.split())
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if " ".join(raw_line.split())
    ]
    if lines:
        return lines
    normalized = " ".join(str(text or "").split())
    return [normalized] if normalized else []


def _read_first_metadata(blocks: tuple[Block, ...], key: str) -> Any:
    """Return the first non-None value for ``key`` in any block's metadata."""
    for block in blocks:
        value = block.metadata.get(key)
        if value is not None:
            return value
    return None


def _aggregate_stage_timings(blocks: tuple[Block, ...]) -> dict[str, float] | None:
    """Sum per-page timing fields from block metadata into doc-level totals.

    Returns None when no timing data is present (e.g. pypdf-only path).
    """
    total_layout = 0.0
    total_ocr_render = 0.0
    total_ocr_provider = 0.0
    has_any = False
    for block in blocks:
        m = block.metadata
        v = m.get("layout_elapsed_s")
        if isinstance(v, (int, float)):
            total_layout += float(v)
            has_any = True
        v = m.get("ocr_render_elapsed_s")
        if isinstance(v, (int, float)):
            total_ocr_render += float(v)
        v = m.get("ocr_provider_elapsed_s")
        if isinstance(v, (int, float)):
            total_ocr_provider += float(v)
    if not has_any:
        return None
    result: dict[str, float] = {"layout_s": round(total_layout, 4)}
    if total_ocr_render > 0:
        result["ocr_render_s"] = round(total_ocr_render, 4)
    if total_ocr_provider > 0:
        result["ocr_provider_s"] = round(total_ocr_provider, 4)
    return result


def _structured_tables(blocks: tuple[Block, ...], *, doc_id: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for fallback_index, block in enumerate(blocks, start=1):
        if block.type != BlockType.TABLE:
            continue
        metadata = block.metadata or {}
        page_number = _safe_int(metadata.get("page"), default=1)
        table_index = _safe_int(metadata.get("table_index"), default=len(tables) + 1)
        rows = _safe_int(metadata.get("rows"), default=0)
        cols = _safe_int(metadata.get("cols"), default=0)
        raw_rows = _table_rows_from_metadata(metadata)
        if not rows:
            rows = len(raw_rows)
        if not cols:
            cols = max((len(row) for row in raw_rows), default=0)
        table_id = f"{doc_id}:p{page_number}:t{table_index}"
        cells = _structured_cells(
            raw_rows,
            page_number=page_number,
            table_index=table_index,
        )
        warnings = _table_warnings(raw_rows=raw_rows, rows=rows, cols=cols)
        table: dict[str, Any] = {
            "table_id": table_id,
            "source_doc_id": doc_id,
            "part_doc_id": doc_id,
            "block_id": block.block_id,
            "page_number": page_number,
            "table_index": table_index,
            "source_parser": str(metadata.get("parser") or ""),
            "bbox": metadata.get("bbox"),
            "rows": rows,
            "cols": cols,
            "header_rows": max(0, _safe_int(metadata.get("header_rows"), default=1 if rows else 0)),
            "cells": cells,
            "warnings": warnings,
        }
        for key in (
            "table_type",
            "sheet_name",
            "cell_range",
            "source_cell_range",
            "sheet_table_index",
            "table_title",
            "hidden_sheet",
            "header_values",
            "merged_cells",
            "has_formula",
            "formula_count",
            "truncated",
            "cells_truncated",
            "cells_total",
            "cells_preview_rows",
            "page_span",
            "continuation_group_id",
            "table_group_id",
            "continued_table_id",
            "is_continuation",
            "continued",
            "continues_from",
            "continues_to",
            "continuation_kind",
        ):
            if key in metadata:
                table[key] = metadata[key]
        if not cells and block.content.strip():
            table["text"] = block.content
        table["empty_cell_ratio"] = _empty_cell_ratio(raw_rows)
        table["source_row_col_counts"] = [len(row) for row in raw_rows]
        table["ordinal"] = fallback_index
        tables.append(table)
    return tables


def _structured_cells(
    rows: list[list[str]],
    *,
    page_number: int,
    table_index: int,
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            cells.append(
                {
                    "row_index": row_index,
                    "col_index": col_index,
                    "text": str(value or ""),
                    "confidence": 1.0,
                    "source_page_number": page_number,
                    "source_table_index": table_index,
                    "warnings": [],
                }
            )
    return cells


def _structured_records(
    *,
    blocks: tuple[Block, ...] = (),
    tables: list[dict[str, Any]],
    quality_signals: list[dict[str, Any]],
    profile: str | None = None,
    doc_id: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    signal_codes_by_row: dict[tuple[str, int], list[str]] = {}
    for signal in quality_signals:
        if not isinstance(signal, dict):
            continue
        table_id = str(signal.get("table_id") or "")
        if not table_id or signal.get("row_index") is None:
            continue
        row_index = _safe_int(signal.get("row_index"), default=0)
        signal_codes_by_row.setdefault((table_id, row_index), []).append(str(signal.get("code") or ""))

    for table in tables:
        if not isinstance(table, dict):
            continue
        table_id = str(table.get("table_id") or "")
        rows = _rows_from_structured_cells(table.get("cells"))
        if not rows:
            continue
        page_number = _safe_int(table.get("page_number"), default=1)
        header_rows = max(0, _safe_int(table.get("header_rows"), default=1 if rows else 0))
        header_values = table.get("header_values")
        if isinstance(header_values, list) and header_values:
            headers = _stable_record_headers(header_values, _record_col_count(rows))
        elif header_rows > 0:
            headers = _stable_record_headers(rows[0], _record_col_count(rows))
        else:
            headers = _stable_record_headers([], _record_col_count(rows))
        first_data_row = min(len(rows), header_rows) if header_rows > 0 else 0
        for row_index, row in enumerate(rows[first_data_row:], start=first_data_row):
            if not any(str(cell or "").strip() for cell in row):
                continue
            fields = {
                headers[col_index]: str(row[col_index] if col_index < len(row) else "")
                for col_index in range(len(headers))
            }
            raw_cells = [str(cell or "") for cell in row]
            raw_text = "\t".join(raw_cells).strip()
            record: dict[str, Any] = {
                "record_id": f"{table_id}:r{row_index}",
                "doc_id": str(table.get("source_doc_id") or ""),
                "source": "table-row",
                "table_id": table_id,
                "block_id": table.get("block_id"),
                "page_start": page_number,
                "page_end": page_number,
                "row_index": row_index,
                "fields": fields,
                "raw_cells": raw_cells,
                "raw_text": raw_text,
                "normalized_text": _normalize_record_text(raw_text),
                "quality_signal_codes": signal_codes_by_row.get((table_id, row_index), []),
            }
            for key in ("section", "sheet_name", "table_title", "table_type"):
                if table.get(key) is not None:
                    record[key] = table.get(key)
            record["quality_signal_codes"] = list(
                dict.fromkeys(list(record.get("quality_signal_codes") or []) + _table_record_signal_codes(record))
            )
            records.append(record)
    if str(profile or "").strip().lower() in _TEXT_RECORD_PROFILES:
        records.extend(
            _text_block_records(
                blocks=blocks,
                doc_id=doc_id,
                existing_count=len(records),
            )
        )
    return records


def _records_from_snapshot(
    snapshot: dict[str, Any],
    *,
    pages: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    profile_resolution: Mapping[str, Any] | None = None,
    quality_signals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    blocks = tuple(snapshot.get("blocks") or ())
    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", ""))
    pages = pages if pages is not None else _project_pages(blocks)
    tables = tables if tables is not None else _structured_tables(blocks, doc_id=doc_id)
    profile_resolution = (
        profile_resolution
        if profile_resolution is not None
        else _profile_resolution_for_document(job=job, pages=pages, tables=tables)
    )
    quality_signals = (
        quality_signals
        if quality_signals is not None
        else _quality_signals(pages=pages, tables=tables, blocks=blocks)
    )
    records = _structured_records(
        blocks=blocks,
        tables=tables,
        quality_signals=quality_signals,
        profile=str(profile_resolution["resolved_profile"]),
        doc_id=doc_id,
    )
    record_signals = _record_quality_signals(records)
    if record_signals:
        codes_by_record: dict[str, list[str]] = {}
        for signal in record_signals:
            record_id = str(signal.get("record_id") or "")
            if record_id:
                codes_by_record.setdefault(record_id, []).append(str(signal.get("code") or ""))
        for record in records:
            record_id = str(record.get("record_id") or "")
            if record_id in codes_by_record:
                existing = list(record.get("quality_signal_codes") or [])
                record["quality_signal_codes"] = list(dict.fromkeys(existing + codes_by_record[record_id]))
    return records


def _text_block_records(
    *,
    blocks: tuple[Block, ...],
    doc_id: str | None,
    existing_count: int = 0,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_parts: list[str] = []
    current_block_ids: list[str] = []

    def finish_current() -> None:
        nonlocal current, current_parts, current_block_ids
        if current is None:
            return
        raw_text = "\n".join(part for part in current_parts if part.strip()).strip()
        current["raw_text"] = raw_text
        current["normalized_text"] = _normalize_record_text(raw_text)
        current["source_block_ids"] = list(dict.fromkeys(current_block_ids))
        current["fields"] = _text_record_fields(current)
        current["quality_signal_codes"] = _text_record_signal_codes(current)
        records.append(current)
        current = None
        current_parts = []
        current_block_ids = []

    for block in blocks:
        if block.type == BlockType.TABLE:
            continue
        metadata = block.metadata or {}
        role = str(metadata.get("semantic_role") or "").strip().lower()
        if role in _ARTIFACT_SEMANTIC_ROLES or block.type == BlockType.TITLE:
            continue
        page_number = _safe_int(metadata.get("page"), default=1)
        block_id = str(block.block_id)
        for line_index, line in enumerate(_record_candidate_lines(block.content), start=1):
            match = _TEXT_RECORD_START_PATTERN.match(line)
            if match is None and _TEXT_RECORD_HEADER_PATTERN.search(line):
                continue
            if match:
                finish_current()
                row_number = int(match.group("row"))
                body = str(match.group("body") or "").strip()
                current = {
                    "record_id": f"{block_id}:text:r{row_number}:l{line_index}",
                    "doc_id": str(doc_id or block.doc_id),
                    "source": "text-block",
                    "page_start": page_number,
                    "page_end": page_number,
                    "row_number": row_number,
                    "line_start": line_index,
                    "line_end": line_index,
                    "raw_cells": [str(row_number), body],
                }
                current_parts = [line]
                current_block_ids = [block_id]
                continue
            if current is not None:
                current_parts.append(line)
                current_block_ids.append(block_id)
                current["page_end"] = max(_safe_int(current.get("page_end"), default=page_number), page_number)
                current["line_end"] = line_index
                current["row_continuation_detected"] = True
    finish_current()
    return records


def _record_candidate_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = " ".join(raw_line.split())
        if normalized:
            lines.append(normalized)
    if not lines:
        normalized_text = " ".join(str(text or "").split())
        if normalized_text:
            lines.append(normalized_text)
    return lines


def _text_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    raw_text = str(record.get("raw_text") or "")
    row_number = record.get("row_number")
    lines = _record_candidate_lines(raw_text)
    body_lines = list(lines)
    if body_lines:
        match = _TEXT_RECORD_START_PATTERN.match(body_lines[0])
        if match:
            body_lines[0] = str(match.group("body") or "").strip()
    body = "\n".join(body_lines)
    certificate = _extract_certificate_or_project_no(body)
    latest_date = _extract_latest_date(raw_text)
    fields: dict[str, Any] = {
        "row_number": row_number,
        "text": _normalize_record_text(body),
    }
    if certificate:
        fields["certificate_or_project_no"] = certificate
    if latest_date:
        fields["latest_date"] = latest_date
    holder = _holder_or_name_start(body, certificate=certificate, latest_date=latest_date)
    if holder:
        fields["holder_or_name_start"] = holder
    return fields


def _extract_certificate_or_project_no(text: str) -> str | None:
    match = _TEXT_RECORD_CERT_PATTERN.search(text)
    if match:
        return match.group(0)
    for token in str(text or "").split():
        cleaned = token.strip(" ,;；，。:：")
        if any(char.isdigit() for char in cleaned) and any(char.isalpha() for char in cleaned) and len(cleaned) >= 4:
            return cleaned
    return None


def _extract_latest_date(text: str) -> str | None:
    matches = [match.group(0) for match in _TEXT_RECORD_DATE_PATTERN.finditer(str(text or ""))]
    for match in reversed(matches):
        normalized = _normalize_record_date(match)
        if normalized:
            return normalized
    return None


def _normalize_record_date(value: str) -> str | None:
    normalized = str(value or "").replace("/", "-").replace(".", "-")
    parts = normalized.split("-")
    if len(parts) != 3:
        return None
    try:
        parsed = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    return parsed.isoformat()


def _contains_valid_record_date(value: str) -> bool:
    return any(_normalize_record_date(match.group(0)) for match in _TEXT_RECORD_DATE_PATTERN.finditer(str(value or "")))


def _holder_or_name_start(text: str, *, certificate: str | None, latest_date: str | None) -> str | None:
    value = str(text or "")
    if certificate:
        value = value.replace(certificate, "", 1)
    if latest_date:
        value = value.replace(latest_date, "", 1)
    value = _TEXT_RECORD_DATE_PATTERN.sub("", value)
    return _normalize_record_text(value)[:120] or None


def _text_record_signal_codes(record: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    raw_text = str(record.get("raw_text") or "")
    if not fields.get("certificate_or_project_no"):
        codes.append("record_field_missing")
    if _TEXT_RECORD_ANY_DATE_PATTERN.search(raw_text) and not fields.get("latest_date"):
        codes.append("date_parse_failed")
    if _record_column_shift_suspected(record):
        codes.append("column_shift_suspected")
    if bool(record.get("row_continuation_detected")):
        codes.append("row_continuation_detected")
    if "\n" in raw_text and not bool(record.get("row_continuation_detected")):
        codes.append("record_boundary_uncertain")
    return list(dict.fromkeys(codes))


def _table_record_signal_codes(record: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    if not fields:
        return codes
    values = [str(value or "") for value in fields.values()]
    has_valid_date = any(_contains_valid_record_date(value) for value in values)
    has_any_date = any(_TEXT_RECORD_ANY_DATE_PATTERN.search(value) for value in values)
    if has_any_date and not has_valid_date:
        codes.append("date_parse_failed")
    if _record_column_shift_suspected(record):
        codes.append("column_shift_suspected")
    return list(dict.fromkeys(codes))


def _record_column_shift_suspected(record: dict[str, Any]) -> bool:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    if not fields:
        return False
    raw_text = str(record.get("raw_text") or "")
    certificate = str(fields.get("certificate_or_project_no") or "")
    latest_date = str(fields.get("latest_date") or "")
    if certificate and latest_date:
        cert_pos = raw_text.find(certificate)
        date_pos = raw_text.find(latest_date)
        if cert_pos >= 0 and date_pos >= 0 and date_pos < cert_pos:
            return True

    date_field_values: list[str] = []
    non_date_values: list[str] = []
    identifier_field_values: list[str] = []
    non_identifier_values: list[str] = []
    for raw_key, raw_value in fields.items():
        key = str(raw_key or "").strip().lower()
        value = str(raw_value or "")
        if _field_has_hint(key, _DATE_FIELD_HINTS):
            date_field_values.append(value)
        else:
            non_date_values.append(value)
        if _field_has_hint(key, _IDENTIFIER_FIELD_HINTS):
            identifier_field_values.append(value)
        else:
            non_identifier_values.append(value)
    date_in_unexpected_field = any(_TEXT_RECORD_ANY_DATE_PATTERN.search(value) for value in non_date_values)
    date_field_valid = any(_contains_valid_record_date(value) for value in date_field_values)
    identifier_in_unexpected_field = any(_extract_certificate_or_project_no(value) for value in non_identifier_values)
    identifier_field_has_value = any(_extract_certificate_or_project_no(value) for value in identifier_field_values)
    return (date_in_unexpected_field and not date_field_valid) or (
        identifier_in_unexpected_field and not identifier_field_has_value
    )


def _field_has_hint(value: str, hints: tuple[str, ...]) -> bool:
    normalized = str(value or "").strip().lower()
    return any(hint in normalized for hint in hints)


def _record_quality_signals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for record in records:
        record_id = str(record.get("record_id") or "")
        page_number = _safe_int(record.get("page_start"), default=1)
        for code in list(record.get("quality_signal_codes") or []):
            severity = "info" if code in {"row_continuation_detected"} else "warning"
            signals.append(
                _quality_signal(
                    code=str(code),
                    severity=severity,
                    message=_quality_signal_message(str(code)),
                    page_number=page_number,
                    record_id=record_id,
                    detail={"row_number": record.get("row_number")},
                )
            )
    return signals


def _rows_from_structured_cells(raw_cells: Any) -> list[list[str]]:
    if not isinstance(raw_cells, list):
        return []
    cells: dict[int, dict[int, str]] = {}
    max_col = -1
    for cell in raw_cells:
        if not isinstance(cell, dict):
            continue
        row_index = _safe_int(cell.get("row_index"), default=0)
        col_index = _safe_int(cell.get("col_index"), default=0)
        max_col = max(max_col, col_index)
        cells.setdefault(row_index, {})[col_index] = str(cell.get("text") or "")
    if not cells:
        return []
    rows: list[list[str]] = []
    for row_index in range(max(cells) + 1):
        row = cells.get(row_index, {})
        rows.append([row.get(col_index, "") for col_index in range(max_col + 1)])
    return rows


def _stable_record_headers(raw_headers: list[Any], col_count: int) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index in range(max(0, col_count)):
        raw = str(raw_headers[index] if index < len(raw_headers) else "").strip()
        header = raw or f"col_{index + 1}"
        if header in seen:
            seen[header] += 1
            header = f"{header}_{seen[header]}"
        else:
            seen[header] = 1
        headers.append(header)
    return headers


def _record_col_count(rows: list[list[str]]) -> int:
    return max((len(row) for row in rows), default=0)


def _normalize_record_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _records_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_table: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for record in records:
        table_id = str(record.get("table_id") or "")
        if table_id:
            by_table[table_id] = by_table.get(table_id, 0) + 1
        source = str(record.get("source") or ("table-row" if table_id else "unknown"))
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "total": len(records),
        "table_count": len(by_table),
        "text_record_count": by_source.get("text-block", 0),
        "by_source": by_source,
        "sample_record_ids": [str(record.get("record_id") or "") for record in records[:5]],
    }


def _table_rows_from_metadata(metadata: dict[str, Any]) -> list[list[str]]:
    raw = metadata.get("cells")
    if raw is None:
        raw = metadata.get("cells_preview")
    if not isinstance(raw, list):
        return []
    rows: list[list[str]] = []
    for row in raw:
        if isinstance(row, (list, tuple)):
            rows.append([str(value or "") for value in row])
        else:
            rows.append([str(row or "")])
    return rows


def _table_warnings(*, raw_rows: list[list[str]], rows: int, cols: int) -> list[str]:
    warnings: list[str] = []
    if rows == 0 or cols == 0:
        warnings.append("table_empty")
    if raw_rows and not any(cell.strip() for cell in raw_rows[0]):
        warnings.append("table_header_missing")
    row_widths = {len(row) for row in raw_rows if row}
    if len(row_widths) > 1:
        warnings.append("table_ragged_rows")
    if _empty_cell_ratio(raw_rows) > 0.5:
        warnings.append("table_empty_ratio_high")
    return warnings


def _quality_signals(
    *,
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    blocks: tuple[Block, ...],
    reading_order_confidence_threshold: float = DEFAULT_READING_ORDER_CONFIDENCE_THRESHOLD,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for page in pages:
        page_number = _safe_int(page.get("page_number"), default=1)
        if not str(page.get("text") or "").strip() and not page.get("tables"):
            signals.append(
                _quality_signal(
                    code="empty_page",
                    severity="info",
                    message="Page has no text or tables",
                    page_number=page_number,
                )
            )
        if bool(page.get("ocr_attempted")):
            signals.append(
                _quality_signal(
                    code="ocr_attempted",
                    severity="info",
                    message="OCR was attempted for this page",
                    page_number=page_number,
                )
            )
        if page.get("ocr_error_reasons"):
            signals.append(
                _quality_signal(
                    code="ocr_failed",
                    severity="error",
                    message="OCR failed for this page",
                    page_number=page_number,
                    detail={"reasons": page.get("ocr_error_reasons")},
                )
            )
        reading_order_confidence = _optional_payload_float(page.get("reading_order_confidence"))
        if (
            reading_order_confidence is not None
            and reading_order_confidence < reading_order_confidence_threshold
        ):
            signals.append(
                _quality_signal(
                    code="reading_order_low_confidence",
                    severity="warning",
                    message=_quality_signal_message("reading_order_low_confidence"),
                    page_number=page_number,
                    detail={
                        "reading_order_confidence": round(reading_order_confidence, 4),
                        "threshold": round(reading_order_confidence_threshold, 4),
                    },
                )
            )

    common_cols = _common_table_col_count(tables)
    for table in tables:
        table_id = str(table.get("table_id") or "")
        page_number = _safe_int(table.get("page_number"), default=1)
        for warning in table.get("warnings", []):
            severity = "warning"
            if warning == "table_empty":
                severity = "error"
            signals.append(
                _quality_signal(
                    code=str(warning),
                    severity=severity,
                    message=_quality_signal_message(str(warning)),
                    page_number=page_number,
                    table_id=table_id,
                    row_index=0 if warning == "table_header_missing" else None,
                )
            )
        cols = _safe_int(table.get("cols"), default=0)
        if common_cols > 0 and cols > 0 and cols != common_cols:
            signals.append(
                _quality_signal(
                    code="table_col_count_changed",
                    severity="warning",
                    message="Table column count differs from the document's common table width",
                    page_number=page_number,
                    table_id=table_id,
                    detail={"cols": cols, "common_cols": common_cols},
                )
            )
        if bool(table.get("cells_truncated")):
            signals.append(
                _quality_signal(
                    code="table_cells_truncated",
                    severity="warning",
                    message=_quality_signal_message("table_cells_truncated"),
                    page_number=page_number,
                    table_id=table_id,
                    detail={
                        "cells_total": table.get("cells_total"),
                        "cells_preview_rows": table.get("cells_preview_rows"),
                    },
                )
            )
        if bool(table.get("truncated")):
            signals.append(
                _quality_signal(
                    code="table_source_truncated",
                    severity="warning",
                    message=_quality_signal_message("table_source_truncated"),
                    page_number=page_number,
                    table_id=table_id,
                )
            )
        if bool(table.get("hidden_sheet")):
            signals.append(
                _quality_signal(
                    code="table_hidden_sheet",
                    severity="info",
                    message=_quality_signal_message("table_hidden_sheet"),
                    page_number=page_number,
                    table_id=table_id,
                )
            )
        merged_cells = table.get("merged_cells")
        if isinstance(merged_cells, list) and merged_cells:
            signals.append(
                _quality_signal(
                    code="table_merged_cells",
                    severity="info",
                    message=_quality_signal_message("table_merged_cells"),
                    page_number=page_number,
                    table_id=table_id,
                    detail={"merged_cells": list(merged_cells)},
                )
            )
        if bool(table.get("has_formula")):
            signals.append(
                _quality_signal(
                    code="table_formula_cells",
                    severity="info",
                    message=_quality_signal_message("table_formula_cells"),
                    page_number=page_number,
                    table_id=table_id,
                    detail={"formula_count": table.get("formula_count")},
                )
            )
        header_values = table.get("header_values")
        if isinstance(header_values, list) and header_values:
            header_texts = [str(value or "").strip() for value in header_values]
            blank_columns = [index for index, value in enumerate(header_texts) if not value]
            if blank_columns and len(blank_columns) < len(header_texts):
                signals.append(
                    _quality_signal(
                        code="table_header_blank_cells",
                        severity="warning",
                        message=_quality_signal_message("table_header_blank_cells"),
                        page_number=page_number,
                        table_id=table_id,
                        row_index=0,
                        detail={"col_indexes": blank_columns},
                    )
                )
            duplicate_headers = _duplicate_header_values(header_texts)
            if duplicate_headers:
                signals.append(
                    _quality_signal(
                        code="table_header_duplicate_values",
                        severity="warning",
                        message=_quality_signal_message("table_header_duplicate_values"),
                        page_number=page_number,
                        table_id=table_id,
                        row_index=0,
                        detail={"values": duplicate_headers},
                    )
                )
    return signals


def _quality_signal(
    *,
    code: str,
    severity: str,
    message: str,
    page_number: int | None = None,
    table_id: str | None = None,
    record_id: str | None = None,
    row_index: int | None = None,
    col_index: int | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if page_number is not None:
        signal["page_number"] = page_number
    if table_id:
        signal["table_id"] = table_id
    if record_id:
        signal["record_id"] = record_id
    if row_index is not None:
        signal["row_index"] = row_index
    if col_index is not None:
        signal["col_index"] = col_index
    if detail:
        signal["detail"] = detail
    return signal


def _quality_signal_message(code: str) -> str:
    return {
        "table_empty": "Table has no structured cells",
        "table_header_missing": "Table header row is empty",
        "table_ragged_rows": "Table rows have inconsistent column counts",
        "table_empty_ratio_high": "Table has a high ratio of empty cells",
        "table_cells_truncated": "Table cell metadata was truncated",
        "table_source_truncated": "Table source range was truncated by parser limits",
        "table_hidden_sheet": "Table comes from a hidden sheet",
        "table_merged_cells": "Table contains merged cells",
        "table_formula_cells": "Table contains formula cells",
        "table_header_blank_cells": "Table header row has blank cells",
        "table_header_duplicate_values": "Table header row has duplicate values",
        "reading_order_low_confidence": "Page reading-order confidence is below threshold",
        "column_shift_suspected": "Record fields may be shifted across columns",
        "date_parse_failed": "Record date field could not be parsed",
        "record_field_missing": "Record is missing an expected field",
        "row_continuation_detected": "Record spans continuation lines",
        "record_boundary_uncertain": "Record boundary is uncertain",
    }.get(code, code)


def _duplicate_header_values(header_values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in header_values:
        normalized = value.strip().lower()
        if not normalized:
            continue
        if normalized in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(normalized)
    return duplicates


def _quality_signal_summary(signals: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for signal in signals:
        severity = str(signal.get("severity") or "info")
        code = str(signal.get("code") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_code[code] = by_code.get(code, 0) + 1
    return {
        "total": len(signals),
        "by_severity": by_severity,
        "by_code": by_code,
    }


def _structured_pages(
    *,
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    quality_signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    table_ids_by_page: dict[int, list[str]] = {}
    for table in tables:
        page_number = _safe_int(table.get("page_number"), default=1)
        table_ids_by_page.setdefault(page_number, []).append(str(table.get("table_id") or ""))
    signal_codes_by_page: dict[int, list[str]] = {}
    for signal in quality_signals:
        page_number = signal.get("page_number")
        if page_number is None:
            continue
        signal_codes_by_page.setdefault(_safe_int(page_number, default=1), []).append(str(signal.get("code") or ""))

    structured: list[dict[str, Any]] = []
    for page in pages:
        page_number = _safe_int(page.get("page_number"), default=1)
        structured.append(
            {
                "page_number": page_number,
                "page_type": page.get("page_type", "body"),
                "text": page.get("text", ""),
                "table_ids": table_ids_by_page.get(page_number, []),
                "quality_signal_codes": signal_codes_by_page.get(page_number, []),
                "confidence": page.get("confidence", 1.0),
                "reading_order_confidence": page.get("reading_order_confidence"),
            }
        )
    return structured


def _parse_units(
    *,
    snapshot: dict[str, Any],
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    quality_signals: list[dict[str, Any]],
    coverage_pages: Sequence[Mapping[str, Any]],
    coverage_units: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    partition_parts = snapshot.get("partition_parts")
    if isinstance(partition_parts, list):
        units: list[dict[str, Any]] = []
        for index, part in enumerate(partition_parts, start=1):
            if not isinstance(part, dict):
                continue
            part_id = str(part.get("part_id") or part.get("parse_unit_id") or f"part-{index}")
            page_start = _safe_int(part.get("page_start"), default=1)
            page_end = _safe_int(part.get("page_end"), default=page_start)
            part_signals = _signals_for_page_range(
                quality_signals=quality_signals,
                page_start=page_start,
                page_end=page_end,
            )
            quality_signal_codes = sorted(
                {
                    str(signal.get("code") or "")
                    for signal in part_signals
                    if str(signal.get("code") or "").strip()
                }
            )
            quality_signal_page_numbers = sorted(
                {
                    _safe_int(signal.get("page_number"), default=0)
                    for signal in part_signals
                    if _safe_int(signal.get("page_number"), default=0) > 0
                }
            )
            coverage_summary, coverage_gap_pages = _part_coverage_summary(
                coverage_pages=coverage_pages,
                coverage_units=coverage_units,
                page_start=page_start,
                page_end=page_end,
            )
            unit_payload = {
                "parse_unit_id": str(part.get("parse_unit_id") or part_id),
                "part_id": part_id,
                "source_doc_id": str(part.get("source_doc_id") or snapshot.get("doc_id") or ""),
                "part_doc_id": str(part.get("part_doc_id") or part_id),
                "part_index": _safe_int(part.get("part_index"), default=index),
                "source_type": str(part.get("source_type") or ""),
                "page_start": page_start,
                "page_end": page_end,
                "state": _state_value(part.get("state")),
                "job_id": part.get("job_id"),
                "table_count": _safe_int(part.get("table_count"), default=0),
                "quality_signal_count": max(
                    _safe_int(part.get("quality_signal_count"), default=0),
                    len(part_signals),
                ),
                "quality_signal_codes": quality_signal_codes,
                "quality_signal_page_numbers": quality_signal_page_numbers,
                "rerun_supported": bool(part.get("rerun_supported", False)),
                "last_error": part.get("last_error"),
                "provider_ids": list(part.get("provider_ids") or []),
                "provider_route_plan": _to_payload(part.get("provider_route_plan")) if isinstance(part.get("provider_route_plan"), Mapping) else None,
                "local_provider_routing": (
                    _normalize_local_provider_routing_payload(part.get("local_provider_routing"))
                    if isinstance(part.get("local_provider_routing"), Mapping)
                    else None
                ),
                "coverage_summary": coverage_summary,
                "coverage_gap_pages": coverage_gap_pages,
                "rag_coverage_quality": rag_coverage_quality_payload(coverage_summary) if coverage_summary is not None else None,
            }
            previous_part_observation = part.get("previous_part_observation")
            if isinstance(previous_part_observation, Mapping):
                unit_payload["previous_part_observation"] = _to_payload(previous_part_observation)
            rerun_comparison = part.get("rerun_comparison")
            if isinstance(rerun_comparison, Mapping):
                unit_payload["rerun_comparison"] = _to_payload(rerun_comparison)
            elif isinstance(previous_part_observation, Mapping):
                unit_payload["rerun_comparison"] = _part_rerun_comparison(
                    previous=previous_part_observation,
                    current=unit_payload,
                )
            units.append(unit_payload)
        if units:
            return units

    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", ""))
    page_numbers = [_safe_int(page.get("page_number"), default=1) for page in pages]
    if not page_numbers:
        page_numbers = [1]
    page_start = min(page_numbers)
    page_end = max(page_numbers)
    coverage_summary, coverage_gap_pages = _part_coverage_summary(
        coverage_pages=coverage_pages,
        coverage_units=coverage_units,
        page_start=page_start,
        page_end=page_end,
    )
    return [
        {
            "parse_unit_id": f"{doc_id}:unit:1",
            "source_doc_id": doc_id,
            "part_doc_id": doc_id,
            "part_index": 1,
            "source_type": str(getattr(job, "media_type", "") or ""),
            "page_start": page_start,
            "page_end": page_end,
            "state": _state_value(getattr(job, "state", None)),
            "table_count": len(tables),
            "quality_signal_count": len(quality_signals),
            "coverage_summary": coverage_summary,
            "coverage_gap_pages": coverage_gap_pages,
            "rag_coverage_quality": rag_coverage_quality_payload(coverage_summary) if coverage_summary is not None else None,
        }
    ]


def _part_coverage_summary(
    *,
    coverage_pages: Sequence[Mapping[str, Any]],
    coverage_units: Sequence[Mapping[str, Any]],
    page_start: int,
    page_end: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    relevant_pages = [
        page
        for page in coverage_pages
        if page_start <= _safe_int(page.get("page_number"), default=0) <= page_end
    ]
    if not relevant_pages:
        return None, []
    relevant_units = [
        unit
        for unit in coverage_units
        if _part_unit_in_page_range(unit=unit, page_start=page_start, page_end=page_end)
    ]

    pages_with_parsed_text = sum(
        1 for page in relevant_pages if _safe_int(page.get("parsed_text_chars"), default=0) > 0
    )
    pages_with_indexable_units = sum(
        1 for page in relevant_pages if _safe_int(page.get("indexable_unit_count"), default=0) > 0
    )
    pages_missing_rag_units = sum(
        1 for page in relevant_pages if str(page.get("missing_reason") or "") == "no_indexable_units"
    )
    pages_missing_chunks = sum(
        1 for page in relevant_pages if str(page.get("missing_reason") or "") == "no_chunks_for_indexable_units"
    )
    pages_chunks_not_embedded = sum(
        1 for page in relevant_pages if str(page.get("missing_reason") or "") == "chunks_not_embedded"
    )
    pages_with_coverage_gaps = sum(
        1
        for page in relevant_pages
        if page.get("missing_reason") is not None
        or _string_list_payload(page.get("table_ids_without_units"))
        or _string_list_payload(page.get("figure_ids_missing_caption"))
    )
    pages_table_without_units = sum(1 for page in relevant_pages if page.get("table_ids_without_units"))
    pages_figure_caption_missing = sum(1 for page in relevant_pages if page.get("figure_ids_missing_caption"))
    total_indexable_units = sum(_safe_int(page.get("indexable_unit_count"), default=0) for page in relevant_pages)
    total_chunked_units = sum(_safe_int(page.get("chunked_unit_count"), default=0) for page in relevant_pages)
    table_pages = sum(1 for page in relevant_pages if _safe_int(page.get("table_count"), default=0) > 0)
    table_pages_with_units = sum(
        1
        for page in relevant_pages
        if _safe_int(page.get("table_count"), default=0) > 0
        and _safe_int(page.get("indexable_unit_count"), default=0) > 0
    )
    gap_pages = [
        _part_gap_page_payload(page)
        for page in relevant_pages
        if page.get("missing_reason") is not None
        or _string_list_payload(page.get("table_ids_without_units"))
        or _string_list_payload(page.get("figure_ids_missing_caption"))
    ]
    if relevant_units:
        indexable_units = [unit for unit in relevant_units if bool(unit.get("should_index_for_rag"))]
        skipped_units = [unit for unit in relevant_units if not bool(unit.get("should_index_for_rag"))]
        embedded_units = [unit for unit in indexable_units if bool(unit.get("embedded"))]
        unembedded_units = [
            unit
            for unit in indexable_units
            if _string_list_payload(unit.get("chunk_ids")) and not bool(unit.get("embedded"))
        ]
        gap_unit_ids = [
            str(unit.get("unit_id") or "")
            for unit in indexable_units
            if unit.get("missing_reason") is not None and str(unit.get("unit_id") or "")
        ]
        total_unit_count = len(relevant_units)
        skipped_unit_count = len(skipped_units)
        embedded_unit_count = len(embedded_units)
        unembedded_unit_count = len(unembedded_units)
        processing_status_counts = {
            status: sum(1 for unit in relevant_units if str(unit.get("processing_status") or "") == status)
            for status in ("pending", "processed", "skipped", "failed", "reviewed")
        }
        unknown_status_count = max(0, total_unit_count - sum(processing_status_counts.values()))
        processing_status_counts["pending"] += unknown_status_count
    else:
        total_unit_count = sum(len(_string_list_payload(page.get("unit_ids"))) for page in relevant_pages)
        skipped_unit_count = sum(len(_string_list_payload(page.get("skipped_unit_ids"))) for page in relevant_pages)
        unchunked_unit_ids = [
            unit_id
            for page in relevant_pages
            for unit_id in _string_list_payload(page.get("unchunked_unit_ids"))
        ]
        unembedded_unit_ids = [
            unit_id
            for page in relevant_pages
            for unit_id in _string_list_payload(page.get("unembedded_unit_ids"))
        ]
        indexable_unit_count = sum(len(_string_list_payload(page.get("indexable_unit_ids"))) for page in relevant_pages)
        gap_unit_ids = sorted(dict.fromkeys(unchunked_unit_ids + unembedded_unit_ids))
        unembedded_unit_count = len(unembedded_unit_ids)
        embedded_unit_count = max(indexable_unit_count - len(unchunked_unit_ids) - unembedded_unit_count, 0)
        failed_unit_count = len(set(unchunked_unit_ids + unembedded_unit_ids))
        processing_status_counts = {
            "pending": 0,
            "processed": max(0, total_unit_count - skipped_unit_count - failed_unit_count),
            "skipped": skipped_unit_count,
            "failed": failed_unit_count,
            "reviewed": 0,
        }
    accounted_unit_count = sum(processing_status_counts.values())
    summary = {
        "total_pages": len(relevant_pages),
        "pages_with_parsed_text": pages_with_parsed_text,
        "pages_with_indexable_units": pages_with_indexable_units,
        "pages_missing_rag_units": pages_missing_rag_units,
        "pages_missing_chunks": pages_missing_chunks,
        "pages_chunks_not_embedded": pages_chunks_not_embedded,
        "pages_with_coverage_gaps": pages_with_coverage_gaps,
        "pages_table_without_units": pages_table_without_units,
        "pages_figure_caption_missing": pages_figure_caption_missing,
        "total_indexable_units": total_indexable_units,
        "total_chunked_units": total_chunked_units,
        "total_unit_count": total_unit_count,
        "accounted_unit_count": accounted_unit_count,
        "unaccounted_unit_count": max(0, total_unit_count - accounted_unit_count),
        "processing_status_counts": processing_status_counts,
        "skipped_unit_count": skipped_unit_count,
        "embedded_unit_count": embedded_unit_count,
        "unembedded_unit_count": unembedded_unit_count,
        "gap_unit_ids": gap_unit_ids,
        "gap_pages": gap_pages,
        "text_page_coverage_ratio": _ratio_float(
            pages_with_indexable_units,
            pages_with_parsed_text,
            default=1.0,
        ),
        "unit_chunk_coverage_ratio": _ratio_float(
            total_chunked_units,
            total_indexable_units,
            default=1.0,
        ),
        "table_unit_coverage_ratio": _ratio_float(
            table_pages_with_units,
            table_pages,
            default=1.0,
        ),
    }
    return summary, gap_pages


def _signals_for_page_range(
    *,
    quality_signals: Sequence[Mapping[str, Any]],
    page_start: int,
    page_end: int,
) -> list[Mapping[str, Any]]:
    return [
        signal
        for signal in quality_signals
        if page_start <= _safe_int(signal.get("page_number"), default=0) <= page_end
    ]


def _part_rerun_comparison(
    *,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    previous_coverage = dict(previous.get("coverage_summary") or {}) if isinstance(previous.get("coverage_summary"), Mapping) else {}
    current_coverage = dict(current.get("coverage_summary") or {}) if isinstance(current.get("coverage_summary"), Mapping) else {}
    previous_rag = dict(previous.get("rag_coverage_quality") or {}) if isinstance(previous.get("rag_coverage_quality"), Mapping) else {}
    current_rag = dict(current.get("rag_coverage_quality") or {}) if isinstance(current.get("rag_coverage_quality"), Mapping) else {}
    previous_signal_codes = _string_list_payload(previous.get("quality_signal_codes"))
    current_signal_codes = _string_list_payload(current.get("quality_signal_codes"))
    previous_flags = _string_list_payload(previous_rag.get("flags"))
    current_flags = _string_list_payload(current_rag.get("flags"))
    previous_selected_provider_id = _part_observation_selected_provider_id(previous)
    current_selected_provider_id = _part_observation_selected_provider_id(current)
    previous_gap_unit_ids = _part_summary_string_list(previous_coverage, "gap_unit_ids")
    current_gap_unit_ids = _part_summary_string_list(current_coverage, "gap_unit_ids")

    improvement_axes: list[str] = []
    regression_axes: list[str] = []

    coverage_gap_delta = _part_summary_int(current_coverage, "pages_with_coverage_gaps") - _part_summary_int(previous_coverage, "pages_with_coverage_gaps")
    quality_signal_count_delta = _safe_int(current.get("quality_signal_count"), default=0) - _safe_int(previous.get("quality_signal_count"), default=0)
    gap_unit_count_delta = len(current_gap_unit_ids) - len(previous_gap_unit_ids)
    unembedded_unit_count_delta = _part_summary_int(current_coverage, "unembedded_unit_count") - _part_summary_int(previous_coverage, "unembedded_unit_count")
    text_page_coverage_ratio_delta = _part_summary_float(current_coverage, "text_page_coverage_ratio") - _part_summary_float(previous_coverage, "text_page_coverage_ratio")
    unit_chunk_coverage_ratio_delta = _part_summary_float(current_coverage, "unit_chunk_coverage_ratio") - _part_summary_float(previous_coverage, "unit_chunk_coverage_ratio")
    table_unit_coverage_ratio_delta = _part_summary_float(current_coverage, "table_unit_coverage_ratio") - _part_summary_float(previous_coverage, "table_unit_coverage_ratio")

    if coverage_gap_delta < 0:
        improvement_axes.append("coverage_gaps")
    elif coverage_gap_delta > 0:
        regression_axes.append("coverage_gaps")
    if quality_signal_count_delta < 0:
        improvement_axes.append("quality_signal_count")
    elif quality_signal_count_delta > 0:
        regression_axes.append("quality_signal_count")
    if gap_unit_count_delta < 0:
        improvement_axes.append("gap_units")
    elif gap_unit_count_delta > 0:
        regression_axes.append("gap_units")
    if unembedded_unit_count_delta < 0:
        improvement_axes.append("unembedded_units")
    elif unembedded_unit_count_delta > 0:
        regression_axes.append("unembedded_units")
    if text_page_coverage_ratio_delta > 0:
        improvement_axes.append("text_page_coverage_ratio")
    elif text_page_coverage_ratio_delta < 0:
        regression_axes.append("text_page_coverage_ratio")
    if unit_chunk_coverage_ratio_delta > 0:
        improvement_axes.append("unit_chunk_coverage_ratio")
    elif unit_chunk_coverage_ratio_delta < 0:
        regression_axes.append("unit_chunk_coverage_ratio")
    if table_unit_coverage_ratio_delta > 0:
        improvement_axes.append("table_unit_coverage_ratio")
    elif table_unit_coverage_ratio_delta < 0:
        regression_axes.append("table_unit_coverage_ratio")

    provider_changed = (
        bool(previous_selected_provider_id or current_selected_provider_id)
        and previous_selected_provider_id != current_selected_provider_id
    )
    if improvement_axes and regression_axes:
        status = "mixed"
    elif improvement_axes:
        status = "improved"
    elif regression_axes:
        status = "regressed"
    elif provider_changed:
        status = "provider_changed"
    else:
        status = "unchanged"

    return {
        "schema_version": PART_RERUN_COMPARISON_SCHEMA_VERSION,
        "status": status,
        "changed": bool(improvement_axes or regression_axes or provider_changed),
        "improved": bool(improvement_axes) and not bool(regression_axes),
        "regressed": bool(regression_axes) and not bool(improvement_axes),
        "improvement_axes": improvement_axes,
        "regression_axes": regression_axes,
        "previous_job_id": previous.get("job_id"),
        "current_job_id": current.get("job_id"),
        "previous_state": str(previous.get("state") or ""),
        "current_state": str(current.get("state") or ""),
        "previous_selected_provider_id": previous_selected_provider_id,
        "current_selected_provider_id": current_selected_provider_id,
        "provider_changed": provider_changed,
        "quality_signal_count_delta": quality_signal_count_delta,
        "coverage_gap_delta": coverage_gap_delta,
        "gap_unit_count_delta": gap_unit_count_delta,
        "unembedded_unit_count_delta": unembedded_unit_count_delta,
        "text_page_coverage_ratio_delta": round(text_page_coverage_ratio_delta, 4),
        "unit_chunk_coverage_ratio_delta": round(unit_chunk_coverage_ratio_delta, 4),
        "table_unit_coverage_ratio_delta": round(table_unit_coverage_ratio_delta, 4),
        "flags_added": sorted(set(current_flags) - set(previous_flags)),
        "flags_removed": sorted(set(previous_flags) - set(current_flags)),
        "quality_signal_codes_added": sorted(set(current_signal_codes) - set(previous_signal_codes)),
        "quality_signal_codes_removed": sorted(set(previous_signal_codes) - set(current_signal_codes)),
        "gap_unit_ids_added": sorted(set(current_gap_unit_ids) - set(previous_gap_unit_ids)),
        "gap_unit_ids_removed": sorted(set(previous_gap_unit_ids) - set(current_gap_unit_ids)),
        "previous_gap_unit_ids": previous_gap_unit_ids,
        "current_gap_unit_ids": current_gap_unit_ids,
        "previous_coverage_gap_pages": _part_gap_page_numbers(previous.get("coverage_gap_pages")),
        "current_coverage_gap_pages": _part_gap_page_numbers(current.get("coverage_gap_pages")),
    }


def _part_observation_selected_provider_id(value: Mapping[str, Any]) -> str | None:
    selected_provider_id = str(value.get("selected_provider_id") or "").strip()
    if selected_provider_id:
        return selected_provider_id
    routing = value.get("local_provider_routing")
    if isinstance(routing, Mapping):
        selected_provider_id = str(routing.get("selected_provider_id") or "").strip()
        if selected_provider_id:
            return selected_provider_id
    provider_ids = _string_list_payload(value.get("provider_ids"))
    return provider_ids[0] if provider_ids else None


def _part_summary_int(summary: Mapping[str, Any], key: str) -> int:
    return _safe_int(summary.get(key), default=0)


def _part_summary_float(summary: Mapping[str, Any], key: str) -> float:
    return _float_value(summary.get(key), default=1.0)


def _part_summary_string_list(summary: Mapping[str, Any], key: str) -> list[str]:
    return _string_list_payload(summary.get(key))


def _part_gap_page_numbers(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    page_numbers = [
        _safe_int(item.get("page_number"), default=0)
        for item in value
        if isinstance(item, Mapping)
    ]
    return sorted(page_number for page_number in page_numbers if page_number > 0)


def _part_gap_page_payload(page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "page_number": _safe_int(page.get("page_number"), default=0),
        "missing_reason": page.get("missing_reason"),
        "unit_ids": _string_list_payload(page.get("unit_ids")),
        "indexable_unit_ids": _string_list_payload(page.get("indexable_unit_ids")),
        "unchunked_unit_ids": _string_list_payload(page.get("unchunked_unit_ids")),
        "unembedded_unit_ids": _string_list_payload(page.get("unembedded_unit_ids")),
        "table_ids_without_units": _string_list_payload(page.get("table_ids_without_units")),
        "figure_ids_missing_caption": _string_list_payload(page.get("figure_ids_missing_caption")),
        "quality_signal_codes": _string_list_payload(page.get("quality_signal_codes")),
    }


def _part_unit_in_page_range(
    *,
    unit: Mapping[str, Any],
    page_start: int,
    page_end: int,
) -> bool:
    page_span = unit.get("page_span")
    if isinstance(page_span, Sequence) and len(page_span) >= 2:
        unit_start = _safe_int(page_span[0], default=0)
        unit_end = _safe_int(page_span[1], default=unit_start)
        if unit_start > 0 and unit_end > 0:
            return unit_start <= page_end and unit_end >= page_start
    return False


def _profile_for_document(
    *,
    job: Any,
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> str:
    return str(_profile_resolution_for_document(job=job, pages=pages, tables=tables)["resolved_profile"])


def _profile_resolution_for_document(
    *,
    job: Any,
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> dict[str, Any]:
    options = getattr(job, "options", {}) or {}
    file_name = None
    requested_profile = None
    if isinstance(options, dict):
        file_name = str(options.get("file_name") or "").strip() or None
        requested_profile = str(options.get("requested_profile") or options.get("profile") or options.get("parse_profile") or "").strip() or None
        if options.get("profile_source"):
            profile = str(options.get("profile") or "default")
            return _profile_resolution_payload(
                requested_profile=requested_profile,
                resolved={
                    "profile": profile,
                    "source": str(options.get("profile_source") or "auto"),
                    "reasons": list(options.get("profile_reasons") or []),
                    "recommended_async": bool(options.get("profile_recommended_async")),
                    "limits": dict(options.get("profile_limits") or {}),
                    "profile_known": bool(options.get("profile_known", True)),
                    "profile_warning": options.get("profile_warning"),
                },
            )
    media_type = str(getattr(job, "media_type", "") or "").lower()
    resolved = resolve_parse_profile(
        media_type=media_type,
        file_name=file_name,
        file_size_bytes=None,
        page_count=len(pages),
        table_count=len(tables),
        requested_profile=requested_profile,
    )
    return _profile_resolution_payload(
        requested_profile=requested_profile,
        resolved=resolved,
    )


def _profile_resolution_payload(
    *,
    requested_profile: str | None,
    resolved: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requested_profile": requested_profile or "auto",
        "resolved_profile": str(resolved.get("profile") or "default"),
        "source": str(resolved.get("source") or "auto"),
        "reasons": list(resolved.get("reasons") or []),
        "recommended_async": bool(resolved.get("recommended_async")),
        "limits": dict(resolved.get("limits") or {}),
        "profile_known": bool(resolved.get("profile_known", True)),
    }
    warning = resolved.get("profile_warning")
    if warning:
        payload["profile_warning"] = str(warning)
    return payload


def _common_table_col_count(tables: list[dict[str, Any]]) -> int:
    counts: dict[int, int] = {}
    for table in tables:
        cols = _safe_int(table.get("cols"), default=0)
        if cols <= 0:
            continue
        counts[cols] = counts.get(cols, 0) + 1
    if not counts:
        return 0
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _empty_cell_ratio(rows: list[list[str]]) -> float:
    total = 0
    empty = 0
    for row in rows:
        for value in row:
            total += 1
            if not str(value or "").strip():
                empty += 1
    if total <= 0:
        return 0.0
    return round(empty / total, 4)


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _state_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _project_pages(blocks: tuple[Block, ...]) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    page_signals: dict[int, dict[str, Any]] = {}
    # logical_page tracking for DOCX (always physical page==1, split by headings/breaks)
    logical_page_texts: dict[int, list[str]] = {}
    has_logical_pages = False
    for block in blocks:
        page_number = int(block.metadata.get("page", 1))
        # Collect logical_page info for DOCX (physical page is always 1).
        lp = block.metadata.get("logical_page")
        if isinstance(lp, int):
            has_logical_pages = True
            if block.content.strip() and block.type != BlockType.TITLE:
                logical_page_texts.setdefault(lp, []).append(block.content)
        signal = page_signals.setdefault(
            page_number,
            {
                "roles": [],
                "all_text": [],
                "has_title": False,
                "page_types": [],
                "cid_token_counts": [],
                "ocr_attempted": False,
                "ocr_fallback": False,
                "ocr_rejected": False,
                "ocr_attempt_reasons": set(),
                "ocr_acceptance_reasons": set(),
                "ocr_rejection_reasons": set(),
                "ocr_error_reasons": set(),
                "native_text_token_count": 0,
                "final_text_token_count": 0,
                "reading_order_confidences": [],
                "page_widths": [],
                "page_heights": [],
                "rotations": [],
                "source_kinds": [],
            },
        )
        role = str(block.metadata.get("semantic_role") or "paragraph")
        signal["roles"].append(role)
        page_type = block.metadata.get("page_type")
        if isinstance(page_type, str) and page_type:
            signal["page_types"].append(page_type)
        if block.content.strip():
            signal["all_text"].append(block.content)
        if block.type == BlockType.TITLE:
            signal["has_title"] = True
        cid_count = block.metadata.get("cid_token_count")
        if isinstance(cid_count, int) and cid_count > 0:
            signal["cid_token_counts"].append(cid_count)
        if bool(block.metadata.get("ocr_attempted")):
            signal["ocr_attempted"] = True
        if bool(block.metadata.get("ocr_fallback_used")):
            signal["ocr_fallback"] = True
        if bool(block.metadata.get("ocr_rejected")):
            signal["ocr_rejected"] = True
        attempt_reason = block.metadata.get("ocr_attempt_reason")
        if isinstance(attempt_reason, str) and attempt_reason:
            signal["ocr_attempt_reasons"].add(attempt_reason)
        acceptance_reason = block.metadata.get("ocr_acceptance_reason")
        if isinstance(acceptance_reason, str) and acceptance_reason:
            signal["ocr_acceptance_reasons"].add(acceptance_reason)
        rejection_reason = block.metadata.get("ocr_rejection_reason")
        if isinstance(rejection_reason, str) and rejection_reason:
            signal["ocr_rejection_reasons"].add(rejection_reason)
        error_reason = block.metadata.get("ocr_error_reason")
        if isinstance(error_reason, str) and error_reason:
            signal["ocr_error_reasons"].add(error_reason)
        native_tokens = block.metadata.get("native_text_token_count")
        if isinstance(native_tokens, int) and native_tokens >= 0:
            signal["native_text_token_count"] = max(int(signal["native_text_token_count"]), native_tokens)
        final_tokens = block.metadata.get("final_text_token_count")
        if isinstance(final_tokens, int) and final_tokens >= 0:
            signal["final_text_token_count"] = max(int(signal["final_text_token_count"]), final_tokens)
        reading_order_confidence = _optional_payload_float(
            block.metadata.get("reading_order_confidence", block.metadata.get("layout_reading_order_confidence"))
        )
        if reading_order_confidence is not None:
            signal["reading_order_confidences"].append(reading_order_confidence)
        page_width = block.metadata.get("page_width")
        if isinstance(page_width, (int, float)) and page_width > 0:
            signal["page_widths"].append(float(page_width))
        page_height = block.metadata.get("page_height")
        if isinstance(page_height, (int, float)) and page_height > 0:
            signal["page_heights"].append(float(page_height))
        rotation = block.metadata.get("rotation")
        if isinstance(rotation, (int, float)):
            signal["rotations"].append(int(rotation))
        source_kind = block.metadata.get("source_kind")
        if isinstance(source_kind, str) and source_kind.strip():
            signal["source_kinds"].append(source_kind.strip())

        if block.type == BlockType.TITLE:
            continue

        entry = pages.setdefault(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "tables": [],
                "artifacts": [],
                "image_descriptions": [],
                "confidence_parts": [],
            },
        )
        if role in _ARTIFACT_SEMANTIC_ROLES:
            artifact_entry: dict[str, Any] = {
                "text": block.content,
                "semantic_role": role,
            }
            bbox = block.metadata.get("bbox")
            if bbox is not None:
                artifact_entry["bbox"] = bbox
            source_kind = block.metadata.get("source_kind")
            if isinstance(source_kind, str) and source_kind:
                artifact_entry["source_kind"] = source_kind
            object_name = block.metadata.get("object_name")
            if isinstance(object_name, str) and object_name:
                artifact_entry["object_name"] = object_name
            caption_confidence = block.metadata.get("caption_confidence")
            if isinstance(caption_confidence, (int, float)):
                artifact_entry["caption_confidence"] = round(float(caption_confidence), 4)
            figure_kind = block.metadata.get("figure_kind")
            if isinstance(figure_kind, str) and figure_kind:
                artifact_entry["figure_kind"] = figure_kind
            page_width = block.metadata.get("page_width")
            if isinstance(page_width, (int, float)) and page_width > 0:
                artifact_entry["page_width"] = float(page_width)
            page_height = block.metadata.get("page_height")
            if isinstance(page_height, (int, float)) and page_height > 0:
                artifact_entry["page_height"] = float(page_height)
            entry["artifacts"].append(artifact_entry)
            if role == "image" and block.content.strip():
                entry["image_descriptions"].append(block.content.strip())
        elif block.type == BlockType.TABLE:
            if block.content.strip():
                entry["tables_markdown"].append(block.content)
                # B2: dual output – include raw cells alongside markdown.
                raw_cells = block.metadata.get("cells")
                table_entry: dict[str, Any] = {"markdown": block.content}
                if raw_cells:
                    table_entry["raw"] = raw_cells
                    table_entry["rows"] = block.metadata.get("rows", 0)
                    table_entry["cols"] = block.metadata.get("cols", 0)
                entry["tables"].append(table_entry)
        elif block.content.strip():
            entry["text_parts"].append(block.content)

        confidence = block.metadata.get("confidence")
        if isinstance(confidence, (int, float)):
            entry["confidence_parts"].append(float(confidence))

    total_pages = len(set(page_signals) | set(pages))
    ordered: list[dict[str, Any]] = []
    for page_number in sorted(set(page_signals) | set(pages)):
        entry = pages.get(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "tables": [],
                "artifacts": [],
                "image_descriptions": [],
                "confidence_parts": [],
            },
        )
        text = "\n\n".join(item for item in entry.pop("text_parts") if item.strip())
        confidences = entry.pop("confidence_parts")
        sig = page_signals.get(page_number, {})
        full_text = "\n\n".join(sig.get("all_text", []))

        # Parser-emitted page_types take priority; accumulate votes for
        # the remaining pages where the parser did not emit a type.
        explicit_types = [t for t in sig.get("page_types", []) if t and t != "body"]
        if explicit_types:
            page_type = explicit_types[0]
            page_type_confidence = "high"
        else:
            page_type, page_type_confidence = _infer_page_type_with_confidence(
                page_number=page_number,
                total_pages=total_pages,
                roles=sig.get("roles", []),
                full_text=full_text,
                has_title=bool(sig.get("has_title")),
                body_text=text,
            )

        page_entry: dict[str, Any] = {
            "page_number": page_number,
            "page_type": page_type,
            "page_type_confidence": page_type_confidence,
            "text": text,
            "tables_markdown": entry["tables_markdown"],
            "tables": entry["tables"],
            "artifacts": entry["artifacts"],
            "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 1.0,
        }
        image_descriptions = [
            description
            for description in entry.get("image_descriptions", [])
            if isinstance(description, str) and description.strip()
        ]
        if image_descriptions:
            page_entry["image_descriptions"] = image_descriptions
        cid_total = sum(sig.get("cid_token_counts", []))
        if cid_total > 0:
            page_entry["cid_token_count"] = cid_total
        if bool(sig.get("ocr_attempted")):
            page_entry["ocr_attempted"] = True
        if bool(sig.get("ocr_fallback")):
            page_entry["ocr_fallback"] = True
        if bool(sig.get("ocr_rejected")):
            page_entry["ocr_rejected"] = True
        attempt_reasons = sorted(sig.get("ocr_attempt_reasons", set()))
        if attempt_reasons:
            page_entry["ocr_attempt_reasons"] = attempt_reasons
        acceptance_reasons = sorted(sig.get("ocr_acceptance_reasons", set()))
        if acceptance_reasons:
            page_entry["ocr_acceptance_reasons"] = acceptance_reasons
        rejection_reasons = sorted(sig.get("ocr_rejection_reasons", set()))
        if rejection_reasons:
            page_entry["ocr_rejection_reasons"] = rejection_reasons
        error_reasons = sorted(sig.get("ocr_error_reasons", set()))
        if error_reasons:
            page_entry["ocr_error_reasons"] = error_reasons
        native_tokens = int(sig.get("native_text_token_count", 0) or 0)
        final_tokens = int(sig.get("final_text_token_count", 0) or 0)
        reading_order_confidences = [
            max(0.0, min(1.0, float(value)))
            for value in sig.get("reading_order_confidences", [])
            if isinstance(value, (int, float))
        ]
        if native_tokens > 0:
            page_entry["native_text_token_count"] = native_tokens
        if final_tokens > 0:
            page_entry["final_text_token_count"] = final_tokens
        if reading_order_confidences:
            page_entry["reading_order_confidence"] = round(
                sum(reading_order_confidences) / len(reading_order_confidences),
                4,
            )
        page_widths = [value for value in sig.get("page_widths", []) if isinstance(value, (int, float)) and value > 0]
        if page_widths:
            page_entry["page_width"] = round(float(page_widths[0]), 4)
        page_heights = [value for value in sig.get("page_heights", []) if isinstance(value, (int, float)) and value > 0]
        if page_heights:
            page_entry["page_height"] = round(float(page_heights[0]), 4)
        rotations = [value for value in sig.get("rotations", []) if isinstance(value, int)]
        if rotations:
            page_entry["rotation"] = rotations[0]
        source_kind = _page_source_kind(sig.get("source_kinds", []))
        if source_kind:
            page_entry["source_kind"] = source_kind
        ordered.append(page_entry)

    # For DOCX documents, attach a logical_pages summary alongside the physical pages.
    if has_logical_pages and logical_page_texts:
        for page_entry in ordered:
            logical_pages_list = [
                {
                    "logical_page_number": lp_idx,
                    "text": "\n\n".join(texts),
                }
                for lp_idx, texts in sorted(logical_page_texts.items())
            ]
            page_entry["logical_pages"] = logical_pages_list
            break  # only attach to the first (and only physical) page entry

    return ordered


# Strong token sets for page-type classification.
# Only fire signature if the page contains a dedicated signature block header
# (not a single mention of "签字" mid-paragraph).
_SIGNATURE_STRONG_TOKENS = frozenset(
    [
        "signature page",
        "signed by:",
        "approved by:",
        "审批人：",
        "审批人:",
        "签字栏",
        "signature block",
        "authorized signature",
    ]
)
# Minimum fraction of blocks whose role must be non-body for the page to be
# classified as a special type.  This prevents a single stray role from
# overriding the whole page.
_PAGE_TYPE_ROLE_THRESHOLD = 0.4


def _infer_page_type_with_confidence(
    *,
    page_number: int,
    total_pages: int,
    roles: list[str],
    full_text: str,
    has_title: bool,
    body_text: str,
) -> tuple[str, str]:
    """Return (page_type, confidence) where confidence is 'high'/'medium'/'low'."""
    n_blocks = max(len(roles), 1)
    normalized_text = full_text.lower()

    # --- TOC / LEP  (role-based, high confidence) ---
    toc_count = sum(1 for r in roles if r in ("toc_entry", "lep_entry"))
    if toc_count / n_blocks >= _PAGE_TYPE_ROLE_THRESHOLD:
        confidence = "high" if toc_count / n_blocks >= 0.7 else "medium"
        return "toc", confidence

    # --- Front matter (role-based, high confidence) ---
    fm_count = sum(1 for r in roles if r in ("front_matter", "revision_record", "distribution_list"))
    if fm_count / n_blocks >= _PAGE_TYPE_ROLE_THRESHOLD:
        confidence = "high" if fm_count / n_blocks >= 0.7 else "medium"
        return "front_matter", confidence

    # --- Signature: require a strong dedicated header, not casual keyword mention ---
    if any(token in normalized_text for token in _SIGNATURE_STRONG_TOKENS):
        return "signature", "high"

    # --- Appendix ---
    if any(token in normalized_text for token in ("appendix", "annex", "附录")):
        return "appendix", "medium"

    # --- Cover page (first page, title only, no body text) ---
    if page_number == 1 and has_title and not body_text.strip():
        return "cover", "high"

    return "body", "high"


def _page_source_kind(source_kinds: list[str]) -> str:
    normalized = [str(value).strip() for value in source_kinds if str(value).strip()]
    if not normalized:
        return ""
    preferred_order = (
        "native_text",
        "ocr_text",
        "structured_table",
        "pdf_image",
    )
    for candidate in preferred_order:
        if candidate in normalized:
            return candidate
    return normalized[0]


# Kept for backwards compatibility with any direct callers in tests.
def _infer_page_type(
    *,
    page_number: int,
    roles: list[str],
    full_text: str,
    has_title: bool,
    body_text: str,
) -> str:
    page_type, _ = _infer_page_type_with_confidence(
        page_number=page_number,
        total_pages=1,
        roles=roles,
        full_text=full_text,
        has_title=has_title,
        body_text=body_text,
    )
    return page_type


def _infer_parser_used(blocks: tuple[Block, ...]) -> str:
    parser_aliases = {
        "docx-native": "python-docx",
        "pdf-text": "pdf-text",
        "text-native": "text-native",
    }
    for block in blocks:
        layout_source = block.metadata.get("layout_source")
        if isinstance(layout_source, str) and layout_source:
            return layout_source
        parser_name = block.metadata.get("parser")
        if isinstance(parser_name, str) and parser_name:
            return parser_aliases.get(parser_name, parser_name)
    return "unknown"
