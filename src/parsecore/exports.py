from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Literal


ExportDataset = Literal["tables", "quality_signals", "parse_units", "records"]
ExportFormat = Literal["jsonl", "csv", "tsv", "xlsx", "sqlite"]

EXPORT_DATASETS = {"tables", "quality_signals", "parse_units", "records"}
EXPORT_FORMATS = {"jsonl", "csv", "tsv", "xlsx", "sqlite"}


def export_structured_projection(
    payload: dict[str, Any],
    *,
    dataset: str,
    format: str,
    as_bytes: bool = False,
) -> dict[str, str | bytes]:
    """Export one structured projection dataset."""
    normalized_dataset = _normalize_dataset(dataset)
    normalized_format = _normalize_format(format)
    rows = _dataset_rows(payload, normalized_dataset)

    content = _serialize_rows(rows, dataset=normalized_dataset, format=normalized_format)
    if isinstance(content, bytes):
        exported_content: str | bytes = content
    elif as_bytes:
        exported_content = content.encode("utf-8")
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


def _serialize_rows(rows: list[dict[str, Any]], *, dataset: ExportDataset, format: ExportFormat) -> str | bytes:
    if format == "jsonl":
        return "\n".join(_json_dumps(row) for row in rows)
    if format == "xlsx":
        return _serialize_xlsx(rows, dataset=dataset)
    if format == "sqlite":
        return _serialize_sqlite(rows, dataset=dataset)

    delimiter = "\t" if format == "tsv" else ","
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_fieldnames(rows), delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _cell_value(row.get(field)) for field in writer.fieldnames or []})
    return output.getvalue()


def _serialize_xlsx(rows: list[dict[str, Any]], *, dataset: ExportDataset) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RuntimeError("xlsx export requires openpyxl") from exc

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = str(dataset)[:31] or "data"
    fieldnames = _fieldnames(rows)
    if fieldnames:
        worksheet.append(fieldnames)
        for row in rows:
            worksheet.append([_cell_value(row.get(field)) for field in fieldnames])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _serialize_sqlite(rows: list[dict[str, Any]], *, dataset: ExportDataset) -> bytes:
    fieldnames = _fieldnames(rows)
    table_name = _sqlite_identifier(str(dataset or "data"))
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as handle:
            temp_path = handle.name
        conn = sqlite3.connect(temp_path)
        try:
            conn.execute(
                f"CREATE TABLE {_quote_sqlite_identifier(table_name)} ({_sqlite_columns(fieldnames)})"
            )
            if fieldnames and rows:
                placeholders = ", ".join("?" for _ in fieldnames)
                columns = ", ".join(_quote_sqlite_identifier(field) for field in fieldnames)
                conn.executemany(
                    f"INSERT INTO {_quote_sqlite_identifier(table_name)} ({columns}) VALUES ({placeholders})",
                    [
                        tuple(_cell_value(row.get(field)) for field in fieldnames)
                        for row in rows
                    ],
                )
            conn.commit()
        finally:
            conn.close()
        return Path(temp_path).read_bytes()
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def _sqlite_columns(fieldnames: list[str]) -> str:
    if not fieldnames:
        return "_empty TEXT"
    return ", ".join(f"{_quote_sqlite_identifier(field)} TEXT" for field in fieldnames)


def _sqlite_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "data")).strip("_")
    if not normalized:
        normalized = "data"
    if normalized[0].isdigit():
        normalized = f"_{normalized}"
    return normalized


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


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
    if format == "xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if format == "sqlite":
        return "application/vnd.sqlite3"
    return "text/csv; charset=utf-8"


def _filename(payload: dict[str, Any], *, dataset: ExportDataset, format: ExportFormat) -> str:
    doc_id = str(payload.get("doc_id") or "document")
    safe_doc_id = re.sub(r"[^A-Za-z0-9._-]+", "-", doc_id).strip("-._") or "document"
    return f"{safe_doc_id}-{dataset}.{format}"


__all__ = ["export_structured_projection"]
