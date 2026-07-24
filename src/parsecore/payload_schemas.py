from __future__ import annotations

from copy import deepcopy
from typing import Any

from .api_payloads import (
    DOCUMENT_SCHEMA_VERSION,
    PROVIDER_USAGE_SCHEMA_VERSION,
    QUALITY_GATE_SCHEMA_VERSION,
    READER_SCHEMA_VERSION,
)
from .ir import COVERAGE_SCHEMA_VERSION, IR_SCHEMA_VERSION


JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
PAYLOAD_SCHEMA_REGISTRY_VERSION = "2026-06-payload-schema-registry"
_SCHEMA_BASE_URI = "https://parsecore.local/schemas"


def payload_schema_registry() -> dict[str, Any]:
    descriptors = [_schema_descriptor(name) for name in payload_schema_names()]
    return {
        "schema_version": PAYLOAD_SCHEMA_REGISTRY_VERSION,
        "schemas": descriptors,
        "summary": {
            "total": len(descriptors),
            "document_projection_count": sum(
                1 for descriptor in descriptors if str(descriptor.get("category") or "") == "document_projection"
            ),
        },
    }


def payload_schema_names() -> tuple[str, ...]:
    return tuple(_SCHEMAS)


def payload_schema(name: str) -> dict[str, Any]:
    normalized = str(name or "").strip().lower()
    schema = _SCHEMAS.get(normalized)
    if schema is None:
        raise KeyError(normalized)
    return deepcopy(schema)


def _schema_descriptor(name: str) -> dict[str, Any]:
    schema = _SCHEMAS[name]
    meta = schema.get("x-parsecore") or {}
    return {
        "name": name,
        "schema_version": str(meta.get("schema_version") or ""),
        "projection": meta.get("projection"),
        "category": str(meta.get("category") or ""),
        "title": str(schema.get("title") or ""),
        "description": str(schema.get("description") or ""),
        "endpoint": f"/v1/parse/schemas/{name}",
    }


def _string_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string"}


def _integer_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["integer", "null"] if nullable else "integer"}


def _number_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["number", "null"] if nullable else "number"}


def _boolean_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {"type": ["boolean", "null"] if nullable else "boolean"}


def _string_list_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": _string_schema(),
    }


def _page_span_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "items": _integer_schema(),
        "minItems": 2,
        "maxItems": 2,
    }


def _bbox_schema() -> dict[str, Any]:
    return {
        "type": ["array", "null"],
        "items": _number_schema(),
        "minItems": 4,
        "maxItems": 4,
    }


def _generic_object_schema(*, nullable: bool = False) -> dict[str, Any]:
    return {
        "type": ["object", "null"] if nullable else "object",
        "additionalProperties": True,
    }


def _any_schema() -> dict[str, Any]:
    return {}


def _quality_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["score", "flags", "warnings", "total_cid_tokens"],
        "properties": {
            "score": _number_schema(),
            "flags": _string_list_schema(),
            "warnings": _string_list_schema(),
            "total_cid_tokens": _integer_schema(),
            "total_pdf_name_tokens": _integer_schema(),
            "recommended_action": _string_schema(nullable=True),
            "ocr_failed_pages": _integer_schema(),
            "suspect_signature_pages": _integer_schema(),
        },
        "additionalProperties": True,
    }


def _quality_signal_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["code", "severity", "message"],
        "properties": {
            "code": _string_schema(),
            "severity": _string_schema(),
            "message": _string_schema(),
            "page_number": _integer_schema(nullable=True),
            "block_id": _string_schema(nullable=True),
            "table_id": _string_schema(nullable=True),
            "figure_id": _string_schema(nullable=True),
            "detail": _generic_object_schema(nullable=True),
        },
        "additionalProperties": True,
    }


