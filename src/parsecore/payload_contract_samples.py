from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .api_payloads import (
    _document_projection,
    _document_providers_projection,
    _document_quality_projection,
)
from .models import Block, BlockType, Chunk, ParseJobState
from .parts import document_parts_projection


def _base_provider_registry() -> dict[str, Any]:
    return {
        "schema_version": "2026-06-local-provider-registry",
        "routing": {
            "enabled": True,
            "fallback_to_default": True,
            "include_disabled": False,
            "routing_policy": "priority_desc_then_id",
        },
        "local_parsers": [
            {
                "id": "pdf-text",
                "enabled": True,
                "priority": 100,
                "media_types": ["application/pdf"],
                "extensions": [".pdf"],
                "profiles": ["table-heavy"],
                "capabilities": ["native-text", "tables", "layout"],
                "admission": {
                    "route_mode": "route",
                    "gate_status": "passed",
                    "gate_checks": ["samples", "license", "performance", "observability"],
                    "route_ready": True,
                },
                "options": {},
            },
            {
                "id": "pymupdf4llm-local",
                "enabled": False,
                "priority": 80,
                "media_types": ["application/pdf"],
                "extensions": [".pdf"],
                "profiles": ["table-heavy"],
                "capabilities": ["markdown", "rag-baseline"],
                "admission": {
                    "route_mode": "evaluate",
                    "gate_status": "pending",
                    "gate_checks": ["samples", "license", "performance", "observability"],
                    "route_ready": False,
                },
                "options": {},
            },
        ],
        "summary": {
            "total": 2,
            "enabled": 1,
            "disabled": 1,
            "route_ready": 1,
            "evaluation_only": 1,
            "gate_pending": 1,
            "gate_failed": 0,
        },
    }


def _base_local_provider_routing() -> dict[str, Any]:
    return {
        "schema_version": "2026-06-local-provider-routing-decision",
        "enabled": True,
        "routing_policy": "priority_desc_then_id",
        "route_status": "selected",
        "selected_provider_id": "pdf-text",
        "selected_route_role": "primary",
        "primary_provider_id": "pdf-text",
        "fallback_provider_ids": ["pymupdf4llm-local"],
        "eligible_provider_ids": ["pdf-text", "pymupdf4llm-local"],
        "excluded_provider_ids": [],
        "fallback_to_default": True,
        "requested": {
            "media_type": "application/pdf",
            "extension": ".pdf",
            "profile": "table-heavy",
            "file_name": "manual.pdf",
            "required_capabilities": [],
            "include_disabled": False,
        },
    }


def build_sample_snapshot() -> dict[str, object]:
    """Return a representative document snapshot that exercises the core payload contracts."""

    job = SimpleNamespace(
        job_id="job-schema-001",
        doc_id="doc-schema-001",
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={
            "profile": "table-heavy",
            "local_provider_routing": _base_local_provider_routing(),
        },
    )
    blocks = (
        Block(
            block_id="blk-title",
            doc_id="doc-schema-001",
            type=BlockType.TITLE,
            content="Maintenance Manual",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "title",
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
                "source_kind": "native_text",
            },
        ),
        Block(
            block_id="blk-body",
            doc_id="doc-schema-001",
            type=BlockType.PARAGRAPH,
            content="Inspect the hydraulic pump before dispatch.",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [10, 20, 300, 60],
                "reading_order": 2,
                "confidence": 0.95,
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
                "source_kind": "native_text",
                "reading_order_confidence": 0.82,
            },
        ),
        Block(
            block_id="blk-table",
            doc_id="doc-schema-001",
            type=BlockType.TABLE,
            content="Part | Qty\nPump | 1",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "table",
                "table_index": 1,
                "rows": 2,
                "cols": 2,
                "cells": [["Part", "Qty"], ["Pump", "1"]],
                "source_kind": "structured_table",
            },
        ),
        Block(
            block_id="blk-figure",
            doc_id="doc-schema-001",
            type=BlockType.IMAGE,
            content="Figure 1. Hydraulic workflow",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "image",
                "bbox": [30, 80, 240, 220],
                "figure_kind": "flowchart",
                "source_kind": "pdf_image",
                "caption_confidence": 0.88,
                "alt_text": "Hydraulic workflow figure",
            },
        ),
        Block(
            block_id="blk-footer",
            doc_id="doc-schema-001",
            type=BlockType.PARAGRAPH,
            content="Page 1",
            metadata={"page": 1, "parser": "pdf-text", "semantic_role": "header_footer"},
        ),
    )
    chunks = (
        Chunk(
            chunk_id="chunk-title",
            doc_id="doc-schema-001",
            block_ids=("blk-title",),
            text="Maintenance Manual",
            semantic_role="title",
            embedding=(0.0, 0.1),
        ),
        Chunk(
            chunk_id="chunk-body",
            doc_id="doc-schema-001",
            block_ids=("blk-body",),
            text="Inspect the hydraulic pump before dispatch.",
            embedding=(0.1, 0.2),
        ),
        Chunk(
            chunk_id="chunk-table",
            doc_id="doc-schema-001",
            block_ids=("blk-table",),
            text="Part Qty Pump 1",
            semantic_role="table",
            embedding=(0.3, 0.4),
        ),
        Chunk(
            chunk_id="chunk-figure",
            doc_id="doc-schema-001",
            block_ids=("blk-figure",),
            text="Figure 1. Hydraulic workflow",
            semantic_role="image",
            embedding=(0.5, 0.6),
        ),
    )
    return {
        "job": job,
        "doc_id": "doc-schema-001",
        "blocks": blocks,
        "chunks": chunks,
        "provider_registry": _base_provider_registry(),
    }


