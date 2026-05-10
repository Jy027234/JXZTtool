from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .exports import write_structured_projection


DEFAULT_EXPORTS: tuple[tuple[str, str], ...] = (
    ("tables", "csv"),
    ("quality_signals", "jsonl"),
    ("parse_units", "tsv"),
)


def create_export_package(
    payload: dict[str, Any],
    output_dir: str | Path,
    *,
    formats: dict[str, str] | None = None,
    includes: list[str] | tuple[str, ...] | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an isolated export package and return its manifest."""
    export_id = f"exp_{uuid4().hex}"
    package_dir = _export_package_dir(output_dir, export_id)
    package_dir.mkdir(parents=True, exist_ok=False)

    filtered_payload = _filtered_payload(payload, filters or {})
    requested_specs = _export_specs(includes=includes, formats=formats)
    manifest: dict[str, Any] = {
        "manifest_schema_version": "2026-05",
        "export_id": export_id,
        "doc_id": payload.get("doc_id"),
        "tenant_id": payload.get("tenant_id"),
        "schema_version": payload.get("schema_version"),
        "parse_run_id": payload.get("parse_run_id"),
        "profile": payload.get("profile"),
        "profile_resolution": payload.get("profile_resolution"),
        "state": "done",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "request": {
            "include": [dataset for dataset, _format in requested_specs],
            "formats": {dataset: format_name for dataset, format_name in requested_specs},
            "filters": filters or {},
        },
        "files": [],
    }

    for dataset, format_name in requested_specs:
        filename = f"{dataset}.{format_name}"
        path = export_file_path(output_dir, export_id, filename)
        exported = write_structured_projection(
            filtered_payload,
            dataset=dataset,
            format=format_name,
            path=path,
        )
        manifest["files"].append(
            {
                "dataset": dataset,
                "format": format_name,
                "path": filename,
                "content_type": exported["content_type"],
                "bytes": exported["bytes"],
                "records": _record_count(filtered_payload.get(dataset)),
            }
        )

    manifest_path = export_file_path(output_dir, export_id, "manifest.json")
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    return manifest


def load_export_manifest(output_dir: str | Path, export_id: str) -> dict[str, Any]:
    manifest_path = export_file_path(output_dir, export_id, "manifest.json")
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("invalid_export_manifest")
    return manifest


def export_file_path(output_dir: str | Path, export_id: str, filename: str) -> Path:
    package_dir = _export_package_dir(output_dir, export_id)
    file_name = Path(str(filename))
    if file_name.is_absolute() or file_name.name != str(filename) or str(filename) in {"", ".", ".."}:
        raise ValueError("invalid_export_filename")
    candidate = package_dir / str(filename)
    resolved = candidate.resolve()
    if resolved != package_dir and package_dir not in resolved.parents:
        raise ValueError("invalid_export_path")
    return resolved


def _export_package_dir(output_dir: str | Path, export_id: str) -> Path:
    root = Path(output_dir).expanduser().resolve()
    export_name = Path(str(export_id))
    if export_name.is_absolute() or export_name.name != str(export_id) or str(export_id) in {"", ".", ".."}:
        raise ValueError("invalid_export_id")
    return (root / str(export_id)).resolve()


def _export_specs(
    *,
    includes: list[str] | tuple[str, ...] | None,
    formats: dict[str, str] | None,
) -> list[tuple[str, str]]:
    if includes is None:
        datasets = [dataset for dataset, _format in DEFAULT_EXPORTS]
    else:
        datasets = [str(dataset) for dataset in includes]

    default_formats = dict(DEFAULT_EXPORTS)
    configured_formats = formats or {}
    return [(dataset, str(configured_formats.get(dataset, default_formats.get(dataset, "jsonl")))) for dataset in datasets]


def _filtered_payload(payload: dict[str, Any], filters: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(payload)
    severity_filter = _severity_filter(filters.get("severity"))
    quality_signal_filter = _string_filter(filters.get("quality_signal") or filters.get("quality_signals"))
    field_filters = _field_filters(filters)
    page_range = _page_range(filters.get("page_range"))

    for dataset in ("pages", "lines", "tables", "quality_signals", "parse_units", "records"):
        rows = filtered.get(dataset)
        if not isinstance(rows, list):
            continue
        kept = rows
        if dataset == "quality_signals" and severity_filter is not None:
            kept = [
                row
                for row in kept
                if not isinstance(row, dict) or str(row.get("severity")) in severity_filter
            ]
        if dataset == "quality_signals" and quality_signal_filter is not None:
            kept = [
                row
                for row in kept
                if not isinstance(row, dict) or str(row.get("code")) in quality_signal_filter
            ]
        if dataset == "records" and quality_signal_filter is not None:
            kept = [
                row
                for row in kept
                if not isinstance(row, dict) or _record_has_quality_signal(row, quality_signal_filter)
            ]
        if dataset == "records" and field_filters:
            kept = [
                row
                for row in kept
                if not isinstance(row, dict) or _record_matches_fields(row, field_filters)
            ]
        if page_range is not None:
            kept = [
                row
                for row in kept
                if not isinstance(row, dict) or _row_overlaps_page_range(row, page_range)
            ]
        filtered[dataset] = kept

    return filtered


def _severity_filter(raw: Any) -> set[str] | None:
    return _string_filter(raw)


def _string_filter(raw: Any) -> set[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, (list, tuple, set)):
        return {str(value) for value in raw}
    return {str(raw)}


def _field_filters(filters: dict[str, Any]) -> dict[str, str]:
    raw = filters.get("fields")
    if raw is None:
        raw = filters.get("field_filters")
    normalized: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            field_name = str(key or "").strip()
            if field_name:
                normalized[field_name] = str(value or "").strip()
    field_name = str(filters.get("field") or filters.get("field_name") or "").strip()
    if field_name:
        normalized[field_name] = str(filters.get("value") or "").strip()
    return normalized


def _record_has_quality_signal(row: dict[str, Any], allowed: set[str]) -> bool:
    return bool({str(code or "") for code in list(row.get("quality_signal_codes") or [])} & allowed)


def _record_matches_fields(row: dict[str, Any], field_filters: dict[str, str]) -> bool:
    fields = row.get("fields")
    if not isinstance(fields, dict):
        return False
    for field_name, expected in field_filters.items():
        if field_name not in fields:
            return False
        value = fields.get(field_name)
        if expected and expected.lower() not in _field_text(value).lower():
            return False
        if not expected and not _field_text(value).strip():
            return False
    return True


def _field_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(item or "") for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(str(item or "") for item in value)
    return str(value or "")


def _page_range(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("invalid_export_filter")
    start = int(raw.get("start", 1))
    end = int(raw.get("end", start))
    if start > end:
        raise ValueError("invalid_export_filter")
    return start, end


def _row_overlaps_page_range(row: dict[str, Any], page_range: tuple[int, int]) -> bool:
    range_start, range_end = page_range
    if row.get("page_number") is not None:
        page_number = int(row["page_number"])
        return range_start <= page_number <= range_end

    page_start = row.get("page_start")
    page_end = row.get("page_end")
    if page_start is None and page_end is None:
        return True
    if page_start is None:
        page_start = page_end
    if page_end is None:
        page_end = page_start
    return int(page_start) <= range_end and int(page_end) >= range_start


def _record_count(rows: Any) -> int:
    return len(rows) if isinstance(rows, list) else 0


__all__ = ["create_export_package", "load_export_manifest", "export_file_path"]