def _coverage_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_number",
            "parsed_text_chars",
            "table_count",
            "figure_count",
            "block_count",
            "indexable_unit_count",
            "chunked_unit_count",
            "unchunked_unit_ids",
            "table_ids_without_units",
            "figure_ids_missing_caption",
            "chunk_ids",
            "embedded",
            "missing_reason",
            "provider_ids",
            "reading_order_confidence",
            "quality_signal_codes",
        ],
        "properties": {
            "page_number": _integer_schema(),
            "parsed_text_chars": _integer_schema(),
            "table_count": _integer_schema(),
            "figure_count": _integer_schema(),
            "block_count": _integer_schema(),
            "unit_ids": _string_list_schema(),
            "indexable_unit_ids": _string_list_schema(),
            "skipped_unit_ids": _string_list_schema(),
            "indexable_unit_count": _integer_schema(),
            "chunked_unit_count": _integer_schema(),
            "unchunked_unit_ids": _string_list_schema(),
            "unembedded_unit_ids": _string_list_schema(),
            "table_ids_without_units": _string_list_schema(),
            "figure_ids_missing_caption": _string_list_schema(),
            "chunk_ids": _string_list_schema(),
            "embedded": _boolean_schema(),
            "missing_reason": _string_schema(nullable=True),
            "provider_ids": _string_list_schema(),
            "reading_order_confidence": _number_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _coverage_gap_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_number",
            "missing_reason",
            "unit_ids",
            "indexable_unit_ids",
            "unchunked_unit_ids",
            "unembedded_unit_ids",
            "table_ids_without_units",
            "figure_ids_missing_caption",
            "quality_signal_codes",
        ],
        "properties": {
            "page_number": _integer_schema(),
            "missing_reason": _string_schema(nullable=True),
            "unit_ids": _string_list_schema(),
            "indexable_unit_ids": _string_list_schema(),
            "unchunked_unit_ids": _string_list_schema(),
            "unembedded_unit_ids": _string_list_schema(),
            "table_ids_without_units": _string_list_schema(),
            "figure_ids_missing_caption": _string_list_schema(),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _coverage_unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "unit_id",
            "doc_id",
            "source_item_ids",
            "source_block_ids",
            "source_table_ids",
            "page_span",
            "text",
            "unit_type",
            "semantic_role",
            "should_index_for_rag",
            "skip_reason",
            "quality_flags",
            "chunk_ids",
            "chunk_count",
            "embedded_chunk_count",
            "embedded",
            "embedding_state",
            "coverage_state",
            "missing_reason",
            "quality_signal_codes",
        ],
        "properties": {
            "unit_id": _string_schema(),
            "doc_id": _string_schema(),
            "source_item_ids": _string_list_schema(),
            "source_block_ids": _string_list_schema(),
            "source_table_ids": _string_list_schema(),
            "page_span": _page_span_schema(),
            "text": _string_schema(),
            "unit_type": _string_schema(),
            "semantic_role": _string_schema(),
            "should_index_for_rag": _boolean_schema(),
            "skip_reason": _string_schema(nullable=True),
            "quality_flags": _string_list_schema(),
            "chunk_ids": _string_list_schema(),
            "chunk_count": _integer_schema(),
            "embedded_chunk_count": _integer_schema(),
            "embedded": _boolean_schema(),
            "embedding_model": _string_schema(nullable=True),
            "embedding_state": _string_schema(),
            "embedding_error_category": _string_schema(nullable=True),
            "coverage_state": _string_schema(),
            "missing_reason": _string_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _coverage_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "total_pages",
            "pages_with_parsed_text",
            "pages_with_indexable_units",
            "pages_missing_rag_units",
            "pages_missing_chunks",
            "pages_chunks_not_embedded",
            "pages_with_coverage_gaps",
            "pages_table_without_units",
            "pages_figure_caption_missing",
            "total_indexable_units",
            "total_chunked_units",
            "text_page_coverage_ratio",
            "unit_chunk_coverage_ratio",
            "table_unit_coverage_ratio",
        ],
        "properties": {
            "total_pages": _integer_schema(),
            "pages_with_parsed_text": _integer_schema(),
            "pages_with_indexable_units": _integer_schema(),
            "pages_missing_rag_units": _integer_schema(),
            "pages_missing_chunks": _integer_schema(),
            "pages_chunks_not_embedded": _integer_schema(),
            "pages_with_coverage_gaps": _integer_schema(),
            "pages_table_without_units": _integer_schema(),
            "pages_figure_caption_missing": _integer_schema(),
            "total_indexable_units": _integer_schema(),
            "total_chunked_units": _integer_schema(),
            "total_unit_count": _integer_schema(),
            "skipped_unit_count": _integer_schema(),
            "embedded_unit_count": _integer_schema(),
            "unembedded_unit_count": _integer_schema(),
            "gap_unit_ids": _string_list_schema(),
            "gap_pages": {
                "type": "array",
                "items": _coverage_gap_page_schema(),
            },
            "text_page_coverage_ratio": _number_schema(),
            "unit_chunk_coverage_ratio": _number_schema(),
            "table_unit_coverage_ratio": _number_schema(),
        },
        "additionalProperties": False,
    }


def _coverage_container_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["summary", "pages"],
        "properties": {
            "summary": _coverage_summary_schema(),
            "pages": {
                "type": "array",
                "items": _coverage_page_schema(),
            },
            "units": {
                "type": "array",
                "items": _coverage_unit_schema(),
            },
        },
        "additionalProperties": False,
    }


def _rag_coverage_quality_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "score",
            "gate",
            "flags",
            "warnings",
            "recommended_action",
            "page_count",
            "pages_with_coverage_gaps",
            "pages_missing_rag_units",
            "pages_missing_chunks",
            "pages_chunks_not_embedded",
            "pages_table_without_units",
            "pages_figure_caption_missing",
            "total_indexable_units",
            "total_chunked_units",
            "unit_chunk_coverage_ratio",
            "text_page_coverage_ratio",
        ],
        "properties": {
            "score": _number_schema(),
            "gate": _string_schema(),
            "flags": _string_list_schema(),
            "warnings": _string_list_schema(),
            "recommended_action": _string_schema(nullable=True),
            "page_count": _integer_schema(),
            "pages_with_coverage_gaps": _integer_schema(),
            "pages_missing_rag_units": _integer_schema(),
            "pages_missing_chunks": _integer_schema(),
            "pages_chunks_not_embedded": _integer_schema(),
            "pages_table_without_units": _integer_schema(),
            "pages_figure_caption_missing": _integer_schema(),
            "total_indexable_units": _integer_schema(),
            "total_chunked_units": _integer_schema(),
            "unit_chunk_coverage_ratio": _number_schema(),
            "text_page_coverage_ratio": _number_schema(),
        },
        "additionalProperties": False,
    }


def _provenance_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["provider_id", "provider_version", "adapter_version", "source_page_number"],
        "properties": {
            "provider_id": _string_schema(),
            "provider_version": _string_schema(),
            "adapter_version": _string_schema(),
            "source_page_number": _integer_schema(),
            "provider_elapsed_s": _number_schema(),
            "provider_memory_mb": _number_schema(),
            "reading_order_confidence": _number_schema(),
        },
        "additionalProperties": True,
    }


