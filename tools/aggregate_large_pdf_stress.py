"""Aggregate non-overlapping large-PDF stress reports into a coverage gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate ParseCore large PDF stress reports")
    parser.add_argument("--report", action="append", required=True, help="Stress JSON report; repeat for each page range")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md")
    return parser


def aggregate(report_paths: list[str | Path]) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    part_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    expected_pages = 0
    expected_parts = 0
    for raw_path in report_paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = payload.get("summary") or {}
        expected_pages = max(expected_pages, int(summary.get("total_pages") or 0))
        expected_parts = max(expected_parts, int(summary.get("planned_parts") or 0))
        errors.extend(
            {"report": str(path), **dict(error)}
            for error in (payload.get("errors") or [])
            if isinstance(error, dict)
        )
        done_parts = [
            part
            for part in (payload.get("manifest_part_index") or {}).get("parts", [])
            if isinstance(part, dict) and str(part.get("state") or "") == "done"
        ]
        reports.append(
            {
                "path": str(path.resolve()),
                "status": payload.get("status"),
                "doc_id": payload.get("doc_id"),
                "part_start": payload.get("part_start", 1),
                "executed_parts": int(summary.get("executed_parts") or 0),
                "error_count": int(summary.get("error_count") or 0),
                "avg_part_elapsed_s": summary.get("avg_part_elapsed_s", 0.0),
                "max_part_elapsed_s": summary.get("max_part_elapsed_s", 0.0),
                "done_parts": len(done_parts),
            }
        )
        for part in done_parts:
            page_range = part.get("page_range") or {}
            part_rows.append(
                {
                    "part_index": int(str(part.get("part_id") or "").rsplit("-part-", 1)[-1]),
                    "part_id": part.get("part_id"),
                    "page_start": int(page_range.get("start") or part.get("page_start") or 0),
                    "page_end": int(page_range.get("end") or part.get("page_end") or 0),
                    "block_count": int(part.get("block_count") or 0),
                    "chunk_count": int(part.get("chunk_count") or 0),
                    "report": str(path.resolve()),
                }
            )

    by_part: dict[int, list[dict[str, Any]]] = {}
    for row in part_rows:
        by_part.setdefault(row["part_index"], []).append(row)
    duplicate_part_indices = sorted(index for index, rows in by_part.items() if len(rows) > 1)
    unique_parts = [rows[0] for rows in by_part.values()]
    unique_parts.sort(key=lambda row: row["part_index"])

    intervals = sorted((row["page_start"], row["page_end"]) for row in unique_parts)
    gaps: list[dict[str, int]] = []
    overlaps: list[dict[str, int]] = []
    cursor = 1
    for start, end in intervals:
        if start > cursor:
            gaps.append({"start": cursor, "end": start - 1})
        if start < cursor:
            overlaps.append({"start": start, "end": min(end, cursor - 1)})
        cursor = max(cursor, end + 1)
    if expected_pages and cursor <= expected_pages:
        gaps.append({"start": cursor, "end": expected_pages})

    report_failures = [report for report in reports if report.get("status") != "ok" or report.get("error_count", 0) > 0]
    total_blocks = sum(row["block_count"] for row in unique_parts)
    total_chunks = sum(row["chunk_count"] for row in unique_parts)
    passed = bool(
        reports
        and not report_failures
        and not errors
        and not duplicate_part_indices
        and not gaps
        and not overlaps
        and (not expected_parts or len(unique_parts) == expected_parts)
        and (not expected_pages or (intervals and intervals[0][0] == 1 and intervals[-1][1] == expected_pages))
    )
    return {
        "status": "ok" if passed else "failed",
        "report_count": len(reports),
        "expected_pages": expected_pages,
        "expected_parts": expected_parts,
        "unique_part_count": len(unique_parts),
        "covered_page_count": sum(end - start + 1 for start, end in intervals),
        "total_block_count": total_blocks,
        "total_chunk_count": total_chunks,
        "duplicate_part_indices": duplicate_part_indices,
        "gaps": gaps,
        "overlaps": overlaps,
        "errors": errors,
        "reports": reports,
        "parts": unique_parts,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Large PDF Stress Coverage Aggregate",
        "",
        f"- status: **{payload.get('status')}**",
        f"- expected pages: {payload.get('expected_pages', 0)}",
        f"- covered pages: {payload.get('covered_page_count', 0)}",
        f"- expected parts: {payload.get('expected_parts', 0)}",
        f"- unique parts: {payload.get('unique_part_count', 0)}",
        f"- blocks: {payload.get('total_block_count', 0)}",
        f"- chunks: {payload.get('total_chunk_count', 0)}",
        "",
        "| report | status | part start | executed | avg s | max s | errors |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in payload.get("reports") or []:
        lines.append(
            "| {path} | {status} | {part_start} | {executed_parts} | {avg_part_elapsed_s} | {max_part_elapsed_s} | {error_count} |".format(
                **report
            )
        )
    lines.extend(["", f"- duplicate part indices: {payload.get('duplicate_part_indices') or []}"])
    lines.append(f"- gaps: {payload.get('gaps') or []}")
    lines.append(f"- overlaps: {payload.get('overlaps') or []}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = aggregate(args.report)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("status", "expected_pages", "covered_page_count", "expected_parts", "unique_part_count", "total_block_count", "total_chunk_count")}, ensure_ascii=False))
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
