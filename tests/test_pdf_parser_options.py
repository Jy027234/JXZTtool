from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from parsecore.config import OcrProviderSettings
from parsecore.models import ParseRequest
from parsecore.ocr import OcrRequestError
from parsecore.parsers import ImageOcrParser, PdfTextParser, _ocr_fallback_reason_for_page, build_parser


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


def _fake_page_layout(
    *,
    text_without_tables: str,
    ocr_fallback_reason: str | None,
    tables: list[object] | None = None,
    column_count_hint: int = 1,
    layout_reading_order_applied: bool = False,
    layout_reading_order_strategy: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text_without_tables=text_without_tables,
        tables=tables or [],
        width=100.0,
        height=100.0,
        column_count_hint=column_count_hint,
        layout_reading_order_applied=layout_reading_order_applied,
        layout_reading_order_strategy=layout_reading_order_strategy,
        layout_elapsed_s=0.0,
        ocr_attempt_reason=ocr_fallback_reason,
        ocr_fallback_reason=ocr_fallback_reason,
        ocr_error_reason=None,
        ocr_engine_init_elapsed_s=0.0,
        ocr_render_elapsed_s=0.0,
        ocr_call_elapsed_s=0.0,
        ocr_provider_elapsed_s=0.0,
        ocr_postprocess_elapsed_s=0.0,
        ocr_total_elapsed_s=0.0,
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
