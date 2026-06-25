"""Large PDF part scheduling stress tool."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.pdf_parts import detect_pdf_page_count  # noqa: E402


DEFAULT_OUT_JSON = ROOT / "var" / "self-check" / "large-pdf-stress.json"
DEFAULT_OUT_MD = ROOT / "var" / "self-check" / "large-pdf-stress.md"
DEFAULT_SYNTHETIC_PDF = ROOT / "var" / "self-check" / "large-pdf-stress.synthetic.pdf"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stress ParseCore large PDF part scheduling")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--pdf", help="Existing PDF path. If omitted, a synthetic text PDF is generated.")
    parser.add_argument(
        "--generate-pages",
        type=int,
        default=200,
        help="Synthetic PDF page count when --pdf is omitted. Use 0 to require --pdf.",
    )
    parser.add_argument("--generated-pdf", default=str(DEFAULT_SYNTHETIC_PDF))
    parser.add_argument("--lines-per-page", type=int, default=8)
    parser.add_argument("--target-pages-per-part", type=int, default=50)
    parser.add_argument("--max-active-parts-per-doc", type=int)
    parser.add_argument("--profile", default="large-pdf")
    parser.add_argument("--doc-id", help="Document id to use. Defaults to a timestamped stress id.")
    parser.add_argument("--tenant-id", default="stress")
    parser.add_argument(
        "--execute-parts",
        action="store_true",
        help="Execute planned part jobs inline. Default is plan-only to avoid accidental long runs.",
    )
    parser.add_argument(
        "--max-parts",
        type=int,
        default=0,
        help="Maximum part jobs to execute when --execute-parts is set. 0 means all planned parts.",
    )
    parser.add_argument("--rerun-part-id", help="Optional part id to rerun after the first execution pass.")
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--out-md", default=str(DEFAULT_OUT_MD))
    parser.add_argument("--fail-on-errors", action="store_true")
    return parser


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_synthetic_pdf_bytes(*, pages: int, lines_per_page: int = 8) -> bytes:
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"PLACEHOLDER_PAGES")

    page_object_ids: list[int] = []
    font_object_id = 3 + pages * 2

    for page_index in range(pages):
        page_number = page_index + 1
        page_object_id = 3 + page_index * 2
        content_object_id = page_object_id + 1
        page_object_ids.append(page_object_id)
        page_object = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
            f"/Contents {content_object_id} 0 R >>"
        ).encode("ascii")
        objects.append(page_object)

        lines = [
            f"Page {page_number:05d} table stress sample",
            "col_a col_b col_c col_d",
        ]
        for row in range(max(0, lines_per_page - len(lines))):
            lines.append(
                f"{page_number:05d}-{row + 1:02d} "
                f"value_a_{page_number}_{row} value_b_{row} value_c_{page_number + row}"
            )

        stream_lines = [b"BT", b"/F1 10 Tf", b"72 720 Td"]
        for line_index, line in enumerate(lines):
            escaped = _escape_pdf_text(line).encode("latin-1", errors="replace")
            if line_index > 0:
                stream_lines.append(b"0 -16 Td")
            stream_lines.append(b"(" + escaped + b") Tj")
        stream_lines.append(b"ET")
        stream = b"\n".join(stream_lines)
        content_object = (
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\n"
            b"stream\n" + stream + b"\nendstream"
        )
        objects.append(content_object)

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids = b" ".join(f"{page_object_id} 0 R".encode("ascii") for page_object_id in page_object_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode("ascii") + b" >>"

    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii"))
        chunks.append(body)
        chunks.append(b"\nendobj\n")

    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def generate_synthetic_pdf(*, path: str | Path, pages: int, lines_per_page: int = 8) -> Path:
    if pages <= 0:
        raise ValueError("generate-pages must be positive when --pdf is omitted")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_synthetic_pdf_bytes(pages=pages, lines_per_page=lines_per_page))
    return target


def _media_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/pdf"


def _close_runtime(runtime: Any) -> None:
    for resource_name in ("job_store", "index"):
        resource = getattr(runtime, resource_name, None)
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def _round(value: float) -> float:
    return round(float(value), 3)


def _part_summary(parts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    states: dict[str, int] = {}
    for part in parts:
        state = str(part.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    return {
        "total": len(parts),
        "states": states,
        "done": states.get("done", 0),
        "failed": states.get("failed", 0) + states.get("cancelled", 0),
        "active": states.get("running", 0),
        "queued": states.get("pending", 0),
    }


def _manifest_part_summary(manifest: dict[str, Any] | None) -> dict[str, Any]:
    part_index = (manifest or {}).get("part_index") if isinstance(manifest, dict) else None
    if not isinstance(part_index, dict):
        return {"available": False, "part_count": 0, "indexed_part_count": 0, "parts": []}
    parts = [
        {
            "part_id": part.get("part_id"),
            "job_id": part.get("job_id"),
            "state": part.get("state"),
            "page_range": part.get("page_range"),
            "chunk_count": part.get("chunk_count", 0),
            "block_count": part.get("block_count", 0),
            "index_version": part.get("index_version"),
        }
        for part in part_index.get("parts", [])
        if isinstance(part, dict)
    ]
    return {
        "available": True,
        "part_count": part_index.get("part_count", len(parts)),
        "indexed_part_count": part_index.get("indexed_part_count", 0),
        "parts": parts,
    }


def build_report(
    *,
    config: str | Path,
    pdf: str | Path | None = None,
    generate_pages: int = 200,
    generated_pdf: str | Path = DEFAULT_SYNTHETIC_PDF,
    lines_per_page: int = 8,
    target_pages_per_part: int = 50,
    max_active_parts_per_doc: int | None = None,
    profile: str = "large-pdf",
    doc_id: str | None = None,
    tenant_id: str | None = "stress",
    execute_parts: bool = False,
    max_parts: int = 0,
    rerun_part_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    generated = False
    if pdf:
        pdf_path = Path(pdf)
    else:
        pdf_path = generate_synthetic_pdf(
            path=generated_pdf,
            pages=max(1, int(generate_pages)),
            lines_per_page=max(1, int(lines_per_page)),
        )
        generated = True
    if not pdf_path.exists():
        raise FileNotFoundError(str(pdf_path))

    total_pages = detect_pdf_page_count(str(pdf_path))
    effective_doc_id = doc_id or f"large-pdf-stress-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    runtime = build_runtime(config)
    part_timings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    rerun_result: dict[str, Any] | None = None
    try:
        source_job = runtime.start(
            ParseRequest(
                doc_id=effective_doc_id,
                file_path=str(pdf_path),
                media_type=_media_type_for(pdf_path),
                options={"profile": profile},
                tenant_id=tenant_id,
                quota_key="large-pdf-stress",
                quota_units=max(1, total_pages),
            )
        )
        plan_started = time.perf_counter()
        planned = runtime.start_pdf_part_jobs(
            doc_id=effective_doc_id,
            tenant_id=tenant_id,
            target_pages_per_part=target_pages_per_part,
            max_active_parts_per_doc=max_active_parts_per_doc,
            profile=profile,
        )
        plan_elapsed_s = _round(time.perf_counter() - plan_started)
        part_jobs = list(planned.get("part_jobs") or [])

        if execute_parts:
            limit = len(part_jobs) if max_parts <= 0 else min(max_parts, len(part_jobs))
            for job in part_jobs[:limit]:
                part_id = str((getattr(job, "options", {}) or {}).get("part_id") or job.doc_id)
                part_started = time.perf_counter()
                try:
                    outcome = runtime.execute(job_id=job.job_id)
                    part_timings.append(
                        {
                            "part_id": part_id,
                            "job_id": job.job_id,
                            "state": outcome.job.state.value,
                            "elapsed_s": _round(time.perf_counter() - part_started),
                            "blocks": len(outcome.blocks),
                            "chunks": len(outcome.chunks),
                        }
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "part_id": part_id,
                            "job_id": job.job_id,
                            "error": str(exc),
                            "elapsed_s": _round(time.perf_counter() - part_started),
                        }
                    )

        if rerun_part_id:
            rerun_started = time.perf_counter()
            rerun = runtime.rerun_pdf_part(
                doc_id=effective_doc_id,
                part_id=rerun_part_id,
                tenant_id=tenant_id,
                profile=profile,
            )
            rerun_job = rerun.get("job")
            rerun_result = {
                "part_id": rerun_part_id,
                "job_id": getattr(rerun_job, "job_id", None),
                "submitted_elapsed_s": _round(time.perf_counter() - rerun_started),
            }
            if execute_parts and rerun_job is not None:
                execute_started = time.perf_counter()
                try:
                    outcome = runtime.execute(job_id=rerun_job.job_id)
                    rerun_result.update(
                        {
                            "state": outcome.job.state.value,
                            "execute_elapsed_s": _round(time.perf_counter() - execute_started),
                            "blocks": len(outcome.blocks),
                            "chunks": len(outcome.chunks),
                        }
                    )
                except Exception as exc:
                    rerun_result.update(
                        {
                            "state": "failed",
                            "error": str(exc),
                            "execute_elapsed_s": _round(time.perf_counter() - execute_started),
                        }
                    )
                    errors.append({"part_id": rerun_part_id, "job_id": getattr(rerun_job, "job_id", None), "error": str(exc)})

        snapshot = runtime.get_document(doc_id=effective_doc_id, tenant_id=tenant_id)
        parts = runtime.partition_parts_for_document(doc_id=effective_doc_id, tenant_id=tenant_id)
        manifest = snapshot.get("index_manifest") if isinstance(snapshot, dict) else None
    finally:
        _close_runtime(runtime)

    summary = _part_summary(parts)
    executed_elapsed_values = [float(item.get("elapsed_s") or 0.0) for item in part_timings]
    summary.update(
        {
            "total_pages": total_pages,
            "target_pages_per_part": target_pages_per_part,
            "planned_parts": len(part_jobs),
            "executed_parts": len(part_timings),
            "error_count": len(errors),
            "plan_elapsed_s": plan_elapsed_s,
            "total_elapsed_s": _round(time.perf_counter() - started),
            "max_part_elapsed_s": _round(max(executed_elapsed_values, default=0.0)),
            "avg_part_elapsed_s": _round(sum(executed_elapsed_values) / len(executed_elapsed_values))
            if executed_elapsed_values
            else 0.0,
        }
    )
    status = "ok"
    if errors:
        status = "degraded" if not execute_parts else "failed"
    return {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": str(Path(config).resolve()),
        "doc_id": effective_doc_id,
        "tenant_id": tenant_id,
        "pdf": str(pdf_path.resolve()),
        "generated_pdf": generated,
        "source_job_id": source_job.job_id,
        "execute_parts": execute_parts,
        "max_parts": max_parts,
        "profile": profile,
        "max_active_parts_per_doc": max_active_parts_per_doc,
        "summary": summary,
        "part_timings": part_timings,
        "errors": errors,
        "rerun": rerun_result,
        "manifest_part_index": _manifest_part_summary(manifest if isinstance(manifest, dict) else None),
    }


def evaluate_gate(report: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a large PDF benchmark report against threshold config.

    Threshold keys follow the convention ``{metric}_{max|min}``:
    - ``plan_elapsed_s_max``: plan_elapsed_s must be <= threshold
    - ``part_count_min``: planned_parts must be >= threshold
    - ``part_count_max``: planned_parts must be <= threshold
    - ``error_count_max``: error_count must be <= threshold
    - ``snapshot_blocks_min``: total snapshot blocks must be >= threshold
    """
    thresholds = config.get("thresholds", {})
    summary = report.get("summary") or {}
    manifest = report.get("manifest_part_index") or {}
    metric_sources: dict[str, Any] = {
        "plan_elapsed_s": summary.get("plan_elapsed_s"),
        "part_count": summary.get("planned_parts"),
        "error_count": summary.get("error_count"),
        "snapshot_blocks": manifest.get("part_count", 0),
        "total_elapsed_s": summary.get("total_elapsed_s"),
        "executed_parts": summary.get("executed_parts"),
    }
    checks: list[dict[str, Any]] = []
    for key, threshold in thresholds.items():
        if key.endswith("_max"):
            metric = key[:-4]
            actual = metric_sources.get(metric)
            passed = actual is not None and actual <= threshold
        elif key.endswith("_min"):
            metric = key[:-4]
            actual = metric_sources.get(metric)
            passed = actual is not None and actual >= threshold
        else:
            continue
        checks.append({
            "metric": metric,
            "actual": actual,
            "threshold": threshold,
            "operator": "max" if key.endswith("_max") else "min",
            "passed": passed,
        })
    all_passed = all(c["passed"] for c in checks) if checks else False
    return {"passed": all_passed, "checks": checks}


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# ParseCore Large PDF Stress Report",
        "",
        f"- status: **{payload.get('status')}**",
        f"- doc_id: `{payload.get('doc_id')}`",
        f"- pdf: `{payload.get('pdf')}`",
        f"- total_pages: {summary.get('total_pages', 0)}",
        f"- planned_parts: {summary.get('planned_parts', 0)}",
        f"- executed_parts: {summary.get('executed_parts', 0)}",
        f"- plan_elapsed_s: {summary.get('plan_elapsed_s', 0)}",
        f"- total_elapsed_s: {summary.get('total_elapsed_s', 0)}",
        "",
        "| part_id | job_id | state | elapsed_s | blocks | chunks |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for item in payload.get("part_timings") or []:
        lines.append(
            "| {part_id} | {job_id} | {state} | {elapsed_s} | {blocks} | {chunks} |".format(
                part_id=item.get("part_id", ""),
                job_id=item.get("job_id", ""),
                state=item.get("state", ""),
                elapsed_s=item.get("elapsed_s", ""),
                blocks=item.get("blocks", 0),
                chunks=item.get("chunks", 0),
            )
        )
    manifest = payload.get("manifest_part_index") or {}
    lines.extend(
        [
            "",
            "## Manifest Part Index",
            "",
            f"- available: {manifest.get('available', False)}",
            f"- part_count: {manifest.get('part_count', 0)}",
            f"- indexed_part_count: {manifest.get('indexed_part_count', 0)}",
            "",
        ]
    )
    if payload.get("errors"):
        lines.extend(["## Errors", ""])
        for error in payload.get("errors") or []:
            lines.append(f"- {error.get('part_id')}: {error.get('error')}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = build_report(
            config=args.config,
            pdf=args.pdf,
            generate_pages=args.generate_pages,
            generated_pdf=args.generated_pdf,
            lines_per_page=args.lines_per_page,
            target_pages_per_part=max(1, args.target_pages_per_part),
            max_active_parts_per_doc=args.max_active_parts_per_doc,
            profile=args.profile,
            doc_id=args.doc_id,
            tenant_id=args.tenant_id,
            execute_parts=bool(args.execute_parts),
            max_parts=max(0, args.max_parts),
            rerun_part_id=args.rerun_part_id,
        )
    except Exception as exc:
        payload = {
            "status": "failed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(text + "\n", encoding="utf-8")
    print(f"[large-pdf-stress] wrote {out_json}")

    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[large-pdf-stress] wrote {out_md}")

    if payload.get("status") == "ok":
        return 0
    return 1 if args.fail_on_errors or payload.get("status") == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