def build_complex_sample_snapshot() -> dict[str, object]:
    """Return a complex multi-page snapshot with TOC, multi-column text, cross-page table, and embedded figure."""

    doc_id = "doc-complex-001"
    job = SimpleNamespace(
        job_id="job-complex-001",
        doc_id=doc_id,
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={
            "profile": "table-heavy",
            "local_provider_routing": _base_local_provider_routing(),
        },
    )
    blocks = (
        Block(
            block_id="cblk-toc",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="1. Introduction\n2. Specifications\n3. Maintenance",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "toc",
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
                "source_kind": "native_text",
                "reading_order": 1,
                "reading_order_confidence": 0.90,
            },
        ),
        Block(
            block_id="cblk-heading",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="2. Specifications",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "heading",
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
                "source_kind": "native_text",
                "reading_order": 1,
                "reading_order_confidence": 0.95,
                "heading_level": 1,
            },
        ),
        Block(
            block_id="cblk-col-left",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="The hydraulic system operates at 3000 PSI nominal pressure.",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [36, 72, 288, 400],
                "reading_order": 2,
                "confidence": 0.92,
                "source_kind": "native_text",
                "column_index": 0,
                "reading_order_confidence": 0.85,
            },
        ),
        Block(
            block_id="cblk-col-right",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="The cooling circuit maintains temperature below 85 degrees Celsius.",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [324, 72, 576, 400],
                "reading_order": 3,
                "confidence": 0.91,
                "source_kind": "native_text",
                "column_index": 1,
                "reading_order_confidence": 0.83,
            },
        ),
        Block(
            block_id="cblk-table-p2",
            doc_id=doc_id,
            type=BlockType.TABLE,
            content="Parameter | Min | Max\nPressure (PSI) | 2800 | 3200\nTemp (C) | 20 | 85\nFlow (L/min) | 10 | 25",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "table",
                "table_index": 1,
                "rows": 4,
                "cols": 3,
                "cells": [
                    ["Parameter", "Min", "Max"],
                    ["Pressure (PSI)", "2800", "3200"],
                    ["Temp (C)", "20", "85"],
                    ["Flow (L/min)", "10", "25"],
                ],
                "source_kind": "structured_table",
                "reading_order": 4,
            },
        ),
        Block(
            block_id="cblk-table-p3",
            doc_id=doc_id,
            type=BlockType.TABLE,
            content="Parameter | Min | Max\nVoltage (V) | 220 | 240\nCurrent (A) | 5 | 15",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": "table",
                "table_index": 1,
                "rows": 3,
                "cols": 3,
                "cells": [
                    ["Parameter", "Min", "Max"],
                    ["Voltage (V)", "220", "240"],
                    ["Current (A)", "5", "15"],
                ],
                "source_kind": "structured_table",
                "table_continuation": True,
                "reading_order": 1,
            },
        ),
        Block(
            block_id="cblk-figure",
            doc_id=doc_id,
            type=BlockType.IMAGE,
            content="Figure 2. System architecture diagram",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": "image",
                "bbox": [72, 200, 540, 500],
                "figure_kind": "diagram",
                "source_kind": "pdf_image",
                "caption_confidence": 0.92,
                "alt_text": "System architecture showing hydraulic and cooling subsystems",
                "reading_order": 2,
            },
        ),
        Block(
            block_id="cblk-caption",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Figure 2. System architecture diagram",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": "image_caption",
                "source_kind": "native_text",
                "reading_order": 3,
                "reading_order_confidence": 0.90,
            },
        ),
        Block(
            block_id="cblk-hf-p2",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Page 2",
            metadata={"page": 2, "parser": "pdf-text", "semantic_role": "header_footer"},
        ),
        Block(
            block_id="cblk-hf-p3",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Page 3",
            metadata={"page": 3, "parser": "pdf-text", "semantic_role": "header_footer"},
        ),
    )
    chunks = (
        Chunk(
            chunk_id="cchunk-toc",
            doc_id=doc_id,
            block_ids=("cblk-toc",),
            text="1. Introduction 2. Specifications 3. Maintenance",
            semantic_role="toc",
            embedding=(0.1, 0.2, 0.3),
        ),
        Chunk(
            chunk_id="cchunk-heading",
            doc_id=doc_id,
            block_ids=("cblk-heading",),
            text="2. Specifications",
            semantic_role="heading",
            embedding=(0.2, 0.3, 0.4),
        ),
        Chunk(
            chunk_id="cchunk-col-left",
            doc_id=doc_id,
            block_ids=("cblk-col-left",),
            text="The hydraulic system operates at 3000 PSI nominal pressure.",
            semantic_role="body_section",
            embedding=(0.3, 0.4, 0.5),
        ),
        Chunk(
            chunk_id="cchunk-col-right",
            doc_id=doc_id,
            block_ids=("cblk-col-right",),
            text="The cooling circuit maintains temperature below 85 degrees Celsius.",
            semantic_role="body_section",
            embedding=(0.4, 0.5, 0.6),
        ),
        Chunk(
            chunk_id="cchunk-table",
            doc_id=doc_id,
            block_ids=("cblk-table-p2", "cblk-table-p3"),
            text="Parameter Min Max Pressure PSI 2800 3200 Temp C 20 85 Flow L/min 10 25 Voltage V 220 240 Current A 5 15",
            semantic_role="table",
            embedding=(0.5, 0.6, 0.7),
        ),
        Chunk(
            chunk_id="cchunk-figure",
            doc_id=doc_id,
            block_ids=("cblk-figure", "cblk-caption"),
            text="Figure 2. System architecture diagram",
            semantic_role="image",
            embedding=(0.6, 0.7, 0.8),
        ),
    )
    return {
        "job": job,
        "doc_id": doc_id,
        "blocks": blocks,
        "chunks": chunks,
        "provider_registry": _base_provider_registry(),
    }


