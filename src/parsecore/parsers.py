from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
import xml.etree.ElementTree as ET
import zipfile

from .config import OcrProviderSettings
from .contracts import ParserAdapter
from .models import Block, BlockType, ParseRequest, SemanticRole
from .ocr import OcrConfigurationError, OcrRequestError, build_ocr_engine
from .stubs import StubParser


_DEFAULT_OCR_PROVIDER_SETTINGS = OcrProviderSettings(
    enabled=True,
    provider="rapidocr",
)


def _classify_ocr_error(exc: Exception) -> str:
    if isinstance(exc, OcrConfigurationError):
        return "provider_configuration_error"
    if isinstance(exc, OcrRequestError):
        return "provider_request_failed"
    if isinstance(exc, RuntimeError):
        return "provider_unavailable"
    return "provider_execution_failed"


class DocxParser(ParserAdapter):
    name = "docx-native"

    def __init__(self, *, media_types: Sequence[str], extensions: Sequence[str]) -> None:
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        document_path = Path(request.file_path)
        with zipfile.ZipFile(document_path) as archive:
            document_xml = archive.read("word/document.xml")

        root = ET.fromstring(document_xml)
        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        blocks: list[Block] = [
            Block(
                block_id=f"{request.doc_id}-title",
                doc_id=request.doc_id,
                type=BlockType.TITLE,
                content=document_path.stem,
                metadata={
                    "page": 1,
                    "page_type": "body",
                    "parser": self.name,
                    "semantic_role": SemanticRole.TITLE.value,
                },
            )
        ]
        position = 1
        for paragraph in root.findall(".//w:p", namespaces):
            texts = [node.text for node in paragraph.findall(".//w:t", namespaces) if node.text]
            content = "".join(texts).strip()
            if not content:
                continue
            blocks.append(
                Block(
                    block_id=f"{request.doc_id}-p-{position}",
                    doc_id=request.doc_id,
                    type=BlockType.PARAGRAPH,
                    content=content,
                    metadata={
                        "page": 1,
                        "page_type": "body",
                        "parser": self.name,
                        "position": position,
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                    },
                )
            )
            position += 1
        return tuple(blocks)


class TextParser(ParserAdapter):
    name = "text-native"

    def __init__(self, *, media_types: Sequence[str], extensions: Sequence[str]) -> None:
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        text = Path(request.file_path).read_text(encoding="utf-8")
        paragraphs = [item.strip() for item in text.split("\n\n") if item.strip()]
        blocks: list[Block] = [
            Block(
                block_id=f"{request.doc_id}-title",
                doc_id=request.doc_id,
                type=BlockType.TITLE,
                content=Path(request.file_path).stem,
                metadata={
                    "page": 1,
                    "page_type": "body",
                    "parser": self.name,
                    "semantic_role": SemanticRole.TITLE.value,
                },
            )
        ]
        for position, paragraph in enumerate(paragraphs, start=1):
            blocks.append(
                Block(
                    block_id=f"{request.doc_id}-p-{position}",
                    doc_id=request.doc_id,
                    type=BlockType.PARAGRAPH,
                    content=paragraph,
                    metadata={
                        "page": 1,
                        "page_type": "body",
                        "parser": self.name,
                        "position": position,
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                    },
                )
            )
        return tuple(blocks)