def _ir_provider_entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["provider_id", "provider_version", "adapter_version", "block_count"],
        "properties": {
            "provider_id": _string_schema(),
            "provider_version": _string_schema(),
            "adapter_version": _string_schema(),
            "block_count": _integer_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_admission_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["route_mode", "gate_status", "gate_checks", "route_ready"],
        "properties": {
            "route_mode": _string_schema(),
            "gate_status": _string_schema(),
            "gate_checks": _string_list_schema(),
            "route_ready": _boolean_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_registry_entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "id",
            "enabled",
            "priority",
            "media_types",
            "extensions",
            "profiles",
            "capabilities",
            "admission",
            "options",
        ],
        "properties": {
            "id": _string_schema(),
            "enabled": _boolean_schema(),
            "priority": _integer_schema(),
            "media_types": _string_list_schema(),
            "extensions": _string_list_schema(),
            "profiles": _string_list_schema(),
            "capabilities": _string_list_schema(),
            "admission": _local_provider_admission_schema(),
            "options": _generic_object_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_registry_routing_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["enabled", "fallback_to_default", "include_disabled", "routing_policy"],
        "properties": {
            "enabled": _boolean_schema(),
            "fallback_to_default": _boolean_schema(),
            "include_disabled": _boolean_schema(),
            "routing_policy": _string_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_registry_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "total",
            "enabled",
            "disabled",
            "route_ready",
            "evaluation_only",
            "gate_pending",
            "gate_failed",
        ],
        "properties": {
            "total": _integer_schema(),
            "enabled": _integer_schema(),
            "disabled": _integer_schema(),
            "route_ready": _integer_schema(),
            "evaluation_only": _integer_schema(),
            "gate_pending": _integer_schema(),
            "gate_failed": _integer_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_registry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["schema_version", "routing", "local_parsers", "summary"],
        "properties": {
            "schema_version": {"const": "2026-06-local-provider-registry"},
            "routing": _local_provider_registry_routing_schema(),
            "local_parsers": {
                "type": "array",
                "items": _local_provider_registry_entry_schema(),
            },
            "summary": _local_provider_registry_summary_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_route_request_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "media_type",
            "extension",
            "file_name",
            "profile",
            "required_capabilities",
            "include_disabled",
        ],
        "properties": {
            "media_type": _string_schema(nullable=True),
            "extension": _string_schema(nullable=True),
            "file_name": _string_schema(nullable=True),
            "profile": _string_schema(nullable=True),
            "required_capabilities": _string_list_schema(),
            "include_disabled": _boolean_schema(),
        },
        "additionalProperties": False,
    }


def _local_provider_routing_decision_schema(*, nullable: bool = False) -> dict[str, Any]:
    schema = {
        "type": "object",
        "required": [
            "schema_version",
            "enabled",
            "routing_policy",
            "route_status",
            "selected_provider_id",
            "selected_route_role",
            "primary_provider_id",
            "fallback_provider_ids",
            "eligible_provider_ids",
            "excluded_provider_ids",
            "fallback_to_default",
            "requested",
        ],
        "properties": {
            "schema_version": {"const": "2026-06-local-provider-routing-decision"},
            "enabled": _boolean_schema(),
            "routing_policy": _string_schema(nullable=True),
            "route_status": _string_schema(),
            "selected_provider_id": _string_schema(nullable=True),
            "selected_route_role": _string_schema(nullable=True),
            "primary_provider_id": _string_schema(nullable=True),
            "fallback_provider_ids": _string_list_schema(),
            "eligible_provider_ids": _string_list_schema(),
            "excluded_provider_ids": _string_list_schema(),
            "fallback_to_default": _boolean_schema(),
            "requested": _local_provider_route_request_schema(),
        },
        "additionalProperties": False,
    }
    if nullable:
        schema["type"] = ["object", "null"]
    return schema


def _ir_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_id",
            "page_number",
            "page_type",
            "width",
            "height",
            "rotation",
            "source_kind",
            "block_ids",
            "quality_flags",
            "reading_order_confidence",
        ],
        "properties": {
            "page_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_type": _string_schema(),
            "width": _number_schema(nullable=True),
            "height": _number_schema(nullable=True),
            "rotation": _integer_schema(),
            "source_kind": _string_schema(),
            "block_ids": _string_list_schema(),
            "quality_flags": _string_list_schema(),
            "reading_order_confidence": _number_schema(nullable=True),
        },
        "additionalProperties": False,
    }


def _ir_block_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "block_id",
            "page_number",
            "page_span",
            "type",
            "semantic_role",
            "text",
            "bbox",
            "reading_order",
            "confidence",
            "source_kind",
            "display_kind",
            "reader_policy",
            "index_policy",
            "alt_text",
            "quality_flags",
            "provenance",
        ],
        "properties": {
            "block_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_span": _page_span_schema(),
            "type": _string_schema(),
            "semantic_role": _string_schema(),
            "text": _string_schema(),
            "bbox": _bbox_schema(),
            "reading_order": _integer_schema(),
            "confidence": _number_schema(nullable=True),
            "source_kind": _string_schema(),
            "display_kind": _string_schema(),
            "reader_policy": _string_schema(),
            "index_policy": _string_schema(),
            "alt_text": _string_schema(),
            "quality_flags": _string_list_schema(),
            "provenance": _provenance_schema(),
            "lines": {
                "type": "array",
                "items": _generic_object_schema(),
            },
            "words": {
                "type": "array",
                "items": _generic_object_schema(),
            },
        },
        "additionalProperties": False,
    }


def _ir_table_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "table_id",
            "source_doc_id",
            "part_doc_id",
            "block_id",
            "page_number",
            "page_span",
            "semantic_role",
            "source_kind",
            "bbox",
            "rows",
            "cols",
            "header_rows",
            "cells",
            "caption",
            "confidence",
            "reader_policy",
            "index_policy",
            "quality_flags",
            "provenance",
        ],
        "properties": {
            "table_id": _string_schema(),
            "source_doc_id": _string_schema(),
            "part_doc_id": _string_schema(),
            "block_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_span": _page_span_schema(),
            "semantic_role": _string_schema(),
            "source_kind": _string_schema(),
            "bbox": _bbox_schema(),
            "rows": _integer_schema(),
            "cols": _integer_schema(),
            "header_rows": _integer_schema(),
            "cells": {
                "type": "array",
                "items": _generic_object_schema(),
            },
            "caption": _string_schema(),
            "confidence": _number_schema(nullable=True),
            "reader_policy": _string_schema(),
            "index_policy": _string_schema(),
            "quality_flags": _string_list_schema(),
            "provenance": _generic_object_schema(),
        },
        "additionalProperties": False,
    }