def build_anomaly_sample_snapshot() -> dict[str, object]:
    """Return a snapshot exercising anomaly paths: OCR degraded page, low-confidence reading order, CID garble, empty text page."""

    doc_id = "doc-anomaly-001"
    job = SimpleNamespace(
        job_id="job-anomaly-001",
        doc_id=doc_id,
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={
            "profile": "table-heavy",
            "local_provider_routing": _base_local_provider_routing(),
        },
    )
    blocks = (
        Block(
            block_id="ablk-ocr-degraded",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Th1s p4ge w4s OCR-pr0cessed w1th l0w c0nf1dence.",
            metadata={
                "page": 1,
                "parser": "ocr-gateway",
                "semantic_role": "body_section",
                "bbox": [36, 72, 576, 400],
                "reading_order": 1,
                "confidence": 0.42,
                "source_kind": "ocr_text",
                "ocr_provider_id": "ocr-gateway-http",
                "ocr_confidence": 0.42,
                "reading_order_confidence": 0.35,
                "cid_garble_ratio": 0.0,
            },
        ),
        Block(
            block_id="ablk-cid-garble",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="\ufffd\ufffd\ufffd Spec\ufffdfications and \ufffdperational \ufffdarameters",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [36, 72, 576, 200],
                "reading_order": 1,
                "confidence": 0.60,
                "source_kind": "native_text",
                "cid_garble_ratio": 0.38,
                "reading_order_confidence": 0.55,
            },
        ),
        Block(
            block_id="ablk-low-ro",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Scrambled column layout detected on this page.",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [36, 220, 576, 400],
                "reading_order": 3,
                "confidence": 0.50,
                "source_kind": "native_text",
                "reading_order_confidence": 0.28,
            },
        ),
        Block(
            block_id="ablk-empty-page",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [0, 0, 0, 0],
                "reading_order": 1,
                "confidence": 0.0,
                "source_kind": "native_text",
                "empty_text_page": True,
            },
        ),
        Block(
            block_id="ablk-artifact",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="<parse_artifact:empty_table>",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": "parse_artifact",
                "source_kind": "native_text",
            },
        ),
        Block(
            block_id="ablk-hf",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Page 1",
            metadata={"page": 1, "parser": "pdf-text", "semantic_role": "header_footer"},
        ),
    )
    chunks = (
        Chunk(
            chunk_id="achunk-ocr",
            doc_id=doc_id,
            block_ids=("ablk-ocr-degraded",),
            text="Th1s p4ge w4s OCR-pr0cessed w1th l0w c0nf1dence.",
            semantic_role="body_section",
            embedding=(0.1, 0.2),
        ),
        Chunk(
            chunk_id="achunk-cid",
            doc_id=doc_id,
            block_ids=("ablk-cid-garble",),
            text="\ufffd\ufffd\ufffd Spec\ufffdfications and \ufffdperational \ufffdarameters",
            semantic_role="body_section",
            embedding=(0.2, 0.3),
        ),
        Chunk(
            chunk_id="achunk-low-ro",
            doc_id=doc_id,
            block_ids=("ablk-low-ro",),
            text="Scrambled column layout detected on this page.",
            semantic_role="body_section",
            embedding=(0.3, 0.4),
        ),
    )
    return {
        "job": job,
        "doc_id": doc_id,
        "blocks": blocks,
        "chunks": chunks,
        "provider_registry": _base_provider_registry(),
    }