class PdfTextParser(ParserAdapter):
    name = "pdf-text"

    def __init__(
        self,
        *,
        media_types: Sequence[str],
        extensions: Sequence[str],
        options: Mapping[str, Any] | None = None,
        ocr_provider_settings: OcrProviderSettings | None = None,
        boundary_refiner: Any = None,
    ) -> None:
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}
        post_process = {}
        if options:
            raw = options.get("post_process")
            if isinstance(raw, Mapping):
                post_process = dict(raw)
        # A3+.1 (opt-in, default False): strip repeated header/footer lines
        # detected via cross-page text similarity. Off by default so the full
        # rendered page content remains visible to downstream consumers; flip
        # ``strip_headers_footers = true`` in parsecore.toml to enable.
        self._strip_hf_enabled = bool(post_process.get("strip_headers_footers", False))
        self._merge_short_enabled = bool(post_process.get("merge_short_blocks", True))
        self._short_block_min_length = int(post_process.get("short_block_min_length", 10))
        self._hf_threshold = float(post_process.get("hf_threshold", 0.5))
        self._hf_head_n = int(post_process.get("hf_head_n", 3))
        self._hf_tail_n = int(post_process.get("hf_tail_n", 3))
        self._hf_min_line_len = int(post_process.get("hf_min_line_len", 4))
        self._split_structural_enabled = bool(
            post_process.get("split_structural_items", True)
        )
        self._structural_min_lines = int(
            post_process.get("structural_min_lines_trigger", 10)
        )
        self._split_inline_structural_enabled = bool(
            post_process.get("split_inline_structural_items", True)
        )
        self._inline_structural_min_length = int(
            post_process.get("inline_structural_min_length_trigger", 120)
        )
        self._split_toc_enabled = bool(
            post_process.get("split_toc_entries", True)
        )
        self._toc_min_entries = int(
            post_process.get("toc_min_entries_trigger", 3)
        )
        self._merge_table_enabled = bool(
            post_process.get("merge_table_continuations", True)
        )
        self._merge_highlights_enabled = bool(
            post_process.get("merge_highlights_entries", True)
        )
        # A3 dual-channel: use pdfplumber for layout-aware tables + text.
        # Default off so existing pypdf-only behaviour is preserved.
        self._dual_channel_enabled = bool(
            post_process.get("dual_channel", False)
        )
        self._dual_table_min_rows = int(
            post_process.get("dual_table_min_rows", 2)
        )
        self._dual_table_min_cols = int(
            post_process.get("dual_table_min_cols", 2)
        )
        # A6: bad-page OCR fallback. Off by default at the library level;
        # enabled in repo config so only obviously broken PDF pages fall back
        # to OCR while healthy digital text pages stay on the native path.
        self._ocr_bad_pages_enabled = bool(post_process.get("ocr_bad_pages", False))
        self._ocr_bad_page_min_cid_tokens = int(
            post_process.get("ocr_bad_page_min_cid_tokens", 5)
        )
        self._ocr_bad_page_min_cid_char_ratio = float(
            post_process.get("ocr_bad_page_min_cid_char_ratio", 0.12)
        )
        self._ocr_render_resolution = int(post_process.get("ocr_render_resolution", 144))
        self._ocr_confidence_threshold = float(
            post_process.get("ocr_confidence_threshold", 0.5)
        )
        self._ocr_merge_line_gap_ratio = float(
            post_process.get("ocr_merge_line_gap_ratio", 1.6)
        )
        self._ocr_provider_settings = (
            ocr_provider_settings or _DEFAULT_OCR_PROVIDER_SETTINGS
        )
        self._ocr_parser: ImageOcrParser | None = None
        # A4 LLM hookup: optional boundary refiner invoked on low-confidence
        # paragraphs only. None == feature disabled (default).
        self._boundary_refiner = boundary_refiner
        self._llm_min_length = int(
            post_process.get("llm_refine_min_length", 600)
        )
        self._llm_min_markers = int(
            post_process.get("llm_refine_min_markers", 2)
        )

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def _ensure_pdf_ocr_engine(self) -> tuple[Any | None, str | None, float]:
        started = time.monotonic()
        if self._ocr_parser is None:
            self._ocr_parser = ImageOcrParser(
                media_types=["image/png", "image/jpeg"],
                extensions=[".png", ".jpg", ".jpeg"],
                options={"confidence_threshold": self._ocr_confidence_threshold},
                ocr_provider_settings=self._ocr_provider_settings,
            )
        try:
            return self._ocr_parser._ensure_engine(), None, round(time.monotonic() - started, 6)
        except Exception as exc:
            return None, _classify_ocr_error(exc), round(time.monotonic() - started, 6)

    def _maybe_recover_page_with_ocr(
        self,
        page: Any,
        table_bboxes: Sequence[tuple[float, float, float, float]],
        column_count_hint: int,
        extracted_text: str | None,
    ) -> tuple[str | None, str | None, str | None, _OcrStageTimings]:
        timings = _OcrStageTimings()
        reason = _ocr_fallback_reason_for_page(
            extracted_text or "",
            min_cid_tokens=self._ocr_bad_page_min_cid_tokens,
            min_cid_char_ratio=self._ocr_bad_page_min_cid_char_ratio,
        )
        if reason is None:
            return None, None, None, timings
        attempt_started = time.monotonic()
        engine, engine_error_reason, timings.engine_init_elapsed_s = self._ensure_pdf_ocr_engine()
        if engine is None:
            timings.total_elapsed_s = round(time.monotonic() - attempt_started, 6)
            return None, reason, engine_error_reason or "provider_unavailable", timings
        text, ocr_error_reason, extract_timings = _extract_ocr_text_from_page(
            page,
            engine=engine,
            table_bboxes=table_bboxes,
            resolution=self._ocr_render_resolution,
            confidence_threshold=self._ocr_confidence_threshold,
            column_count_hint=column_count_hint,
            merge_line_gap_ratio=self._ocr_merge_line_gap_ratio,
        )
        timings.render_elapsed_s = extract_timings.render_elapsed_s
        timings.call_elapsed_s = extract_timings.call_elapsed_s
        timings.provider_elapsed_s = extract_timings.provider_elapsed_s
        timings.postprocess_elapsed_s = extract_timings.postprocess_elapsed_s
        timings.total_elapsed_s = round(time.monotonic() - attempt_started, 6)
        if not text:
            return None, reason, ocr_error_reason or "empty_ocr_text", timings
        return text, reason, None, timings

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        PdfReader = _load_pdf_reader()
        request_enable_ocr = _resolve_request_enable_ocr(request)
        effective_ocr_bad_pages_enabled = (
            self._ocr_bad_pages_enabled
            if request_enable_ocr is None
            else request_enable_ocr
        )
        effective_dual_channel_enabled = (
            self._dual_channel_enabled or effective_ocr_bad_pages_enabled
        )

        if self._boundary_refiner is not None:
            reset = getattr(self._boundary_refiner, "reset", None)
            if callable(reset):
                reset()

        reader = PdfReader(request.file_path)
        page_texts = [page.extract_text() or "" for page in reader.pages]
        layout_pages: list[_PageLayout] = []
        if effective_dual_channel_enabled:
            ocr_page_text_fn = self._maybe_recover_page_with_ocr if effective_ocr_bad_pages_enabled else None
            layout_pages = _extract_pdfplumber_layout(
                request.file_path,
                min_rows=self._dual_table_min_rows,
                min_cols=self._dual_table_min_cols,
                ocr_page_text_fn=ocr_page_text_fn,
            )
            # Replace per-page text with the table-stripped pdfplumber text so
            # the existing splitters do not see table contents twice.
            for index, layout in enumerate(layout_pages):
                if index < len(page_texts) and layout.text_without_tables is not None:
                    page_texts[index] = layout.text_without_tables
        if self._strip_hf_enabled:
            cleaned_page_texts = _strip_repeated_headers_footers(
                page_texts,
                threshold=self._hf_threshold,
                head_n=self._hf_head_n,
                tail_n=self._hf_tail_n,
                min_line_len=self._hf_min_line_len,
            )
            stripped_page_numbers: set[int] = set()
            for index, (original, cleaned) in enumerate(zip(page_texts, cleaned_page_texts), start=1):
                if original.strip() and original.strip() != cleaned.strip():
                    stripped_page_numbers.add(index)
        else:
            cleaned_page_texts = list(page_texts)
            stripped_page_numbers = set()

        blocks: list[Block] = [
            Block(
                block_id=f"{request.doc_id}-title",
                doc_id=request.doc_id,
                type=BlockType.TITLE,
                content=Path(request.file_path).stem,
                metadata={
                    "page": 1,
                    "page_type": "body",
                    "parser": self.name,
                    "semantic_role": SemanticRole.TITLE.value,
                },
            )
        ]
        position = 1
        for page_number, page_text in enumerate(cleaned_page_texts, start=1):
            page_layout = (
                layout_pages[page_number - 1]
                if effective_dual_channel_enabled and page_number - 1 < len(layout_pages)
                else None
            )
            paragraphs = _split_pdf_page_text(page_text)
            if self._split_structural_enabled:
                paragraphs = _split_structural_items(
                    paragraphs, min_lines_trigger=self._structural_min_lines
                )
            if self._split_inline_structural_enabled:
                paragraphs = _split_inline_structural_items(
                    paragraphs,
                    min_length_trigger=self._inline_structural_min_length,
                )
            if self._split_toc_enabled:
                paragraphs = _split_toc_entries(
                    paragraphs, min_entries_trigger=self._toc_min_entries
                )
            if self._merge_short_enabled:
                paragraphs = _merge_short_blocks(
                    paragraphs, min_length=self._short_block_min_length
                )
            if self._merge_table_enabled:
                paragraphs = _merge_table_continuations(paragraphs)
            if self._merge_highlights_enabled:
                paragraphs = _merge_highlights_entries(paragraphs)
            if self._boundary_refiner is not None:
                paragraphs = _refine_low_confidence_paragraphs(
                    paragraphs,
                    refiner=self._boundary_refiner,
                    min_length=self._llm_min_length,
                    min_markers=self._llm_min_markers,
                )
            is_highlights_page = _is_highlights_page(paragraphs)
            paragraph_roles = [
                _infer_semantic_role(paragraph, is_highlights_page=is_highlights_page)
                for paragraph in paragraphs
            ]
            page_type = _infer_page_type(
                page_number=page_number,
                roles=paragraph_roles,
                full_text="\n\n".join(paragraphs),
                has_title=(page_number == 1),
            )
            if page_layout is not None:
                for table_index, table in enumerate(page_layout.tables, start=1):
                    blocks.append(
                        Block(
                            block_id=f"{request.doc_id}-t-{position}",
                            doc_id=request.doc_id,
                            type=BlockType.TABLE,
                            content=table.render_text(),
                            metadata={
                                "page": page_number,
                                "page_type": page_type,
                                "parser": self.name,
                                "position": position,
                                "kind": "table",
                                "semantic_role": SemanticRole.TABLE.value,
                                "rows": table.row_count,
                                "cols": table.col_count,
                                "bbox": table.bbox,
                                "cells": table.cells,
                                "table_index": table_index,
                            },
                        )
                    )
                    _attach_page_layout_metadata(blocks[-1].metadata, page_layout)
                    position += 1
            if not paragraphs:
                continue
            for page_position, (paragraph, semantic_role) in enumerate(zip(paragraphs, paragraph_roles), start=1):
                metadata: dict[str, Any] = {
                    "page": page_number,
                    "page_type": page_type,
                    "parser": self.name,
                    "position": position,
                    "page_position": page_position,
                    "semantic_role": semantic_role,
                }
                if page_layout is not None:
                    _attach_page_layout_metadata(metadata, page_layout)
                if page_number in stripped_page_numbers:
                    metadata["header_footer_stripped"] = True
                blocks.append(
                    Block(
                        block_id=f"{request.doc_id}-p-{position}",
                        doc_id=request.doc_id,
                        type=BlockType.PARAGRAPH,
                        content=paragraph,
                        metadata=metadata,
                    )
                )
                position += 1
        if len(blocks) == 1:
            raise RuntimeError("No extractable text found in PDF")
        return tuple(blocks)


