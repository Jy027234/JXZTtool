from __future__ import annotations

from types import SimpleNamespace
import unittest

from parsecore.api_payloads import (
    _document_projection,
    _document_providers_projection,
    _document_quality_projection,
    _document_records_projection,
    _normalize_reader_text,
    _quality_required_provider_capabilities,
    _project_pages,
    _structured_lines_from_blocks,
)
from parsecore.models import Block, BlockType, Chunk, ParseJobState, SemanticRole


class DocumentProjectionQualitySignalTests(unittest.TestCase):
    def test_structured_lines_preserve_ocr_source_regions(self) -> None:
        block = Block(
            block_id="blk-ocr-1",
            doc_id="doc-ocr-001",
            type=BlockType.PARAGRAPH,
            content="Recovered OCR text",
            metadata={
                "page": 3,
                "parser": "pdf-text",
                "semantic_role": SemanticRole.PARAGRAPH.value,
                "lines": [
                    {
                        "line_id": "p3:ocr-p1-l1",
                        "line_index": 7,
                        "paragraph_index": 1,
                        "paragraph_line_index": 1,
                        "page_number": 3,
                        "text": "Recovered OCR text",
                        "bbox": (10.0, 20.0, 80.0, 36.0),
                        "page_width": 100.0,
                        "page_height": 100.0,
                        "confidence": 0.93,
                        "source_kind": "pdf_ocr_fallback",
                    }
                ],
            },
        )

        lines = _structured_lines_from_blocks(
            (block,),
            doc_id=block.doc_id,
            parse_run_id="job-ocr-001",
        )

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["line_id"], "blk-ocr-1:line:1")
        self.assertEqual(lines[0]["source_line_id"], "p3:ocr-p1-l1")
        self.assertEqual(lines[0]["source_line_index"], 7)
        self.assertEqual(lines[0]["bbox"], [10.0, 20.0, 80.0, 36.0])
        self.assertEqual(lines[0]["confidence"], 0.93)
        self.assertEqual(lines[0]["source_kind"], "pdf_ocr_fallback")

    def test_ir_and_reader_preserve_ocr_source_regions(self) -> None:
        job = SimpleNamespace(
            job_id="job-ocr-regions-001",
            doc_id="doc-ocr-regions-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "scan-pdf"},
        )
        source_line = {
            "line_id": "p2:ocr-p1-l1",
            "line_index": 1,
            "paragraph_index": 1,
            "paragraph_line_index": 1,
            "page_number": 2,
            "text": "Recovered OCR text",
            "bbox": (10.0, 20.0, 80.0, 36.0),
            "page_width": 100.0,
            "page_height": 100.0,
            "confidence": 0.93,
            "source_kind": "pdf_ocr_fallback",
        }
        block = Block(
            block_id="blk-ocr-regions-1",
            doc_id=job.doc_id,
            type=BlockType.PARAGRAPH,
            content="Recovered OCR text",
            metadata={
                "page": 2,
                "parser": "pdf-text",
                "semantic_role": SemanticRole.PARAGRAPH.value,
                "bbox": (10.0, 20.0, 80.0, 36.0),
                "page_width": 100.0,
                "page_height": 100.0,
                "source_kind": "pdf_ocr_fallback",
                "lines": [source_line],
            },
        )
        snapshot = {
            "job": job,
            "doc_id": job.doc_id,
            "blocks": (block,),
            "chunks": (),
        }

        ir = _document_projection(snapshot, projection="ir")
        reader = _document_projection(snapshot, projection="reader")

        expected_source_line = dict(source_line, bbox=[10.0, 20.0, 80.0, 36.0])
        self.assertEqual(ir["blocks"][0]["lines"], [expected_source_line])
        self.assertEqual(reader["blocks"][0]["lines"], [expected_source_line])
        self.assertEqual(reader["blocks"][0]["bbox"], [10.0, 20.0, 80.0, 36.0])

    def test_reader_text_preserves_regulatory_hierarchy_and_joins_continuations(self) -> None:
        text = (
            "(a) The competent authorities shall establish a system of record-keeping that allows\n"
            "adequate traceability of each certificate.\n"
            "(b) The records shall include as a minimum:\n"
            "1. the application for an organisation approval;\n"
            "2. the organisation approval certificate including any changes;\n"
            "M.A.714 Record-keeping\n"
            "The records shall be retained for five years."
        )

        self.assertEqual(
            _normalize_reader_text(text),
            "(a) The competent authorities shall establish a system of record-keeping that allows adequate traceability of each certificate.\n\n"
            "(b) The records shall include as a minimum:\n\n"
            "1. the application for an organisation approval;\n\n"
            "2. the organisation approval certificate including any changes;\n\n"
            "M.A.714 Record-keeping The records shall be retained for five years.",
        )

    def test_quality_required_provider_capabilities_use_local_ocr_fallback(self) -> None:
        self.assertEqual(
            _quality_required_provider_capabilities(["rag_empty_text_page", "text_page_coverage_below_threshold"]),
            ["native-text", "local-ocr-fallback"],
        )

    def test_quality_required_provider_capabilities_include_layout_for_reading_order(self) -> None:
        self.assertEqual(
            _quality_required_provider_capabilities(["reading_order_low_confidence"]),
            ["layout"],
        )

    def test_project_pages_preserves_image_artifacts_and_descriptions(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-text-1",
                    doc_id="doc-image-001",
                    type=BlockType.PARAGRAPH,
                    content="The hydraulic system narrative remains searchable.",
                    metadata={
                        "page": 4,
                        "page_type": "body",
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                    },
                ),
                Block(
                    block_id="blk-image-1",
                    doc_id="doc-image-001",
                    type=BlockType.IMAGE,
                    content="Figure 4-2. Hydraulic layout overview",
                    metadata={
                        "page": 4,
                        "page_type": "body",
                        "semantic_role": SemanticRole.IMAGE.value,
                        "bbox": (120.0, 240.0, 420.0, 540.0),
                        "source_kind": "pdf-image",
                        "page_width": 612.0,
                        "page_height": 792.0,
                        "caption_confidence": 0.91234,
                        "figure_kind": "diagram",
                    },
                ),
            )
        )

        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertEqual(page["page_number"], 4)
        self.assertEqual(page["text"], "The hydraulic system narrative remains searchable.")
        self.assertEqual(page["image_descriptions"], ["Figure 4-2. Hydraulic layout overview"])
        self.assertEqual(len(page["artifacts"]), 1)
        artifact = page["artifacts"][0]
        self.assertEqual(artifact["semantic_role"], "image")
        self.assertEqual(artifact["bbox"], (120.0, 240.0, 420.0, 540.0))
        self.assertEqual(artifact["source_kind"], "pdf-image")
        self.assertEqual(artifact["page_width"], 612.0)
        self.assertEqual(artifact["page_height"], 792.0)
        self.assertEqual(artifact["caption_confidence"], 0.9123)

    def test_project_pages_aggregates_reading_order_confidence(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-reading-order-1",
                    doc_id="doc-reading-order-001",
                    type=BlockType.PARAGRAPH,
                    content="Column one",
                    metadata={
                        "page": 2,
                        "page_type": "body",
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                        "layout_reading_order_confidence": 0.62,
                    },
                ),
                Block(
                    block_id="blk-reading-order-2",
                    doc_id="doc-reading-order-001",
                    type=BlockType.PARAGRAPH,
                    content="Column two",
                    metadata={
                        "page": 2,
                        "page_type": "body",
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                        "layout_reading_order_confidence": 0.58,
                    },
                ),
            )
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["reading_order_confidence"], 0.6)

    def test_table_metadata_becomes_quality_signals(self) -> None:
        job = SimpleNamespace(
            job_id="job-quality-001",
            doc_id="doc-quality-001",
            state=ParseJobState.DONE,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            options={"profile": "excel-ledger", "file_name": "ledger.xlsx"},
        )
        blocks = (
            Block(
                block_id="blk-table-1",
                doc_id="doc-quality-001",
                type=BlockType.TABLE,
                content="Task | Task |",
                metadata={
                    "page": 1,
                    "parser": "excel-native",
                    "table_type": "spreadsheet",
                    "sheet_name": "HiddenLedger",
                    "rows": 3,
                    "cols": 3,
                    "cells": [["Task", "Task", ""], ["A", "B", "C"], ["D", "", "F"]],
                    "header_values": ["Task", "Task", ""],
                    "hidden_sheet": True,
                    "merged_cells": ["A1:B1"],
                    "has_formula": True,
                    "formula_count": 2,
                    "truncated": True,
                    "cells_truncated": True,
                    "cells_total": 100,
                    "cells_preview_rows": 2,
                },
            ),
        )

        payload = _document_projection(
            {
                "job": job,
                "doc_id": "doc-quality-001",
                "blocks": blocks,
                "chunks": (),
            },
            projection="structured",
        )

        table = payload["tables"][0]
        self.assertEqual(table["header_values"], ["Task", "Task", ""])
        self.assertEqual(table["merged_cells"], ["A1:B1"])
        self.assertTrue(table["hidden_sheet"])
        self.assertTrue(table["has_formula"])
        self.assertTrue(table["cells_truncated"])
        self.assertEqual(payload["profile_resolution"]["resolved_profile"], "excel-ledger")
        self.assertEqual(payload["profile_resolution"]["source"], "requested")

        signals = {signal["code"]: signal for signal in payload["quality_signals"]}
        self.assertIn("table_cells_truncated", signals)
        self.assertIn("table_source_truncated", signals)
        self.assertIn("table_hidden_sheet", signals)
        self.assertIn("table_merged_cells", signals)
        self.assertIn("table_formula_cells", signals)
        self.assertIn("table_header_blank_cells", signals)
        self.assertIn("table_header_duplicate_values", signals)
        self.assertEqual(signals["table_header_blank_cells"]["detail"]["col_indexes"], [2])
        self.assertEqual(signals["table_header_duplicate_values"]["detail"]["values"], ["Task"])
        self.assertEqual(payload["quality_summary"]["by_code"]["table_cells_truncated"], 1)
        self.assertEqual(payload["records_summary"]["total"], 2)

        records_payload = _document_records_projection(
            {
                "job": job,
                "doc_id": "doc-quality-001",
                "blocks": blocks,
                "chunks": (),
            },
            query="F",
            limit=10,
            offset=0,
        )

        self.assertEqual(records_payload["projection"], "records")
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertEqual(record["record_id"], "doc-quality-001:p1:t1:r2")
        self.assertEqual(record["fields"], {"Task": "D", "Task_2": "", "col_3": "F"})
        self.assertEqual(record["page_start"], 1)

    def test_catalog_text_blocks_become_records(self) -> None:
        job = SimpleNamespace(
            job_id="job-catalog-001",
            doc_id="doc-catalog-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={
                "profile": "large-pdf-catalog",
                "requested_profile": "large-pdf-catalog",
                "profile_source": "requested",
                "profile_recommended_async": True,
            },
        )
        blocks = (
            Block(
                block_id="doc-catalog-001-title",
                doc_id="doc-catalog-001",
                type=BlockType.TITLE,
                content="catalog",
                metadata={"page": 1, "semantic_role": "title"},
            ),
            Block(
                block_id="blk-text-1",
                doc_id="doc-catalog-001",
                type=BlockType.PARAGRAPH,
                content=(
                    "序号 证件编号 持证人 型别 最新批准日期\n"
                    "1 TC001A 哈尔滨飞机制造公司 Y11B 1992-12-28\n"
                    "2 PMA0013-01-XN 重庆兴山泉航空设备有限公司 航空专用净水器\n"
                    "SQ-737-1518 波音 2025-01-10\n"
                    "3 无编号公司 缺少证件编号"
                ),
                metadata={"page": 2, "semantic_role": "paragraph"},
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-catalog-001",
            "blocks": blocks,
            "chunks": (),
        }
        payload = _document_projection(snapshot, projection="structured")

        self.assertEqual(payload["records_summary"]["total"], 3)
        self.assertEqual(payload["records_summary"]["text_record_count"], 3)
        self.assertEqual(payload["records_summary"]["by_source"]["text-block"], 3)
        self.assertIn("row_continuation_detected", payload["quality_summary"]["by_code"])
        self.assertIn("record_field_missing", payload["quality_summary"]["by_code"])

        records_payload = _document_records_projection(snapshot, query="波音", limit=10, offset=0)

        self.assertEqual(records_payload["projection"], "records")
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertEqual(record["source"], "text-block")
        self.assertEqual(record["row_number"], 2)
        self.assertEqual(record["page_start"], 2)
        self.assertEqual(record["page_end"], 2)
        self.assertEqual(record["fields"]["certificate_or_project_no"], "PMA0013-01-XN")
        self.assertEqual(record["fields"]["latest_date"], "2025-01-10")
        self.assertIn("row_continuation_detected", record["quality_signal_codes"])

        field_payload = _document_records_projection(
            snapshot,
            field_filters={"certificate_or_project_no": "PMA0013"},
            limit=10,
            offset=0,
        )
        self.assertEqual(field_payload["total"], 1)
        self.assertEqual(field_payload["items"][0]["row_number"], 2)

    def test_record_quality_signals_detect_shifted_columns_and_bad_dates(self) -> None:
        job = SimpleNamespace(
            job_id="job-record-quality",
            doc_id="doc-record-quality",
            state=ParseJobState.DONE,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            options={"profile": "excel-ledger"},
        )
        blocks = (
            Block(
                block_id="blk-shifted-table",
                doc_id="doc-record-quality",
                type=BlockType.TABLE,
                content="Certificate\tLatest Date\tHolder\nPMA0013-01-XN\tACME\t2025-02-31",
                metadata={
                    "page": 4,
                    "parser": "excel-native",
                    "rows": 2,
                    "cols": 3,
                    "header_values": ["Certificate", "Latest Date", "Holder"],
                    "cells": [["Certificate", "Latest Date", "Holder"], ["PMA0013-01-XN", "ACME", "2025-02-31"]],
                },
            ),
        )
        snapshot = {"job": job, "doc_id": "doc-record-quality", "blocks": blocks, "chunks": ()}

        payload = _document_projection(snapshot, projection="structured")
        records_payload = _document_records_projection(
            snapshot,
            quality_signal="column_shift_suspected",
            limit=10,
            offset=0,
        )

        self.assertIn("column_shift_suspected", payload["quality_summary"]["by_code"])
        self.assertIn("date_parse_failed", payload["quality_summary"]["by_code"])
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertIn("column_shift_suspected", record["quality_signal_codes"])
        self.assertIn("date_parse_failed", record["quality_signal_codes"])

    def test_catalog_text_record_quality_detects_date_before_certificate(self) -> None:
        job = SimpleNamespace(
            job_id="job-catalog-shift",
            doc_id="doc-catalog-shift",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "large-pdf-catalog"},
        )
        blocks = (
            Block(
                block_id="blk-catalog-shift",
                doc_id="doc-catalog-shift",
                type=BlockType.PARAGRAPH,
                content="1 2025-01-10 PMA0013-01-XN 重庆兴山泉航空设备有限公司",
                metadata={"page": 8, "semantic_role": "paragraph"},
            ),
        )
        snapshot = {"job": job, "doc_id": "doc-catalog-shift", "blocks": blocks, "chunks": ()}

        records_payload = _document_records_projection(
            snapshot,
            quality_signal="column_shift_suspected",
            limit=10,
            offset=0,
        )

        self.assertEqual(records_payload["total"], 1)
        self.assertIn("column_shift_suspected", records_payload["items"][0]["quality_signal_codes"])

    def test_catalog_text_records_continue_across_pages_while_skipping_image_artifacts(self) -> None:
        job = SimpleNamespace(
            job_id="job-catalog-cross-page",
            doc_id="doc-catalog-cross-page",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "large-pdf-catalog"},
        )
        blocks = (
            Block(
                block_id="blk-row-start",
                doc_id="doc-catalog-cross-page",
                type=BlockType.PARAGRAPH,
                content="12 PMA0013-01-XN 重庆兴山泉航空设备有限公司 航空专用净水器",
                metadata={"page": 5, "semantic_role": "paragraph"},
            ),
            Block(
                block_id="blk-workflow-artifact",
                doc_id="doc-catalog-cross-page",
                type=BlockType.IMAGE,
                content="Maintenance workflow - approval process",
                metadata={"page": 5, "semantic_role": SemanticRole.IMAGE.value},
            ),
            Block(
                block_id="blk-row-continuation",
                doc_id="doc-catalog-cross-page",
                type=BlockType.PARAGRAPH,
                content="SQ-737-1518 波音 2025-01-10",
                metadata={"page": 6, "semantic_role": "paragraph"},
            ),
        )
        snapshot = {"job": job, "doc_id": "doc-catalog-cross-page", "blocks": blocks, "chunks": ()}

        payload = _document_projection(snapshot, projection="structured")
        records_payload = _document_records_projection(snapshot, query="波音", limit=10, offset=0)

        self.assertEqual(payload["records_summary"]["text_record_count"], 1)
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertEqual(record["page_start"], 5)
        self.assertEqual(record["page_end"], 6)
        self.assertEqual(record["fields"]["certificate_or_project_no"], "PMA0013-01-XN")
        self.assertEqual(record["fields"]["latest_date"], "2025-01-10")
        self.assertIn("row_continuation_detected", record["quality_signal_codes"])
        self.assertIn("SQ-737-1518 波音 2025-01-10", record["raw_text"])
        self.assertNotIn("Maintenance workflow", record["raw_text"])

    def test_ir_projection_exposes_reader_policy_units_and_coverage(self) -> None:
        job = SimpleNamespace(
            job_id="job-ir-001",
            doc_id="doc-ir-001",
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
                doc_id="doc-ir-001",
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
                doc_id="doc-ir-001",
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
                doc_id="doc-ir-001",
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
                doc_id="doc-ir-001",
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
                doc_id="doc-ir-001",
                type=BlockType.PARAGRAPH,
                content="Page 1",
                metadata={"page": 1, "parser": "pdf-text", "semantic_role": "header_footer"},
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-title",
                doc_id="doc-ir-001",
                block_ids=("blk-title",),
                text="Maintenance Manual",
                semantic_role="title",
                embedding=(0.0, 0.1),
            ),
            Chunk(
                chunk_id="chunk-body",
                doc_id="doc-ir-001",
                block_ids=("blk-body",),
                text="Inspect the hydraulic pump before dispatch.",
                embedding=(0.1, 0.2),
            ),
            Chunk(
                chunk_id="chunk-table",
                doc_id="doc-ir-001",
                block_ids=("blk-table",),
                text="Part Qty Pump 1",
                semantic_role="table",
                embedding=(0.3, 0.4),
            ),
            Chunk(
                chunk_id="chunk-figure",
                doc_id="doc-ir-001",
                block_ids=("blk-figure",),
                text="Figure 1. Hydraulic workflow",
                semantic_role="image",
                embedding=(0.5, 0.6),
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-ir-001",
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

        structured = _document_projection(snapshot, projection="structured")
        coverage_projection = _document_projection(snapshot, projection="coverage")
        quality_projection = _document_quality_projection(snapshot)
        payload = _document_projection(
            snapshot,
            projection="ir",
        )

        self.assertEqual(structured["local_provider_routing"]["selected_provider_id"], "pdf-text")
        self.assertEqual(coverage_projection["local_provider_routing"]["route_status"], "selected")
        self.assertEqual(quality_projection["local_provider_routing"]["primary_provider_id"], "pdf-text")
        self.assertEqual(payload["schema_version"], "2026-06-ir")
        self.assertEqual(payload["projection"], "ir")
        self.assertEqual(payload["local_provider_routing"]["selected_provider_id"], "pdf-text")
        self.assertEqual(payload["provider_registry"]["summary"]["total"], 2)
        self.assertEqual(payload["providers"][0]["provider_id"], "pdf-text")
        blocks_by_id = {block["block_id"]: block for block in payload["blocks"]}
        self.assertEqual(blocks_by_id["blk-body"]["reader_policy"], "inline")
        self.assertEqual(blocks_by_id["blk-table"]["reader_policy"], "table")
        self.assertEqual(blocks_by_id["blk-table"]["index_policy"], "index_table_summary_and_cells")
        self.assertEqual(blocks_by_id["blk-figure"]["display_kind"], "figure")
        self.assertEqual(blocks_by_id["blk-figure"]["reader_policy"], "source_snapshot")
        self.assertEqual(blocks_by_id["blk-footer"]["index_policy"], "skip")
        self.assertEqual(payload["pages"][0]["width"], 612.0)
        self.assertEqual(payload["pages"][0]["height"], 792.0)
        self.assertEqual(payload["pages"][0]["rotation"], 0)
        self.assertEqual(payload["pages"][0]["source_kind"], "native_text")
        self.assertEqual(payload["pages"][0]["reading_order_confidence"], 0.82)
        self.assertEqual(payload["tables"][0]["table_id"], "doc-ir-001:p1:t1")
        self.assertEqual(payload["tables"][0]["page_span"], [1, 1])
        self.assertEqual(payload["tables"][0]["semantic_role"], "table")
        self.assertEqual(payload["tables"][0]["source_kind"], "structured_table")
        self.assertEqual(payload["figures"][0]["figure_type"], "flowchart")
        self.assertEqual(payload["figures"][0]["page_span"], [1, 1])
        self.assertEqual(payload["figures"][0]["semantic_role"], "image")
        self.assertEqual(payload["figures"][0]["source_kind"], "pdf_image")
        self.assertEqual(payload["figures"][0]["confidence"], 0.88)

        units_by_block = {
            tuple(unit["source_block_ids"]): unit
            for unit in payload["knowledge_units"]
        }
        self.assertTrue(units_by_block[("blk-body",)]["should_index_for_rag"])
        self.assertEqual(units_by_block[("blk-body",)]["chunk_ids"], ["chunk-body"])
        self.assertEqual(units_by_block[("blk-table",)]["source_table_ids"], ["doc-ir-001:p1:t1"])
        self.assertEqual(units_by_block[("blk-footer",)]["skip_reason"], "semantic_role:header_footer")
        coverage_page = payload["coverage"]["pages"][0]
        self.assertEqual(coverage_page["page_number"], 1)
        self.assertEqual(coverage_page["table_count"], 1)
        self.assertEqual(coverage_page["figure_count"], 1)
        self.assertEqual(coverage_page["chunked_unit_count"], 4)
        self.assertEqual(coverage_page["unchunked_unit_ids"], [])
        self.assertIn("chunk-body", coverage_page["chunk_ids"])
        self.assertIn("chunk-table", coverage_page["chunk_ids"])
        self.assertIn("chunk-figure", coverage_page["chunk_ids"])
        self.assertIsNone(coverage_page["missing_reason"])
        self.assertEqual(payload["rag_coverage_quality"]["gate"], "accept")
        self.assertEqual(payload["rag_coverage_quality"]["score"], 1.0)
        self.assertEqual(payload["rag_coverage_quality"]["flags"], [])
        rag_manifest = payload["index_manifest"]["rag_coverage"]
        self.assertEqual(rag_manifest["unit_count"], 5)
        self.assertEqual(rag_manifest["indexable_unit_count"], 4)
        self.assertEqual(rag_manifest["skipped_unit_count"], 1)
        self.assertEqual(rag_manifest["chunked_unit_count"], 4)
        self.assertEqual(rag_manifest["embedded_chunk_count"], 4)
        self.assertEqual(rag_manifest["embedded_unit_count"], 4)
        self.assertEqual(rag_manifest["unembedded_unit_count"], 0)
        self.assertEqual(rag_manifest["coverage_score"], 1.0)
        units_by_id = {unit["unit_id"]: unit for unit in rag_manifest["units"]}
        self.assertEqual(units_by_id["doc-ir-001:ku:000002"]["chunk_ids"], ["chunk-body"])
        self.assertTrue(units_by_id["doc-ir-001:ku:000002"]["embedded"])
        self.assertEqual(units_by_id["doc-ir-001:ku:000005"]["skip_reason"], "semantic_role:header_footer")
        self.assertEqual(payload["quality_gate"]["gate"], "accept")
        self.assertTrue(payload["quality_gate"]["passed"])
        self.assertEqual(payload["quality_gate"]["enforcement"], "report_only")
        self.assertEqual(payload["quality_gate"]["action_suggestions"], [])

        reader = _document_projection(snapshot, projection="reader")
        self.assertEqual(reader["schema_version"], "2026-06-reader")
        self.assertEqual(reader["projection"], "reader")
        self.assertEqual(reader["local_provider_routing"]["route_status"], "selected")
        self.assertEqual(reader["reader_summary"]["block_count"], 4)
        self.assertEqual(reader["reader_summary"]["hidden_block_count"], 1)
        self.assertEqual(reader["reader_summary"]["by_type"], {"figure": 1, "table": 1, "text": 1, "title": 1})
        self.assertEqual(reader["pages"][0]["page_id"], "p0001")
        self.assertEqual(reader["pages"][0]["page_type"], "body")
        self.assertEqual(reader["pages"][0]["width"], 612.0)
        self.assertEqual(reader["pages"][0]["height"], 792.0)
        self.assertEqual(reader["pages"][0]["rotation"], 0)
        self.assertEqual(reader["pages"][0]["source_kind"], "native_text")
        self.assertEqual(reader["pages"][0]["reading_order_confidence"], 0.82)
        self.assertIn("blk-body", reader["pages"][0]["block_ids"])
        self.assertEqual(reader["pages"][0]["reader_block_count"], 4)
        self.assertEqual(reader["pages"][0]["hidden_block_count"], 1)
        reader_blocks_by_source = {
            tuple(block["source_block_ids"]): block
            for block in reader["blocks"]
        }
        self.assertNotIn(("blk-footer",), reader_blocks_by_source)
        self.assertEqual(reader_blocks_by_source[("blk-body",)]["type"], "text")
        self.assertEqual(reader_blocks_by_source[("blk-body",)]["semantic_role"], "body_section")
        self.assertEqual(reader_blocks_by_source[("blk-body",)]["source_kind"], "native_text")
        self.assertEqual(reader_blocks_by_source[("blk-body",)]["confidence"], 0.95)
        self.assertEqual(reader_blocks_by_source[("blk-table",)]["type"], "table")
        self.assertEqual(reader_blocks_by_source[("blk-table",)]["semantic_role"], "table")
        self.assertEqual(reader_blocks_by_source[("blk-table",)]["source_kind"], "structured_table")
        self.assertEqual(reader_blocks_by_source[("blk-table",)]["table"]["table_id"], "doc-ir-001:p1:t1")
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["type"], "figure")
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["semantic_role"], "image")
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["source_kind"], "pdf_image")
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["confidence"], 1.0)
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["alt_text"], "Hydraulic workflow figure")
        self.assertEqual(reader_blocks_by_source[("blk-figure",)]["figure"]["figure_type"], "flowchart")
        self.assertIn("rag_coverage", reader["index_manifest"])

    def test_ir_projection_prefers_runtime_rag_units_and_chunk_text(self) -> None:
        job = SimpleNamespace(
            job_id="job-runtime-ku-001",
            doc_id="doc-runtime-ku-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-table",
                doc_id="doc-runtime-ku-001",
                type=BlockType.TABLE,
                content="Part\tQty\nBolt\t2",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 1,
                    "table_title": "Installed parts",
                    "cells": [["Part", "Qty"], ["Bolt", "2"]],
                },
            ),
        )
        chunk_text = "Installed parts\n\n| Part | Qty |\n| --- | --- |\n| Bolt | 2 |"
        chunks = (
            Chunk(
                chunk_id="chunk-table",
                doc_id="doc-runtime-ku-001",
                block_ids=("blk-table",),
                text=chunk_text,
                semantic_role="table",
                embedding=(0.1, 0.2),
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-runtime-ku-001",
            "blocks": blocks,
            "chunks": chunks,
            "index_manifest": {
                "rag_coverage": {
                    "schema_version": "2026-06-rag-index-manifest",
                    "source": "runtime_index_manifest",
                    "strategy": "document_knowledge_units",
                    "units": [
                        {
                            "unit_id": "doc-runtime-ku-001:ku:000001",
                            "source_item_id": "itm-table",
                            "unit_type": "table",
                            "semantic_role": "table",
                            "page_span": [1, 1],
                            "source_block_ids": ["blk-table"],
                            "source_table_ids": ["itm-table"],
                            "should_index_for_rag": True,
                            "chunk_ids": ["chunk-table"],
                            "embedded": True,
                        }
                    ],
                }
            },
        }

        payload = _document_projection(snapshot, projection="ir")
        reader = _document_projection(snapshot, projection="reader")

        unit = payload["knowledge_units"][0]
        self.assertEqual(unit["text"], chunk_text)
        self.assertEqual(unit["source_item_ids"], ["itm-table"])
        self.assertEqual(
            unit["source_table_ids"],
            ["doc-runtime-ku-001:p1:t1", "itm-table"],
        )
        self.assertEqual(payload["coverage"]["pages"][0]["table_ids_without_units"], [])
        self.assertNotIn("rag_table_without_unit", payload["rag_coverage_quality"]["flags"])
        rag_manifest = payload["index_manifest"]["rag_coverage"]
        self.assertEqual(rag_manifest["source"], "runtime_index_manifest")
        self.assertEqual(rag_manifest["strategy"], "document_knowledge_units")
        self.assertEqual(rag_manifest["embedded_unit_count"], 1)
        self.assertEqual(rag_manifest["unembedded_unit_count"], 0)
        self.assertEqual(rag_manifest["units"][0]["source_table_ids"], ["doc-runtime-ku-001:p1:t1", "itm-table"])
        reader_table = reader["blocks"][0]
        self.assertEqual(reader_table["type"], "table")
        self.assertEqual(reader_table["text"], "Installed parts")
        self.assertEqual(reader_table["rag_text"], chunk_text)
        self.assertEqual(reader_table["source_unit_ids"], ["doc-runtime-ku-001:ku:000001"])
        self.assertEqual(reader_table["rag_chunk_ids"], ["chunk-table"])
        self.assertTrue(reader_table["should_index_for_rag"])
        self.assertEqual(reader_table["knowledge_units"][0]["text"], chunk_text)
        self.assertEqual(reader_table["knowledge_units"][0]["source_item_ids"], ["itm-table"])

    def test_coverage_accepts_figure_alt_text_as_caption_source(self) -> None:
        job = SimpleNamespace(
            job_id="job-figure-alt-001",
            doc_id="doc-figure-alt-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-figure-alt",
                doc_id="doc-figure-alt-001",
                type=BlockType.IMAGE,
                content="",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "image",
                    "figure_kind": "diagram",
                    "alt_text": "Hydraulic system flow diagram",
                },
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-figure-alt",
                doc_id="doc-figure-alt-001",
                block_ids=("blk-figure-alt",),
                text="Hydraulic system flow diagram",
                semantic_role="image",
                embedding=(0.3, 0.4),
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-figure-alt-001",
            "blocks": blocks,
            "chunks": chunks,
        }
        ir = _document_projection(snapshot, projection="ir")
        coverage = _document_projection(snapshot, projection="coverage")
        reader = _document_projection(snapshot, projection="reader")

        self.assertEqual(ir["blocks"][0]["index_policy"], "index_caption_only")
        self.assertEqual(ir["figures"][0]["index_policy"], "index_caption_only")
        self.assertEqual(ir["knowledge_units"][0]["text"], "Hydraulic system flow diagram")
        self.assertEqual(coverage["coverage"]["pages"][0]["figure_ids_missing_caption"], [])
        self.assertNotIn("rag_figure_caption_missing", coverage["rag_coverage_quality"]["flags"])
        self.assertEqual(reader["blocks"][0]["type"], "figure")
        self.assertEqual(reader["blocks"][0]["text"], "Hydraulic system flow diagram")
        self.assertEqual(reader["blocks"][0]["rag_text"], "Hydraulic system flow diagram")
        self.assertEqual(reader["blocks"][0]["rag_chunk_ids"], ["chunk-figure-alt"])

    def test_document_providers_projection_summarizes_provider_footprint(self) -> None:
        job = SimpleNamespace(
            job_id="job-provider-usage-001",
            doc_id="doc-provider-usage-001",
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
                    "fallback_provider_ids": [],
                    "eligible_provider_ids": ["pdf-text"],
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
                block_id="blk-native-text",
                doc_id="doc-provider-usage-001",
                type=BlockType.PARAGRAPH,
                content="Native text from the PDF provider.",
                metadata={"page": 1, "parser": "pdf-text", "semantic_role": "paragraph"},
            ),
            Block(
                block_id="blk-native-table",
                doc_id="doc-provider-usage-001",
                type=BlockType.TABLE,
                content="Part | Qty\nPump | 1",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "rows": 2,
                    "cols": 2,
                    "cells": [["Part", "Qty"], ["Pump", "1"]],
                },
            ),
            Block(
                block_id="blk-local-figure",
                doc_id="doc-provider-usage-001",
                type=BlockType.IMAGE,
                content="Figure caption from a local OCR/layout provider.",
                metadata={"page": 1, "provider_id": "rapidocr-local", "semantic_role": "image"},
            ),
        )

        payload = _document_providers_projection(
            {
                "job": job,
                "doc_id": "doc-provider-usage-001",
                "blocks": blocks,
                "chunks": (),
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
                            "capabilities": ["native-text"],
                            "admission": {
                                "route_mode": "route",
                                "gate_status": "passed",
                                "gate_checks": ["samples", "license", "performance", "observability"],
                                "route_ready": True,
                            },
                            "options": {},
                        }
                    ],
                    "summary": {
                        "total": 1,
                        "enabled": 1,
                        "disabled": 0,
                        "route_ready": 1,
                        "evaluation_only": 0,
                        "gate_pending": 0,
                        "gate_failed": 0,
                    },
                },
            }
        )

        self.assertEqual(payload["schema_version"], "2026-06-provider-usage")
        self.assertEqual(payload["projection"], "providers")
        self.assertEqual(payload["local_provider_routing"]["selected_provider_id"], "pdf-text")
        self.assertEqual(payload["summary"]["provider_count"], 2)
        self.assertEqual(payload["summary"]["primary_provider_id"], "pdf-text")
        self.assertEqual(payload["provider_registry"]["summary"]["enabled"], 1)
        self.assertEqual(payload["quality_gate"]["gate"], "accept_with_warning")
        self.assertEqual(
            [action["action_id"] for action in payload["quality_gate"]["action_suggestions"]],
            ["rechunk_document", "inspect_provider_comparison", "inspect_provider_route_plan", "review_quality"],
        )
        self.assertEqual(
            payload["quality_gate"]["provider_comparison"]["summary"]["recommended_action"],
            "inspect_provider_route_plan",
        )
        self.assertEqual(
            [action["action_id"] for action in payload["quality_gate"]["provider_comparison"]["actions"]],
            ["inspect_provider_comparison", "inspect_provider_route_plan"],
        )
        providers_by_id = {provider["provider_id"]: provider for provider in payload["providers"]}
        self.assertEqual(providers_by_id["pdf-text"]["block_count"], 2)
        self.assertEqual(providers_by_id["pdf-text"]["table_count"], 1)
        self.assertEqual(providers_by_id["pdf-text"]["page_numbers"], [1, 2])
        self.assertEqual(providers_by_id["rapidocr-local"]["figure_count"], 1)
        self.assertEqual(providers_by_id["rapidocr-local"]["block_types"], {"image": 1})
        self.assertIn("rag_units_without_chunks", providers_by_id["pdf-text"]["quality_signal_codes"])
        comparison = payload["comparison_report"]
        self.assertEqual(comparison["schema_version"], "2026-06-provider-comparison")
        self.assertEqual(comparison["primary_provider_id"], "pdf-text")
        self.assertEqual(comparison["best_provider_id"], "rapidocr-local")
        self.assertEqual(comparison["summary"]["provider_count"], 2)
        self.assertEqual(comparison["summary"]["primary_provider_rank"], 2)
        self.assertTrue(comparison["summary"]["best_provider_differs_from_primary"])
        self.assertEqual(comparison["summary"]["providers_with_quality_warnings"], 2)
        self.assertEqual(comparison["summary"]["providers_with_reading_order_warning"], 0)
        self.assertEqual(comparison["summary"]["providers_with_coverage_gaps"], 2)
        self.assertEqual(comparison["summary"]["quality_warning_provider_ids"], ["rapidocr-local", "pdf-text"])
        self.assertEqual(comparison["summary"]["reading_order_warning_provider_ids"], [])
        self.assertEqual(comparison["summary"]["coverage_gap_provider_ids"], ["rapidocr-local", "pdf-text"])
        self.assertEqual(comparison["summary"]["attention_provider_ids"], ["pdf-text", "rapidocr-local"])
        self.assertTrue(comparison["summary"]["needs_attention"])
        self.assertEqual(comparison["summary"]["recommended_action"], "inspect_provider_route_plan")
        self.assertIn("elapsed_s", comparison["summary"]["pending_axes"])
        comparison_by_id = {item["provider_id"]: item for item in comparison["rankings"]}
        self.assertEqual(comparison_by_id["pdf-text"]["metrics"]["coverage_gap_count"], 2)
        self.assertEqual(comparison_by_id["pdf-text"]["metrics"]["table_count"], 1)
        self.assertEqual(
            comparison_by_id["pdf-text"]["recommendation"],
            "rechunk_before_provider_change",
        )
        self.assertEqual(comparison_by_id["pdf-text"]["axes"]["rag_chunking"]["status"], "gap")
        self.assertEqual(comparison_by_id["pdf-text"]["axes"]["performance"]["status"], "not_observed")
        self.assertEqual(comparison_by_id["rapidocr-local"]["metrics"]["figure_count"], 1)
        self.assertEqual(
            [action["action_id"] for action in payload["comparison_actions"]],
            ["inspect_provider_comparison", "inspect_provider_route_plan"],
        )
        page_one = next(page for page in payload["pages"] if page["page_number"] == 1)
        self.assertEqual(page_one["provider_ids"], ["pdf-text", "rapidocr-local"])
        self.assertEqual(page_one["coverage_missing_reason"], "no_chunks_for_indexable_units")

    def test_document_providers_projection_observes_provider_metrics(self) -> None:
        job = SimpleNamespace(
            job_id="job-provider-metrics-001",
            doc_id="doc-provider-metrics-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-provider-metrics-1",
                doc_id="doc-provider-metrics-001",
                type=BlockType.PARAGRAPH,
                content="Provider metric page one.",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                    "provider_elapsed_s": 2.5,
                    "provider_memory_mb": 128,
                    "reading_order_confidence": 0.82,
                },
            ),
            Block(
                block_id="blk-provider-metrics-2",
                doc_id="doc-provider-metrics-001",
                type=BlockType.PARAGRAPH,
                content="Same page, lower elapsed should not double count.",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                    "provider_elapsed_s": 1.0,
                    "provider_memory_mb": 96,
                    "reading_order_confidence": 0.8,
                },
            ),
            Block(
                block_id="blk-provider-metrics-3",
                doc_id="doc-provider-metrics-001",
                type=BlockType.PARAGRAPH,
                content="Provider metric page two.",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                    "elapsed_s": 0.5,
                    "peak_kb": 2048,
                    "reading_order_confidence": 0.65,
                },
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-provider-metrics-1",
                doc_id="doc-provider-metrics-001",
                block_ids=("blk-provider-metrics-1",),
                text="Provider metric page one.",
                embedding=(0.1, 0.2),
            ),
            Chunk(
                chunk_id="chunk-provider-metrics-2",
                doc_id="doc-provider-metrics-001",
                block_ids=("blk-provider-metrics-2",),
                text="Same page, lower elapsed should not double count.",
                embedding=(0.2, 0.3),
            ),
            Chunk(
                chunk_id="chunk-provider-metrics-3",
                doc_id="doc-provider-metrics-001",
                block_ids=("blk-provider-metrics-3",),
                text="Provider metric page two.",
                embedding=(0.3, 0.4),
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-provider-metrics-001",
            "blocks": blocks,
            "chunks": chunks,
        }
        ir = _document_projection(snapshot, projection="ir")
        payload = _document_providers_projection(snapshot)

        self.assertEqual(ir["blocks"][0]["provenance"]["provider_elapsed_s"], 2.5)
        self.assertEqual(ir["blocks"][2]["provenance"]["provider_memory_mb"], 2.0)
        provider = payload["providers"][0]
        self.assertEqual(provider["provider_id"], "pdf-text")
        self.assertEqual(provider["provider_elapsed_s"], 3.0)
        self.assertEqual(provider["provider_elapsed_page_count"], 2)
        self.assertEqual(provider["provider_memory_mb"], 128.0)
        self.assertEqual(provider["provider_memory_page_count"], 2)
        self.assertEqual(provider["reading_order_confidence"], 0.735)
        self.assertEqual(provider["reading_order_confidence_page_count"], 2)
        comparison = payload["comparison_report"]
        self.assertNotIn("elapsed_s", comparison["summary"]["pending_axes"])
        self.assertNotIn("memory_mb", comparison["summary"]["pending_axes"])
        self.assertNotIn("reading_order_confidence", comparison["summary"]["pending_axes"])
        ranking = comparison["rankings"][0]
        self.assertEqual(comparison["summary"]["primary_provider_rank"], 1)
        self.assertAlmostEqual(comparison["summary"]["primary_provider_score"], ranking["score"])
        self.assertEqual(comparison["summary"]["primary_provider_recommendation"], "keep_candidate")
        self.assertEqual(comparison["summary"]["best_provider_score"], ranking["score"])
        self.assertEqual(comparison["summary"]["best_provider_recommendation"], "keep_candidate")
        self.assertFalse(comparison["summary"]["best_provider_differs_from_primary"])
        self.assertEqual(comparison["summary"]["providers_with_quality_warnings"], 1)
        self.assertEqual(comparison["summary"]["providers_with_reading_order_warning"], 1)
        self.assertEqual(comparison["summary"]["providers_with_coverage_gaps"], 0)
        self.assertEqual(comparison["summary"]["quality_warning_provider_ids"], ["pdf-text"])
        self.assertEqual(comparison["summary"]["reading_order_warning_provider_ids"], ["pdf-text"])
        self.assertEqual(comparison["summary"]["coverage_gap_provider_ids"], [])
        self.assertEqual(comparison["summary"]["attention_provider_ids"], ["pdf-text"])
        self.assertTrue(comparison["summary"]["needs_attention"])
        self.assertEqual(comparison["summary"]["recommended_action"], "inspect_provider_comparison")
        self.assertEqual(ranking["axes"]["performance"]["status"], "observed")
        self.assertEqual(ranking["axes"]["performance"]["provider_elapsed_s"], 3.0)
        self.assertEqual(ranking["axes"]["memory"]["provider_memory_mb"], 128.0)
        self.assertEqual(ranking["axes"]["reading_order"]["status"], "warning")
        self.assertEqual(ranking["metrics"]["reading_order_confidence"], 0.735)
        self.assertLess(ranking["score"], 1.0)
        self.assertEqual(
            payload["quality_gate"]["provider_comparison"]["summary"]["recommended_action"],
            "inspect_provider_comparison",
        )
        self.assertEqual(
            [action["action_id"] for action in payload["comparison_actions"]],
            ["inspect_provider_comparison"],
        )
        self.assertEqual(
            [action["action_id"] for action in payload["quality_gate"]["action_suggestions"]],
            ["inspect_provider_route_plan", "reparse_document", "inspect_provider_comparison", "review_quality"],
        )
        quality_payload = _document_quality_projection(snapshot)
        self.assertEqual(
            quality_payload["provider_diagnostics"]["comparison_report"]["summary"]["recommended_action"],
            "inspect_provider_comparison",
        )
        self.assertEqual(
            [action["action_id"] for action in quality_payload["provider_diagnostics"]["comparison_actions"]],
            ["inspect_provider_comparison"],
        )
        self.assertEqual(quality_payload["parts_diagnostics"]["part_summary"]["total"], 1)
        self.assertEqual(
            quality_payload["parts_diagnostics"]["attention_parts"][0]["recommended_focus"],
            "quality_review",
        )
        self.assertEqual(quality_payload["attention_summary"]["recommended_focus"], "providers")
        self.assertEqual(quality_payload["attention_summary"]["recommended_action"], "inspect_provider_comparison")
        self.assertEqual(
            quality_payload["attention_summary"]["recommended_entrypoint"],
            "/v1/parse/documents/doc-provider-metrics-001/providers",
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["default_request"]["action_id"],
            "inspect_provider_comparison",
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["preferred_execute_request"]["action_id"],
            "reparse_document",
        )
        self.assertEqual(
            [item["action_id"] for item in quality_payload["attention_summary"]["contracts"]["execute_requests"]],
            ["reparse_document"],
        )
        self.assertIn(
            "open_parts",
            [item["action_id"] for item in quality_payload["attention_summary"]["contracts"]["inspect_requests"]],
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["entrypoint_requests"]["providers"]["request"]["endpoint"],
            "/v1/parse/documents/doc-provider-metrics-001/providers",
        )
        workflow = quality_payload["attention_summary"]["contracts"]["workflow"]
        self.assertEqual(workflow["default_phase"], "inspect")
        self.assertEqual(
            [phase["phase"] for phase in workflow["phases"]],
            ["inspect", "compare", "execute", "verify"],
        )
        self.assertEqual(workflow["phases"][0]["preferred_contract_id"], "entrypoint:providers")
        self.assertEqual(workflow["phases"][1]["preferred_contract_id"], "recommended:1")
        self.assertEqual(workflow["phases"][2]["preferred_contract_id"], "recommended:3")
        self.assertEqual(workflow["phases"][3]["preferred_contract_id"], "entrypoint:quality")
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["parts_batch_rerun_requests"],
            [],
        )
        self.assertTrue(quality_payload["attention_summary"]["entrypoints"]["providers"]["recommended"])
        self.assertEqual(quality_payload["attention_summary"]["entrypoints"]["providers"]["state"], "attention")
        self.assertEqual(quality_payload["attention_summary"]["entrypoints"]["providers"]["attention_count"], 1)
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["providers"]["context"]["attention_provider_ids"],
            ["pdf-text"],
        )
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["params"],
            {"state": "warning|failed"},
        )
        self.assertTrue(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["context"]["attention_part_ids"]
        )
        self.assertEqual(quality_payload["attention_summary"]["entrypoints"]["coverage"]["state"], "ok")
        self.assertEqual(
            [action["action_id"] for action in quality_payload["attention_summary"]["recommended_actions"]],
            ["inspect_provider_comparison", "inspect_provider_route_plan", "reparse_document", "review_quality"],
        )

    def test_coverage_projection_flags_indexable_units_without_chunks(self) -> None:
        job = SimpleNamespace(
            job_id="job-coverage-001",
            doc_id="doc-coverage-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-orphan-text",
                doc_id="doc-coverage-001",
                type=BlockType.PARAGRAPH,
                content="This paragraph was parsed but never chunked.",
                metadata={"page": 3, "parser": "pdf-text", "semantic_role": "paragraph"},
            ),
        )

        payload = _document_projection(
            {
                "job": job,
                "doc_id": "doc-coverage-001",
                "blocks": blocks,
                "chunks": (),
            },
            projection="coverage",
        )

        self.assertEqual(payload["schema_version"], "2026-06-coverage")
        self.assertEqual(payload["projection"], "coverage")
        page = payload["coverage"]["pages"][0]
        self.assertEqual(page["page_number"], 3)
        self.assertEqual(page["unit_ids"], ["doc-coverage-001:ku:000001"])
        self.assertEqual(page["indexable_unit_ids"], ["doc-coverage-001:ku:000001"])
        self.assertEqual(page["skipped_unit_ids"], [])
        self.assertEqual(page["indexable_unit_count"], 1)
        self.assertEqual(page["chunked_unit_count"], 0)
        self.assertEqual(page["unchunked_unit_ids"], ["doc-coverage-001:ku:000001"])
        self.assertEqual(page["unembedded_unit_ids"], [])
        self.assertEqual(page["missing_reason"], "no_chunks_for_indexable_units")
        self.assertIn("rag_units_without_chunks", page["quality_signal_codes"])
        unit = payload["coverage"]["units"][0]
        self.assertEqual(unit["unit_id"], "doc-coverage-001:ku:000001")
        self.assertEqual(unit["coverage_state"], "missing_chunks")
        self.assertEqual(unit["missing_reason"], "no_chunks_for_indexable_units")
        self.assertEqual(unit["chunk_count"], 0)
        self.assertEqual(unit["embedded_chunk_count"], 0)
        self.assertFalse(unit["embedded"])
        self.assertIn("rag_units_without_chunks", unit["quality_signal_codes"])
        self.assertEqual(payload["quality_summary"]["by_code"], {"rag_units_without_chunks": 1})
        self.assertEqual(payload["coverage"]["summary"]["total_indexable_units"], 1)
        self.assertEqual(payload["coverage"]["summary"]["total_chunked_units"], 0)
        self.assertEqual(payload["coverage"]["summary"]["gap_unit_ids"], ["doc-coverage-001:ku:000001"])
        self.assertEqual(payload["coverage"]["summary"]["gap_pages"][0]["page_number"], 3)
        self.assertEqual(
            payload["coverage"]["summary"]["gap_pages"][0]["unchunked_unit_ids"],
            ["doc-coverage-001:ku:000001"],
        )
        self.assertEqual(payload["coverage"]["summary"]["unit_chunk_coverage_ratio"], 0.0)
        rag_manifest = payload["index_manifest"]["rag_coverage"]
        self.assertEqual(rag_manifest["unit_count"], 1)
        self.assertEqual(rag_manifest["indexable_unit_count"], 1)
        self.assertEqual(rag_manifest["chunked_unit_count"], 0)
        self.assertEqual(rag_manifest["unchunked_unit_count"], 1)
        self.assertEqual(rag_manifest["coverage_score"], 0.0)
        self.assertEqual(rag_manifest["units"][0]["page_span"], [3, 3])
        self.assertEqual(rag_manifest["units"][0]["chunk_ids"], [])
        self.assertFalse(rag_manifest["units"][0]["embedded"])
        self.assertEqual(rag_manifest["units"][0]["coverage_state"], "missing_chunks")
        self.assertEqual(rag_manifest["units"][0]["missing_reason"], "no_chunks_for_indexable_units")
        self.assertEqual(payload["rag_coverage_quality"]["score"], 0.0)
        self.assertEqual(payload["rag_coverage_quality"]["gate"], "accept_with_warning")
        self.assertEqual(payload["rag_coverage_quality"]["flags"], ["rag_units_without_chunks"])
        self.assertEqual(payload["rag_coverage_quality"]["recommended_action"], "rechunk_document")
        self.assertEqual(payload["quality_gate"]["gate"], "accept_with_warning")
        self.assertEqual(payload["quality_gate"]["recommended_action"], "rechunk_document")
        self.assertIn("unit_chunk_coverage_below_threshold", payload["quality_gate"]["flags"])
        self.assertIn("rag_units_without_chunks", payload["quality_gate"]["flags"])
        self.assertEqual(payload["quality_gate"]["action_suggestions"][0]["action_id"], "rechunk_document")
        self.assertEqual(
            payload["quality_gate"]["action_suggestions"][0]["endpoint"],
            "/v1/parse/documents/doc-coverage-001/rechunk",
        )

    def test_coverage_and_reader_projection_expose_unit_level_coverage_state(self) -> None:
        job = SimpleNamespace(
            job_id="job-coverage-unit-state-001",
            doc_id="doc-coverage-unit-state-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-body-embed-gap",
                doc_id="doc-coverage-unit-state-001",
                type=BlockType.PARAGRAPH,
                content="This paragraph has a chunk but no embedding.",
                metadata={"page": 7, "parser": "pdf-text", "semantic_role": "paragraph"},
            ),
            Block(
                block_id="blk-footer-skip",
                doc_id="doc-coverage-unit-state-001",
                type=BlockType.PARAGRAPH,
                content="Page 7",
                metadata={"page": 7, "parser": "pdf-text", "semantic_role": "header_footer"},
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-body-embed-gap",
                doc_id="doc-coverage-unit-state-001",
                block_ids=("blk-body-embed-gap",),
                text="This paragraph has a chunk but no embedding.",
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-coverage-unit-state-001",
            "blocks": blocks,
            "chunks": chunks,
        }

        coverage_payload = _document_projection(snapshot, projection="coverage")
        reader_payload = _document_projection(snapshot, projection="reader")
        quality_payload = _document_quality_projection(snapshot)

        page = coverage_payload["coverage"]["pages"][0]
        self.assertEqual(page["page_number"], 7)
        self.assertEqual(page["unembedded_unit_ids"], ["doc-coverage-unit-state-001:ku:000001"])
        self.assertEqual(page["skipped_unit_ids"], ["doc-coverage-unit-state-001:ku:000002"])
        body_unit = coverage_payload["coverage"]["units"][0]
        self.assertEqual(body_unit["coverage_state"], "chunks_not_embedded")
        self.assertEqual(body_unit["missing_reason"], "chunks_not_embedded")
        self.assertEqual(body_unit["chunk_count"], 1)
        self.assertEqual(body_unit["embedded_chunk_count"], 0)
        self.assertFalse(body_unit["embedded"])
        self.assertIn("rag_chunks_not_embedded", body_unit["quality_signal_codes"])
        footer_unit = coverage_payload["coverage"]["units"][1]
        self.assertEqual(footer_unit["coverage_state"], "skipped")
        self.assertIsNone(footer_unit["missing_reason"])
        self.assertEqual(footer_unit["skip_reason"], "semantic_role:header_footer")
        self.assertEqual(coverage_payload["coverage"]["summary"]["gap_unit_ids"], ["doc-coverage-unit-state-001:ku:000001"])
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["coverage"]["context"]["gap_unit_ids"],
            ["doc-coverage-unit-state-001:ku:000001"],
        )
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["coverage"]["context"]["gap_page_numbers"],
            [7],
        )
        reader_unit = reader_payload["blocks"][0]["knowledge_units"][0]
        self.assertEqual(reader_unit["coverage_state"], "chunks_not_embedded")
        self.assertEqual(reader_unit["missing_reason"], "chunks_not_embedded")
        self.assertFalse(reader_unit["embedded"])
        self.assertEqual(reader_unit["embedded_chunk_count"], 0)
        self.assertIn("rag_chunks_not_embedded", reader_unit["quality_signal_codes"])

    def test_coverage_projection_flags_table_and_figure_rag_gaps(self) -> None:
        job = SimpleNamespace(
            job_id="job-rag-gaps-001",
            doc_id="doc-rag-gaps-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            file_path="/samples/table-heavy/manual.pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-empty-table",
                doc_id="doc-rag-gaps-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 1,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
            Block(
                block_id="blk-figure-no-caption",
                doc_id="doc-rag-gaps-001",
                type=BlockType.IMAGE,
                content="",
                metadata={"page": 2, "parser": "pdf-text", "semantic_role": "image"},
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-rag-gaps-001",
            "blocks": blocks,
            "chunks": (),
            "provider_registry": {
                "routing": {
                    "enabled": False,
                    "fallback_to_default": True,
                    "include_disabled": False,
                }
            },
        }

        payload = _document_projection(snapshot, projection="coverage")
        reader = _document_projection(snapshot, projection="reader")

        page = payload["coverage"]["pages"][0]
        self.assertEqual(page["table_ids_without_units"], ["doc-rag-gaps-001:p2:t1"])
        self.assertEqual(page["figure_ids_missing_caption"], ["doc-rag-gaps-001:p2:f1"])
        self.assertIn("rag_table_without_unit", page["quality_signal_codes"])
        self.assertIn("rag_figure_caption_missing", page["quality_signal_codes"])
        self.assertEqual(payload["quality_summary"]["by_code"]["rag_table_without_unit"], 1)
        self.assertEqual(payload["quality_summary"]["by_code"]["rag_figure_caption_missing"], 1)
        self.assertEqual(payload["coverage"]["summary"]["pages_with_coverage_gaps"], 1)
        self.assertEqual(payload["coverage"]["summary"]["pages_table_without_units"], 1)
        self.assertEqual(payload["coverage"]["summary"]["pages_figure_caption_missing"], 1)
        self.assertEqual(payload["coverage"]["summary"]["gap_pages"][0]["page_number"], 2)
        self.assertIn("rag_table_without_unit", payload["rag_coverage_quality"]["flags"])
        self.assertIn("rag_figure_caption_missing", payload["rag_coverage_quality"]["flags"])
        self.assertEqual(payload["quality_gate"]["recommended_action"], "local_provider_rerun")
        suggestions = payload["quality_gate"]["action_suggestions"]
        self.assertEqual(suggestions[0]["action_id"], "inspect_provider_route_plan")
        self.assertEqual(suggestions[0]["method"], "GET")
        self.assertEqual(suggestions[0]["endpoint"], "/v1/parse/providers/route-plan")
        self.assertEqual(suggestions[0]["params"]["media_type"], "application/pdf")
        self.assertEqual(suggestions[0]["params"]["file_name"], "manual.pdf")
        self.assertEqual(suggestions[0]["params"]["profile"], "table-heavy")
        self.assertEqual(suggestions[0]["params"]["required_capabilities"], ["tables", "layout", "figures"])
        route_context = suggestions[0]["context"]["local_provider_routing"]
        self.assertFalse(route_context["routing_enabled"])
        self.assertEqual(route_context["execution_mode"], "inspect_only")
        self.assertEqual(route_context["enable_config_path"], "providers.local_parser_routing.enabled")
        self.assertEqual(route_context["requires_configuration"], ["providers.local_parser_routing.enabled"])
        self.assertEqual(route_context["routing_config"]["fallback_to_default"], True)
        self.assertEqual(suggestions[1]["action_id"], "reparse_document")
        self.assertEqual(
            suggestions[1]["payload"]["provider_route_plan"]["required_capabilities"],
            ["tables", "layout", "figures"],
        )
        self.assertEqual(suggestions[1]["context"]["local_provider_routing"], route_context)
        reader_by_type = {block["type"]: block for block in reader["blocks"]}
        self.assertIn("rag_table_without_unit", reader_by_type["table"]["quality_signal_codes"])
        self.assertNotIn("rag_figure_caption_missing", reader_by_type["table"]["quality_signal_codes"])
        self.assertIn("rag_figure_caption_missing", reader_by_type["figure"]["quality_signal_codes"])
        self.assertNotIn("rag_table_without_unit", reader_by_type["figure"]["quality_signal_codes"])

    def test_quality_gate_rerun_warning_parts_filters_already_rerun_parts(self) -> None:
        job = SimpleNamespace(
            job_id="job-rerun-filter-001",
            doc_id="doc-rerun-filter-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            file_path="/samples/table-heavy/manual.pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-empty-table-1",
                doc_id="doc-rerun-filter-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 1,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
            Block(
                block_id="blk-empty-table-2",
                doc_id="doc-rerun-filter-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 2,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
            Block(
                block_id="blk-rerun-filter-paragraph-2",
                doc_id="doc-rerun-filter-001",
                type=BlockType.PARAGRAPH,
                content="This paragraph is still missing chunks.",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                },
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-rerun-filter-001",
            "blocks": blocks,
            "chunks": (),
            "partition_parts": [
                {
                    "part_id": "doc-rerun-filter-001-part-1",
                    "parse_unit_id": "doc-rerun-filter-001-part-1",
                    "page_start": 1,
                    "page_end": 1,
                    "state": "done",
                    "rerun_supported": True,
                    "rerun_comparison": {
                        "schema_version": "2026-06-part-rerun-comparison",
                        "status": "unchanged",
                        "previous_job_id": "job-prev-1",
                        "current_job_id": "job-current-1",
                    },
                },
                {
                    "part_id": "doc-rerun-filter-001-part-2",
                    "parse_unit_id": "doc-rerun-filter-001-part-2",
                    "page_start": 2,
                    "page_end": 2,
                    "state": "done",
                    "rerun_supported": True,
                },
            ],
        }

        payload = _document_projection(snapshot, projection="coverage")

        suggestions = payload["quality_gate"]["action_suggestions"]
        self.assertEqual(suggestions[0]["action_id"], "rerun_warning_parts")
        self.assertEqual(
            suggestions[0]["payload"]["part_ids"],
            ["doc-rerun-filter-001-part-2"],
        )
        rerun_candidates = suggestions[0]["context"]["rerun_candidates"]
        self.assertEqual(rerun_candidates["eligible_count"], 1)
        self.assertEqual(rerun_candidates["coverage_gap_part_ids"], ["doc-rerun-filter-001-part-2"])
        self.assertEqual(rerun_candidates["coverage_gap_unit_part_ids"], ["doc-rerun-filter-001-part-2"])
        self.assertEqual(len(rerun_candidates["gap_unit_ids"]), 1)
        self.assertEqual(
            rerun_candidates["eligible_parts"][0]["part_id"],
            "doc-rerun-filter-001-part-2",
        )
        self.assertEqual(
            rerun_candidates["eligible_parts"][0]["page_range"],
            {"start": 2, "end": 2},
        )
        self.assertEqual(rerun_candidates["eligible_parts"][0]["coverage_gap_unit_count"], 1)
        self.assertEqual(len(rerun_candidates["eligible_parts"][0]["gap_unit_ids"]), 1)
        self.assertEqual(
            rerun_candidates["skipped_parts"][0]["reason"],
            "already_rerun:unchanged",
        )

    def test_quality_gate_skips_rerun_warning_parts_when_all_warning_parts_already_reran(self) -> None:
        job = SimpleNamespace(
            job_id="job-rerun-skip-001",
            doc_id="doc-rerun-skip-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            file_path="/samples/table-heavy/manual.pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-empty-table-only",
                doc_id="doc-rerun-skip-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 4,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 1,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-rerun-skip-001",
            "blocks": blocks,
            "chunks": (),
            "partition_parts": [
                {
                    "part_id": "doc-rerun-skip-001-part-1",
                    "parse_unit_id": "doc-rerun-skip-001-part-1",
                    "page_start": 4,
                    "page_end": 4,
                    "state": "done",
                    "rerun_supported": True,
                    "rerun_comparison": {
                        "schema_version": "2026-06-part-rerun-comparison",
                        "status": "regressed",
                        "previous_job_id": "job-prev-4",
                        "current_job_id": "job-current-4",
                    },
                },
            ],
        }

        payload = _document_projection(snapshot, projection="coverage")

        suggestions = payload["quality_gate"]["action_suggestions"]
        self.assertEqual(suggestions[0]["action_id"], "inspect_provider_route_plan")
        self.assertEqual(suggestions[0]["context"]["rerun_candidates"]["eligible_count"], 0)
        self.assertEqual(
            suggestions[0]["context"]["rerun_candidates"]["skipped_parts"][0]["reason"],
            "already_rerun:regressed",
        )
        self.assertEqual(suggestions[1]["action_id"], "reparse_document")

    def test_quality_gate_uses_reading_order_confidence_for_local_layout_rerun(self) -> None:
        job = SimpleNamespace(
            job_id="job-reading-order-001",
            doc_id="doc-reading-order-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            file_path="/samples/layout/manual.pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-reading-order-body-1",
                doc_id="doc-reading-order-001",
                type=BlockType.PARAGRAPH,
                content="Column one paragraph",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                    "layout_reading_order_confidence": 0.62,
                },
            ),
            Block(
                block_id="blk-reading-order-body-2",
                doc_id="doc-reading-order-001",
                type=BlockType.PARAGRAPH,
                content="Column two paragraph",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                    "layout_reading_order_confidence": 0.58,
                },
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chk-reading-order-1",
                doc_id="doc-reading-order-001",
                block_ids=("blk-reading-order-body-1",),
                text="Column one paragraph",
                embedding=(0.1, 0.2),
            ),
            Chunk(
                chunk_id="chk-reading-order-2",
                doc_id="doc-reading-order-001",
                block_ids=("blk-reading-order-body-2",),
                text="Column two paragraph",
                embedding=(0.1, 0.2),
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-reading-order-001",
            "blocks": blocks,
            "chunks": chunks,
            "provider_registry": {
                "routing": {
                    "enabled": False,
                    "fallback_to_default": True,
                }
            },
        }

        payload = _document_projection(snapshot, projection="coverage")

        self.assertEqual(payload["coverage"]["pages"][0]["reading_order_confidence"], 0.62)
        self.assertEqual(payload["coverage"]["pages"][1]["reading_order_confidence"], 0.58)
        self.assertEqual(payload["quality_gate"]["gate"], "local_rerun")
        self.assertEqual(payload["quality_gate"]["recommended_action"], "local_provider_rerun")
        self.assertEqual(payload["quality_gate"]["observed"]["reading_order_confidence"], 0.6)
        self.assertIn("reading_order_confidence_below_threshold", payload["quality_gate"]["flags"])
        self.assertIn("reading_order_low_confidence", payload["coverage"]["pages"][0]["quality_signal_codes"])
        self.assertIn("reading_order_low_confidence", payload["coverage"]["pages"][1]["quality_signal_codes"])
        suggestions = payload["quality_gate"]["action_suggestions"]
        self.assertEqual(suggestions[0]["action_id"], "inspect_provider_route_plan")
        self.assertEqual(suggestions[0]["params"]["required_capabilities"], ["layout"])
        self.assertEqual(
            suggestions[1]["payload"]["provider_route_plan"]["required_capabilities"],
            ["layout"],
        )

    def test_structured_projection_includes_rag_coverage_quality_signals(self) -> None:
        job = SimpleNamespace(
            job_id="job-structured-coverage-001",
            doc_id="doc-structured-coverage-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-orphan-structured-text",
                doc_id="doc-structured-coverage-001",
                type=BlockType.PARAGRAPH,
                content="This paragraph was parsed but has not entered the RAG chunk path.",
                metadata={"page": 7, "parser": "pdf-text", "semantic_role": "paragraph"},
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-structured-coverage-001",
            "blocks": blocks,
            "chunks": (),
        }

        payload = _document_projection(snapshot, projection="structured")
        quality_payload = _document_quality_projection(snapshot)

        self.assertIn("rag_units_without_chunks", payload["quality_summary"]["by_code"])
        self.assertEqual(payload["coverage_summary"]["unit_chunk_coverage_ratio"], 0.0)
        self.assertEqual(payload["rag_coverage_quality"]["gate"], "accept_with_warning")
        self.assertEqual(payload["rag_coverage_quality"]["recommended_action"], "rechunk_document")
        self.assertEqual(payload["quality_gate"]["gate"], "accept_with_warning")
        self.assertEqual(payload["quality_gate"]["observed"]["unit_chunk_coverage_ratio"], 0.0)
        self.assertEqual(payload["index_manifest"]["rag_coverage"]["coverage_score"], 0.0)
        self.assertIn("rag_units_without_chunks", payload["pages"][0]["quality_signal_codes"])
        self.assertEqual(payload["parse_units"][0]["quality_signal_count"], 1)
        self.assertEqual(payload["parse_units"][0]["coverage_summary"]["pages_with_coverage_gaps"], 1)
        self.assertEqual(payload["parse_units"][0]["coverage_summary"]["unit_chunk_coverage_ratio"], 0.0)
        self.assertEqual(
            payload["parse_units"][0]["coverage_summary"]["gap_unit_ids"],
            ["doc-structured-coverage-001:ku:000001"],
        )
        self.assertEqual(payload["parse_units"][0]["rag_coverage_quality"]["flags"], ["rag_units_without_chunks"])
        self.assertEqual(payload["parse_units"][0]["coverage_gap_pages"][0]["page_number"], 7)
        self.assertEqual(
            payload["parse_units"][0]["coverage_gap_pages"][0]["missing_reason"],
            "no_chunks_for_indexable_units",
        )
        self.assertEqual(
            payload["parse_units"][0]["coverage_gap_pages"][0]["unchunked_unit_ids"],
            ["doc-structured-coverage-001:ku:000001"],
        )
        self.assertIn("rag_coverage_quality", quality_payload)
        self.assertEqual(quality_payload["rag_coverage_quality"]["flags"], ["rag_units_without_chunks"])
        self.assertIn("quality_gate", quality_payload)
        self.assertEqual(quality_payload["quality_gate"]["recommended_action"], "rechunk_document")
        self.assertEqual(quality_payload["quality_gate"]["action_suggestions"][0]["method"], "POST")
        self.assertEqual(quality_payload["provider_diagnostics"]["comparison_report"]["primary_provider_id"], "pdf-text")
        self.assertEqual(quality_payload["parts_diagnostics"]["part_summary"]["warning_parts"], 1)
        self.assertEqual(
            quality_payload["parts_diagnostics"]["attention_parts"][0]["recommended_focus"],
            "quality_review",
        )
        self.assertEqual(
            quality_payload["parts_diagnostics"]["attention_parts"][0]["coverage_gap_unit_count"],
            1,
        )
        self.assertEqual(
            quality_payload["parts_diagnostics"]["attention_parts"][0]["gap_unit_ids"],
            ["doc-structured-coverage-001:ku:000001"],
        )
        self.assertTrue(quality_payload["attention_summary"]["needs_attention"])
        self.assertEqual(quality_payload["attention_summary"]["recommended_focus"], "quality_gate")
        self.assertEqual(quality_payload["attention_summary"]["recommended_action"], "rechunk_document")
        self.assertEqual(
            quality_payload["attention_summary"]["recommended_entrypoint"],
            "/v1/parse/documents/doc-structured-coverage-001/quality",
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["default_request"]["request"]["endpoint"],
            "/v1/parse/documents/doc-structured-coverage-001/rechunk",
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["preferred_execute_request"]["action_id"],
            "rechunk_document",
        )
        workflow = quality_payload["attention_summary"]["contracts"]["workflow"]
        self.assertEqual(
            [phase["phase"] for phase in workflow["phases"]],
            ["inspect", "compare", "execute", "verify"],
        )
        self.assertEqual(workflow["phases"][0]["preferred_contract_id"], "entrypoint:quality")
        self.assertEqual(workflow["phases"][2]["preferred_contract_id"], "recommended:1")
        self.assertEqual(
            quality_payload["attention_summary"]["attention_sources"],
            {"quality_gate": True, "providers": True, "parts": 1},
        )
        self.assertTrue(quality_payload["attention_summary"]["entrypoints"]["quality"]["recommended"])
        self.assertEqual(quality_payload["attention_summary"]["entrypoints"]["coverage"]["state"], "attention")
        self.assertEqual(quality_payload["attention_summary"]["entrypoints"]["coverage"]["attention_count"], 1)
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["coverage"]["context"]["pages_missing_chunks"],
            1,
        )
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["params"],
            {"state": "warning|failed"},
        )
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["context"]["coverage_gap_unit_part_ids"],
            ["doc-structured-coverage-001:unit:1"],
        )
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["context"]["gap_unit_ids"],
            ["doc-structured-coverage-001:ku:000001"],
        )
        self.assertEqual(
            [action["action_id"] for action in quality_payload["attention_summary"]["recommended_actions"]],
            ["rechunk_document", "inspect_provider_comparison", "review_quality"],
        )

    def test_parse_units_autogenerate_rerun_comparison_with_gap_unit_deltas(self) -> None:
        job = SimpleNamespace(
            job_id="job-part-rerun-unit-001",
            doc_id="doc-part-rerun-unit-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "default"},
        )
        blocks = (
            Block(
                block_id="blk-part-rerun-unit-001",
                doc_id="doc-part-rerun-unit-001",
                type=BlockType.PARAGRAPH,
                content="Chunk is now embedded after rerun.",
                metadata={"page": 4, "parser": "pdf-layout", "semantic_role": "paragraph"},
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-part-rerun-unit-001",
                doc_id="doc-part-rerun-unit-001",
                block_ids=("blk-part-rerun-unit-001",),
                text="Chunk is now embedded after rerun.",
                semantic_role="paragraph",
                embedding=(0.1, 0.2),
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-part-rerun-unit-001",
            "blocks": blocks,
            "chunks": chunks,
            "partition_parts": [
                {
                    "part_id": "doc-part-rerun-unit-001-part-1",
                    "parse_unit_id": "doc-part-rerun-unit-001-part-1",
                    "part_doc_id": "doc-part-rerun-unit-001-part-1",
                    "page_start": 4,
                    "page_end": 4,
                    "state": "done",
                    "job_id": "job-part-rerun-unit-001-current",
                    "rerun_supported": True,
                    "provider_ids": ["pdf-layout"],
                    "local_provider_routing": {
                        "schema_version": "2026-06-local-provider-routing-decision",
                        "enabled": True,
                        "routing_policy": "priority_desc_then_id",
                        "selected_provider_id": "pdf-layout",
                        "route_status": "selected",
                        "selected_route_role": "primary",
                        "primary_provider_id": "pdf-layout",
                        "fallback_provider_ids": [],
                        "eligible_provider_ids": ["pdf-layout"],
                        "excluded_provider_ids": ["pdf-text"],
                        "fallback_to_default": True,
                        "requested": {
                            "media_type": "application/pdf",
                            "extension": ".pdf",
                            "profile": "default",
                            "file_name": "manual.pdf",
                            "required_capabilities": ["layout"],
                            "include_disabled": False,
                        },
                    },
                    "previous_part_observation": {
                        "schema_version": "2026-06-part-observation",
                        "job_id": "job-part-rerun-unit-001-prev",
                        "state": "warning",
                        "quality_signal_count": 1,
                        "quality_signal_codes": ["rag_chunks_not_embedded"],
                        "provider_ids": ["pdf-text"],
                        "selected_provider_id": "pdf-text",
                        "coverage_summary": {
                            "total_pages": 1,
                            "pages_with_parsed_text": 1,
                            "pages_with_indexable_units": 1,
                            "pages_missing_rag_units": 0,
                            "pages_missing_chunks": 0,
                            "pages_chunks_not_embedded": 1,
                            "pages_with_coverage_gaps": 1,
                            "pages_table_without_units": 0,
                            "pages_figure_caption_missing": 0,
                            "total_indexable_units": 1,
                            "total_chunked_units": 1,
                            "total_unit_count": 1,
                            "skipped_unit_count": 0,
                            "embedded_unit_count": 0,
                            "unembedded_unit_count": 1,
                            "gap_unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                            "gap_pages": [
                                {
                                    "page_number": 4,
                                    "missing_reason": "chunks_not_embedded",
                                    "unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                    "indexable_unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                    "unchunked_unit_ids": [],
                                    "unembedded_unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                    "table_ids_without_units": [],
                                    "figure_ids_missing_caption": [],
                                    "quality_signal_codes": ["rag_chunks_not_embedded"],
                                }
                            ],
                            "text_page_coverage_ratio": 1.0,
                            "unit_chunk_coverage_ratio": 1.0,
                            "table_unit_coverage_ratio": 1.0,
                        },
                        "coverage_gap_pages": [
                            {
                                "page_number": 4,
                                "missing_reason": "chunks_not_embedded",
                                "unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                "indexable_unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                "unchunked_unit_ids": [],
                                "unembedded_unit_ids": ["doc-part-rerun-unit-001:ku:000001"],
                                "table_ids_without_units": [],
                                "figure_ids_missing_caption": [],
                                "quality_signal_codes": ["rag_chunks_not_embedded"],
                            }
                        ],
                        "rag_coverage_quality": {
                            "score": 0.0,
                            "gate": "accept_with_warning",
                            "flags": ["rag_chunks_not_embedded"],
                            "warnings": ["1 page(s) have chunks that have not been embedded"],
                            "recommended_action": "reembed_document",
                        },
                    },
                }
            ],
        }

        payload = _document_projection(snapshot, projection="structured")
        part = payload["parse_units"][0]

        self.assertEqual(part["coverage_summary"]["gap_unit_ids"], [])
        self.assertEqual(part["coverage_summary"]["embedded_unit_count"], 1)
        self.assertEqual(part["coverage_summary"]["unembedded_unit_count"], 0)
        self.assertEqual(part["coverage_gap_pages"], [])
        self.assertEqual(part["rerun_comparison"]["status"], "improved")
        self.assertEqual(part["rerun_comparison"]["gap_unit_count_delta"], -1)
        self.assertEqual(part["rerun_comparison"]["unembedded_unit_count_delta"], -1)
        self.assertEqual(
            part["rerun_comparison"]["gap_unit_ids_removed"],
            ["doc-part-rerun-unit-001:ku:000001"],
        )
        self.assertEqual(part["rerun_comparison"]["current_gap_unit_ids"], [])
        self.assertIn("gap_units", part["rerun_comparison"]["improvement_axes"])
        self.assertIn("unembedded_units", part["rerun_comparison"]["improvement_axes"])

        quality_payload = _document_quality_projection(snapshot)
        attention_part = quality_payload["parts_diagnostics"]["attention_parts"][0]
        self.assertEqual(attention_part["part_id"], "doc-part-rerun-unit-001-part-1")
        self.assertEqual(attention_part["coverage_gap_unit_count"], 0)
        self.assertEqual(attention_part["gap_unit_ids"], [])
        self.assertEqual(attention_part["unembedded_unit_count"], 0)
        self.assertEqual(attention_part["gap_unit_count_delta"], -1)
        self.assertEqual(
            attention_part["gap_unit_ids_removed"],
            ["doc-part-rerun-unit-001:ku:000001"],
        )
        self.assertEqual(attention_part["gap_unit_ids_added"], [])
        self.assertEqual(
            quality_payload["attention_summary"]["entrypoints"]["parts"]["context"]["rerun_gap_unit_part_ids"],
            ["doc-part-rerun-unit-001-part-1"],
        )

    def test_quality_projection_builds_batch_rerun_contracts_for_attention_parts(self) -> None:
        job = SimpleNamespace(
            job_id="job-quality-rerun-contract-001",
            doc_id="doc-quality-rerun-contract-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            file_path="/samples/table-heavy/manual.pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-quality-rerun-table-1",
                doc_id="doc-quality-rerun-contract-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 1,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
            Block(
                block_id="blk-quality-rerun-paragraph-1",
                doc_id="doc-quality-rerun-contract-001",
                type=BlockType.PARAGRAPH,
                content="Paragraph on page 1 still needs chunk coverage.",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                },
            ),
            Block(
                block_id="blk-quality-rerun-table-2",
                doc_id="doc-quality-rerun-contract-001",
                type=BlockType.TABLE,
                content="",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "table",
                    "table_index": 2,
                    "rows": 2,
                    "cols": 2,
                    "cells": [],
                },
            ),
            Block(
                block_id="blk-quality-rerun-paragraph-2",
                doc_id="doc-quality-rerun-contract-001",
                type=BlockType.PARAGRAPH,
                content="Paragraph on page 2 still needs chunk coverage.",
                metadata={
                    "page": 2,
                    "parser": "pdf-text",
                    "semantic_role": "paragraph",
                },
            ),
        )
        snapshot = {
            "job": job,
            "doc_id": "doc-quality-rerun-contract-001",
            "blocks": blocks,
            "chunks": (),
            "partition_parts": [
                {
                    "part_id": "doc-quality-rerun-contract-001-part-1",
                    "parse_unit_id": "doc-quality-rerun-contract-001-part-1",
                    "page_start": 1,
                    "page_end": 1,
                    "state": "done",
                    "rerun_supported": True,
                },
                {
                    "part_id": "doc-quality-rerun-contract-001-part-2",
                    "parse_unit_id": "doc-quality-rerun-contract-001-part-2",
                    "page_start": 2,
                    "page_end": 2,
                    "state": "done",
                    "rerun_supported": True,
                },
            ],
        }

        quality_payload = _document_quality_projection(snapshot)

        contracts = quality_payload["attention_summary"]["contracts"]["parts_batch_rerun_requests"]
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0]["action_id"], "rerun_attention_parts")
        self.assertEqual(contracts[0]["target_count"], 2)
        self.assertEqual(
            contracts[0]["part_ids"],
            [
                "doc-quality-rerun-contract-001-part-1",
                "doc-quality-rerun-contract-001-part-2",
            ],
        )
        self.assertEqual(
            contracts[0]["required_capabilities"],
            ["tables"],
        )
        self.assertEqual(
            contracts[0]["request"]["endpoint"],
            "/v1/parse/documents/doc-quality-rerun-contract-001/parts/rerun",
        )
        self.assertEqual(
            contracts[0]["request"]["payload"]["part_ids"],
            [
                "doc-quality-rerun-contract-001-part-1",
                "doc-quality-rerun-contract-001-part-2",
            ],
        )
        self.assertEqual(
            contracts[0]["request"]["payload"]["provider_route_plan"]["required_capabilities"],
            ["tables"],
        )
        self.assertEqual(
            contracts[0]["context"]["coverage_gap_unit_part_ids"],
            [
                "doc-quality-rerun-contract-001-part-1",
                "doc-quality-rerun-contract-001-part-2",
            ],
        )
        self.assertEqual(len(contracts[0]["context"]["gap_unit_ids"]), 2)
        self.assertEqual(len(contracts[0]["context"]["attention_parts"]), 2)
        self.assertEqual(
            contracts[0]["context"]["attention_parts"][0]["coverage_gap_unit_count"],
            1,
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["preferred_execute_request"]["request"]["endpoint"],
            "/v1/parse/documents/doc-quality-rerun-contract-001/parts/rerun",
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["preferred_execute_request"]["context"]["coverage_gap_unit_part_ids"],
            [
                "doc-quality-rerun-contract-001-part-1",
                "doc-quality-rerun-contract-001-part-2",
            ],
        )
        self.assertEqual(
            quality_payload["attention_summary"]["contracts"]["preferred_execute_request"]["part_ids"],
            [
                "doc-quality-rerun-contract-001-part-1",
                "doc-quality-rerun-contract-001-part-2",
            ],
        )
        self.assertEqual(
            [item["action_id"] for item in quality_payload["attention_summary"]["contracts"]["execute_requests"]],
            ["rerun_warning_parts", "reparse_document", "rechunk_document"],
        )
        workflow = quality_payload["attention_summary"]["contracts"]["workflow"]
        self.assertEqual(workflow["phases"][0]["preferred_contract_id"], "entrypoint:parts")
        self.assertEqual(workflow["phases"][1]["preferred_contract_id"], "recommended:2")
        self.assertEqual(workflow["phases"][2]["preferred_contract_id"], "recommended:1")


