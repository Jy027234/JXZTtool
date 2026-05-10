from __future__ import annotations

from types import SimpleNamespace
import unittest

from parsecore.api_payloads import _document_projection, _document_records_projection
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
        self.assertEqual(payload["records_summary"]["total"], 2)

        records_payload = _document_records_projection(
            {
                "job": job,
                "doc_id": "doc-quality-001",
                "blocks": blocks,
                "chunks": (),
            },
            query="F",
            limit=10,
            offset=0,
        )

        self.assertEqual(records_payload["projection"], "records")
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertEqual(record["record_id"], "doc-quality-001:p1:t1:r2")
        self.assertEqual(record["fields"], {"Task": "D", "Task_2": "", "col_3": "F"})
        self.assertEqual(record["page_start"], 1)

    def test_catalog_text_blocks_become_records(self) -> None:
        job = SimpleNamespace(
            job_id="job-catalog-001",
            doc_id="doc-catalog-001",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={
                "profile": "large-pdf-catalog",
                "requested_profile": "large-pdf-catalog",
                "profile_source": "requested",
                "profile_recommended_async": True,
            },
        )
        blocks = (
            Block(
                block_id="doc-catalog-001-title",
                doc_id="doc-catalog-001",
                type=BlockType.TITLE,
                content="catalog",
                metadata={"page": 1, "semantic_role": "title"},
            ),
            Block(
                block_id="blk-text-1",
                doc_id="doc-catalog-001",
                type=BlockType.PARAGRAPH,
                content=(
                    "序号 证件编号 持证人 型别 最新批准日期\n"
                    "1 TC001A 哈尔滨飞机制造公司 Y11B 1992-12-28\n"
                    "2 PMA0013-01-XN 重庆兴山泉航空设备有限公司 航空专用净水器\n"
                    "SQ-737-1518 波音 2025-01-10\n"
                    "3 无编号公司 缺少证件编号"
                ),
                metadata={"page": 2, "semantic_role": "paragraph"},
            ),
        )

        snapshot = {
            "job": job,
            "doc_id": "doc-catalog-001",
            "blocks": blocks,
            "chunks": (),
        }
        payload = _document_projection(snapshot, projection="structured")

        self.assertEqual(payload["records_summary"]["total"], 3)
        self.assertEqual(payload["records_summary"]["text_record_count"], 3)
        self.assertEqual(payload["records_summary"]["by_source"]["text-block"], 3)
        self.assertIn("row_continuation_detected", payload["quality_summary"]["by_code"])
        self.assertIn("record_field_missing", payload["quality_summary"]["by_code"])

        records_payload = _document_records_projection(snapshot, query="波音", limit=10, offset=0)

        self.assertEqual(records_payload["projection"], "records")
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertEqual(record["source"], "text-block")
        self.assertEqual(record["row_number"], 2)
        self.assertEqual(record["page_start"], 2)
        self.assertEqual(record["page_end"], 2)
        self.assertEqual(record["fields"]["certificate_or_project_no"], "PMA0013-01-XN")
        self.assertEqual(record["fields"]["latest_date"], "2025-01-10")
        self.assertIn("row_continuation_detected", record["quality_signal_codes"])

    def test_record_quality_signals_detect_shifted_columns_and_bad_dates(self) -> None:
        job = SimpleNamespace(
            job_id="job-record-quality",
            doc_id="doc-record-quality",
            state=ParseJobState.DONE,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            options={"profile": "excel-ledger"},
        )
        blocks = (
            Block(
                block_id="blk-shifted-table",
                doc_id="doc-record-quality",
                type=BlockType.TABLE,
                content="Certificate\tLatest Date\tHolder\nPMA0013-01-XN\tACME\t2025-02-31",
                metadata={
                    "page": 4,
                    "parser": "excel-native",
                    "rows": 2,
                    "cols": 3,
                    "header_values": ["Certificate", "Latest Date", "Holder"],
                    "cells": [["Certificate", "Latest Date", "Holder"], ["PMA0013-01-XN", "ACME", "2025-02-31"]],
                },
            ),
        )
        snapshot = {"job": job, "doc_id": "doc-record-quality", "blocks": blocks, "chunks": ()}

        payload = _document_projection(snapshot, projection="structured")
        records_payload = _document_records_projection(
            snapshot,
            quality_signal="column_shift_suspected",
            limit=10,
            offset=0,
        )

        self.assertIn("column_shift_suspected", payload["quality_summary"]["by_code"])
        self.assertIn("date_parse_failed", payload["quality_summary"]["by_code"])
        self.assertEqual(records_payload["total"], 1)
        record = records_payload["items"][0]
        self.assertIn("column_shift_suspected", record["quality_signal_codes"])
        self.assertIn("date_parse_failed", record["quality_signal_codes"])

    def test_catalog_text_record_quality_detects_date_before_certificate(self) -> None:
        job = SimpleNamespace(
            job_id="job-catalog-shift",
            doc_id="doc-catalog-shift",
            state=ParseJobState.DONE,
            media_type="application/pdf",
            options={"profile": "large-pdf-catalog"},
        )
        blocks = (
            Block(
                block_id="blk-catalog-shift",
                doc_id="doc-catalog-shift",
                type=BlockType.PARAGRAPH,
                content="1 2025-01-10 PMA0013-01-XN 重庆兴山泉航空设备有限公司",
                metadata={"page": 8, "semantic_role": "paragraph"},
            ),
        )
        snapshot = {"job": job, "doc_id": "doc-catalog-shift", "blocks": blocks, "chunks": ()}

        records_payload = _document_records_projection(
            snapshot,
            quality_signal="column_shift_suspected",
            limit=10,
            offset=0,
        )

        self.assertEqual(records_payload["total"], 1)
        self.assertIn("column_shift_suspected", records_payload["items"][0]["quality_signal_codes"])


if __name__ == "__main__":
    unittest.main()