def _split_pdf_page_text(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    current_lines: list[str] = []
    for raw_line in normalized.split("\n"):
        if not raw_line.strip():
            if current_lines:
                paragraphs.append("\n".join(current_lines))
                current_lines = []
            continue
        current_lines.append(raw_line)
    if current_lines:
        paragraphs.append("\n".join(current_lines))
    return paragraphs


_LEP_ENTRY_PATTERN = re.compile(r"(?:LIST OF EFFECTIVE PAGES|\bPage\s+[A-Z0-9.\-/]+)", re.IGNORECASE)


_HEADING_LINE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*[.、)]|第[\d一二三四五六七八九十百]+[章节条款])\s"
)

_STRUCTURAL_ITEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\([a-zA-Z]\)\s"),
    re.compile(r"^\s*\(\d+\)\s"),
    re.compile(
        r"^\s*(?:NOTE|WARNING|CAUTION|Note|Warning|Caution|注意|警告|小心)\s*[:：]"
    ),
)

_INLINE_STRUCTURAL_MARKER_PATTERN = re.compile(
    r"(?:\(\d+\)|\([A-Za-z]\))"
)


# TOC entry terminator: dot leaders (>=2 runs of dots separated by whitespace)
# followed by a page number OR a status token like "Not applicable"/"N/A"/"TBD".
# Used to split a single paragraph/line into multiple TOC entries even when
# entries share one line separated only by spaces.
_TOC_ENTRY_TERMINATOR = re.compile(
    r"(?:\.\s*){2,}\s*(?:\d+|Not\s+applicable|N/A|TBD)\b",
    re.IGNORECASE,
)

_TABLE_COLUMN_HEADER_PATTERN = re.compile(
    r"^\s*Index\s+Name\s+P/N\s+or\s+Type\s+Manufacturer\s*$",
    re.IGNORECASE,
)

_TABLE_NOTE_PATTERN = re.compile(r"^\s*NOTE\s*[:：]", re.IGNORECASE)

_LOWERCASE_WORD_PATTERN = re.compile(r"\b[a-z]{3,}\b")

_HIGHLIGHTS_HEADER_PATTERN = re.compile(
    r"CHAPTER/Section/Page\s+Description of Change(?:\s+Check)?",
    re.IGNORECASE,
)

_HIGHLIGHTS_CHANGE_START_PATTERN = re.compile(
    r"^\s*(?:Changed|Added|Updated|Corrected|Revised|Removed|This page is identified|The pages of this Repair are identified)\b",
    re.IGNORECASE,
)

_HIGHLIGHTS_PAGE_REF_PATTERN = re.compile(r"\bPages?\s+\d", re.IGNORECASE)


def _split_toc_entries(
    paragraphs: list[str],
    *,
    min_entries_trigger: int = 3,
) -> list[str]:
    """Split paragraphs containing multiple TOC-style entries.

    A TOC entry is ``<title> <dot leaders> <page-or-status>``. Splitting is
    terminator-driven (not line-driven) so it correctly handles both:

    * multi-line paragraphs where each line is one entry joined by ``\\n``
    * single-line paragraphs where multiple entries share one physical line
      separated only by whitespace (common when pdfplumber packs a TOC page
      into one giant string).

    Non-numeric terminators ``Not applicable`` / ``N/A`` / ``TBD`` are also
    recognised because parts inventories and maintenance tables often list
    "not applicable" rather than a page number.
    """
    if not paragraphs:
        return []

    result: list[str] = []
    for paragraph in paragraphs:
        matches = list(_TOC_ENTRY_TERMINATOR.finditer(paragraph))
        if len(matches) < min_entries_trigger:
            result.append(paragraph)
            continue

        segments: list[str] = []
        last_end = 0
        for match in matches:
            end = match.end()
            segment = paragraph[last_end:end].strip()
            if segment:
                segments.append(segment)
            last_end = end
        tail = paragraph[last_end:].strip()
        if tail:
            if segments:
                segments[-1] = segments[-1] + "\n" + tail
            else:
                segments.append(tail)
        result.extend(segments if segments else [paragraph])
    return result


def _is_highlights_page(paragraphs: Sequence[str]) -> bool:
    collapsed = [" ".join((paragraph or "").split()) for paragraph in paragraphs]
    return any("HIGHLIGHTS" in item.upper() for item in collapsed) and any(
        _HIGHLIGHTS_HEADER_PATTERN.search(item) for item in collapsed
    )


def _infer_semantic_role(
    paragraph: str,
    *,
    is_highlights_page: bool = False,
) -> str:
    stripped = paragraph.strip()
    if not stripped:
        return SemanticRole.PARAGRAPH.value
    upper = stripped.upper()
    if re.match(r"^(?:NOTE|注意)\s*[:：]", stripped, re.IGNORECASE):
        return SemanticRole.NOTE.value
    if re.match(r"^(?:WARNING|警告)\s*[:：]", stripped, re.IGNORECASE):
        return SemanticRole.WARNING.value
    if re.match(r"^(?:CAUTION|小心)\s*[:：]", stripped, re.IGNORECASE):
        return SemanticRole.CAUTION.value
    if _TOC_ENTRY_TERMINATOR.search(stripped):
        return SemanticRole.TOC_ENTRY.value
    if "LIST OF EFFECTIVE PAGES" in upper or _LEP_ENTRY_PATTERN.search(stripped):
        return SemanticRole.LEP_ENTRY.value
    if is_highlights_page and not _HIGHLIGHTS_HEADER_PATTERN.search(stripped):
        if _HIGHLIGHTS_CHANGE_START_PATTERN.match(stripped) or _HIGHLIGHTS_PAGE_REF_PATTERN.search(stripped):
            return SemanticRole.HIGHLIGHTS_ENTRY.value
    return SemanticRole.PARAGRAPH.value


