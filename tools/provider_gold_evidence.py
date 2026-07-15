"""Build a visual evidence packet for pending Provider gold reviews.

The packet is intentionally evidence-only: it renders source PDF pages,
records text/page probes and copies the controlled queue metadata, but never
fills gold labels or changes ``review_status``. Screenshots are kept under the
selected output directory so a reviewer can inspect the original page before
entering anchors, block order and table cells into the controlled gold corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "2026-07-provider-gold-evidence-packet"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    return normalized.strip("._") or "document"


def _resolve_source(source_map_path: Path, source_map: Mapping[str, Any], document_id: str) -> Path:
    raw = str(source_map.get(document_id) or "").strip()
    if not raw:
        raise ValueError(f"gold_source_map_missing:{document_id}")
    source = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not source.is_absolute():
        source = (source_map_path.parent / source).resolve()
    else:
        source = source.resolve()
    if not source.exists():
        raise ValueError(f"gold_source_not_found:{document_id}:{source}")
    if source.suffix.casefold() != ".pdf":
        raise ValueError(f"gold_source_must_be_pdf:{document_id}:{source}")
    return source


def _find_pdftoppm() -> str:
    configured = str(os.environ.get("PARSECORE_PDFTOPPM") or "").strip()
    if configured:
        if not Path(configured).exists():
            raise RuntimeError(f"pdftoppm_not_found:{configured}")
        return configured
    found = shutil.which("pdftoppm")
    if not found:
        raise RuntimeError("pdftoppm_not_found")
    # The bundled Windows runtime exposes a .cmd shim whose nested relative
    # call can fail under PowerShell. Resolve it to the native executable when
    # that layout is present; on other platforms the discovered executable is
    # already directly runnable.
    candidate = Path(found)
    if candidate.suffix.casefold() == ".cmd":
        dependencies = candidate.parents[2]
        native = dependencies / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
        if native.exists():
            return str(native)
    return str(candidate)


def _render_page(
    source: Path,
    page_number: int,
    output_prefix: Path,
    *,
    dpi: int = 150,
    pdftoppm: str | None = None,
) -> Path:
    if int(page_number) <= 0:
        raise ValueError("gold_page_number_must_be_positive")
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    executable = pdftoppm or _find_pdftoppm()
    command = [
        executable,
        "-png",
        "-singlefile",
        "-r",
        str(max(72, int(dpi))),
        "-f",
        str(int(page_number)),
        "-l",
        str(int(page_number)),
        str(source),
        str(output_prefix),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"pdftoppm_failed:{source.name}:p{page_number}:{detail}")
    output = output_prefix.with_suffix(".png")
    if not output.exists():
        raise RuntimeError(f"gold_screenshot_missing:{output}")
    return output


def _page_probe(source: Path, page_number: int) -> tuple[str, dict[str, Any]]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        if int(page_number) > len(reader.pages):
            raise ValueError(f"gold_page_out_of_range:{source.name}:p{page_number}")
        page = reader.pages[int(page_number) - 1]
        text = str(page.extract_text() or "")
        try:
            image_count = len(page.images)
        except Exception:
            image_count = None
        box = page.mediabox
        probe = {
            "text_chars": len(text.strip()),
            "image_count": image_count,
            "media_box": [float(box.left), float(box.bottom), float(box.right), float(box.top)],
        }
        return text, probe
    except ValueError:
        raise
    except Exception as exc:  # pragma: no cover - depends on damaged PDFs
        raise RuntimeError(f"gold_page_probe_failed:{source.name}:p{page_number}:{exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_readme(output_dir: Path, pages: Sequence[Mapping[str, Any]]) -> None:
    counts = {
        "approved": sum(1 for page in pages if str(page.get("review_status") or "") == "approved"),
        "pending": sum(1 for page in pages if str(page.get("review_status") or "") == "pending"),
        "rejected": sum(1 for page in pages if str(page.get("review_status") or "") == "rejected"),
    }
    review_instruction = (
        "Inspect the linked source screenshot and text probe; pages still pending require a named reviewer, "
        "expected evidence and an explicit status update."
        if counts["pending"]
        else "All pages have a recorded review status; use the linked evidence for post-review risk triage."
    )
    lines = [
        "# Provider gold review evidence",
        "",
        (
            "This packet is evidence-only. Status counts: "
            f"approved={counts['approved']}, pending={counts['pending']}, rejected={counts['rejected']}."
        ),
        "Expected anchors, block labels and table cells are copied from the controlled queue.",
        review_instruction,
        "",
        "| ID | Document | Page | Status | Source screenshot | Text probe |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for page in pages:
        lines.append(
            "| {id} | {document_id} | {page_number} | {status} | [{screenshot}](<{screenshot}>) | {text_chars} chars / {image_count} images |".format(
                id=page["id"],
                document_id=page["document_id"],
                page_number=page["page_number"],
                status=page.get("review_status") or "pending",
                screenshot=page["evidence"]["screenshot"],
                text_chars=page["source_probe"]["text_chars"],
                image_count=page["source_probe"]["image_count"],
            )
        )
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_risk_review_index(
    *,
    evidence_manifest_path: str | Path,
    evaluation_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write a review-only priority index that links risks to evidence files."""
    manifest_file = Path(evidence_manifest_path).resolve()
    evaluation_file = Path(evaluation_path).resolve()
    manifest = _load_json(manifest_file)
    evaluation = _load_json(evaluation_file)
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("pages"), Sequence):
        raise ValueError("gold_evidence_manifest_must_contain_pages")
    if not isinstance(evaluation, Mapping):
        raise ValueError("gold_evaluation_must_be_object")
    gold_evaluation = evaluation.get("gold_evaluation")
    if not isinstance(gold_evaluation, Mapping):
        gold_evaluation = evaluation
    risk_summary = gold_evaluation.get("risk_summary")
    if not isinstance(risk_summary, Mapping):
        raise ValueError("gold_evaluation_missing_risk_summary")
    priority_pages = risk_summary.get("priority_pages")
    if not isinstance(priority_pages, Sequence) or isinstance(priority_pages, (str, bytes)):
        raise ValueError("gold_risk_summary_missing_priority_pages")
    pending_page_count = int(manifest.get("pending_page_count") or 0)
    page_by_id = {
        str(page.get("id") or ""): page
        for page in manifest.get("pages", [])
        if isinstance(page, Mapping) and str(page.get("id") or "")
    }
    resolved_output = (
        Path(output_path).resolve()
        if output_path is not None
        else manifest_file.parent / "RISK_REVIEW.md"
    )
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Provider gold review priority",
        "",
        "This index is review-only. It does not fill expected labels or change any `review_status`.",
        (
            "Use the linked source screenshot and text probe for post-review risk triage; all pages already have a recorded review status."
            if pending_page_count == 0
            else "Use the linked source screenshot and text probe to review the page, then update the controlled gold corpus with a named reviewer."
        ),
        "",
        f"- Evidence manifest: `{manifest_file}`",
        f"- Evaluation report: `{evaluation_file}`",
        f"- Approved pages in packet: `{manifest.get('approved_page_count', 0)}`",
        f"- Pending pages in packet: `{manifest.get('pending_page_count', 0)}`",
        f"- Priority pages listed: `{min(len(priority_pages), 20)}`",
        "",
        "| Priority | Page | Risk codes | Candidate / baseline (s) | Blocks | Tables | Evidence |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- |",
    ]
    listed = 0
    for priority, raw_page in enumerate(priority_pages, start=1):
        if listed >= 20 or not isinstance(raw_page, Mapping):
            break
        page_id = str(raw_page.get("page_id") or "")
        page = page_by_id.get(page_id)
        if page is None:
            continue
        evidence = page.get("evidence") if isinstance(page.get("evidence"), Mapping) else {}
        screenshot = str(evidence.get("screenshot") or "")
        text_probe = str(evidence.get("text") or "")
        risk_codes = "<br>".join(str(code) for code in raw_page.get("risk_codes", []) or []) or "-"
        candidate_elapsed = raw_page.get("candidate_elapsed_s")
        baseline_elapsed = raw_page.get("baseline_elapsed_s")
        elapsed = f"{candidate_elapsed} / {baseline_elapsed if baseline_elapsed is not None else '-'}"
        blocks = f"{raw_page.get('candidate_blocks', '-')} / {raw_page.get('baseline_blocks', '-')}"
        tables = f"{raw_page.get('candidate_tables', '-')} / {raw_page.get('baseline_tables', '-')}"
        evidence_links = []
        if screenshot:
            evidence_links.append(f"[PNG](<{screenshot}>)")
        if text_probe:
            evidence_links.append(f"[text](<{text_probe}>)")
        lines.append(
            "| {priority} | {page_id} (p{page_number}) | {risk_codes} | {elapsed} | {blocks} | {tables} | {evidence} |".format(
                priority=priority,
                page_id=page_id,
                page_number=raw_page.get("page_number") or page.get("page_number") or "?",
                risk_codes=risk_codes,
                elapsed=elapsed,
                blocks=blocks,
                tables=tables,
                evidence=" / ".join(evidence_links) or "-",
            )
        )
        listed += 1
    lines.extend(
        [
            "",
            (
                "The priority score is diagnostic only. A page remains `pending` until a named reviewer "
                "confirms the screenshot, block order, table cells, critical tokens and exclusions."
                if pending_page_count > 0
                else "All pages in the packet have a recorded review status; the priority score remains diagnostic only."
            ),
            "",
        ]
    )
    resolved_output.write_text("\n".join(lines), encoding="utf-8")
    updated_manifest = dict(manifest)
    updated_manifest["risk_review"] = {
        "path": resolved_output.relative_to(manifest_file.parent).as_posix(),
        "source_evaluation": str(evaluation_file),
        "priority_page_count": listed,
    }
    manifest_file.write_text(
        json.dumps(updated_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(resolved_output),
        "priority_page_count": listed,
        "pending_page_count": pending_page_count,
    }


def build_evidence_packet(
    *,
    queue_path: str | Path,
    source_map_path: str | Path,
    output_dir: str | Path,
    dpi: int = 150,
    render_page: Callable[..., Path] = _render_page,
) -> dict[str, Any]:
    queue_file = Path(queue_path).resolve()
    source_map_file = Path(source_map_path).resolve()
    output = Path(output_dir).resolve()
    queue = _load_json(queue_file)
    source_map = _load_json(source_map_file)
    if not isinstance(queue, Mapping) or not isinstance(queue.get("pages"), Sequence):
        raise ValueError("gold_review_queue_must_contain_pages")
    if not isinstance(source_map, Mapping):
        raise ValueError("gold_source_map_must_be_object")
    output.mkdir(parents=True, exist_ok=True)
    packet_pages: list[dict[str, Any]] = []
    for raw_page in queue.get("pages") or []:
        if not isinstance(raw_page, Mapping):
            raise ValueError("gold_review_page_must_be_object")
        page_id = str(raw_page.get("id") or "").strip()
        document_id = str(raw_page.get("document_id") or "").strip()
        page_number = int(raw_page.get("page_number") or 0)
        if not page_id or not document_id or page_number <= 0:
            raise ValueError("gold_review_page_identity_missing")
        source = _resolve_source(source_map_file, source_map, document_id)
        relative_dir = Path("pages") / _safe_name(document_id)
        prefix = output / relative_dir / f"p{page_number:04d}"
        screenshot = render_page(source, page_number, prefix, dpi=dpi)
        text, probe = _page_probe(source, page_number)
        text_path = prefix.with_suffix(".txt")
        text_path.write_text(text, encoding="utf-8")
        packet_pages.append(
            {
                "id": page_id,
                "document_id": document_id,
                "page_number": page_number,
                "review_status": str(raw_page.get("review_status") or "pending"),
                "review": dict(raw_page.get("review") or {}),
                "expected": dict(raw_page.get("expected") or {}),
                "source_pdf": str(source),
                "source_probe": probe,
                "evidence": {
                    "screenshot": screenshot.relative_to(output).as_posix(),
                    "screenshot_sha256": _sha256(screenshot),
                    "text": text_path.relative_to(output).as_posix(),
                    "text_sha256": _sha256(text_path),
                },
            }
        )
    approved_page_count = sum(1 for page in packet_pages if page["review_status"] == "approved")
    pending_page_count = sum(1 for page in packet_pages if page["review_status"] == "pending")
    rejected_page_count = sum(1 for page in packet_pages if page["review_status"] == "rejected")
    packet = {
        "schema_version": SCHEMA_VERSION,
        "description": (
            "Rendered source-page evidence for Provider gold review. "
            "This packet copies queue status but never marks pages approved or fills expected labels."
        ),
        "queue": str(queue_file),
        "source_map": str(source_map_file),
        "output_dir": str(output),
        "dpi": max(72, int(dpi)),
        "page_count": len(packet_pages),
        "approved_page_count": approved_page_count,
        "pending_page_count": pending_page_count,
        "rejected_page_count": rejected_page_count,
        "pages": packet_pages,
    }
    (output / "manifest.json").write_text(
        json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_readme(output, packet_pages)
    return packet


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render pending Provider gold review evidence")
    parser.add_argument("--queue", required=True, help="Pending review queue JSON")
    parser.add_argument("--source-map", required=True, help="JSON mapping document_id to PDF path")
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "output" / "pdf" / "provider-gold-review-20260714"),
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--pdftoppm", help="Optional native pdftoppm executable")
    parser.add_argument(
        "--evaluation-json",
        help="Optional provider-gold evaluation JSON used to write RISK_REVIEW.md",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    render = _render_page
    if args.pdftoppm:
        render = lambda source, page_number, output_prefix, *, dpi: _render_page(  # noqa: E731
            source,
            page_number,
            output_prefix,
            dpi=dpi,
            pdftoppm=args.pdftoppm,
        )
    packet = build_evidence_packet(
        queue_path=args.queue,
        source_map_path=args.source_map,
        output_dir=args.out_dir,
        dpi=args.dpi,
        render_page=render,
    )
    risk_review = None
    if args.evaluation_json:
        risk_review = build_risk_review_index(
            evidence_manifest_path=Path(args.out_dir).resolve() / "manifest.json",
            evaluation_path=args.evaluation_json,
        )
    print(json.dumps({
        "status": "ok",
        "out_dir": packet["output_dir"],
        "page_count": packet["page_count"],
        "pending_page_count": packet["pending_page_count"],
        "manifest": str(Path(args.out_dir).resolve() / "manifest.json"),
        "risk_review": risk_review,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
