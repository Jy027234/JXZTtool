from __future__ import annotations

from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator

from parsecore.api_payloads import (
    _document_projection,
    _document_providers_projection,
    _document_quality_projection,
)
from parsecore.models import Block, BlockType, Chunk, ParseJobState
from parsecore.parts import document_parts_projection
from parsecore.payload_schemas import (
    PAYLOAD_SCHEMA_REGISTRY_VERSION,
    payload_schema,
    payload_schema_names,
    payload_schema_registry,
)


def _sample_snapshot() -> dict[str, object]:
    job = SimpleNamespace(
        job_id="job-schema-001",
        doc_id="doc-schema-001",
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={
            "profile": "table-heavy",
            "local_provider_routing": {
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
            },
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
                "lines": [
                    {
                        "line_id": "p1:ocr-p1-l1",
                        "line_index": 1,
                        "paragraph_index": 1,
                        "paragraph_line_index": 1,
                        "page_number": 1,
                        "text": "Inspect the hydraulic pump before dispatch.",
                        "bbox": [10, 20, 300, 60],
                        "page_width": 612.0,
                        "page_height": 792.0,
                        "confidence": 0.95,
                        "source_kind": "native_text",
                    }
                ],
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
        "provider_registry": {
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
        },
    }


class PayloadSchemaTests(unittest.TestCase):
    def test_schema_registry_lists_frozen_document_contracts(self) -> None:
        registry = payload_schema_registry()

        self.assertEqual(registry["schema_version"], PAYLOAD_SCHEMA_REGISTRY_VERSION)
        self.assertEqual(
            payload_schema_names(),
            (
                "document-coverage",
                "document-ir",
                "document-parts",
                "document-providers",
                "document-quality",
                "document-reader",
            ),
        )
        self.assertEqual(registry["summary"]["total"], 6)
        self.assertEqual(registry["summary"]["document_projection_count"], 6)
        self.assertEqual(
            [descriptor["endpoint"] for descriptor in registry["schemas"]],
            [
                "/v1/parse/schemas/document-coverage",
                "/v1/parse/schemas/document-ir",
                "/v1/parse/schemas/document-parts",
                "/v1/parse/schemas/document-providers",
                "/v1/parse/schemas/document-quality",
                "/v1/parse/schemas/document-reader",
            ],
        )

    def test_all_registered_schemas_are_valid_json_schema(self) -> None:
        expected_versions = {
            "document-coverage": "2026-06-coverage",
            "document-ir": "2026-06-ir",
            "document-parts": "2026-06",
            "document-providers": "2026-06-provider-usage",
            "document-quality": "2026-06",
            "document-reader": "2026-06-reader",
        }
        for name, schema_version in expected_versions.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                Draft202012Validator.check_schema(schema)
                self.assertEqual(schema["x-parsecore"]["schema_version"], schema_version)

    def test_ir_payload_matches_frozen_schema(self) -> None:
        payload = _document_projection(_sample_snapshot(), projection="ir")
        validator = Draft202012Validator(payload_schema("document-ir"))

        validator.validate(payload)

    def test_ir_payload_normalizes_legacy_provider_registry_and_routing_shapes(self) -> None:
        snapshot = _sample_snapshot()
        job = snapshot["job"]
        job.options["local_provider_routing"] = {  # type: ignore[index]
            "selected_provider_id": "pdf-text",
            "route_status": "selected",
        }
        snapshot["provider_registry"] = {
            "local_parsers": [
                {"id": "pdf-text", "enabled": True, "priority": 100},
            ]
        }

        payload = _document_projection(snapshot, projection="ir")
        validator = Draft202012Validator(payload_schema("document-ir"))

        validator.validate(payload)
        self.assertEqual(payload["local_provider_routing"]["routing_policy"], "priority_desc_then_id")
        self.assertEqual(payload["local_provider_routing"]["selected_route_role"], "primary")
        self.assertEqual(payload["local_provider_routing"]["requested"]["required_capabilities"], [])
        self.assertEqual(payload["provider_registry"]["routing"]["routing_policy"], "priority_desc_then_id")
        self.assertEqual(payload["provider_registry"]["local_parsers"][0]["admission"]["gate_status"], "passed")
        self.assertEqual(payload["provider_registry"]["summary"]["route_ready"], 1)

    def test_coverage_payload_matches_frozen_schema(self) -> None:
        payload = _document_projection(_sample_snapshot(), projection="coverage")
        validator = Draft202012Validator(payload_schema("document-coverage"))

        validator.validate(payload)

    def test_providers_payload_matches_frozen_schema(self) -> None:
        payload = _document_providers_projection(_sample_snapshot())
        validator = Draft202012Validator(payload_schema("document-providers"))

        validator.validate(payload)

    def test_quality_payload_matches_frozen_schema(self) -> None:
        payload = _document_quality_projection(_sample_snapshot())
        validator = Draft202012Validator(payload_schema("document-quality"))

        validator.validate(payload)

    def test_reader_payload_matches_frozen_schema(self) -> None:
        payload = _document_projection(_sample_snapshot(), projection="reader")
        validator = Draft202012Validator(payload_schema("document-reader"))

        validator.validate(payload)

    def test_parts_payload_matches_frozen_schema(self) -> None:
        structured = _document_projection(_sample_snapshot(), projection="structured")
        payload = document_parts_projection(structured)
        validator = Draft202012Validator(payload_schema("document-parts"))

        validator.validate(payload)
