"""P0-04: the quality rules that decide publication.

The point of these tests is that a run cannot reach ``published`` while any of
the reproduced delivery problems is present: a placeholder result, a missing
required artifact, a broken source reference, or a validator that failed.
"""

from __future__ import annotations

import unittest

from parsecore.models import Block, BlockType, Chunk
from parsecore.publication import ValidationStatus
from parsecore.validation import (
    VALIDATION_RULE_VERSION,
    ValidationReport,
    ValidationRule,
    validate_run,
)


def _block(
    block_id: str,
    text: str,
    *,
    page: int = 1,
    metadata: dict | None = None,
    block_type: BlockType = BlockType.PARAGRAPH,
) -> Block:
    payload = {"page": page}
    payload.update(metadata or {})
    return Block(
        block_id=block_id,
        doc_id="doc-1",
        type=block_type,
        content=text,
        metadata=payload,
    )


def _views(pages: int = 1, *, records: int = 0) -> dict[str, list[dict[str, object]]]:
    view: dict[str, list[dict[str, object]]] = {
        "pages": [{"page_number": number, "text": "x"} for number in range(1, pages + 1)]
    }
    if records:
        view["records"] = [
            {"record_id": f"r{index}", "page_start": 1, "fields": {"text": "x"}}
            for index in range(records)
        ]
    return view


def _healthy_blocks() -> tuple[Block, ...]:
    return (
        _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
        _block("b2", "Apply the specified torque to the fastener before inspection."),
    )


def _healthy_chunks() -> tuple[Chunk, ...]:
    return (
        Chunk(
            chunk_id="c1",
            doc_id="doc-1",
            block_ids=("b2",),
            text="Apply the specified torque to the fastener before inspection.",
        ),
    )


class ValidationReportShapeTests(unittest.TestCase):
    def test_healthy_run_passes(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(), chunks=_healthy_chunks(), views=_views()
        )
        self.assertIs(report.status, ValidationStatus.PASSED)
        self.assertEqual(report.reason, "all_required_rules_passed")
        self.assertEqual(report.errors, ())
        self.assertEqual(report.warnings, ())
        self.assertEqual(report.rule_version, VALIDATION_RULE_VERSION)
        self.assertIn("quality_score", report.metrics)

    def test_report_payload_is_json_serialisable_and_carries_the_policy(self) -> None:
        import json

        report = validate_run(
            blocks=_healthy_blocks(), chunks=_healthy_chunks(), views=_views()
        )
        payload = report.as_payload()
        json.dumps(payload)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["policy_version"], "2026-09-validation-policy-v1")
        self.assertEqual(payload["rule_version"], VALIDATION_RULE_VERSION)
        self.assertIn("rules", payload)

    def test_unknown_profile_is_an_error_not_a_pass(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            artifact_profile="not-a-profile",
        )
        self.assertIs(report.status, ValidationStatus.ERROR)
        self.assertEqual(report.reason, "unknown_artifact_profile")
        self.assertIn("unknown_artifact_profile", report.errors)


class ReproducedProblemTests(unittest.TestCase):
    """Each reproduced delivery problem must be blocked by a rule."""

    def test_placeholder_result_cannot_pass(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            placeholder_parsers=("mineru-local",),
        )
        self.assertIs(report.status, ValidationStatus.REJECTED)
        self.assertIn("placeholder_result", report.rejections)
        self.assertIn("mineru-local", report.rules[0].message)

    def test_missing_reader_view_is_rejected(self) -> None:
        report = validate_run(blocks=_healthy_blocks(), chunks=_healthy_chunks(), views={})
        self.assertIs(report.status, ValidationStatus.REJECTED)
        self.assertIn("required_artifact_missing:reader", report.rejections)

    def test_missing_blocks_are_rejected(self) -> None:
        report = validate_run(blocks=(), chunks=(), views=_views())
        self.assertIs(report.status, ValidationStatus.REJECTED)
        codes = set(report.rejections)
        self.assertIn("empty_document", codes)
        self.assertIn("required_artifact_missing:blocks", codes)

    def test_cid_garble_requires_review(self) -> None:
        # The rest of the page is usually usable, so a garbled token marks the run
        # for review instead of discarding the document. Such a run is delivered as
        # an unreviewed preview and is not published.
        blocks = (
            _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
            _block("b2", "(cid:12)(cid:34)(cid:56) torque value missing"),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())
        self.assertIs(report.status, ValidationStatus.NEEDS_REVIEW)
        self.assertIn("cid_garble", report.reviews)
        self.assertEqual(report.metrics["total_cid_tokens"], 3)

    def test_broken_source_reference_signal_is_rejected(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            quality_signals=(
                {
                    "code": "source_reference_broken",
                    "severity": "error",
                    "message": "block b9 is referenced but not present",
                },
            ),
        )
        self.assertIs(report.status, ValidationStatus.REJECTED)
        self.assertIn("quality_signal:source_reference_broken", report.rejections)


