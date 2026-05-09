from __future__ import annotations

import unittest

from parsecore.parts import document_parts_projection


class DocumentPartsProjectionTests(unittest.TestCase):
    def test_projects_parse_units_with_quality_signal_state(self) -> None:
        payload = {
            "schema_version": "2026-06",
            "doc_id": "doc-parts",
            "parse_run_id": "job-001",
            "profile": "large-pdf",
            "state": "done",
            "parse_units": [
                {
                    "parse_unit_id": "doc-parts:unit:1",
                    "source_doc_id": "doc-parts",
                    "part_doc_id": "doc-parts",
                    "part_index": 1,
                    "page_start": 1,
                    "page_end": 10,
                    "state": "done",
                    "table_count": 3,
                },
                {
                    "parse_unit_id": "doc-parts:unit:2",
                    "source_doc_id": "doc-parts",
                    "part_doc_id": "doc-parts",
                    "part_index": 2,
                    "page_start": 11,
                    "page_end": 20,
                    "state": "done",
                    "table_count": 2,
                },
            ],
            "quality_signals": [
                {
                    "code": "truncated_table",
                    "severity": "warning",
                    "page_number": 12,
                },
                {
                    "code": "ocr_failed_page",
                    "severity": "error",
                    "page_number": 15,
                },
            ],
        }

        result = document_parts_projection(payload)

        self.assertEqual(result["projection"], "parts")
        self.assertEqual(result["part_summary"]["total"], 2)
        self.assertTrue(result["part_summary"]["partitioned"])
        self.assertEqual(result["part_summary"]["states"], {"done": 1, "warning": 1})
        self.assertEqual(result["parts"][0]["state"], "done")
        self.assertEqual(result["parts"][1]["state"], "warning")
        self.assertEqual(result["parts"][1]["quality_signal_count"], 2)
        self.assertEqual(result["parts"][1]["quality_signal_codes"], ["ocr_failed_page", "truncated_table"])
        self.assertEqual(result["parts"][1]["severity_counts"], {"error": 1, "warning": 1})
        self.assertFalse(result["parts"][1]["rerun_supported"])

    def test_filters_part_states_with_comma_and_pipe(self) -> None:
        payload = {
            "doc_id": "doc-parts",
            "parse_units": [
                {"parse_unit_id": "u1", "state": "done", "page_start": 1, "page_end": 1},
                {"parse_unit_id": "u2", "state": "failed", "page_start": 2, "page_end": 2},
                {"parse_unit_id": "u3", "state": "parsing", "page_start": 3, "page_end": 3},
            ],
        }

        result = document_parts_projection(payload, state_filter="failed|parsing")

        self.assertEqual(result["state_filter"], ["failed", "parsing"])
        self.assertEqual([part["part_id"] for part in result["parts"]], ["u2", "u3"])
        self.assertEqual(result["part_summary"]["filtered"], 2)

    def test_rejects_invalid_state_filter(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_part_state"):
            document_parts_projection({"parse_units": []}, state_filter="cancelled")


if __name__ == "__main__":
    unittest.main()
