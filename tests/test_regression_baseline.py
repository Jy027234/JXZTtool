from __future__ import annotations

import argparse
import unittest

from parsecore.models import Block, BlockType, Chunk, SemanticRole
from tools import regression_baseline


class RegressionBaselineTests(unittest.TestCase):
    def test_request_option_overrides_include_table_stage_when_enabled(self) -> None:
        args = argparse.Namespace(
            enable_layout_reading_order=None,
            enable_table_structure=True,
            table_output_format="markdown",
            table_header_rows=2,
        )

        overrides = regression_baseline._request_option_overrides_from_args(args)

        self.assertEqual(overrides["source"], "regression-baseline")
        self.assertTrue(overrides["enrichment"]["table_structure"]["enabled"])
        self.assertEqual(
            overrides["enrichment"]["table_structure"]["output_format"],
            "markdown",
        )
        self.assertEqual(overrides["enrichment"]["table_structure"]["header_rows"], 2)

    def test_request_option_overrides_include_layout_stage_when_enabled(self) -> None:
        args = argparse.Namespace(
            enable_layout_reading_order=True,
            enable_table_structure=None,
            table_output_format=None,
            table_header_rows=None,
        )

        overrides = regression_baseline._request_option_overrides_from_args(args)

        self.assertEqual(overrides["source"], "regression-baseline")
        self.assertTrue(overrides["post_process"]["layout_reading_order"])

    def test_layout_quality_detects_applied_pages(self) -> None:
        blocks = (
            Block(
                block_id="blk-1",
                doc_id="doc-layout",
                type=BlockType.PARAGRAPH,
                content="Column one",
                metadata={
                    "page": 1,
                    "column_count_hint": 2,
                    "layout_reading_order_applied": True,
                    "layout_reading_order_strategy": "column-reflow",
                },
            ),
            Block(
                block_id="blk-2",
                doc_id="doc-layout",
                type=BlockType.PARAGRAPH,
                content="Column two",
                metadata={
                    "page": 1,
                    "column_count_hint": 2,
                    "layout_reading_order_applied": True,
                    "layout_reading_order_strategy": "column-reflow",
                },
            ),
        )

        metrics = regression_baseline._layout_reading_order_quality_to_dict(
            blocks=blocks,
            request_options={"source": "regression-baseline"},
        )

        self.assertTrue(metrics["enabled"])
        self.assertEqual(metrics["multi_column_pages"], 1)
        self.assertEqual(metrics["applied_pages"], 1)
        self.assertEqual(metrics["applied_blocks"], 2)
        self.assertEqual(metrics["applied_page_ratio"], 1.0)

    def test_table_quality_detects_markdown_ready_chunks(self) -> None:
        blocks = (
            Block(
                block_id="blk-1",
                doc_id="doc-table",
                type=BlockType.TABLE,
                content="Part\tQty\nBolt\t2",
                metadata={
                    "semantic_role": SemanticRole.TABLE.value,
                    "cells": [["Part", "Qty"], ["Bolt", "2"]],
                },
            ),
        )
        chunks = (
            Chunk(
                chunk_id="chk-1",
                doc_id="doc-table",
                block_ids=("blk-1",),
                text="| Part | Qty |\n| --- | --- |\n| Bolt | 2 |",
                semantic_role=SemanticRole.TABLE.value,
            ),
        )

        metrics = regression_baseline._table_quality_to_dict(
            blocks=blocks,
            chunks=chunks,
            request_options={
                "source": "regression-baseline",
                "enrichment": {
                    "table_structure": {"enabled": True, "output_format": "markdown"}
                },
            },
        )

        self.assertTrue(metrics["enabled"])
        self.assertEqual(metrics["table_block_count"], 1)
        self.assertEqual(metrics["table_blocks_with_cells"], 1)
        self.assertEqual(metrics["rendered_ready_chunks"], 1)
        self.assertEqual(metrics["rendered_ready_ratio"], 1.0)

    def test_check_drift_flags_table_metric_regression(self) -> None:
        args = argparse.Namespace(
            max_very_short_delta=0.01,
            max_block_count_delta_pct=0.05,
            max_page_count_delta=0,
            max_numeric_heavy_delta=2,
            max_header_footer_delta=2,
            max_embedded_chunk_ratio_drop=0.05,
            max_layout_reading_order_pages_drop=0,
            max_layout_reading_order_page_ratio_drop=0.05,
            max_table_rendered_ready_ratio_drop=0.05,
            max_table_blocks_with_cells_drop=0,
        )
        baseline = {
            "block_counts": {"total": 10},
            "quality": {
                "very_short_ratio": 0.0,
                "page_count": 1,
                "numeric_heavy_total": 0,
                "suspected_header_footer_total": 0,
            },
            "embedding_quality": {"embedded_chunk_ratio": 1.0},
            "table_quality": {
                "enabled": True,
                "rendered_ready_ratio": 1.0,
                "table_blocks_with_cells": 2,
            },
        }
        candidate = {
            "block_counts": {"total": 10},
            "quality": {
                "very_short_ratio": 0.0,
                "page_count": 1,
                "numeric_heavy_total": 0,
                "suspected_header_footer_total": 0,
            },
            "embedding_quality": {"embedded_chunk_ratio": 1.0},
            "table_quality": {
                "enabled": True,
                "rendered_ready_ratio": 0.5,
                "table_blocks_with_cells": 1,
            },
        }

        failures = regression_baseline._check_drift(
            name="table-sample",
            baseline=baseline,
            candidate=candidate,
            args=args,
        )

        self.assertEqual(len(failures), 2)
        self.assertTrue(any("rendered_ready_ratio" in failure for failure in failures))
        self.assertTrue(any("table_blocks_with_cells" in failure for failure in failures))

    def test_check_drift_flags_layout_metric_regression(self) -> None:
        args = argparse.Namespace(
            max_very_short_delta=0.01,
            max_block_count_delta_pct=0.05,
            max_page_count_delta=0,
            max_numeric_heavy_delta=2,
            max_header_footer_delta=2,
            max_embedded_chunk_ratio_drop=0.05,
            max_layout_reading_order_pages_drop=0,
            max_layout_reading_order_page_ratio_drop=0.05,
            max_table_rendered_ready_ratio_drop=0.05,
            max_table_blocks_with_cells_drop=0,
        )
        baseline = {
            "block_counts": {"total": 10},
            "quality": {
                "very_short_ratio": 0.0,
                "page_count": 1,
                "numeric_heavy_total": 0,
                "suspected_header_footer_total": 0,
            },
            "embedding_quality": {"embedded_chunk_ratio": 1.0},
            "layout_quality": {
                "enabled": True,
                "multi_column_pages": 2,
                "applied_pages": 2,
                "applied_blocks": 4,
                "applied_page_ratio": 1.0,
            },
        }
        candidate = {
            "block_counts": {"total": 10},
            "quality": {
                "very_short_ratio": 0.0,
                "page_count": 1,
                "numeric_heavy_total": 0,
                "suspected_header_footer_total": 0,
            },
            "embedding_quality": {"embedded_chunk_ratio": 1.0},
            "layout_quality": {
                "enabled": True,
                "multi_column_pages": 2,
                "applied_pages": 1,
                "applied_blocks": 2,
                "applied_page_ratio": 0.5,
            },
        }

        failures = regression_baseline._check_drift(
            name="layout-sample",
            baseline=baseline,
            candidate=candidate,
            args=args,
        )

        self.assertEqual(len(failures), 2)
        self.assertTrue(any("applied_pages" in failure for failure in failures))
        self.assertTrue(any("applied_page_ratio" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
