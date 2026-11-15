"""Build the Reader view from document IR and optional source blocks."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Mapping, Sequence

from .models import Block, BlockType
from .payload_utils import _optional_payload_float, _safe_int, _string_list_payload

READER_SCHEMA_VERSION = "2026-07-reader"


def _document_reader_projection(
    ir: Mapping[str, Any],
    *,
    source_blocks: Sequence[Block] = (),
) -> dict[str, Any]:
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

    source_positions = _reader_pdf_text_mixed_positions(source_blocks)
    for block in ir_blocks:
        source_position = source_positions.get(str(block.get("block_id") or ""))
        if source_position is not None:
            # Keep IR's reading_order unchanged because it is part of the
            # public IR identity; this override exists only in the Reader view.
            block["reading_order"] = source_position

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


def _reader_pdf_text_mixed_positions(source_blocks: Sequence[Block]) -> dict[str, int]:
    blocks_by_page: dict[int, list[Block]] = {}
    for block in source_blocks:
        metadata = block.metadata or {}
        page_number = _safe_int(metadata.get("page", metadata.get("page_number")), default=1)
        blocks_by_page.setdefault(page_number, []).append(block)

    positions: dict[str, int] = {}
    for page_blocks in blocks_by_page.values():
        content_blocks = [
            block
            for block in page_blocks
            if block.type in {BlockType.PARAGRAPH, BlockType.TABLE, BlockType.IMAGE}
        ]
        content_types = {block.type for block in content_blocks}
        if not {BlockType.PARAGRAPH, BlockType.IMAGE} <= content_types:
            continue
        # Only reconcile the known pdf-text mixed-page case when every content
        # block uses the same provider and has a source position. An explicit
        # reading_order anywhere on the page keeps its existing coordinate
        # system for the whole page.
        if any("reading_order" in (block.metadata or {}) for block in content_blocks):
            continue
        if any(
            str((block.metadata or {}).get("provider_id") or (block.metadata or {}).get("parser") or "")
            .strip()
            .lower()
            != "pdf-text"
            for block in content_blocks
        ):
            continue
        page_positions = [
            (block, (block.metadata or {}).get("position"))
            for block in content_blocks
        ]
        if any(
            not isinstance(position, int) or isinstance(position, bool) or position < 1
            for _, position in page_positions
        ):
            continue
        positions.update(
            (str(block.block_id), position)
            for block, position in page_positions
            if block.block_id
        )
    return positions


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
