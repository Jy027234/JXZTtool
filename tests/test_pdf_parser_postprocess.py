from __future__ import annotations

import unittest

from parsecore.parsers import (
    _looks_heading_like,
    _merge_short_blocks,
    _normalize_ocr_provider_counts,
    _normalize_ocr_provider_timings,
    _ocr_fallback_reason_for_page,
    _prepare_ocr_input_image,
    _split_pdf_page_text,
    _strip_repeated_headers_footers,
)


class StripRepeatedHeadersFootersTests(unittest.TestCase):
    def test_returns_pages_unchanged_when_fewer_than_three(self) -> None:
        pages = ["a\nbody", "a\nbody"]
        self.assertEqual(_strip_repeated_headers_footers(pages), pages)

    def test_removes_lines_repeating_on_majority_of_pages(self) -> None:
        pages = [
            "Confidential Header\nparagraph one content here.\nCompany Footer Line",
            "Confidential Header\nparagraph two keeps going.\nCompany Footer Line",
            "Confidential Header\nparagraph three body text.\nCompany Footer Line",
            "Confidential Header\nparagraph four wraps up.\nCompany Footer Line",
        ]
        cleaned = _strip_repeated_headers_footers(pages)
        for page in cleaned:
            self.assertNotIn("Confidential Header", page)
            self.assertNotIn("Company Footer Line", page)
        self.assertIn("paragraph one content here.", cleaned[0])

    def test_preserves_unique_lines(self) -> None:
        pages = [
            "Header\nunique alpha content\nFooter",
            "Header\nunique beta content\nFooter",
            "Header\nunique gamma content\nFooter",
        ]
        cleaned = _strip_repeated_headers_footers(pages)
        self.assertIn("unique alpha content", cleaned[0])
        self.assertIn("unique gamma content", cleaned[2])
        for page in cleaned:
            self.assertNotIn("Header", page)
            self.assertNotIn("Footer", page)


class MergeShortBlocksTests(unittest.TestCase):
    def test_empty_and_single_returns_input(self) -> None:
        self.assertEqual(_merge_short_blocks([]), [])
        self.assertEqual(_merge_short_blocks(["only one paragraph"]), ["only one paragraph"])

    def test_merges_short_fragment_into_previous(self) -> None:
        paragraphs = ["first real paragraph of text.", "x", "second paragraph continues."]
        merged = _merge_short_blocks(paragraphs)
        self.assertEqual(len(merged), 2)
        self.assertIn("first real paragraph", merged[0])
        self.assertIn("x", merged[0])

    def test_preserves_heading_like_short_blocks(self) -> None:
        paragraphs = ["TITLE", "actual body paragraph with enough content."]
        merged = _merge_short_blocks(paragraphs)
        self.assertEqual(merged[0], "TITLE")
        self.assertEqual(len(merged), 2)

    def test_preserves_numbered_heading(self) -> None:
        paragraphs = ["1. Intro", "content paragraph one."]
        merged = _merge_short_blocks(paragraphs)
        # "1. Intro" length 8 < 10 but heading-like → kept standalone
        self.assertEqual(merged[0], "1. Intro")
        self.assertEqual(len(merged), 2)

    def test_leading_short_block_kept_when_no_predecessor(self) -> None:
        paragraphs = ["x", "body paragraph content."]
        merged = _merge_short_blocks(paragraphs)
        # first block has no predecessor to merge into → kept
        self.assertEqual(merged[0], "x")


class LooksHeadingLikeTests(unittest.TestCase):
    def test_all_caps_short(self) -> None:
        self.assertTrue(_looks_heading_like("TABLE OF CONTENTS"))

    def test_numbered(self) -> None:
        self.assertTrue(_looks_heading_like("2.1. Scope"))

    def test_rejects_long_text(self) -> None:
        self.assertFalse(_looks_heading_like("x" * 100))

    def test_rejects_multiline(self) -> None:
        self.assertFalse(_looks_heading_like("TITLE\nmore"))


class SplitPdfPageTextTests(unittest.TestCase):
    def test_splits_on_blank_lines(self) -> None:
        text = "para one line 1\npara one line 2\n\npara two\n\n\npara three"
        parts = _split_pdf_page_text(text)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "para one line 1\npara one line 2")
        self.assertEqual(parts[2], "para three")


