from __future__ import annotations

import csv
import io
import json
import unittest

from parsecore.exports import export_structured_projection


class StructuredProjectionExportTests(unittest.TestCase):
    def test_exports_quality_signals_as_jsonl(self) -> None:
        payload = {
            "doc_id": "doc-001",
            "quality_signals": [
                {
                    "code": "table_ragged_rows",
                    "severity": "warning",
                    "message": "Table rows have inconsistent column counts",
                    "page_number": 2,
                    "detail": {"widths": [2, 3]},
                },
                {
                    "code": "ocr_attempted",
                    "severity": "info",
                    "message": "OCR was attempted for this page",
                    "page_number": 3,
                },
            ],
        }

        result = export_structured_projection(payload, dataset="quality_signals", format="jsonl")

        self.assertEqual(result["content_type"], "application/x-ndjson; charset=utf-8")
        self.assertEqual(result["filename"], "doc-001-quality_signals.jsonl")
        lines = str(result["content"]).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["detail"], {"widths": [2, 3]})
        self.assertEqual(json.loads(lines[1])["code"], "ocr_attempted")

    def test_exports_tables_as_csv_with_nested_values_encoded_as_json_strings(self) -> None:
        payload = {
            "doc_id": "合同/2026",
            "tables": [
                {
                    "table_id": "doc:p1:t1",
                    "page_number": 1,
                    "rows": 2,
                    "cols": 2,
                    "cells": [{"row_index": 0, "col_index": 0, "text": "Task"}],
                    "warnings": ["table_ragged_rows"],
                    "detail": {"source": "xlsx", "range": "A1:B2"},
                }
            ],
        }

        result = export_structured_projection(payload, dataset="tables", format="csv")

        self.assertEqual(result["content_type"], "text/csv; charset=utf-8")
        self.assertEqual(result["filename"], "2026-tables.csv")
        rows = list(csv.DictReader(io.StringIO(str(result["content"]))))
        self.assertEqual(rows[0]["table_id"], "doc:p1:t1")
        self.assertEqual(rows[0]["warnings"], '["table_ragged_rows"]')
        self.assertEqual(json.loads(rows[0]["cells"]), [{"col_index": 0, "row_index": 0, "text": "Task"}])
        self.assertEqual(json.loads(rows[0]["detail"]), {"range": "A1:B2", "source": "xlsx"})

    def test_exports_parse_units_as_tsv_bytes(self) -> None:
        payload = {
            "doc_id": "doc-002",
            "parse_units": [
                {
                    "parse_unit_id": "doc-002:unit:1",
                    "source_doc_id": "doc-002",
                    "page_start": 1,
                    "page_end": 5,
                    "state": "done",
                    "detail": {"tables": 3},
                }
            ],
        }

        result = export_structured_projection(payload, dataset="parse_units", format="tsv", as_bytes=True)

        self.assertEqual(result["content_type"], "text/tab-separated-values; charset=utf-8")
        self.assertEqual(result["filename"], "doc-002-parse_units.tsv")
        self.assertIsInstance(result["content"], bytes)
        content = result["content"].decode("utf-8")  # type: ignore[union-attr]
        self.assertIn("parse_unit_id\tsource_doc_id\tpage_start", content)
        rows = list(csv.DictReader(io.StringIO(content), delimiter="\t"))
        self.assertEqual(rows[0]["detail"], '{"tables":3}')

    def test_empty_dataset_exports_csv_header_only_when_no_fields_exist(self) -> None:
        result = export_structured_projection({"doc_id": "doc-empty"}, dataset="tables", format="csv")

        self.assertEqual(result["content"], "\n")

    def test_rejects_invalid_dataset_and_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_export_dataset"):
            export_structured_projection({}, dataset="pages", format="csv")

        with self.assertRaisesRegex(ValueError, "invalid_export_format"):
            export_structured_projection({}, dataset="tables", format="xlsx")


if __name__ == "__main__":
    unittest.main()
