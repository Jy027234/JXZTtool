from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable


PART_STATE_FILTERS = {
    "pending",
    "parsing",
    "structuring",
    "embedding",
    "done",
    "failed",
    "cancelled",
    "warning",
    "partial",
    "unknown",
}


def document_parts_projection(
    payload: dict[str, Any],
    *,
    state_filter: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    """Project structured parse units into the public document-parts view."""
    filters = _normalize_state_filter(state_filter)
    parts = [
        _part_payload(payload=payload, unit=unit, fallback_index=index)
        for index, unit in enumerate(_parse_units(payload), start=1)
    ]
    filtered_parts = [part for part in parts if not filters or part["state"] in filters]
    state_counts = Counter(str(part["state"]) for part in parts)
    rerun_status_counts = Counter(
        str((part.get("diagnostics") or {}).get("rerun_status"))
        for part in parts
        if isinstance(part.get("diagnostics"), dict) and (part.get("diagnostics") or {}).get("rerun_compared")
    )
    active_parts = sum(
        state_counts.get(state, 0)
        for state in ("parsing", "structuring", "embedding")
    )

    return {
        "schema_version": payload.get("schema_version"),
        "projection": "parts",
        "doc_id": payload.get("doc_id"),
        "parse_run_id": payload.get("parse_run_id"),
        "profile": payload.get("profile"),
        "profile_resolution": payload.get("profile_resolution"),
        "state": payload.get("state"),
        "state_filter": sorted(filters) if filters else [],
        "parts": filtered_parts,
        "part_summary": {
            "total": len(parts),
            "filtered": len(filtered_parts),
            "partitioned": len(parts) > 1,
            "states": dict(sorted(state_counts.items())),
            "warning_parts": state_counts.get("warning", 0),
            "failed_parts": state_counts.get("failed", 0),
            "queued_parts": state_counts.get("pending", 0),
            "active_parts": active_parts,
            "cancelled_parts": state_counts.get("cancelled", 0),
            "coverage_gap_parts": sum(
                1
                for part in parts
                if _safe_int((part.get("coverage_summary") or {}).get("pages_with_coverage_gaps"), default=0) > 0
            ),
            "gap_unit_parts": sum(
                1
                for part in parts
                if _safe_int(part.get("coverage_gap_unit_count"), default=0) > 0
            ),
            "gap_unit_count": sum(_safe_int(part.get("coverage_gap_unit_count"), default=0) for part in parts),
            "unembedded_unit_count": sum(
                _safe_int((part.get("coverage_summary") or {}).get("unembedded_unit_count"), default=0)
                for part in parts
            ),
            "rerun_compared_parts": sum(
                1
                for part in parts
                if isinstance(part.get("diagnostics"), dict) and bool((part.get("diagnostics") or {}).get("rerun_compared"))
            ),
            "rerun_statuses": dict(sorted(rerun_status_counts.items())),
            "provider_changed_parts": sum(
                1
                for part in parts
                if isinstance(part.get("diagnostics"), dict) and bool((part.get("diagnostics") or {}).get("provider_changed"))
            ),
            "selected_provider_ids": sorted(
                {
                    str(part.get("selected_provider_id") or "")
                    for part in parts
                    if str(part.get("selected_provider_id") or "").strip()
                }
            ),
        },
    }


def _normalize_state_filter(state_filter: str | Iterable[str] | None) -> set[str]:
    if state_filter is None:
        return set()
    if isinstance(state_filter, str):
        values = [state_filter]
    else:
        values = [str(value) for value in state_filter]

    states: set[str] = set()
    for value in values:
        for item in re.split(r"[,|]", str(value)):
            state = item.strip().lower()
            if not state:
                continue
            if state not in PART_STATE_FILTERS:
                raise ValueError("invalid_part_state")
            states.add(state)
    return states


def _parse_units(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_units = payload.get("parse_units") or []
    if not isinstance(raw_units, list):
        raise ValueError("invalid_parts_payload")
    units: list[dict[str, Any]] = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            raise ValueError("invalid_parts_payload")
        units.append(unit)
    return units


def _part_payload(*, payload: dict[str, Any], unit: dict[str, Any], fallback_index: int) -> dict[str, Any]:
    doc_id = str(payload.get("doc_id") or unit.get("source_doc_id") or "")
    part_index = _safe_int(unit.get("part_index"), default=fallback_index)
    part_id = str(
        unit.get("part_id")
        or unit.get("parse_unit_id")
        or f"{doc_id}:part:{part_index}"
    )
    page_start = _safe_int(unit.get("page_start"), default=1)
    page_end = _safe_int(unit.get("page_end"), default=page_start)
    signals = _signals_for_range(payload, page_start=page_start, page_end=page_end)
    quality_signal_count = max(_safe_int(unit.get("quality_signal_count"), default=0), len(signals))
    raw_state = _state_value(unit.get("state"))
    state = _effective_part_state(raw_state=raw_state, quality_signal_count=quality_signal_count)

    rerun_supported = bool(unit.get("rerun_supported", False))
    quality_signal_codes = sorted(
        {
            str(signal.get("code"))
            for signal in signals
            if isinstance(signal, dict) and signal.get("code")
        }
    )
    quality_signal_page_numbers = sorted(
        {
            _safe_int(signal.get("page_number"), default=0)
            for signal in signals
            if isinstance(signal, dict) and _safe_int(signal.get("page_number"), default=0) > 0
        }
    )
    part = {
        "part_id": part_id,
        "parse_unit_id": str(unit.get("parse_unit_id") or part_id),
        "source_doc_id": str(unit.get("source_doc_id") or doc_id),
        "part_doc_id": str(unit.get("part_doc_id") or doc_id),
        "part_index": part_index,
        "source_type": str(unit.get("source_type") or ""),
        "page_start": page_start,
        "page_end": page_end,
        "page_range": {"start": page_start, "end": page_end},
        "state": state,
        "raw_state": raw_state,
        "table_count": _safe_int(unit.get("table_count"), default=0),
        "quality_signal_count": quality_signal_count,
        "quality_signal_codes": quality_signal_codes,
        "quality_signal_page_numbers": quality_signal_page_numbers,
        "severity_counts": _severity_counts(signals),
        "rerun_supported": rerun_supported,
        "provider_ids": _string_list(unit.get("provider_ids")),
        "action_suggestions": _part_action_suggestions(
            doc_id=doc_id,
            part_id=part_id,
            page_start=page_start,
            page_end=page_end,
            state=state,
            quality_signal_codes=quality_signal_codes,
            rerun_supported=rerun_supported,
            profile=str(payload.get("profile") or "").strip() or None,
            rerun_comparison=unit.get("rerun_comparison") if isinstance(unit.get("rerun_comparison"), dict) else None,
        ),
    }
    coverage_summary = unit.get("coverage_summary")
    if isinstance(coverage_summary, dict):
        part["coverage_summary"] = dict(coverage_summary)
        part["coverage_gap_count"] = _safe_int(coverage_summary.get("pages_with_coverage_gaps"), default=0)
        part["coverage_gap_unit_count"] = len(_string_list(coverage_summary.get("gap_unit_ids")))
    coverage_gap_pages = unit.get("coverage_gap_pages")
    if isinstance(coverage_gap_pages, list):
        part["coverage_gap_pages"] = [dict(page) for page in coverage_gap_pages if isinstance(page, dict)]
        if "coverage_gap_count" not in part:
            part["coverage_gap_count"] = len(part["coverage_gap_pages"])
    if "coverage_gap_unit_count" not in part:
        part["coverage_gap_unit_count"] = 0
    rag_coverage_quality = unit.get("rag_coverage_quality")
    if isinstance(rag_coverage_quality, dict):
        part["rag_coverage_quality"] = dict(rag_coverage_quality)
    previous_part_observation = unit.get("previous_part_observation")
    if isinstance(previous_part_observation, dict):
        part["previous_part_observation"] = dict(previous_part_observation)
    rerun_comparison = unit.get("rerun_comparison")
    if isinstance(rerun_comparison, dict):
        part["rerun_comparison"] = dict(rerun_comparison)
    route_plan = unit.get("provider_route_plan")
    if isinstance(route_plan, dict):
        part["provider_route_plan"] = dict(route_plan)
    local_provider_routing = unit.get("local_provider_routing")
    if isinstance(local_provider_routing, dict):
        part["local_provider_routing"] = dict(local_provider_routing)
        selected_provider_id = str(local_provider_routing.get("selected_provider_id") or "").strip()
        if selected_provider_id:
            part["selected_provider_id"] = selected_provider_id
        route_status = str(local_provider_routing.get("route_status") or "").strip()
        if route_status:
            part["route_status"] = route_status
    for optional_key in ("job_id", "attempts", "profile", "parser_options", "last_error"):
        if optional_key in unit:
            part[optional_key] = unit[optional_key]
    part["diagnostics"] = _part_diagnostics(part)
    return part


def _signals_for_range(
    payload: dict[str, Any],
    *,
    page_start: int,
    page_end: int,
) -> list[dict[str, Any]]:
    raw_signals = payload.get("quality_signals") or []
    if not isinstance(raw_signals, list):
        return []
    signals: list[dict[str, Any]] = []
    for signal in raw_signals:
        if not isinstance(signal, dict):
            continue
        page_number = _safe_int(signal.get("page_number"), default=0)
        if page_number and page_start <= page_number <= page_end:
            signals.append(signal)
    return signals


def _effective_part_state(*, raw_state: str, quality_signal_count: int) -> str:
    if raw_state == "failed":
        return "failed"
    if quality_signal_count > 0 and raw_state in {"", "done"}:
        return "warning"
    if raw_state in PART_STATE_FILTERS:
        return raw_state
    return "unknown"


def _severity_counts(signals: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(
        str(signal.get("severity") or "unknown")
        for signal in signals
        if isinstance(signal, dict)
    )
    return dict(sorted(counts.items()))


def _part_action_suggestions(
    *,
    doc_id: str,
    part_id: str,
    page_start: int,
    page_end: int,
    state: str,
    quality_signal_codes: list[str],
    rerun_supported: bool,
    profile: str | None,
    rerun_comparison: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if state not in {"warning", "failed"} and not quality_signal_codes:
        return []
    reason_codes = list(dict.fromkeys(quality_signal_codes))
    suggestions: list[dict[str, Any]] = []
    rerun_candidate = rerun_supported and (
        state == "failed"
        or "rag_empty_text_page" in reason_codes
        or "rag_table_without_unit" in reason_codes
        or "rag_figure_caption_missing" in reason_codes
        or "reading_order_low_confidence" in reason_codes
        or any(code.startswith("table_") for code in reason_codes)
    )
    route_plan_payload = _part_provider_route_plan_payload(
        profile=profile,
        reason_codes=reason_codes,
    )
    if rerun_candidate and rerun_comparison:
        suggestions.extend(
            _rerun_comparison_follow_up_suggestions(
                doc_id=doc_id,
                part_id=part_id,
                page_start=page_start,
                page_end=page_end,
                reason_codes=reason_codes,
                route_plan_payload=route_plan_payload,
                rerun_comparison=rerun_comparison,
            )
        )
    elif rerun_candidate:
        route_plan_payload = _part_provider_route_plan_payload(
            profile=profile,
            reason_codes=reason_codes,
        )
        suggestions.append(
            _action_suggestion(
                action_id="rerun_part",
                label="Rerun part",
                method="POST",
                endpoint=f"/v1/parse/documents/{doc_id}/parts/{part_id}/rerun",
                scope="part",
                reason_codes=reason_codes,
                payload={"provider_route_plan": route_plan_payload} if route_plan_payload else None,
            )
        )
    if "rag_units_without_chunks" in reason_codes:
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
    if "rag_chunks_not_embedded" in reason_codes:
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
    if reason_codes:
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


def _part_diagnostics(part: dict[str, Any]) -> dict[str, Any]:
    coverage_gap_count = _safe_int(part.get("coverage_gap_count"), default=0)
    gap_unit_count = _safe_int(part.get("coverage_gap_unit_count"), default=0)
    unembedded_unit_count = _safe_int((part.get("coverage_summary") or {}).get("unembedded_unit_count"), default=0)
    rag_coverage_quality = part.get("rag_coverage_quality")
    rag_gate = None
    if isinstance(rag_coverage_quality, dict):
        value = str(rag_coverage_quality.get("gate") or "").strip()
        rag_gate = value or None

    rerun_comparison = part.get("rerun_comparison")
    previous_selected_provider_id: str | None = None
    current_selected_provider_id: str | None = None
    rerun_status: str | None = None
    provider_changed = False
    improvement_axes: list[str] = []
    regression_axes: list[str] = []
    quality_signal_count_delta: int | None = None
    coverage_gap_delta: int | None = None
    gap_unit_count_delta: int | None = None

    if isinstance(rerun_comparison, dict):
        rerun_status_value = str(rerun_comparison.get("status") or "").strip()
        rerun_status = rerun_status_value or None
        previous_provider_value = str(rerun_comparison.get("previous_selected_provider_id") or "").strip()
        current_provider_value = str(rerun_comparison.get("current_selected_provider_id") or "").strip()
        previous_selected_provider_id = previous_provider_value or None
        current_selected_provider_id = current_provider_value or None
        provider_changed = bool(
            rerun_comparison.get("provider_changed")
            or (
                previous_selected_provider_id
                and current_selected_provider_id
                and previous_selected_provider_id != current_selected_provider_id
            )
        )
        improvement_axes = [str(item) for item in rerun_comparison.get("improvement_axes", []) if str(item)]
        regression_axes = [str(item) for item in rerun_comparison.get("regression_axes", []) if str(item)]
        if "quality_signal_count_delta" in rerun_comparison:
            quality_signal_count_delta = _safe_int(rerun_comparison.get("quality_signal_count_delta"), default=0)
        if "coverage_gap_delta" in rerun_comparison:
            coverage_gap_delta = _safe_int(rerun_comparison.get("coverage_gap_delta"), default=0)
        if "gap_unit_count_delta" in rerun_comparison:
            gap_unit_count_delta = _safe_int(rerun_comparison.get("gap_unit_count_delta"), default=0)

    if not current_selected_provider_id:
        current_provider_value = str(part.get("selected_provider_id") or "").strip()
        current_selected_provider_id = current_provider_value or None
    if not previous_selected_provider_id:
        previous_observation = part.get("previous_part_observation")
        if isinstance(previous_observation, dict):
            previous_provider_value = str(previous_observation.get("selected_provider_id") or "").strip()
            previous_selected_provider_id = previous_provider_value or None

    return {
        "has_coverage_gaps": coverage_gap_count > 0,
        "coverage_gap_count": coverage_gap_count,
        "gap_unit_count": gap_unit_count,
        "unembedded_unit_count": unembedded_unit_count,
        "rag_gate": rag_gate,
        "rerun_compared": isinstance(rerun_comparison, dict),
        "rerun_status": rerun_status,
        "provider_changed": provider_changed,
        "previous_selected_provider_id": previous_selected_provider_id,
        "current_selected_provider_id": current_selected_provider_id,
        "quality_signal_count_delta": quality_signal_count_delta,
        "coverage_gap_delta": coverage_gap_delta,
        "gap_unit_count_delta": gap_unit_count_delta,
        "improvement_axes": improvement_axes,
        "regression_axes": regression_axes,
        "recommended_focus": _part_recommended_focus(
            rerun_status=rerun_status,
            provider_changed=provider_changed,
            coverage_gap_count=coverage_gap_count,
            quality_signal_count=_safe_int(part.get("quality_signal_count"), default=0),
            rag_gate=rag_gate,
            rerun_supported=bool(part.get("rerun_supported")),
        ),
    }


def _part_recommended_focus(
    *,
    rerun_status: str | None,
    provider_changed: bool,
    coverage_gap_count: int,
    quality_signal_count: int,
    rag_gate: str | None,
    rerun_supported: bool,
) -> str | None:
    normalized_status = str(rerun_status or "").strip().lower()
    if normalized_status in {"unchanged", "provider_changed"}:
        return "provider_route_plan"
    if normalized_status in {"regressed", "mixed"}:
        return "parse_ir"
    if normalized_status == "improved":
        if coverage_gap_count > 0 or rag_gate == "accept_with_warning":
            return "coverage_gaps"
        return "keep_current_provider" if provider_changed else "quality_review"
    if rerun_supported and (coverage_gap_count > 0 or rag_gate == "local_rerun"):
        return "local_provider_rerun"
    if quality_signal_count > 0:
        return "quality_review"
    return None


def _rerun_comparison_follow_up_suggestions(
    *,
    doc_id: str,
    part_id: str,
    page_start: int,
    page_end: int,
    reason_codes: list[str],
    route_plan_payload: dict[str, Any] | None,
    rerun_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    status = str(rerun_comparison.get("status") or "").strip().lower()
    context = {
        "part_id": part_id,
        "page_range": {"start": page_start, "end": page_end},
        "rerun_comparison": dict(rerun_comparison),
    }
    suggestions: list[dict[str, Any]] = []

    if status in {"unchanged", "provider_changed"} and route_plan_payload:
        suggestions.append(
            _action_suggestion(
                action_id="inspect_provider_route_plan",
                label="Inspect local provider route plan",
                method="GET",
                endpoint="/v1/parse/providers/route-plan",
                scope="provider_route",
                reason_codes=reason_codes,
                params=route_plan_payload,
                context=context,
            )
        )
        suggestions.append(
            _action_suggestion(
                action_id="review_parse_ir",
                label="Review Parse IR",
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}",
                scope="document",
                reason_codes=reason_codes,
                params={"projection": "ir"},
                context=context,
            )
        )
        return suggestions

    if status in {"regressed", "mixed"}:
        suggestions.append(
            _action_suggestion(
                action_id="review_parse_ir",
                label="Review Parse IR",
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}",
                scope="document",
                reason_codes=reason_codes,
                params={"projection": "ir"},
                context=context,
            )
        )
        if route_plan_payload:
            suggestions.append(
                _action_suggestion(
                    action_id="inspect_provider_route_plan",
                    label="Inspect local provider route plan",
                    method="GET",
                    endpoint="/v1/parse/providers/route-plan",
                    scope="provider_route",
                    reason_codes=reason_codes,
                    params=route_plan_payload,
                    context=context,
                )
            )
        return suggestions

    if status == "improved":
        suggestions.append(
            _action_suggestion(
                action_id="review_parse_ir",
                label="Review Parse IR",
                method="GET",
                endpoint=f"/v1/parse/documents/{doc_id}",
                scope="document",
                reason_codes=reason_codes,
                params={"projection": "ir"},
                context=context,
            )
        )
        if route_plan_payload:
            suggestions.append(
                _action_suggestion(
                    action_id="inspect_provider_route_plan",
                    label="Inspect local provider route plan",
                    method="GET",
                    endpoint="/v1/parse/providers/route-plan",
                    scope="provider_route",
                    reason_codes=reason_codes,
                    params=route_plan_payload,
                    context=context,
                )
            )
        return suggestions

    return []


def _action_suggestion(
    *,
    action_id: str,
    label: str,
    method: str,
    endpoint: str,
    scope: str,
    reason_codes: list[str],
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    suggestion = {
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


def _part_provider_route_plan_payload(
    *,
    profile: str | None,
    reason_codes: list[str],
) -> dict[str, Any] | None:
    required_capabilities = _required_provider_capabilities_for_reason_codes(reason_codes)
    payload: dict[str, Any] = {}
    if profile:
        payload["profile"] = profile
    if required_capabilities:
        payload["required_capabilities"] = required_capabilities
    return payload or None


def _required_provider_capabilities_for_reason_codes(reason_codes: list[str]) -> list[str]:
    capabilities: list[str] = []
    reasons = {str(code) for code in reason_codes if str(code)}
    if reasons & {"rag_table_without_unit", "table_unit_coverage_below_threshold"}:
        capabilities.append("tables")
    if "rag_figure_caption_missing" in reasons:
        capabilities.extend(["layout", "figures"])
    if reasons & {"reading_order_low_confidence", "reading_order_confidence_below_threshold"}:
        capabilities.append("layout")
    if reasons & {"rag_empty_text_page", "text_page_coverage_below_threshold", "ocr_failed_page"}:
        capabilities.extend(["native-text", "local-ocr-fallback"])
    if any(code.startswith("table_") for code in reasons):
        capabilities.append("tables")
    return list(dict.fromkeys(capabilities))


def _state_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().lower()


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


# ---------------------------------------------------------------------------
# Part / export artifact cleanup (P5-T10)
# ---------------------------------------------------------------------------


_ARTIFACT_KINDS = ("part_pdf", "export_package", "comparison_report")


def list_artifact_candidates(
    artifact_root: Any,
    *,
    retention_seconds: int,
    kind: str = "part_pdf",
) -> list[dict[str, Any]]:
    """Return a list of artifact files older than *retention_seconds*.

    Each entry is ``{"path": str, "age_seconds": int, "kind": str}``.
    When *artifact_root* is ``None`` or not a directory the result is empty.
    """
    from pathlib import Path as _Path
    import time as _time

    if artifact_root is None:
        return []
    root = _Path(artifact_root)
    if not root.is_dir():
        return []
    now = _time.time()
    candidates: list[dict[str, Any]] = []
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        try:
            age = int(now - entry.stat().st_mtime)
        except OSError:
            continue
        if age > retention_seconds:
            candidates.append({"path": str(entry), "age_seconds": age, "kind": kind})
    return candidates


def cleanup_artifacts(
    artifact_root: Any,
    *,
    retention_seconds: int,
    kind: str = "part_pdf",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove (or list) artifact files older than *retention_seconds*.

    Returns a report payload::

        {
            "schema_version": "2026-06-artifact-cleanup",
            "kind": <kind>,
            "dry_run": <bool>,
            "artifact_root": <root>,
            "retention_seconds": <int>,
            "candidates": <count>,
            "removed": <count>,
            "errors": <count>,
            "files": [{"path": ..., "age_seconds": ..., "action": "remove"|"skip"}, ...]
        }
    """
    from pathlib import Path as _Path

    candidates = list_artifact_candidates(
        artifact_root, retention_seconds=retention_seconds, kind=kind
    )
    removed = 0
    errors = 0
    file_reports: list[dict[str, Any]] = []
    for candidate in candidates:
        path = candidate["path"]
        if dry_run:
            file_reports.append({**candidate, "action": "skip"})
            continue
        try:
            _Path(path).unlink()
            removed += 1
            file_reports.append({**candidate, "action": "remove"})
        except OSError:
            errors += 1
            file_reports.append({**candidate, "action": "error"})
    return {
        "schema_version": "2026-06-artifact-cleanup",
        "kind": kind,
        "dry_run": dry_run,
        "artifact_root": str(artifact_root) if artifact_root is not None else None,
        "retention_seconds": retention_seconds,
        "candidates": len(candidates),
        "removed": removed,
        "errors": errors,
        "files": file_reports,
    }


__all__ = [
    "PART_STATE_FILTERS",
    "document_parts_projection",
    "list_artifact_candidates",
    "cleanup_artifacts",
    "_ARTIFACT_KINDS",
]
