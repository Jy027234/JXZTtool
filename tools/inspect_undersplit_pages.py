"""Inspect under-split pages: compare pypdf raw text vs legacy display blocks.

For a list of page numbers, print:
  * pypdf extract_text raw, with line-break markers
  * paragraph split result under current rules
  * legacy display-block snippet (from latest compare report) for contrast
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from parsecore.parsers import (  # noqa: E402
    _load_pdf_reader,
    _merge_short_blocks,
    _split_pdf_page_text,
    _strip_repeated_headers_footers,
)


def show_page(pdf: Path, page_no: int) -> dict:
    PdfReader = _load_pdf_reader()
    reader = PdfReader(str(pdf))
    texts = [p.extract_text() or "" for p in reader.pages]
    cleaned = _strip_repeated_headers_footers(texts)
    raw = cleaned[page_no - 1]
    lines = raw.split("\n")
    paragraphs = _merge_short_blocks(_split_pdf_page_text(raw))
    return {
        "page": page_no,
        "raw_len": len(raw),
        "line_count": len(lines),
        "non_empty_line_count": sum(1 for ln in lines if ln.strip()),
        "blank_line_count": sum(1 for ln in lines if not ln.strip()),
        "paragraph_count": len(paragraphs),
        "first_20_lines": lines[:20],
        "last_10_lines": lines[-10:],
        "paragraph_preview": [p[:120] for p in paragraphs[:8]],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--pages", nargs="+", type=int, required=True)
    args = parser.parse_args()
    out = {"pdf": args.file, "pages": [show_page(Path(args.file), p) for p in args.pages]}
    out_path = REPO_ROOT / "tools" / "undersplit_inspection.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {out_path}")


if __name__ == "__main__":
    main()
