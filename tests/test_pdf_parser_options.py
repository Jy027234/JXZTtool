from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

from parsecore.config import OcrProviderSettings
from parsecore.models import Block, BlockType, ParseRequest
from parsecore.ocr import OcrRequestError
from parsecore.parsers import (
    ImageOcrParser,
    PdfTextParser,
    _OcrLine,
    _OcrStageTimings,
    _extract_pdf_figure_regions,
    _filter_repeated_pdf_figure_regions,
    _layout_reading_order_confidence,
    _ocr_fallback_reason_for_page,
    _ocr_line_records,
    _ocr_line_records_for_page,
    _pdf_page_image_count_hint,
    _select_pdf_layout_pages,
    _select_pdf_layout_work,
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


class _FakeEmptyPdfReader:
    def __init__(self, _path: str) -> None:
        self.pages = [_FakePdfPage("")]


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
    ocr_lines: list[dict[str, object]] | None = None,
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
        ocr_lines=ocr_lines or [],
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
    def test_blank_pdf_page_is_preserved_as_non_indexable_artifact(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": False, "ocr_bad_pages": False}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "blank.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-empty-page",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakeEmptyPdfReader):
                blocks = parser.parse(request)

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1].content, "")
        self.assertEqual(blocks[1].metadata["kind"], "empty_page")
        self.assertEqual(blocks[1].metadata["missing_reason"], "page_without_extractable_content")
        self.assertEqual(blocks[1].metadata["quality_flags"], ["empty_page"])

    def test_adaptive_layout_selector_keeps_layout_and_ocr_pages(self) -> None:
        page_texts = [
            "ordinary native text",
            "Table 1\nA   B\n1   2",
            "ordinary native text",
            "/0 /1 /2 /3 /4 /5",
            "ordinary native text",
            "ordinary native text",
            "ordinary native text",
            "ordinary native text",
        ]
        pages = [SimpleNamespace(images=[]) for _ in page_texts]
        pages[2].images = [object()] * 5

        selected = _select_pdf_layout_pages(
            page_texts,
            pages,
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            max_page_ratio=0.85,
            min_pages=8,
        )

        self.assertEqual(selected, {2, 3, 4})

    def test_adaptive_layout_selector_limits_table_workset_to_strong_signals(self) -> None:
        page_texts = ["ordinary native text"] * 8
        page_texts[1] = "Table 1\nA\nB"
        page_texts[3] = "Removal Procedures Tables of the Harness Components."
        page_texts[5] = "PART NUMBER\n320-366-701-0"
        pages = [SimpleNamespace(images=[]) for _ in page_texts]

        work = _select_pdf_layout_work(
            page_texts,
            pages,
            min_cid_tokens=5,
            min_cid_char_ratio=0.12,
            max_page_ratio=0.85,
            min_pages=8,
        )

        self.assertIsNotNone(work)
        assert work is not None
        self.assertIn(4, work[1])
        self.assertNotIn(2, work[1])
        self.assertNotIn(6, work[1])

    def test_pdf_image_count_hint_reads_xobject_resources_without_materializing_images(self) -> None:
        class _RawPage:
            def __init__(self) -> None:
                self.resources = {"/Resources": {"/XObject": {"/img": {"/Subtype": "/Image"}}}}

            def get(self, key: str):
                return self.resources.get(key)

            @property
            def images(self):
                raise AssertionError("image objects should not be materialized")

        self.assertEqual(_pdf_page_image_count_hint(_RawPage()), 1)

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
        self.assertFalse(parser._parse_cache_enabled)
        self.assertEqual(parser._parse_cache_max_entries, 2)

    def test_parse_cache_reuses_source_across_doc_ids_without_reloading_pdf(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"parse_cache": True, "parse_cache_max_entries": 2}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            copied_pdf_path = Path(temp_dir) / "copied-sample.pdf"
            copied_pdf_path.write_bytes(pdf_path.read_bytes())
            request = ParseRequest(
                doc_id="doc-pdf-cache",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            second_request = ParseRequest(
                doc_id="doc-pdf-cache-2",
                file_path=str(copied_pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader) as mocked_reader:
                first = parser.parse(request)
                second = parser.parse(second_request)
                copied_pdf_path.write_bytes(b"%PDF-1.5\n")
                third = parser.parse(
                    ParseRequest(
                        doc_id="doc-pdf-cache-3",
                        file_path=str(copied_pdf_path),
                        media_type="application/pdf",
                    )
                )

        self.assertEqual(tuple(block.content for block in first), tuple(block.content for block in second))
        self.assertTrue(first[0].metadata["parse_cache_enabled"])
        self.assertFalse(first[0].metadata["parse_cache_hit"])
        self.assertEqual(first[0].metadata["parse_cache_state"], "cold")
        self.assertTrue(second[0].metadata["parse_cache_enabled"])
        self.assertTrue(second[0].metadata["parse_cache_hit"])
        self.assertEqual(second[0].metadata["parse_cache_state"], "warm")
        self.assertTrue(all(block.doc_id == "doc-pdf-cache-2" for block in second))
        self.assertTrue(all(block.block_id.startswith("doc-pdf-cache-2-") for block in second))
        self.assertTrue(all(block.doc_id == "doc-pdf-cache-3" for block in third))
        self.assertFalse(third[0].metadata["parse_cache_hit"])
        self.assertEqual(third[0].metadata["parse_cache_state"], "cold")
        self.assertEqual(mocked_reader.call_count, 2)

    def test_request_parse_cache_override_bypasses_reads_and_writes(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"parse_cache": True, "parse_cache_max_entries": 2}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            first_request = ParseRequest(
                doc_id="doc-pdf-cache-default",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            direct_override = ParseRequest(
                doc_id="doc-pdf-cache-direct-bypass",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"parse_cache": False},
            )
            nested_override = ParseRequest(
                doc_id="doc-pdf-cache-nested-bypass",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"post_process": {"parse_cache": False}},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader) as mocked_reader:
                parser.parse(first_request)
                direct_blocks = parser.parse(direct_override)
                nested_blocks = parser.parse(nested_override)

        self.assertEqual(mocked_reader.call_count, 3)
        for blocks in (direct_blocks, nested_blocks):
            self.assertFalse(blocks[0].metadata["parse_cache_enabled"])
            self.assertFalse(blocks[0].metadata["parse_cache_hit"])
            self.assertEqual(blocks[0].metadata["parse_cache_state"], "disabled")

    def test_parse_cache_rebinds_merged_block_ids(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
        )
        cached = (
            Block(
                block_id="source-p-1",
                doc_id="source",
                type=BlockType.PARAGRAPH,
                content="first",
                metadata={"merged_block_ids": ["source-p-1", "source-p-2"]},
            ),
        )
        rebound = parser._rebind_cached_blocks(
            cached,
            ParseRequest(doc_id="target", file_path="unused.pdf"),
        )
        self.assertEqual(rebound[0].block_id, "target-p-1")
        self.assertEqual(rebound[0].doc_id, "target")
        self.assertEqual(rebound[0].metadata["merged_block_ids"], ["target-p-1", "target-p-2"])

    def test_parse_cache_single_flight_coalesces_concurrent_source_requests(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"parse_cache": True, "parse_cache_max_entries": 2}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            first_request = ParseRequest(
                doc_id="doc-pdf-flight-a",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            second_request = ParseRequest(
                doc_id="doc-pdf-flight-b",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            started = Event()
            release = Event()
            uncached_calls: list[str] = []
            original_uncached = parser._parse_uncached

            def slow_uncached(request: ParseRequest):
                uncached_calls.append(request.doc_id)
                started.set()
                release.wait(timeout=2.0)
                return original_uncached(request)

            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader) as mocked_reader, patch.object(
                parser,
                "_parse_uncached",
                side_effect=slow_uncached,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(parser.parse, first_request)
                    self.assertTrue(started.wait(timeout=1.0))
                    second_future = executor.submit(parser.parse, second_request)
                    release.set()
                    first = first_future.result(timeout=2.0)
                    second = second_future.result(timeout=2.0)

        self.assertEqual(len(uncached_calls), 1)
        self.assertEqual(tuple(block.content for block in first), tuple(block.content for block in second))
        self.assertTrue(all(block.doc_id == "doc-pdf-flight-b" for block in second))
        self.assertTrue(all(block.block_id.startswith("doc-pdf-flight-b-") for block in second))
        mocked_reader.assert_called_once_with()

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
            return [
                _fake_page_layout(
                    text_without_tables="Recovered OCR text",
                    ocr_fallback_reason="cid_ratio",
                    ocr_lines=[
                        {
                            "line_id": "p1:ocr-p1-l1",
                            "line_index": 1,
                            "paragraph_index": 1,
                            "paragraph_line_index": 1,
                            "page_number": 1,
                            "text": "Recovered OCR text",
                            "bbox": (10.0, 20.0, 80.0, 36.0),
                            "page_width": 100.0,
                            "page_height": 100.0,
                            "confidence": 0.93,
                            "source_kind": "pdf_ocr_fallback",
                        }
                    ],
                )
            ]

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
        self.assertEqual(blocks[1].metadata["source_kind"], "pdf_ocr_fallback")
        self.assertEqual(blocks[1].metadata["bbox"], (10.0, 20.0, 80.0, 36.0))
        self.assertEqual(blocks[1].metadata["lines"][0]["text"], "Recovered OCR text")
        self.assertEqual(blocks[1].metadata["lines"][0]["bbox"], (10.0, 20.0, 80.0, 36.0))

    def test_ocr_fallback_without_live_lines_does_not_fabricate_regions(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "ocr_bad_pages": True}},
        )

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-cached-text",
                file_path=str(pdf_path),
                media_type="application/pdf",
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                return_value=[
                    _fake_page_layout(
                        text_without_tables="Recovered cached OCR text",
                        ocr_fallback_reason="native_text_empty",
                    )
                ],
            ):
                blocks = parser.parse(request)

        self.assertEqual(blocks[1].content, "Recovered cached OCR text")
        self.assertEqual(blocks[1].metadata["source_kind"], "pdf_ocr_fallback")
        self.assertNotIn("lines", blocks[1].metadata)
        self.assertNotIn("bbox", blocks[1].metadata)

    def test_ocr_recovery_preserves_line_regions_from_extract_timings(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"ocr_bad_pages": True}},
        )
        extract_timings = _OcrStageTimings(
            ocr_lines=[
                {
                    "line_id": "ocr-p1-l1",
                    "line_index": 1,
                    "paragraph_index": 1,
                    "paragraph_line_index": 1,
                    "text": "Recovered OCR text",
                    "bbox": (10.0, 20.0, 80.0, 36.0),
                    "page_width": 100.0,
                    "page_height": 100.0,
                    "confidence": 0.93,
                    "source_kind": "pdf_ocr_fallback",
                }
            ]
        )

        with patch.object(parser, "_ensure_pdf_ocr_engine", return_value=(object(), None, 0.0)), patch(
            "parsecore.parsers._extract_ocr_text_from_page",
            return_value=("Recovered OCR text", None, extract_timings),
        ):
            text, reason, error, timings = parser._maybe_recover_page_with_ocr(
                SimpleNamespace(width=100.0, height=100.0, images=[object()]),
                [],
                1,
                "",
            )

        self.assertEqual(text, "Recovered OCR text")
        self.assertEqual(reason, "native_text_empty")
        self.assertIsNone(error)
        self.assertEqual(timings.ocr_lines[0]["text"], "Recovered OCR text")
        self.assertEqual(timings.ocr_lines[0]["bbox"], (10.0, 20.0, 80.0, 36.0))

    def test_ocr_line_records_scale_render_coordinates_to_pdf_page(self) -> None:
        records = _ocr_line_records(
            [
                [
                    _OcrLine(
                        bbox=(20.0, 40.0, 160.0, 72.0),
                        text="Recovered OCR text",
                        confidence=0.93,
                    )
                ]
            ],
            rendered_width=200.0,
            rendered_height=200.0,
            page_width=100.0,
            page_height=100.0,
        )

        self.assertEqual(records[0]["bbox"], (10.0, 20.0, 80.0, 36.0))
        self.assertEqual(records[0]["confidence"], 0.93)
        self.assertEqual(records[0]["source_kind"], "pdf_ocr_fallback")
        page_records = _ocr_line_records_for_page(records, page_number=3)
        self.assertEqual(page_records[0]["line_id"], "p3:ocr-p1-l1")
        self.assertEqual(page_records[0]["page_number"], 3)

    def test_request_ocr_cache_override_disables_layout_cache(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={
                "post_process": {
                    "dual_channel": True,
                    "ocr_bad_pages": False,
                    "ocr_cache": True,
                    "adaptive_ocr_cache_fast_path": True,
                }
            },
        )
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["ocr_cache_enabled"] = kwargs["ocr_cache"].enabled
            captured["ocr_cache_fast_path"] = kwargs["ocr_cache_fast_path"]
            return [_fake_page_layout(text_without_tables="Native text path", ocr_fallback_reason=None)]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-cache-bypass",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"post_process": {"ocr_cache": False}},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                parser.parse(request)

        self.assertFalse(captured["ocr_cache_enabled"])
        self.assertFalse(captured["ocr_cache_fast_path"])

    def test_ocr_cache_config_opt_out_and_request_reenable_are_explicit(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "ocr_cache": False}},
        )
        self.assertFalse(parser._ocr_cache.enabled)
        captured: dict[str, object] = {}

        def fake_extract_pdfplumber_layout(*_args, **kwargs):
            captured["ocr_cache_enabled"] = kwargs["ocr_cache"].enabled
            return [_fake_page_layout(text_without_tables="Native text path", ocr_fallback_reason=None)]

        with TemporaryDirectory(prefix="parsecore-pdf-options-") as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            request = ParseRequest(
                doc_id="doc-pdf-ocr-cache-reenable",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"post_process": {"ocr_cache": True}},
            )
            with patch("parsecore.parsers._load_pdf_reader", return_value=_FakePdfReader), patch(
                "parsecore.parsers._extract_pdfplumber_layout",
                side_effect=fake_extract_pdfplumber_layout,
            ):
                parser.parse(request)

        self.assertTrue(captured["ocr_cache_enabled"])

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
                SimpleNamespace(images=[object()]),
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
        self.assertEqual(blocks[1].metadata["ocr_attempt_reason"], "native_text_empty")
        self.assertEqual(blocks[1].metadata["ocr_error_reason"], "provider_request_failed")
        self.assertNotIn("ocr_fallback_used", blocks[1].metadata)

    def test_empty_image_page_is_ocr_candidate(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"dual_channel": True, "ocr_bad_pages": True}},
        )
        timings = SimpleNamespace(
            render_elapsed_s=0.01,
            call_elapsed_s=0.02,
            provider_elapsed_s=0.02,
            postprocess_elapsed_s=0.001,
        )
        with patch(
            "parsecore.parsers.PdfTextParser._ensure_pdf_ocr_engine",
            return_value=(object(), None, 0.0),
        ), patch(
            "parsecore.parsers._extract_ocr_text_from_page",
            return_value=("Recovered scan text", None, timings),
        ):
            recovered, reason, error, _ = parser._maybe_recover_page_with_ocr(
                SimpleNamespace(images=[object()]),
                [],
                1,
                "",
            )

        self.assertEqual(recovered, "Recovered scan text")
        self.assertEqual(reason, "native_text_empty")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
