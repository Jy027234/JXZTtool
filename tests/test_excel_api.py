from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import unittest

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from tests.support import TemporaryWorkspace


EXCEL_API_CONFIG = """
[project]
name = "test-api-excel"
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
class ExcelApiTests(unittest.TestCase):
    def _create_excel_workbook(self, workspace: TemporaryWorkspace, name: str) -> Path:
        from openpyxl import Workbook

        assert workspace.root is not None
        target = workspace.root / name
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Plan"
        sheet.append(["Upgrade Plan", ""])
        sheet.merge_cells("A1:B1")
        sheet.append(["Task", "Owner"])
        sheet.append(["Design", "Engineering"])
        workbook.save(target)
        return target

    def test_parse_upload_excel_aliases_return_tables(self) -> None:
        with TemporaryWorkspace(EXCEL_API_CONFIG) as workspace:
            document_path = self._create_excel_workbook(workspace, "plan.xlsx")
            file_bytes = document_path.read_bytes()
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                root_response = client.post(
                    "/parse",
                    files={
                        "file": (
                            document_path.name,
                            file_bytes,
                            "application/vnd.ms-excel",
                        )
                    },
                )
                versioned_response = client.post(
                    "/v1/parse",
                    files={
                        "file": (
                            document_path.name,
                            file_bytes,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    },
                )

        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(versioned_response.status_code, 200)
        for response in (root_response, versioned_response):
            body = response.json()
            self.assertEqual(body["metadata"]["parser"], "excel-native")
            self.assertEqual(body["total_pages"], 1)
            self.assertIn("| Task | Owner |", body["pages"][0]["tables_markdown"][0])
            self.assertEqual(body["pages"][0]["tables"][0]["rows"], 2)
            self.assertEqual(body["pages"][0]["tables"][0]["cols"], 2)
            self.assertEqual(body["pages"][0]["tables"][0]["raw"][0], ["Task", "Owner"])

    def test_parse_batch_excel_returns_enterprise_tables(self) -> None:
        with TemporaryWorkspace(EXCEL_API_CONFIG) as workspace:
            document_path = self._create_excel_workbook(workspace, "plan-batch.xlsx")
            payload = {
                "file_base64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
                "file_name": document_path.name,
                "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "tenant_id": "tenant-excel-batch",
                "quota_key": "excel",
            }
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post("/parse/batch", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["parser_used"], "excel-native")
        self.assertEqual(body["total_pages"], 1)
        self.assertIn("| Task | Owner |", body["pages"][0]["tables_markdown"][0])
        self.assertEqual(body["pages"][0]["tables"][0]["raw"][1], ["Design", "Engineering"])

    @unittest.skipUnless(
        Path("D:/app/uploads/ARJ21主轮IPC.xls").exists()
        and importlib.util.find_spec("xlrd"),
        "legacy .xls sample or xlrd not available",
    )
    def test_parse_upload_accepts_real_legacy_xls_sample(self) -> None:
        sample_path = Path("D:/app/uploads/ARJ21主轮IPC.xls")
        with TemporaryWorkspace(EXCEL_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse",
                    files={
                        "file": (
                            sample_path.name,
                            sample_path.read_bytes(),
                            "application/vnd.ms-excel",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["metadata"]["parser"], "excel-native")
        self.assertEqual(body["total_pages"], 1)
        self.assertGreaterEqual(len(body["pages"][0]["tables_markdown"]), 1)


if __name__ == "__main__":
    unittest.main()
