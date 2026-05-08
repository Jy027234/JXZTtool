"""Structural self-check for ParseCore output (Phase A2').

Computes structural-quality metrics directly from a list of `Block` objects,
without comparing against any legacy implementation. Used as:

1. A regression guard during ParseCore changes (no ground truth required).
2. Input to scenario-based human spot checks (top-N noisy pages).
3. A neutral evaluator that replaces the legacy-vs-ParseCore comparison once
   jobcard's compare-only narrow rules are retired.

The tool is intentionally engine-agnostic: it consumes `Block` objects as
emitted by `Runtime.parse_document` and groups them by their `metadata['page']`
field. Pages with no page number are bucketed under page 0.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from math import sqrt
from typing import Iterable, Sequence

from .garble import analyze_text_fragments_garble
from .models import Block, Chunk


_HEADER_FOOTER_MAX_LEN = 80
_VERY_SHORT_LEN = 10
_NUMERIC_HEAVY_RATIO = 0.6
_LONG_BLOCK_LEN = 2000

# CID-garble gate thresholds
_CID_TOTAL_WARN_TOKENS = 50        # ≥50 cid tokens doc-wide → warn
_CID_TOTAL_GATE_TOKENS = 200       # ≥200 cid tokens doc-wide → flag cid_garble
_CID_PAGE_RATIO_GATE = 0.12        # page-level char ratio already used by parser
_PDF_NAME_TOTAL_WARN_TOKENS = 40
_PDF_NAME_TOTAL_GATE_TOKENS = 120
_MAX_CONTROL_CHAR_RATIO = 0.03
_MIN_PRINTABLE_RATIO = 0.75
_SHORT_TOKEN_REPEAT_GATE = 0.35

@dataclass(slots=True)
class PageQuality:
    page_number: int
    block_count: int
    median_length: float
    very_short_ratio: float
    suspected_header_footer: int
    numeric_heavy: int
    max_length: int


@dataclass(slots=True)
class OcrPageSignal:
    page_number: int
    ocr_total_elapsed_s: float
    ocr_engine_exec_elapsed_s: float
    ocr_provider_cls_elapsed_s: float
    ocr_provider_crop_count: int
    ocr_provider_cls_rotate_positive_count: int
    ocr_provider_cls_rotate_high_count: int
    cls_rotate_positive_ratio: float
    cls_rotate_high_ratio: float


@dataclass(slots=True)
class LayoutSignalsReport:
    pages_with_layout_metadata: int
    multi_column_pages: int
    header_footer_stripped_pages: int
    header_footer_stripped_blocks: int
    ocr_attempted_pages: int
    ocr_attempted_blocks: int
    ocr_fallback_pages: int
    ocr_fallback_blocks: int
    ocr_failed_pages: int
    ocr_failed_blocks: int
    layout_elapsed_s: float
    ocr_engine_init_elapsed_s: float
    ocr_render_elapsed_s: float
    ocr_input_prepare_elapsed_s: float
    ocr_engine_exec_elapsed_s: float
    ocr_call_elapsed_s: float
    ocr_provider_elapsed_s: float
    ocr_provider_det_elapsed_s: float
    ocr_provider_cls_elapsed_s: float
    ocr_provider_rec_elapsed_s: float
    ocr_provider_crop_count: int
    ocr_provider_cls_rotate_positive_count: int
    ocr_provider_cls_rotate_high_count: int
    ocr_postprocess_elapsed_s: float
    ocr_total_elapsed_s: float
    max_ocr_page_elapsed_s: float
    ocr_page_signals: tuple[OcrPageSignal, ...] = field(default_factory=tuple)

    def ocr_hot_pages(self, *, top: int = 10) -> tuple[OcrPageSignal, ...]:
        ranked = sorted(
            (page for page in self.ocr_page_signals if page.ocr_total_elapsed_s > 0.0),
            key=lambda page: (
                page.ocr_total_elapsed_s,
                page.ocr_engine_exec_elapsed_s,
                page.ocr_provider_crop_count,
            ),
            reverse=True,
        )
        return tuple(ranked[:top])

    def ocr_sparse_cls_pages(self, *, top: int = 10) -> tuple[OcrPageSignal, ...]:
        ranked = sorted(
            (
                page
                for page in self.ocr_page_signals
                if page.ocr_total_elapsed_s > 0.0 and page.ocr_provider_crop_count > 0
            ),
            key=lambda page: (
                page.cls_rotate_high_ratio,
                page.ocr_total_elapsed_s * -1.0,
                page.ocr_provider_crop_count * -1,
                page.page_number,
            ),
        )
        return tuple(ranked[:top])


@dataclass(slots=True)
class EmbeddingQualityReport:
    total_chunks: int
    embedded_chunks: int
    embedded_chunk_ratio: float
    mean_embedding_dim_norm: float


@dataclass(slots=True)
class StructuralQualityReport:
    total_blocks: int
    page_count: int
    median_block_length: float
    very_short_ratio: float
    suspected_header_footer_total: int
    numeric_heavy_total: int
    long_block_total: int
    pages: tuple[PageQuality, ...] = field(default_factory=tuple)

    def noisy_pages(self, *, top: int = 10) -> tuple[PageQuality, ...]:
        """Pages most likely to need human spot-check, ranked by noise score."""

        def _score(page: PageQuality) -> tuple[float, int]:
            return (
                page.very_short_ratio,
                page.suspected_header_footer + page.numeric_heavy,
            )

        ranked = sorted(self.pages, key=_score, reverse=True)
        return tuple(ranked[:top])


def _is_numeric_heavy(text: str) -> bool:
    if not text:
        return False
    digits = sum(1 for char in text if char.isdigit())
    return digits / max(len(text), 1) >= _NUMERIC_HEAVY_RATIO


def _is_suspected_header_footer(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > _HEADER_FOOTER_MAX_LEN:
        return False
    return _is_numeric_heavy(stripped) or stripped.lower().startswith("page ")


def _page_number_of(block: Block) -> int:
    raw = block.metadata.get("page")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def evaluate_blocks(blocks: Iterable[Block]) -> StructuralQualityReport:
    """Compute the structural quality report for a single document."""

    blocks_list: list[Block] = list(blocks)
    page_buckets: dict[int, list[Block]] = {}
    for block in blocks_list:
        page_buckets.setdefault(_page_number_of(block), []).append(block)

    pages: list[PageQuality] = []
    very_short_total = 0
    header_footer_total = 0
    numeric_heavy_total = 0
    long_block_total = 0
    all_lengths: list[int] = []

    for page_number in sorted(page_buckets):
        page_blocks = page_buckets[page_number]
        lengths = [len(block.content) for block in page_blocks]
        all_lengths.extend(lengths)

        very_short = sum(1 for length in lengths if length < _VERY_SHORT_LEN)
        header_footer = sum(
            1 for block in page_blocks if _is_suspected_header_footer(block.content)
        )
        numeric_heavy = sum(
            1 for block in page_blocks if _is_numeric_heavy(block.content)
        )
        long_blocks = sum(1 for length in lengths if length >= _LONG_BLOCK_LEN)

        very_short_total += very_short
        header_footer_total += header_footer
        numeric_heavy_total += numeric_heavy
        long_block_total += long_blocks

        pages.append(
            PageQuality(
                page_number=page_number,
                block_count=len(page_blocks),
                median_length=float(statistics.median(lengths)) if lengths else 0.0,
                very_short_ratio=(very_short / len(lengths)) if lengths else 0.0,
                suspected_header_footer=header_footer,
                numeric_heavy=numeric_heavy,
                max_length=max(lengths, default=0),
            )
        )

    total = len(blocks_list)
    median_length = float(statistics.median(all_lengths)) if all_lengths else 0.0
    very_short_ratio = (very_short_total / total) if total else 0.0

    return StructuralQualityReport(
        total_blocks=total,
        page_count=len(pages),
        median_block_length=median_length,
        very_short_ratio=very_short_ratio,
        suspected_header_footer_total=header_footer_total,
        numeric_heavy_total=numeric_heavy_total,
        long_block_total=long_block_total,
        pages=tuple(pages),
    )


def evaluate_layout_signals(blocks: Iterable[Block]) -> LayoutSignalsReport:
    """Summarize optional layout-side signals emitted in block metadata.

    These metrics are informational rather than gating. They help track whether
    a run carried page-level layout annotations, whether any pages were marked
    as multi-column by the parser, and whether opt-in header/footer stripping
    was active for any page.
    """

    pages_with_layout_metadata: set[int] = set()
    multi_column_pages: set[int] = set()
    header_footer_stripped_pages: set[int] = set()
    header_footer_stripped_blocks = 0
    ocr_attempted_pages: set[int] = set()
    ocr_attempted_blocks = 0
    ocr_fallback_pages: set[int] = set()
    ocr_fallback_blocks = 0
    ocr_failed_pages: set[int] = set()
    ocr_failed_blocks = 0
    page_timings: dict[int, dict[str, float]] = {}
    timing_keys = (
        "layout_elapsed_s",
        "ocr_engine_init_elapsed_s",
        "ocr_render_elapsed_s",
        "ocr_input_prepare_elapsed_s",
        "ocr_engine_exec_elapsed_s",
        "ocr_call_elapsed_s",
        "ocr_provider_elapsed_s",
        "ocr_provider_det_elapsed_s",
        "ocr_provider_cls_elapsed_s",
        "ocr_provider_rec_elapsed_s",
        "ocr_postprocess_elapsed_s",
        "ocr_total_elapsed_s",
    )
    count_keys = (
        "ocr_provider_crop_count",
        "ocr_provider_cls_rotate_positive_count",
        "ocr_provider_cls_rotate_high_count",
    )

    for block in blocks:
        page_number = _page_number_of(block)
        metadata = block.metadata
        if (
            metadata.get("layout_source")
            or metadata.get("page_width") is not None
            or metadata.get("page_height") is not None
        ):
            pages_with_layout_metadata.add(page_number)
        try:
            column_count_hint = int(metadata.get("column_count_hint", 1))
        except (TypeError, ValueError):
            column_count_hint = 1
        if column_count_hint > 1:
            multi_column_pages.add(page_number)
        if bool(metadata.get("header_footer_stripped")):
            header_footer_stripped_pages.add(page_number)
            header_footer_stripped_blocks += 1
        if bool(metadata.get("ocr_attempted")):
            ocr_attempted_pages.add(page_number)
            ocr_attempted_blocks += 1
        if bool(metadata.get("ocr_fallback_used")):
            ocr_fallback_pages.add(page_number)
            ocr_fallback_blocks += 1
        if metadata.get("ocr_error_reason"):
            ocr_failed_pages.add(page_number)
            ocr_failed_blocks += 1
        page_timing = page_timings.setdefault(page_number, {})
        for key in timing_keys:
            raw_value = metadata.get(key)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value <= 0.0:
                continue
            page_timing[key] = max(page_timing.get(key, 0.0), value)
        for key in count_keys:
            raw_value = metadata.get(key)
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            page_timing[key] = max(page_timing.get(key, 0.0), float(value))

    layout_elapsed_s = sum(item.get("layout_elapsed_s", 0.0) for item in page_timings.values())
    ocr_engine_init_elapsed_s = sum(item.get("ocr_engine_init_elapsed_s", 0.0) for item in page_timings.values())
    ocr_render_elapsed_s = sum(item.get("ocr_render_elapsed_s", 0.0) for item in page_timings.values())
    ocr_input_prepare_elapsed_s = sum(item.get("ocr_input_prepare_elapsed_s", 0.0) for item in page_timings.values())
    ocr_engine_exec_elapsed_s = sum(item.get("ocr_engine_exec_elapsed_s", 0.0) for item in page_timings.values())
    ocr_call_elapsed_s = sum(item.get("ocr_call_elapsed_s", 0.0) for item in page_timings.values())
    ocr_provider_elapsed_s = sum(item.get("ocr_provider_elapsed_s", 0.0) for item in page_timings.values())
    ocr_provider_det_elapsed_s = sum(item.get("ocr_provider_det_elapsed_s", 0.0) for item in page_timings.values())
    ocr_provider_cls_elapsed_s = sum(item.get("ocr_provider_cls_elapsed_s", 0.0) for item in page_timings.values())
    ocr_provider_rec_elapsed_s = sum(item.get("ocr_provider_rec_elapsed_s", 0.0) for item in page_timings.values())
    ocr_provider_crop_count = int(sum(item.get("ocr_provider_crop_count", 0.0) for item in page_timings.values()))
    ocr_provider_cls_rotate_positive_count = int(
        sum(item.get("ocr_provider_cls_rotate_positive_count", 0.0) for item in page_timings.values())
    )
    ocr_provider_cls_rotate_high_count = int(
        sum(item.get("ocr_provider_cls_rotate_high_count", 0.0) for item in page_timings.values())
    )
    ocr_postprocess_elapsed_s = sum(item.get("ocr_postprocess_elapsed_s", 0.0) for item in page_timings.values())
    ocr_total_elapsed_s = sum(item.get("ocr_total_elapsed_s", 0.0) for item in page_timings.values())
    max_ocr_page_elapsed_s = max(
        (item.get("ocr_total_elapsed_s", 0.0) for item in page_timings.values()),
        default=0.0,
    )
    ocr_page_signals: list[OcrPageSignal] = []
    for page_number in sorted(page_timings):
        item = page_timings[page_number]
        if not item:
            continue
        crop_count = int(item.get("ocr_provider_crop_count", 0.0))
        rotate_positive_count = int(item.get("ocr_provider_cls_rotate_positive_count", 0.0))
        rotate_high_count = int(item.get("ocr_provider_cls_rotate_high_count", 0.0))
        crop_base = max(crop_count, 1)
        ocr_page_signals.append(
            OcrPageSignal(
                page_number=page_number,
                ocr_total_elapsed_s=item.get("ocr_total_elapsed_s", 0.0),
                ocr_engine_exec_elapsed_s=item.get("ocr_engine_exec_elapsed_s", 0.0),
                ocr_provider_cls_elapsed_s=item.get("ocr_provider_cls_elapsed_s", 0.0),
                ocr_provider_crop_count=crop_count,
                ocr_provider_cls_rotate_positive_count=rotate_positive_count,
                ocr_provider_cls_rotate_high_count=rotate_high_count,
                cls_rotate_positive_ratio=rotate_positive_count / crop_base if crop_count else 0.0,
                cls_rotate_high_ratio=rotate_high_count / crop_base if crop_count else 0.0,
            )
        )

    return LayoutSignalsReport(
        pages_with_layout_metadata=len(pages_with_layout_metadata),
        multi_column_pages=len(multi_column_pages),
        header_footer_stripped_pages=len(header_footer_stripped_pages),
        header_footer_stripped_blocks=header_footer_stripped_blocks,
        ocr_attempted_pages=len(ocr_attempted_pages),
        ocr_attempted_blocks=ocr_attempted_blocks,
        ocr_fallback_pages=len(ocr_fallback_pages),
        ocr_fallback_blocks=ocr_fallback_blocks,
        ocr_failed_pages=len(ocr_failed_pages),
        ocr_failed_blocks=ocr_failed_blocks,
        layout_elapsed_s=layout_elapsed_s,
        ocr_engine_init_elapsed_s=ocr_engine_init_elapsed_s,
        ocr_render_elapsed_s=ocr_render_elapsed_s,
        ocr_input_prepare_elapsed_s=ocr_input_prepare_elapsed_s,
        ocr_engine_exec_elapsed_s=ocr_engine_exec_elapsed_s,
        ocr_call_elapsed_s=ocr_call_elapsed_s,
        ocr_provider_elapsed_s=ocr_provider_elapsed_s,
        ocr_provider_det_elapsed_s=ocr_provider_det_elapsed_s,
        ocr_provider_cls_elapsed_s=ocr_provider_cls_elapsed_s,
        ocr_provider_rec_elapsed_s=ocr_provider_rec_elapsed_s,
        ocr_provider_crop_count=ocr_provider_crop_count,
        ocr_provider_cls_rotate_positive_count=ocr_provider_cls_rotate_positive_count,
        ocr_provider_cls_rotate_high_count=ocr_provider_cls_rotate_high_count,
        ocr_postprocess_elapsed_s=ocr_postprocess_elapsed_s,
        ocr_total_elapsed_s=ocr_total_elapsed_s,
        max_ocr_page_elapsed_s=max_ocr_page_elapsed_s,
        ocr_page_signals=tuple(ocr_page_signals),
    )


def evaluate_chunk_embeddings(chunks: Iterable[Chunk]) -> EmbeddingQualityReport:
    chunks_list = list(chunks)
    total_chunks = len(chunks_list)
    embedded_norms: list[float] = []

    for chunk in chunks_list:
        if not chunk.embedding:
            continue
        vector = [float(value) for value in chunk.embedding]
        embedded_norms.append(sqrt(sum(value * value for value in vector)))

    embedded_chunks = len(embedded_norms)
    embedded_chunk_ratio = (embedded_chunks / total_chunks) if total_chunks else 0.0
    mean_embedding_dim_norm = (
        float(statistics.mean(embedded_norms)) if embedded_norms else 0.0
    )
    return EmbeddingQualityReport(
        total_chunks=total_chunks,
        embedded_chunks=embedded_chunks,
        embedded_chunk_ratio=embedded_chunk_ratio,
        mean_embedding_dim_norm=mean_embedding_dim_norm,
    )


def diff_reports(
    baseline: StructuralQualityReport,
    candidate: StructuralQualityReport,
) -> dict[str, float | int]:
    """Return scalar deltas between two reports (candidate - baseline).

    Useful as a regression guard: run ParseCore before and after a change on
    the same document and require the deltas to stay within a chosen budget.
    """

    return {
        "total_blocks": candidate.total_blocks - baseline.total_blocks,
        "page_count": candidate.page_count - baseline.page_count,
        "median_block_length": candidate.median_block_length - baseline.median_block_length,
        "very_short_ratio": candidate.very_short_ratio - baseline.very_short_ratio,
        "suspected_header_footer_total": (
            candidate.suspected_header_footer_total - baseline.suspected_header_footer_total
        ),
        "numeric_heavy_total": candidate.numeric_heavy_total - baseline.numeric_heavy_total,
        "long_block_total": candidate.long_block_total - baseline.long_block_total,
    }


__all__ = [
    "EmbeddingQualityReport",
    "LayoutSignalsReport",
    "OcrPageSignal",
    "PageQuality",
    "ParseQualitySummary",
    "StructuralQualityReport",
    "evaluate_blocks",
    "evaluate_chunk_embeddings",
    "evaluate_layout_signals",
    "evaluate_parse_quality",
    "evaluate_projected_parse_quality",
    "diff_reports",
]


@dataclass(slots=True)
class ParseQualitySummary:
    """High-level quality summary surfaced in API responses.

    ``score``  – float in [0, 1]; 1.0 means no detected quality issues.
    ``flags``  – set of short issue codes (e.g. ``cid_garble``, ``ocr_failed``).
    ``warnings`` – human-readable list, safe to return to API consumers.
    ``recommended_action`` – optional hint such as ``retry_with_ocr``.
    ``total_cid_tokens`` – raw count of ``(cid:N)`` tokens across all blocks.
    """

    score: float
    flags: frozenset[str]
    warnings: tuple[str, ...]
    recommended_action: str | None
    total_cid_tokens: int
    total_pdf_name_tokens: int
    ocr_failed_pages: int
    suspect_signature_pages: int


def _evaluate_text_quality(
    texts: Iterable[str],
    *,
    ocr_failed_pages: int,
    ocr_fallback_detected: bool,
    suspect_signature_pages: int,
    empty_output: bool,
    docx_single_page: bool,
) -> ParseQualitySummary:
    signals = analyze_text_fragments_garble(texts)

    flags: set[str] = set()
    warnings: list[str] = []
    penalty = 0.0

    if signals.total_cid_tokens >= _CID_TOTAL_GATE_TOKENS:
        flags.add("cid_garble")
        warnings.append(
            f"Document contains {signals.total_cid_tokens} CID tokens (≥{_CID_TOTAL_GATE_TOKENS}); "
            "text extraction is likely incomplete or garbled."
        )
        penalty += 0.35
    elif signals.total_cid_tokens >= _CID_TOTAL_WARN_TOKENS:
        flags.add("cid_warn")
        warnings.append(
            f"Document contains {signals.total_cid_tokens} CID tokens; "
            "some text may be garbled."
        )
        penalty += 0.15

    if signals.total_pdf_name_tokens >= _PDF_NAME_TOTAL_GATE_TOKENS:
        flags.add("pdf_name_garble")
        warnings.append(
            f"Document contains {signals.total_pdf_name_tokens} PDF name-map tokens (≥{_PDF_NAME_TOTAL_GATE_TOKENS}); "
            "text may be parser-encoded garbage."
        )
        penalty += 0.25
    elif signals.total_pdf_name_tokens >= _PDF_NAME_TOTAL_WARN_TOKENS:
        flags.add("pdf_name_warn")
        warnings.append(
            f"Document contains {signals.total_pdf_name_tokens} PDF name-map tokens; "
            "some text may be parser-encoded garbage."
        )
        penalty += 0.1

    control_char_ratio = (
        signals.control_char_count / max(signals.total_chars, 1)
        if signals.total_chars > 0
        else 0.0
    )
    if control_char_ratio >= _MAX_CONTROL_CHAR_RATIO:
        flags.add("control_char_dense")
        warnings.append("Document contains dense control characters; text stream may be corrupted.")
        penalty += 0.1

    if signals.total_chars > 0 and signals.printable_ratio < _MIN_PRINTABLE_RATIO:
        flags.add("low_printable_ratio")
        warnings.append("Document has a low printable-character ratio; text may be partially unreadable.")
        penalty += 0.1

    if signals.repeated_short_token_ratio >= _SHORT_TOKEN_REPEAT_GATE:
        flags.add("repeated_short_tokens")
        warnings.append("Document has repeated short tokens; likely OCR or encoding artifacts.")
        penalty += 0.05

    if ocr_failed_pages > 0:
        flags.add("ocr_failed")
        warnings.append(
            f"OCR failed on {ocr_failed_pages} page(s); "
            "affected pages may have missing text."
        )
        penalty += 0.1 * min(ocr_failed_pages, 3)

    if suspect_signature_pages >= 5:
        flags.add("suspect_signature_overload")
        warnings.append(
            f"{suspect_signature_pages} pages classified as signature, which is unusually high. "
            "Review page-type classification."
        )
        penalty += 0.1

    if empty_output:
        flags.add("empty_output")
        warnings.append("Parser produced no content blocks.")
        penalty += 0.5

    if docx_single_page:
        flags.add("docx_single_page")
        warnings.append(
            "Document was collapsed into a single page; "
            "logical section/page splitting may be needed."
        )
        penalty += 0.05

    score = max(0.0, round(1.0 - penalty, 4))

    severe_output_garble = bool(
        {"cid_garble", "pdf_name_garble", "control_char_dense", "low_printable_ratio"}.intersection(flags)
    )

    recommended_action: str | None = None
    if severe_output_garble and "ocr_failed" not in flags and not ocr_fallback_detected:
        recommended_action = "retry_with_ocr"
    elif "docx_single_page" in flags:
        recommended_action = "retry_with_vector_refine"
    elif "suspect_signature_overload" in flags:
        recommended_action = "retry_with_llm_refine"

    return ParseQualitySummary(
        score=score,
        flags=frozenset(flags),
        warnings=tuple(warnings),
        recommended_action=recommended_action,
        total_cid_tokens=signals.total_cid_tokens,
        total_pdf_name_tokens=signals.total_pdf_name_tokens,
        ocr_failed_pages=ocr_failed_pages,
        suspect_signature_pages=suspect_signature_pages,
    )


def evaluate_parse_quality(blocks: Iterable[Block]) -> ParseQualitySummary:
    """Derive a ``ParseQualitySummary`` from raw block output."""

    blocks_list = list(blocks)
    ocr_failed_pages: set[int] = set()
    ocr_fallback_pages: set[int] = set()
    page_signature_votes: dict[int, int] = {}
    page_block_counts: dict[int, int] = {}
    texts: list[str] = []

    for block in blocks_list:
        content = block.content or ""
        texts.append(content)

        meta = block.metadata or {}
        page_number = int(meta.get("page", 1))
        page_block_counts[page_number] = page_block_counts.get(page_number, 0) + 1

        if meta.get("ocr_error_reason"):
            ocr_failed_pages.add(page_number)
        if meta.get("ocr_fallback_used"):
            ocr_fallback_pages.add(page_number)

        role = str(meta.get("semantic_role") or "")
        page_type = str(meta.get("page_type") or "")
        if role == "signature" or page_type == "signature":
            page_signature_votes[page_number] = page_signature_votes.get(page_number, 0) + 1

    # Signature-overload: pages where every (or almost every) block is signature
    suspect_signature_pages = sum(
        1 for pn, votes in page_signature_votes.items()
        if page_block_counts.get(pn, 1) > 0
        and votes / page_block_counts[pn] >= 0.5
    )

    return _evaluate_text_quality(
        texts,
        ocr_failed_pages=len(ocr_failed_pages),
        ocr_fallback_detected=bool(ocr_fallback_pages),
        suspect_signature_pages=suspect_signature_pages,
        empty_output=not blocks_list,
        docx_single_page=(len(page_block_counts) == 1 and 1 in page_block_counts and page_block_counts[1] > 20),
    )


def evaluate_projected_parse_quality(pages: Sequence[dict[str, object]]) -> ParseQualitySummary:
    texts = [str(page.get("text") or "") for page in pages]
    return _evaluate_text_quality(
        texts,
        ocr_failed_pages=0,
        ocr_fallback_detected=False,
        suspect_signature_pages=0,
        empty_output=(len(pages) == 0),
        docx_single_page=False,
    )


def _summarize_pages(pages: Sequence[PageQuality]) -> list[dict[str, float | int]]:
    """Helper exposed for ad-hoc CLI usage; not part of the public API."""

    return [
        {
            "page_number": page.page_number,
            "block_count": page.block_count,
            "median_length": page.median_length,
            "very_short_ratio": page.very_short_ratio,
            "suspected_header_footer": page.suspected_header_footer,
            "numeric_heavy": page.numeric_heavy,
            "max_length": page.max_length,
        }
        for page in pages
    ]