def build_part_rerun_sample_snapshot() -> dict[str, object]:
    """Return a snapshot exercising the part-rerun contract: previous_part_observation + rerun_comparison."""

    doc_id = "doc-rerun-sample-001"
    job = SimpleNamespace(
        job_id="job-rerun-current",
        doc_id=doc_id,
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={
            "profile": "table-heavy",
            "source_job_id": "job-rerun-previous",
            "local_provider_routing": _base_local_provider_routing(),
        },
    )
    blocks = (
        Block(
            block_id="rblk-body",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Rerun produced improved table extraction with complete cell coverage.",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "body_section",
                "bbox": [36, 72, 576, 200],
                "reading_order": 1,
                "confidence": 0.95,
                "page_width": 612.0,
                "page_height": 792.0,
                "rotation": 0,
                "source_kind": "native_text",
                "reading_order_confidence": 0.88,
            },
        ),
        Block(
            block_id="rblk-table",
            doc_id=doc_id,
            type=BlockType.TABLE,
            content="Item | Status\nPump A | OK\nPump B | Replaced",
            metadata={
                "page": 1,
                "parser": "pdf-text",
                "semantic_role": "table",
                "table_index": 1,
                "rows": 3,
                "cols": 2,
                "cells": [["Item", "Status"], ["Pump A", "OK"], ["Pump B", "Replaced"]],
                "source_kind": "structured_table",
                "reading_order": 2,
            },
        ),
    )
    chunks = (
        Chunk(
            chunk_id="rchunk-body",
            doc_id=doc_id,
            block_ids=("rblk-body",),
            text="Rerun produced improved table extraction with complete cell coverage.",
            semantic_role="body_section",
            embedding=(0.1, 0.2),
        ),
        Chunk(
            chunk_id="rchunk-table",
            doc_id=doc_id,
            block_ids=("rblk-table",),
            text="Item Status Pump A OK Pump B Replaced",
            semantic_role="table",
            embedding=(0.3, 0.4),
        ),
    )
    previous_part_observation = {
        "schema_version": "2026-06-part-observation",
        "part_id": f"{doc_id}-part-1",
        "job_id": "job-rerun-previous",
        "state": "warning",
        "raw_state": "done",
        "quality_signal_count": 2,
        "quality_signal_codes": ["rag_table_without_unit", "reading_order_low_confidence"],
        "quality_signal_page_numbers": [1],
        "provider_ids": ["pymupdf4llm-local"],
        "coverage_summary": {
            "pages_with_coverage_gaps": 1,
            "gap_unit_ids": [f"{doc_id}:ku:000003"],
            "unembedded_unit_count": 1,
            "text_page_coverage_ratio": 0.75,
            "unit_chunk_coverage_ratio": 0.80,
            "table_unit_coverage_ratio": 0.50,
        },
        "coverage_gap_count": 1,
        "coverage_gap_pages": [
            {"page_number": 1, "unit_ids": [f"{doc_id}:ku:000003"]},
        ],
        "rag_coverage_quality": {
            "status": "partial",
            "flags": ["rag_table_without_unit"],
            "recommended_action": "local_provider_rerun",
        },
        "selected_provider_id": "pymupdf4llm-local",
        "route_status": "selected",
    }
    rerun_comparison = {
        "schema_version": "2026-06-part-rerun-comparison",
        "status": "improved",
        "changed": True,
        "improved": True,
        "regressed": False,
        "improvement_axes": ["coverage_gaps", "gap_units", "table_unit_coverage_ratio"],
        "regression_axes": [],
        "previous_job_id": "job-rerun-previous",
        "current_job_id": "job-rerun-current",
        "previous_state": "warning",
        "current_state": "done",
        "previous_selected_provider_id": "pymupdf4llm-local",
        "current_selected_provider_id": "pdf-text",
        "provider_changed": True,
        "quality_signal_count_delta": -2,
        "coverage_gap_delta": -1,
        "gap_unit_count_delta": -1,
        "unembedded_unit_count_delta": -1,
        "text_page_coverage_ratio_delta": 0.25,
        "unit_chunk_coverage_ratio_delta": 0.20,
        "table_unit_coverage_ratio_delta": 0.50,
        "gap_unit_ids_added": [],
        "gap_unit_ids_removed": [f"{doc_id}:ku:000003"],
    }
    return {
        "job": job,
        "doc_id": doc_id,
        "blocks": blocks,
        "chunks": chunks,
        "provider_registry": _base_provider_registry(),
        # Runtime snapshots expose partitioned parse units as ``partition_parts``.
        # Keeping the sample on the same boundary is important: otherwise the
        # generic fallback path silently drops previous_part_observation and
        # rerun_comparison, so the P1 part-rerun contract is not actually
        # exercised by the sample payloads.
        "partition_parts": [
            {
                "part_id": f"{doc_id}-part-1",
                "parse_unit_id": f"{doc_id}-part-1",
                "page_start": 1,
                "page_end": 1,
                "state": "done",
                "rerun_supported": True,
                "previous_part_observation": previous_part_observation,
                "rerun_comparison": rerun_comparison,
            },
        ],
    }


