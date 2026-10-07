from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from parsecore.models import Block, BlockType, Chunk, SemanticRole
from tools import corpus_manifest, regression_baseline


class RegressionBaselineTests(unittest.TestCase):
    def test_warning_run_measures_its_sealed_manifest_instead_of_old_formal(self) -> None:
        outcome = SimpleNamespace(
            job=SimpleNamespace(job_id="job-new"), blocks=(), chunks=(),
        )
        runtime = Mock(run_publication_ready=True)
        runtime.submit.return_value = outcome
        runtime.get_document.return_value = {
            "publication": {"served_run_id": "run-old"},
            "index_manifest": {"structure_quality": {"heading_body_binding_rate": 1.0}},
        }
        runtime.run_store.list_runs.return_value = (
            SimpleNamespace(job_id="job-new", parse_run_id="run-new"),
            SimpleNamespace(job_id="job-old", parse_run_id="run-old"),
        )
        runtime.get_document_preview.return_value = {
            "publication": {"served_run_id": "run-new"},
            "index_manifest": {"structure_quality": {"heading_body_binding_rate": 0.5}},
        }
        with TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "sample.txt"
            fixture.write_text("body", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                measured = regression_baseline._run_one(
                    runtime, fixture=fixture, top_pages=5, request_options={},
                )
        self.assertEqual(measured["structure_quality"]["heading_body_binding_rate"], 0.5)
        runtime.get_document_preview.assert_called_once_with(
            doc_id="regression:sample.txt", tenant_id="default", parse_run_id="run-new",
        )

    def test_regression_refuses_to_measure_another_job_when_run_is_missing(self) -> None:
        runtime = Mock(run_publication_ready=True)
        runtime.submit.return_value = SimpleNamespace(
            job=SimpleNamespace(job_id="job-new"), blocks=(), chunks=(),
        )
        runtime.run_store.list_runs.return_value = (
            SimpleNamespace(job_id="job-old", parse_run_id="run-old"),
        )
        with TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "sample.txt"
            fixture.write_text("body", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "no matching run"):
                    regression_baseline._run_one(
                        runtime, fixture=fixture, top_pages=5, request_options={},
                    )
        runtime.get_document_preview.assert_not_called()

    def test_fixture_record_captures_relative_path_from_fixture_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            fixture = fixture_root / "sample.pdf"
            fixture.write_bytes(b"pdf")

            record = regression_baseline._fixture_record(fixture, fixture_root)

        self.assertEqual(record["fixture_name"], "sample.pdf")
        self.assertEqual(record["fixture_relative_path"], "sample.pdf")

    def test_fixture_record_captures_digest_for_hash_drift_detection(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fixture = Path(temp_dir) / "sample.pdf"
            fixture.write_bytes(b"pdf")

            record = regression_baseline._fixture_record(fixture, None)

        self.assertEqual(record["fixture_sha256"], hashlib.sha256(b"pdf").hexdigest())
        self.assertEqual(record["fixture_size_bytes"], 3)

    def test_resolve_fixture_entry_path_prefers_portable_fixture_root(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            fixture = fixture_root / "sample.pdf"
            fixture.write_bytes(b"pdf")

            resolved = regression_baseline._resolve_fixture_entry_path(
                {
                    "fixture": r"D:\app\uploads\sample.pdf",
                    "fixture_relative_path": "sample.pdf",
                },
                baseline_dir=Path(temp_dir),
                fixture_root=fixture_root,
                fixture_root_env=regression_baseline.FIXTURE_ROOT_ENV,
            )

        self.assertEqual(resolved, fixture)

    def test_resolve_fixture_entry_path_uses_fixture_root_env(self) -> None:
        with TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            fixture = fixture_root / "env-sample.pdf"
            fixture.write_bytes(b"pdf")
            with patch.dict(
                "os.environ",
                {regression_baseline.FIXTURE_ROOT_ENV: str(fixture_root)},
                clear=False,
            ):
                resolved = regression_baseline._resolve_fixture_entry_path(
                    {
                        "fixture": r"D:\app\uploads\env-sample.pdf",
                        "fixture_relative_path": "env-sample.pdf",
                    },
                    baseline_dir=Path(temp_dir),
                    fixture_root=None,
                    fixture_root_env=regression_baseline.FIXTURE_ROOT_ENV,
                )

        self.assertEqual(resolved, fixture)

    def test_check_suite_reports_unverified_corpus_and_requires_flag(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "empty.json").write_text(json.dumps({"fixtures": []}), encoding="utf-8")
            suite = root / "suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "name": "disabled-multi-column",
                                "disabled": True,
                                # Empty baseline resolves to this directory; the old
                                # detector tried to read it as JSON and raised.
                                "baseline": "",
                                "fixture_relative_path": "fixtures/multi-column.pdf",
                            },
                            {"name": "missing-baseline", "baseline": "gone.json"},
                            {"name": "empty-fixtures", "baseline": "empty.json"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            default_output = io.StringIO()
            with contextlib.redirect_stdout(default_output):
                default_exit = regression_baseline.main(["check-suite", "--suite", str(suite)])
            release_output = io.StringIO()
            with contextlib.redirect_stdout(release_output):
                release_exit = regression_baseline.main(
                    ["check-suite", "--suite", str(suite), "--require-verified-corpus"]
                )

        default_lines = default_output.getvalue()
        release_lines = release_output.getvalue()
        self.assertEqual(default_exit, 0)
        self.assertIn("[suite][corpus] DISABLED: disabled-multi-column: disabled_entry", default_lines)
        self.assertIn("UNVERIFIED: missing-baseline: baseline_missing", default_lines)
        self.assertIn("UNVERIFIED: empty-fixtures: empty_fixture_set", default_lines)
        self.assertIn("[suite][corpus] CORPUS NOT VERIFIED", default_lines)
        self.assertIn("corpus NOT verified", default_lines)
        self.assertIn("status=unavailable", default_lines)

        self.assertEqual(release_exit, 1)
        self.assertIn("[suite] FAILED: corpus-not-verified", release_lines)

    def test_check_suite_empty_suite_returns_nonzero(self) -> None:
        with TemporaryDirectory() as temp_dir:
            suite = Path(temp_dir) / "suite.json"
            suite.write_text(json.dumps({"entries": []}), encoding="utf-8")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = regression_baseline.main(["check-suite", "--suite", str(suite)])

        self.assertEqual(exit_code, 1)
        self.assertIn("no entries", output.getvalue())

    def test_resolve_fixture_entry_path_delegates_to_corpus_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = root / "nested" / "sample.pdf"
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(b"pdf")
            entry = {"fixture_relative_path": "nested/sample.pdf"}

            recorded = regression_baseline._resolve_fixture_entry_path(
                entry,
                baseline_dir=root,
                fixture_root=None,
            )
            expected = corpus_manifest.resolve_fixture_entry_path(
                entry,
                baseline_dir=root,
                fixture_root=None,
            )

        self.assertEqual(recorded, expected)
        self.assertEqual(recorded, fixture)

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

    def test_check_drift_flags_structure_metric_regression(self) -> None:
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
            max_directory_recognition_drop=0.05,
            max_chapter_coverage_drop=0.05,
            max_noise_ratio_increase=0.05,
            max_heading_body_binding_drop=0.05,
            max_evidence_binding_strength_drop=0.05,
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
            "structure_quality": {
                "directory_recognition_rate": 1.0,
                "toc_recognition_rate": 1.0,
                "chapter_coverage_rate": 1.0,
                "noise_ratio": 0.05,
                "heading_body_binding_rate": 1.0,
                "evidence_binding_strength": 1.0,
                "structure_usability_score": 0.99,
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
            "structure_quality": {
                "directory_recognition_rate": 0.8,
                "toc_recognition_rate": 0.8,
                "chapter_coverage_rate": 0.7,
                "noise_ratio": 0.2,
                "heading_body_binding_rate": 0.8,
                "evidence_binding_strength": 0.7,
                "structure_usability_score": 0.64,
            },
        }

        failures = regression_baseline._check_drift(
            name="structure-sample",
            baseline=baseline,
            candidate=candidate,
            args=args,
        )

        self.assertEqual(len(failures), 5)
        self.assertTrue(any("directory_recognition_rate" in failure for failure in failures))
        self.assertTrue(any("chapter_coverage_rate" in failure for failure in failures))
        self.assertTrue(any("noise_ratio" in failure for failure in failures))
        self.assertTrue(any("heading_body_binding_rate" in failure for failure in failures))
        self.assertTrue(any("evidence_binding_strength" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