def _infer_page_type(
    *,
    page_number: int,
    roles: Sequence[str],
    full_text: str,
    has_title: bool,
) -> str:
    role_set = set(roles)
    normalized_text = full_text.lower()
    stripped_text = full_text.strip()
    if SemanticRole.TOC_ENTRY.value in role_set or SemanticRole.LEP_ENTRY.value in role_set:
        return "toc"
    if any(token in normalized_text for token in ("signature", "signed by", "approved by", "签字", "签名", "审批")):
        return "signature"
    if any(token in normalized_text for token in ("appendix", "annex", "附录")):
        return "appendix"
    if page_number == 1 and has_title and not stripped_text:
        return "cover"
    return "body"


def _resolve_request_enable_ocr(request: ParseRequest) -> bool | None:
    if "enable_ocr" not in request.options:
        return None
    value = request.options.get("enable_ocr")
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _split_structural_items(
    paragraphs: list[str],
    *,
    min_lines_trigger: int = 10,
    min_markers: int = 2,
) -> list[str]:
    """Secondary splitter for paragraphs containing numbered/labeled items.

    Some PDFs (typical maintenance manuals) emit structured lists with single
    line breaks instead of blank lines, so the blank-line splitter returns one
    giant paragraph per page. When a paragraph spans many lines and contains
    multiple structural markers (e.g. ``(a)``, ``(26)``, ``NOTE:``), split it
    at each marker boundary.
    """
    if not paragraphs:
        return []

    result: list[str] = []
    for paragraph in paragraphs:
        lines = paragraph.split("\n")
        if len(lines) < min_lines_trigger:
            result.append(paragraph)
            continue
        marker_indices = [
            idx
            for idx, line in enumerate(lines)
            if any(pattern.match(line) for pattern in _STRUCTURAL_ITEM_PATTERNS)
        ]
        if len(marker_indices) < min_markers:
            result.append(paragraph)
            continue

        segments: list[str] = []
        prev = 0
        for idx in marker_indices:
            if idx <= prev:
                continue
            segment = "\n".join(lines[prev:idx]).strip()
            if segment:
                segments.append(segment)
            prev = idx
        tail = "\n".join(lines[prev:]).strip()
        if tail:
            segments.append(tail)
        result.extend(segments if segments else [paragraph])
    return result


def _split_inline_structural_items(
    paragraphs: list[str],
    *,
    min_length_trigger: int = 120,
    min_markers: int = 2,
) -> list[str]:
    """Split long paragraphs on inline structural markers like ``(5)`` / ``(a)``.

    Some PDFs flatten nested procedures into a single paragraph even though the
    content is clearly itemized. This helper splits only long paragraphs that
    contain multiple plain structural markers, so connector references such as
    ``(2-160)`` are left untouched.
    """
    if not paragraphs:
        return []

    result: list[str] = []
    for paragraph in paragraphs:
        text = str(paragraph or "")
        if len(text) < min_length_trigger:
            result.append(paragraph)
            continue

        split_points: list[int] = []
        for match in _INLINE_STRUCTURAL_MARKER_PATTERN.finditer(text):
            start = match.start()
            if start == 0:
                continue
            if not text[start - 1].isspace():
                continue
            split_points.append(start)

        if len(split_points) < min_markers:
            result.append(paragraph)
            continue

        parts: list[str] = []
        prev = 0
        for split_index in split_points:
            part = text[prev:split_index].strip()
            if part:
                parts.append(part)
            prev = split_index
        tail = text[prev:].strip()
        if tail:
            parts.append(tail)
        result.extend(parts if len(parts) >= min_markers + 1 else [paragraph])
    return result


def _strip_repeated_headers_footers(
    pages: list[str],
    *,
    threshold: float = 0.5,
    head_n: int = 3,
    tail_n: int = 3,
    min_line_len: int = 4,
) -> list[str]:
    """Remove header/footer lines that repeat on >= threshold fraction of non-empty pages.

    Mirrors jobcard.backend.pdf_parser._strip_repeated_headers_footers to keep behaviour
    consistent across the two parsing pipelines.
    """
    if len(pages) < 3:
        return list(pages)
    non_empty_count = sum(1 for page in pages if page.strip())
    if non_empty_count < 3:
        return list(pages)
    min_count = max(2, int(non_empty_count * threshold))

    head_counter: dict[str, int] = {}
    tail_counter: dict[str, int] = {}
    for page_text in pages:
        if not page_text.strip():
            continue
        lines = [line.strip() for line in page_text.split("\n") if line.strip()]
        if not lines:
            continue
        for line in set(lines[:head_n]):
            if len(line) >= min_line_len:
                head_counter[line] = head_counter.get(line, 0) + 1
        for line in set(lines[-tail_n:]):
            if len(line) >= min_line_len:
                tail_counter[line] = tail_counter.get(line, 0) + 1

    hf_lines: set[str] = set()
    for line, count in head_counter.items():
        if count >= min_count:
            hf_lines.add(line)
    for line, count in tail_counter.items():
        if count >= min_count:
            hf_lines.add(line)
    if not hf_lines:
        return list(pages)

    cleaned: list[str] = []
    for page_text in pages:
        lines = page_text.split("\n")
        filtered = [line for line in lines if line.strip() not in hf_lines]
        cleaned.append("\n".join(filtered).strip())
    return cleaned


def _merge_short_blocks(
    paragraphs: list[str],
    *,
    min_length: int = 10,
) -> list[str]:
    """Merge blocks shorter than ``min_length`` characters into an adjacent block.

    Heading-like tokens (numbered or all-caps short lines) are preserved as-is because
    they carry semantic structure. Other very short fragments usually come from PDF
    extraction artefacts (stray numbers, isolated letters) and inflate the very-short
    block ratio without contributing information.
    """
    if len(paragraphs) <= 1:
        return list(paragraphs)

    merged: list[str] = []
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if not stripped:
            continue
        if len(stripped) >= min_length or _looks_heading_like(stripped):
            merged.append(paragraph)
            continue
        if merged:
            merged[-1] = merged[-1] + "\n" + paragraph
        else:
            merged.append(paragraph)
    return merged


def _merge_table_continuations(paragraphs: list[str]) -> list[str]:
    """Merge continuation fragments on tabular pages back into their owning row.

    pypdf sometimes emits one logical table row as several paragraphs: a row-start
    block containing the item description, then one or more manufacturer/address
    blocks. Restrict this merge to pages that explicitly carry the canonical tools
    table column header so we do not accidentally collapse ordinary narrative text.
    """
    if len(paragraphs) <= 4:
        return list(paragraphs)

    collapsed_paragraphs = [" ".join(paragraph.split()) for paragraph in paragraphs]
    if not any(
        _TABLE_COLUMN_HEADER_PATTERN.match(collapsed)
        for collapsed in collapsed_paragraphs
    ):
        return list(paragraphs)

    merged: list[str] = []
    seen_table_header = False
    for paragraph, collapsed in zip(paragraphs, collapsed_paragraphs, strict=False):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if not seen_table_header:
            merged.append(paragraph)
            if _TABLE_COLUMN_HEADER_PATTERN.match(collapsed):
                seen_table_header = True
            continue
        if _TABLE_NOTE_PATTERN.match(stripped):
            merged.append(paragraph)
            continue
        if _looks_table_row_start(collapsed):
            merged.append(paragraph)
            continue
        if merged and not _TABLE_NOTE_PATTERN.match(merged[-1].strip()):
            merged[-1] = merged[-1] + "\n" + paragraph
        else:
            merged.append(paragraph)
    return merged


