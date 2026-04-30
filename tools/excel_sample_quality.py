"""Generate a quality report for real spreadsheet samples."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import BlockType, ParseRequest  # noqa: E402


SPREADSHEET_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}

REQUIRED_TABLE_METADATA = (
    "sheet_name",
    "cell_range",
    "source_cell_range",
    "header_row",
    "header_values",
    "rows",
    "cols",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report ParseCore spreadsheet sample quality")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--sample-dir", default="D:/app/uploads")
    parser.add_argument("--out-json", help="Optional JSON output path")
    parser.add_argument("--out-md", help="Optional Markdown output path")
    parser.add_argument("--fail-on-issues", action="store_true")
    return parser


def _round(value: float) -> float:
    return round(float(value), 3)


def _spreadsheet_paths(sample_dir: Path) -> list[Path]:
    if not sample_dir.exists():
        return []
    return sorted(
        path
        for path in sample_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SPREADSHEET_MEDIA_TYPES
    )


def _table_issue_summary(table_metadata: dict[str, Any], content: str) -> list[str]:
    issues: list[str] = []
    missing = [key for key in REQUIRED_TABLE_METADATA if key not in table_metadata]
    if missing:
        issues.append("missing_metadata:" + ",".join(missing))
    if not content.strip():
        issues.append("empty_content")
    try:
        rows = int(table_metadata.get("rows", 0))
        cols = int(table_metadata.get("cols", 0))
    except (TypeError, ValueError):
        rows = 0
        cols = 0
    if rows <= 0 or cols <= 0:
        issues.append("empty_shape")
    if bool(table_metadata.get("truncated")):
        issues.append("sheet_truncated")
    if bool(table_metadata.get("cells_truncated")):
        issues.append("metadata_cells_truncated")
    return issues


def _summarize_tables(table_blocks: list[Any]) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    max_rows = 0
    max_cols = 0
    total_rows = 0
    total_cells = 0
    table_titles: list[str] = []

    for block in table_blocks:
        metadata = dict(block.metadata or {})
        table_issues = _table_issue_summary(metadata, block.content)
        issues.extend(table_issues)
        rows = int(metadata.get("rows") or 0)
        cols = int(metadata.get("cols") or 0)
        max_rows = max(max_rows, rows)
        max_cols = max(max_cols, cols)
        total_rows += rows
        total_cells += rows * cols
        title = metadata.get("table_title")
        if title:
            table_titles.append(str(title))

    summary = {
        "tables": len(table_blocks),
        "titled_tables": sum(1 for block in table_blocks if block.metadata.get("table_title")),
        "merged_cell_tables": sum(1 for block in table_blocks if block.metadata.get("merged_cells")),
        "formula_tables": sum(1 for block in table_blocks if block.metadata.get("has_formula")),
        "hidden_tables": sum(1 for block in table_blocks if block.metadata.get("hidden_sheet")),
        "truncated_tables": sum(1 for block in table_blocks if block.metadata.get("truncated")),
        "metadata_cells_truncated_tables": sum(
            1 for block in table_blocks if block.metadata.get("cells_truncated")
        ),
        "empty_tables": sum(1 for block in table_blocks if not block.content.strip()),
        "total_rows": total_rows,
        "total_cells": total_cells,
        "max_rows": max_rows,
        "max_cols": max_cols,
        "table_titles": table_titles[:10],
    }
    if not table_blocks:
        issues.append("no_tables")
    return summary, sorted(set(issues))


def _run_one(*, runtime: Any, path: Path, index: int) -> dict[str, Any]:
    started = time.monotonic()
    media_type = SPREADSHEET_MEDIA_TYPES[path.suffix.lower()]
    try:
        outcome = runtime.submit(
            ParseRequest(
                doc_id=f"spreadsheet-sample-{index}",
                file_path=str(path),
                media_type=media_type,
                tenant_id="sample-quality",
                quota_key="spreadsheet",
                quota_units=1,
            )
        )
    except Exception as exc:
        return {
            "document": str(path),
            "file_name": path.name,
            "suffix": path.suffix.lower(),
            "status": "failed",
            "elapsed_s": _round(time.monotonic() - started),
            "error": str(exc),
            "issues": ["parse_failed"],
        }

    elapsed_s = _round(time.monotonic() - started)
    table_blocks = [block for block in outcome.blocks if block.type == BlockType.TABLE]
    table_summary, issues = _summarize_tables(table_blocks)
    return {
        "document": str(path),
        "file_name": path.name,
        "suffix": path.suffix.lower(),
        "status": outcome.job.state.value,
        "elapsed_s": elapsed_s,
        "blocks": len(outcome.blocks),
        "chunks": len(outcome.chunks),
        "issues": issues,
        **table_summary,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "documents": len(results),
        "failed_documents": sum(1 for item in results if item.get("status") == "failed"),
        "documents_with_issues": sum(1 for item in results if item.get("issues")),
        "total_tables": sum(int(item.get("tables") or 0) for item in results),
        "titled_tables": sum(int(item.get("titled_tables") or 0) for item in results),
        "merged_cell_tables": sum(int(item.get("merged_cell_tables") or 0) for item in results),
        "formula_tables": sum(int(item.get("formula_tables") or 0) for item in results),
        "empty_tables": sum(int(item.get("empty_tables") or 0) for item in results),
        "truncated_tables": sum(int(item.get("truncated_tables") or 0) for item in results),
        "metadata_cells_truncated_tables": sum(
            int(item.get("metadata_cells_truncated_tables") or 0) for item in results
        ),
        "elapsed_s": _round(sum(float(item.get("elapsed_s", 0.0)) for item in results)),
    }


def _close_runtime(runtime: Any) -> None:
    for resource_name in ("job_store", "index"):
        resource = getattr(runtime, resource_name, None)
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def build_report(*, config: str | Path, sample_dir: str | Path) -> dict[str, Any]:
    sample_root = Path(sample_dir)
    paths = _spreadsheet_paths(sample_root)
    runtime = build_runtime(config)
    try:
        results = [
            _run_one(runtime=runtime, path=path, index=index)
            for index, path in enumerate(paths, start=1)
        ]
    finally:
        _close_runtime(runtime)

    status = "ok"
    if not sample_root.exists():
        status = "failed"
    elif not paths:
        status = "failed"
    elif any(item.get("status") == "failed" for item in results):
        status = "failed"

    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": str(Path(config).resolve()),
        "sample_dir": str(sample_root.resolve()),
        "summary": _summary(results),
        "results": results,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# ParseCore Spreadsheet Sample Quality",
        "",
        f"- status: **{payload.get('status')}**",
        f"- sample_dir: `{payload.get('sample_dir')}`",
        f"- documents: {summary.get('documents', 0)}",
        f"- total_tables: {summary.get('total_tables', 0)}",
        f"- documents_with_issues: {summary.get('documents_with_issues', 0)}",
        "",
        "| document | status | tables | titled | merged | empty | truncated | issues | elapsed_s |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for item in payload.get("results") or []:
        issues = ", ".join(item.get("issues") or [])
        lines.append(
            "| {file_name} | {status} | {tables} | {titled} | {merged} | {empty} | {truncated} | {issues} | {elapsed} |".format(
                file_name=item.get("file_name", ""),
                status=item.get("status", ""),
                tables=item.get("tables", 0),
                titled=item.get("titled_tables", 0),
                merged=item.get("merged_cell_tables", 0),
                empty=item.get("empty_tables", 0),
                truncated=item.get("truncated_tables", 0),
                issues=issues,
                elapsed=item.get("elapsed_s", ""),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = build_report(config=args.config, sample_dir=args.sample_dir)
    if args.fail_on_issues and int((payload.get("summary") or {}).get("documents_with_issues") or 0) > 0:
        payload["status"] = "failed"

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(f"[excel-sample-quality] wrote {output_path}")
    else:
        print(text)

    if args.out_md:
        markdown_path = Path(args.out_md)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[excel-sample-quality] wrote {markdown_path}")

    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
