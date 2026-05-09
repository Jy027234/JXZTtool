from __future__ import annotations

from types import SimpleNamespace
import unittest

from parsecore.api_payloads import _document_projection
from parsecore.models import Block, BlockType, ParseJobState


class DocumentProjectionQualitySignalTests(unittest.TestCase):
    def test_table_metadata_becomes_quality_signals(self) -> None:
        job = SimpleNamespace(
            job_id="job-quality-001",
            doc_id="doc-quality-001",
            state=ParseJobState.DONE,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            options={"profile": "excel-ledger", "file_name": "ledger.xlsx"},
        )
        blocks = (
            Block(
                block_id="blk-table-1",
                doc_id="doc-quality-001",
                type=BlockType.TABLE,
                content="Task | Task |",
                metadata={
                    "page": 1,
                    "parser": "excel-native",
                    "table_type": "spreadsheet",
                    "sheet_name": "HiddenLedger",
                    "rows": 3,
                    "cols": 3,
                    "cells": [["Task", "Task", ""], ["A", "B", "C"], ["D", "", "F"]],
                    "header_values": ["Task", "Task", ""],
                    "hidden_sheet": True,
                    "merged_cells": ["A1:B1"],
                    "has_formula": True,
                    "formula_count": 2,
                    "truncated": True,
                    "cells_truncated": True,
                    "cells_total": 100,
                    "cells_preview_rows": 2,
                },
            ),
        )

        payload = _document_projection(
            {
                "job": job,
                "doc_id": "doc-quality-001",
                "blocks": blocks,
                "chunks": (),
            },
            projection="structured",
        )

        table = payload["tables"][0]
        self.assertEqual(table["header_values"], ["Task", "Task", ""])
        self.assertEqual(table["merged_cells"], ["A1:B1"])
        self.assertTrue(table["hidden_sheet"])
        self.assertTrue(table["has_formula"])
        self.assertTrue(table["cells_truncated"])
        self.assertEqual(payload["profile_resolution"]["resolved_profile"], "excel-ledger")
        self.assertEqual(payload["profile_resolution"]["source"], "requested")

        signals = {signal["code"]: signal for signal in payload["quality_signals"]}
        self.assertIn("table_cells_truncated", signals)
        self.assertIn("table_source_truncated", signals)
        self.assertIn("table_hidden_sheet", signals)
        self.assertIn("table_merged_cells", signals)
        self.assertIn("table_formula_cells", signals)
        self.assertIn("table_header_blank_cells", signals)
        self.assertIn("table_header_duplicate_values", signals)
        self.assertEqual(signals["table_header_blank_cells"]["detail"]["col_indexes"], [2])
        self.assertEqual(signals["table_header_duplicate_values"]["detail"]["values"], ["Task"])
        self.assertEqual(payload["quality_summary"]["by_code"]["table_cells_truncated"], 1)


if __name__ == "__main__":
    unittest.main()
