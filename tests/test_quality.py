from __future__ import annotations

import unittest

from parsecore.models import Block, BlockType, Chunk
from parsecore.quality import evaluate_chunk_embeddings, evaluate_layout_signals


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
                    "ocr_call_elapsed_s": 3.0,
                    "ocr_provider_elapsed_s": 2.5,
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
                    "ocr_call_elapsed_s": 3.0,
                    "ocr_provider_elapsed_s": 2.5,
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
                    "ocr_call_elapsed_s": 4.0,
                    "ocr_provider_elapsed_s": 3.5,
                    "ocr_postprocess_elapsed_s": 0.25,
                    "ocr_total_elapsed_s": 5.25,
                },
            ),
        ]

        report = evaluate_layout_signals(blocks)

        self.assertAlmostEqual(report.layout_elapsed_s, 2.25)
        self.assertAlmostEqual(report.ocr_engine_init_elapsed_s, 0.2)
        self.assertAlmostEqual(report.ocr_render_elapsed_s, 3.0)
        self.assertAlmostEqual(report.ocr_call_elapsed_s, 7.0)
        self.assertAlmostEqual(report.ocr_provider_elapsed_s, 6.0)
        self.assertAlmostEqual(report.ocr_postprocess_elapsed_s, 0.75)
        self.assertAlmostEqual(report.ocr_total_elapsed_s, 10.75)
        self.assertAlmostEqual(report.max_ocr_page_elapsed_s, 5.5)


if __name__ == "__main__":
    unittest.main()