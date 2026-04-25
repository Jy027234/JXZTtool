"""PyMuPDF A/B evaluation against the baseline PDF.

Computes structure-quality metrics comparable to parsecore_compare._compute_side_structure_stats
so we can objectively compare three PDF engines on the same file:
  - legacy (pdfplumber-style, from dual-run report)
  - ParseCore current (pypdf)
  - PyMuPDF (this script)

Usage:
  python tools/pymupdf_eval.py [--pdf PATH] [--baseline PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

DEFAULT_PDF = r"d:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf"
DEFAULT_BASELINE = r"D:\个人文件\个人开发\jobcard\backend\data\parsecore_dual_run_report_direct_file.json"


def _split_pdf_page_text(text: str) -> list[str]:
    """Mirror of parsecore.parsers._split_pdf_page_text (blank-line paragraph split)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in normalized.split("\n"):
        if not raw_line.strip():
            if current:
                paragraphs.append("\n".join(current))
                current = []
            continue
        current.append(raw_line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def _strip_repeated_headers_footers(pages: list[str], threshold: float = 0.5) -> list[str]:
    """Mirror of jobcard pdf_parser._strip_repeated_headers_footers."""
    if len(pages) < 3:
        return pages
    HEAD_N = 3
    TAIL_N = 3
    MIN_LINE_LEN = 4
    non_empty_count = sum(1 for p in pages if p.strip())
    if non_empty_count < 3:
        return pages
    min_count = max(2, int(non_empty_count * threshold))
    head_counter: dict[str, int] = {}
    tail_counter: dict[str, int] = {}
    for page_text in pages:
        if not page_text.strip():
            continue
        lines = [l.strip() for l in page_text.split("\n") if l.strip()]
        if not lines:
            continue
        for line in set(lines[:HEAD_N]):
            if len(line) >= MIN_LINE_LEN:
                head_counter[line] = head_counter.get(line, 0) + 1
        for line in set(lines[-TAIL_N:]):
            if len(line) >= MIN_LINE_LEN:
                tail_counter[line] = tail_counter.get(line, 0) + 1
    hf_lines: set[str] = set()
    for line, count in head_counter.items():
        if count >= min_count:
            hf_lines.add(line)
    for line, count in tail_counter.items():
        if count >= min_count:
            hf_lines.add(line)
    if not hf_lines:
        return pages
    cleaned: list[str] = []
    for page_text in pages:
        lines = page_text.split("\n")
        filtered = [l for l in lines if l.strip() not in hf_lines]
        cleaned.append("\n".join(filtered).strip())
    return cleaned


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(quantile * (len(ordered) - 1)))))
    return ordered[idx]


