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
        self.assertEqual(payload["summary"]["documents"], 1)
        self.assertEqual(payload["summary"]["failed_documents"], 0)
        self.assertGreaterEqual(payload["summary"]["total_tables"], 1)
        result = payload["results"][0]
        self.assertEqual(result["file_name"], "plan.xlsx")
        self.assertGreaterEqual(result["elapsed_s"], 0.0)
        self.assertGreater(result["peak_kb"], 0.0)
        self.assertEqual(result["tables"], 1)

    def test_render_markdown_includes_perf_columns(self) -> None:
        payload = {
            "status": "ok",
            "sample_dir": "samples",
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


if __name__ == "__main__":
    unittest.main()
