from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import Block, BlockType, Chunk


IR_SCHEMA_VERSION = "2026-06-ir"
COVERAGE_SCHEMA_VERSION = "2026-06-coverage"

_SKIP_INDEX_ROLES = {
    "header_footer",
    "parse_artifact",
    "page_ref_cell",
    "version_cell",
}


def build_ir_projection(
    *,
    snapshot: Mapping[str, Any],
    doc_id: str,
    parse_run_id: str,
    profile: str,
    profile_resolution: Mapping[str, Any],
    state: str,
    blocks: Sequence[Block],
    chunks: Sequence[Chunk],
    pages: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
    quality: Mapping[str, Any],
    raw_quality: Mapping[str, Any],
    output_quality: Mapping[str, Any],
    quality_signals: Sequence[Mapping[str, Any]],
    ocr_decision_trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the stable Parse IR projection from existing ParseCore models."""

    ir_blocks = [_ir_block(block, index=index) for index, block in enumerate(blocks, start=1)]
    ir_tables = _ir_tables(tables=tables, blocks=blocks)
    figures = _ir_figures(blocks)
    knowledge_units = _knowledge_units(
        doc_id=doc_id,
        ir_blocks=ir_blocks,
        ir_tables=ir_tables,
        chunks=chunks,
        index_manifest=snapshot.get("index_manifest"),
    )
    coverage = build_coverage_payload(
        doc_id=doc_id,
        parse_run_id=parse_run_id,
        profile=profile,
        state=state,
        blocks=blocks,
        chunks=chunks,
        pages=pages,
        ir_blocks=ir_blocks,
        tables=ir_tables,
        figures=figures,
        knowledge_units=knowledge_units,
        quality_signals=quality_signals,
        index_manifest=snapshot.get("index_manifest"),
    )
    return {
        "schema_version": IR_SCHEMA_VERSION,
        "projection": "ir",
        "doc_id": doc_id,
        "parse_run_id": parse_run_id,
        "profile": profile,
        "profile_resolution": dict(profile_resolution),
        "state": state,
        "provider_registry": _normalize_provider_registry(snapshot.get("provider_registry")),
        "local_provider_routing": _local_provider_routing_decision(snapshot),
        "providers": _providers(ir_blocks),
        "pages": _ir_pages(pages=pages, ir_blocks=ir_blocks),
        "blocks": ir_blocks,
        "tables": ir_tables,
        "figures": figures,
        "knowledge_units": (coverage.get("coverage") or {}).get("units") or knowledge_units,
        "quality": dict(quality),
        "raw_quality": dict(raw_quality),
        "output_quality": dict(output_quality),
        "quality_signals": [dict(signal) for signal in quality_signals],
        "coverage": coverage["coverage"],
        "coverage_quality_signals": coverage["quality_signals"],
        "rag_coverage_quality": coverage["rag_coverage_quality"],
        "ocr_decision_trace": dict(ocr_decision_trace),
        "index_manifest": coverage["index_manifest"],
    }


def build_coverage_projection(
    *,
    snapshot: Mapping[str, Any],
    doc_id: str,
    parse_run_id: str,
    profile: str,
    state: str,
    blocks: Sequence[Block],
    chunks: Sequence[Chunk],
    pages: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
    quality_signals: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a compact RAG/readability coverage projection."""

    ir_blocks = [_ir_block(block, index=index) for index, block in enumerate(blocks, start=1)]
    ir_tables = _ir_tables(tables=tables, blocks=blocks)
    figures = _ir_figures(blocks)
    knowledge_units = _knowledge_units(
        doc_id=doc_id,
        ir_blocks=ir_blocks,
        ir_tables=ir_tables,
        chunks=chunks,
        index_manifest=snapshot.get("index_manifest"),
    )
    payload = build_coverage_payload(
        doc_id=doc_id,
        parse_run_id=parse_run_id,
        profile=profile,
        state=state,
        blocks=blocks,
        chunks=chunks,
        pages=pages,
        ir_blocks=ir_blocks,
        tables=ir_tables,
        figures=figures,
        knowledge_units=knowledge_units,
        quality_signals=quality_signals,
        index_manifest=snapshot.get("index_manifest"),
    )
    payload["local_provider_routing"] = _local_provider_routing_decision(snapshot)
    return payload


def build_coverage_payload(
    *,
    doc_id: str,
    parse_run_id: str,
    profile: str,
    state: str,
    blocks: Sequence[Block],
    chunks: Sequence[Chunk],
    pages: Sequence[Mapping[str, Any]],
    ir_blocks: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    knowledge_units: Sequence[Mapping[str, Any]],
    quality_signals: Sequence[Mapping[str, Any]],
    index_manifest: Any,
) -> dict[str, Any]:
    page_numbers = sorted(
        {
            _safe_int(page.get("page_number"), default=1)
            for page in pages
            if isinstance(page, Mapping)
        }
        | {_page_number(block.metadata) for block in blocks}
    )
    pages_by_number = {
        _safe_int(page.get("page_number"), default=1): page
        for page in pages
        if isinstance(page, Mapping)
    }
    if not page_numbers:
        page_numbers = [1]

    chunks_by_block: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        for block_id in tuple(chunk.block_ids or ()):
            chunks_by_block.setdefault(str(block_id), []).append(chunk)

    signals_by_page: dict[int, list[str]] = {}
    for signal in quality_signals:
        page_number = signal.get("page_number")
        if page_number is not None:
            signals_by_page.setdefault(_safe_int(page_number, default=1), []).append(str(signal.get("code") or ""))

    ir_blocks_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for block in ir_blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        ir_blocks_by_page.setdefault(page_number, []).append(block)

    units_by_page: dict[int, list[Mapping[str, Any]]] = {}
    embedded_chunk_ids = {chunk.chunk_id for chunk in chunks if chunk.embedding is not None}
    coverage_units = _coverage_units(
        knowledge_units=knowledge_units,
        embedded_chunk_ids=embedded_chunk_ids,
    )
    for unit in coverage_units:
        start, end = _page_span_tuple(unit.get("page_span"))
        for page_number in range(start, end + 1):
            units_by_page.setdefault(page_number, []).append(unit)

    tables_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for table in tables:
        tables_by_page.setdefault(_safe_int(table.get("page_number"), default=1), []).append(table)

    figures_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for figure in figures:
        figures_by_page.setdefault(_safe_int(figure.get("page_number"), default=1), []).append(figure)

    coverage_pages: list[dict[str, Any]] = []
    coverage_signals: list[dict[str, Any]] = []
    for page_number in page_numbers:
        page_payload = pages_by_number.get(page_number, {})
        page_blocks = ir_blocks_by_page.get(page_number, [])
        page_units = units_by_page.get(page_number, [])
        page_tables = tables_by_page.get(page_number, [])
        page_figures = figures_by_page.get(page_number, [])
        unit_ids = [str(unit.get("unit_id") or "") for unit in page_units if str(unit.get("unit_id") or "")]
        indexable_units = [unit for unit in page_units if bool(unit.get("should_index_for_rag"))]
        indexable_unit_ids = [str(unit.get("unit_id") or "") for unit in indexable_units if str(unit.get("unit_id") or "")]
        skipped_unit_ids = [
            str(unit.get("unit_id") or "")
            for unit in page_units
            if not bool(unit.get("should_index_for_rag")) and str(unit.get("unit_id") or "")
        ]
        table_ids_without_units = _table_ids_without_indexable_units(tables=page_tables, units=page_units)
        figure_ids_missing_caption = _figure_ids_missing_caption(page_figures)
        unchunked_unit_ids = [
            str(unit.get("unit_id") or "")
            for unit in indexable_units
            if not unit.get("chunk_ids")
        ]
        unembedded_unit_ids = [
            str(unit.get("unit_id") or "")
            for unit in indexable_units
            if unit.get("chunk_ids") and not bool(unit.get("embedded"))
        ]
        parsed_text_chars = sum(
            len(str(block.get("text") or "").strip())
            for block in page_blocks
            if str(block.get("index_policy") or "") != "skip"
        )
        chunk_ids = sorted(
            {
                chunk.chunk_id
                for block in page_blocks
                for chunk in chunks_by_block.get(str(block.get("block_id") or ""), [])
            }
        )
        provider_ids = sorted(
            {
                str((block.get("provenance") or {}).get("provider_id") or "")
                for block in page_blocks
                if str((block.get("provenance") or {}).get("provider_id") or "")
            }
        )
        page_chunks = [
            chunk
            for block in page_blocks
            for chunk in chunks_by_block.get(str(block.get("block_id") or ""), [])
        ]
        embedded = bool(page_chunks) and all(chunk.embedding is not None for chunk in page_chunks)
        missing_reason = _coverage_missing_reason(
            parsed_text_chars=parsed_text_chars,
            indexable_unit_count=len(indexable_units),
            unchunked_unit_ids=unchunked_unit_ids,
            chunk_ids=chunk_ids,
            embedded=embedded,
            has_chunks=bool(page_chunks),
        )
        signal_codes = list(dict.fromkeys(signals_by_page.get(page_number, [])))
        if missing_reason:
            code = _missing_reason_signal_code(missing_reason)
            signal_codes.append(code)
            detail: dict[str, Any] = {"missing_reason": missing_reason}
            if missing_reason == "no_chunks_for_indexable_units":
                detail["unit_ids"] = unchunked_unit_ids
            elif missing_reason == "chunks_not_embedded":
                detail["unit_ids"] = unembedded_unit_ids
            coverage_signals.append(
                {
                    "code": code,
                    "severity": "warning",
                    "message": _coverage_signal_message(code),
                    "page_number": page_number,
                    "detail": detail,
                }
            )
        if table_ids_without_units:
            code = "rag_table_without_unit"
            signal_codes.append(code)
            coverage_signals.append(
                {
                    "code": code,
                    "severity": "warning",
                    "message": _coverage_signal_message(code),
                    "page_number": page_number,
                    "detail": {"table_ids": table_ids_without_units},
                }
            )
        if figure_ids_missing_caption:
            code = "rag_figure_caption_missing"
            signal_codes.append(code)
            coverage_signals.append(
                {
                    "code": code,
                    "severity": "warning",
                    "message": _coverage_signal_message(code),
                    "page_number": page_number,
                    "detail": {"figure_ids": figure_ids_missing_caption},
                }
            )
        coverage_pages.append(
            {
                "page_number": page_number,
                "parsed_text_chars": parsed_text_chars,
                "table_count": len(page_tables),
                "figure_count": len(page_figures),
                "block_count": len(page_blocks),
                "unit_ids": unit_ids,
                "indexable_unit_ids": indexable_unit_ids,
                "skipped_unit_ids": skipped_unit_ids,
                "indexable_unit_count": len(indexable_units),
                "chunked_unit_count": len(indexable_units) - len(unchunked_unit_ids),
                "unchunked_unit_ids": unchunked_unit_ids,
                "unembedded_unit_ids": unembedded_unit_ids,
                "table_ids_without_units": table_ids_without_units,
                "figure_ids_missing_caption": figure_ids_missing_caption,
                "chunk_ids": chunk_ids,
                "embedded": embedded,
                "missing_reason": missing_reason,
                "provider_ids": provider_ids,
                "reading_order_confidence": _optional_float(page_payload.get("reading_order_confidence")),
                "quality_signal_codes": list(dict.fromkeys(signal_codes)),
            }
        )

    summary = _coverage_summary(pages=coverage_pages, units=coverage_units)
    rag_coverage_quality = rag_coverage_quality_payload(summary)
    enriched_index_manifest = _index_manifest_with_rag_coverage(
        index_manifest=index_manifest,
        coverage_units=coverage_units,
        chunks=chunks,
        summary=summary,
        coverage_pages=coverage_pages,
    )
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "projection": "coverage",
        "doc_id": doc_id,
        "parse_run_id": parse_run_id,
        "profile": profile,
        "state": state,
        "coverage": {
            "summary": summary,
            "pages": coverage_pages,
            "units": coverage_units,
        },
        "quality_signals": coverage_signals,
        "quality_summary": _signal_summary(coverage_signals),
        "rag_coverage_quality": rag_coverage_quality,
        "index_manifest": enriched_index_manifest,
    }


