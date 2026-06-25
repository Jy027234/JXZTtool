from __future__ import annotations

import csv
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from parsecore.exports import export_structured_projection, write_structured_projection


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

    def test_exports_records_as_jsonl(self) -> None:
        payload = {
            "doc_id": "doc-records",
            "records": [
                {
                    "record_id": "rec-1",
                    "page_start": 1,
                    "fields": {"certificate": "TC001A", "holder": "ACME"},
                }
            ],
        }

        result = export_structured_projection(payload, dataset="records", format="jsonl")

        self.assertEqual(result["filename"], "doc-records-records.jsonl")
        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        self.assertEqual(rows[0]["record_id"], "rec-1")
        self.assertEqual(rows[0]["fields"]["certificate"], "TC001A")

    def test_exports_coverage_pages_as_jsonl(self) -> None:
        payload = {
            "doc_id": "doc-coverage",
            "coverage": {
                "summary": {"total_pages": 1},
                "pages": [
                    {
                        "page_number": 3,
                        "parsed_text_chars": 42,
                        "indexable_unit_count": 1,
                        "chunked_unit_count": 0,
                        "missing_reason": "no_chunks_for_indexable_units",
                        "quality_signal_codes": ["rag_units_without_chunks"],
                    }
                ],
            },
        }

        result = export_structured_projection(payload, dataset="coverage", format="jsonl")

        self.assertEqual(result["filename"], "doc-coverage-coverage.jsonl")
        lines = str(result["content"]).splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["page_number"], 3)
        self.assertEqual(row["missing_reason"], "no_chunks_for_indexable_units")

    def test_exports_reader_blocks_from_reader_projection_as_jsonl(self) -> None:
        payload = {
            "doc_id": "doc-reader",
            "projection": "reader",
            "blocks": [
                {
                    "reader_block_id": "reader:000001",
                    "type": "table",
                    "page_number": 2,
                    "text": "Installed parts",
                    "rag_text": "| Part | Qty |",
                    "quality_signal_codes": ["rag_table_without_unit"],
                    "table": {"table_id": "doc-reader:p2:t1"},
                }
            ],
        }

        result = export_structured_projection(payload, dataset="reader", format="jsonl")

        self.assertEqual(result["filename"], "doc-reader-reader.jsonl")
        lines = str(result["content"]).splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["reader_block_id"], "reader:000001")
        self.assertEqual(row["type"], "table")
        self.assertEqual(row["table"]["table_id"], "doc-reader:p2:t1")

    def test_exports_pages_and_lines_as_first_class_datasets(self) -> None:
        payload = {
            "doc_id": "doc-view",
            "pages": [
                {"page_number": 1, "page_type": "body", "text": "Alpha"},
                {"page_number": 2, "page_type": "body", "text": "Beta"},
            ],
            "lines": [
                {"line_id": "l1", "page_number": 1, "line_index": 1, "text": "Alpha"},
                {"line_id": "l2", "page_number": 2, "line_index": 1, "text": "Beta"},
            ],
        }

        pages = export_structured_projection(payload, dataset="pages", format="jsonl")
        lines = export_structured_projection(payload, dataset="lines", format="csv")

        self.assertEqual(pages["filename"], "doc-view-pages.jsonl")
        self.assertEqual([json.loads(line)["page_number"] for line in str(pages["content"]).splitlines()], [1, 2])
        self.assertEqual(lines["filename"], "doc-view-lines.csv")
        rows = list(csv.DictReader(io.StringIO(str(lines["content"]))))
        self.assertEqual([row["line_id"] for row in rows], ["l1", "l2"])

    def test_writes_projection_directly_to_disk(self) -> None:
        payload = {
            "doc_id": "doc-stream",
            "records": [
                {"record_id": "rec-1", "page_start": 1, "fields": {"name": "alpha"}},
                {"record_id": "rec-2", "page_start": 2, "fields": {"name": "beta"}},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "records.jsonl"
            csv_path = Path(tmp) / "records.csv"

            jsonl = write_structured_projection(payload, dataset="records", format="jsonl", path=jsonl_path)
            csv_result = write_structured_projection(payload, dataset="records", format="csv", path=csv_path)

            self.assertEqual(jsonl["content_type"], "application/x-ndjson; charset=utf-8")
            self.assertEqual(jsonl["bytes"], jsonl_path.stat().st_size)
            self.assertEqual([json.loads(line)["record_id"] for line in jsonl_path.read_text(encoding="utf-8").splitlines()], ["rec-1", "rec-2"])
            self.assertEqual(csv_result["content_type"], "text/csv; charset=utf-8")
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
            self.assertEqual([row["record_id"] for row in rows], ["rec-1", "rec-2"])

    @unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl not installed")
    def test_exports_records_as_xlsx(self) -> None:
        from openpyxl import load_workbook

        payload = {
            "doc_id": "doc-records",
            "records": [
                {
                    "record_id": "rec-1",
                    "page_start": 1,
                    "fields": {"certificate": "TC001A", "holder": "ACME"},
                }
            ],
        }

        result = export_structured_projection(payload, dataset="records", format="xlsx", as_bytes=True)

        self.assertEqual(
            result["content_type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(result["filename"], "doc-records-records.xlsx")
        self.assertIsInstance(result["content"], bytes)
        workbook = load_workbook(io.BytesIO(result["content"]))  # type: ignore[arg-type]
        worksheet = workbook.active
        headers = [cell.value for cell in worksheet[1]]
        values = [cell.value for cell in worksheet[2]]
        self.assertIn("record_id", headers)
        self.assertEqual(values[headers.index("record_id")], "rec-1")
        self.assertEqual(json.loads(values[headers.index("fields")])["certificate"], "TC001A")

    def test_exports_records_as_sqlite(self) -> None:
        payload = {
            "doc_id": "doc-records",
            "records": [
                {"record_id": "rec-1", "page_start": 1, "fields": {"name": "alpha"}},
                {"record_id": "rec-2", "page_start": 2, "fields": {"name": "beta"}},
            ],
        }

        result = export_structured_projection(payload, dataset="records", format="sqlite", as_bytes=True)

        self.assertEqual(result["content_type"], "application/vnd.sqlite3")
        self.assertEqual(result["filename"], "doc-records-records.sqlite")
        self.assertIsInstance(result["content"], bytes)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.sqlite"
            path.write_bytes(result["content"])  # type: ignore[arg-type]
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute("SELECT record_id, fields FROM records ORDER BY record_id").fetchall()
            finally:
                conn.close()
        self.assertEqual([row[0] for row in rows], ["rec-1", "rec-2"])
        self.assertEqual(json.loads(rows[0][1]), {"name": "alpha"})

    def test_empty_dataset_exports_csv_header_only_when_no_fields_exist(self) -> None:
        result = export_structured_projection({"doc_id": "doc-empty"}, dataset="tables", format="csv")

        self.assertEqual(result["content"], "\n")

    def test_rejects_invalid_dataset_and_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_export_dataset"):
            export_structured_projection({}, dataset="images", format="csv")

        with self.assertRaisesRegex(ValueError, "invalid_export_format"):
            export_structured_projection({}, dataset="tables", format="parquet")


class DatasetLinkageValidationTests(unittest.TestCase):
    """P2-T10: validate cross-dataset consistency when exporting multiple projections."""

    @staticmethod
    def _build_linked_payload() -> dict[str, Any]:
        """Build a payload that has consistent IDs across coverage, reader, parse_units."""
        return {
            "doc_id": "doc-linkage-001",
            "projection": "reader",
            "coverage": {
                "summary": {"total_pages": 1},
                "pages": [
                    {
                        "page_number": 1,
                        "parsed_text_chars": 120,
                        "table_count": 1,
                        "figure_count": 1,
                        "block_count": 3,
                        "unit_ids": ["doc-linkage-001:ku:000001", "doc-linkage-001:ku:000002"],
                        "indexable_unit_ids": ["doc-linkage-001:ku:000001", "doc-linkage-001:ku:000002"],
                        "indexable_unit_count": 2,
                        "chunked_unit_count": 2,
                        "unchunked_unit_ids": [],
                        "chunk_ids": ["chk-1", "chk-2"],
                        "embedded": True,
                        "missing_reason": None,
                        "provider_ids": ["pdf-text"],
                        "reading_order_confidence": 0.72,
                        "quality_signal_codes": ["reading_order_low_confidence"],
                    }
                ],
                "units": [
                    {
                        "unit_id": "doc-linkage-001:ku:000001",
                        "page_number": 1,
                        "source_block_ids": ["blk-body"],
                        "chunk_ids": ["chk-1"],
                        "embedded": True,
                        "coverage_state": "covered",
                    },
                    {
                        "unit_id": "doc-linkage-001:ku:000002",
                        "page_number": 1,
                        "source_block_ids": ["blk-table"],
                        "chunk_ids": ["chk-2"],
                        "embedded": True,
                        "coverage_state": "covered",
                    },
                ],
            },
            "blocks": [
                {
                    "reader_block_id": "reader:000001",
                    "type": "text",
                    "page_number": 1,
                    "text": "Main body content",
                    "source_block_ids": ["blk-body"],
                    "quality_signal_codes": ["reading_order_low_confidence"],
                },
                {
                    "reader_block_id": "reader:000002",
                    "type": "table",
                    "page_number": 1,
                    "text": "Parts table",
                    "source_block_ids": ["blk-table"],
                    "quality_signal_codes": [],
                    "table": {"table_id": "doc-linkage-001:p1:t1"},
                },
            ],
            "pages": [
                {
                    "page_number": 1,
                    "page_id": "p0001",
                    "page_type": "body",
                    "block_ids": ["blk-body", "blk-table"],
                    "reading_order_confidence": 0.72,
                    "quality_flags": ["reading_order_low_confidence"],
                }
            ],
            "parse_units": [
                {
                    "unit_id": "doc-linkage-001:ku:000001",
                    "page_number": 1,
                    "source_block_ids": ["blk-body"],
                    "chunk_ids": ["chk-1"],
                    "coverage_state": "covered",
                },
                {
                    "unit_id": "doc-linkage-001:ku:000002",
                    "page_number": 1,
                    "source_block_ids": ["blk-table"],
                    "chunk_ids": ["chk-2"],
                    "coverage_state": "covered",
                },
            ],
            "quality_signals": [
                {
                    "code": "reading_order_low_confidence",
                    "page_number": 1,
                    "severity": "warning",
                    "message": "Reading order confidence below threshold",
                }
            ],
        }

    def test_coverage_unit_ids_appear_in_parse_units(self) -> None:
        """Coverage page unit_ids must match parse_units export rows."""
        payload = self._build_linked_payload()
        coverage_result = export_structured_projection(payload, dataset="coverage", format="jsonl")
        units_result = export_structured_projection(payload, dataset="parse_units", format="jsonl")

        coverage_rows = [json.loads(line) for line in str(coverage_result["content"]).splitlines() if line.strip()]
        unit_rows = [json.loads(line) for line in str(units_result["content"]).splitlines() if line.strip()]

        coverage_unit_ids = set()
        for row in coverage_rows:
            coverage_unit_ids.update(row.get("unit_ids", []))
        parse_unit_ids = {row["unit_id"] for row in unit_rows}

        self.assertEqual(coverage_unit_ids, parse_unit_ids)

    def test_reader_quality_signals_appear_in_quality_signals_dataset(self) -> None:
        """Reader block quality_signal_codes must be a subset of quality_signals export codes."""
        payload = self._build_linked_payload()
        reader_result = export_structured_projection(payload, dataset="reader", format="jsonl")
        signals_result = export_structured_projection(payload, dataset="quality_signals", format="jsonl")

        reader_rows = [json.loads(line) for line in str(reader_result["content"]).splitlines() if line.strip()]
        signal_rows = [json.loads(line) for line in str(signals_result["content"]).splitlines() if line.strip()]

        reader_codes: set[str] = set()
        for row in reader_rows:
            reader_codes.update(row.get("quality_signal_codes", []))
        signal_codes = {row["code"] for row in signal_rows}

        self.assertTrue(reader_codes.issubset(signal_codes))

    def test_page_numbers_consistent_across_datasets(self) -> None:
        """Page numbers must be consistent across coverage, reader, and parse_units."""
        payload = self._build_linked_payload()
        for dataset in ("coverage", "reader", "parse_units"):
            result = export_structured_projection(payload, dataset=dataset, format="jsonl")
            rows = [json.loads(line) for line in str(result["content"]).splitlines() if line.strip()]
            page_numbers = {row["page_number"] for row in rows}
            self.assertEqual(page_numbers, {1}, f"Dataset {dataset} has inconsistent page numbers")

    def test_linkage_full_export_package(self) -> None:
        """Export all linked datasets and verify cross-references hold."""
        payload = self._build_linked_payload()
        datasets = ("coverage", "reader", "parse_units", "quality_signals")
        exported: dict[str, list[dict[str, Any]]] = {}
        for ds in datasets:
            result = export_structured_projection(payload, dataset=ds, format="jsonl")
            exported[ds] = [json.loads(line) for line in str(result["content"]).splitlines() if line.strip()]

        # Coverage unit_ids ↔ parse_units unit_id
        cov_unit_ids = set()
        for row in exported["coverage"]:
            cov_unit_ids.update(row.get("unit_ids", []))
        pu_unit_ids = {row["unit_id"] for row in exported["parse_units"]}
        self.assertEqual(cov_unit_ids, pu_unit_ids)

        # Reader quality_signal_codes ⊆ quality_signals codes
        reader_codes: set[str] = set()
        for row in exported["reader"]:
            reader_codes.update(row.get("quality_signal_codes", []))
        qs_codes = {row["code"] for row in exported["quality_signals"]}
        self.assertTrue(reader_codes.issubset(qs_codes))


class ExportFilteringTests(unittest.TestCase):
    """P5-T11: 大文件导出筛选 — page_range 和 quality_signal。"""

    def test_export_records_filtered_by_page_range(self) -> None:
        """P5-T11: records 导出只包含指定页范围的行。"""
        payload = {
            "doc_id": "doc-filter",
            "records": [
                {"record_id": "r1", "page_start": 1, "page_end": 1, "fields": {"name": "alpha"}},
                {"record_id": "r2", "page_start": 2, "page_end": 2, "fields": {"name": "beta"}},
                {"record_id": "r3", "page_start": 3, "page_end": 3, "fields": {"name": "gamma"}},
                {"record_id": "r4", "page_start": 5, "page_end": 5, "fields": {"name": "delta"}},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="records",
            format="jsonl",
            page_start=1,
            page_end=2,
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["record_id"] for r in rows}, {"r1", "r2"})

    def test_export_parse_units_filtered_by_page_range(self) -> None:
        """P5-T11: parse_units 导出只包含指定页范围的行。"""
        payload = {
            "doc_id": "doc-filter",
            "parse_units": [
                {"unit_id": "u1", "page_start": 1, "page_end": 5},
                {"unit_id": "u2", "page_start": 6, "page_end": 10},
                {"unit_id": "u3", "page_start": 11, "page_end": 15},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="parse_units",
            format="jsonl",
            page_start=8,
            page_end=12,
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        # u2: 6-10 overlaps with 8-12, u3: 11-15 overlaps with 8-12
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["unit_id"] for r in rows}, {"u2", "u3"})

    def test_export_records_filtered_by_quality_signal(self) -> None:
        """P5-T11: records 导出只包含指定 quality_signal 的行。"""
        payload = {
            "doc_id": "doc-filter",
            "records": [
                {"record_id": "r1", "page_start": 1, "quality_signal_codes": ["rag_empty_text_page"]},
                {"record_id": "r2", "page_start": 2, "quality_signal_codes": ["rag_units_without_chunks"]},
                {"record_id": "r3", "page_start": 3, "quality_signal_codes": ["rag_empty_text_page", "rag_units_without_chunks"]},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="records",
            format="jsonl",
            quality_signal="rag_empty_text_page",
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["record_id"] for r in rows}, {"r1", "r3"})

    def test_export_quality_signals_filtered_by_code(self) -> None:
        """P5-T11: quality_signals 数据集支持按 code 筛选。"""
        payload = {
            "doc_id": "doc-filter",
            "quality_signals": [
                {"code": "rag_empty_text_page", "severity": "warning", "page_number": 1},
                {"code": "rag_units_without_chunks", "severity": "warning", "page_number": 2},
                {"code": "rag_empty_text_page", "severity": "warning", "page_number": 3},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="quality_signals",
            format="jsonl",
            quality_signal="rag_empty_text_page",
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["page_number"] for r in rows], [1, 3])

    def test_export_reader_filtered_by_quality_signal(self) -> None:
        """P5-T11: reader 导出支持按 quality_signal_codes 筛选。"""
        payload = {
            "doc_id": "doc-filter",
            "reader": [
                {"reader_block_id": "rb1", "page_number": 1, "quality_signal_codes": ["rag_table_without_unit"]},
                {"reader_block_id": "rb2", "page_number": 2, "quality_signal_codes": ["rag_units_without_chunks"]},
                {"reader_block_id": "rb3", "page_number": 3, "quality_signal_codes": []},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="reader",
            format="jsonl",
            quality_signal="rag_table_without_unit",
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reader_block_id"], "rb1")

    def test_export_invalid_page_range_raises_error(self) -> None:
        """P5-T11: page_start > page_end 时抛出 ValueError。"""
        payload = {
            "doc_id": "doc-filter",
            "records": [{"record_id": "r1", "page_start": 1}],
        }

        with self.assertRaisesRegex(ValueError, "invalid_page_range"):
            export_structured_projection(
                payload,
                dataset="records",
                format="jsonl",
                page_start=5,
                page_end=1,
            )

    def test_export_without_filter_returns_all_rows(self) -> None:
        """P5-T11: 不传筛选参数时返回全部行（向后兼容）。"""
        payload = {
            "doc_id": "doc-filter",
            "records": [
                {"record_id": "r1", "page_start": 1},
                {"record_id": "r2", "page_start": 2},
                {"record_id": "r3", "page_start": 5},
            ],
        }

        # 无筛选
        result_no_filter = export_structured_projection(payload, dataset="records", format="jsonl")
        # 全部 None
        result_all_none = export_structured_projection(
            payload, dataset="records", format="jsonl",
            page_start=None, page_end=None, quality_signal=None,
        )

        rows_no = [json.loads(line) for line in str(result_no_filter["content"]).splitlines()]
        rows_none = [json.loads(line) for line in str(result_all_none["content"]).splitlines()]
        self.assertEqual(len(rows_no), 3)
        self.assertEqual(rows_no, rows_none)

    def test_export_pages_with_no_page_field_preserved(self) -> None:
        """P5-T11: 无页码字段的行不受 page_range 筛选影响。"""
        payload = {
            "doc_id": "doc-filter",
            "quality_signals": [
                {"code": "rag_empty_text_page", "severity": "info"},  # 无 page_number
                {"code": "rag_units_without_chunks", "severity": "warning", "page_number": 2},
            ],
        }

        result = export_structured_projection(
            payload,
            dataset="quality_signals",
            format="jsonl",
            page_start=1,
            page_end=1,
        )

        rows = [json.loads(line) for line in str(result["content"]).splitlines()]
        # 无 page_number 的行保留, page_number=2 的行被过滤
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "rag_empty_text_page")


if __name__ == "__main__":
    unittest.main()
