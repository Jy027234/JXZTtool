from __future__ import annotations

import importlib.util
import unittest

from tests.support import TemporaryWorkspace
from tools.excel_sample_quality import build_report, render_markdown


EXCEL_CONFIG = """
[project]
name = "test-excel-sample-quality"
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
class ExcelSampleQualityTests(unittest.TestCase):
    def _create_titled_workbook(self, workspace: TemporaryWorkspace, name: str) -> None:
        from openpyxl import Workbook

        assert workspace.root is not None
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Plan"
        sheet.append(["Upgrade Plan", ""])
        sheet.merge_cells("A1:B1")
        sheet.append(["Task", "Owner"])
        sheet.append(["Design", "Engineering"])
        workbook.save(workspace.root / name)

    def test_build_report_summarizes_spreadsheet_samples(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            self._create_titled_workbook(workspace, "plan.xlsx")
            assert workspace.config_path is not None
            assert workspace.root is not None

            payload = build_report(config=workspace.config_path, sample_dir=workspace.root)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["summary"]["documents"], 1)
        self.assertEqual(payload["summary"]["total_tables"], 1)
        self.assertEqual(payload["summary"]["titled_tables"], 1)
        self.assertEqual(payload["summary"]["merged_cell_tables"], 1)
        self.assertEqual(payload["summary"]["documents_with_issues"], 0)
        result = payload["results"][0]
        self.assertEqual(result["file_name"], "plan.xlsx")
        self.assertEqual(result["tables"], 1)
        self.assertEqual(result["table_titles"], ["Upgrade Plan"])

    def test_render_markdown_includes_key_quality_columns(self) -> None:
        payload = {
            "status": "ok",
            "sample_dir": "samples",
            "summary": {
                "documents": 1,
                "total_tables": 2,
                "documents_with_issues": 1,
            },
            "results": [
                {
                    "file_name": "sample.xlsx",
                    "status": "done",
                    "tables": 2,
                    "titled_tables": 1,
                    "merged_cell_tables": 1,
                    "empty_tables": 0,
                    "truncated_tables": 1,
                    "issues": ["sheet_truncated"],
                    "elapsed_s": 0.25,
                }
            ],
        }

        markdown = render_markdown(payload)

        self.assertIn("ParseCore Spreadsheet Sample Quality", markdown)
        self.assertIn("sample.xlsx", markdown)
        self.assertIn("sheet_truncated", markdown)


if __name__ == "__main__":
    unittest.main()
