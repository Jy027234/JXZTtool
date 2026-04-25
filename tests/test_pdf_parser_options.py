from __future__ import annotations

import unittest

from parsecore.parsers import PdfTextParser, build_parser


class PdfTextParserOptionsTests(unittest.TestCase):
    def test_defaults_keep_opt_in_features_off(self) -> None:
        parser = PdfTextParser(media_types=["application/pdf"], extensions=[".pdf"])
        self.assertFalse(parser._strip_hf_enabled)
        self.assertTrue(parser._merge_short_enabled)
        self.assertEqual(parser._short_block_min_length, 10)
        self.assertAlmostEqual(parser._hf_threshold, 0.5)
        self.assertEqual(parser._hf_head_n, 3)
        self.assertEqual(parser._hf_tail_n, 3)
        self.assertEqual(parser._hf_min_line_len, 4)
        self.assertFalse(parser._ocr_bad_pages_enabled)
        self.assertEqual(parser._ocr_bad_page_min_cid_tokens, 5)
        self.assertAlmostEqual(parser._ocr_bad_page_min_cid_char_ratio, 0.12)

    def test_options_override_post_process(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={
                "post_process": {
                    "strip_headers_footers": True,
                    "merge_short_blocks": False,
                    "short_block_min_length": 25,
                    "hf_threshold": 0.7,
                    "hf_head_n": 2,
                    "hf_tail_n": 1,
                    "hf_min_line_len": 6,
                    "ocr_bad_pages": True,
                    "ocr_bad_page_min_cid_tokens": 9,
                    "ocr_bad_page_min_cid_char_ratio": 0.2,
                }
            },
        )
        self.assertTrue(parser._strip_hf_enabled)
        self.assertFalse(parser._merge_short_enabled)
        self.assertEqual(parser._short_block_min_length, 25)
        self.assertAlmostEqual(parser._hf_threshold, 0.7)
        self.assertEqual(parser._hf_head_n, 2)
        self.assertEqual(parser._hf_tail_n, 1)
        self.assertEqual(parser._hf_min_line_len, 6)
        self.assertTrue(parser._ocr_bad_pages_enabled)
        self.assertEqual(parser._ocr_bad_page_min_cid_tokens, 9)
        self.assertAlmostEqual(parser._ocr_bad_page_min_cid_char_ratio, 0.2)

    def test_build_parser_passes_options(self) -> None:
        parser = build_parser(
            "pdf-text",
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"short_block_min_length": 15}},
        )
        self.assertIsInstance(parser, PdfTextParser)
        self.assertEqual(parser._short_block_min_length, 15)  # type: ignore[attr-defined]

    def test_build_parser_without_options_uses_defaults(self) -> None:
        parser = build_parser(
            "pdf-text",
            media_types=["application/pdf"],
            extensions=[".pdf"],
        )
        self.assertIsInstance(parser, PdfTextParser)
        self.assertEqual(parser._short_block_min_length, 10)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