def build_payload_contract_samples() -> dict[str, dict[str, Any]]:
    """Build representative payloads for each frozen document contract."""

    snapshot = build_sample_snapshot()
    structured = _document_projection(snapshot, projection="structured")
    return {
        "document-coverage": _document_projection(snapshot, projection="coverage"),
        "document-ir": _document_projection(snapshot, projection="ir"),
        "document-parts": document_parts_projection(structured),
        "document-providers": _document_providers_projection(snapshot),
        "document-quality": _document_quality_projection(snapshot),
        "document-reader": _document_projection(snapshot, projection="reader"),
    }


def build_complex_payload_contract_samples() -> dict[str, dict[str, Any]]:
    """Build complex multi-page payloads for each frozen document contract."""

    snapshot = build_complex_sample_snapshot()
    structured = _document_projection(snapshot, projection="structured")
    return {
        "document-coverage": _document_projection(snapshot, projection="coverage"),
        "document-ir": _document_projection(snapshot, projection="ir"),
        "document-parts": document_parts_projection(structured),
        "document-providers": _document_providers_projection(snapshot),
        "document-quality": _document_quality_projection(snapshot),
        "document-reader": _document_projection(snapshot, projection="reader"),
    }


def build_anomaly_payload_contract_samples() -> dict[str, dict[str, Any]]:
    """Build anomaly-path payloads exercising OCR degradation, CID garble, and empty pages."""

    snapshot = build_anomaly_sample_snapshot()
    structured = _document_projection(snapshot, projection="structured")
    return {
        "document-coverage": _document_projection(snapshot, projection="coverage"),
        "document-ir": _document_projection(snapshot, projection="ir"),
        "document-parts": document_parts_projection(structured),
        "document-providers": _document_providers_projection(snapshot),
        "document-quality": _document_quality_projection(snapshot),
        "document-reader": _document_projection(snapshot, projection="reader"),
    }


def build_part_rerun_payload_contract_samples() -> dict[str, dict[str, Any]]:
    """Build part-rerun payloads exercising previous_part_observation + rerun_comparison."""

    snapshot = build_part_rerun_sample_snapshot()
    structured = _document_projection(snapshot, projection="structured")
    return {
        "document-coverage": _document_projection(snapshot, projection="coverage"),
        "document-ir": _document_projection(snapshot, projection="ir"),
        "document-parts": document_parts_projection(structured),
        "document-providers": _document_providers_projection(snapshot),
        "document-quality": _document_quality_projection(snapshot),
        "document-reader": _document_projection(snapshot, projection="reader"),
    }