class OcrFallbackReasonTests(unittest.TestCase):
    def test_empty_page_requests_ocr(self) -> None:
        self.assertEqual(
            _ocr_fallback_reason_for_page(
                "   ", min_cid_tokens=5, min_cid_char_ratio=0.12
            ),
            "empty_text",
        )

    def test_cid_dense_page_requests_ocr(self) -> None:
        text = " ".join(["(cid:14)"] * 12) + " trailing"
        self.assertEqual(
            _ocr_fallback_reason_for_page(
                text, min_cid_tokens=5, min_cid_char_ratio=0.12
            ),
            "cid_dense",
        )

    def test_clean_text_does_not_request_ocr(self) -> None:
        self.assertIsNone(
            _ocr_fallback_reason_for_page(
                "正常中文正文内容，包含标题和段落。",
                min_cid_tokens=5,
                min_cid_char_ratio=0.12,
            )
        )


class PrepareOcrInputImageTests(unittest.TestCase):
    def test_rapidocr_uses_grayscale_image(self) -> None:
        from PIL import Image

        class _RapidOcrEngine:
            pass

        _RapidOcrEngine.__module__ = "rapidocr_onnxruntime.engine"
        image = Image.new("RGB", (10, 10), color=(255, 0, 0))

        prepared = _prepare_ocr_input_image(image, engine=_RapidOcrEngine())

        self.assertEqual(prepared.mode, "L")

    def test_non_rapidocr_keeps_rgb_image(self) -> None:
        from PIL import Image

        class _OtherEngine:
            pass

        _OtherEngine.__module__ = "custom_ocr.engine"
        image = Image.new("RGB", (10, 10), color=(255, 0, 0))

        prepared = _prepare_ocr_input_image(image, engine=_OtherEngine())

        self.assertEqual(prepared.mode, "RGB")

    def test_parsecore_rapidocr_wrapper_uses_grayscale_image(self) -> None:
        from PIL import Image

        class _WrappedRapidOcrEngine:
            _parsecore_rapidocr = True

        image = Image.new("RGB", (10, 10), color=(255, 0, 0))

        prepared = _prepare_ocr_input_image(image, engine=_WrappedRapidOcrEngine())

        self.assertEqual(prepared.mode, "L")


class NormalizeOcrProviderTimingsTests(unittest.TestCase):
    def test_rapidocr_stage_list_becomes_total_and_named_stages(self) -> None:
        total, det_elapsed_s, cls_elapsed_s, rec_elapsed_s = _normalize_ocr_provider_timings(
            [0.37, 0.12, 2.01]
        )

        self.assertAlmostEqual(total, 2.5)
        self.assertAlmostEqual(det_elapsed_s, 0.37)
        self.assertAlmostEqual(cls_elapsed_s, 0.12)
        self.assertAlmostEqual(rec_elapsed_s, 2.01)

    def test_scalar_provider_elapsed_keeps_backward_compatible_total(self) -> None:
        total, det_elapsed_s, cls_elapsed_s, rec_elapsed_s = _normalize_ocr_provider_timings(0.42)

        self.assertAlmostEqual(total, 0.42)
        self.assertEqual(det_elapsed_s, 0.0)
        self.assertEqual(cls_elapsed_s, 0.0)
        self.assertEqual(rec_elapsed_s, 0.0)

    def test_mapping_provider_elapsed_extracts_named_stages(self) -> None:
        total, det_elapsed_s, cls_elapsed_s, rec_elapsed_s = _normalize_ocr_provider_timings(
            {
                "elapsed": 2.5,
                "det_elapsed_s": 0.37,
                "cls_elapsed_s": 0.12,
                "rec_elapsed_s": 2.01,
            }
        )

        self.assertAlmostEqual(total, 2.5)
        self.assertAlmostEqual(det_elapsed_s, 0.37)
        self.assertAlmostEqual(cls_elapsed_s, 0.12)
        self.assertAlmostEqual(rec_elapsed_s, 2.01)


class NormalizeOcrProviderCountsTests(unittest.TestCase):
    def test_mapping_provider_counts_extracts_classifier_hits(self) -> None:
        crop_count, rotate_positive_count, rotate_high_count = _normalize_ocr_provider_counts(
            {
                "crop_count": 92,
                "cls_rotate_positive_count": 8,
                "cls_rotate_high_count": 4,
            }
        )

        self.assertEqual(crop_count, 92)
        self.assertEqual(rotate_positive_count, 8)
        self.assertEqual(rotate_high_count, 4)


if __name__ == "__main__":
    unittest.main()
