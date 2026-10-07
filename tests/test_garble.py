from __future__ import annotations

import unittest

from parsecore.garble import analyze_text_garble, detect_page_garble_reason
from parsecore.models import Block, BlockType
from parsecore.parsers import _ocr_fallback_reason_for_page
from parsecore.quality import evaluate_parse_quality


class NumericSlashGarbleTests(unittest.TestCase):
    def _maintenance_list(self) -> str:
        rows = [f"698.{index}/698.{index + 1} May 31/08" for index in range(1, 18)]
        rows.extend(f"698.{index} Feb 05/21" for index in range(18, 30))
        return "LIST OF EFFECTIVE PAGES (CONTINUED)\n" + "\n".join(rows)

    def test_numeric_date_and_page_separators_are_not_pdf_name_tokens(self) -> None:
        text = self._maintenance_list()
        signals = analyze_text_garble(text)

        self.assertEqual(signals.total_pdf_name_tokens, 0)
        self.assertIsNone(
            detect_page_garble_reason(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )
        self.assertIsNone(
            _ocr_fallback_reason_for_page(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )

    def test_quality_does_not_flag_numeric_maintenance_list_as_garble(self) -> None:
        quality = evaluate_parse_quality(
            [
                Block(
                    block_id="maintenance-list",
                    doc_id="doc-maintenance-list",
                    type=BlockType.PARAGRAPH,
                    content=self._maintenance_list(),
                    metadata={"page": 1},
                )
            ]
        )

        self.assertNotIn("pdf_name_garble", quality.flags)
        self.assertNotIn("pdf_name_warn", quality.flags)
        self.assertNotIn("repeated_short_tokens", quality.flags)

    def test_dense_standalone_pdf_names_still_request_ocr(self) -> None:
        text = " /0 /1 /2 /3 /i255 /i128 /9 /8 " * 6

        self.assertGreater(analyze_text_garble(text).total_pdf_name_tokens, 0)
        self.assertEqual(
            _ocr_fallback_reason_for_page(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            ),
            "pdf_name_dense",
        )

    def test_repeated_standalone_numbers_and_words_still_trigger_garble_signal(self) -> None:
        for text in ("1 1 1 1 1 1", "lo lo lo lo lo"):
            with self.subTest(text=text):
                self.assertEqual(
                    detect_page_garble_reason(
                        text,
                        min_cid_tokens=5,
                        min_cid_char_ratio=0.12,
                    ),
                    "repeated_short_tokens",
                )

    def test_readable_numeric_item_reference_does_not_trigger_short_token_signal(self) -> None:
        text = "(4) Items 4-130 to 4-300 (refer to Tables 610A to 610C)."
        signals = analyze_text_garble(text)

        self.assertLess(signals.repeated_short_token_ratio, 0.35)
        self.assertIsNone(
            detect_page_garble_reason(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )

    def test_page_291_margin_marks_do_not_trigger_full_page_ocr(self) -> None:
        # Native extraction from page 291: numeric part/page identifiers are
        # compounds, while the original R margin marks remain separate tokens.
        text = "320-366-701-0 R R R R R R - ITEM NOT ILLUSTRATED Page 1026 73-22-87 Feb 05/21"
        signals = analyze_text_garble(text)

        self.assertLess(signals.repeated_short_token_ratio, 0.35)
        self.assertIsNone(
            detect_page_garble_reason(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )
        self.assertIsNone(
            _ocr_fallback_reason_for_page(
                text,
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )


if __name__ == "__main__":
    unittest.main()
