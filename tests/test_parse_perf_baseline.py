from __future__ import annotations

import importlib.util
import unittest

from tests.support import TemporaryWorkspace
from tools.parse_perf_baseline import build_report, render_markdown


EXCEL_CONFIG = """
[project]
name = "test-parse-perf-baseline"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "excel-native"
media_types = [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.ms-excel",
]
extensions = [".xlsx", ".xlsm", ".xls"]
""".strip()


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl not installed")
class ParsePerfBaselineTests(unittest.TestCase):
    def _create_workbook(self, workspace: TemporaryWorkspace, name: str) -> None:
        from openpyxl import Workbook

        assert workspace.root is not None
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Plan"
        sheet.append(["Task", "Owner"])
        sheet.append(["Design", "Engineering"])
        workbook.save(workspace.root / name)

    def test_build_report_captures_parse_perf_metrics(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            self._create_workbook(workspace, "plan.xlsx")
            assert workspace.config_path is not None
            assert workspace.root is not None

            payload = build_report(
                config=workspace.config_path,
                sample_dir=workspace.root,
                extensions={".xlsx"},
            )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["measurement"]["track_python_memory"])
        self.assertEqual(payload["measurement"]["cache"]["mode"], "ocr_warm")
        self.assertEqual(payload["summary"]["documents"], 1)
        self.assertEqual(payload["summary"]["failed_documents"], 0)
        self.assertGreaterEqual(payload["summary"]["total_tables"], 1)
        result = payload["results"][0]
        self.assertEqual(result["file_name"], "plan.xlsx")
        self.assertGreaterEqual(result["elapsed_s"], 0.0)
        self.assertGreater(result["peak_kb"], 0.0)
        self.assertEqual(result["tables"], 1)
        self.assertEqual(result["primary_provider_id"], "excel-native")
        self.assertEqual(result["best_provider_id"], "excel-native")
        self.assertIn("parse", result["stage_timings"])
        self.assertIn("normalize_ir", result["stage_timings"])
        self.assertIn("chunk", result["stage_timings"])
        self.assertIn("persist_index", result["stage_timings"])
        self.assertIn("quality_observability", result["stage_timings"])
        self.assertIn("provider_projection", result["stage_timings"])
        self.assertIn("quality_projection", result["stage_timings"])
        self.assertEqual(result["fingerprint"]["raw_blocks"], result["blocks"])
        self.assertIn("parser_lifecycle", result)
        self.assertEqual(result["cache_state"]["requested_mode"], "ocr_warm")
        self.assertIn("process_telemetry", result)
        self.assertIn("provider_report", result)
        provider_report = result["provider_report"]
        self.assertEqual(provider_report["schema_version"], "2026-06-provider-usage")
        self.assertEqual(provider_report["comparison_report"]["schema_version"], "2026-06-provider-comparison")
        ranking = provider_report["comparison_report"]["rankings"][0]
        self.assertEqual(ranking["provider_id"], "excel-native")
        self.assertEqual(ranking["axes"]["performance"]["status"], "observed")
        self.assertEqual(ranking["axes"]["memory"]["status"], "observed")
        self.assertNotIn("elapsed_s", provider_report["comparison_report"]["summary"]["pending_axes"])
        self.assertNotIn("memory_mb", provider_report["comparison_report"]["summary"]["pending_axes"])

    def test_build_report_can_separate_latency_from_memory_instrumentation(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            self._create_workbook(workspace, "latency.xlsx")
            assert workspace.config_path is not None
            assert workspace.root is not None

            payload = build_report(
                config=workspace.config_path,
                sample_dir=workspace.root,
                extensions={".xlsx"},
                track_python_memory=False,
            )

        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["measurement"]["track_python_memory"])
        self.assertIsNone(payload["summary"]["max_peak_kb"])
        self.assertIsNone(payload["results"][0]["peak_kb"])
        comparison_summary = payload["results"][0]["provider_report"]["comparison_report"]["summary"]
        self.assertIn("memory_mb", comparison_summary["pending_axes"])

    def test_multi_run_report_keeps_warm_lifecycle_and_stability_statistics(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            self._create_workbook(workspace, "stability.xlsx")
            assert workspace.config_path is not None
            assert workspace.root is not None
            sample = workspace.root / "stability.xlsx"

            payload = build_report(
                config=workspace.config_path,
                sample_dir=workspace.root,
                samples=[sample],
                track_python_memory=False,
                runs=3,
                warmup_runs=1,
                process_telemetry=False,
                stability_policy={
                    "latency": {
                        "min_runs": 3,
                        "min_success_rate_pct": 100,
                        "require_uniform_lifecycle": True,
                    }
                },
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["warmup_results"]), 1)
        self.assertEqual(len(payload["results"]), 3)
        stability = payload["stability"]
        self.assertEqual(stability["status"], "passed")
        self.assertTrue(stability["comparison_valid"])
        self.assertEqual(stability["elapsed_s"]["count"], 3)
        self.assertIsNotNone(stability["elapsed_s"]["p95"])
        self.assertEqual(len(stability["comparison_groups"]), 1)
        self.assertEqual(stability["comparison_groups"][0]["key"], "reused_runtime:warm:cache=ocr_warm")
        self.assertTrue(all(item["parser_lifecycle"]["phase"] == "warm" for item in payload["results"]))
        self.assertTrue(all(item["cache_state"]["requested_mode"] == "ocr_warm" for item in payload["results"]))
        self.assertTrue(
            all(item["process_telemetry"]["status"] == "disabled" for item in payload["results"])
        )

    def test_render_markdown_includes_perf_columns(self) -> None:
        payload = {
            "status": "ok",
            "sample_dir": "samples",
            "measurement": {
                "elapsed_scope": "runtime.submit_end_to_end",
                "track_python_memory": False,
            },
            "summary": {
                "documents": 1,
                "total_elapsed_s": 0.2,
                "max_peak_kb": 42,
            },
            "results": [
                {
                    "file_name": "sample.xlsx",
                    "status": "done",
                    "size_bytes": 100,
                    "elapsed_s": 0.2,
                    "peak_kb": 42,
                    "mb_per_s": 0.1,
                    "primary_provider_id": "excel-native",
                    "best_provider_id": "excel-native",
                    "best_provider_score": 1.0,
                    "blocks": 2,
                    "chunks": 1,
                    "tables": 1,
                }
            ],
        }

        markdown = render_markdown(payload)

        self.assertIn("ParseCore Parse Performance Baseline", markdown)
        self.assertIn("sample.xlsx", markdown)
        self.assertIn("peak_kb", markdown)
        self.assertIn("primary_provider", markdown)
        self.assertIn("excel-native", markdown)
        self.assertIn("track_python_memory: `False`", markdown)


if __name__ == "__main__":
    unittest.main()