def _looks_table_like(text: str) -> bool:
    value = str(text or "")
    if not value.strip():
        return False
    lines = [line for line in value.splitlines() if line.strip()]
    if not lines:
        return False
    pipe_heavy = sum(1 for line in lines if line.count("|") >= 2)
    tab_heavy = sum(1 for line in lines if line.count("\t") >= 2)
    if pipe_heavy >= max(2, len(lines) // 2):
        return True
    if tab_heavy >= max(2, len(lines) // 2):
        return True
    multi_col = sum(
        1 for line in lines
        if len(re.split(r"\s{2,}", line.strip())) >= 3 and len(line.strip()) < 120
    )
    return multi_col >= max(3, len(lines) // 2)


def _looks_numeric_heavy(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) < 3:
        return False
    digits = sum(1 for ch in stripped if ch.isdigit())
    letters = sum(1 for ch in stripped if ch.isalpha())
    if digits == 0:
        return False
    return digits >= max(4, len(stripped) // 2) and digits > letters


def _looks_heading_like(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > 80 or "\n" in stripped:
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    if upper_ratio >= 0.8 and len(stripped) >= 3:
        return True
    if re.match(r"^(\d+(\.\d+)*[.、)]|第[\d一二三四五六七八九十百]+[章节条款])\s", stripped):
        return True
    return False


def _compute_stats(raw_blocks: list[dict[str, Any]], display_block_count: int) -> dict[str, Any]:
    texts = [str(b.get("text") or "") for b in raw_blocks]
    non_empty = [t for t in texts if t.strip()]
    lengths = [len(t) for t in non_empty]
    total = len(raw_blocks)
    ne = len(non_empty)
    very_short = sum(1 for t in non_empty if len(t.strip()) < 10)
    very_long = sum(1 for l in lengths if l > 2000)
    single_line = sum(1 for t in non_empty if "\n" not in t)
    table_like = sum(1 for t in non_empty if _looks_table_like(t))
    numeric_heavy = sum(1 for t in non_empty if _looks_numeric_heavy(t))
    heading_like = sum(1 for t in non_empty if _looks_heading_like(t))
    suspected_hf = max(0, total - display_block_count)
    return {
        "total_block_count": total,
        "non_empty_block_count": ne,
        "average_length": round(mean(lengths), 2) if lengths else 0.0,
        "median_length": int(median(lengths)) if lengths else 0,
        "p90_length": _percentile(lengths, 0.9),
        "max_length": max(lengths, default=0),
        "very_short_block_count": very_short,
        "very_short_block_ratio": round(very_short / ne, 4) if ne else 0.0,
        "very_long_block_count": very_long,
        "very_long_block_ratio": round(very_long / ne, 4) if ne else 0.0,
        "single_line_block_count": single_line,
        "table_like_block_count": table_like,
        "numeric_heavy_block_count": numeric_heavy,
        "all_caps_short_block_count": heading_like,
        "suspected_header_footer_block_count": suspected_hf,
    }


def _extract_with_pymupdf(pdf_path: str) -> list[tuple[int, str]]:
    import fitz  # type: ignore
    doc = fitz.open(pdf_path)
    out: list[tuple[int, str]] = []
    try:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            out.append((i, text))
    finally:
        doc.close()
    return out


def _extract_with_pdfplumber(pdf_path: str) -> list[tuple[int, str]]:
    import pdfplumber  # type: ignore
    out: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as doc:
        for i, page in enumerate(doc.pages, start=1):
            text = page.extract_text() or ""
            out.append((i, text))
    return out


def _extract_with_pypdf(pdf_path: str) -> list[tuple[int, str]]:
    from pypdf import PdfReader  # type: ignore
    reader = PdfReader(pdf_path)
    return [(i, page.extract_text() or "") for i, page in enumerate(reader.pages, start=1)]


def _extract_with_pymupdf_blocks(pdf_path: str) -> list[tuple[int, list[str]]]:
    """Alternative: use fitz block extraction (returns (x0,y0,x1,y1,text,block_no,block_type))."""
    import fitz  # type: ignore
    doc = fitz.open(pdf_path)
    out: list[tuple[int, list[str]]] = []
    try:
        for i, page in enumerate(doc, start=1):
            blocks = page.get_text("blocks") or []
            # text blocks only (block_type 0); sort by (y0, x0)
            text_blocks = [b for b in blocks if len(b) >= 6 and (len(b) < 7 or b[6] == 0)]
            text_blocks.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))
            texts = [str(b[4]).strip() for b in text_blocks if str(b[4]).strip()]
            out.append((i, texts))
    finally:
        doc.close()
    return out


def _run_engine(
    label: str,
    extractor,
    pdf_path: str,
    gap_pages: list[int],
) -> tuple[dict[str, Any] | None, dict[int, int]]:
    """Run an engine, apply blank-line paragraph split + HF-strip, return (stats, per_page_counts)."""
    try:
        per_page_text = extractor(pdf_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] engine {label} failed: {exc}", file=sys.stderr)
        return None, {}
    page_texts = [t for _, t in per_page_text]
    raw_blocks: list[dict[str, Any]] = []
    per_page_counts: dict[int, int] = {}
    for page_no, text in per_page_text:
        paragraphs = _split_pdf_page_text(text)
        per_page_counts[page_no] = len(paragraphs)
        for p in paragraphs:
            raw_blocks.append({"page_number": page_no, "text": p})
    display_pages = _strip_repeated_headers_footers(page_texts)
    display_count = 0
    for text in display_pages:
        display_count += len(_split_pdf_page_text(text))
    stats = _compute_stats(raw_blocks, display_count)
    return stats, per_page_counts


def evaluate(pdf_path: str, baseline_path: str | None) -> dict[str, Any]:
    gap_pages_of_interest = [23, 24, 25, 78, 80]

    # Engines that may or may not be available
    pypdf_stats, pypdf_pp = _run_engine("pypdf", _extract_with_pypdf, pdf_path, gap_pages_of_interest)
    pdfplumber_stats, pdfplumber_pp = _run_engine(
        "pdfplumber", _extract_with_pdfplumber, pdf_path, gap_pages_of_interest
    )
    pymupdf_stats, pymupdf_pp = _run_engine(
        "pymupdf", _extract_with_pymupdf, pdf_path, gap_pages_of_interest
    )
    pymupdf_native_stats: dict[str, Any] | None = None
    pymupdf_native_pp: dict[int, int] = {}
    try:
        per_page_blocks = _extract_with_pymupdf_blocks(pdf_path)
        raw_native: list[dict[str, Any]] = []
        for page_no, blocks in per_page_blocks:
            pymupdf_native_pp[page_no] = len(blocks)
            for b in blocks:
                raw_native.append({"page_number": page_no, "text": b})
        page_count = len(per_page_blocks)
        repeated_counter: dict[str, int] = {}
        for _, blocks in per_page_blocks:
            for b in set(blocks):
                if len(b) <= 120:
                    repeated_counter[b] = repeated_counter.get(b, 0) + 1
        min_rep = max(2, int(page_count * 0.5))
        hf_blocks = {b for b, c in repeated_counter.items() if c >= min_rep}
        display_native = sum(1 for b in raw_native if b["text"] not in hf_blocks)
        pymupdf_native_stats = _compute_stats(raw_native, display_native)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] pymupdf native-blocks engine failed: {exc}", file=sys.stderr)

    # --- Load baseline from jobcard report ---
    baseline: dict[str, Any] = {}
    if baseline_path and Path(baseline_path).exists():
        try:
            rpt = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
            comp = rpt["items"][0]["comparison"]
            sq = comp.get("structure_quality_summary", {})
            baseline = {
                "legacy_side": sq.get("legacy_side"),
                "parsecore_side": sq.get("parsecore_side"),
                "deltas": sq.get("deltas"),
                "average_similarity": rpt.get("summary", {}).get("average_similarity"),
                "gap_page_samples": comp.get("gap_page_samples"),
            }
        except Exception as exc:  # noqa: BLE001
            baseline = {"error": f"failed to load baseline: {exc}"}

    per_page_comparison: list[dict[str, Any]] = []
    sample_map: dict[int, Any] = {}
    if baseline and isinstance(baseline.get("gap_page_samples"), dict):
        sample_map = {s["page_number"]: s for s in baseline["gap_page_samples"].get("samples", [])}
    for pg in gap_pages_of_interest:
        row: dict[str, Any] = {"page_number": pg}
        if pg in sample_map:
            row["legacy_block_count"] = sample_map[pg].get("legacy_block_count")
            row["parsecore_pypdf_block_count_baseline"] = sample_map[pg].get(
                "parsecore_block_count"
            )
        row["pypdf_block_count"] = pypdf_pp.get(pg)
        row["pdfplumber_block_count"] = pdfplumber_pp.get(pg)
        row["pymupdf_paragraph_block_count"] = pymupdf_pp.get(pg)
        row["pymupdf_native_block_count"] = pymupdf_native_pp.get(pg)
        per_page_comparison.append(row)

    return {
        "pdf_path": pdf_path,
        "engine_pypdf_paragraph_split": pypdf_stats,
        "engine_pdfplumber_paragraph_split": pdfplumber_stats,
        "engine_pymupdf_paragraph_split": pymupdf_stats,
        "engine_pymupdf_native_blocks": pymupdf_native_stats,
        "baseline_legacy_side": baseline.get("legacy_side"),
        "baseline_parsecore_side": baseline.get("parsecore_side"),
        "per_page_comparison_on_gap_pages": per_page_comparison,
    }


def _print_report(result: dict[str, Any]) -> None:
    print("=" * 90)
    print(f"PDF: {result['pdf_path']}")
    print("=" * 90)

    def _row(label: str, stats: dict[str, Any] | None) -> None:
        if not stats:
            print(f"{label:42s} n/a (unavailable)")
            return
        print(
            f"{label:42s} total={stats.get('total_block_count'):>5} "
            f"median={stats.get('median_length'):>4} "
            f"p90={stats.get('p90_length'):>5} "
            f"max={stats.get('max_length'):>5} "
            f"vshort%={stats.get('very_short_block_ratio'):.4f} "
            f"vlong={stats.get('very_long_block_count'):>3} "
            f"tblike={stats.get('table_like_block_count'):>3} "
            f"hf={stats.get('suspected_header_footer_block_count'):>3}"
        )

    _row("jobcard legacy (pdfplumber+own split)", result.get("baseline_legacy_side"))
    _row("jobcard ParseCore path (pypdf)", result.get("baseline_parsecore_side"))
    _row("local pypdf + paragraph split", result.get("engine_pypdf_paragraph_split"))
    _row("local pdfplumber + paragraph split", result.get("engine_pdfplumber_paragraph_split"))
    _row("local pymupdf + paragraph split", result.get("engine_pymupdf_paragraph_split"))
    _row("local pymupdf + native blocks", result.get("engine_pymupdf_native_blocks"))

    print("\nPer-page block counts on known gap pages:")
    for row in result.get("per_page_comparison_on_gap_pages", []):
        print(
            f"  p{row.get('page_number'):>3}: "
            f"legacy={row.get('legacy_block_count')} "
            f"baseline_pypdf={row.get('parsecore_pypdf_block_count_baseline')} "
            f"pypdf={row.get('pypdf_block_count')} "
            f"pdfplumber={row.get('pdfplumber_block_count')} "
            f"pymu_para={row.get('pymupdf_paragraph_block_count')} "
            f"pymu_native={row.get('pymupdf_native_block_count')}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--json", action="store_true", help="also emit raw JSON")
    ap.add_argument("--out", default=None, help="write JSON to this path")
    args = ap.parse_args()

    if not Path(args.pdf).exists():
        print(f"ERROR: pdf not found: {args.pdf}", file=sys.stderr)
        return 2

    result = evaluate(args.pdf, args.baseline)
    _print_report(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