def _merge_highlights_entries(paragraphs: list[str]) -> list[str]:
    """Merge over-split entries on HIGHLIGHTS / change-log pages.

    These pages typically follow a two-column pattern: a page reference anchor
    plus one or more change-description fragments. pypdf can emit them as
    several small paragraphs (for example, ``Pages 505 and 506`` followed by
    ``Changed to ...``). Restrict this merge to explicit HIGHLIGHTS pages so
    the heuristic cannot affect normal narrative content.
    """
    if len(paragraphs) <= 2:
        return list(paragraphs)

    collapsed_paragraphs = [" ".join(paragraph.split()) for paragraph in paragraphs]
    if not any("HIGHLIGHTS" in collapsed.upper() for collapsed in collapsed_paragraphs):
        return list(paragraphs)
    if not any(_HIGHLIGHTS_HEADER_PATTERN.search(collapsed) for collapsed in collapsed_paragraphs):
        return list(paragraphs)

    merged: list[str] = []
    for paragraph, collapsed in zip(paragraphs, collapsed_paragraphs, strict=False):
        if not merged:
            merged.append(paragraph)
            continue
        previous = " ".join(merged[-1].split())
        if _should_merge_highlights_fragment(previous, collapsed):
            merged[-1] = merged[-1] + "\n" + paragraph
        else:
            merged.append(paragraph)
    return merged


def _should_merge_highlights_fragment(previous: str, current: str) -> bool:
    if _HIGHLIGHTS_HEADER_PATTERN.search(previous):
        return False
    if _HIGHLIGHTS_HEADER_PATTERN.search(current):
        return False

    current_starts_change = _HIGHLIGHTS_CHANGE_START_PATTERN.match(current) is not None
    previous_starts_change = _HIGHLIGHTS_CHANGE_START_PATTERN.match(previous) is not None
    previous_page_ref_count = _count_highlights_page_refs(previous)
    current_page_ref_count = _count_highlights_page_refs(current)
    previous_lower = previous.lstrip().lower()
    current_lower = current.lstrip().lower()

    if _looks_highlights_anchor_only(previous) and current_starts_change:
        return True
    if previous_starts_change and current_starts_change:
        if (
            previous_lower.startswith("added ")
            and current_lower.startswith("added ")
            and previous_page_ref_count == 1
            and current_page_ref_count == 1
        ):
            return True
        if current_page_ref_count == 0 and previous_page_ref_count >= 1:
            return True
    return False


def _looks_highlights_anchor_only(text: str) -> bool:
    stripped = text.strip()
    if _HIGHLIGHTS_HEADER_PATTERN.search(stripped):
        return False
    if _HIGHLIGHTS_CHANGE_START_PATTERN.match(stripped):
        return False
    return _count_highlights_page_refs(stripped) >= 1 and len(stripped) <= 80


def _count_highlights_page_refs(text: str) -> int:
    return len(_HIGHLIGHTS_PAGE_REF_PATTERN.findall(text))


def _looks_table_row_start(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) < 24:
        return False
    if _TABLE_COLUMN_HEADER_PATTERN.match(stripped) or _TABLE_NOTE_PATTERN.match(stripped):
        return False
    leading_window = stripped[:20]
    if re.search(r"\b[A-Z]\b", leading_window) is None:
        return False
    return len(_LOWERCASE_WORD_PATTERN.findall(stripped)) >= 2


def _looks_heading_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped or "\n" in stripped or len(stripped) > 80:
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if letters:
        upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if upper_ratio >= 0.8 and len(stripped) >= 3:
            return True
    if _HEADING_LINE_PATTERN.match(stripped + " "):
        return True
    return False


def _load_pdf_reader():
    try:
        from pypdf import PdfReader  # type: ignore

        return PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore

            return PdfReader
        except ImportError as exc:
            raise RuntimeError("pypdf or PyPDF2 is required for pdf-text parser") from exc


# --- A5: image OCR parser (RapidOCR + onnxruntime) ----------------------------


class ImageOcrParser(ParserAdapter):
    """Image parser backed by RapidOCR (CPU-friendly, no GPU dependency).

    Each detected text region becomes one PARAGRAPH block whose metadata
    carries the polygon ``bbox``, recogniser ``confidence`` and reading order.
    The first emitted block is a TITLE block derived from the file stem so
    downstream chunkers and indexers behave consistently with the other
    parsers in this module.
    """

    name = "image-ocr"

    def __init__(
        self,
        *,
        media_types: Sequence[str],
        extensions: Sequence[str],
        options: Mapping[str, Any] | None = None,
        ocr_provider_settings: OcrProviderSettings | None = None,
    ) -> None:
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}
        opts = dict(options or {})
        self._confidence_threshold = float(opts.get("confidence_threshold", 0.5))
        self._merge_short_enabled = bool(opts.get("merge_short_blocks", False))
        self._short_block_min_length = int(opts.get("short_block_min_length", 3))
        self._ocr_provider_settings = (
            ocr_provider_settings or _DEFAULT_OCR_PROVIDER_SETTINGS
        )
        # Cached singleton; RapidOCR initialisation loads ~50MB of ONNX models.
        self._engine: Any = None

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            self._engine = build_ocr_engine(self._ocr_provider_settings)
        return self._engine

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        engine = self._ensure_engine()
        document_path = Path(request.file_path)
        result, _elapse = engine(str(document_path))
        regions: list[tuple[list[list[float]], str, float]] = list(result or [])

        blocks: list[Block] = [
            Block(
                block_id=f"{request.doc_id}-title",
                doc_id=request.doc_id,
                type=BlockType.TITLE,
                content=document_path.stem,
                metadata={
                    "page": 1,
                    "parser": self.name,
                    "semantic_role": SemanticRole.TITLE.value,
                },
            )
        ]

        kept: list[tuple[list[list[float]], str, float]] = []
        for entry in regions:
            try:
                box, text, confidence = entry
            except (TypeError, ValueError):
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                confidence_value = 0.0
            if confidence_value < self._confidence_threshold:
                continue
            kept.append((box, text.strip(), confidence_value))

        # Reading order: top-to-bottom, then left-to-right.
        def _order_key(item: tuple[list[list[float]], str, float]) -> tuple[float, float]:
            box = item[0]
            ys = [float(point[1]) for point in box]
            xs = [float(point[0]) for point in box]
            return (min(ys), min(xs))

        kept.sort(key=_order_key)

        position = 1
        for box, text, confidence_value in kept:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            blocks.append(
                Block(
                    block_id=f"{request.doc_id}-p-{position}",
                    doc_id=request.doc_id,
                    type=BlockType.PARAGRAPH,
                    content=text,
                    metadata={
                        "page": 1,
                        "parser": self.name,
                        "position": position,
                        "bbox": bbox,
                        "confidence": confidence_value,
                        "ocr_engine": "rapidocr_onnxruntime",
                        "semantic_role": SemanticRole.PARAGRAPH.value,
                    },
                )
            )
            position += 1

        if len(blocks) == 1:
            raise RuntimeError("OCR returned no text above the configured confidence threshold")
        return tuple(blocks)


