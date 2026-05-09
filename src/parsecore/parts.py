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
        "quality_signal_codes": sorted(
            {
                str(signal.get("code"))
                for signal in signals
                if isinstance(signal, dict) and signal.get("code")
            }
        ),
        "severity_counts": _severity_counts(signals),
        "rerun_supported": rerun_supported,
    }
    for optional_key in ("job_id", "attempts", "profile", "parser_options", "last_error"):
        if optional_key in unit:
            part[optional_key] = unit[optional_key]
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


def _state_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().lower()


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["PART_STATE_FILTERS", "document_parts_projection"]
