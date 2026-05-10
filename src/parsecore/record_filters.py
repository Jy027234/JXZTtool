from __future__ import annotations

import json
from typing import Any, Iterable, Mapping


def filter_records(
    records: Iterable[Mapping[str, Any]],
    *,
    query: str | None = None,
    table_id: str | None = None,
    quality_signal: str | None = None,
    field_filters: Mapping[str, Any] | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[dict[str, Any]]:
    start = int(page_start) if page_start is not None else None
    end = int(page_end) if page_end is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("invalid_page_range")
    normalized_fields = normalize_field_filters(field_filters)
    return [
        dict(record)
        for record in records
        if record_matches(
            record,
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=normalized_fields,
            page_start=start,
            page_end=end,
        )
    ]


def collect_record_page(
    records: Iterable[Mapping[str, Any]],
    *,
    limit: int | None,
    offset: int = 0,
    query: str | None = None,
    table_id: str | None = None,
    quality_signal: str | None = None,
    field_filters: Mapping[str, Any] | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    normalized_offset = max(0, int(offset or 0))
    normalized_limit = None if limit is None else max(1, int(limit))
    start = int(page_start) if page_start is not None else None
    end = int(page_end) if page_end is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("invalid_page_range")
    normalized_fields = normalize_field_filters(field_filters)
    total = 0
    items: list[dict[str, Any]] = []
    for record in records:
        if not record_matches(
            record,
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=normalized_fields,
            page_start=start,
            page_end=end,
        ):
            continue
        if total >= normalized_offset and (normalized_limit is None or len(items) < normalized_limit):
            items.append(dict(record))
        total += 1
    return {
        "total": total,
        "limit": normalized_limit,
        "offset": normalized_offset,
        "items": items,
    }


def collect_record_query(
    records: Iterable[Mapping[str, Any]],
    *,
    limit: int | None,
    offset: int = 0,
    query: str | None = None,
    table_id: str | None = None,
    quality_signal: str | None = None,
    field_filters: Mapping[str, Any] | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    return collect_record_page(
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


def record_matches(
    record: Mapping[str, Any],
    *,
    query: str | None = None,
    table_id: str | None = None,
    quality_signal: str | None = None,
    field_filters: Mapping[str, Any] | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> bool:
    normalized_query = str(query or "").strip().lower()
    normalized_table_id = str(table_id or "").strip()
    normalized_quality_signal = str(quality_signal or "").strip()
    if normalized_query and normalized_query not in _record_search_text(record).lower():
        return False
    if normalized_table_id and str(record.get("table_id") or "") != normalized_table_id:
        return False
    if normalized_quality_signal and normalized_quality_signal not in {
        str(code or "") for code in list(record.get("quality_signal_codes") or [])
    }:
        return False
    normalized_fields = normalize_field_filters(field_filters)
    if normalized_fields and not _record_matches_field_filters(record, normalized_fields):
        return False
    record_start = _safe_int(record.get("page_start"), default=1)
    record_end = _safe_int(record.get("page_end"), default=record_start)
    if page_start is not None and record_end < page_start:
        return False
    if page_end is not None and record_start > page_end:
        return False
    return True


def normalize_field_filters(field_filters: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(field_filters, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_value in field_filters.items():
        key = str(raw_key or "").strip()
        if key:
            normalized[key] = str(raw_value or "").strip()
    return normalized


def _record_matches_field_filters(record: Mapping[str, Any], field_filters: Mapping[str, str]) -> bool:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return False
    for field_name, expected in field_filters.items():
        if field_name not in fields:
            return False
        value = fields.get(field_name)
        if expected and expected.lower() not in _field_value_text(value).lower():
            return False
        if not expected and not _field_value_text(value).strip():
            return False
    return True


def _field_value_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return _jsonish_text(value)
    return str(value or "")


def _record_search_text(record: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(record.get("record_id") or ""),
            str(record.get("raw_text") or ""),
            str(record.get("normalized_text") or ""),
            _jsonish_text(record.get("fields")),
            _jsonish_text(record),
        ]
    )


def _jsonish_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_jsonish_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_jsonish_text(item) for item in value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