# --- A3 dual-channel: pdfplumber-backed layout extraction ---------------------


from dataclasses import dataclass, field as _dc_field


@dataclass(slots=True)
class _PdfTable:
    bbox: tuple[float, float, float, float]
    cells: list[list[str]]

    @property
    def row_count(self) -> int:
        return len(self.cells)

    @property
    def col_count(self) -> int:
        return max((len(row) for row in self.cells), default=0)

    def render_text(self) -> str:
        """Render cells as a tab-separated table for human-readable storage.

        Cells are joined per row with TAB; rows joined by newline. Empty cells
        become an empty string. The structured form is preserved in metadata so
        downstream consumers can re-parse it without relying on this rendering.
        """

        lines: list[str] = []
        for row in self.cells:
            cleaned = ["" if cell is None else str(cell).strip().replace("\t", " ") for cell in row]
            lines.append("\t".join(cleaned))
        return "\n".join(lines)


@dataclass(slots=True)
class _PageLayout:
    page_number: int
    width: float
    height: float
    text_without_tables: str | None = None
    tables: list[_PdfTable] = _dc_field(default_factory=list)
    column_count_hint: int = 1
    layout_elapsed_s: float = 0.0
    ocr_attempt_reason: str | None = None
    ocr_fallback_reason: str | None = None
    ocr_error_reason: str | None = None
    ocr_engine_init_elapsed_s: float = 0.0
    ocr_render_elapsed_s: float = 0.0
    ocr_call_elapsed_s: float = 0.0
    ocr_provider_elapsed_s: float = 0.0
    ocr_postprocess_elapsed_s: float = 0.0
    ocr_total_elapsed_s: float = 0.0


@dataclass(slots=True)
class _OcrStageTimings:
    engine_init_elapsed_s: float = 0.0
    render_elapsed_s: float = 0.0
    call_elapsed_s: float = 0.0
    provider_elapsed_s: float = 0.0
    postprocess_elapsed_s: float = 0.0
    total_elapsed_s: float = 0.0


@dataclass(slots=True)
class _OcrLine:
    bbox: tuple[float, float, float, float]
    text: str
    confidence: float
    column_index: int = 0


def _extract_pdfplumber_layout(
    file_path: str,
    *,
    min_rows: int,
    min_cols: int,
    ocr_page_text_fn: Callable[
        [Any, Sequence[tuple[float, float, float, float]], int, str | None],
        tuple[str | None, str | None, str | None, _OcrStageTimings],
    ] | None = None,
) -> list[_PageLayout]:
    """Extract per-page layout (tables + table-stripped text) using pdfplumber.

    Returns an empty list when pdfplumber is unavailable; callers should treat
    that as "dual channel disabled for this run" and fall through to pypdf
    behaviour without raising.
    """

    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return []

    layouts: list[_PageLayout] = []
    with pdfplumber.open(file_path) as document:
        for index, page in enumerate(document.pages, start=1):
            page_started = time.monotonic()
            tables: list[_PdfTable] = []
            try:
                found = page.find_tables() or []
            except Exception:  # pragma: no cover - pdfplumber edge cases
                found = []
            table_bboxes: list[tuple[float, float, float, float]] = []
            for table in found:
                try:
                    cells = table.extract() or []
                except Exception:
                    cells = []
                cells = [
                    [(cell or "") for cell in row]
                    for row in cells
                    if any((cell or "").strip() for cell in row)
                ]
                if len(cells) < min_rows:
                    continue
                if max((len(row) for row in cells), default=0) < min_cols:
                    continue
                bbox = tuple(float(value) for value in table.bbox)
                tables.append(_PdfTable(bbox=bbox, cells=cells))  # type: ignore[arg-type]
                table_bboxes.append(bbox)  # type: ignore[arg-type]

            column_count_hint = _estimate_column_count(page)
            text_without_tables: str | None
            ocr_attempt_reason: str | None = None
            ocr_fallback_reason: str | None = None
            ocr_error_reason: str | None = None
            ocr_timings = _OcrStageTimings()
            try:
                if _should_rebuild_multi_column_text(page, column_count_hint=column_count_hint):
                    text_without_tables = _extract_text_by_columns(
                        page,
                        table_bboxes,
                        column_count=column_count_hint,
                    )
                elif table_bboxes:
                    text_without_tables = _extract_text_excluding_bboxes(page, table_bboxes)
                else:
                    text_without_tables = page.extract_text() or ""
            except Exception:
                text_without_tables = None
            layout_elapsed_s = round(time.monotonic() - page_started, 6)

            if ocr_page_text_fn is not None:
                recovered_text, ocr_attempt_reason, ocr_error_reason, ocr_timings = ocr_page_text_fn(
                    page,
                    table_bboxes,
                    column_count_hint,
                    text_without_tables,
                )
                if recovered_text:
                    text_without_tables = recovered_text
                    ocr_fallback_reason = ocr_attempt_reason

            layouts.append(
                _PageLayout(
                    page_number=index,
                    width=float(page.width or 0.0),
                    height=float(page.height or 0.0),
                    text_without_tables=text_without_tables,
                    tables=tables,
                    column_count_hint=column_count_hint,
                    layout_elapsed_s=layout_elapsed_s,
                    ocr_attempt_reason=ocr_attempt_reason,
                    ocr_fallback_reason=ocr_fallback_reason,
                    ocr_error_reason=ocr_error_reason,
                    ocr_engine_init_elapsed_s=ocr_timings.engine_init_elapsed_s,
                    ocr_render_elapsed_s=ocr_timings.render_elapsed_s,
                    ocr_call_elapsed_s=ocr_timings.call_elapsed_s,
                    ocr_provider_elapsed_s=ocr_timings.provider_elapsed_s,
                    ocr_postprocess_elapsed_s=ocr_timings.postprocess_elapsed_s,
                    ocr_total_elapsed_s=ocr_timings.total_elapsed_s,
                )
            )
    return layouts


