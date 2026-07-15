from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing

from tests.support import TemporaryWorkspace
from tools import large_pdf_stress


PDF_CONFIG = """
[project]
name = "test-parsecore-stress"
mode = "embedded-sdk"

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[product]
adapter = "embedded"

[[parsers]]
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
""".strip()


class LargePdfStressTests(unittest.TestCase):
    def test_default_job_store_is_temporary_and_does_not_touch_configured_database(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            configured_database = workspace.root / "parsecore.db"
            report = large_pdf_stress.build_report(
                config=workspace.config_path,
                generated_pdf=workspace.root / "stress-isolated.pdf",
                generate_pages=3,
                lines_per_page=3,
                target_pages_per_part=1,
                doc_id="doc-stress-isolated",
            )
            configured_database_exists = configured_database.exists()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["job_store"]["mode"], "temporary_sqlite")
        self.assertFalse(report["job_store"]["configured_store_used"])
        self.assertEqual(report["job_store"]["cleanup"], "removed_after_report")
        self.assertFalse(configured_database_exists)

    def test_configured_job_store_requires_explicit_opt_in(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            configured_database = workspace.root / "parsecore.db"
            report = large_pdf_stress.build_report(
                config=workspace.config_path,
                generated_pdf=workspace.root / "stress-persisted.pdf",
                generate_pages=3,
                lines_per_page=3,
                target_pages_per_part=1,
                doc_id="doc-stress-persisted",
                use_configured_job_store=True,
            )
            with closing(sqlite3.connect(configured_database)) as conn:
                state_counts = dict(
                    conn.execute(
                        "SELECT state, COUNT(*) FROM parse_jobs GROUP BY state"
                    ).fetchall()
                )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["job_store"]["mode"], "configured")
        self.assertTrue(report["job_store"]["configured_store_used"])
        self.assertEqual(state_counts, {"partial": 1, "pending": 4})

    def test_build_report_plans_parts_and_records_manifest_summary(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            generated_pdf = workspace.root / "stress.pdf"
            report = large_pdf_stress.build_report(
                config=workspace.config_path,
                generated_pdf=generated_pdf,
                generate_pages=4,
                lines_per_page=3,
                target_pages_per_part=2,
                doc_id="doc-stress",
                execute_parts=True,
                max_parts=1,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["total_pages"], 4)
        self.assertEqual(report["summary"]["planned_parts"], 2)
        self.assertEqual(report["summary"]["executed_parts"], 1)
        self.assertEqual(report["manifest_part_index"]["part_count"], 2)
        self.assertEqual(report["manifest_part_index"]["indexed_part_count"], 1)
        self.assertTrue(report["part_timings"][0]["chunks"])

    def test_main_writes_json_and_markdown(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            out_json = workspace.root / "stress.json"
            out_md = workspace.root / "stress.md"
            exit_code = large_pdf_stress.main(
                [
                    "--config",
                    str(workspace.config_path),
                    "--generated-pdf",
                    str(workspace.root / "stress-main.pdf"),
                    "--generate-pages",
                    "3",
                    "--target-pages-per-part",
                    "2",
                    "--doc-id",
                    "doc-stress-main",
                    "--out-json",
                    str(out_json),
                    "--out-md",
                    str(out_md),
                ]
            )

            payload = json.loads(out_json.read_text(encoding="utf-8"))
            markdown_exists = out_md.exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["summary"]["planned_parts"], 2)
        self.assertEqual(payload["job_store"]["mode"], "temporary_sqlite")
        self.assertTrue(markdown_exists)

    def test_build_report_can_execute_parts_with_bounded_parallelism(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            report = large_pdf_stress.build_report(
                config=workspace.config_path,
                generated_pdf=workspace.root / "stress-parallel.pdf",
                generate_pages=4,
                lines_per_page=3,
                target_pages_per_part=1,
                doc_id="doc-stress-parallel",
                execute_parts=True,
                max_parts=2,
                parallel_parts=2,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["parallel_parts"], 2)
        self.assertEqual(report["summary"]["executed_parts"], 2)
        self.assertEqual(len(report["errors"]), 0)

    def test_build_report_can_start_execution_from_a_later_part(self) -> None:
        with TemporaryWorkspace(PDF_CONFIG) as workspace:
            report = large_pdf_stress.build_report(
                config=workspace.config_path,
                generated_pdf=workspace.root / "stress-offset.pdf",
                generate_pages=4,
                lines_per_page=3,
                target_pages_per_part=1,
                doc_id="doc-stress-offset",
                execute_parts=True,
                part_start=3,
                max_parts=1,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["part_start"], 3)
        self.assertEqual(report["summary"]["executed_parts"], 1)
        self.assertEqual(report["part_timings"][0]["part_id"], "doc-stress-offset-part-3")


class EvaluateGateTests(unittest.TestCase):
    def _sample_report(self, *, planned_parts: int = 342, plan_elapsed_s: float = 5.0, error_count: int = 0) -> dict:
        return {
            "summary": {
                "total_pages": 17101,
                "planned_parts": planned_parts,
                "plan_elapsed_s": plan_elapsed_s,
                "error_count": error_count,
                "executed_parts": 0,
                "total_elapsed_s": 5.0,
            },
            "manifest_part_index": {"part_count": 342},
        }

    def _sample_config(self) -> dict:
        return {
            "thresholds": {
                "plan_elapsed_s_max": 10.0,
                "part_count_min": 340,
                "part_count_max": 350,
                "error_count_max": 0,
            }
        }

    def test_evaluate_gate_passes_when_all_thresholds_met(self) -> None:
        gate = large_pdf_stress.evaluate_gate(self._sample_report(), self._sample_config())
        self.assertTrue(gate["passed"])
        self.assertEqual(len(gate["checks"]), 4)
        self.assertTrue(all(c["passed"] for c in gate["checks"]))

    def test_evaluate_gate_fails_when_part_count_below_min(self) -> None:
        report = self._sample_report(planned_parts=100)
        gate = large_pdf_stress.evaluate_gate(report, self._sample_config())
        self.assertFalse(gate["passed"])
        failed = [c for c in gate["checks"] if not c["passed"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["metric"], "part_count")

    def test_evaluate_gate_fails_when_plan_elapsed_exceeds_max(self) -> None:
        report = self._sample_report(plan_elapsed_s=20.0)
        gate = large_pdf_stress.evaluate_gate(report, self._sample_config())
        self.assertFalse(gate["passed"])

    def test_evaluate_gate_fails_when_errors_exceed_max(self) -> None:
        report = self._sample_report(error_count=3)
        gate = large_pdf_stress.evaluate_gate(report, self._sample_config())
        self.assertFalse(gate["passed"])

    def test_evaluate_gate_returns_false_with_empty_thresholds(self) -> None:
        gate = large_pdf_stress.evaluate_gate(self._sample_report(), {"thresholds": {}})
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["checks"], [])

    def test_evaluate_gate_handles_missing_metrics(self) -> None:
        report = {"summary": {}, "manifest_part_index": {}}
        gate = large_pdf_stress.evaluate_gate(report, self._sample_config())
        self.assertFalse(gate["passed"])
        self.assertTrue(all(not c["passed"] for c in gate["checks"]))

    def test_evaluate_gate_skips_snapshot_threshold_for_plan_only_runs(self) -> None:
        config = {
            "thresholds": {
                "part_count_min": 1,
                "snapshot_blocks_min": 100,
            }
        }
        gate = large_pdf_stress.evaluate_gate(self._sample_report(planned_parts=1), config)

        self.assertTrue(gate["passed"])
        snapshot_check = next(check for check in gate["checks"] if check["metric"] == "snapshot_blocks")
        self.assertTrue(snapshot_check["skipped"])
        self.assertEqual(snapshot_check["reason"], "part_execution_disabled")

    def test_evaluate_gate_uses_snapshot_block_count_when_parts_execute(self) -> None:
        config = {"thresholds": {"snapshot_blocks_min": 100}}
        report = self._sample_report()
        report["execute_parts"] = True
        report["summary"]["executed_parts"] = 1
        report["summary"]["snapshot_blocks"] = 101

        gate = large_pdf_stress.evaluate_gate(report, config)

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["checks"][0]["actual"], 101)


if __name__ == "__main__":
    unittest.main()
