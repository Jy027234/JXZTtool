from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date
import re
from typing import Any, Iterable, Mapping

from .models import Block, BlockType, ParseOutcome
from .ocr_trace import build_ocr_decision_trace, ocr_decision_trace_payload
from .profiles import resolve_parse_profile
from .quality import ParseQualitySummary, evaluate_parse_quality, evaluate_projected_parse_quality


_ARTIFACT_SEMANTIC_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
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
    if normalized_projection not in {"compat", "structured", "full"}:
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
    )
    records = _structured_records(
        blocks=blocks,
        tables=tables,
        quality_signals=quality_signals,
        profile=profile,
        doc_id=doc_id,
    )
    quality_signals.extend(_record_quality_signals(records))
    structured_pages = _structured_pages(
        pages=pages,
        tables=tables,
        quality_signals=quality_signals,
    )
    payload: dict[str, Any] = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": normalized_projection,
        "doc_id": doc_id,
        "parse_run_id": str(getattr(job, "job_id", "") or ""),
        "profile": profile,
        "profile_resolution": profile_resolution,
        "state": _state_value(getattr(job, "state", None)),
        "compat_pages": pages,
        "pages": structured_pages,
        "tables": tables,
        "records_summary": _records_summary(records),
        "quality": _quality_payload(output_qs),
        "raw_quality": _quality_payload(raw_qs),
        "output_quality": _quality_payload(output_qs),
        "quality_signals": quality_signals,
        "quality_summary": _quality_signal_summary(quality_signals),
        "ocr_decision_trace": trace_payload,
        "parse_units": _parse_units(
            snapshot=snapshot,
            pages=pages,
            tables=tables,
            quality_signals=quality_signals,
        ),
        "index_manifest": snapshot.get("index_manifest"),
    }
    if normalized_projection == "full":
        payload["job"] = _to_payload(job)
        payload["blocks"] = _to_payload(blocks)
        payload["chunks"] = _to_payload(chunks)
    return payload


def _document_quality_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    structured = _document_projection(snapshot, projection="structured")
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": "quality",
        "doc_id": structured["doc_id"],
        "parse_run_id": structured["parse_run_id"],
        "profile": structured["profile"],
        "profile_resolution": structured["profile_resolution"],
        "state": structured["state"],
        "quality": structured["quality"],
        "raw_quality": structured["raw_quality"],
        "output_quality": structured["output_quality"],
        "quality_signals": structured["quality_signals"],
        "quality_summary": structured["quality_summary"],
        "ocr_decision_trace": structured["ocr_decision_trace"],
        "parse_units": structured["parse_units"],
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
    structured = _document_projection(snapshot, projection="structured")
    persisted_records = _persisted_records_from_snapshot(snapshot)
    records = persisted_records if persisted_records is not None else _records_from_snapshot(snapshot)
    filtered = _filter_records(
        records,
        query=query,
        table_id=table_id,
        quality_signal=quality_signal,
        field_filters=field_filters,
        page_start=page_start,
        page_end=page_end,
    )
    normalized_offset = max(0, int(offset or 0))
    if limit is None:
        items = filtered[normalized_offset:]
        normalized_limit = None
    else:
        normalized_limit = max(1, int(limit))
        items = filtered[normalized_offset: normalized_offset + normalized_limit]
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "projection": "records",
        "doc_id": structured["doc_id"],
        "parse_run_id": structured["parse_run_id"],
        "profile": structured["profile"],
        "profile_resolution": structured["profile_resolution"],
        "state": structured["state"],
        "total": len(filtered),
        "limit": normalized_limit,
        "offset": normalized_offset,
        "items": items,
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

    structured = _document_projection(snapshot, projection="structured")
    result: dict[str, list[dict[str, Any]]] = {}
    if "pages" in requested:
        pages = persisted.get("pages")
        if pages is None:
            pages = [dict(page) for page in structured.get("pages", []) if isinstance(page, dict)]
        result["pages"] = pages
    if "lines" in requested:
        lines = persisted.get("lines")
        if lines is None:
            lines = _structured_lines_from_blocks(
                tuple(snapshot.get("blocks") or ()),
                doc_id=str(structured.get("doc_id") or snapshot.get("doc_id") or ""),
                parse_run_id=str(structured.get("parse_run_id") or ""),
            )
        result["lines"] = lines
    if "records" in requested:
        records = persisted.get("records")
        if records is None:
            records = [dict(record) for record in _records_from_snapshot(snapshot)]
        result["records"] = records
    return result


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
        for line_index, text in enumerate(_block_lines(block.content), start=1):
            line_number = len(lines) + 1
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