class ThresholdAndSeverityTests(unittest.TestCase):
    def test_low_reading_order_confidence_is_a_warning_by_default(self) -> None:
        blocks = (
            _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
            _block(
                "b2",
                "Apply the specified torque to the fastener before inspection.",
                metadata={"reading_order_confidence": 0.4},
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())
        self.assertIs(report.status, ValidationStatus.WARNING)
        self.assertIn("low_reading_order_confidence", report.warnings)
        self.assertAlmostEqual(report.metrics["min_reading_order_confidence"], 0.4, places=4)

    def test_reading_order_threshold_is_configurable(self) -> None:
        blocks = (
            _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
            _block(
                "b2",
                "Apply the specified torque to the fastener before inspection.",
                metadata={"reading_order_confidence": 0.6},
            ),
        )
        strict = validate_run(
            blocks=blocks,
            chunks=_healthy_chunks(),
            views=_views(),
            options={"min_reading_order_confidence": 0.9},
        )
        relaxed = validate_run(
            blocks=blocks,
            chunks=_healthy_chunks(),
            views=_views(),
            options={"min_reading_order_confidence": 0.5},
        )
        self.assertIs(strict.status, ValidationStatus.WARNING)
        self.assertIs(relaxed.status, ValidationStatus.PASSED)

    def test_warning_severity_signal_does_not_reject(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            quality_signals=(
                {"code": "noisy_page", "severity": "warning", "message": "page 2 noisy"},
            ),
        )
        self.assertIs(report.status, ValidationStatus.WARNING)
        self.assertIn("quality_signal:noisy_page", report.warnings)

    def test_non_error_severity_is_treated_as_a_warning(self) -> None:
        # Only ``error`` severity rejects; anything else is advisory, so an
        # unfamiliar severity string must not silently become a rejection.
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            quality_signals=({"code": "mystery", "severity": "catastrophic"},),
        )
        self.assertIs(report.status, ValidationStatus.WARNING)
        self.assertIn("quality_signal:mystery", report.warnings)

    def test_signal_without_a_code_is_ignored(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            quality_signals=({"severity": "error", "message": "no code"},),
        )
        self.assertIs(report.status, ValidationStatus.PASSED)


