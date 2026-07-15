"""Render and fingerprint pages classified as extractability gaps.

This is a read-only evidence tool used by the P0 audit.  It does not change
the source PDFs or ParseCore outputs; it renders each flagged page and records
both visual and PDF-object probes so a blank-page decision is reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _content_stream_bytes(page: Any) -> int:
    try:
        contents = page.get_contents()
        if contents is None:
            return 0
        data = contents.get_data()
        return len(data) if isinstance(data, bytes) else len(bytes(data))
    except Exception:
        return -1


def _render_with_pdftoppm(
    *,
    pdftoppm: str,
    source: Path,
    page_number: int,
    target: Path,
) -> tuple[bool, str | None]:
    prefix = target.with_suffix("")
    png_path = prefix.with_suffix(".png")
    command = [
        pdftoppm,
        "-png",
        "-singlefile",
        "-r",
        "30",
        "-f",
        str(page_number),
        "-l",
        str(page_number),
        str(source),
        str(prefix),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=60,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, f"render_exception:{type(exc).__name__}:{exc}"
    if completed.returncode != 0 or not png_path.exists():
        detail = (completed.stderr or completed.stdout or "render_failed").strip()
        return False, f"render_failed:{detail[:500]}"
    return True, None


def _resolve_pdftoppm() -> str | None:
    """Resolve the bundled Windows wrapper to the native executable."""

    located = shutil.which("pdftoppm")
    if not located:
        return None
    path = Path(located)
    if path.suffix.casefold() != ".cmd":
        return str(path)
    # The Codex runtime exposes a .cmd shim under dependencies/bin/override,
    # while Python's CreateProcess cannot execute that shim directly with an
    # argv list.  Prefer its sibling native Poppler executable.
    candidates = [
        path.parents[2] / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe",
        path.parent.parent / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(path)


def _visual_probe(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            gray = image.convert("L")
            histogram = gray.histogram()
            non_white = sum(histogram[:245])
            width, height = gray.size
            return {
                "width": width,
                "height": height,
                "non_white_pixel_count": non_white,
                "non_white_ratio": round(non_white / max(width * height, 1), 8),
                "visual_blank": non_white == 0,
            }
    except Exception as exc:
        return {"visual_blank": None, "visual_error": f"{type(exc).__name__}:{exc}"}


def _is_empty_page_row(raw: Mapping[str, Any]) -> bool:
    """Select both legacy gaps and explicit non-indexable empty-page rows."""

    if raw.get("missing_reason") == "page_without_extractable_content":
        return True
    codes = raw.get("quality_signal_codes") or []
    return raw.get("parsed_text_chars") == 0 and "empty_page" in codes


def build_evidence(*, coverage_report: Path, out_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw_line in coverage_report.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        raw = json.loads(raw_line)
        if isinstance(raw, Mapping) and _is_empty_page_row(raw):
            rows.append(dict(raw))

    out_dir.mkdir(parents=True, exist_ok=True)
    pdftoppm = _resolve_pdftoppm()
    if not pdftoppm:
        raise RuntimeError("pdftoppm_not_available")

    from pypdf import PdfReader

    source_cache: dict[str, tuple[Path, Any, str]] = {}
    evidence_rows: list[dict[str, Any]] = []
    render_errors = 0
    for row in rows:
        source = Path(str(row.get("document") or "")).resolve()
        page_number = int(row.get("page_number") or 0)
        source_key = str(source)
        cached = source_cache.get(source_key)
        if cached is None:
            reader = PdfReader(str(source))
            cached = (source, reader, _sha256(source))
            source_cache[source_key] = cached
        _, reader, source_sha256 = cached
        page = reader.pages[page_number - 1]
        evidence_id = f"{_safe_name(str(row.get('sample_id') or 'sample'))}-p{page_number:04d}"
        png_path = out_dir / f"{evidence_id}.png"
        rendered, render_error = _render_with_pdftoppm(
            pdftoppm=pdftoppm,
            source=source,
            page_number=page_number,
            target=png_path,
        )
        if not rendered:
            render_errors += 1
        visual = _visual_probe(png_path) if rendered else {"visual_blank": None}
        mediabox = page.mediabox
        row_evidence = {
            "evidence_id": evidence_id,
            "sample_id": row.get("sample_id"),
            "source": str(source),
            "source_sha256": source_sha256,
            "source_page_number": page_number,
            "source_page_count": len(reader.pages),
            "page_width": float(mediabox.width),
            "page_height": float(mediabox.height),
            "text_chars_probe": int((row.get("page_probe") or {}).get("text_chars") or 0),
            "image_count_probe": int((row.get("page_probe") or {}).get("image_count") or 0),
            "content_stream_bytes": _content_stream_bytes(page),
            "rendered_png": png_path.name if rendered else None,
            "render_error": render_error,
            **visual,
        }
        evidence_rows.append(row_evidence)

    summary = {
        "schema_version": "2026-07-empty-page-evidence",
        "coverage_report": str(coverage_report.resolve()),
        "out_dir": str(out_dir.resolve()),
        "source_document_count": len(source_cache),
        "flagged_page_count": len(evidence_rows),
        "rendered_page_count": sum(1 for row in evidence_rows if row.get("rendered_png")),
        "visual_blank_count": sum(1 for row in evidence_rows if row.get("visual_blank") is True),
        "visual_non_blank_count": sum(1 for row in evidence_rows if row.get("visual_blank") is False),
        "render_error_count": render_errors,
        "pages": evidence_rows,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "README.md").write_text(
        "# Empty-page evidence\n\n"
        "This read-only packet renders every page classified as either "
        "the legacy `page_without_extractable_content` gap or the explicit "
        "non-indexable `empty_page` artifact by the P0 audit. "
        "`visual_blank=true` means no pixel below the 245/255 grayscale "
        "threshold was found in the 30-DPI render; source SHA-256 and PDF "
        "content-stream probes are recorded in `manifest.json`.\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-report", required=True)
    parser.add_argument(
        "--out-dir",
        default="output/pdf/p0-empty-page-evidence-20260715",
    )
    args = parser.parse_args()
    summary = build_evidence(
        coverage_report=Path(args.coverage_report),
        out_dir=Path(args.out_dir),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["render_error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
