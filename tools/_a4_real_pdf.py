"""End-to-end smoke for A4: parse the real test PDF with LLM boundary refining.

Run:
    set PARSECORE_LLM_API_KEY=...; python tools/_a4_real_pdf.py
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.config import load_settings  # noqa: E402
from parsecore.llm import LlmBoundaryRefiner, build_llm_client  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.parsers import build_parser  # noqa: E402
from parsecore.quality import evaluate_blocks  # noqa: E402


def main() -> int:
    settings = load_settings(ROOT / "parsecore.toml")
    overridden = dataclasses.replace(settings.providers.llm, enabled=True)
    client = build_llm_client(overridden)
    refiner = LlmBoundaryRefiner(client, max_calls_per_doc=50) if client else None

    pdf_cfg = next(p for p in settings.parsers if p.name == "pdf-text")
    parser = build_parser(
        "pdf-text",
        media_types=pdf_cfg.media_types,
        extensions=pdf_cfg.extensions,
        options=pdf_cfg.options,
        boundary_refiner=refiner,
    )
    request = ParseRequest(
        doc_id="a4-real",
        file_path=r"D:\app\uploads\36d65cd6b61346e28e97dbaf829646de.pdf",
        media_type="application/pdf",
    )
    started = time.time()
    blocks = parser.parse(request)
    elapsed = time.time() - started

    table_blocks = [b for b in blocks if b.type.value == "table"]
    para_blocks = [b for b in blocks if b.type.value == "paragraph"]
    print(f"elapsed={elapsed:.1f}s blocks={len(blocks)} paragraphs={len(para_blocks)} tables={len(table_blocks)}")
    if refiner is not None:
        print(f"llm_calls_used={refiner.calls_used} llm_calls_failed={refiner.calls_failed}")

    report = evaluate_blocks(blocks)
    print(
        "self_check: median_len=%.1f very_short_ratio=%.4f hf=%d num_heavy=%d long_blocks=%d"
        % (
            report.median_block_length,
            report.very_short_ratio,
            report.suspected_header_footer_total,
            report.numeric_heavy_total,
            report.long_block_total,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