class PrecedenceTests(unittest.TestCase):
    def test_rejected_outranks_needs_review_and_warning(self) -> None:
        # A missing required artifact rejects, which outranks review and warning
        # findings on the same run.
        report = validate_run(
            blocks=(
                _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
                _block("b2", "", page=4, metadata={"ocr_error_reason": "engine_timeout"}),
            ),
            chunks=(),
            views={"pages": []},
            quality_signals=({"code": "noisy", "severity": "warning"},),
        )
        self.assertIs(report.status, ValidationStatus.REJECTED)

    def test_needs_review_outranks_warning(self) -> None:
        blocks = (
            _block("b1", "Maintenance Manual", block_type=BlockType.TITLE),
            _block("b2", "", page=2, metadata={"ocr_error_reason": "engine_timeout"}),
            _block(
                "b3",
                "Apply the specified torque to the fastener before inspection.",
                metadata={"reading_order_confidence": 0.3},
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views(pages=2))
        self.assertIs(report.status, ValidationStatus.NEEDS_REVIEW)
        self.assertIn("low_reading_order_confidence", report.warnings)

    def test_every_non_passing_rule_is_recorded_as_evidence(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            quality_signals=({"code": "noisy", "severity": "warning"},),
        )
        self.assertIn("rule:quality_signal:noisy", report.evidence_refs)


class OptionalArtifactTests(unittest.TestCase):
    def test_missing_optional_artifact_is_informational_not_a_failure(self) -> None:
        # ``default`` requires blocks + reader; records, rag, assets and export
        # are optional.  A deployment that does not vectorise must stay on the
        # "passed" path rather than being pushed onto the warning policy.
        report = validate_run(blocks=_healthy_blocks(), chunks=(), views=_views())
        self.assertIs(report.status, ValidationStatus.PASSED)
        self.assertIn("optional_artifacts_empty", report.informational)
        self.assertEqual(report.warnings, ())
        self.assertEqual(
            report.metrics["degraded_optional_artifacts"],
            ["assets", "export", "rag", "records"],
        )
        self.assertIn("optional_artifacts_empty", report.metrics["informational_rules"])

    def test_records_profile_requires_records_and_export(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            artifact_profile="records",
        )
        self.assertIs(report.status, ValidationStatus.REJECTED)
        self.assertIn("required_artifact_missing:export", report.rejections)

    def test_rag_profile_requires_chunks(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=(),
            views=_views(),
            artifact_profile="rag",
        )
        self.assertIs(report.status, ValidationStatus.REJECTED)
        self.assertIn("required_artifact_missing:rag", report.rejections)

    def test_explicit_required_artifacts_override_the_profile(self) -> None:
        report = validate_run(
            blocks=_healthy_blocks(),
            chunks=_healthy_chunks(),
            views=_views(),
            required_artifacts=("blocks",),
        )
        # With only ``blocks`` required, the empty rag is informational and the
        # page view is not required at all.
        self.assertIs(report.status, ValidationStatus.PASSED)
        self.assertEqual(report.rejections, ())
        self.assertIn("optional_artifacts_empty", report.informational)


class ValidationRuleTests(unittest.TestCase):
    def test_a_table_grid_out_of_range_is_reported(self) -> None:
        blocks = _healthy_blocks() + (
            _block(
                "t1",
                "| a | b |",
                block_type=BlockType.TABLE,
                metadata={
                    "rows": 2,
                    "cols": 2,
                    "cells": [["a", "b"], ["c", "d"]],
                    "table_cells": [
                        {"row_index": 0, "col_index": 0},
                        {"row_index": 0, "col_index": 1},
                        {"row_index": 1, "col_index": 0},
                        {"row_index": 1, "col_index": 5},
                    ],
                },
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())

        self.assertIs(report.status, ValidationStatus.WARNING)
        self.assertIn("table_grid_invalid", report.warnings)
        self.assertEqual(report.metrics["table_grid_defects"], ["t1:1_cells_out_of_range"])

    def test_two_cells_in_one_position_are_reported(self) -> None:
        blocks = _healthy_blocks() + (
            _block(
                "t1",
                "| a | b |",
                block_type=BlockType.TABLE,
                metadata={
                    "rows": 1,
                    "cols": 2,
                    "table_cells": [
                        {"row_index": 0, "col_index": 1},
                        {"row_index": 0, "col_index": 1},
                    ],
                },
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())

        self.assertIn("table_grid_invalid", report.warnings)
        self.assertEqual(report.metrics["table_grid_defects"], ["t1:1_cells_overlap"])

    def test_a_well_formed_table_grid_is_not_reported(self) -> None:
        blocks = _healthy_blocks() + (
            _block(
                "t1",
                "| a | b |",
                block_type=BlockType.TABLE,
                metadata={
                    "rows": 1,
                    "cols": 2,
                    "table_cells": [
                        {"row_index": 0, "col_index": 0},
                        {"row_index": 0, "col_index": 1},
                    ],
                },
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())

        self.assertNotIn("table_grid_invalid", report.warnings)
        self.assertIs(report.status, ValidationStatus.PASSED)

    def test_a_table_without_cell_positions_is_not_judged(self) -> None:
        # A parser that only publishes rows of strings has no grid to be wrong
        # about, and must not be warned for it.
        blocks = _healthy_blocks() + (
            _block(
                "t1",
                "| a | b |",
                block_type=BlockType.TABLE,
                metadata={"rows": 1, "cols": 2, "cells": [["a", "b"]]},
            ),
        )
        report = validate_run(blocks=blocks, chunks=_healthy_chunks(), views=_views())

        self.assertNotIn("table_grid_invalid", report.warnings)

    def test_rule_payload_omits_empty_metric_fields(self) -> None:
        rule = ValidationRule(
            code="empty_document", status=ValidationStatus.REJECTED, message="no blocks"
        )
        self.assertEqual(
            rule.as_payload(),
            {"code": "empty_document", "status": "rejected", "message": "no blocks"},
        )

    def test_report_is_immutable(self) -> None:
        report = ValidationReport(status=ValidationStatus.PASSED, reason="ok")
        with self.assertRaises(Exception):
            report.status = ValidationStatus.REJECTED  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