def _records_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = tuple(snapshot.get("blocks") or ())
    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", ""))
    pages = _project_pages(blocks)
    tables = _structured_tables(blocks, doc_id=doc_id)
    profile_resolution = _profile_resolution_for_document(job=job, pages=pages, tables=tables)
    quality_signals = _quality_signals(pages=pages, tables=tables, blocks=blocks)
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


def _filter_records(
    records: list[dict[str, Any]],
    *,
    query: str | None,
    table_id: str | None,
    quality_signal: str | None,
    field_filters: Mapping[str, Any] | None,
    page_start: int | None,
    page_end: int | None,
) -> list[dict[str, Any]]:
    normalized_query = str(query or "").strip().lower()
    normalized_table_id = str(table_id or "").strip()
    normalized_quality_signal = str(quality_signal or "").strip()
    normalized_field_filters = _normalize_field_filters(field_filters)
    start = int(page_start) if page_start is not None else None
    end = int(page_end) if page_end is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError("invalid_page_range")

    filtered: list[dict[str, Any]] = []
    for record in records:
        if normalized_table_id and str(record.get("table_id") or "") != normalized_table_id:
            continue
        if normalized_quality_signal and normalized_quality_signal not in {
            str(code or "") for code in list(record.get("quality_signal_codes") or [])
        }:
            continue
        if normalized_field_filters and not _record_matches_field_filters(record, normalized_field_filters):
            continue
        record_start = _safe_int(record.get("page_start"), default=1)
        record_end = _safe_int(record.get("page_end"), default=record_start)
        if start is not None and record_end < start:
            continue
        if end is not None and record_start > end:
            continue
        if normalized_query:
            haystack = " ".join(
                [
                    str(record.get("record_id") or ""),
                    str(record.get("raw_text") or ""),
                    str(record.get("normalized_text") or ""),
                    _jsonish_text(record.get("fields")),
                ]
            ).lower()
            if normalized_query not in haystack:
                continue
        filtered.append(record)
    return filtered


def _normalize_field_filters(field_filters: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(field_filters, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_value in field_filters.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        normalized[key] = str(raw_value or "").strip()
    return normalized


def _record_matches_field_filters(record: dict[str, Any], field_filters: Mapping[str, str]) -> bool:
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


def _jsonish_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(item or "") for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(str(item or "") for item in value)
    return str(value or "")


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
            }
        )
    return structured


def _parse_units(
    *,
    snapshot: dict[str, Any],
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    quality_signals: list[dict[str, Any]],
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
            units.append(
                {
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
                    "quality_signal_count": _safe_int(part.get("quality_signal_count"), default=0),
                    "rerun_supported": bool(part.get("rerun_supported", False)),
                    "last_error": part.get("last_error"),
                }
            )
        if units:
            return units

    job = snapshot.get("job")
    doc_id = str(snapshot.get("doc_id") or getattr(job, "doc_id", ""))
    page_numbers = [_safe_int(page.get("page_number"), default=1) for page in pages]
    if not page_numbers:
        page_numbers = [1]
    return [
        {
            "parse_unit_id": f"{doc_id}:unit:1",
            "source_doc_id": doc_id,
            "part_doc_id": doc_id,
            "part_index": 1,
            "source_type": str(getattr(job, "media_type", "") or ""),
            "page_start": min(page_numbers),
            "page_end": max(page_numbers),
            "state": _state_value(getattr(job, "state", None)),
            "table_count": len(tables),
            "quality_signal_count": len(quality_signals),
        }
    ]


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
    logical_page_map: dict[int, set[int]] = {}  # logical_page_index -> {position indices}
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
                "confidence_parts": [],
            },
        )
        if role in _ARTIFACT_SEMANTIC_ROLES:
            entry["artifacts"].append({"text": block.content, "semantic_role": role})
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
        if native_tokens > 0:
            page_entry["native_text_token_count"] = native_tokens
        if final_tokens > 0:
            page_entry["final_text_token_count"] = final_tokens
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
    role_set = set(roles)
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