def _ir_figure_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "figure_id",
            "block_id",
            "page_number",
            "page_span",
            "semantic_role",
            "source_kind",
            "bbox",
            "figure_type",
            "caption",
            "alt_text",
            "confidence",
            "reader_policy",
            "index_policy",
            "quality_flags",
            "provenance",
        ],
        "properties": {
            "figure_id": _string_schema(),
            "block_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_span": _page_span_schema(),
            "semantic_role": _string_schema(),
            "source_kind": _string_schema(),
            "bbox": _bbox_schema(),
            "figure_type": _string_schema(),
            "caption": _string_schema(),
            "alt_text": _string_schema(),
            "confidence": _number_schema(nullable=True),
            "reader_policy": _string_schema(),
            "index_policy": _string_schema(),
            "quality_flags": _string_list_schema(),
            "provenance": _generic_object_schema(),
        },
        "additionalProperties": False,
    }


def _knowledge_unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "unit_id",
            "doc_id",
            "source_block_ids",
            "source_table_ids",
            "page_span",
            "text",
            "unit_type",
            "semantic_role",
            "should_index_for_rag",
            "skip_reason",
            "quality_flags",
            "chunk_ids",
            "embedding_state",
        ],
        "properties": {
            "unit_id": _string_schema(),
            "doc_id": _string_schema(),
            "source_item_ids": _string_list_schema(),
            "source_block_ids": _string_list_schema(),
            "source_table_ids": _string_list_schema(),
            "page_span": _page_span_schema(),
            "text": _string_schema(),
            "unit_type": _string_schema(),
            "semantic_role": _string_schema(),
            "should_index_for_rag": _boolean_schema(),
            "skip_reason": _string_schema(nullable=True),
            "quality_flags": _string_list_schema(),
            "chunk_ids": _string_list_schema(),
            "chunk_count": _integer_schema(),
            "embedded_chunk_count": _integer_schema(),
            "embedded": _boolean_schema(),
            "embedding_model": _string_schema(nullable=True),
            "embedding_state": _string_schema(),
            "embedding_error_category": _string_schema(nullable=True),
            "coverage_state": _string_schema(),
            "missing_reason": _string_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": True,
    }


def _reader_unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "unit_id",
            "unit_type",
            "semantic_role",
            "page_span",
            "text",
            "source_item_ids",
            "source_block_ids",
            "source_table_ids",
            "should_index_for_rag",
            "skip_reason",
            "quality_flags",
            "chunk_ids",
            "embedding_state",
        ],
        "properties": {
            "unit_id": _string_schema(),
            "unit_type": _string_schema(),
            "semantic_role": _string_schema(),
            "page_span": _page_span_schema(),
            "text": _string_schema(),
            "source_item_ids": _string_list_schema(),
            "source_block_ids": _string_list_schema(),
            "source_table_ids": _string_list_schema(),
            "should_index_for_rag": _boolean_schema(),
            "skip_reason": _string_schema(nullable=True),
            "quality_flags": _string_list_schema(),
            "chunk_ids": _string_list_schema(),
            "chunk_count": _integer_schema(),
            "embedded_chunk_count": _integer_schema(),
            "embedded": _boolean_schema(),
            "embedding_model": _string_schema(nullable=True),
            "embedding_state": _string_schema(),
            "embedding_error_category": _string_schema(nullable=True),
            "coverage_state": _string_schema(),
            "missing_reason": _string_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _reader_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_id",
            "page_number",
            "page_type",
            "width",
            "height",
            "rotation",
            "source_kind",
            "block_ids",
            "reader_block_ids",
            "reader_block_count",
            "hidden_block_count",
            "quality_flags",
            "reading_order_confidence",
            "quality_signal_codes",
            "coverage_missing_reason",
            "provider_ids",
        ],
        "properties": {
            "page_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_type": _string_schema(),
            "width": _number_schema(nullable=True),
            "height": _number_schema(nullable=True),
            "rotation": _integer_schema(),
            "source_kind": _string_schema(),
            "block_ids": _string_list_schema(),
            "reader_block_ids": _string_list_schema(),
            "reader_block_count": _integer_schema(),
            "hidden_block_count": _integer_schema(),
            "quality_flags": _string_list_schema(),
            "reading_order_confidence": _number_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
            "coverage_missing_reason": _string_schema(nullable=True),
            "provider_ids": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _reader_block_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "reader_block_id",
            "page_number",
            "page_span",
            "type",
            "display_kind",
            "reader_policy",
            "semantic_role",
            "index_policy",
            "text",
            "rag_text",
            "source_unit_ids",
            "source_block_ids",
            "source_table_ids",
            "source_figure_ids",
            "rag_chunk_ids",
            "should_index_for_rag",
            "knowledge_units",
            "bbox",
            "reading_order",
            "source_kind",
            "confidence",
            "alt_text",
            "quality_flags",
            "provenance",
            "quality_signal_codes",
        ],
        "properties": {
            "reader_block_id": _string_schema(),
            "page_number": _integer_schema(),
            "page_span": _page_span_schema(),
            "type": _string_schema(),
            "display_kind": _string_schema(),
            "reader_policy": _string_schema(),
            "semantic_role": _string_schema(),
            "index_policy": _string_schema(),
            "text": _string_schema(),
            "rag_text": _string_schema(),
            "source_unit_ids": _string_list_schema(),
            "source_block_ids": _string_list_schema(),
            "source_table_ids": _string_list_schema(),
            "source_figure_ids": _string_list_schema(),
            "rag_chunk_ids": _string_list_schema(),
            "should_index_for_rag": _boolean_schema(),
            "knowledge_units": {
                "type": "array",
                "items": _reader_unit_schema(),
            },
            "bbox": _bbox_schema(),
            "reading_order": _integer_schema(),
            "source_kind": _string_schema(),
            "confidence": _number_schema(nullable=True),
            "alt_text": _string_schema(),
            "quality_flags": _string_list_schema(),
            "provenance": _generic_object_schema(),
            "quality_signal_codes": _string_list_schema(),
            "table": _generic_object_schema(),
            "figure": _generic_object_schema(),
            "lines": {
                "type": "array",
                "items": _generic_object_schema(),
            },
            "words": {
                "type": "array",
                "items": _generic_object_schema(),
            },
        },
        "additionalProperties": False,
    }


