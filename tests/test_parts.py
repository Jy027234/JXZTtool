from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parsecore.parts import (
    cleanup_artifacts,
    document_parts_projection,
    list_artifact_candidates,
)


class DocumentPartsProjectionTests(unittest.TestCase):
    def test_projects_parse_units_with_quality_signal_state(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts",
            "parse_run_id": "job-001",
            "profile": "large-pdf",
            "state": "done",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts:unit:1",
                    "source_doc_id": "doc-parts",
                    "part_doc_id": "doc-parts",
                    "part_index": 1,
                    "page_start": 1,
                    "page_end": 10,
                    "state": "done",
                    "table_count": 3,
                },
                {
                    "parse_unit_id": "doc-parts:unit:2",
                    "source_doc_id": "doc-parts",
                    "part_doc_id": "doc-parts",
                    "part_index": 2,
                    "page_start": 11,
                    "page_end": 20,
                    "state": "done",
                    "table_count": 2,
                },
            ],
            "quality_signals": [
                {
                    "code": "truncated_table",
                    "severity": "warning",
                    "page_number": 12,
                },
                {
                    "code": "ocr_failed_page",
                    "severity": "error",
                    "page_number": 15,
                },
            ],
        }

        result = document_parts_projection(payload)

        self.assertEqual(result["projection"], "parts")
        self.assertEqual(result["part_summary"]["total"], 2)
        self.assertTrue(result["part_summary"]["partitioned"])
        self.assertEqual(result["part_summary"]["states"], {"done": 1, "warning": 1})
        self.assertEqual(result["part_summary"]["active_parts"], 0)
        self.assertEqual(result["part_summary"]["queued_parts"], 0)
        self.assertEqual(result["part_summary"]["cancelled_parts"], 0)
        self.assertEqual(result["part_summary"]["rerun_compared_parts"], 0)
        self.assertEqual(result["part_summary"]["rerun_statuses"], {})
        self.assertEqual(result["part_summary"]["provider_changed_parts"], 0)
        self.assertEqual(result["part_summary"]["selected_provider_ids"], [])
        self.assertEqual(result["parts"][0]["state"], "done")
        self.assertEqual(result["parts"][1]["state"], "warning")
        self.assertEqual(result["parts"][1]["quality_signal_count"], 2)
        self.assertEqual(result["parts"][1]["quality_signal_codes"], ["ocr_failed_page", "truncated_table"])
        self.assertEqual(result["parts"][1]["severity_counts"], {"error": 1, "warning": 1})
        self.assertFalse(result["parts"][1]["rerun_supported"])
        self.assertEqual(result["parts"][1]["diagnostics"]["recommended_focus"], "quality_review")
        self.assertEqual(result["parts"][1]["action_suggestions"][0]["action_id"], "review_quality")

    def test_rag_coverage_signals_mark_part_warning(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-rag",
            "parse_run_id": "job-rag-001",
            "profile": "default",
            "state": "done",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-rag:unit:1",
                    "source_doc_id": "doc-parts-rag",
                    "part_doc_id": "doc-parts-rag",
                    "part_index": 1,
                    "page_start": 1,
                    "page_end": 2,
                    "state": "done",
                },
                {
                    "parse_unit_id": "doc-parts-rag:unit:2",
                    "source_doc_id": "doc-parts-rag",
                    "part_doc_id": "doc-parts-rag",
                    "part_index": 2,
                    "page_start": 3,
                    "page_end": 4,
                    "state": "done",
                },
            ],
            "quality_signals": [
                {
                    "code": "rag_units_without_chunks",
                    "severity": "warning",
                    "page_number": 3,
                    "detail": {"missing_reason": "no_chunks_for_indexable_units"},
                }
            ],
        }

        result = document_parts_projection(payload)

        self.assertEqual(result["part_summary"]["states"], {"done": 1, "warning": 1})
        self.assertEqual(result["parts"][0]["state"], "done")
        self.assertEqual(result["parts"][1]["state"], "warning")
        self.assertEqual(result["parts"][1]["quality_signal_codes"], ["rag_units_without_chunks"])
        self.assertEqual(result["parts"][1]["severity_counts"], {"warning": 1})
        self.assertEqual(
            [action["action_id"] for action in result["parts"][1]["action_suggestions"]],
            ["rechunk_document", "review_quality"],
        )
        self.assertEqual(
            result["parts"][1]["action_suggestions"][0]["endpoint"],
            "/v1/parse/documents/doc-parts-rag/rechunk",
        )

    def test_rerunnable_gap_part_suggests_part_rerun(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-rerun",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-rerun:unit:1",
                    "part_id": "doc-parts-rerun-part-1",
                    "page_start": 1,
                    "page_end": 1,
                    "state": "done",
                    "rerun_supported": True,
                }
            ],
            "quality_signals": [
                {
                    "code": "rag_empty_text_page",
                    "severity": "warning",
                    "page_number": 1,
                    "detail": {"missing_reason": "no_indexable_units"},
                }
            ],
        }

        result = document_parts_projection(payload)

        actions = result["parts"][0]["action_suggestions"]
        self.assertEqual(actions[0]["action_id"], "rerun_part")
        self.assertEqual(
            actions[0]["endpoint"],
            "/v1/parse/documents/doc-parts-rerun/parts/doc-parts-rerun-part-1/rerun",
        )
        self.assertEqual(
            actions[0]["payload"]["provider_route_plan"]["required_capabilities"],
            ["native-text", "local-ocr-fallback"],
        )
        self.assertEqual(actions[-1]["action_id"], "review_quality")

    def test_rerunnable_table_gap_part_suggests_part_rerun(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-table-gap",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-table-gap:unit:1",
                    "part_id": "doc-parts-table-gap-part-1",
                    "page_start": 4,
                    "page_end": 4,
                    "state": "done",
                    "rerun_supported": True,
                }
            ],
            "quality_signals": [
                {
                    "code": "rag_table_without_unit",
                    "severity": "warning",
                    "page_number": 4,
                    "detail": {"table_ids": ["doc-parts-table-gap:p4:t1"]},
                }
            ],
        }

        result = document_parts_projection(payload)

        self.assertEqual(result["parts"][0]["state"], "warning")
        self.assertEqual(result["parts"][0]["action_suggestions"][0]["action_id"], "rerun_part")
        self.assertEqual(
            result["parts"][0]["action_suggestions"][0]["payload"]["provider_route_plan"]["required_capabilities"],
            ["tables"],
        )

    def test_rerunnable_reading_order_warning_suggests_layout_rerun(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-reading-order",
            "profile": "default",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-reading-order:unit:1",
                    "part_id": "doc-parts-reading-order-part-1",
                    "page_start": 6,
                    "page_end": 6,
                    "state": "done",
                    "rerun_supported": True,
                }
            ],
            "quality_signals": [
                {
                    "code": "reading_order_low_confidence",
                    "severity": "warning",
                    "page_number": 6,
                    "detail": {"reading_order_confidence": 0.58, "threshold": 0.75},
                }
            ],
        }

        result = document_parts_projection(payload)

        self.assertEqual(result["parts"][0]["state"], "warning")
        self.assertEqual(result["parts"][0]["action_suggestions"][0]["action_id"], "rerun_part")
        self.assertEqual(
            result["parts"][0]["action_suggestions"][0]["payload"]["provider_route_plan"]["required_capabilities"],
            ["layout"],
        )

    def test_rerun_comparison_unchanged_prefers_review_over_repeat_rerun(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-rerun-unchanged",
            "profile": "table-heavy",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-rerun-unchanged:unit:1",
                    "part_id": "doc-parts-rerun-unchanged-part-1",
                    "page_start": 9,
                    "page_end": 10,
                    "state": "done",
                    "rerun_supported": True,
                    "rerun_comparison": {
                        "schema_version": "2026-06-part-rerun-comparison",
                        "status": "unchanged",
                        "changed": False,
                        "previous_job_id": "job-prev",
                        "current_job_id": "job-current",
                    },
                }
            ],
            "quality_signals": [
                {
                    "code": "rag_table_without_unit",
                    "severity": "warning",
                    "page_number": 9,
                    "detail": {"table_ids": ["doc-parts-rerun-unchanged:p9:t1"]},
                }
            ],
        }

        result = document_parts_projection(payload)

        actions = result["parts"][0]["action_suggestions"]
        self.assertEqual(
            [action["action_id"] for action in actions],
            ["inspect_provider_route_plan", "review_parse_ir", "review_quality"],
        )
        self.assertEqual(actions[0]["params"]["required_capabilities"], ["tables"])
        self.assertEqual(actions[0]["context"]["rerun_comparison"]["status"], "unchanged")
        self.assertEqual(actions[1]["params"]["projection"], "ir")
        self.assertTrue(result["parts"][0]["diagnostics"]["rerun_compared"])
        self.assertEqual(result["parts"][0]["diagnostics"]["rerun_status"], "unchanged")
        self.assertEqual(result["parts"][0]["diagnostics"]["recommended_focus"], "provider_route_plan")
        self.assertEqual(result["part_summary"]["rerun_compared_parts"], 1)
        self.assertEqual(result["part_summary"]["rerun_statuses"], {"unchanged": 1})
        self.assertEqual(result["part_summary"]["provider_changed_parts"], 0)

    def test_part_projection_exposes_provider_rerun_observability(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts-provider-route",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts-provider-route:unit:1",
                    "part_id": "doc-parts-provider-route-part-1",
                    "part_doc_id": "doc-parts-provider-route-part-1",
                    "page_start": 6,
                    "page_end": 8,
                    "state": "done",
                    "rerun_supported": True,
                    "provider_ids": ["pdf-layout"],
                    "coverage_summary": {
                        "total_pages": 3,
                        "pages_with_parsed_text": 3,
                        "pages_with_indexable_units": 3,
                        "pages_missing_rag_units": 0,
                        "pages_missing_chunks": 0,
                        "pages_chunks_not_embedded": 0,
                        "pages_with_coverage_gaps": 1,
                        "pages_table_without_units": 1,
                        "pages_figure_caption_missing": 0,
                        "total_indexable_units": 3,
                        "total_chunked_units": 3,
                        "total_unit_count": 3,
                        "skipped_unit_count": 0,
                        "embedded_unit_count": 3,
                        "unembedded_unit_count": 0,
                        "gap_unit_ids": ["doc-parts-provider-route:ku:000002"],
                        "gap_pages": [
                            {
                                "page_number": 7,
                                "missing_reason": None,
                                "unit_ids": ["doc-parts-provider-route:ku:000002"],
                                "indexable_unit_ids": ["doc-parts-provider-route:ku:000002"],
                                "unchunked_unit_ids": [],
                                "unembedded_unit_ids": [],
                                "table_ids_without_units": ["doc-parts-provider-route:p7:t1"],
                                "figure_ids_missing_caption": [],
                                "quality_signal_codes": ["rag_table_without_unit"],
                            }
                        ],
                        "text_page_coverage_ratio": 1.0,
                        "unit_chunk_coverage_ratio": 1.0,
                        "table_unit_coverage_ratio": 0.5,
                    },
                    "coverage_gap_pages": [
                        {
                            "page_number": 7,
                            "missing_reason": None,
                            "unit_ids": ["doc-parts-provider-route:ku:000002"],
                            "indexable_unit_ids": ["doc-parts-provider-route:ku:000002"],
                            "unchunked_unit_ids": [],
                            "unembedded_unit_ids": [],
                            "table_ids_without_units": ["doc-parts-provider-route:p7:t1"],
                            "figure_ids_missing_caption": [],
                            "quality_signal_codes": ["rag_table_without_unit"],
                        }
                    ],
                    "rag_coverage_quality": {
                        "score": 1.0,
                        "gate": "accept_with_warning",
                        "flags": ["rag_table_without_unit"],
                        "warnings": ["1 page(s) have tables without indexable RAG units"],
                        "recommended_action": "local_provider_rerun",
                    },
                    "previous_part_observation": {
                        "schema_version": "2026-06-part-observation",
                        "job_id": "job-prev-provider-route",
                        "state": "warning",
                        "quality_signal_count": 2,
                        "quality_signal_codes": ["rag_empty_text_page", "rag_table_without_unit"],
                        "provider_ids": ["pdf-text"],
                        "selected_provider_id": "pdf-text",
                    },
                    "rerun_comparison": {
                        "schema_version": "2026-06-part-rerun-comparison",
                        "status": "improved",
                        "changed": True,
                        "improved": True,
                        "regressed": False,
                        "improvement_axes": ["quality_signal_count"],
                        "regression_axes": [],
                        "previous_job_id": "job-prev-provider-route",
                        "current_job_id": "job-current-provider-route",
                        "previous_selected_provider_id": "pdf-text",
                        "current_selected_provider_id": "pdf-layout",
                        "provider_changed": True,
                        "quality_signal_count_delta": -1,
                        "coverage_gap_delta": 0,
                        "gap_unit_count_delta": -1,
                        "unembedded_unit_count_delta": 0,
                        "text_page_coverage_ratio_delta": 0.0,
                        "unit_chunk_coverage_ratio_delta": 0.0,
                        "table_unit_coverage_ratio_delta": 0.5,
                        "flags_added": [],
                        "flags_removed": ["rag_empty_text_page"],
                        "quality_signal_codes_added": [],
                        "quality_signal_codes_removed": ["rag_empty_text_page"],
                        "gap_unit_ids_added": [],
                        "gap_unit_ids_removed": ["doc-parts-provider-route:ku:000001"],
                        "previous_gap_unit_ids": ["doc-parts-provider-route:ku:000001"],
                        "current_gap_unit_ids": ["doc-parts-provider-route:ku:000002"],
                        "previous_coverage_gap_pages": [7],
                        "current_coverage_gap_pages": [7],
                    },
                    "provider_route_plan": {"required_capabilities": ["layout", "tables"]},
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
                            "file_name": "manual.pdf",
                            "profile": "large-pdf",
                            "required_capabilities": ["layout", "tables"],
                            "include_disabled": False,
                        },
                    },
                }
            ],
            "quality_signals": [],
        }

        result = document_parts_projection(payload)

        part = result["parts"][0]
        self.assertEqual(part["provider_ids"], ["pdf-layout"])
        self.assertEqual(part["provider_route_plan"]["required_capabilities"], ["layout", "tables"])
        self.assertEqual(part["selected_provider_id"], "pdf-layout")
        self.assertEqual(part["route_status"], "selected")
        self.assertEqual(part["local_provider_routing"]["selected_provider_id"], "pdf-layout")
        self.assertEqual(part["coverage_gap_count"], 1)
        self.assertEqual(part["coverage_gap_unit_count"], 1)
        self.assertEqual(part["coverage_summary"]["pages_with_coverage_gaps"], 1)
        self.assertEqual(part["coverage_summary"]["gap_unit_ids"], ["doc-parts-provider-route:ku:000002"])
        self.assertEqual(part["coverage_gap_pages"][0]["page_number"], 7)
        self.assertEqual(part["coverage_gap_pages"][0]["unit_ids"], ["doc-parts-provider-route:ku:000002"])
        self.assertEqual(part["rag_coverage_quality"]["recommended_action"], "local_provider_rerun")
        self.assertEqual(part["previous_part_observation"]["job_id"], "job-prev-provider-route")
        self.assertEqual(part["rerun_comparison"]["status"], "improved")
        self.assertTrue(part["rerun_comparison"]["provider_changed"])
        self.assertEqual(part["rerun_comparison"]["current_selected_provider_id"], "pdf-layout")
        self.assertEqual(part["rerun_comparison"]["gap_unit_count_delta"], -1)
        self.assertEqual(part["diagnostics"]["rerun_status"], "improved")
        self.assertTrue(part["diagnostics"]["rerun_compared"])
        self.assertTrue(part["diagnostics"]["provider_changed"])
        self.assertEqual(part["diagnostics"]["previous_selected_provider_id"], "pdf-text")
        self.assertEqual(part["diagnostics"]["current_selected_provider_id"], "pdf-layout")
        self.assertEqual(part["diagnostics"]["quality_signal_count_delta"], -1)
        self.assertEqual(part["diagnostics"]["coverage_gap_delta"], 0)
        self.assertEqual(part["diagnostics"]["gap_unit_count"], 1)
        self.assertEqual(part["diagnostics"]["gap_unit_count_delta"], -1)
        self.assertEqual(part["diagnostics"]["unembedded_unit_count"], 0)
        self.assertEqual(part["diagnostics"]["recommended_focus"], "coverage_gaps")
        self.assertEqual(result["part_summary"]["gap_unit_parts"], 1)
        self.assertEqual(result["part_summary"]["gap_unit_count"], 1)
        self.assertEqual(result["part_summary"]["unembedded_unit_count"], 0)
        self.assertEqual(result["part_summary"]["rerun_compared_parts"], 1)
        self.assertEqual(result["part_summary"]["rerun_statuses"], {"improved": 1})
        self.assertEqual(result["part_summary"]["provider_changed_parts"], 1)
        self.assertEqual(result["part_summary"]["selected_provider_ids"], ["pdf-layout"])

    def test_filters_part_states_with_comma_and_pipe(self) -> None:
        payload = {
            "doc_id": "doc-parts",
            "parse_units": [
                {"parse_unit_id": "u1", "state": "done", "page_start": 1, "page_end": 1},
                {"parse_unit_id": "u2", "state": "failed", "page_start": 2, "page_end": 2},
                {"parse_unit_id": "u3", "state": "parsing", "page_start": 3, "page_end": 3},
            ],
        }

        result = document_parts_projection(payload, state_filter="failed|parsing")

        self.assertEqual(result["state_filter"], ["failed", "parsing"])
        self.assertEqual([part["part_id"] for part in result["parts"]], ["u2", "u3"])
        self.assertEqual(result["part_summary"]["filtered"], 2)

    def test_counts_cancelled_and_active_part_states(self) -> None:
        payload = {
            "doc_id": "doc-parts",
            "parse_units": [
                {"parse_unit_id": "u1", "state": "cancelled", "page_start": 1, "page_end": 1},
                {"parse_unit_id": "u2", "state": "parsing", "page_start": 2, "page_end": 2},
                {"parse_unit_id": "u3", "state": "pending", "page_start": 3, "page_end": 3},
            ],
        }

        result = document_parts_projection(payload, state_filter="cancelled")

        self.assertEqual(result["part_summary"]["cancelled_parts"], 1)
        self.assertEqual(result["part_summary"]["active_parts"], 1)
        self.assertEqual(result["part_summary"]["queued_parts"], 1)
        self.assertEqual([part["part_id"] for part in result["parts"]], ["u1"])

    def test_rejects_invalid_state_filter(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_part_state"):
            document_parts_projection({"parse_units": []}, state_filter="bogus")


if __name__ == "__main__":
    unittest.main()


class ArtifactCleanupTests(unittest.TestCase):
    """P5-T10: part / export artifact cleanup strategy."""

    def test_cleanup_artifacts_dry_run_lists_candidates_without_removing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_file = root / "old_part.pdf"
            old_file.write_text("old", encoding="utf-8")
            # Force mtime to 2 days ago
            old_mtime = time.time() - 2 * 86400
            old_file.touch()
            import os
            os.utime(old_file, (old_mtime, old_mtime))

            new_file = root / "new_part.pdf"
            new_file.write_text("new", encoding="utf-8")

            report = cleanup_artifacts(
                root, retention_seconds=86400, kind="part_pdf", dry_run=True
            )

            self.assertTrue(report["dry_run"])
            self.assertEqual(report["kind"], "part_pdf")
            self.assertEqual(report["candidates"], 1)
            self.assertEqual(report["removed"], 0)
            self.assertTrue(old_file.exists())
            self.assertTrue(new_file.exists())
            actions = [f["action"] for f in report["files"]]
            self.assertEqual(actions, ["skip"])

    def test_cleanup_artifacts_removes_old_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_file = root / "old_part.pdf"
            old_file.write_text("old", encoding="utf-8")
            old_mtime = time.time() - 8 * 86400
            import os
            os.utime(old_file, (old_mtime, old_mtime))

            new_file = root / "new_part.pdf"
            new_file.write_text("new", encoding="utf-8")

            report = cleanup_artifacts(
                root, retention_seconds=7 * 86400, kind="part_pdf", dry_run=False
            )

            self.assertFalse(report["dry_run"])
            self.assertEqual(report["candidates"], 1)
            self.assertEqual(report["removed"], 1)
            self.assertEqual(report["errors"], 0)
            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())

    def test_cleanup_artifacts_none_root_returns_empty(self) -> None:
        report = cleanup_artifacts(None, retention_seconds=86400, dry_run=True)
        self.assertEqual(report["candidates"], 0)
        self.assertEqual(report["removed"], 0)

    def test_list_artifact_candidates_nonexistent_dir_returns_empty(self) -> None:
        candidates = list_artifact_candidates(
            "/nonexistent/path", retention_seconds=86400, kind="part_pdf"
        )
        self.assertEqual(candidates, [])

    def test_cleanup_artifacts_schema_version_present(self) -> None:
        with TemporaryDirectory() as tmpdir:
            report = cleanup_artifacts(
                Path(tmpdir), retention_seconds=86400, dry_run=True
            )
        self.assertEqual(report["schema_version"], "2026-06-artifact-cleanup")
