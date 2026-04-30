"""Capture lightweight ParseCore parsing performance baselines."""

from __future__ import annotations

import argparse
import json
import mimetypes
import statistics
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import BlockType, ParseRequest  # noqa: E402


MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}

DEFAULT_EXTENSIONS = (".xls", ".xlsx", ".xlsm")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture ParseCore parse performance baselines")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--sample-dir", default="D:/app/uploads")
    parser.add_argument("--sample", action="append", help="Explicit sample file; can be repeated")
    parser.add_argument(
        "--extensions",
        default=",".join(DEFAULT_EXTENSIONS),
        help="Comma-separated extensions to scan when --sample is not supplied",
    )
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--out-json", help="Optional JSON output path")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument("--fail-on-errors", action="store_true")
    return parser


def _round(value: float) -> float:
    return round(float(value), 3)


def _parse_extensions(value: str) -> set[str]:
    result: set[str] = set()
    for item in value.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        result.add(normalized if normalized.startswith(".") else f".{normalized}")
    return result or set(DEFAULT_EXTENSIONS)


def _discover_samples(*, sample_dir: Path, extensions: set[str], max_files: int) -> list[Path]:
    if not sample_dir.exists():
        return []
    return sorted(
        path
        for path in sample_dir.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    )[: max(1, max_files)]


def _media_type_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in MEDIA_TYPES:
        return MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _run_one(*, runtime: Any, path: Path, index: int) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    media_type = _media_type_for(path)
    started = time.perf_counter()
    tracemalloc.start()
    try:
        outcome = runtime.submit(
            ParseRequest(
                doc_id=f"perf-sample-{index}",
                file_path=str(path),
                media_type=media_type,
                tenant_id="perf-baseline",
                quota_key="parse",
                quota_units=1,
            )
        )
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    except Exception as exc:
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        elapsed_s = _round(time.perf_counter() - started)
        tracemalloc.stop()
        return {
            "document": str(path),
            "file_name": path.name,
            "suffix": path.suffix.lower(),
            "status": "failed",
            "media_type": media_type,
            "size_bytes": size_bytes,
            "elapsed_s": elapsed_s,
            "peak_kb": _round(peak_bytes / 1024),
            "error": str(exc),
        }
    elapsed_s = _round(time.perf_counter() - started)
    tracemalloc.stop()
    table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
    return {
        "document": str(path),
        "file_name": path.name,
        "suffix": path.suffix.lower(),
        "status": outcome.job.state.value,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "elapsed_s": elapsed_s,
        "peak_kb": _round(peak_bytes / 1024),
        "mb_per_s": _round((size_bytes / 1048576) / elapsed_s) if elapsed_s > 0 else 0.0,
        "blocks": len(outcome.blocks),
        "chunks": len(outcome.chunks),
        "tables": len(table_blocks),
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed_values = [
        float(item.get("elapsed_s", 0.0))
        for item in results
        if item.get("status") != "failed"
    ]
    slowest = max(results, key=lambda item: float(item.get("elapsed_s", 0.0)), default={})
    return {
        "documents": len(results),
        "failed_documents": sum(1 for item in results if item.get("status") == "failed"),
        "total_elapsed_s": _round(sum(float(item.get("elapsed_s", 0.0)) for item in results)),
        "median_elapsed_s": _round(statistics.median(elapsed_values)) if elapsed_values else 0.0,
        "max_elapsed_s": _round(max(elapsed_values, default=0.0)),
        "max_peak_kb": _round(max((float(item.get("peak_kb", 0.0)) for item in results), default=0.0)),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in results),
        "total_blocks": sum(int(item.get("blocks") or 0) for item in results),
        "total_chunks": sum(int(item.get("chunks") or 0) for item in results),
        "total_tables": sum(int(item.get("tables") or 0) for item in results),
        "slowest_document": slowest.get("file_name"),
    }


def _close_runtime(runtime: Any) -> None:
    for resource_name in ("job_store", "index"):
        resource = getattr(runtime, resource_name, None)
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def build_report(
    *,
    config: str | Path,
    sample_dir: str | Path,
    samples: list[str | Path] | None = None,
    extensions: set[str] | None = None,
    max_files: int = 20,
) -> dict[str, Any]:
    sample_root = Path(sample_dir)
    paths = [Path(item) for item in samples or []]
    if not paths:
        paths = _discover_samples(
            sample_dir=sample_root,
            extensions=extensions or set(DEFAULT_EXTENSIONS),
            max_files=max_files,
        )
    runtime = build_runtime(config)
    try:
        results = [
            _run_one(runtime=runtime, path=path, index=index)
            for index, path in enumerate(paths, start=1)
        ]
    finally:
        _close_runtime(runtime)
    status = "ok"
    if not paths or any(item.get("status") == "failed" for item in results):
        status = "failed"
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": str(Path(config).resolve()),
        "sample_dir": str(sample_root.resolve()),
        "extensions": sorted(extensions or set(DEFAULT_EXTENSIONS)),
        "summary": _summary(results),
        "results": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# ParseCore Parse Performance Baseline",
        "",
        f"- status: **{payload.get('status')}**",
        f"- sample_dir: `{payload.get('sample_dir')}`",
        f"- documents: {summary.get('documents', 0)}",
        f"- total_elapsed_s: {summary.get('total_elapsed_s', 0)}",
        f"- max_peak_kb: {summary.get('max_peak_kb', 0)}",
        "",
        "| document | status | size_bytes | elapsed_s | peak_kb | mb_per_s | blocks | chunks | tables |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in payload.get("results") or []:
        lines.append(
            "| {file_name} | {status} | {size_bytes} | {elapsed_s} | {peak_kb} | {mb_per_s} | {blocks} | {chunks} | {tables} |".format(
                file_name=item.get("file_name", ""),
                status=item.get("status", ""),
                size_bytes=item.get("size_bytes", 0),
                elapsed_s=item.get("elapsed_s", ""),
                peak_kb=item.get("peak_kb", ""),
                mb_per_s=item.get("mb_per_s", ""),
                blocks=item.get("blocks", 0),
                chunks=item.get("chunks", 0),
                tables=item.get("tables", 0),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    explicit_samples = [Path(item) for item in args.sample] if args.sample else None
    extensions = _parse_extensions(args.extensions)
    payload = build_report(
        config=args.config,
        sample_dir=args.sample_dir,
        samples=explicit_samples,
        extensions=extensions,
        max_files=max(1, args.max_files),
    )
    if args.fail_on_errors and int((payload.get("summary") or {}).get("failed_documents") or 0) > 0:
        payload["status"] = "failed"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[parse-perf-baseline] wrote {output_path}")
    else:
        print(text)

    if args.out_md:
        markdown_path = Path(args.out_md)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[parse-perf-baseline] wrote {markdown_path}")

    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
