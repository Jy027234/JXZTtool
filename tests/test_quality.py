from __future__ import annotations

import unittest

from parsecore.models import Block, BlockType, Chunk
from parsecore.api_payloads import _project_pages
from parsecore.quality import (
    evaluate_chunk_embeddings,
    evaluate_layout_signals,
    evaluate_parse_quality,
    reconcile_quality_with_projected_pages,
)


class EvaluateChunkEmbeddingsTests(unittest.TestCase):
    def test_reports_embedding_coverage_and_mean_norm(self) -> None:
        chunks = [
            Chunk(
                chunk_id="chk-1",
                doc_id="doc-1",
                block_ids=("blk-1",),
                text="alpha",
                embedding=(3.0, 4.0),
            ),
            Chunk(
                chunk_id="chk-2",
                doc_id="doc-1",
                block_ids=("blk-2",),
                text="beta",
                embedding=None,
            ),
        ]

        report = evaluate_chunk_embeddings(chunks)

        self.assertEqual(report.total_chunks, 2)
        self.assertEqual(report.embedded_chunks, 1)
        self.assertAlmostEqual(report.embedded_chunk_ratio, 0.5)
        self.assertAlmostEqual(report.mean_embedding_dim_norm, 5.0)


class EvaluateParseQualityTests(unittest.TestCase):
    def test_flags_pdf_name_map_garble(self) -> None:
        garbled = " ".join(f"/{index % 40}" for index in range(240))
        blocks = [
            Block(
                block_id="blk-1",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content=garbled,
                metadata={"page": 1},
            )
        ]

        report = evaluate_parse_quality(blocks)

        self.assertIn("pdf_name_garble", report.flags)
        self.assertEqual(report.recommended_action, "retry_with_ocr")
        self.assertLess(report.score, 1.0)

    def test_reconciles_quality_against_ocr_projected_text(self) -> None:
        garbled = "".join(f"(cid:{index % 80})" for index in range(260))
        blocks = [
            Block(
                block_id="blk-1",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content=garbled,
                metadata={"page": 1},
            )
        ]
        raw_report = evaluate_parse_quality(blocks)

        report = reconcile_quality_with_projected_pages(
            raw_report,
            [{"page_number": 1, "text": "Recovered OCR text with readable maintenance manual content."}],
        )

        self.assertIn("cid_garble", raw_report.flags)
        self.assertNotIn("cid_garble", report.flags)
        self.assertIsNone(report.recommended_action)
        self.assertEqual(report.total_cid_tokens, 0)
        self.assertGreater(report.score, raw_report.score)


class ApiPayloadProjectionTests(unittest.TestCase):
    def test_title_only_page_defaults_to_empty_tables(self) -> None:
        blocks = [
            Block(
                block_id="blk-title",
                doc_id="doc-1",
                type=BlockType.TITLE,
                content="Manual title",
                metadata={"page": 1},
            )
        ]

        pages = _project_pages(tuple(blocks))

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["tables"], [])
        self.assertEqual(pages[0]["tables_markdown"], [])


