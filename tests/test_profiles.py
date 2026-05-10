from __future__ import annotations

import unittest

from parsecore.profiles import MIB, describe_parse_profiles, resolve_parse_profile


class ParseProfileResolutionTests(unittest.TestCase):
    def test_describe_parse_profiles_exposes_supported_profiles_and_auto_thresholds(self) -> None:
        description = describe_parse_profiles()

        self.assertEqual(description["default_profile"], "default")
        self.assertEqual(description["auto_profile"], "auto")
        self.assertEqual(
            description["supported_profiles"],
            [
                "default",
                "large-pdf",
                "large-pdf-catalog",
                "large-pdf-ledger",
                "table-heavy",
                "ocr-heavy",
                "excel-ledger",
                "scan-pdf",
            ],
        )
        self.assertEqual(description["default_auto_rule_thresholds"]["max_file_size_bytes"], 50 * MIB)
        self.assertEqual(description["default_auto_rule_thresholds"]["max_page_count"], 500)
        self.assertEqual(description["default_auto_rule_thresholds"]["max_table_density"], 0.5)
        self.assertEqual(
            description["recommended_async_profiles"],
            ["large-pdf", "large-pdf-catalog", "large-pdf-ledger", "scan-pdf"],
        )
        self.assertTrue(
            any(
                rule["profile"] == "large-pdf" and rule["recommended_async"]
                for rule in description["auto_rules"]
            )
        )

    def test_requested_profile_takes_precedence(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="huge.pdf",
            file_size_bytes=100 * MIB,
            page_count=1000,
            table_count=900,
            requested_profile="table-heavy",
        )

        self.assertEqual(resolved["profile"], "table-heavy")
        self.assertEqual(resolved["source"], "requested")
        self.assertFalse(resolved["recommended_async"])
        self.assertTrue(resolved["profile_known"])
        self.assertIn("requested_profile=table-heavy", resolved["reasons"])

    def test_unknown_requested_profile_is_reported_without_rejection(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="manual.pdf",
            file_size_bytes=1 * MIB,
            page_count=1,
            table_count=0,
            requested_profile="large_pdf",
        )

        self.assertEqual(resolved["profile"], "large_pdf")
        self.assertEqual(resolved["source"], "requested")
        self.assertFalse(resolved["profile_known"])
        self.assertEqual(resolved["profile_warning"], "unknown_profile")
        self.assertEqual(resolved["limits"]["profile"], "custom")

    def test_requested_scan_profile_recommends_async(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="scan.pdf",
            file_size_bytes=2 * MIB,
            page_count=1,
            table_count=0,
            requested_profile="scan-pdf",
        )

        self.assertEqual(resolved["profile"], "scan-pdf")
        self.assertEqual(resolved["source"], "requested")
        self.assertTrue(resolved["recommended_async"])

    def test_large_pdf_by_page_count_recommends_async(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="manual.pdf",
            file_size_bytes=3 * MIB,
            page_count=500,
            table_count=0,
            requested_profile="auto",
        )

        self.assertEqual(resolved["profile"], "large-pdf")
        self.assertTrue(resolved["recommended_async"])
        self.assertIn("page_count>=500", resolved["reasons"])

    def test_large_pdf_catalog_by_name_hint_takes_precedence(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="approved-products-catalog.pdf",
            file_size_bytes=3 * MIB,
            page_count=500,
            table_count=0,
            requested_profile="auto",
        )

        self.assertEqual(resolved["profile"], "large-pdf-catalog")
        self.assertTrue(resolved["recommended_async"])
        self.assertIn("page_count>=500", resolved["reasons"])
        self.assertIn("catalog_name_hint", resolved["reasons"])

    def test_large_pdf_ledger_by_table_density_takes_precedence(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="huge-report.pdf",
            file_size_bytes=3 * MIB,
            page_count=600,
            table_count=500,
            requested_profile=None,
        )

        self.assertEqual(resolved["profile"], "large-pdf-ledger")
        self.assertTrue(resolved["recommended_async"])
        self.assertIn("table_density>=0.5", resolved["reasons"])

    def test_requested_large_pdf_catalog_profile_is_known(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="manual.pdf",
            file_size_bytes=1 * MIB,
            page_count=1,
            table_count=0,
            requested_profile="large-pdf-catalog",
        )

        self.assertEqual(resolved["profile"], "large-pdf-catalog")
        self.assertEqual(resolved["source"], "requested")
        self.assertTrue(resolved["profile_known"])
        self.assertTrue(resolved["recommended_async"])

    def test_table_heavy_pdf_by_density(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/pdf",
            file_name="report.pdf",
            file_size_bytes=2 * MIB,
            page_count=10,
            table_count=5,
            requested_profile=None,
        )

        self.assertEqual(resolved["profile"], "table-heavy")
        self.assertEqual(resolved["source"], "auto")
        self.assertFalse(resolved["recommended_async"])
        self.assertIn("table_density>=0.5", resolved["reasons"])

    def test_excel_input_uses_ledger_profile(self) -> None:
        resolved = resolve_parse_profile(
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_name="ledger.xlsx",
            file_size_bytes=None,
            page_count=None,
            table_count=None,
            requested_profile=None,
        )

        self.assertEqual(resolved["profile"], "excel-ledger")
        self.assertIn("excel_input", resolved["reasons"])

    def test_image_input_uses_ocr_profile(self) -> None:
        resolved = resolve_parse_profile(
            media_type="image/png",
            file_name="receipt.png",
            file_size_bytes=400_000,
            page_count=None,
            table_count=None,
            requested_profile=None,
        )

        self.assertEqual(resolved["profile"], "ocr-heavy")
        self.assertIn("image_input", resolved["reasons"])

    def test_default_when_no_rule_matches(self) -> None:
        resolved = resolve_parse_profile(
            media_type="text/plain",
            file_name="notes.txt",
            file_size_bytes=1000,
            page_count=1,
            table_count=0,
            requested_profile=None,
        )

        self.assertEqual(resolved["profile"], "default")
        self.assertEqual(resolved["source"], "auto")
        self.assertFalse(resolved["recommended_async"])
        self.assertIn("fallback_default", resolved["reasons"])
        self.assertIn("limits", resolved)


if __name__ == "__main__":
    unittest.main()