def _attach_page_layout_metadata(metadata: dict[str, Any], page_layout: _PageLayout) -> None:
    metadata["page_width"] = page_layout.width
    metadata["page_height"] = page_layout.height
    metadata["layout_source"] = "pdfplumber"
    metadata["column_count_hint"] = page_layout.column_count_hint
    metadata["layout_elapsed_s"] = page_layout.layout_elapsed_s
    if page_layout.ocr_attempt_reason is not None:
        metadata["ocr_attempted"] = True
        metadata["ocr_attempt_reason"] = page_layout.ocr_attempt_reason
    if page_layout.ocr_fallback_reason is not None:
        metadata["ocr_fallback_used"] = True
        metadata["ocr_fallback_reason"] = page_layout.ocr_fallback_reason
    if page_layout.ocr_error_reason is not None:
        metadata["ocr_error_reason"] = page_layout.ocr_error_reason
    if page_layout.ocr_engine_init_elapsed_s > 0.0:
        metadata["ocr_engine_init_elapsed_s"] = page_layout.ocr_engine_init_elapsed_s
    if page_layout.ocr_render_elapsed_s > 0.0:
        metadata["ocr_render_elapsed_s"] = page_layout.ocr_render_elapsed_s
    if page_layout.ocr_call_elapsed_s > 0.0:
        metadata["ocr_call_elapsed_s"] = page_layout.ocr_call_elapsed_s
    if page_layout.ocr_provider_elapsed_s > 0.0:
        metadata["ocr_provider_elapsed_s"] = page_layout.ocr_provider_elapsed_s
    if page_layout.ocr_postprocess_elapsed_s > 0.0:
        metadata["ocr_postprocess_elapsed_s"] = page_layout.ocr_postprocess_elapsed_s
    if page_layout.ocr_total_elapsed_s > 0.0:
        metadata["ocr_total_elapsed_s"] = page_layout.ocr_total_elapsed_s


_CID_TOKEN_PATTERN = re.compile(r"\(cid:\d+\)")


def _ocr_fallback_reason_for_page(
    text: str,
    *,
    min_cid_tokens: int,
    min_cid_char_ratio: float,
) -> str | None:
    stripped = text.strip()
    if not stripped:
        return "empty_text"
    matches = list(_CID_TOKEN_PATTERN.finditer(stripped))
    if len(matches) < min_cid_tokens:
        return None
    cid_chars = sum(match.end() - match.start() for match in matches)
    if cid_chars / max(len(stripped), 1) >= min_cid_char_ratio:
        return "cid_dense"
    return None


def _extract_ocr_text_from_page(
    page: Any,
    *,
    engine: Any,
    table_bboxes: Sequence[tuple[float, float, float, float]],
    resolution: int,
    confidence_threshold: float,
    column_count_hint: int,
    merge_line_gap_ratio: float,
) -> tuple[str | None, str | None, _OcrStageTimings]:
    timings = _OcrStageTimings()
    try:
        import numpy as np  # type: ignore
        from PIL import ImageDraw  # type: ignore
    except ImportError:
        return None, "ocr_dependencies_missing", timings

    render_started = time.monotonic()
    try:
        rendered = page.to_image(resolution=resolution).original.convert("RGB")
    except Exception:
        timings.render_elapsed_s = round(time.monotonic() - render_started, 6)
        timings.total_elapsed_s = timings.render_elapsed_s
        return None, "ocr_render_failed", timings

    if table_bboxes:
        draw = ImageDraw.Draw(rendered)
        scale_x = rendered.width / max(float(page.width or 1.0), 1.0)
        scale_y = rendered.height / max(float(page.height or 1.0), 1.0)
        for x0, top, x1, bottom in table_bboxes:
            draw.rectangle(
                (
                    x0 * scale_x,
                    top * scale_y,
                    x1 * scale_x,
                    bottom * scale_y,
                ),
                fill="white",
            )
    timings.render_elapsed_s = round(time.monotonic() - render_started, 6)

    call_started = time.monotonic()
    try:
        result, provider_elapsed = engine(np.array(rendered))
    except Exception as exc:
        timings.call_elapsed_s = round(time.monotonic() - call_started, 6)
        timings.total_elapsed_s = round(
            timings.render_elapsed_s + timings.call_elapsed_s,
            6,
        )
        return None, _classify_ocr_error(exc), timings
    timings.call_elapsed_s = round(time.monotonic() - call_started, 6)
    try:
        timings.provider_elapsed_s = float(provider_elapsed)
    except (TypeError, ValueError):
        timings.provider_elapsed_s = 0.0

    postprocess_started = time.monotonic()
    lines = _collect_ocr_lines(
        result,
        confidence_threshold=confidence_threshold,
        column_count_hint=column_count_hint,
        page_width=float(rendered.width),
    )
    paragraphs = _group_ocr_lines(lines, merge_line_gap_ratio=merge_line_gap_ratio)
    timings.postprocess_elapsed_s = round(time.monotonic() - postprocess_started, 6)
    timings.total_elapsed_s = round(
        timings.render_elapsed_s + timings.call_elapsed_s + timings.postprocess_elapsed_s,
        6,
    )
    if not paragraphs:
        return None, "empty_ocr_text", timings
    return "\n\n".join(paragraphs), None, timings


def _collect_ocr_lines(
    result: Any,
    *,
    confidence_threshold: float,
    column_count_hint: int,
    page_width: float,
) -> list[_OcrLine]:
    lines: list[_OcrLine] = []
    for entry in result or []:
        try:
            box, text, confidence = entry
        except (TypeError, ValueError):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < confidence_threshold:
            continue
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        bbox = (min(xs), min(ys), max(xs), max(ys))
        column_index = 0
        if column_count_hint > 1 and page_width > 0.0:
            column_width = page_width / float(column_count_hint)
            center_x = (bbox[0] + bbox[2]) / 2.0
            column_index = min(
                column_count_hint - 1,
                max(0, int(center_x / max(column_width, 1.0))),
            )
        lines.append(
            _OcrLine(
                bbox=bbox,
                text=text.strip(),
                confidence=confidence_value,
                column_index=column_index,
            )
        )

    lines.sort(key=lambda item: (item.column_index, item.bbox[1], item.bbox[0]))
    return lines


def _group_ocr_lines(
    lines: Sequence[_OcrLine],
    *,
    merge_line_gap_ratio: float,
) -> list[str]:
    if not lines:
        return []

    paragraphs: list[str] = []
    current: list[str] = [lines[0].text]
    previous = lines[0]
    for line in lines[1:]:
        previous_height = max(1.0, previous.bbox[3] - previous.bbox[1])
        current_height = max(1.0, line.bbox[3] - line.bbox[1])
        vertical_gap = line.bbox[1] - previous.bbox[3]
        x_shift = abs(line.bbox[0] - previous.bbox[0])
        same_paragraph = (
            line.column_index == previous.column_index
            and 0.0 <= vertical_gap <= max(previous_height, current_height) * merge_line_gap_ratio
            and x_shift <= max(previous_height, current_height) * 6.0
        )
        if same_paragraph:
            current.append(line.text)
        else:
            paragraphs.append(" ".join(current))
            current = [line.text]
        previous = line
    paragraphs.append(" ".join(current))
    return [paragraph for paragraph in paragraphs if paragraph.strip()]


def _should_rebuild_multi_column_text(page: Any, *, column_count_hint: int) -> bool:
    if column_count_hint <= 1:
        return False
    try:
        width = float(page.width or 0.0)
        height = float(page.height or 0.0)
        return width > 0.0 and height > 0.0
    except (TypeError, ValueError):
        return False