def _reader_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_count",
            "block_count",
            "hidden_block_count",
            "by_type",
            "table_blocks",
            "figure_blocks",
            "pages_with_quality_signals",
        ],
        "properties": {
            "page_count": _integer_schema(),
            "block_count": _integer_schema(),
            "hidden_block_count": _integer_schema(),
            "by_type": {
                "type": "object",
                "additionalProperties": _integer_schema(),
            },
            "table_blocks": _integer_schema(),
            "figure_blocks": _integer_schema(),
            "pages_with_quality_signals": _integer_schema(),
        },
        "additionalProperties": False,
    }


def _counter_map_schema(*, value_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": value_schema or _integer_schema(),
    }


def _quality_attention_part_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "part_id",
            "state",
            "page_range",
            "quality_signal_codes",
            "coverage_gap_count",
            "coverage_gap_unit_count",
            "gap_unit_ids",
            "unembedded_unit_count",
            "selected_provider_id",
            "recommended_focus",
            "rerun_status",
            "gap_unit_count_delta",
            "gap_unit_ids_added",
            "gap_unit_ids_removed",
            "provider_changed",
            "action_suggestions",
        ],
        "properties": {
            "part_id": _string_schema(),
            "state": _string_schema(),
            "page_range": {
                "type": "object",
                "required": ["start", "end"],
                "properties": {"start": _integer_schema(), "end": _integer_schema()},
                "additionalProperties": False,
            },
            "quality_signal_codes": _string_list_schema(),
            "coverage_gap_count": _integer_schema(),
            "coverage_gap_unit_count": _integer_schema(),
            "gap_unit_ids": _string_list_schema(),
            "unembedded_unit_count": _integer_schema(),
            "selected_provider_id": _string_schema(nullable=True),
            "recommended_focus": _string_schema(nullable=True),
            "rerun_status": _string_schema(nullable=True),
            "gap_unit_count_delta": _integer_schema(nullable=True),
            "gap_unit_ids_added": _string_list_schema(),
            "gap_unit_ids_removed": _string_list_schema(),
            "provider_changed": _boolean_schema(),
            "action_suggestions": {
                "type": "array",
                "items": _action_suggestion_schema(),
            },
        },
        "additionalProperties": False,
    }


def _quality_signal_summary_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["total", "by_severity", "by_code"],
        "properties": {
            "total": _integer_schema(),
            "by_severity": _counter_map_schema(),
            "by_code": _counter_map_schema(),
        },
        "additionalProperties": False,
    }


def _action_suggestion_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "action_id",
            "label",
            "method",
            "endpoint",
            "scope",
            "reason_codes",
            "auto_execute",
        ],
        "properties": {
            "action_id": _string_schema(),
            "label": _string_schema(),
            "method": _string_schema(),
            "endpoint": _string_schema(),
            "scope": _string_schema(),
            "reason_codes": _string_list_schema(),
            "auto_execute": _boolean_schema(),
            "payload": _generic_object_schema(),
            "params": _generic_object_schema(),
            "context": _generic_object_schema(),
        },
        "additionalProperties": False,
    }


def _quality_gate_schema(*, include_provider_comparison: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "schema_version": {"const": QUALITY_GATE_SCHEMA_VERSION},
        "enabled": _boolean_schema(),
        "gate": _string_schema(),
        "passed": _boolean_schema(),
        "blocking": _boolean_schema(),
        "enforcement": _string_schema(),
        "recommended_action": _string_schema(nullable=True),
        "flags": _string_list_schema(),
        "warnings": _string_list_schema(),
        "thresholds": _generic_object_schema(),
        "observed": _generic_object_schema(),
        "actions": _generic_object_schema(),
        "action_suggestions": {
            "type": "array",
            "items": _action_suggestion_schema(),
        },
    }
    required = list(properties)
    if include_provider_comparison:
        properties["provider_comparison"] = _generic_object_schema()
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": include_provider_comparison,
    }


def _provider_usage_page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "page_number",
            "provider_ids",
            "block_count",
            "table_count",
            "figure_count",
            "coverage_missing_reason",
            "quality_signal_codes",
        ],
        "properties": {
            "page_number": _integer_schema(),
            "provider_ids": _string_list_schema(),
            "block_count": _integer_schema(),
            "table_count": _integer_schema(),
            "figure_count": _integer_schema(),
            "coverage_missing_reason": _string_schema(nullable=True),
            "quality_signal_codes": _string_list_schema(),
        },
        "additionalProperties": False,
    }


