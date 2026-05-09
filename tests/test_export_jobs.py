from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from parsecore.export_jobs import create_export_package, export_file_path, load_export_manifest


class ExportPackageTests(unittest.TestCase):
    def test_creates_default_export_package(self) -> None:
        payload = {
            "doc_id": "doc-001",
            "tables": [{"table_id": "t1", "page_number": 1, "rows": 2}],
            "quality_signals": [{"code": "warn", "severity": "warning", "page_number": 1}],
            "parse_units": [{"parse_unit_id": "u1", "page_start": 1, "page_end": 2}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = create_export_package(payload, tmp)

            self.assertEqual(manifest["doc_id"], "doc-001")
            self.assertEqual(manifest["state"], "done")
            self.assertTrue(manifest["export_id"].startswith("exp_"))
            self.assertEqual(
                [(entry["dataset"], entry["format"], entry["path"]) for entry in manifest["files"]],
                [
                    ("tables", "csv", "tables.csv"),
                    ("quality_signals", "jsonl", "quality_signals.jsonl"),
                    ("parse_units", "tsv", "parse_units.tsv"),
                ],
            )
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "tables.csv").exists())
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "quality_signals.jsonl").exists())
            self.assertTrue(export_file_path(tmp, manifest["export_id"], "parse_units.tsv").exists())
            self.assertEqual(load_export_manifest(tmp, manifest["export_id"]), manifest)
            for entry in manifest["files"]:
                self.assertGreater(entry["bytes"], 0)
                self.assertIn("content_type", entry)

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
                filters={"severity": ["warning"], "page_range": {"start": 2, "end": 3}},
            )

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

    def test_custom_invalid_dataset_and_format_raise_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invalid_export_dataset"):
                create_export_package({}, tmp, includes=["pages"])

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invalid_export_format"):
                create_export_package({}, tmp, includes=["tables"], formats={"tables": "xlsx"})

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
