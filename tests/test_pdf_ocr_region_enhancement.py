"""待办 4: a mixed page is enhanced only where it needs it.

A page that is part digital text and part scan (or one whose column has no font
mapping) used to be all-or-nothing: either the whole page was re-read by OCR and
its good text layer discarded, or nothing happened and the unreadable lines
stayed unreadable.  Region enhancement reads only the lines that cannot be
trusted, merges the result back with the page's own lines, and drops what the
text layer already provides.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from parsecore.parsers import (
    PdfTextParser,
    _merge_native_and_ocr_lines,
    _ocr_enhancement_regions,
    _OcrStageTimings,
    _page_text_lines,
)


GARBLED = "(cid:12)(cid:13)(cid:14)(cid:15)(cid:16) torque value"
USABLE_ONE = "Apply the specified torque to the fastener."
USABLE_TWO = "Replace the self-locking nut after removal."


def _line(text: str, top: float, *, x0: float = 50.0, x1: float = 280.0) -> dict:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 12.0}


class _FakeCrop:
    def __init__(self, bbox: tuple[float, float, float, float]) -> None:
        self.bbox = bbox

    def to_image(self, *, resolution: int) -> SimpleNamespace:
        width = max(1, int((self.bbox[2] - self.bbox[0]) / 72.0 * resolution))
        height = max(1, int((self.bbox[3] - self.bbox[1]) / 72.0 * resolution))
        image = Image.new("RGB", (width, height), "white")
        return SimpleNamespace(original=image)


class _FakePage:
    """A pdfplumber-like page whose lines and crops are known exactly."""

    def __init__(self, lines: list[dict], *, width: float = 300.0, height: float = 400.0) -> None:
        self.width = width
        self.height = height
        self.images: list[object] = []
        self._lines = list(lines)
        self.cropped: list[tuple[float, float, float, float]] = []

    def extract_text_lines(self) -> list[dict]:
        return [dict(line) for line in self._lines]

    def extract_text(self) -> str:
        return "\n".join(str(line["text"]) for line in self._lines)

    def crop(self, bbox) -> _FakeCrop:
        self.cropped.append(tuple(float(value) for value in bbox))
        return _FakeCrop(tuple(float(value) for value in bbox))


class _RecordingEngine:
    """Returns queued OCR results and records how often it was called."""

    def __init__(self, *results: list) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self, array):
        self.calls += 1
        if not self.results:
            return [], {}
        return self.results.pop(0), {}


def _parser(**post_process) -> PdfTextParser:
    options = {"dual_channel": True, "ocr_bad_pages": True}
    options.update(post_process)
    return PdfTextParser(
        media_types=["application/pdf"],
        extensions=[".pdf"],
        options={"post_process": options},
    )


def _box(x0: float, top: float, x1: float, bottom: float) -> list[list[float]]:
    return [[x0, top], [x1, top], [x1, bottom], [x0, bottom]]


class EnhancementRegionTests(unittest.TestCase):
    def test_a_healthy_page_has_no_region(self) -> None:
        lines = _page_text_lines(
            _FakePage([_line(USABLE_ONE, 100.0), _line(USABLE_TWO, 130.0)])
        )
        regions = _ocr_enhancement_regions(
            lines=lines,
            table_bboxes=(),
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            page_width=300.0,
            page_height=400.0,
        )
        self.assertEqual(regions, [])

    def test_only_the_unreadable_lines_become_a_region(self) -> None:
        page = _FakePage(
            [
                _line(USABLE_ONE, 100.0),
                _line(GARBLED, 200.0),
                _line(USABLE_TWO, 300.0),
            ]
        )
        lines = _page_text_lines(page)
        regions = _ocr_enhancement_regions(
            lines=lines,
            table_bboxes=(),
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            page_width=300.0,
            page_height=400.0,
        )

        self.assertEqual(len(regions), 1)
        x0, top, x1, bottom = regions[0]
        # The band covers the garbled line, not the whole page.
        self.assertLessEqual(top, 200.0)
        self.assertGreaterEqual(bottom, 212.0)
        self.assertGreater(top, 100.0)
        self.assertLess(bottom, 300.0)

    def test_a_table_region_is_left_to_the_table_block(self) -> None:
        page = _FakePage([_line(GARBLED, 200.0)])
        lines = _page_text_lines(page)
        regions = _ocr_enhancement_regions(
            lines=lines,
            table_bboxes=((40.0, 190.0, 290.0, 220.0),),
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            page_width=300.0,
            page_height=400.0,
        )
        self.assertEqual(regions, [])

    def test_a_mostly_unreadable_page_falls_back_to_the_whole_page(self) -> None:
        page = _FakePage([_line(GARBLED, 20.0 + index * 30) for index in range(14)])
        lines = _page_text_lines(page)
        regions = _ocr_enhancement_regions(
            lines=lines,
            table_bboxes=(),
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            page_width=300.0,
            page_height=400.0,
        )
        self.assertEqual(regions, [])


class MixedPageEnhancementTests(unittest.TestCase):
    def _page(self) -> _FakePage:
        return _FakePage(
            [
                _line(USABLE_ONE, 100.0),
                _line(GARBLED, 200.0),
                _line(USABLE_TWO, 300.0),
            ]
        )

    def test_only_the_region_is_rendered_and_the_good_text_survives(self) -> None:
        page = self._page()
        engine = _RecordingEngine([(_box(2.0, 2.0, 60.0, 12.0), "Torque value", 0.95)])
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, "\n".join([USABLE_ONE, GARBLED, USABLE_TWO])
            )

        self.assertIsNone(error)
        # The page-level detector fired first, so it names the trigger even though
        # only one band was actually read.
        self.assertEqual(reason, "cid_dense")
        self.assertEqual(engine.calls, 1)
        # One crop, around the garbled line only.
        self.assertEqual(len(page.cropped), 1)
        self.assertGreater(page.cropped[0][1], 100.0)
        self.assertLess(page.cropped[0][3], 300.0)
        # The good lines are kept verbatim, the unreadable one is replaced.
        self.assertIn(USABLE_ONE, text or "")
        self.assertIn(USABLE_TWO, text or "")
        self.assertIn("Torque value", text or "")
        self.assertNotIn("cid:12", text or "")
        # Every line is located, and the sources stay distinguishable.
        kinds = {line["source_kind"] for line in timings.ocr_lines}
        self.assertEqual(kinds, {"pdf_native_text", "pdf_ocr_fallback"})

    def test_lines_that_are_fine_on_their_own_trigger_region_enhancement(self) -> None:
        # The page reads fine as a whole (a handful of bad tokens stays under the
        # page threshold) but individual lines do not.
        page = self._page()
        engine = _RecordingEngine([(_box(2.0, 2.0, 60.0, 12.0), "Torque value", 0.95)])
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, USABLE_ONE
            )

        self.assertIsNone(error)
        self.assertEqual(reason, "mixed_page_regions")
        self.assertIn("Torque value", text or "")
        self.assertIn(USABLE_ONE, text or "")

    def test_the_recovered_line_keeps_page_coordinates(self) -> None:
        page = self._page()
        engine = _RecordingEngine([(_box(2.0, 2.0, 60.0, 12.0), "Torque value", 0.95)])
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            _text, _reason, _error, timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, GARBLED
            )

        recovered = [
            line for line in timings.ocr_lines if line["source_kind"] == "pdf_ocr_fallback"
        ]
        self.assertTrue(recovered)
        x0, top, x1, bottom = recovered[0]["bbox"]
        crop = page.cropped[0]
        # The box is inside the region it was read from, in page coordinates.
        self.assertGreaterEqual(x0, crop[0] - 1.0)
        self.assertGreaterEqual(top, crop[1] - 1.0)
        self.assertLessEqual(x1, crop[2] + 1.0)
        self.assertLessEqual(bottom, crop[3] + 1.0)

    def test_a_line_the_text_layer_already_provides_is_not_repeated(self) -> None:
        page = _FakePage([_line(USABLE_ONE, 100.0), _line(GARBLED, 200.0)])
        engine = _RecordingEngine(
            [
                (_box(2.0, 2.0, 200.0, 12.0), USABLE_ONE, 0.95),
                (_box(2.0, 2.0, 120.0, 12.0), "Torque value", 0.95),
            ]
        )
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, _reason, _error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, USABLE_ONE
            )

        self.assertEqual((text or "").count(USABLE_ONE), 1)
        self.assertIn("Torque value", text or "")

    def test_an_unreadable_line_stays_visible_when_ocr_recovers_nothing(self) -> None:
        page = _FakePage([_line(USABLE_ONE, 100.0), _line(GARBLED, 200.0)])
        engine = _RecordingEngine([])  # engine returns nothing
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, USABLE_ONE
            )

        self.assertIsNone(text)
        self.assertEqual(reason, "mixed_page_regions")
        self.assertEqual(error, "empty_ocr_text")

    def test_a_scanned_page_still_uses_the_whole_page_path(self) -> None:
        # No native lines at all: there is no region to enhance, so the page is
        # read in one pass as before.
        page = _FakePage([])
        page.images = [object()]
        engine = _RecordingEngine()
        parser = _parser()

        with patch.object(
            parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)
        ), patch(
            "parsecore.parsers._extract_ocr_text_from_page",
            return_value=("Whole page OCR", None, _OcrStageTimings()),
        ) as whole_page:
            text, reason, _error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, ""
            )

        whole_page.assert_called_once()
        self.assertEqual(text, "Whole page OCR")
        self.assertEqual(reason, "native_text_empty")
        self.assertEqual(page.cropped, [])

    def test_a_forced_full_page_request_does_not_use_regions(self) -> None:
        page = self._page()
        engine = _RecordingEngine()
        parser = _parser()

        with patch.object(
            parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)
        ), patch(
            "parsecore.parsers._extract_ocr_text_from_page",
            return_value=("Forced OCR text", None, _OcrStageTimings()),
        ) as whole_page, patch(
            "parsecore.parsers._extract_ocr_region_lines"
        ) as region:
            text, reason, _error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, "\n".join([USABLE_ONE, GARBLED, USABLE_TWO]), force_full_page=True
            )

        whole_page.assert_called_once()
        region.assert_not_called()
        self.assertEqual(text, "Forced OCR text")
        self.assertEqual(reason, "forced_full_page")


class MergeHelperTests(unittest.TestCase):
    def test_conflicts_resolve_towards_the_reliable_side(self) -> None:
        native = _page_text_lines(
            _FakePage([_line(USABLE_ONE, 100.0), _line(GARBLED, 200.0)])
        )
        merged = _merge_native_and_ocr_lines(
            native_lines=native,
            ocr_lines=[
                {"text": "Torque value", "bbox": (50.0, 200.0, 150.0, 212.0), "source_kind": "pdf_ocr_fallback"}
            ],
            replaced=[native[1]],
        )

        texts = [line["text"] for line in merged]
        self.assertEqual(texts, [USABLE_ONE, "Torque value"])

    def test_lines_are_ordered_by_position(self) -> None:
        native = _page_text_lines(
            _FakePage([_line(USABLE_ONE, 300.0), _line(USABLE_TWO, 100.0)])
        )
        merged = _merge_native_and_ocr_lines(native_lines=native, ocr_lines=[], replaced=[])

        self.assertEqual([line["text"] for line in merged], [USABLE_TWO, USABLE_ONE])


if __name__ == "__main__":
    unittest.main()