def _provider_usage_entry_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "provider_id",
            "provider_version",
            "adapter_version",
            "block_count",
            "table_count",
            "figure_count",
            "coverage_page_count",
            "coverage_gap_count",
            "page_numbers",
            "page_count",
            "block_types",
            "source_kinds",
            "reader_policies",
            "index_policies",
            "coverage_missing_reasons",
            "quality_signal_codes",
            "provider_elapsed_s",
            "provider_elapsed_page_count",
            "provider_memory_mb",
            "provider_memory_page_count",
            "reading_order_confidence",
            "reading_order_confidence_page_count",
        ],
        "properties": {
            "provider_id": _string_schema(),
            "provider_version": _string_schema(),
            "adapter_version": _string_schema(),
            "block_count": _integer_schema(),
            "table_count": _integer_schema(),
            "figure_count": _integer_schema(),
            "coverage_page_count": _integer_schema(),
            "coverage_gap_count": _integer_schema(),
            "page_numbers": {
                "type": "array",
                "items": _integer_schema(),
            },
            "page_count": _integer_schema(),
            "block_types": _counter_map_schema(),
            "source_kinds": _counter_map_schema(),
            "reader_policies": _counter_map_schema(),
            "index_policies": _counter_map_schema(),
            "coverage_missing_reasons": _counter_map_schema(),
            "quality_signal_codes": _string_list_schema(),
            "provider_elapsed_s": _number_schema(nullable=True),
            "provider_elapsed_page_count": _integer_schema(),
            "provider_memory_mb": _number_schema(nullable=True),
            "provider_memory_page_count": _integer_schema(),
            "reading_order_confidence": _number_schema(nullable=True),
            "reading_order_confidence_page_count": _integer_schema(),
        },
        "additionalProperties": False,
    }


def _provider_comparison_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["schema_version", "primary_provider_id", "best_provider_id", "summary", "rankings"],
        "properties": {
            "schema_version": {"const": "2026-06-provider-comparison"},
            "primary_provider_id": _string_schema(nullable=True),
            "best_provider_id": _string_schema(nullable=True),
            "summary": _generic_object_schema(),
            "rankings": {
                "type": "array",
                "items": _generic_object_schema(),
            },
        },
        "additionalProperties": False,
    }


def _parse_unit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "parse_unit_id",
            "source_doc_id",
            "part_doc_id",
            "part_index",
            "page_start",
            "page_end",
            "state",
            "table_count",
            "quality_signal_count",
            "coverage_summary",
            "coverage_gap_pages",
            "rag_coverage_quality",
        ],
        "properties": {
            "parse_unit_id": _string_schema(),
            "part_id": _string_schema(),
            "source_doc_id": _string_schema(),
            "part_doc_id": _string_schema(),
            "part_index": _integer_schema(),
            "source_type": _string_schema(),
            "page_start": _integer_schema(),
            "page_end": _integer_schema(),
            "state": _string_schema(),
            "job_id": _string_schema(nullable=True),
            "table_count": _integer_schema(),
            "quality_signal_count": _integer_schema(),
            "quality_signal_codes": _string_list_schema(),
            "quality_signal_page_numbers": {
                "type": "array",
                "items": _integer_schema(),
            },
            "rerun_supported": _boolean_schema(),
            "last_error": _any_schema(),
            "provider_ids": _string_list_schema(),
            "provider_route_plan": _generic_object_schema(nullable=True),
            "local_provider_routing": _generic_object_schema(nullable=True),
            "coverage_summary": {
                "type": ["object", "null"],
                **{k: v for k, v in _coverage_summary_schema().items() if k != "type"},
            },
            "coverage_gap_pages": {
                "type": "array",
                "items": _coverage_gap_page_schema(),
            },
            "rag_coverage_quality": {
                "type": ["object", "null"],
                **{k: v for k, v in _rag_coverage_quality_schema().items() if k != "type"},
            },
            "previous_part_observation": _generic_object_schema(),
            "rerun_comparison": _generic_object_schema(),
        },
        "additionalProperties": False,
    }


def _coverage_document_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-coverage.json",
        "title": "ParseCore Coverage Projection",
        "description": "Stable page-level RAG coverage audit contract derived from Parse IR.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "state",
            "coverage",
            "quality_signals",
            "quality_summary",
            "rag_coverage_quality",
            "index_manifest",
            "local_provider_routing",
            "quality_gate",
        ],
        "properties": {
            "schema_version": {"const": COVERAGE_SCHEMA_VERSION},
            "projection": {"const": "coverage"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "state": _string_schema(),
            "coverage": _coverage_container_schema(),
            "quality_signals": {
                "type": "array",
                "items": _quality_signal_schema(),
            },
            "quality_summary": _quality_signal_summary_schema(),
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "index_manifest": _generic_object_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(nullable=True),
            "quality_gate": _quality_gate_schema(),
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-coverage",
            "category": "document_projection",
            "projection": "coverage",
            "schema_version": COVERAGE_SCHEMA_VERSION,
        },
    }


def _providers_document_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-providers.json",
        "title": "ParseCore Provider Usage Projection",
        "description": "Stable provider-level diagnostics contract for local Provider usage and comparison.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "provider_registry",
            "summary",
            "providers",
            "pages",
            "comparison_report",
            "comparison_actions",
            "rag_coverage_quality",
            "quality_gate",
        ],
        "properties": {
            "schema_version": {"const": PROVIDER_USAGE_SCHEMA_VERSION},
            "projection": {"const": "providers"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "profile_resolution": _generic_object_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(nullable=True),
            "state": _string_schema(),
            "provider_registry": _local_provider_registry_schema(),
            "summary": _generic_object_schema(),
            "providers": {
                "type": "array",
                "items": _provider_usage_entry_schema(),
            },
            "pages": {
                "type": "array",
                "items": _provider_usage_page_schema(),
            },
            "comparison_report": _provider_comparison_schema(),
            "comparison_actions": {
                "type": "array",
                "items": _action_suggestion_schema(),
            },
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "quality_gate": _quality_gate_schema(include_provider_comparison=True),
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-providers",
            "category": "document_projection",
            "projection": "providers",
            "schema_version": PROVIDER_USAGE_SCHEMA_VERSION,
        },
    }