class EvaluateLayoutSignalsTests(unittest.TestCase):
    def test_reports_ocr_attempt_and_failure_signals(self) -> None:
        blocks = [
            Block(
                block_id="blk-1",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content="native text kept",
                metadata={
                    "page": 1,
                    "layout_source": "pdfplumber",
                    "ocr_attempted": True,
                    "ocr_attempt_reason": "empty_text",
                    "ocr_error_reason": "provider_request_failed",
                },
            ),
            Block(
                block_id="blk-2",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content="ocr recovered text",
                metadata={
                    "page": 2,
                    "layout_source": "pdfplumber",
                    "ocr_attempted": True,
                    "ocr_attempt_reason": "cid_dense",
                    "ocr_fallback_used": True,
                    "ocr_fallback_reason": "cid_dense",
                },
            ),
        ]

        report = evaluate_layout_signals(blocks)

        self.assertEqual(report.pages_with_layout_metadata, 2)
        self.assertEqual(report.ocr_attempted_pages, 2)
        self.assertEqual(report.ocr_attempted_blocks, 2)
        self.assertEqual(report.ocr_fallback_pages, 1)
        self.assertEqual(report.ocr_fallback_blocks, 1)
        self.assertEqual(report.ocr_failed_pages, 1)
        self.assertEqual(report.ocr_failed_blocks, 1)
        self.assertEqual(report.layout_elapsed_s, 0.0)
        self.assertEqual(report.ocr_total_elapsed_s, 0.0)

    def test_aggregates_page_level_ocr_timing_once_per_page(self) -> None:
        blocks = [
            Block(
                block_id="blk-1",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content="page one first block",
                metadata={
                    "page": 1,
                    "layout_source": "pdfplumber",
                    "layout_elapsed_s": 1.5,
                    "ocr_attempted": True,
                    "ocr_render_elapsed_s": 2.0,
                    "ocr_input_prepare_elapsed_s": 0.01,
                    "ocr_engine_exec_elapsed_s": 2.99,
                    "ocr_call_elapsed_s": 3.0,
                    "ocr_provider_elapsed_s": 2.5,
                    "ocr_provider_det_elapsed_s": 0.4,
                    "ocr_provider_cls_elapsed_s": 0.1,
                    "ocr_provider_rec_elapsed_s": 2.0,
                    "ocr_provider_crop_count": 12,
                    "ocr_provider_cls_rotate_positive_count": 3,
                    "ocr_provider_cls_rotate_high_count": 1,
                    "ocr_postprocess_elapsed_s": 0.5,
                    "ocr_total_elapsed_s": 5.5,
                },
            ),
            Block(
                block_id="blk-2",
                doc_id="doc-1",
                type=BlockType.TABLE,
                content="page one table",
                metadata={
                    "page": 1,
                    "layout_source": "pdfplumber",
                    "layout_elapsed_s": 1.5,
                    "ocr_attempted": True,
                    "ocr_render_elapsed_s": 2.0,
                    "ocr_input_prepare_elapsed_s": 0.01,
                    "ocr_engine_exec_elapsed_s": 2.99,
                    "ocr_call_elapsed_s": 3.0,
                    "ocr_provider_elapsed_s": 2.5,
                    "ocr_provider_det_elapsed_s": 0.4,
                    "ocr_provider_cls_elapsed_s": 0.1,
                    "ocr_provider_rec_elapsed_s": 2.0,
                    "ocr_provider_crop_count": 12,
                    "ocr_provider_cls_rotate_positive_count": 3,
                    "ocr_provider_cls_rotate_high_count": 1,
                    "ocr_postprocess_elapsed_s": 0.5,
                    "ocr_total_elapsed_s": 5.5,
                },
            ),
            Block(
                block_id="blk-3",
                doc_id="doc-1",
                type=BlockType.PARAGRAPH,
                content="page two block",
                metadata={
                    "page": 2,
                    "layout_source": "pdfplumber",
                    "layout_elapsed_s": 0.75,
                    "ocr_attempted": True,
                    "ocr_engine_init_elapsed_s": 0.2,
                    "ocr_render_elapsed_s": 1.0,
                    "ocr_input_prepare_elapsed_s": 0.02,
                    "ocr_engine_exec_elapsed_s": 3.98,
                    "ocr_call_elapsed_s": 4.0,
                    "ocr_provider_elapsed_s": 3.5,
                    "ocr_provider_det_elapsed_s": 0.6,
                    "ocr_provider_cls_elapsed_s": 0.2,
                    "ocr_provider_rec_elapsed_s": 2.7,
                    "ocr_provider_crop_count": 8,
                    "ocr_provider_cls_rotate_positive_count": 2,
                    "ocr_provider_cls_rotate_high_count": 0,
                    "ocr_postprocess_elapsed_s": 0.25,
                    "ocr_total_elapsed_s": 5.25,
                },
            ),
        ]

        report = evaluate_layout_signals(blocks)

        self.assertAlmostEqual(report.layout_elapsed_s, 2.25)
        self.assertAlmostEqual(report.ocr_engine_init_elapsed_s, 0.2)
        self.assertAlmostEqual(report.ocr_render_elapsed_s, 3.0)
        self.assertAlmostEqual(report.ocr_input_prepare_elapsed_s, 0.03)
        self.assertAlmostEqual(report.ocr_engine_exec_elapsed_s, 6.97)
        self.assertAlmostEqual(report.ocr_call_elapsed_s, 7.0)
        self.assertAlmostEqual(report.ocr_provider_elapsed_s, 6.0)
        self.assertAlmostEqual(report.ocr_provider_det_elapsed_s, 1.0)
        self.assertAlmostEqual(report.ocr_provider_cls_elapsed_s, 0.3)
        self.assertAlmostEqual(report.ocr_provider_rec_elapsed_s, 4.7)
        self.assertEqual(report.ocr_provider_crop_count, 20)
        self.assertEqual(report.ocr_provider_cls_rotate_positive_count, 5)
        self.assertEqual(report.ocr_provider_cls_rotate_high_count, 1)
        self.assertAlmostEqual(report.ocr_postprocess_elapsed_s, 0.75)
        self.assertAlmostEqual(report.ocr_total_elapsed_s, 10.75)
        self.assertAlmostEqual(report.max_ocr_page_elapsed_s, 5.5)
        self.assertEqual(len(report.ocr_page_signals), 2)
        hot_page = report.ocr_hot_pages(top=1)[0]
        self.assertEqual(hot_page.page_number, 1)
        self.assertAlmostEqual(hot_page.cls_rotate_high_ratio, 1 / 12)
        sparse_page = report.ocr_sparse_cls_pages(top=1)[0]
        self.assertEqual(sparse_page.page_number, 2)
        self.assertAlmostEqual(sparse_page.cls_rotate_high_ratio, 0.0)


if __name__ == "__main__":
    unittest.main()
