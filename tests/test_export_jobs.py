from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from parsecore.export_jobs import create_export_package, export_file_path, load_export_manifest


class ExportPackageTests(unittest.TestCase):
    def test_creates_default_export_package(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-001",
            "tenant_id": "tenant-a",
            "parse_run_id": "job-001",
            "profile": "table-heavy",
            "tables": [{"table_id": "t1", "page_number": 1, "rows": 2}],
            "quality_signals": [{"code": "warn", "severity": "warning", "page_number": 1}],
            "coverage": [{"page_number": 1, "indexable_unit_count": 1, "chunk_ids": ["chunk-1"]}],
            "parse_units": [{"parse_unit_id": "u1", "page_start": 1, "page_end": 2}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(payload, tmp)

            self.assertEqual(manifest["doc_id"], "doc-001")
            self.assertEqual(manifest["manifest_schema_version"], "2026-05")
            self.assertEqual(manifest["tenant_id"], "tenant-a")
            self.assertEqual(manifest["schema_version"], "2026-06")
            self.assertEqual(manifest["parse_run_id"], "job-001")
            self.assertEqual(manifest["profile"], "table-heavy")
            self.assertEqual(manifest["state"], "done")
            self.assertEqual(
                manifest["request"],
                {
                    "include": ["tables", "quality_signals", "coverage", "parse_units"],
                    "formats": {
                        "tables": "csv",
                        "quality_signals": "jsonl",
                        "coverage": "jsonl",
                        "parse_units": "tsv",
                    },
                    "filters": {},
                },
            )
            self.assertTrue(manifest["export_id"].startswith("exp_"))
            self.assertEqual(
                [(entry["dataset"], entry["format"], entry["path"]) for entry in manifest["files"]],
                [
                    ("tables", "csv", "tables.csv"),
                    ("quality_signals", "jsonl", "quality_signals.jsonl"),
                    ("coverage", "jsonl", "coverage.jsonl"),
                    ("parse_units", "tsv", "parse_units.tsv"),
                ],
            )
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "tables.csv").exists())
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "quality_signals.jsonl").exists())
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "coverage.jsonl").exists())
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "parse_units.tsv").exists())
            self.assertEqual(load_export_manifest(tmp, manifest["export_id"]), manifest)
            for entry in manifest["files"]:
                self.assertGreater(entry["bytes"], 0)
                self.assertIn("content_type", entry)
                self.assertEqual(entry["records"], 1)

    def test_filters_severity_and_page_range(self) -> None:
        payload = {
            "doc_id": "doc-filtered",
            "tables": [
                {"table_id": "t1", "page_number": 1},
                {"table_id": "t2", "page_number": 3},
            ],
            "quality_signals": [
                {"code": "info-1", "severity": "info", "page_number": 2},
                {"code": "warning-1", "severity": "warning", "page_number": 2},
                {"code": "warning-2", "severity": "warning", "page_number": 5},
            ],
            "parse_units": [
                {"parse_unit_id": "u1", "page_start": 1, "page_end": 1},
                {"parse_unit_id": "u2", "page_start": 2, "page_end": 4},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["tables", "quality_signals", "parse_units"],
                formats={"tables": "csv", "quality_signals": "jsonl", "parse_units": "tsv"},
                filters={"severity": ["warning"], "page_range": {"start": 2, "end": 3}},
            )
            self.assertEqual(manifest["request"]["include"], ["tables", "quality_signals", "parse_units"])
            self.assertEqual(manifest["request"]["filters"]["severity"], ["warning"])

            tables_content = export_file_path(tmp, manifest["export_id"], "tables.csv").read_text(encoding="utf-8")
            tables = list(csv.DictReader(io.StringIO(tables_content)))
            self.assertEqual([row["table_id"] for row in tables], ["t2"])

            signal_lines = export_file_path(tmp, manifest["export_id"], "quality_signals.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["code"] for line in signal_lines], ["warning-1"])

            units_content = export_file_path(tmp, manifest["export_id"], "parse_units.tsv").read_text(encoding="utf-8")
            units = list(csv.DictReader(io.StringIO(units_content), delimiter="\t"))
            self.assertEqual([row["parse_unit_id"] for row in units], ["u2"])

    def test_custom_export_package_can_include_records(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-records",
            "records": [
                {"record_id": "rec-1", "page_start": 1, "fields": {"name": "alpha"}},
                {"record_id": "rec-2", "page_start": 3, "fields": {"name": "beta"}},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["records"],
                formats={"records": "jsonl"},
                filters={"page_range": {"start": 2, "end": 4}},
            )

            self.assertEqual(manifest["request"]["include"], ["records"])
            self.assertEqual(manifest["files"][0]["dataset"], "records")
            self.assertEqual(manifest["files"][0]["records"], 1)
            record_lines = export_file_path(tmp, manifest["export_id"], "records.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["record_id"] for line in record_lines], ["rec-2"])

    def test_custom_export_package_can_include_pages_and_lines(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-views",
            "pages": [
                {"page_number": 1, "text": "Alpha"},
                {"page_number": 2, "text": "Beta"},
            ],
            "lines": [
                {"line_id": "l1", "page_number": 1, "text": "Alpha"},
                {"line_id": "l2", "page_number": 2, "text": "Beta"},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["pages", "lines"],
                formats={"pages": "jsonl", "lines": "csv"},
                filters={"page_range": {"start": 2, "end": 2}},
            )

            self.assertEqual(manifest["request"]["include"], ["pages", "lines"])
            self.assertEqual(
                [(entry["dataset"], entry["records"]) for entry in manifest["files"]],
                [("pages", 1), ("lines", 1)],
            )
            page_lines = export_file_path(tmp, manifest["export_id"], "pages.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["page_number"] for line in page_lines], [2])
            lines_content = export_file_path(tmp, manifest["export_id"], "lines.csv").read_text(encoding="utf-8")
            line_rows = list(csv.DictReader(io.StringIO(lines_content)))
            self.assertEqual([row["line_id"] for row in line_rows], ["l2"])

    def test_custom_export_package_can_filter_coverage(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-coverage",
            "coverage": [
                {
                    "page_number": 1,
                    "missing_reason": None,
                    "quality_signal_codes": [],
                },
                {
                    "page_number": 2,
                    "missing_reason": "no_chunks_for_indexable_units",
                    "quality_signal_codes": ["rag_units_without_chunks"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["coverage"],
                formats={"coverage": "jsonl"},
                filters={
                    "page_range": {"start": 2, "end": 2},
                    "quality_signal": "rag_units_without_chunks",
                },
            )

            self.assertEqual(manifest["files"][0]["dataset"], "coverage")
            self.assertEqual(manifest["files"][0]["records"], 1)
            coverage_lines = export_file_path(tmp, manifest["export_id"], "coverage.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["page_number"] for line in coverage_lines], [2])

    def test_custom_export_package_can_filter_reader_blocks(self) -> None:
        payload = {
            "schema_version": "2026-06-reader",
            "doc_id": "doc-reader",
            "reader": [
                {
                    "reader_block_id": "reader:000001",
                    "type": "text",
                    "page_number": 1,
                    "text": "Overview",
                    "quality_signal_codes": [],
                },
                {
                    "reader_block_id": "reader:000002",
                    "type": "figure",
                    "page_number": 3,
                    "text": "",
                    "quality_signal_codes": ["rag_figure_caption_missing"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["reader"],
                formats={"reader": "jsonl"},
                filters={
                    "page_range": {"start": 2, "end": 4},
                    "quality_signal": "rag_figure_caption_missing",
                },
            )

            self.assertEqual(manifest["files"][0]["dataset"], "reader")
            self.assertEqual(manifest["files"][0]["records"], 1)
            reader_lines = export_file_path(tmp, manifest["export_id"], "reader.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["reader_block_id"] for line in reader_lines], ["reader:000002"])

    def test_export_filters_records_by_quality_signal_and_fields(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-record-filters",
            "quality_signals": [
                {"code": "column_shift_suspected", "severity": "warning", "record_id": "rec-2"},
                {"code": "record_field_missing", "severity": "warning", "record_id": "rec-1"},
            ],
            "records": [
                {
                    "record_id": "rec-1",
                    "page_start": 1,
                    "fields": {"certificate_or_project_no": "TC001A", "holder": "alpha"},
                    "quality_signal_codes": ["record_field_missing"],
                },
                {
                    "record_id": "rec-2",
                    "page_start": 2,
                    "fields": {"certificate_or_project_no": "PMA0013-01-XN", "holder": "beta"},
                    "quality_signal_codes": ["column_shift_suspected"],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["quality_signals", "records"],
                formats={"quality_signals": "jsonl", "records": "jsonl"},
                filters={
                    "quality_signal": "column_shift_suspected",
                    "fields": {"certificate_or_project_no": "PMA0013"},
                },
            )

            signal_lines = export_file_path(tmp, manifest["export_id"], "quality_signals.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            record_lines = export_file_path(tmp, manifest["export_id"], "records.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([json.loads(line)["code"] for line in signal_lines], ["column_shift_suspected"])
            self.assertEqual([json.loads(line)["record_id"] for line in record_lines], ["rec-2"])
            self.assertEqual(manifest["files"][1]["records"], 1)

    def test_custom_export_package_can_write_records_sqlite(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-records",
            "records": [
                {"record_id": "rec-1", "page_start": 1, "fields": {"name": "alpha"}},
                {"record_id": "rec-2", "page_start": 2, "fields": {"name": "beta"}},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(
                payload,
                tmp,
                includes=["records"],
                formats={"records": "sqlite"},
            )

            self.assertEqual(manifest["files"][0]["path"], "records.sqlite")
            self.assertEqual(manifest["files"][0]["content_type"], "application/vnd.sqlite3")
            sqlite_path = export_file_path(tmp, manifest["export_id"], "records.sqlite")
            conn = sqlite3.connect(sqlite_path)
            try:
                rows = conn.execute("SELECT record_id, fields FROM records ORDER BY record_id").fetchall()
            finally:
                conn.close()
            self.assertEqual([row[0] for row in rows], ["rec-1", "rec-2"])
            self.assertEqual(json.loads(rows[1][1]), {"name": "beta"})

    def test_custom_invalid_dataset_and_format_raise_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invalid_export_dataset"):
                create_export_package({}, tmp, includes=["images"])

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invalid_export_format"):
                create_export_package({}, tmp, includes=["tables"], formats={"tables": "parquet"})

    def test_export_paths_reject_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            safe = export_file_path(tmp, "exp_safe", "manifest.json")
            self.assertEqual(safe, Path(tmp).resolve() / "exp_safe" / "manifest.json")

            with self.assertRaisesRegex(ValueError, "invalid_export_id"):
                export_file_path(tmp, "../outside", "manifest.json")
            with self.assertRaisesRegex(ValueError, "invalid_export_filename"):
                export_file_path(tmp, "exp_safe", "../manifest.json")
            with self.assertRaisesRegex(ValueError, "invalid_export_filename"):
                export_file_path(tmp, "exp_safe", str(Path(tmp).resolve() / "manifest.json"))
            with self.assertRaisesRegex(ValueError, "invalid_export_id"):
                load_export_manifest(tmp, "../outside")


if __name__ == "__main__":
    unittest.main()