def _extract_text_by_columns(
    page: Any,
    bboxes: Sequence[tuple[float, float, float, float]],
    *,
    column_count: int,
) -> str:
    """Extract text region-by-region to preserve multi-column reading order.

    Used only for pages already flagged as likely multi-column. Single-column
    pages continue to use the existing page-wide extraction path.
    """

    if column_count <= 1:
        return _extract_text_excluding_bboxes(page, bboxes) if bboxes else (page.extract_text() or "")

    filtered = _filter_page_excluding_bboxes(page, bboxes)
    width = float(page.width or 0.0)
    height = float(page.height or 0.0)
    if width <= 0.0 or height <= 0.0:
        return filtered.extract_text() or ""

    column_width = width / float(column_count)
    parts: list[str] = []
    for index in range(column_count):
        x0 = index * column_width
        x1 = width if index == column_count - 1 else (index + 1) * column_width
        try:
            region = filtered.crop((x0, 0.0, x1, height))
            text = (region.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)
    return filtered.extract_text() or ""


def _estimate_column_count(page: Any) -> int:
    """Return a conservative page-level column count hint from word x positions.

    This is intentionally informational only. It does not change parse order or
    splitting behaviour; it merely tags pages that look strongly two-column so
    the regression report can surface them for human inspection.
    """

    try:
        words = page.extract_words() or []
    except Exception:
        return 1

    height = float(page.height or 0.0)
    width = float(page.width or 0.0)
    if width <= 0.0 or height <= 0.0:
        return 1

    lines: list[dict[str, float | int]] = []
    for word in words:
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        try:
            x0 = float(word.get("x0", 0.0))
            x1 = float(word.get("x1", x0))
            top = float(word.get("top", 0.0))
        except (TypeError, ValueError):
            continue
        if not lines or abs(top - float(lines[-1]["top"])) > 3.0:
            lines.append({"top": top, "x0": x0, "x1": x1, "words": 1})
            continue
        lines[-1]["x0"] = min(float(lines[-1]["x0"]), x0)
        lines[-1]["x1"] = max(float(lines[-1]["x1"]), x1)
        lines[-1]["words"] = int(lines[-1]["words"]) + 1

    if len(lines) < 12:
        return 1

    left_bands: set[int] = set()
    right_bands: set[int] = set()
    wide_lines = 0
    for line in lines:
        x0 = float(line["x0"])
        x1 = float(line["x1"])
        span = x1 - x0
        top = float(line["top"])
        band = min(19, max(0, int((top / height) * 20)))
        if span >= width * 0.55:
            wide_lines += 1
            continue
        if int(line["words"]) < 2 or span > width * 0.42:
            continue
        if x1 <= width * 0.48:
            left_bands.add(band)
            continue
        if x0 >= width * 0.52:
            right_bands.add(band)

    shared_bands = left_bands & right_bands
    if (
        len(shared_bands) >= 4
        and len(left_bands) >= 6
        and len(right_bands) >= 6
        and wide_lines <= max(3, len(lines) // 4)
    ):
        return 2
    return 1


def _extract_text_excluding_bboxes(
    page: Any,
    bboxes: Sequence[tuple[float, float, float, float]],
) -> str:
    """Return ``page.extract_text()`` with words inside ``bboxes`` removed.

    pdfplumber's ``filter`` API operates per-object so we drop chars whose
    centre falls inside any table bbox before re-extracting text. Works
    regardless of pdfplumber minor version because it uses the public
    ``page.filter`` callable.
    """

    filtered = _filter_page_excluding_bboxes(page, bboxes)
    return filtered.extract_text() or ""


def _filter_page_excluding_bboxes(
    page: Any,
    bboxes: Sequence[tuple[float, float, float, float]],
) -> Any:
    def _keep(obj: Mapping[str, Any]) -> bool:
        x0 = float(obj.get("x0", 0.0))
        x1 = float(obj.get("x1", 0.0))
        top = float(obj.get("top", 0.0))
        bottom = float(obj.get("bottom", 0.0))
        cx = (x0 + x1) / 2.0
        cy = (top + bottom) / 2.0
        for bx0, btop, bx1, bbottom in bboxes:
            if bx0 <= cx <= bx1 and btop <= cy <= bbottom:
                return False
        return True

    return page.filter(_keep)


# --- A4 hookup helpers --------------------------------------------------------


_LLM_REFINE_KEYWORD_PATTERN = re.compile(
    r"\b(?:NOTE|WARNING|CAUTION|SB|Service\s+Bulletin|Step|Procedure)\b",
    re.IGNORECASE,
)


def _is_low_confidence_paragraph(
    paragraph: str,
    *,
    min_length: int,
    min_markers: int,
) -> bool:
    """Conservative gate for sending a paragraph to the LLM refiner.

    Triggers only when the paragraph is long, contains multiple structural
    markers, AND mentions a procedural keyword. This intentionally biases
    towards skipping borderline cases so the per-document call cap is spent
    on the highest-value candidates.
    """

    if len(paragraph) < min_length:
        return False
    structural_markers = sum(
        1
        for pattern in _STRUCTURAL_ITEM_PATTERNS
        for _ in pattern.finditer(paragraph)
    )
    structural_markers += len(
        re.findall(r"(?:^|\s)(?:\(\d+\)|\([A-Za-z]\))", paragraph)
    )
    if structural_markers < min_markers:
        return False
    if not _LLM_REFINE_KEYWORD_PATTERN.search(paragraph):
        return False
    return True


def _refine_low_confidence_paragraphs(
    paragraphs: list[str],
    *,
    refiner: Any,
    min_length: int,
    min_markers: int,
) -> list[str]:
    if not paragraphs:
        return paragraphs
    refine = getattr(refiner, "refine_paragraph", None)
    if not callable(refine):
        return paragraphs
    refined: list[str] = []
    for paragraph in paragraphs:
        if not _is_low_confidence_paragraph(
            paragraph, min_length=min_length, min_markers=min_markers
        ):
            refined.append(paragraph)
            continue
        try:
            parts = refine(paragraph)
        except Exception:
            refined.append(paragraph)
            continue
        if not parts:
            refined.append(paragraph)
            continue
        refined.extend(parts)
    return refined


def build_parser(
    name: str,
    *,
    media_types: Sequence[str],
    extensions: Sequence[str],
    options: Mapping[str, Any] | None = None,
    ocr_provider_settings: OcrProviderSettings | None = None,
    boundary_refiner: Any = None,
) -> ParserAdapter:
    normalized = name.strip().lower()
    if normalized == "docx-native":
        return DocxParser(media_types=media_types, extensions=extensions)
    if normalized == "text-native":
        return TextParser(media_types=media_types, extensions=extensions)
    if normalized == "pdf-text":
        return PdfTextParser(
            media_types=media_types,
            extensions=extensions,
            options=options,
            ocr_provider_settings=ocr_provider_settings,
            boundary_refiner=boundary_refiner,
        )
    if normalized == "image-ocr":
        return ImageOcrParser(
            media_types=media_types,
            extensions=extensions,
            options=options,
            ocr_provider_settings=ocr_provider_settings,
        )
    return StubParser(name=name, media_types=media_types, extensions=extensions)