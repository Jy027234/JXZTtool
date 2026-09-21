"""待办 1/3: a region says where it came from in coordinates one can crop with.

The completion criteria ask the reader to be able to keep the original table or
figure image, and the source-location rule asks for the coordinate contract that
makes it possible: which page, which box, in what unit, with which origin and page
rotation, and how big the page was.  A page number and a bare box are not enough --
a renderer cannot tell whether the box is in points or fractions, nor whether the
page was rotated when the box was measured.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from parsecore.models import BlockType, ParseRequest
from parsecore.parsers import PdfTextParser, _pdf_source_region


class _FakePdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, _path: str) -> None:
        self.pages = [_FakePdfPage("Digital text")]


class _FakeTable:
    def __init__(self, *, bbox, cells) -> None:
        self.bbox = bbox
        self.cells = cells
        self.row_count = len(cells)
        self.col_count = max((len(row) for row in cells), default=0)

    def render_text(self) -> str:
        return "\n".join("\t".join(row) for row in self.cells)


class _FakeFigure:
    def __init__(self, *, bbox) -> None:
        self.bbox = bbox
        self.description = "Figure 1 Wheel assembly"
        self.source_kind = "pdf-image"
        self.object_name = "Im1"
        self.caption_confidence = 0.8
        self.figure_kind = "illustration"


def _page_layout(*, rotation: int, width: float = 595.0, height: float = 842.0):
    return SimpleNamespace(
        text_without_tables="Digital text",
        tables=[_FakeTable(bbox=(72.0, 100.0, 523.0, 400.0), cells=[["Part", "Qty"], ["Bolt", "2"]])],
        figure_regions=[_FakeFigure(bbox=(80.0, 500.0, 380.0, 700.0))],
        width=width,
        height=height,
        rotation=rotation,
        column_count_hint=1,
        layout_reading_order_applied=False,
        layout_reading_order_strategy=None,
        layout_reading_order_confidence=1.0,
        layout_elapsed_s=0.0,
        ocr_attempt_reason=None,
        ocr_fallback_reason=None,
        ocr_acceptance_reason=None,
        ocr_rejection_reason=None,
        ocr_error_reason=None,
        ocr_lines=[],
        ocr_engine_init_elapsed_s=0.0,
        ocr_render_elapsed_s=0.0,
        ocr_input_prepare_elapsed_s=0.0,
        ocr_engine_exec_elapsed_s=0.0,
        ocr_call_elapsed_s=0.0,
        ocr_provider_elapsed_s=0.0,
        ocr_provider_det_elapsed_s=0.0,
        ocr_provider_cls_elapsed_s=0.0,
        ocr_provider_rec_elapsed_s=0.0,
        ocr_provider_crop_count=0,
        ocr_provider_cls_rotate_positive_count=0,
        ocr_provider_cls_rotate_high_count=0,
        ocr_postprocess_elapsed_s=0.0,
        ocr_total_elapsed_s=0.0,
        native_text_token_count=0,
        final_text_token_count=0,
    )


def _parse(rotation: int):
    parser = PdfTextParser(
        media_types=["application/pdf"],
        extensions=[".pdf"],
        options={"post_process": {"dual_channel": True, "ocr_bad_pages": False}},
    )
    with TemporaryDirectory(prefix="parsecore-source-region-") as temp_dir:
        pdf_path = Path(temp_dir) / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        request = ParseRequest(
            doc_id="doc-region",
            file_path=str(pdf_path),
            media_type="application/pdf",
        )
        with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
            "parsecore.parsers._extract_pdfplumber_layout",
            return_value=[_page_layout(rotation=rotation)],
        ):
            return list(parser.parse(request))


class SourceRegionContractTests(unittest.TestCase):
    def test_a_table_region_is_croppable(self) -> None:
        blocks = _parse(rotation=0)
        table = next(block for block in blocks if block.type == BlockType.TABLE)
        region = table.metadata["source_region"]

        self.assertEqual(region["kind"], "pdf_table")
        self.assertEqual(region["page_number"], 1)
        self.assertEqual(region["bbox"], (72.0, 100.0, 523.0, 400.0))
        self.assertEqual(region["unit"], "pt")
        self.assertEqual(region["origin"], "top_left")
        self.assertEqual(region["rotation_degrees"], 0)
        self.assertEqual((region["page_width"], region["page_height"]), (595.0, 842.0))
        x0, top, x1, bottom = region["crop_fraction"]
        self.assertAlmostEqual(x0, 72.0 / 595.0, places=5)
        self.assertAlmostEqual(bottom, 400.0 / 842.0, places=5)

    def test_a_rotated_page_says_so(self) -> None:
        blocks = _parse(rotation=90)
        table = next(block for block in blocks if block.type == BlockType.TABLE)
        self.assertEqual(table.metadata["source_region"]["rotation_degrees"], 90)

    def test_a_figure_region_uses_the_same_contract(self) -> None:
        blocks = _parse(rotation=0)
        figure = next(block for block in blocks if block.type == BlockType.IMAGE)
        region = figure.metadata["source_region"]

        self.assertEqual(region["kind"], "pdf_figure")
        self.assertEqual(region["bbox"], (80.0, 500.0, 380.0, 700.0))
        self.assertEqual(region["origin"], "top_left")
        self.assertIn("crop_fraction", region)

    def test_the_helper_refuses_what_it_cannot_describe(self) -> None:
        layout = SimpleNamespace(width=0.0, height=0.0, rotation=45)
        self.assertIsNone(_pdf_source_region(kind="pdf_table", page_number=1, bbox=None, page_layout=layout))
        self.assertIsNone(
            _pdf_source_region(kind="pdf_table", page_number=1, bbox=(1.0, 2.0), page_layout=layout)
        )
        # An unknown rotation is reported as unrotated rather than passed through.
        region = _pdf_source_region(
            kind="pdf_table",
            page_number=2,
            bbox=(0.0, 0.0, 10.0, 10.0),
            page_layout=layout,
        )
        self.assertIsNotNone(region)
        assert region is not None
        self.assertEqual(region["rotation_degrees"], 0)
        self.assertNotIn("crop_fraction", region)


if __name__ == "__main__":
    unittest.main()
