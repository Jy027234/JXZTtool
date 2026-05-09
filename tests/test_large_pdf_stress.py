from __future__ import annotations

import json
import unittest

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
        self.assertTrue(markdown_exists)


if __name__ == "__main__":
    unittest.main()
