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
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from parsecore.models import ParseRequest
from parsecore.parsers import (
    PdfTextParser,
    _image_ocr_regions,
    _merge_native_and_ocr_lines,
    _ocr_enhancement_regions,
    _OcrStageTimings,
    _PageLayout,
    _normalize_pdf_symbol_line_records,
    _normalize_pdf_symbol_text,
    _page_text_lines,
    _strip_pdf_page_furniture_lines,
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


class PictureRegionTests(unittest.TestCase):
    """待办 4: a scanned insert on a digital page is read only when asked for."""

    def _page_with_picture(self, *, picture_top: float = 180.0) -> _FakePage:
        page = _FakePage(
            [_line(USABLE_ONE, 100.0), _line(USABLE_TWO, 320.0)],
            width=300.0,
            height=400.0,
        )
        # A 200x100pt picture covering a sixth of the page, with no text on it.
        page.images = [
            {"x0": 60.0, "top": picture_top, "x1": 260.0, "bottom": picture_top + 100.0}
        ]
        return page

    def test_a_picture_region_is_read_when_the_caller_asks_for_it(self) -> None:
        page = self._page_with_picture()
        engine = _RecordingEngine(
            [(_box(2.0, 2.0, 120.0, 14.0), "320-366-701-0", 0.93)]
        )
        parser = _parser(ocr_image_regions=True)

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, "\n".join([USABLE_ONE, USABLE_TWO])
            )

        self.assertIsNone(error)
        self.assertEqual(reason, "image_regions")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(len(page.cropped), 1)
        # The picture's text is added, and the page's own text is untouched.
        self.assertIn("320-366-701-0", text or "")
        self.assertIn(USABLE_ONE, text or "")
        self.assertIn(USABLE_TWO, text or "")
        # It is labelled as a transcription of an image, not as page text.
        self.assertIn(
            "pdf_ocr_image_region",
            {line["source_kind"] for line in timings.ocr_lines},
        )
        recovered = next(
            line
            for line in timings.ocr_lines
            if line["source_kind"] == "pdf_ocr_image_region"
        )
        x0, top, x1, bottom = recovered["bbox"]
        crop = page.cropped[0]
        self.assertGreaterEqual(x0, crop[0] - 1.0)
        self.assertGreaterEqual(top, crop[1] - 1.0)
        self.assertLessEqual(x1, crop[2] + 1.0)
        self.assertLessEqual(bottom, crop[3] + 1.0)

    def test_a_picture_region_is_left_alone_by_default(self) -> None:
        # Reading a picture costs a render plus an engine call and produces
        # transcribed, not extracted, text: it is an explicit request.
        page = self._page_with_picture()
        engine = _RecordingEngine(
            [(_box(2.0, 2.0, 120.0, 14.0), "320-366-701-0", 0.93)]
        )
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, "\n".join([USABLE_ONE, USABLE_TWO])
            )

        self.assertIsNone(text)
        self.assertIsNone(reason)
        self.assertIsNone(error)
        self.assertEqual(engine.calls, 0)
        self.assertEqual(page.cropped, [])

    def test_a_picture_under_the_text_is_not_read(self) -> None:
        # An image behind text is a background or a watermark: reading it would
        # only repeat what the page already says.
        page = _FakePage([_line(USABLE_ONE, 100.0), _line(USABLE_TWO, 320.0)], width=300.0, height=400.0)
        page.images = [{"x0": 40.0, "top": 90.0, "x1": 280.0, "bottom": 140.0}]
        engine = _RecordingEngine([])
        parser = _parser(ocr_image_regions=True)

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, _error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, "\n".join([USABLE_ONE, USABLE_TWO])
            )

        self.assertIsNone(text)
        self.assertIsNone(reason)
        self.assertEqual(engine.calls, 0)

    def test_a_tiny_picture_and_a_full_page_picture_are_not_regions(self) -> None:
        tiny = _FakePage([_line(USABLE_ONE, 100.0)], width=300.0, height=400.0)
        tiny.images = [{"x0": 10.0, "top": 10.0, "x1": 30.0, "bottom": 20.0}]
        full = _FakePage([_line(USABLE_ONE, 100.0)], width=300.0, height=400.0)
        full.images = [{"x0": 0.0, "top": 0.0, "x1": 300.0, "bottom": 400.0}]

        self.assertEqual(
            _image_ocr_regions(
                page=tiny,
                native_lines=_page_text_lines(tiny),
                table_bboxes=(),
                page_width=300.0,
                page_height=400.0,
            ),
            [],
        )
        self.assertEqual(
            _image_ocr_regions(
                page=full,
                native_lines=_page_text_lines(full),
                table_bboxes=(),
                page_width=300.0,
                page_height=400.0,
            ),
            [],
        )

    def test_a_picture_inside_a_table_belongs_to_the_table(self) -> None:
        page = self._page_with_picture()
        regions = _image_ocr_regions(
            page=page,
            native_lines=_page_text_lines(page),
            table_bboxes=[(50.0, 170.0, 270.0, 290.0)],
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

    def test_symbolmt_private_glyphs_map_to_unicode_with_font_and_bbox_evidence(self) -> None:
        glyphs = "\uf02d \uf0ae \uf0a3"
        page = _FakePage([])
        page.chars = [
            {
                "text": glyph,
                "fontname": "KKENPA+SymbolMT",
                "x0": 50.0 + index * 20.0,
                "top": 200.0,
                "x1": 60.0 + index * 20.0,
                "bottom": 212.0,
            }
            for index, glyph in enumerate(("\uf02d", "\uf0ae", "\uf0a3"))
        ]

        normalized, evidence = _normalize_pdf_symbol_text(page, glyphs)

        self.assertEqual(normalized, "- → ≤")
        self.assertEqual(
            {item["source_codepoint"]: item["unicode_text"] for item in evidence},
            {"U+F02D": "-", "U+F0AE": "→", "U+F0A3": "≤"},
        )
        self.assertTrue(all(item["source_font_names"] == ["KKENPA+SymbolMT"] for item in evidence))
        self.assertTrue(all(item["source_char_bboxes"] for item in evidence))

    def test_private_glyph_is_not_mapped_for_unknown_font_or_incomplete_source(self) -> None:
        glyph = "\uf0a3"
        unknown_font = _FakePage([])
        unknown_font.chars = [
            {
                "text": glyph,
                "fontname": "ABCDEF+Helvetica",
                "x0": 50.0,
                "top": 200.0,
                "x1": 60.0,
                "bottom": 212.0,
            }
        ]
        normalized_unknown, unknown_evidence = _normalize_pdf_symbol_text(unknown_font, glyph)
        self.assertEqual(normalized_unknown, glyph)
        self.assertEqual(unknown_evidence, [])

        incomplete_source = _FakePage([])
        incomplete_source.chars = [
            {
                "text": glyph,
                "fontname": "KKENPA+SymbolMT",
                "x0": 50.0,
                "top": 200.0,
                "x1": 60.0,
                "bottom": 212.0,
            },
            {"text": glyph, "fontname": "KKENPA+SymbolMT"},
        ]
        normalized_incomplete, incomplete_evidence = _normalize_pdf_symbol_text(
            incomplete_source,
            glyph,
        )
        self.assertEqual(normalized_incomplete, glyph)
        self.assertEqual(incomplete_evidence, [])

    def test_only_native_symbol_lines_are_normalized(self) -> None:
        glyph = "\uf0a3"
        bbox = [50.0, 200.0, 60.0, 212.0]
        page = _FakePage([])
        page.chars = [
            {
                "text": glyph,
                "fontname": "KKENPA+SymbolMT",
                "x0": bbox[0],
                "top": bbox[1],
                "x1": bbox[2],
                "bottom": bbox[3],
            }
        ]

        lines, evidence = _normalize_pdf_symbol_line_records(
            page,
            [
                {"text": glyph, "bbox": bbox, "source_kind": "pdf_native_text"},
                {"text": glyph, "bbox": bbox, "source_kind": "pdf_ocr_fallback"},
            ],
        )

        self.assertEqual(lines[0]["text"], "≤")
        self.assertEqual(lines[1]["text"], glyph)
        self.assertEqual(lines[0]["native_symbol_glyph_mappings"], evidence)
        self.assertEqual(evidence[0]["source_char_bboxes"], [bbox])

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

    def test_known_native_symbol_glyphs_do_not_trigger_empty_ocr_review(self) -> None:
        parser = _parser()
        engine = _RecordingEngine([])

        for symbol in ("\uf02d", "\uf0ae", "\uf0a3", "\uf02d -"):
            with self.subTest(symbol=repr(symbol)):
                page = _FakePage(
                    [_line(USABLE_ONE, 100.0), _line(symbol, 200.0), _line(USABLE_TWO, 300.0)]
                )
                glyphs = [character for character in symbol if character in "\uf02d\uf0ae\uf0a3"]
                page.chars = [
                    {
                        "text": character,
                        "fontname": "KKENPA+SymbolMT",
                        "x0": 50.0,
                        "top": 200.0,
                        "x1": 60.0,
                        "bottom": 212.0,
                    }
                    for character in glyphs
                ]
                with patch.object(
                    parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)
                ):
                    text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                        page, [], 1, "\n".join([USABLE_ONE, symbol, USABLE_TWO])
                    )

                self.assertIsNone(text)
                self.assertIsNone(reason)
                self.assertIsNone(error)
                self.assertEqual(engine.calls, 0)
                self.assertEqual(page.cropped, [])

    def test_same_private_use_glyph_from_unknown_font_still_requires_ocr_review(self) -> None:
        page = _FakePage([_line(USABLE_ONE, 100.0), _line("\uf02d", 200.0)])
        page.chars = [
            {
                "text": "\uf02d",
                "fontname": "ABCDEF+Helvetica",
                "x0": 50.0,
                "top": 200.0,
                "x1": 60.0,
                "bottom": 212.0,
            }
        ]
        engine = _RecordingEngine([])
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, f"{USABLE_ONE}\n\uf02d"
            )

        self.assertIsNone(text)
        self.assertEqual(reason, "mixed_page_regions")
        self.assertEqual(error, "empty_ocr_text")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(len(page.cropped), 1)

    def test_verified_symbol_glyph_mixed_with_body_garble_still_requires_ocr_review(self) -> None:
        mixed_line = f"\uf02d {GARBLED}"
        page = _FakePage([_line(USABLE_ONE, 100.0), _line(mixed_line, 200.0)])
        page.chars = [
            {
                "text": "\uf02d",
                "fontname": "KKENPA+SymbolMT",
                "x0": 50.0,
                "top": 200.0,
                "x1": 60.0,
                "bottom": 212.0,
            }
        ]
        engine = _RecordingEngine([])
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page, [], 1, f"{USABLE_ONE}\n{mixed_line}"
            )

        self.assertIsNone(text)
        self.assertEqual(reason, "cid_dense")
        self.assertEqual(error, "empty_ocr_text")
        self.assertEqual(engine.calls, 1)
        self.assertEqual(len(page.cropped), 1)

    def test_parse_drops_split_edge_furniture_and_keeps_page_content(self) -> None:
        running_identifier = "1234567890---"
        numeric_index_entry = "02 04 196 223 321"
        body_text = USABLE_ONE
        recovered_text = "Recovered torque value"
        line_records = [
            {
                "text": running_identifier,
                "bbox": (120.0, 12.0, 180.0, 24.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": body_text,
                "bbox": (50.0, 100.0, 280.0, 112.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": numeric_index_entry,
                "bbox": (50.0, 145.0, 280.0, 157.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": recovered_text,
                "bbox": (50.0, 200.0, 150.0, 212.0),
                "source_kind": "pdf_ocr_fallback",
            },
            {
                "text": "Pa",
                "bbox": (80.0, 430.0, 95.0, 442.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": "ge 129 / 20",
                "bbox": (97.0, 430.0, 150.0, 442.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": "26-08-20",
                "bbox": (152.0, 430.0, 215.0, 442.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": "Page 129",
                "bbox": (80.0, 470.0, 110.0, 482.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": "12 APR-34/",
                "bbox": (112.0, 470.0, 170.0, 482.0),
                "source_kind": "pdf_native_text",
            },
            {
                "text": "78 90",
                "bbox": (172.0, 470.0, 215.0, 482.0),
                "source_kind": "pdf_native_text",
            },
        ]
        stale_page_text = "\n".join(line["text"] for line in line_records)
        layout = _PageLayout(
            page_number=1,
            width=300.0,
            height=500.0,
            text_without_tables=stale_page_text,
            ocr_attempt_reason="mixed_page_regions",
            ocr_fallback_reason="mixed_page_regions",
            ocr_acceptance_reason="regions_enhanced",
            ocr_lines=line_records,
        )

        class _FakePdfReader:
            def __init__(self, _path: str) -> None:
                self.pages = [_FakePage([])]

        parser = _parser()
        with TemporaryDirectory(prefix="parsecore-edge-furniture-") as temp_dir:
            pdf_path = Path(temp_dir) / "region-enhanced.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-edge-furniture",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch(
                "parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader
            ), patch(
                "parsecore.parsers._extract_pdfplumber_layout", return_value=[layout]
            ):
                blocks = parser.parse(request)

        content = "\n".join(block.content for block in blocks)
        self.assertNotIn(running_identifier, content)
        self.assertNotIn("Page 129 / 2026-08-20", content)
        self.assertNotIn("2026-08-20", content)
        self.assertNotIn("Page 129 12 APR-34/78 90", content)
        self.assertIn(body_text, content)
        self.assertIn(numeric_index_entry, content)
        self.assertIn(recovered_text, content)
        surviving_line_text = {str(line.get("text") or "") for line in layout.ocr_lines}
        self.assertNotIn(running_identifier, surviving_line_text)
        self.assertNotIn("Pa", surviving_line_text)
        self.assertNotIn("ge 129 / 20", surviving_line_text)
        self.assertNotIn("26-08-20", surviving_line_text)
        self.assertTrue(surviving_line_text.isdisjoint({"Page 129", "12 APR-34/", "78 90"}))
        block_line_text = {
            str(line.get("text") or "")
            for block in blocks
            for line in block.metadata.get("lines", [])
        }
        self.assertNotIn(running_identifier, block_line_text)
        self.assertTrue(
            block_line_text.isdisjoint(
                {
                    "Pa",
                    "ge 129 / 20",
                    "26-08-20",
                    "Page 129",
                    "12 APR-34/",
                    "78 90",
                }
            )
        )

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

    def test_an_ocr_match_at_a_different_location_is_not_a_duplicate(self) -> None:
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

        # The OCR box is in the garbled-line crop, far from the native line at
        # y=100.  Equal words at different locations are separate source text.
        self.assertEqual((text or "").count(USABLE_ONE), 2)
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
    def test_edge_numeric_cell_inside_a_table_box_is_preserved(self) -> None:
        table_cell = {
            "text": "1234567890---",
            "bbox": (120.0, 12.0, 180.0, 24.0),
            "source_kind": "pdf_native_text",
        }

        kept = _strip_pdf_page_furniture_lines(
            [table_cell],
            page_width=300.0,
            page_height=400.0,
            protected_bboxes=((100.0, 0.0, 200.0, 40.0),),
        )

        self.assertEqual(kept, [table_cell])

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

    def test_recovered_text_repeated_elsewhere_on_the_page_is_not_dropped(self) -> None:
        repeated = "Apply the specified torque."
        native = _page_text_lines(
            _FakePage([_line(repeated, 100.0), _line(GARBLED, 200.0)])
        )
        merged = _merge_native_and_ocr_lines(
            native_lines=native,
            ocr_lines=[
                {
                    "text": repeated,
                    "bbox": (50.0, 200.0, 280.0, 212.0),
                    "source_kind": "pdf_ocr_fallback",
                }
            ],
            replaced=[native[1]],
        )

        self.assertEqual([line["text"] for line in merged].count(repeated), 2)

    def test_an_ocr_line_already_present_at_the_same_location_is_not_repeated(self) -> None:
        repeated = "Apply the specified torque."
        native = _page_text_lines(_FakePage([_line(repeated, 100.0)]))
        merged = _merge_native_and_ocr_lines(
            native_lines=native,
            ocr_lines=[
                {
                    "text": repeated,
                    "bbox": (50.0, 100.0, 280.0, 112.0),
                    "source_kind": "pdf_ocr_fallback",
                }
            ],
            replaced=[],
        )

        self.assertEqual([line["text"] for line in merged].count(repeated), 1)


class MixedPageReadingOrderTests(unittest.TestCase):
    def test_region_ocr_keeps_two_column_reading_order(self) -> None:
        left_top = "The inspection procedure requires a torque wrench."
        left_bottom = "Record the measured torque on the work card."
        right_top = "Remove the retaining ring with the spreader tool."
        right_bottom = "Check the axle and bearing before service."
        page = _FakePage(
            [
                _line(left_top, 100.0, x0=40.0, x1=140.0),
                _line(right_top, 100.0, x0=170.0, x1=280.0),
                _line(GARBLED, 120.0, x0=40.0, x1=140.0),
                _line(right_bottom, 120.0, x0=170.0, x1=280.0),
                _line(left_bottom, 140.0, x0=40.0, x1=140.0),
            ]
        )
        engine = _RecordingEngine(
            [(_box(2.0, 2.0, 180.0, 12.0), "Recovered left-column text.", 0.95)]
        )
        parser = _parser()

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(engine, None, 0.0)):
            text, _reason, error, _timings = parser._maybe_recover_page_with_ocr(
                page,
                [],
                2,
                "\n".join([left_top, right_top, GARBLED, right_bottom, left_bottom]),
            )

        self.assertIsNone(error)
        assert text is not None
        left_end = text.index(left_bottom)
        right_start = text.index(right_top)
        self.assertLess(text.index(left_top), text.index("Recovered left-column text."))
        self.assertLess(text.index("Recovered left-column text."), left_end)
        self.assertLess(left_end, right_start)
        self.assertLess(right_start, text.index(right_bottom))


if __name__ == "__main__":
    unittest.main()
