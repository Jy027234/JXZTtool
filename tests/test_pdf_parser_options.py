from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from parsecore.config import OcrProviderSettings
from parsecore.models import ParseRequest
from parsecore.ocr import OcrRequestError
from parsecore.parsers import (
    ImageOcrParser,
    PdfTextParser,
    _extract_pdf_figure_regions,
    _filter_repeated_pdf_figure_regions,
    _layout_reading_order_confidence,
    _ocr_fallback_reason_for_page,
    build_parser,
)


class _FakePdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, _path: str) -> None:
        self.pages = [_FakePdfPage("broken text")]


class _FakeTable:
    def __init__(self, *, bbox: tuple[float, float, float, float], cells: list[list[str]]) -> None:
        self.bbox = bbox
        self.cells = cells
        self.row_count = len(cells)
        self.col_count = max((len(row) for row in cells), default=0)

    def render_text(self) -> str:
        return "\n".join("\t".join(cell for cell in row) for row in self.cells)


class _FakePdfImagePage:
    def __init__(
        self,
        *,
        width: float,
        height: float,
        images: list[dict[str, object]],
        words: list[dict[str, object]] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.images = images
        self._words = words or []

    def extract_words(self) -> list[dict[str, object]]:
        return list(self._words)


def _fake_page_layout(
    *,
    text_without_tables: str,
    ocr_fallback_reason: str | None,
    tables: list[object] | None = None,
    figure_regions: list[object] | None = None,
    column_count_hint: int = 1,
    layout_reading_order_applied: bool = False,
    layout_reading_order_strategy: str | None = None,
    layout_reading_order_confidence: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text_without_tables=text_without_tables,
        tables=tables or [],
        figure_regions=figure_regions or [],
        width=100.0,
        height=100.0,
        column_count_hint=column_count_hint,
        layout_reading_order_applied=layout_reading_order_applied,
        layout_reading_order_strategy=layout_reading_order_strategy,
        layout_reading_order_confidence=layout_reading_order_confidence,
        layout_elapsed_s=0.0,
        ocr_attempt_reason=ocr_fallback_reason,
        ocr_fallback_reason=ocr_fallback_reason,
        ocr_acceptance_reason=None,
        ocr_rejection_reason=None,
        ocr_error_reason=None,
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


class PdfTextParserOptionsTests(unittest.TestCase):
    def test_ocr_fallback_reason_supports_pdf_name_dense_tokens(self) -> None:
        text = " /0 /1 /2 /3 /i255 /i128 /9 /8 " * 6
        reason = _ocr_fallback_reason_for_page(
            text,
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
        )
        self.assertEqual(reason, "pdf_name_dense")

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

    def test_image_ocr_parser_uses_shared_ocr_provider_builder(self) -> None:
        provider_settings = OcrProviderSettings(
            enabled=True,
            provider="rapidocr",
            options={"det_use_dilation": True},
        )
        parser = ImageOcrParser(
            media_types=["image/png"],
            extensions=[".png"],
            ocr_provider_settings=provider_settings,
        )
        engine = object()

        with patch("parsecore.parsers.build_ocr_engine", return_value=engine) as mocked:
            self.assertIs(parser._ensure_engine(), engine)
            self.assertIs(parser._ensure_engine(), engine)

        mocked.assert_called_once_with(provider_settings)

    def test_request_enable_ocr_turns_on_dual_channel_and_ocr_callback(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": False, "ocr_bad_pages": False}},
        )
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["ocr_page_text_fn"] = kwargs.get("ocr_page_text_fn")
            return [_fake_page_layout(text_without_tables="Recovered OCR text", ocr_fallback_reason="cid_ratio")]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-on",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"enable_ocr": True},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertIsNotNone(captured.get("ocr_page_text_fn"))
        self.assertEqual(blocks[1].content, "Recovered OCR text")
        self.assertTrue(blocks[1].metadata["ocr_fallback_used"])

    def test_layout_reading_order_flag_is_forwarded_to_layout_extractor(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "layout_reading_order": False}},
        )
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["layout_reading_order_enabled"] = kwargs.get("layout_reading_order_enabled")
            return [_fake_page_layout(text_without_tables="Native text path", ocr_fallback_reason=None)]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-layout-off",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"post_process": {"layout_reading_order": True}},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                parser.parse(request)

        self.assertTrue(captured["layout_reading_order_enabled"])

    def test_large_pdf_catalog_profile_uses_fast_text_path_by_default(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "layout_reading_order": True, "ocr_bad_pages": True}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-catalog-fast",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"profile": "large-pdf-catalog"},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=AssertionError("layout extractor should be skipped"),
            ):
                blocks = parser.parse(request)

        self.assertEqual(blocks[1].content, "broken text")
        self.assertTrue(blocks[1].metadata["profile_fast_text_path"])
        self.assertNotIn("layout_source", blocks[1].metadata)
        self.assertEqual(blocks[0].metadata["ocr_strategy"], "off")

    def test_large_pdf_catalog_profile_allows_explicit_layout_opt_in(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": False, "layout_reading_order": False, "ocr_bad_pages": False}},
        )
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["layout_reading_order_enabled"] = kwargs.get("layout_reading_order_enabled")
            return [_fake_page_layout(text_without_tables="Layout text", ocr_fallback_reason=None)]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-catalog-layout",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={
                    "profile": "large-pdf-catalog",
                    "post_process": {"dual_channel": True, "layout_reading_order": True},
                },
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertTrue(captured["layout_reading_order_enabled"])
        self.assertEqual(blocks[1].content, "Layout text")

    def test_layout_reading_order_metadata_is_exposed_on_blocks(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "layout_reading_order": True}},
        )

        def fake_extract_pdfplumber_layout(*_args, **_kwargs):
            return [
                _fake_page_layout(
                    text_without_tables="Column one\n\nColumn two",
                    ocr_fallback_reason=None,
                    column_count_hint=2,
                    layout_reading_order_applied=True,
                    layout_reading_order_strategy="column-reflow",
                    layout_reading_order_confidence=0.9,
                )
            ]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-layout-meta",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertTrue(blocks[1].metadata["layout_reading_order_applied"])
        self.assertEqual(blocks[1].metadata["layout_reading_order_strategy"], "column-reflow")
        self.assertEqual(blocks[1].metadata["column_count_hint"], 2)
        self.assertEqual(blocks[1].metadata["layout_reading_order_confidence"], 0.9)

    def test_layout_reading_order_confidence_heuristic_tracks_column_reflow(self) -> None:
        self.assertEqual(
            _layout_reading_order_confidence(
                text_without_tables="Single column body text",
                column_count_hint=1,
                layout_reading_order_applied=False,
                layout_reading_order_strategy=None,
                table_count=0,
                figure_count=0,
            ),
            0.98,
        )
        self.assertEqual(
            _layout_reading_order_confidence(
                text_without_tables="Column one\n\nColumn two",
                column_count_hint=2,
                layout_reading_order_applied=True,
                layout_reading_order_strategy="column-reflow",
                table_count=0,
                figure_count=0,
            ),
            0.9,
        )
        self.assertEqual(
            _layout_reading_order_confidence(
                text_without_tables="Column one\n\nColumn two",
                column_count_hint=2,
                layout_reading_order_applied=False,
                layout_reading_order_strategy=None,
                table_count=0,
                figure_count=0,
            ),
            0.58,
        )

    def test_tables_are_interleaved_with_paragraphs_by_vertical_anchor(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "layout_reading_order": True}},
        )

        def fake_extract_pdfplumber_layout(*_args, **_kwargs):
            return [
                _fake_page_layout(
                    text_without_tables="Intro paragraph\n\nClosing paragraph",
                    ocr_fallback_reason=None,
                    tables=[
                        _FakeTable(
                            bbox=(8.0, 45.0, 92.0, 68.0),
                            cells=[["Part", "Qty"], ["Bolt", "2"]],
                        )
                    ],
                    column_count_hint=1,
                    layout_reading_order_applied=True,
                    layout_reading_order_strategy="column-reflow",
                )
            ]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-mixed-order",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertEqual(blocks[1].type.value, "paragraph")
        self.assertEqual(blocks[1].content, "Intro paragraph")
        self.assertEqual(blocks[2].type.value, "table")
        self.assertEqual(blocks[2].metadata["table_index"], 1)
        self.assertEqual(blocks[3].type.value, "paragraph")
        self.assertEqual(blocks[3].content, "Closing paragraph")

    def test_figures_are_emitted_as_image_blocks_by_vertical_anchor(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "layout_reading_order": True}},
        )

        def fake_extract_pdfplumber_layout(*_args, **_kwargs):
            return [
                _fake_page_layout(
                    text_without_tables="Intro paragraph\n\nClosing paragraph",
                    ocr_fallback_reason=None,
                    figure_regions=[
                        SimpleNamespace(
                            bbox=(12.0, 38.0, 88.0, 72.0),
                            description="Figure 2-1. Flight deck layout",
                            source_kind="pdf-image",
                            object_name="Image001",
                            caption_confidence=0.95,
                            figure_kind="diagram",
                        )
                    ],
                    column_count_hint=1,
                    layout_reading_order_applied=True,
                    layout_reading_order_strategy="column-reflow",
                )
            ]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-figure-order",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertEqual(blocks[1].type.value, "paragraph")
        self.assertEqual(blocks[2].type.value, "image")
        self.assertEqual(blocks[2].content, "Figure 2-1. Flight deck layout")
        self.assertEqual(blocks[2].metadata["semantic_role"], "image")
        self.assertEqual(blocks[2].metadata["bbox"], (12.0, 38.0, 88.0, 72.0))
        self.assertEqual(blocks[2].metadata["source_kind"], "pdf-image")
        self.assertEqual(blocks[2].metadata["caption_confidence"], 0.95)
        self.assertEqual(blocks[2].metadata["figure_kind"], "diagram")
        self.assertEqual(blocks[3].type.value, "paragraph")

    def test_pdf_image_extraction_filters_margin_noise(self) -> None:
        page = _FakePdfImagePage(
            width=600.0,
            height=800.0,
            images=[
                {"x0": 50.0, "x1": 150.0, "top": -8.0, "bottom": 36.0, "name": "LogoTop"},
                {"x0": 460.0, "x1": 560.0, "top": 24.0, "bottom": 72.0, "name": "LogoHeader"},
                {"x0": 50.0, "x1": 150.0, "top": 735.0, "bottom": 780.0, "name": "LogoFooter"},
            ],
        )

        regions = _extract_pdf_figure_regions(page, page_number=3, table_bboxes=())

        self.assertEqual(regions, [])

    def test_pdf_image_extraction_keeps_captioned_structure_figures(self) -> None:
        page = _FakePdfImagePage(
            width=600.0,
            height=800.0,
            images=[
                {"x0": 80.0, "x1": 520.0, "top": 48.0, "bottom": 190.0, "name": "ImageStruct"},
            ],
            words=[
                {
                    "text": "图 2-1 质量体系结构图",
                    "x0": 170.0,
                    "x1": 430.0,
                    "top": 198.0,
                    "bottom": 214.0,
                },
            ],
        )

        regions = _extract_pdf_figure_regions(page, page_number=2, table_bboxes=())

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].description, "图 2-1 质量体系结构图")
        self.assertEqual(regions[0].figure_kind, "structure")
        self.assertGreaterEqual(regions[0].caption_confidence, 0.8)

    def test_pdf_image_extraction_keeps_captioned_full_page_figures(self) -> None:
        page = _FakePdfImagePage(
            width=600.0,
            height=800.0,
            images=[
                {"x0": 12.0, "x1": 588.0, "top": 16.0, "bottom": 720.0, "name": "FullPageFlow"},
            ],
            words=[
                {
                    "text": "Figure 4-2. Maintenance workflow diagram",
                    "x0": 120.0,
                    "x1": 480.0,
                    "top": 732.0,
                    "bottom": 748.0,
                },
            ],
        )

        regions = _extract_pdf_figure_regions(page, page_number=4, table_bboxes=())

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].description, "Figure 4-2. Maintenance workflow diagram")
        self.assertEqual(regions[0].figure_kind, "flowchart")
        self.assertGreaterEqual(regions[0].caption_confidence, 0.8)

    def test_pdf_image_extraction_keeps_captioned_footer_workflow_figures(self) -> None:
        page = _FakePdfImagePage(
            width=600.0,
            height=800.0,
            images=[
                {"x0": 170.0, "x1": 430.0, "top": 642.0, "bottom": 702.0, "name": "FooterWorkflow"},
            ],
            words=[
                {
                    "text": "Maintenance workflow - approval process",
                    "x0": 155.0,
                    "x1": 445.0,
                    "top": 612.0,
                    "bottom": 626.0,
                },
            ],
        )

        regions = _extract_pdf_figure_regions(page, page_number=7, table_bboxes=())

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].description, "Maintenance workflow - approval process")
        self.assertEqual(regions[0].figure_kind, "flowchart")
        self.assertGreaterEqual(regions[0].caption_confidence, 0.8)

    def test_repeated_generic_small_images_are_filtered_but_captioned_figures_remain(self) -> None:
        repeated_bbox = (250.0, 300.0, 300.0, 350.0)
        layouts = [
            SimpleNamespace(
                width=600.0,
                height=800.0,
                figure_regions=[
                    SimpleNamespace(
                        bbox=repeated_bbox,
                        description=f"第 {index} 页图示区域",
                        caption_confidence=0.15,
                        figure_kind="generic",
                    )
                ],
            )
            for index in range(1, 4)
        ]
        layouts.append(
            SimpleNamespace(
                width=600.0,
                height=800.0,
                figure_regions=[
                    SimpleNamespace(
                        bbox=repeated_bbox,
                        description="Figure 4-1. Fuel flow diagram",
                        caption_confidence=0.9,
                        figure_kind="diagram",
                    )
                ],
            )
        )

        _filter_repeated_pdf_figure_regions(layouts)

        self.assertTrue(all(not layout.figure_regions for layout in layouts[:3]))
        self.assertEqual(len(layouts[3].figure_regions), 1)
        self.assertEqual(layouts[3].figure_regions[0].description, "Figure 4-1. Fuel flow diagram")

    def test_request_enable_ocr_false_disables_ocr_callback_even_if_config_default_on(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "ocr_bad_pages": True}},
        )
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["ocr_page_text_fn"] = kwargs.get("ocr_page_text_fn")
            return [_fake_page_layout(text_without_tables="Native text path", ocr_fallback_reason=None)]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-off",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"enable_ocr": False},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                blocks = parser.parse(request)

        self.assertIsNone(captured.get("ocr_page_text_fn"))
        self.assertEqual(blocks[1].content, "Native text path")

    def test_failed_ocr_attempt_surfaces_attempt_and_error_metadata(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": False, "ocr_bad_pages": False}},
        )

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            recovered_text, attempt_reason, error_reason, _timings = kwargs["ocr_page_text_fn"](
                SimpleNamespace(),
                [],
                1,
                "",
            )
            self.assertIsNone(recovered_text)
            return [
                SimpleNamespace(
                    text_without_tables="broken native text",
                    tables=[],
                    width=100.0,
                    height=100.0,
                    column_count_hint=1,
                    layout_reading_order_applied=False,
                    layout_reading_order_strategy=None,
                    layout_elapsed_s=0.0,
                    ocr_attempt_reason=attempt_reason,
                    ocr_fallback_reason=attempt_reason if recovered_text else None,
                    ocr_error_reason=error_reason,
                    ocr_engine_init_elapsed_s=0.0,
                    ocr_render_elapsed_s=0.0,
                    ocr_call_elapsed_s=0.0,
                    ocr_provider_elapsed_s=0.0,
                    ocr_postprocess_elapsed_s=0.0,
                    ocr_total_elapsed_s=0.0,
                )
            ]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-failed",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"enable_ocr": True},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ), patch(
                "parsecore.parsers.PdfTextParser._ensure_pdf_ocr_engine",
                return_value=(object(), None, 0.0),
            ), patch(
                "parsecore.parsers._extract_ocr_text_from_page",
                return_value=(
                    None,
                    "provider_request_failed",
                    SimpleNamespace(
                        render_elapsed_s=0.0,
                        call_elapsed_s=0.0,
                        provider_elapsed_s=0.0,
                        postprocess_elapsed_s=0.0,
                    ),
                ),
            ):
                blocks = parser.parse(request)

        self.assertEqual(blocks[1].content, "broken native text")
        self.assertTrue(blocks[1].metadata["ocr_attempted"])
        self.assertEqual(blocks[1].metadata["ocr_attempt_reason"], "empty_text")
        self.assertEqual(blocks[1].metadata["ocr_error_reason"], "provider_request_failed")
        self.assertNotIn("ocr_fallback_used", blocks[1].metadata)


if __name__ == "__main__":
    unittest.main()