class ReadingOrderConfidenceDrillDownTests(unittest.TestCase):
    """P4-T07: verify the drill-down path when reading_order_confidence is low.

    Chain: reader_page.reading_order_confidence → quality_signal_codes →
           coverage_missing_reason → provider_ids → provider_provenance.reading_order_confidence
    """

    def _build_snapshot(self, confidence: float = 0.45) -> dict[str, Any]:
        job = SimpleNamespace(
            job_id="job-roc-001",
            doc_id="doc-roc-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "table-heavy"},
        )
        blocks = (
            Block(
                block_id="blk-body-low-roc",
                doc_id="doc-roc-001",
                type=BlockType.PARAGRAPH,
                content="Body text with uncertain reading order.",
                metadata={
                    "page": 1,
                    "parser": "pdf-text",
                    "semantic_role": "body_section",
                    "page_width": 612.0,
                    "page_height": 792.0,
                    "source_kind": "native_text",
                    "reading_order": 1,
                    "confidence": 0.90,
                    "reading_order_confidence": confidence,
                },
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chunk-body-low-roc",
                doc_id="doc-roc-001",
                block_ids=("blk-body-low-roc",),
                text="Body text with uncertain reading order.",
                embedding=(0.1, 0.2),
            ),
        )
        return {
            "job": job,
            "doc_id": "doc-roc-001",
            "blocks": blocks,
            "chunks": chunks,
        }

    def test_reader_page_carries_low_reading_order_confidence(self) -> None:
        """Reader page must carry reading_order_confidence from IR page."""
        snapshot = self._build_snapshot(confidence=0.45)
        reader = _document_projection(snapshot, projection="reader")
        page = reader["pages"][0]
        self.assertEqual(page["reading_order_confidence"], 0.45)

    def test_low_confidence_generates_reading_order_quality_signal(self) -> None:
        """Low confidence must produce reading_order_low_confidence quality signal."""
        snapshot = self._build_snapshot(confidence=0.45)
        coverage = _document_projection(snapshot, projection="coverage")
        # Coverage page carries the reading_order_low_confidence signal code
        cov_page = coverage["coverage"]["pages"][0]
        self.assertIn("reading_order_low_confidence", cov_page["quality_signal_codes"])

    def test_reader_page_quality_signal_codes_linked_to_coverage(self) -> None:
        """Reader page quality_signal_codes must match coverage page quality_signal_codes."""
        snapshot = self._build_snapshot(confidence=0.45)
        reader = _document_projection(snapshot, projection="reader")
        coverage = _document_projection(snapshot, projection="coverage")
        reader_page = reader["pages"][0]
        cov_page = coverage["coverage"]["pages"][0]
        self.assertEqual(
            set(reader_page["quality_signal_codes"]),
            set(cov_page["quality_signal_codes"]),
        )
        self.assertIn("reading_order_low_confidence", reader_page["quality_signal_codes"])

    def test_reader_page_has_provider_ids_for_drill_down(self) -> None:
        """Reader page must carry provider_ids for drill-down to provider provenance."""
        snapshot = self._build_snapshot(confidence=0.45)
        reader = _document_projection(snapshot, projection="reader")
        page = reader["pages"][0]
        self.assertTrue(len(page["provider_ids"]) > 0)
        self.assertIn("pdf-text", page["provider_ids"])

    def test_ir_block_provenance_carries_reading_order_confidence(self) -> None:
        """IR block provenance must carry reading_order_confidence from block metadata."""
        snapshot = self._build_snapshot(confidence=0.45)
        ir = _document_projection(snapshot, projection="ir")
        block = ir["blocks"][0]
        provenance = block.get("provenance", {})
        self.assertEqual(provenance.get("reading_order_confidence"), 0.45)

    def test_coverage_page_carries_reading_order_confidence(self) -> None:
        """Coverage page must carry reading_order_confidence for drill-down."""
        snapshot = self._build_snapshot(confidence=0.45)
        coverage = _document_projection(snapshot, projection="coverage")
        cov_page = coverage["coverage"]["pages"][0]
        self.assertEqual(cov_page["reading_order_confidence"], 0.45)

    def test_high_confidence_does_not_generate_signal(self) -> None:
        """Confidence above threshold must NOT produce reading_order_low_confidence signal."""
        snapshot = self._build_snapshot(confidence=0.90)
        coverage = _document_projection(snapshot, projection="coverage")
        signal_codes = {s["code"] for s in coverage.get("quality_signals", [])}
        self.assertNotIn("reading_order_low_confidence", signal_codes)

    def test_full_drill_down_chain(self) -> None:
        """Verify the complete drill-down chain end-to-end."""
        snapshot = self._build_snapshot(confidence=0.45)
        ir = _document_projection(snapshot, projection="ir")
        coverage = _document_projection(snapshot, projection="coverage")
        reader = _document_projection(snapshot, projection="reader")

        # Step 1: IR block provenance has reading_order_confidence
        ir_block = ir["blocks"][0]
        self.assertEqual(ir_block["provenance"]["reading_order_confidence"], 0.45)

        # Step 2: Coverage page has reading_order_confidence + quality_signal_codes
        cov_page = coverage["coverage"]["pages"][0]
        self.assertEqual(cov_page["reading_order_confidence"], 0.45)
        self.assertIn("reading_order_low_confidence", cov_page["quality_signal_codes"])

        # Step 3: Reader page has reading_order_confidence from IR/coverage page
        reader_page = reader["pages"][0]
        self.assertEqual(reader_page["reading_order_confidence"], 0.45)

        # Step 4: Reader page quality_signal_codes match coverage page
        self.assertEqual(
            set(reader_page["quality_signal_codes"]),
            set(cov_page["quality_signal_codes"]),
        )

        # Step 5: Reader page has provider_ids for provider drill-down
        self.assertTrue(len(reader_page["provider_ids"]) > 0)

        # Step 6: Reader page has coverage_missing_reason for coverage drill-down
        self.assertIn("coverage_missing_reason", reader_page)


if __name__ == "__main__":
    unittest.main()