def _parts_part_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "part_id",
            "parse_unit_id",
            "source_doc_id",
            "part_doc_id",
            "part_index",
            "source_type",
            "page_start",
            "page_end",
            "page_range",
            "state",
            "raw_state",
            "table_count",
            "quality_signal_count",
            "quality_signal_codes",
            "quality_signal_page_numbers",
            "severity_counts",
            "rerun_supported",
            "provider_ids",
            "action_suggestions",
            "diagnostics",
        ],
        "properties": {
            "part_id": _string_schema(),
            "parse_unit_id": _string_schema(),
            "source_doc_id": _string_schema(),
            "part_doc_id": _string_schema(),
            "part_index": _integer_schema(),
            "source_type": _string_schema(),
            "page_start": _integer_schema(),
            "page_end": _integer_schema(),
            "page_range": {
                "type": "object",
                "required": ["start", "end"],
                "properties": {"start": _integer_schema(), "end": _integer_schema()},
                "additionalProperties": False,
            },
            "state": _string_schema(),
            "raw_state": _string_schema(),
            "table_count": _integer_schema(),
            "quality_signal_count": _integer_schema(),
            "quality_signal_codes": _string_list_schema(),
            "quality_signal_page_numbers": {
                "type": "array",
                "items": _integer_schema(),
            },
            "severity_counts": _counter_map_schema(),
            "rerun_supported": _boolean_schema(),
            "provider_ids": _string_list_schema(),
            "action_suggestions": {
                "type": "array",
                "items": _action_suggestion_schema(),
            },
            "coverage_summary": _coverage_summary_schema(),
            "coverage_gap_count": _integer_schema(),
            "coverage_gap_unit_count": _integer_schema(),
            "coverage_gap_pages": {
                "type": "array",
                "items": _coverage_gap_page_schema(),
            },
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "previous_part_observation": _generic_object_schema(),
            "rerun_comparison": _generic_object_schema(),
            "provider_route_plan": _generic_object_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(),
            "selected_provider_id": _string_schema(),
            "route_status": _string_schema(),
            "job_id": _string_schema(nullable=True),
            "attempts": _integer_schema(),
            "profile": _string_schema(),
            "parser_options": _generic_object_schema(),
            "last_error": _any_schema(),
            "diagnostics": {
                "type": "object",
                "required": [
                    "has_coverage_gaps",
                    "coverage_gap_count",
                    "gap_unit_count",
                    "unembedded_unit_count",
                    "rag_gate",
                    "rerun_compared",
                    "rerun_status",
                    "provider_changed",
                    "previous_selected_provider_id",
                    "current_selected_provider_id",
                    "quality_signal_count_delta",
                    "coverage_gap_delta",
                    "gap_unit_count_delta",
                    "improvement_axes",
                    "regression_axes",
                    "recommended_focus",
                ],
                "properties": {
                    "has_coverage_gaps": _boolean_schema(),
                    "coverage_gap_count": _integer_schema(),
                    "gap_unit_count": _integer_schema(),
                    "unembedded_unit_count": _integer_schema(),
                    "rag_gate": _string_schema(nullable=True),
                    "rerun_compared": _boolean_schema(),
                    "rerun_status": _string_schema(nullable=True),
                    "provider_changed": _boolean_schema(),
                    "previous_selected_provider_id": _string_schema(nullable=True),
                    "current_selected_provider_id": _string_schema(nullable=True),
                    "quality_signal_count_delta": _integer_schema(nullable=True),
                    "coverage_gap_delta": _integer_schema(nullable=True),
                    "gap_unit_count_delta": _integer_schema(nullable=True),
                    "improvement_axes": _string_list_schema(),
                    "regression_axes": _string_list_schema(),
                    "recommended_focus": _string_schema(nullable=True),
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


def _parts_document_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-parts.json",
        "title": "ParseCore Parts Projection",
        "description": "Stable part-level diagnostics and rerun contract for partitioned documents.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "state",
            "state_filter",
            "parts",
            "part_summary",
        ],
        "properties": {
            "schema_version": {"const": DOCUMENT_SCHEMA_VERSION},
            "projection": {"const": "parts"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "profile_resolution": _generic_object_schema(),
            "state": _string_schema(),
            "state_filter": _string_list_schema(),
            "parts": {
                "type": "array",
                "items": _parts_part_schema(),
            },
            "part_summary": _generic_object_schema(),
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-parts",
            "category": "document_projection",
            "projection": "parts",
            "schema_version": DOCUMENT_SCHEMA_VERSION,
        },
    }


def _quality_document_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-quality.json",
        "title": "ParseCore Quality Projection",
        "description": "Stable document-level quality diagnostics contract aggregating quality, providers, parts, and attention workflow.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "quality_summary",
            "coverage_summary",
            "rag_coverage_quality",
            "quality_gate",
            "ocr_decision_trace",
            "parse_units",
            "provider_diagnostics",
            "parts_diagnostics",
            "attention_summary",
        ],
        "properties": {
            "schema_version": {"const": DOCUMENT_SCHEMA_VERSION},
            "projection": {"const": "quality"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "profile_resolution": _generic_object_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(nullable=True),
            "state": _string_schema(),
            "quality": _quality_summary_schema(),
            "raw_quality": _quality_summary_schema(),
            "output_quality": _quality_summary_schema(),
            "quality_signals": {
                "type": "array",
                "items": _quality_signal_schema(),
            },
            "quality_summary": _quality_signal_summary_schema(),
            "coverage_summary": _coverage_summary_schema(),
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "quality_gate": _quality_gate_schema(),
            "ocr_decision_trace": _generic_object_schema(),
            "parse_units": {
                "type": "array",
                "items": _parse_unit_schema(),
            },
            "provider_diagnostics": {
                "type": "object",
                "required": ["summary", "comparison_report", "comparison_actions"],
                "properties": {
                    "summary": _generic_object_schema(),
                    "comparison_report": {
                        "type": "object",
                        "required": ["primary_provider_id", "best_provider_id", "summary"],
                        "properties": {
                            "primary_provider_id": _string_schema(nullable=True),
                            "best_provider_id": _string_schema(nullable=True),
                            "summary": _generic_object_schema(),
                        },
                        "additionalProperties": False,
                    },
                    "comparison_actions": {
                        "type": "array",
                        "items": _action_suggestion_schema(),
                    },
                },
                "additionalProperties": False,
            },
            "parts_diagnostics": {
                "type": "object",
                "required": ["part_summary", "attention_parts", "actions"],
                "properties": {
                    "part_summary": _generic_object_schema(),
                    "attention_parts": {
                        "type": "array",
                        "items": _quality_attention_part_schema(),
                    },
                    "actions": {
                        "type": "array",
                        "items": _action_suggestion_schema(),
                    },
                },
                "additionalProperties": False,
            },
            "attention_summary": {
                "type": "object",
                "required": [
                    "needs_attention",
                    "quality_gate_attention",
                    "provider_attention",
                    "part_attention_count",
                    "recommended_focus",
                    "recommended_action",
                    "recommended_entrypoint",
                    "recommended_actions",
                    "entrypoints",
                    "contracts",
                    "attention_sources",
                ],
                "properties": {
                    "needs_attention": _boolean_schema(),
                    "quality_gate_attention": _boolean_schema(),
                    "provider_attention": _boolean_schema(),
                    "part_attention_count": _integer_schema(),
                    "recommended_focus": _string_schema(nullable=True),
                    "recommended_action": _string_schema(nullable=True),
                    "recommended_entrypoint": _string_schema(nullable=True),
                    "recommended_actions": {
                        "type": "array",
                        "items": _action_suggestion_schema(),
                    },
                    "entrypoints": _generic_object_schema(),
                    "contracts": _generic_object_schema(),
                    "attention_sources": {
                        "type": "object",
                        "required": ["quality_gate", "providers", "parts"],
                        "properties": {
                            "quality_gate": _boolean_schema(),
                            "providers": _boolean_schema(),
                            "parts": _integer_schema(),
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-quality",
            "category": "document_projection",
            "projection": "quality",
            "schema_version": DOCUMENT_SCHEMA_VERSION,
        },
    }


def _ir_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-ir.json",
        "title": "ParseCore Document IR Projection",
        "description": "Stable document-level Parse IR contract for local Provider normalization and downstream reader/RAG consumers.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "state",
            "provider_registry",
            "local_provider_routing",
            "providers",
            "pages",
            "blocks",
            "tables",
            "figures",
            "knowledge_units",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "coverage",
            "coverage_quality_signals",
            "rag_coverage_quality",
            "ocr_decision_trace",
            "index_manifest",
            "quality_gate",
        ],
        "properties": {
            "schema_version": {"const": IR_SCHEMA_VERSION},
            "projection": {"const": "ir"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "profile_resolution": _generic_object_schema(),
            "state": _string_schema(),
            "provider_registry": _local_provider_registry_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(nullable=True),
            "providers": {
                "type": "array",
                "items": _ir_provider_entry_schema(),
            },
            "pages": {
                "type": "array",
                "items": _ir_page_schema(),
            },
            "blocks": {
                "type": "array",
                "items": _ir_block_schema(),
            },
            "tables": {
                "type": "array",
                "items": _ir_table_schema(),
            },
            "figures": {
                "type": "array",
                "items": _ir_figure_schema(),
            },
            "knowledge_units": {
                "type": "array",
                "items": _knowledge_unit_schema(),
            },
            "quality": _quality_summary_schema(),
            "raw_quality": _quality_summary_schema(),
            "output_quality": _quality_summary_schema(),
            "quality_signals": {
                "type": "array",
                "items": _quality_signal_schema(),
            },
            "coverage": _coverage_container_schema(),
            "coverage_quality_signals": {
                "type": "array",
                "items": _quality_signal_schema(),
            },
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "ocr_decision_trace": _generic_object_schema(),
            "index_manifest": _generic_object_schema(),
            "quality_gate": _generic_object_schema(),
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-ir",
            "category": "document_projection",
            "projection": "ir",
            "schema_version": IR_SCHEMA_VERSION,
        },
    }


def _reader_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        "$id": f"{_SCHEMA_BASE_URI}/document-reader.json",
        "title": "ParseCore Reader Projection",
        "description": "Stable reader-oriented contract derived from Parse IR for rendering and page-level diagnostics.",
        "type": "object",
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "pages",
            "blocks",
            "reader_summary",
            "quality_signals",
            "quality_gate",
            "rag_coverage_quality",
            "index_manifest",
        ],
        "properties": {
            "schema_version": {"const": READER_SCHEMA_VERSION},
            "projection": {"const": "reader"},
            "doc_id": _string_schema(),
            "parse_run_id": _string_schema(),
            "profile": _string_schema(),
            "profile_resolution": _generic_object_schema(),
            "local_provider_routing": _local_provider_routing_decision_schema(nullable=True),
            "state": _string_schema(),
            "pages": {
                "type": "array",
                "items": _reader_page_schema(),
            },
            "blocks": {
                "type": "array",
                "items": _reader_block_schema(),
            },
            "reader_summary": _reader_summary_schema(),
            "quality_signals": {
                "type": "array",
                "items": _quality_signal_schema(),
            },
            "quality_gate": _generic_object_schema(),
            "rag_coverage_quality": _rag_coverage_quality_schema(),
            "index_manifest": _generic_object_schema(),
        },
        "additionalProperties": False,
        "x-parsecore": {
            "name": "document-reader",
            "category": "document_projection",
            "projection": "reader",
            "schema_version": READER_SCHEMA_VERSION,
        },
    }


_SCHEMAS = {
    "document-coverage": _coverage_document_schema(),
    "document-ir": _ir_schema(),
    "document-parts": _parts_document_schema(),
    "document-providers": _providers_document_schema(),
    "document-quality": _quality_document_schema(),
    "document-reader": _reader_schema(),
}
