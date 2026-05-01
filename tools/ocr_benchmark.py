"""Benchmark OCR-heavy PDF parsing and layout-side OCR signals."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.ocr_trace import build_ocr_decision_trace, ocr_decision_trace_payload  # noqa: E402
from parsecore.quality import evaluate_blocks, evaluate_layout_signals  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OCR benchmark samples")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--pdf", action="append", required=True, help="PDF path to benchmark")
    parser.add_argument("--out", help="Optional JSON output path")
    parser.add_argument("--top-pages", type=int, default=5)
    parser.add_argument("--no-enable-ocr", action="store_true")
    parser.add_argument("--fail-on-ocr-errors", action="store_true")
    return parser


def _round(value: float) -> float:
    return round(float(value), 3)


def _structural_summary(blocks: Any) -> dict[str, Any]:
    report = evaluate_blocks(blocks)
    return {
        "total_blocks": report.total_blocks,
        "page_count": report.page_count,
        "median_block_length": report.median_block_length,
        "very_short_ratio": report.very_short_ratio,
        "suspected_header_footer_total": report.suspected_header_footer_total,
        "numeric_heavy_total": report.numeric_heavy_total,
        "long_block_total": report.long_block_total,
        "noisy_pages": [asdict(page) for page in report.noisy_pages(top=5)],
    }


def _run_one(
    *,
    runtime: Any,
    pdf_path: Path,
    index: int,
    top_pages: int,
    enable_ocr: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        outcome = runtime.submit(
            ParseRequest(
                doc_id=f"ocr-bench-{index}-{pdf_path.stem}",
                file_path=str(pdf_path),
                media_type="application/pdf",
                options={"enable_ocr": enable_ocr},
                tenant_id="benchmark",
                quota_key="ocr",
                quota_units=1,
            )
        )
    except Exception as exc:
        return {
            "document": str(pdf_path),
            "status": "failed",
            "elapsed_s": _round(time.monotonic() - started),
            "error": str(exc),
        }

    elapsed_s = _round(time.monotonic() - started)
    layout = evaluate_layout_signals(outcome.blocks)
    ocr_trace = build_ocr_decision_trace(outcome.blocks)
    layout_payload = asdict(layout)
    layout_payload["ocr_hot_pages"] = [
        asdict(page) for page in layout.ocr_hot_pages(top=top_pages)
    ]
    layout_payload["ocr_sparse_cls_pages"] = [
        asdict(page) for page in layout.ocr_sparse_cls_pages(top=top_pages)
    ]
    return {
        "document": str(pdf_path),
        "status": outcome.job.state.value,
        "elapsed_s": elapsed_s,
        "blocks": len(outcome.blocks),
        "chunks": len(outcome.chunks),
        "structural_quality": _structural_summary(outcome.blocks),
        "layout_signals": layout_payload,
        "ocr_decision_trace": ocr_decision_trace_payload(ocr_trace),
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    layout_items = [
        item.get("layout_signals", {})
        for item in results
        if isinstance(item.get("layout_signals"), dict)
    ]
    trace_items = [
        item.get("ocr_decision_trace", {})
        for item in results
        if isinstance(item.get("ocr_decision_trace"), dict)
    ]
    return {
        "documents": len(results),
        "failed_documents": sum(1 for item in results if item.get("status") == "failed"),
        "elapsed_s": _round(sum(float(item.get("elapsed_s", 0.0)) for item in results)),
        "ocr_attempted_pages": (
            sum(int(item.get("ocr_attempted_pages", 0)) for item in trace_items)
            if trace_items
            else sum(int(item.get("ocr_attempted_pages", 0)) for item in layout_items)
        ),
        "ocr_fallback_pages": (
            sum(int(item.get("ocr_fallback_pages", 0)) for item in trace_items)
            if trace_items
            else sum(int(item.get("ocr_fallback_pages", 0)) for item in layout_items)
        ),
        "ocr_rejected_pages": sum(int(item.get("ocr_rejected_pages", 0)) for item in trace_items),
        "ocr_failed_pages": (
            sum(int(item.get("ocr_failed_pages", 0)) for item in trace_items)
            if trace_items
            else sum(int(item.get("ocr_failed_pages", 0)) for item in layout_items)
        ),
        "native_text_token_count": sum(int(item.get("native_text_token_count", 0)) for item in trace_items),
        "final_text_token_count": sum(int(item.get("final_text_token_count", 0)) for item in trace_items),
        "ocr_total_elapsed_s": _round(
            sum(float(item.get("ocr_total_elapsed_s", 0.0)) for item in layout_items)
        ),
        "max_ocr_page_elapsed_s": _round(
            max((float(item.get("max_ocr_page_elapsed_s", 0.0)) for item in layout_items), default=0.0)
        ),
    }


def _close_runtime(runtime: Any) -> None:
    for resource_name in ("job_store", "index"):
        resource = getattr(runtime, resource_name, None)
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    pdf_paths = [Path(item).resolve() for item in args.pdf]
    missing = [str(path) for path in pdf_paths if not path.exists()]
    if missing:
        print(json.dumps({"status": "failed", "missing": missing}, ensure_ascii=False, indent=2))
        return 1

    runtime = build_runtime(args.config)
    try:
        results = [
            _run_one(
                runtime=runtime,
                pdf_path=pdf_path,
                index=index,
                top_pages=max(1, args.top_pages),
                enable_ocr=not args.no_enable_ocr,
            )
            for index, pdf_path in enumerate(pdf_paths, start=1)
        ]
    finally:
        _close_runtime(runtime)

    payload = {
        "status": "ok",
        "config": str(Path(args.config).resolve()),
        "summary": _summary(results),
        "results": results,
    }
    if args.fail_on_ocr_errors and payload["summary"]["ocr_failed_pages"] > 0:
        payload["status"] = "failed"
    if any(item.get("status") == "failed" for item in results):
        payload["status"] = "failed"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[ocr-benchmark] wrote {output_path}")
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
