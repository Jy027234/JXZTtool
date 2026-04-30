from __future__ import annotations

import importlib.util
import unittest

from parsecore.bootstrap import build_runtime
from parsecore.models import BlockType, ParseRequest, SemanticRole
from tests.support import TemporaryWorkspace


EXCEL_CONFIG = """
[project]
name = "test-excel"
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
]
extensions = [".xlsx", ".xlsm"]
""".strip()


EXCEL_VISIBLE_ONLY_CONFIG = EXCEL_CONFIG + """

[parsers.options]
include_hidden_sheets = false
max_rows_per_sheet = 2
max_cols_per_sheet = 2
""".rstrip()


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl not installed")
class ExcelParserTests(unittest.TestCase):
    def _create_workbook(self, workspace: TemporaryWorkspace, name: str):
        from openpyxl import Workbook

        assert workspace.root is not None
        target = workspace.root / name
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "BOM"
        sheet.append(["Part", "Qty", "Cost"])
        sheet.append(["A-100", 2, 3.5])
        sheet.append(["Total", "", "=B2*C2"])
        hidden = workbook.create_sheet("HiddenCalc")
        hidden.sheet_state = "hidden"
        hidden.append(["Secret", "Value"])
        hidden.append(["Internal", 42])
        workbook.save(target)
        return target

    def _create_multi_table_workbook(self, workspace: TemporaryWorkspace, name: str):
        from openpyxl import Workbook

        assert workspace.root is not None
        target = workspace.root / name
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Mixed"
        sheet.append(["Part", "Qty"])
        sheet.append(["A-100", 2])
        sheet.append([])
        sheet.append(["Checklist", "Status"])
        sheet.append(["Torque", "Done"])
        workbook.save(target)
        return target

    def test_excel_parser_emits_sheet_table_blocks(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            workbook_path = self._create_workbook(workspace, "bom.xlsx")
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-excel",
                    file_path=str(workbook_path),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            )

        self.assertEqual(outcome.blocks[0].type, BlockType.TITLE)
        self.assertEqual(outcome.blocks[0].metadata["parser"], "excel-native")
        table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
        self.assertEqual(len(table_blocks), 2)
        bom = table_blocks[0]
        self.assertIn("| Part | Qty | Cost |", bom.content)
        self.assertIn("| A-100 | 2 | 3.5 |", bom.content)
        self.assertEqual(bom.metadata["semantic_role"], SemanticRole.TABLE.value)
        self.assertEqual(bom.metadata["table_type"], "spreadsheet")
        self.assertEqual(bom.metadata["sheet_name"], "BOM")
        self.assertEqual(bom.metadata["cell_range"], "A1:C3")
        self.assertEqual(bom.metadata["sheet_table_index"], 1)
        self.assertEqual(bom.metadata["sheet_table_count"], 1)
        self.assertEqual(bom.metadata["row_range"], "1:3")
        self.assertEqual(bom.metadata["column_range"], "A:C")
        self.assertEqual(bom.metadata["rows"], 3)
        self.assertEqual(bom.metadata["cols"], 3)
        self.assertTrue(bom.metadata["has_formula"])
        self.assertEqual(bom.metadata["formula_count"], 1)
        self.assertFalse(bom.metadata["hidden_sheet"])
        hidden = table_blocks[1]
        self.assertEqual(hidden.metadata["sheet_name"], "HiddenCalc")
        self.assertTrue(hidden.metadata["hidden_sheet"])

    def test_excel_parser_splits_tables_separated_by_blank_rows(self) -> None:
        with TemporaryWorkspace(EXCEL_CONFIG) as workspace:
            workbook_path = self._create_multi_table_workbook(workspace, "mixed.xlsx")
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-excel-mixed",
                    file_path=str(workbook_path),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            )

        table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
        self.assertEqual(len(table_blocks), 2)
        first, second = table_blocks
        self.assertEqual(first.metadata["sheet_name"], "Mixed")
        self.assertEqual(first.metadata["sheet_table_index"], 1)
        self.assertEqual(first.metadata["sheet_table_count"], 2)
        self.assertEqual(first.metadata["cell_range"], "A1:B2")
        self.assertIn("| Part | Qty |", first.content)
        self.assertEqual(second.metadata["sheet_table_index"], 2)
        self.assertEqual(second.metadata["sheet_table_count"], 2)
        self.assertEqual(second.metadata["cell_range"], "A4:B5")
        self.assertIn("| Checklist | Status |", second.content)

    def test_excel_parser_honors_visibility_and_size_options(self) -> None:
        with TemporaryWorkspace(EXCEL_VISIBLE_ONLY_CONFIG) as workspace:
            workbook_path = self._create_workbook(workspace, "limited.xlsx")
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-excel-limited",
                    file_path=str(workbook_path),
                    media_type=None,
                )
            )

        table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
        self.assertEqual(len(table_blocks), 1)
        table = table_blocks[0]
        self.assertEqual(table.metadata["sheet_name"], "BOM")
        self.assertEqual(table.metadata["cell_range"], "A1:B2")
        self.assertEqual(table.metadata["rows"], 2)
        self.assertEqual(table.metadata["cols"], 2)
        self.assertTrue(table.metadata["truncated"])
        self.assertNotIn("HiddenCalc", {block.metadata.get("sheet_name") for block in table_blocks})


if __name__ == "__main__":
    unittest.main()
