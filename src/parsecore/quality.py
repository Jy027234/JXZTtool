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
from typing import Iterable, Sequence

from .models import Block


_HEADER_FOOTER_MAX_LEN = 80
_VERY_SHORT_LEN = 10
_NUMERIC_HEAVY_RATIO = 0.6
_LONG_BLOCK_LEN = 2000


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
class LayoutSignalsReport:
    pages_with_layout_metadata: int
    multi_column_pages: int
    header_footer_stripped_pages: int
    header_footer_stripped_blocks: int
    ocr_fallback_pages: int
    ocr_fallback_blocks: int


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
    ocr_fallback_pages: set[int] = set()
    ocr_fallback_blocks = 0

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
        if bool(metadata.get("ocr_fallback_used")):
            ocr_fallback_pages.add(page_number)
            ocr_fallback_blocks += 1

    return LayoutSignalsReport(
        pages_with_layout_metadata=len(pages_with_layout_metadata),
        multi_column_pages=len(multi_column_pages),
        header_footer_stripped_pages=len(header_footer_stripped_pages),
        header_footer_stripped_blocks=header_footer_stripped_blocks,
        ocr_fallback_pages=len(ocr_fallback_pages),
        ocr_fallback_blocks=ocr_fallback_blocks,
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
    "LayoutSignalsReport",
    "PageQuality",
    "StructuralQualityReport",
    "evaluate_blocks",
    "evaluate_layout_signals",
    "diff_reports",
]


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
