from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Literal


ExportDataset = Literal["tables", "quality_signals", "parse_units"]
ExportFormat = Literal["jsonl", "csv", "tsv"]

EXPORT_DATASETS = {"tables", "quality_signals", "parse_units"}
EXPORT_FORMATS = {"jsonl", "csv", "tsv"}


def export_structured_projection(
    payload: dict[str, Any],
    *,
    dataset: str,
    format: str,
    as_bytes: bool = False,
) -> dict[str, str | bytes]:
    """Export one structured projection dataset as jsonl/csv/tsv content."""
    normalized_dataset = _normalize_dataset(dataset)
    normalized_format = _normalize_format(format)
    rows = _dataset_rows(payload, normalized_dataset)

    content = _serialize_rows(rows, format=normalized_format)
    if as_bytes:
        exported_content: str | bytes = content.encode("utf-8")
    else:
        exported_content = content

    return {
        "content_type": _content_type(normalized_format),
        "filename": _filename(payload, dataset=normalized_dataset, format=normalized_format),
        "content": exported_content,
    }


def _normalize_dataset(dataset: str) -> ExportDataset:
    normalized = str(dataset or "").strip().lower()
    if normalized not in EXPORT_DATASETS:
        raise ValueError("invalid_export_dataset")
    return normalized  # type: ignore[return-value]


def _normalize_format(format: str) -> ExportFormat:
    normalized = str(format or "").strip().lower()
    if normalized not in EXPORT_FORMATS:
        raise ValueError("invalid_export_format")
    return normalized  # type: ignore[return-value]


def _dataset_rows(payload: dict[str, Any], dataset: ExportDataset) -> list[dict[str, Any]]:
    rows = payload.get(dataset)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("invalid_export_dataset_payload")

    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_export_dataset_payload")
        normalized_rows.append(row)
    return normalized_rows


def _serialize_rows(rows: list[dict[str, Any]], *, format: ExportFormat) -> str:
    if format == "jsonl":
        return "\n".join(_json_dumps(row) for row in rows)

    delimiter = "\t" if format == "tsv" else ","
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_fieldnames(rows), delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _cell_value(row.get(field)) for field in writer.fieldnames or []})
    return output.getvalue()


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            field = str(key)
            if field in seen:
                continue
            seen.add(field)
            fields.append(field)
    return fields


def _cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return _json_dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_type(format: ExportFormat) -> str:
    if format == "jsonl":
        return "application/x-ndjson; charset=utf-8"
    if format == "tsv":
        return "text/tab-separated-values; charset=utf-8"
    return "text/csv; charset=utf-8"


def _filename(payload: dict[str, Any], *, dataset: ExportDataset, format: ExportFormat) -> str:
    doc_id = str(payload.get("doc_id") or "document")
    safe_doc_id = re.sub(r"[^A-Za-z0-9._-]+", "-", doc_id).strip("-._") or "document"
    return f"{safe_doc_id}-{dataset}.{format}"


__all__ = ["export_structured_projection"]
