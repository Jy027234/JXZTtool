"""Sensitivity scan of _merge_short_blocks min_length on a real PDF.

Runs the existing HF-strip + page split once, then re-applies merge at varying
min_length thresholds and reports the resulting block-count distribution plus TOC
page integrity (pages 23/25) so we can pick a default that keeps structure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from parsecore.parsers import (  # noqa: E402
    _merge_short_blocks,
    _split_pdf_page_text,
    _split_structural_items,
    _strip_repeated_headers_footers,
    _load_pdf_reader,
)


def _looks_numeric_heavy(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    digits = sum(1 for ch in stripped if ch.isdigit())
    return digits / max(len(stripped), 1) >= 0.6


def scan(pdf_path: Path, thresholds: list[int]) -> dict:
    PdfReader = _load_pdf_reader()
    reader = PdfReader(str(pdf_path))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    cleaned = _strip_repeated_headers_footers(page_texts)

    page_paragraphs_raw = [
        _split_structural_items(_split_pdf_page_text(pt)) for pt in cleaned
    ]

    results: list[dict] = []
    for min_len in thresholds:
        per_page_counts: list[int] = []
        all_blocks: list[str] = []
        for paragraphs in page_paragraphs_raw:
            merged = _merge_short_blocks(paragraphs, min_length=min_len)
            per_page_counts.append(len(merged))
            all_blocks.extend(merged)

        lengths = [len(b) for b in all_blocks]
        very_short = sum(1 for ln in lengths if ln < 10)
        numeric_heavy = sum(1 for b in all_blocks if _looks_numeric_heavy(b))
        results.append({
            "min_length": min_len,
            "total_blocks": len(all_blocks),
            "median_length": int(median(lengths)) if lengths else 0,
            "avg_length": round(sum(lengths) / len(lengths), 2) if lengths else 0,
            "very_short_count": very_short,
            "very_short_ratio": round(very_short / max(len(all_blocks), 1), 4),
            "numeric_heavy_count": numeric_heavy,
            "toc_p23_blocks": per_page_counts[22] if len(per_page_counts) > 22 else None,
            "toc_p25_blocks": per_page_counts[24] if len(per_page_counts) > 24 else None,
            "under_p125_blocks": per_page_counts[124] if len(per_page_counts) > 124 else None,
            "under_p206_blocks": per_page_counts[205] if len(per_page_counts) > 205 else None,
            "under_p216_blocks": per_page_counts[215] if len(per_page_counts) > 215 else None,
            "under_p223_blocks": per_page_counts[222] if len(per_page_counts) > 222 else None,
        })
    return {
        "pdf": str(pdf_path),
        "total_pages": len(page_texts),
        "hf_cleaned": True,
        "scan": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument(
        "--thresholds",
        nargs="*",
        type=int,
        default=[0, 5, 8, 10, 15, 20, 30],
    )
    args = parser.parse_args()
    out = scan(Path(args.file), args.thresholds)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