def _ir_pages(*, pages: Sequence[Mapping[str, Any]], ir_blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blocks_by_page: dict[int, list[str]] = {}
    for block in ir_blocks:
        page_number = _safe_int(block.get("page_number"), default=1)
        blocks_by_page.setdefault(page_number, []).append(str(block.get("block_id") or ""))

    ir_pages: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        page_number = _safe_int(page.get("page_number"), default=1)
        ir_pages.append(
            {
                "page_id": f"p{page_number:04d}",
                "page_number": page_number,
                "page_type": str(page.get("page_type") or "body"),
                "width": _optional_float(page.get("page_width")),
                "height": _optional_float(page.get("page_height")),
                "rotation": _safe_int(page.get("rotation"), default=0),
                "source_kind": str(page.get("source_kind") or "unknown"),
                "block_ids": blocks_by_page.get(page_number, []),
                "quality_flags": _string_list(page.get("quality_flags")),
                "reading_order_confidence": _optional_float(page.get("reading_order_confidence")),
            }
        )
    return ir_pages


def _ir_block(block: Block, *, index: int) -> dict[str, Any]:
    metadata = block.metadata or {}
    role = str(metadata.get("semantic_role") or block.type.value)
    page_number = _page_number(metadata)
    provider_id = _provider_id(metadata, block_type=block.type)
    source_kind = _source_kind(metadata, block_type=block.type)
    reader_policy = _reader_policy(block_type=block.type, semantic_role=role)
    index_policy = _index_policy(
        block_type=block.type,
        semantic_role=role,
        text=block.content,
        alt_text=str(metadata.get("alt_text") or ""),
        has_table_cells=bool(metadata.get("cells")),
    )
    provenance = {
        "provider_id": provider_id,
        "provider_version": str(metadata.get("provider_version") or metadata.get("parser_version") or ""),
        "adapter_version": str(metadata.get("adapter_version") or ""),
        "source_page_number": page_number,
    }
    _append_optional_provenance_float(
        provenance,
        "provider_elapsed_s",
        metadata,
        (
            "provider_elapsed_s",
            "parser_elapsed_s",
            "parse_elapsed_s",
            "elapsed_s",
            "total_elapsed_s",
            "layout_elapsed_s",
            "ocr_provider_elapsed_s",
        ),
    )
    _append_optional_provenance_float(
        provenance,
        "provider_memory_mb",
        metadata,
        (
            "provider_memory_mb",
            "memory_mb",
            "peak_memory_mb",
            "provider_peak_memory_mb",
            "peak_mb",
        ),
    )
    peak_kb = _optional_float(metadata.get("peak_kb"))
    if peak_kb is not None and "provider_memory_mb" not in provenance:
        provenance["provider_memory_mb"] = round(max(0.0, peak_kb / 1024.0), 4)
    _append_optional_provenance_float(
        provenance,
        "reading_order_confidence",
        metadata,
        (
            "reading_order_confidence",
            "layout_reading_order_confidence",
            "reading_order_score",
        ),
        clamp=(0.0, 1.0),
    )
    return {
        "block_id": block.block_id,
        "page_number": page_number,
        "page_span": list(_page_span_tuple(metadata.get("page_span"), fallback_page=page_number)),
        "type": block.type.value,
        "semantic_role": role,
        "text": block.content,
        "bbox": _bbox(metadata.get("bbox")),
        "reading_order": _safe_int(metadata.get("reading_order", metadata.get("page_position")), default=index),
        "confidence": _optional_float(metadata.get("confidence"), default=1.0),
        "source_kind": source_kind,
        "display_kind": _display_kind(block_type=block.type, semantic_role=role),
        "reader_policy": reader_policy,
        "index_policy": index_policy,
        "alt_text": str(metadata.get("alt_text") or "") if block.type == BlockType.IMAGE or role == "image" else "",
        "quality_flags": _string_list(metadata.get("quality_flags")),
        "provenance": provenance,
    }


def _ir_tables(*, tables: Sequence[Mapping[str, Any]], blocks: Sequence[Block]) -> list[dict[str, Any]]:
    block_provider = {block.block_id: _provider_id(block.metadata or {}, block_type=block.type) for block in blocks}
    block_source_kind = {block.block_id: _source_kind(block.metadata or {}, block_type=block.type) for block in blocks}
    ir_tables: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        block_id = str(table.get("block_id") or "")
        provider_id = str(table.get("source_parser") or block_provider.get(block_id) or "")
        warnings = _string_list(table.get("warnings"))
        page_number = _safe_int(table.get("page_number"), default=1)
        ir_tables.append(
            {
                "table_id": str(table.get("table_id") or ""),
                "source_doc_id": str(table.get("source_doc_id") or ""),
                "part_doc_id": str(table.get("part_doc_id") or ""),
                "block_id": block_id,
                "page_number": page_number,
                "page_span": [page_number, page_number],
                "semantic_role": str(table.get("semantic_role") or "table"),
                "source_kind": str(table.get("source_kind") or block_source_kind.get(block_id) or "structured_table"),
                "bbox": _bbox(table.get("bbox")),
                "rows": _safe_int(table.get("rows"), default=0),
                "cols": _safe_int(table.get("cols"), default=0),
                "header_rows": _safe_int(table.get("header_rows"), default=0),
                "cells": [dict(cell) for cell in table.get("cells", []) if isinstance(cell, Mapping)],
                "caption": str(table.get("table_title") or table.get("caption") or ""),
                "confidence": _optional_float(table.get("confidence"), default=1.0),
                "reader_policy": "table",
                "index_policy": "index_table_summary_and_cells" if table.get("cells") or table.get("text") else "skip_empty_table",
                "quality_flags": warnings,
                "provenance": {"provider_id": provider_id},
            }
        )
    return ir_tables


def _ir_figures(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    for block in blocks:
        metadata = block.metadata or {}
        role = str(metadata.get("semantic_role") or block.type.value)
        if block.type != BlockType.IMAGE and role != "image":
            continue
        page_number = _page_number(metadata)
        figures.append(
            {
                "figure_id": f"{block.doc_id}:p{page_number}:f{len(figures) + 1}",
                "block_id": block.block_id,
                "page_number": page_number,
                "page_span": [page_number, page_number],
                "semantic_role": role,
                "source_kind": _source_kind(metadata, block_type=block.type),
                "bbox": _bbox(metadata.get("bbox")),
                "figure_type": str(metadata.get("figure_kind") or metadata.get("figure_type") or "image"),
                "caption": block.content,
                "alt_text": str(metadata.get("alt_text") or ""),
                "confidence": _optional_float(
                    metadata.get("caption_confidence", metadata.get("confidence")),
                    default=1.0,
                ),
                "reader_policy": "source_snapshot",
                "index_policy": "index_caption_only"
                if block.content.strip() or str(metadata.get("alt_text") or "").strip()
                else "skip",
                "quality_flags": _string_list(metadata.get("quality_flags")),
                "provenance": {"provider_id": _provider_id(metadata, block_type=block.type)},
            }
        )
    return figures


def _knowledge_units(
    *,
    doc_id: str,
    ir_blocks: Sequence[Mapping[str, Any]],
    ir_tables: Sequence[Mapping[str, Any]],
    chunks: Sequence[Chunk],
    index_manifest: Any = None,
) -> list[dict[str, Any]]:
    chunk_ids_by_block: dict[str, list[str]] = {}
    for chunk in chunks:
        for block_id in tuple(chunk.block_ids or ()):
            chunk_ids_by_block.setdefault(str(block_id), []).append(chunk.chunk_id)
    table_ids_by_block = {
        str(table.get("block_id") or ""): str(table.get("table_id") or "")
        for table in ir_tables
        if table.get("block_id") and table.get("table_id")
    }
    manifest_units = _knowledge_units_from_runtime_manifest(
        doc_id=doc_id,
        index_manifest=index_manifest,
        ir_blocks=ir_blocks,
        ir_tables=ir_tables,
        chunks=chunks,
        chunk_ids_by_block=chunk_ids_by_block,
        table_ids_by_block=table_ids_by_block,
    )
    if manifest_units:
        return manifest_units

    units: list[dict[str, Any]] = []
    tables_by_block = {
        str(table.get("block_id") or ""): table
        for table in ir_tables
        if str(table.get("block_id") or "")
    }
    for block in ir_blocks:
        block_id = str(block.get("block_id") or "")
        chunk_ids = sorted(set(chunk_ids_by_block.get(block_id, [])))
        text = _knowledge_unit_text_from_exact_chunks(
            chunk_ids=chunk_ids,
            source_block_ids=[block_id] if block_id else [],
            chunks_by_id={str(chunk.chunk_id): chunk for chunk in chunks},
        )
        if not text:
            text = _fallback_block_unit_text(block=block, table=tables_by_block.get(block_id))
        index_policy = str(block.get("index_policy") or "skip")
        should_index = index_policy != "skip" and bool(text.strip())
        table_id = table_ids_by_block.get(block_id)
        source_table_ids = [table_id] if table_id else []
        units.append(
            {
                "unit_id": f"{doc_id}:ku:{len(units) + 1:06d}",
                "doc_id": doc_id,
                "source_block_ids": [block_id] if block_id else [],
                "source_table_ids": source_table_ids,
                "page_span": list(_page_span_tuple(block.get("page_span"))),
                "text": text,
                "unit_type": _unit_type(block),
                "semantic_role": str(block.get("semantic_role") or ""),
                "should_index_for_rag": should_index,
                "skip_reason": None if should_index else _skip_reason(block),
                "quality_flags": _string_list(block.get("quality_flags")),
                "chunk_ids": chunk_ids,
                "embedding_state": "skipped" if not should_index else ("pending" if chunk_ids else "pending"),
            }
        )
    return units


def _knowledge_units_from_runtime_manifest(
    *,
    doc_id: str,
    index_manifest: Any,
    ir_blocks: Sequence[Mapping[str, Any]],
    ir_tables: Sequence[Mapping[str, Any]],
    chunks: Sequence[Chunk],
    chunk_ids_by_block: Mapping[str, Sequence[str]],
    table_ids_by_block: Mapping[str, str],
) -> list[dict[str, Any]]:
    rag_manifest = _existing_rag_coverage_manifest(index_manifest)
    raw_units = rag_manifest.get("units") if rag_manifest is not None else None
    if not isinstance(raw_units, list):
        return []

    blocks_by_id = {
        str(block.get("block_id") or ""): block
        for block in ir_blocks
        if str(block.get("block_id") or "")
    }
    tables_by_block = {
        str(table.get("block_id") or ""): table
        for table in ir_tables
        if str(table.get("block_id") or "")
    }
    chunks_by_id = {str(chunk.chunk_id): chunk for chunk in chunks}
    units: list[dict[str, Any]] = []
    for index, raw_unit in enumerate(raw_units, start=1):
        if not isinstance(raw_unit, Mapping):
            continue
        source_block_ids = _string_list(raw_unit.get("source_block_ids"))
        chunk_ids = _string_list(raw_unit.get("chunk_ids"))
        if not chunk_ids:
            chunk_ids = sorted(
                {
                    str(chunk_id)
                    for block_id in source_block_ids
                    for chunk_id in chunk_ids_by_block.get(block_id, ())
                }
            )
        source_table_ids = _string_list(raw_unit.get("source_table_ids"))
        for block_id in source_block_ids:
            table_id = table_ids_by_block.get(block_id)
            if table_id:
                source_table_ids.append(table_id)
        source_table_ids = sorted(dict.fromkeys(source_table_ids))
        source_blocks = [blocks_by_id[block_id] for block_id in source_block_ids if block_id in blocks_by_id]
        source_tables = [tables_by_block[block_id] for block_id in source_block_ids if block_id in tables_by_block]
        text = _knowledge_unit_text_from_exact_chunks(
            chunk_ids=chunk_ids,
            source_block_ids=source_block_ids,
            chunks_by_id=chunks_by_id,
        )
        if not text:
            text = _knowledge_unit_text_from_ir_sources(
                raw_unit=raw_unit,
                source_blocks=source_blocks,
                source_tables=source_tables,
            )
        fallback_block = source_blocks[0] if source_blocks else {}
        unit_type = str(raw_unit.get("unit_type") or _unit_type(fallback_block) or "paragraph")
        semantic_role = str(raw_unit.get("semantic_role") or fallback_block.get("semantic_role") or "")
        should_index = raw_unit.get("should_index_for_rag")
        if should_index is None:
            should_index = bool(text.strip()) and str(fallback_block.get("index_policy") or "skip") != "skip"
        page_span = raw_unit.get("page_span")
        if page_span is None:
            page_span = fallback_block.get("page_span")
        source_item_ids = _manifest_source_item_ids(raw_unit)
        units.append(
            {
                "unit_id": str(raw_unit.get("unit_id") or f"{doc_id}:ku:{index:06d}"),
                "doc_id": doc_id,
                "source_item_ids": source_item_ids,
                "source_block_ids": source_block_ids,
                "source_table_ids": source_table_ids,
                "page_span": list(_page_span_tuple(page_span)),
                "text": text,
                "unit_type": unit_type,
                "semantic_role": semantic_role,
                "should_index_for_rag": bool(should_index),
                "skip_reason": raw_unit.get("skip_reason") if not bool(should_index) else None,
                "quality_flags": _merged_quality_flags(source_blocks),
                "chunk_ids": sorted(dict.fromkeys(chunk_ids)),
                "embedding_state": raw_unit.get("embedding_state") or ("skipped" if not bool(should_index) else "pending"),
            }
        )
    return units


def _knowledge_unit_text_from_exact_chunks(
    *,
    chunk_ids: Sequence[str],
    source_block_ids: Sequence[str],
    chunks_by_id: Mapping[str, Chunk],
) -> str:
    source_block_set = {str(block_id) for block_id in source_block_ids if str(block_id)}
    parts: list[str] = []
    for chunk_id in chunk_ids:
        chunk = chunks_by_id.get(str(chunk_id))
        if chunk is None:
            continue
        chunk_block_set = {str(block_id) for block_id in tuple(chunk.block_ids or ()) if str(block_id)}
        if chunk_block_set != source_block_set:
            continue
        text = str(chunk.text or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(dict.fromkeys(parts)).strip()


def _knowledge_unit_text_from_ir_sources(
    *,
    raw_unit: Mapping[str, Any],
    source_blocks: Sequence[Mapping[str, Any]],
    source_tables: Sequence[Mapping[str, Any]],
) -> str:
    unit_type = str(raw_unit.get("unit_type") or "").strip().lower()
    if unit_type == "table" and source_tables:
        rendered_tables = [_render_ir_table_text(table) for table in source_tables]
        text = "\n\n".join(part for part in rendered_tables if part.strip()).strip()
        if text:
            return text
    if unit_type == "figure_caption":
        parts: list[str] = []
        for block in source_blocks:
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
            alt_text = str(block.get("alt_text") or "").strip()
            if alt_text:
                parts.append(alt_text)
        return "\n\n".join(dict.fromkeys(parts)).strip()
    return "\n\n".join(
        str(block.get("text") or "").strip()
        for block in source_blocks
        if str(block.get("text") or "").strip()
    ).strip()


def _unit_embedding_state(
    *,
    should_index_for_rag: bool,
    chunk_ids: Sequence[str],
    embedded: bool,
) -> str:
    if not should_index_for_rag:
        return "skipped"
    if embedded:
        return "embedded"
    if list(chunk_ids):
        return "pending"
    return "pending"


def _coverage_units(
    *,
    knowledge_units: Sequence[Mapping[str, Any]],
    embedded_chunk_ids: set[str],
) -> list[dict[str, Any]]:
    coverage_units: list[dict[str, Any]] = []
    for unit in knowledge_units:
        chunk_ids = _string_list(unit.get("chunk_ids"))
        should_index = bool(unit.get("should_index_for_rag"))
        embedded = _unit_embedded(unit, embedded_chunk_ids=embedded_chunk_ids)
        embedding_state = _unit_embedding_state(
            should_index_for_rag=should_index,
            chunk_ids=chunk_ids,
            embedded=embedded,
        )
        missing_reason = _coverage_unit_missing_reason(
            should_index_for_rag=should_index,
            chunk_ids=chunk_ids,
            embedded=embedded,
        )
        coverage_state = _coverage_unit_state(
            should_index_for_rag=should_index,
            missing_reason=missing_reason,
        )
        quality_signal_codes = _coverage_unit_quality_signal_codes(
            unit=unit,
            missing_reason=missing_reason,
        )
        coverage_units.append(
            {
                "unit_id": str(unit.get("unit_id") or ""),
                "doc_id": str(unit.get("doc_id") or ""),
                "source_item_ids": _string_list(unit.get("source_item_ids")),
                "source_block_ids": _string_list(unit.get("source_block_ids")),
                "source_table_ids": _string_list(unit.get("source_table_ids")),
                "page_span": list(_page_span_tuple(unit.get("page_span"))),
                "text": str(unit.get("text") or ""),
                "unit_type": str(unit.get("unit_type") or ""),
                "semantic_role": str(unit.get("semantic_role") or ""),
                "should_index_for_rag": should_index,
                "skip_reason": unit.get("skip_reason"),
                "quality_flags": _string_list(unit.get("quality_flags")),
                "chunk_ids": chunk_ids,
                "chunk_count": len(chunk_ids),
                "embedded_chunk_count": len([chunk_id for chunk_id in chunk_ids if chunk_id in embedded_chunk_ids]),
                "embedded": embedded,
                "embedding_model": unit.get("embedding_model"),
                "embedding_state": embedding_state,
                "embedding_error_category": unit.get("embedding_error_category"),
                "coverage_state": coverage_state,
                "missing_reason": missing_reason,
                "quality_signal_codes": quality_signal_codes,
            }
        )
    return coverage_units


def _fallback_block_unit_text(*, block: Mapping[str, Any], table: Mapping[str, Any] | None) -> str:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "table" and table is not None:
        text = _render_ir_table_text(table)
        if text:
            return text
    if block_type == "image":
        parts = [
            str(block.get("text") or "").strip(),
            str(block.get("alt_text") or "").strip(),
        ]
        return "\n\n".join(dict.fromkeys(part for part in parts if part)).strip()
    return str(block.get("text") or "").strip()


def _render_ir_table_text(table: Mapping[str, Any]) -> str:
    rows = _table_rows_from_cells(table.get("cells"))
    body = _markdown_table(rows) if rows else ""
    caption = str(table.get("caption") or "").strip()
    parts = [part for part in (caption, body) if part]
    return "\n\n".join(parts).strip()


def _table_rows_from_cells(cells: Any) -> list[list[str]]:
    if not isinstance(cells, list):
        return []
    rows: dict[int, dict[int, str]] = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        row_index = _safe_int(cell.get("row", cell.get("row_index")), default=0)
        col_index = _safe_int(cell.get("col", cell.get("col_index")), default=0)
        rows.setdefault(row_index, {})[col_index] = str(cell.get("text") or "")
    if not rows:
        return []
    width = max((max(row) for row in rows.values() if row), default=-1) + 1
    if width <= 0:
        return []
    return [
        [rows.get(row_index, {}).get(col_index, "") for col_index in range(width)]
        for row_index in range(max(rows) + 1)
    ]


def _markdown_table(rows: Sequence[Sequence[str]]) -> str:
    normalized = [[str(cell).strip() for cell in row] for row in rows]
    normalized = [row for row in normalized if any(cell for cell in row)]
    if not normalized:
        return ""
    width = max(len(row) for row in normalized)
    padded = [row + [""] * (width - len(row)) for row in normalized]
    header = padded[0]
    separator = ["---"] * width
    body = padded[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _manifest_source_item_ids(raw_unit: Mapping[str, Any]) -> list[str]:
    source_item_ids = _string_list(raw_unit.get("source_item_ids"))
    source_item_id = str(raw_unit.get("source_item_id") or "").strip()
    if source_item_id:
        source_item_ids.append(source_item_id)
    return sorted(dict.fromkeys(source_item_ids))


def _merged_quality_flags(blocks: Sequence[Mapping[str, Any]]) -> list[str]:
    flags: list[str] = []
    for block in blocks:
        flags.extend(_string_list(block.get("quality_flags")))
    return sorted(dict.fromkeys(flags))


def _providers(ir_blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    providers: dict[str, dict[str, Any]] = {}
    for block in ir_blocks:
        provenance = block.get("provenance")
        if not isinstance(provenance, Mapping):
            continue
        provider_id = str(provenance.get("provider_id") or "")
        if not provider_id:
            continue
        entry = providers.setdefault(
            provider_id,
            {
                "provider_id": provider_id,
                "provider_version": str(provenance.get("provider_version") or ""),
                "adapter_version": str(provenance.get("adapter_version") or ""),
                "block_count": 0,
            },
        )
        if not str(entry.get("provider_version") or ""):
            entry["provider_version"] = str(provenance.get("provider_version") or "")
        if not str(entry.get("adapter_version") or ""):
            entry["adapter_version"] = str(provenance.get("adapter_version") or "")
        entry["block_count"] = int(entry.get("block_count", 0)) + 1
    return list(providers.values())


def _index_manifest_with_rag_coverage(
    *,
    index_manifest: Any,
    coverage_units: Sequence[Mapping[str, Any]],
    chunks: Sequence[Chunk],
    summary: Mapping[str, Any],
    coverage_pages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = dict(index_manifest) if isinstance(index_manifest, Mapping) else {}
    existing_rag = _existing_rag_coverage_manifest(manifest) or {}
    embedded_chunk_ids = {chunk.chunk_id for chunk in chunks if chunk.embedding is not None}
    all_chunk_ids = sorted({chunk_id for page in coverage_pages for chunk_id in _string_list(page.get("chunk_ids"))})
    indexable_units = [unit for unit in coverage_units if bool(unit.get("should_index_for_rag"))]
    chunked_units = [unit for unit in indexable_units if _string_list(unit.get("chunk_ids"))]
    skipped_units = [unit for unit in coverage_units if not bool(unit.get("should_index_for_rag"))]
    rag_units = [
        {
            "unit_id": str(unit.get("unit_id") or ""),
            "unit_type": str(unit.get("unit_type") or ""),
            "semantic_role": str(unit.get("semantic_role") or ""),
            "page_span": list(_page_span_tuple(unit.get("page_span"))),
            "source_item_ids": _string_list(unit.get("source_item_ids")),
            "source_block_ids": _string_list(unit.get("source_block_ids")),
            "source_table_ids": _string_list(unit.get("source_table_ids")),
            "should_index_for_rag": bool(unit.get("should_index_for_rag")),
            "skip_reason": unit.get("skip_reason"),
            "chunk_ids": _string_list(unit.get("chunk_ids")),
            "embedded": _unit_embedded(unit, embedded_chunk_ids=embedded_chunk_ids),
            "coverage_state": str(unit.get("coverage_state") or ""),
            "missing_reason": unit.get("missing_reason"),
            "quality_signal_codes": _string_list(unit.get("quality_signal_codes")),
        }
        for unit in coverage_units
    ]
    manifest["rag_coverage"] = {
        "schema_version": "2026-06-rag-index-manifest",
        "source": str(existing_rag.get("source") or "ir_projection"),
        "strategy": str(existing_rag.get("strategy") or "projection_knowledge_units"),
        "unit_count": len(coverage_units),
        "indexable_unit_count": len(indexable_units),
        "skipped_unit_count": len(skipped_units),
        "chunked_unit_count": len(chunked_units),
        "unchunked_unit_count": len(indexable_units) - len(chunked_units),
        "chunk_count": len(all_chunk_ids),
        "embedded_chunk_count": len(embedded_chunk_ids),
        "coverage_score": _optional_float(summary.get("unit_chunk_coverage_ratio"), default=1.0),
        "text_page_coverage_ratio": _optional_float(summary.get("text_page_coverage_ratio"), default=1.0),
        "table_unit_coverage_ratio": _optional_float(summary.get("table_unit_coverage_ratio"), default=1.0),
        "pages_with_coverage_gaps": _safe_int(summary.get("pages_with_coverage_gaps"), default=0),
        "chunk_ids": all_chunk_ids,
        "units": rag_units,
    }
    return manifest


def _existing_rag_coverage_manifest(index_manifest: Any) -> Mapping[str, Any] | None:
    if not isinstance(index_manifest, Mapping):
        return None
    rag_manifest = index_manifest.get("rag_coverage")
    if not isinstance(rag_manifest, Mapping):
        return None
    return rag_manifest


def _coverage_unit_missing_reason(
    *,
    should_index_for_rag: bool,
    chunk_ids: Sequence[str],
    embedded: bool,
) -> str | None:
    if not should_index_for_rag:
        return None
    if not list(chunk_ids):
        return "no_chunks_for_indexable_units"
    if not embedded:
        return "chunks_not_embedded"
    return None


def _coverage_unit_state(
    *,
    should_index_for_rag: bool,
    missing_reason: str | None,
) -> str:
    if not should_index_for_rag:
        return "skipped"
    if missing_reason == "no_chunks_for_indexable_units":
        return "missing_chunks"
    if missing_reason == "chunks_not_embedded":
        return "chunks_not_embedded"
    return "covered"


def _coverage_unit_quality_signal_codes(
    *,
    unit: Mapping[str, Any],
    missing_reason: str | None,
) -> list[str]:
    codes = _string_list(unit.get("quality_flags"))
    if missing_reason:
        code = _missing_reason_signal_code(missing_reason)
        if code and code not in codes:
            codes.append(code)
    return codes


def _unit_embedded(unit: Mapping[str, Any], *, embedded_chunk_ids: set[str]) -> bool:
    chunk_ids = _string_list(unit.get("chunk_ids"))
    return bool(chunk_ids) and all(chunk_id in embedded_chunk_ids for chunk_id in chunk_ids)


def _table_ids_without_indexable_units(
    *,
    tables: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
) -> list[str]:
    indexable_table_ids = {
        table_id
        for unit in units
        if bool(unit.get("should_index_for_rag"))
        for table_id in _string_list(unit.get("source_table_ids"))
    }
    missing: list[str] = []
    for table in tables:
        table_id = str(table.get("table_id") or "")
        if table_id and table_id not in indexable_table_ids:
            missing.append(table_id)
    return missing


def _figure_ids_missing_caption(figures: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(figure.get("figure_id") or "")
        for figure in figures
        if str(figure.get("figure_id") or "")
        and not str(figure.get("caption") or "").strip()
        and not str(figure.get("alt_text") or "").strip()
    ]


def _coverage_summary(
    *,
    pages: Sequence[Mapping[str, Any]],
    units: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    total_pages = len(pages)
    pages_with_parsed_text = sum(1 for page in pages if _safe_int(page.get("parsed_text_chars"), default=0) > 0)
    pages_with_indexable_units = sum(1 for page in pages if _safe_int(page.get("indexable_unit_count"), default=0) > 0)
    pages_missing_chunks = sum(1 for page in pages if page.get("missing_reason") == "no_chunks_for_indexable_units")
    pages_missing_rag_units = sum(1 for page in pages if page.get("missing_reason") == "no_indexable_units")
    pages_chunks_not_embedded = sum(1 for page in pages if page.get("missing_reason") == "chunks_not_embedded")
    pages_with_coverage_gaps = sum(
        1
        for page in pages
        if page.get("missing_reason") is not None
        or _string_list(page.get("table_ids_without_units"))
        or _string_list(page.get("figure_ids_missing_caption"))
    )
    pages_table_without_units = sum(1 for page in pages if page.get("table_ids_without_units"))
    pages_figure_caption_missing = sum(1 for page in pages if page.get("figure_ids_missing_caption"))
    total_indexable_units = sum(_safe_int(page.get("indexable_unit_count"), default=0) for page in pages)
    total_chunked_units = sum(_safe_int(page.get("chunked_unit_count"), default=0) for page in pages)
    table_pages = sum(1 for page in pages if _safe_int(page.get("table_count"), default=0) > 0)
    table_pages_with_units = sum(
        1
        for page in pages
        if _safe_int(page.get("table_count"), default=0) > 0
        and _safe_int(page.get("indexable_unit_count"), default=0) > 0
    )
    gap_pages = [
        {
            "page_number": _safe_int(page.get("page_number"), default=1),
            "missing_reason": page.get("missing_reason"),
            "unit_ids": _string_list(page.get("unit_ids")),
            "indexable_unit_ids": _string_list(page.get("indexable_unit_ids")),
            "unchunked_unit_ids": _string_list(page.get("unchunked_unit_ids")),
            "unembedded_unit_ids": _string_list(page.get("unembedded_unit_ids")),
            "table_ids_without_units": _string_list(page.get("table_ids_without_units")),
            "figure_ids_missing_caption": _string_list(page.get("figure_ids_missing_caption")),
            "quality_signal_codes": _string_list(page.get("quality_signal_codes")),
        }
        for page in pages
        if page.get("missing_reason") is not None
        or _string_list(page.get("table_ids_without_units"))
        or _string_list(page.get("figure_ids_missing_caption"))
    ]
    indexable_units = [unit for unit in units if bool(unit.get("should_index_for_rag"))]
    skipped_units = [unit for unit in units if not bool(unit.get("should_index_for_rag"))]
    embedded_units = [unit for unit in indexable_units if bool(unit.get("embedded"))]
    unembedded_units = [
        unit
        for unit in indexable_units
        if _string_list(unit.get("chunk_ids")) and not bool(unit.get("embedded"))
    ]
    gap_unit_ids = [
        str(unit.get("unit_id") or "")
        for unit in indexable_units
        if unit.get("missing_reason") is not None and str(unit.get("unit_id") or "")
    ]
    return {
        "total_pages": total_pages,
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
        "total_unit_count": len(units),
        "skipped_unit_count": len(skipped_units),
        "embedded_unit_count": len(embedded_units),
        "unembedded_unit_count": len(unembedded_units),
        "gap_unit_ids": gap_unit_ids,
        "gap_pages": gap_pages,
        "text_page_coverage_ratio": _ratio(pages_with_indexable_units, pages_with_parsed_text),
        "unit_chunk_coverage_ratio": _ratio(total_chunked_units, total_indexable_units),
        "table_unit_coverage_ratio": _ratio(table_pages_with_units, table_pages),
    }


def rag_coverage_quality_payload(summary: Mapping[str, Any]) -> dict[str, Any]:
    total_pages = _safe_int(summary.get("total_pages"), default=0)
    pages_missing_rag_units = _safe_int(summary.get("pages_missing_rag_units"), default=0)
    pages_missing_chunks = _safe_int(summary.get("pages_missing_chunks"), default=0)
    pages_chunks_not_embedded = _safe_int(summary.get("pages_chunks_not_embedded"), default=0)
    pages_with_coverage_gaps = _safe_int(summary.get("pages_with_coverage_gaps"), default=0)
    pages_table_without_units = _safe_int(summary.get("pages_table_without_units"), default=0)
    pages_figure_caption_missing = _safe_int(summary.get("pages_figure_caption_missing"), default=0)
    flags: list[str] = []
    warnings: list[str] = []
    if pages_missing_rag_units:
        flags.append("rag_empty_text_page")
        warnings.append(f"{pages_missing_rag_units} page(s) have parsed text but no indexable RAG unit")
    if pages_missing_chunks:
        flags.append("rag_units_without_chunks")
        warnings.append(f"{pages_missing_chunks} page(s) have indexable RAG units without chunks")
    if pages_chunks_not_embedded:
        flags.append("rag_chunks_not_embedded")
        warnings.append(f"{pages_chunks_not_embedded} page(s) have chunks that have not been embedded")
    if pages_table_without_units:
        flags.append("rag_table_without_unit")
        warnings.append(f"{pages_table_without_units} page(s) have tables without indexable RAG units")
    if pages_figure_caption_missing:
        flags.append("rag_figure_caption_missing")
        warnings.append(f"{pages_figure_caption_missing} page(s) have figures without captions")

    if pages_missing_rag_units:
        recommended_action = "review_parse_ir"
        gate = "manual_review"
    elif pages_table_without_units:
        recommended_action = "local_provider_rerun"
        gate = "accept_with_warning"
    elif pages_figure_caption_missing:
        recommended_action = "review_parse_ir"
        gate = "accept_with_warning"
    elif pages_missing_chunks:
        recommended_action = "rechunk_document"
        gate = "accept_with_warning"
    elif pages_chunks_not_embedded:
        recommended_action = "reembed_document"
        gate = "accept_with_warning"
    else:
        recommended_action = None
        gate = "accept"

    page_denominator = max(total_pages, 1)
    page_gap_score = max(0.0, 1.0 - (pages_with_coverage_gaps / page_denominator))
    raw_unit_chunk_score = _optional_float(summary.get("unit_chunk_coverage_ratio"), default=1.0)
    unit_chunk_score = float(raw_unit_chunk_score if raw_unit_chunk_score is not None else 1.0)
    score = round(min(page_gap_score, unit_chunk_score), 4)
    return {
        "score": score,
        "gate": gate,
        "flags": flags,
        "warnings": warnings,
        "recommended_action": recommended_action,
        "page_count": total_pages,
        "pages_with_coverage_gaps": pages_with_coverage_gaps,
        "pages_missing_rag_units": pages_missing_rag_units,
        "pages_missing_chunks": pages_missing_chunks,
        "pages_chunks_not_embedded": pages_chunks_not_embedded,
        "pages_table_without_units": pages_table_without_units,
        "pages_figure_caption_missing": pages_figure_caption_missing,
        "total_indexable_units": _safe_int(summary.get("total_indexable_units"), default=0),
        "total_chunked_units": _safe_int(summary.get("total_chunked_units"), default=0),
        "unit_chunk_coverage_ratio": unit_chunk_score,
        "text_page_coverage_ratio": float(_optional_float(summary.get("text_page_coverage_ratio"), default=1.0) or 0.0),
    }


def _coverage_missing_reason(
    *,
    parsed_text_chars: int,
    indexable_unit_count: int,
    unchunked_unit_ids: Sequence[str],
    chunk_ids: Sequence[str],
    embedded: bool,
    has_chunks: bool,
) -> str | None:
    if parsed_text_chars > 0 and indexable_unit_count == 0:
        return "no_indexable_units"
    if indexable_unit_count > 0 and (unchunked_unit_ids or not chunk_ids):
        return "no_chunks_for_indexable_units"
    if has_chunks and not embedded:
        return "chunks_not_embedded"
    return None


def _missing_reason_signal_code(missing_reason: str) -> str:
    return {
        "no_indexable_units": "rag_empty_text_page",
        "no_chunks_for_indexable_units": "rag_units_without_chunks",
        "chunks_not_embedded": "rag_chunks_not_embedded",
    }.get(missing_reason, "rag_coverage_gap")


def _coverage_signal_message(code: str) -> str:
    return {
        "rag_empty_text_page": "Page has parsed text but no indexable RAG unit",
        "rag_units_without_chunks": "Page has indexable RAG units but no chunks",
        "rag_chunks_not_embedded": "Page has chunks that have not been embedded",
        "rag_table_without_unit": "Page has structured tables without indexable RAG units",
        "rag_figure_caption_missing": "Page has figures without captions for RAG",
    }.get(code, code)


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
    payload = {str(key): _json_safe_value(item) for key, item in value.items()}
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


def _normalize_local_provider_admission(
    value: Mapping[str, Any] | None,
    *,
    enabled: bool,
) -> dict[str, Any]:
    admission = value if isinstance(value, Mapping) else {}
    route_mode = str(admission.get("route_mode") or "").strip().lower() or "route"
    gate_status = str(admission.get("gate_status") or "").strip().lower() or "passed"
    gate_checks = [
        str(item).strip().lower()
        for item in (admission.get("gate_checks") or [])
        if str(item).strip()
    ]
    route_ready = bool(
        admission.get(
            "route_ready",
            enabled and route_mode == "route" and gate_status == "passed",
        )
    )
    return {
        "route_mode": route_mode,
        "gate_status": gate_status,
        "gate_checks": gate_checks,
        "route_ready": route_ready,
    }


def _normalize_provider_registry(value: Any) -> dict[str, Any]:
    registry = value if isinstance(value, Mapping) else {}
    routing = registry.get("routing") if isinstance(registry.get("routing"), Mapping) else {}
    local_parsers: list[dict[str, Any]] = []
    for item in registry.get("local_parsers") or []:
        if not isinstance(item, Mapping):
            continue
        provider_id = str(item.get("id") or "").strip()
        if not provider_id:
            continue
        enabled = bool(item.get("enabled", False))
        local_parsers.append(
            {
                "id": provider_id,
                "enabled": enabled,
                "priority": int(item.get("priority", 0) or 0),
                "media_types": [str(token) for token in (item.get("media_types") or []) if str(token).strip()],
                "extensions": [str(token) for token in (item.get("extensions") or []) if str(token).strip()],
                "profiles": [str(token) for token in (item.get("profiles") or []) if str(token).strip()],
                "capabilities": [str(token) for token in (item.get("capabilities") or []) if str(token).strip()],
                "admission": _normalize_local_provider_admission(item.get("admission"), enabled=enabled),
                "options": _json_safe_value(item.get("options") or {}),
            }
        )
    summary = {
        "total": len(local_parsers),
        "enabled": len([provider for provider in local_parsers if provider["enabled"]]),
        "disabled": len([provider for provider in local_parsers if not provider["enabled"]]),
        "route_ready": len([provider for provider in local_parsers if provider["admission"]["route_ready"]]),
        "evaluation_only": len(
            [provider for provider in local_parsers if provider["admission"]["route_mode"] == "evaluate"]
        ),
        "gate_pending": len(
            [provider for provider in local_parsers if provider["admission"]["gate_status"] == "pending"]
        ),
        "gate_failed": len(
            [provider for provider in local_parsers if provider["admission"]["gate_status"] == "failed"]
        ),
    }
    raw_summary = registry.get("summary")
    if isinstance(raw_summary, Mapping):
        for key in summary:
            if key in raw_summary:
                try:
                    summary[key] = int(raw_summary.get(key))  # type: ignore[assignment]
                except (TypeError, ValueError):
                    pass
    return {
        "schema_version": str(registry.get("schema_version") or "2026-06-local-provider-registry"),
        "routing": {
            "enabled": bool(routing.get("enabled", False)),
            "fallback_to_default": bool(routing.get("fallback_to_default", True)),
            "include_disabled": bool(routing.get("include_disabled", False)),
            "routing_policy": str(routing.get("routing_policy") or "").strip() or "priority_desc_then_id",
        },
        "local_parsers": local_parsers,
        "summary": summary,
    }


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    return value


def _signal_summary(signals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_severity: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for signal in signals:
        severity = str(signal.get("severity") or "info")
        code = str(signal.get("code") or "unknown")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_code[code] = by_code.get(code, 0) + 1
    return {"total": len(signals), "by_severity": by_severity, "by_code": by_code}


def _reader_policy(*, block_type: BlockType, semantic_role: str) -> str:
    if semantic_role in _SKIP_INDEX_ROLES:
        return "hidden"
    if block_type == BlockType.TABLE:
        return "table"
    if block_type == BlockType.IMAGE or semantic_role == "image":
        return "source_snapshot"
    return "inline"


def _index_policy(
    *,
    block_type: BlockType,
    semantic_role: str,
    text: str,
    alt_text: str = "",
    has_table_cells: bool = False,
) -> str:
    if semantic_role in _SKIP_INDEX_ROLES:
        return "skip"
    if block_type == BlockType.TABLE:
        return "index_table_summary_and_cells" if text.strip() or has_table_cells else "skip"
    if block_type == BlockType.IMAGE or semantic_role == "image":
        return "index_caption_only" if text.strip() or alt_text.strip() else "skip"
    return "index" if text.strip() else "skip"


def _display_kind(*, block_type: BlockType, semantic_role: str) -> str:
    if semantic_role in _SKIP_INDEX_ROLES:
        return "artifact"
    if block_type == BlockType.TABLE:
        return "table"
    if block_type == BlockType.IMAGE or semantic_role == "image":
        return "figure"
    return "text"


def _unit_type(block: Mapping[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    if block_type == "table":
        return "table"
    if block_type == "image":
        return "figure_caption"
    if block_type == "title":
        return "title"
    return "paragraph"


def _skip_reason(block: Mapping[str, Any]) -> str:
    text = str(block.get("text") or "").strip()
    if not text:
        return "empty_text"
    role = str(block.get("semantic_role") or "")
    if role in _SKIP_INDEX_ROLES:
        return f"semantic_role:{role}"
    block_type = str(block.get("type") or "").strip().lower()
    # Figure without any caption or alt_text
    if (block_type == "image" or role == "image") and not text and not str(block.get("alt_text") or "").strip():
        return "figure_caption_missing"
    # Low-confidence OCR result
    ocr_confidence = block.get("ocr_confidence")
    if ocr_confidence is not None:
        try:
            if float(ocr_confidence) < 0.5:
                return "low_confidence_ocr"
        except (TypeError, ValueError):
            pass
    cid_garble_ratio = block.get("cid_garble_ratio")
    if cid_garble_ratio is not None:
        try:
            if float(cid_garble_ratio) > 0.3:
                return "low_confidence_ocr"
        except (TypeError, ValueError):
            pass
    # TOC entries that duplicate already-indexed headings
    if role == "toc" or role == "toc_entry":
        return "toc_duplicate"
    # Diagnostic or artifact text markers
    if text.startswith("<parse_artifact") or text.startswith("<diagnostic"):
        return "diagnostic_text"
    return "index_policy_skip"


def _provider_id(metadata: Mapping[str, Any], *, block_type: BlockType) -> str:
    for key in ("provider_id", "parser", "layout_source", "ocr_engine"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if block_type == BlockType.IMAGE:
        return "image-ocr" if metadata.get("ocr_engine") else "pdf-text"
    return "parsecore-native"


def _source_kind(metadata: Mapping[str, Any], *, block_type: BlockType) -> str:
    value = metadata.get("source_kind")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if metadata.get("ocr_engine") or metadata.get("ocr_fallback_used"):
        return "ocr_text"
    if block_type == BlockType.TABLE:
        return "structured_table"
    if block_type == BlockType.IMAGE:
        return "pdf_image"
    return "native_text"


def _page_number(metadata: Mapping[str, Any]) -> int:
    return _safe_int(metadata.get("page", metadata.get("page_number")), default=1)


def _page_span_tuple(value: Any, *, fallback_page: int = 1) -> tuple[int, int]:
    if isinstance(value, Mapping):
        start = _safe_int(value.get("start", value.get("page_start")), default=fallback_page)
        end = _safe_int(value.get("end", value.get("page_end")), default=start)
        return (min(start, end), max(start, end))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _safe_int(value[0], default=fallback_page)
        end = _safe_int(value[1], default=start)
        return (min(start, end), max(start, end))
    return (fallback_page, fallback_page)


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _append_optional_provenance_float(
    provenance: dict[str, Any],
    output_key: str,
    metadata: Mapping[str, Any],
    keys: Sequence[str],
    *,
    clamp: tuple[float, float] | None = None,
) -> None:
    for key in keys:
        value = _optional_float(metadata.get(key))
        if value is None:
            continue
        if clamp is not None:
            value = max(clamp[0], min(clamp[1], value))
        provenance[output_key] = round(max(0.0, value), 6)
        return


def _optional_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)
