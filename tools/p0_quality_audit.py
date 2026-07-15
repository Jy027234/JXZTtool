"""Run the P0 read-only quality audit on a fixed local sample set.

The audit deliberately does not change parser routing or production state.  It
parses each sample once, materializes a compact ``projection=full`` artifact,
and emits a page-level coverage JSONL that can be reviewed independently of
the parser process.  A PDF page range is copied to a temporary part before
parsing so large documents can be sampled without silently ignoring the range
request.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.api_payloads import _document_projection  # noqa: E402
from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.pdf_parts import create_pdf_part_file  # noqa: E402
from parsecore.stubs import FakeEmbeddingProvider  # noqa: E402


SCHEMA_VERSION = "2026-07-p0-quality-audit"
MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".txt": "text/plain",
    ".md": "text/plain",
}


@dataclass(frozen=True, slots=True)
class SampleSpec:
    sample_id: str
    category: str
    path: Path
    page_start: int | None = None
    page_end: int | None = None
    profile: str = "auto"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
    return normalized.strip("._") or "sample"


def _positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _resolve_sample_path(raw_path: str, *, sample_root: Path, manifest_path: Path) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate.resolve()
    root_candidate = (sample_root / candidate).resolve()
    if root_candidate.exists():
        return root_candidate
    return (manifest_path.parent / candidate).resolve()


def load_sample_specs(manifest_path: str | Path, *, sample_root: str | Path) -> list[SampleSpec]:
    """Load and validate the portable P0 sample manifest."""

    manifest = Path(manifest_path).resolve()
    payload = _load_json(manifest)
    if not isinstance(payload, Mapping):
        raise ValueError("p0_sample_manifest_must_be_object")
    entries = payload.get("samples")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ValueError("p0_sample_manifest_samples_must_be_list")
    resolved_root = Path(sample_root).expanduser().resolve()
    specs: list[SampleSpec] = []
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"p0_sample_entry_must_be_object:{index}")
        sample_id = str(raw.get("id") or raw.get("sample_id") or f"sample-{index}").strip()
        category = str(raw.get("category") or "unspecified").strip() or "unspecified"
        raw_path = str(raw.get("path") or raw.get("file_name") or "").strip()
        if not raw_path:
            raise ValueError(f"p0_sample_path_missing:{sample_id}")
        page_start = _positive_int(raw.get("page_start"))
        page_end = _positive_int(raw.get("page_end"))
        if (page_start is None) != (page_end is None):
            raise ValueError(f"p0_sample_page_range_incomplete:{sample_id}")
        if page_start is not None and page_end is not None and page_end < page_start:
            raise ValueError(f"p0_sample_page_range_invalid:{sample_id}")
        specs.append(
            SampleSpec(
                sample_id=sample_id,
                category=category,
                path=_resolve_sample_path(raw_path, sample_root=resolved_root, manifest_path=manifest),
                page_start=page_start,
                page_end=page_end,
                profile=str(raw.get("profile") or "auto").strip() or "auto",
            )
        )
    if not specs:
        raise ValueError("p0_sample_manifest_has_no_samples")
    return specs


def _media_type(path: Path) -> str | None:
    media_type = MEDIA_TYPES.get(path.suffix.casefold())
    if media_type:
        return media_type
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _page_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _block_page_numbers(block: Any) -> tuple[int, ...]:
    metadata = getattr(block, "metadata", None)
    if not isinstance(metadata, Mapping):
        return tuple()
    values: list[int] = []
    for key in ("page", "page_number", "page_start", "page_end"):
        page = _page_number(metadata.get(key))
        if page is not None:
            values.append(page)
    page_span = metadata.get("page_span")
    if isinstance(page_span, Mapping):
        for key in ("start", "end", "page_start", "page_end"):
            page = _page_number(page_span.get(key))
            if page is not None:
                values.append(page)
    elif isinstance(page_span, Sequence) and not isinstance(page_span, (str, bytes)):
        values.extend(page for page in (_page_number(item) for item in page_span) if page is not None)
    return tuple(sorted(set(values)))


def traceability_metrics(blocks: Sequence[Any], chunks: Sequence[Any]) -> dict[str, Any]:
    """Measure whether every chunk can be resolved to a source block and page."""

    block_by_id = {
        str(getattr(block, "block_id", "")): block
        for block in blocks
        if str(getattr(block, "block_id", ""))
    }
    total = len(chunks)
    with_block = 0
    with_page = 0
    fully_traceable = 0
    missing_block_ids: list[str] = []
    for chunk in chunks:
        block_ids = tuple(str(item) for item in (getattr(chunk, "block_ids", ()) or ()) if str(item))
        source_blocks = [block_by_id.get(block_id) for block_id in block_ids]
        if block_ids and all(block is not None for block in source_blocks):
            with_block += 1
            pages = [page for block in source_blocks for page in _block_page_numbers(block)]
            if pages:
                with_page += 1
                fully_traceable += 1
        else:
            missing_block_ids.extend(block_ids or [str(getattr(chunk, "chunk_id", ""))])
    return {
        "chunk_count": total,
        "chunks_with_source_block": with_block,
        "chunks_with_source_page": with_page,
        "fully_traceable_chunk_count": fully_traceable,
        "traceability_ratio": round(fully_traceable / total, 6) if total else 1.0,
        "missing_source_ids": sorted(set(item for item in missing_block_ids if item)),
    }


def _coverage_gap(page: Mapping[str, Any]) -> bool:
    return bool(
        page.get("missing_reason")
        or page.get("unchunked_unit_ids")
        or page.get("unembedded_unit_ids")
        or page.get("table_ids_without_units")
        or page.get("figure_ids_missing_caption")
    )


def _coverage_metrics(pages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    missing_reasons = Counter(
        str(page.get("missing_reason") or "")
        for page in pages
        if str(page.get("missing_reason") or "")
    )
    quality_codes = Counter(
        str(code)
        for page in pages
        for code in (page.get("quality_signal_codes") or [])
        if str(code)
    )
    gap_pages = [page for page in pages if _coverage_gap(page)]
    pages_with_reason = [page for page in gap_pages if str(page.get("missing_reason") or "").strip()]
    return {
        "coverage_page_count": len(pages),
        "gap_page_count": len(gap_pages),
        "pages_with_missing_reason": len(pages_with_reason),
        "missing_reason_complete": len(gap_pages) == len(pages_with_reason),
        "missing_reason_counts": dict(sorted(missing_reasons.items())),
        "quality_signal_counts": dict(sorted(quality_codes.items())),
        "table_pages_without_units": sum(1 for page in pages if page.get("table_ids_without_units")),
        "figure_pages_missing_caption": sum(1 for page in pages if page.get("figure_ids_missing_caption")),
    }


def _pdf_page_count(path: Path) -> int | None:
    """Return the source PDF page count for full-document coverage checks."""

    if path.suffix.casefold() != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:  # pragma: no cover - depends on damaged PDFs
        return None


def _pdf_page_extractability(
    path: Path,
    page_number: int,
    *,
    reader: Any | None = None,
) -> dict[str, Any]:
    """Probe whether a missing PDF page has extractable text or images.

    This is deliberately a conservative diagnostic used only for a page that
    the parser did not emit.  A page is classified as lacking extractable
    content only when pypdf can read it and reports neither non-whitespace text
    nor image resources.  Any probe failure keeps the original parser omission
    signal rather than guessing.
    """

    if path.suffix.casefold() != ".pdf" or int(page_number) <= 0:
        return {"status": "unknown"}
    try:
        from pypdf import PdfReader

        if reader is None:
            reader = PdfReader(str(path))
        if int(page_number) > len(reader.pages):
            return {"status": "unknown", "error": "page_not_found"}
        page = reader.pages[int(page_number) - 1]
        text = str(page.extract_text() or "")
        try:
            image_count = len(page.images)
        except Exception:
            image_count = None
        text_chars = len(text.strip())
        if image_count is None:
            return {
                "status": "unknown",
                "text_chars": text_chars,
                "image_count": None,
            }
        return {
            "status": "extractable" if text_chars or image_count else "empty",
            "text_chars": text_chars,
            "image_count": int(image_count),
        }
    except Exception as exc:  # pragma: no cover - depends on damaged PDFs
        return {"status": "unknown", "error": str(exc)}


def _expected_page_count(spec: SampleSpec) -> int:
    """Return the audit denominator for a sample's requested page scope."""

    if spec.page_start is not None and spec.page_end is not None:
        return max(0, spec.page_end - spec.page_start + 1)
    count = _pdf_page_count(spec.path)
    return int(count or 0)


def _apply_embedding_override(runtime: Any, provider: str | None) -> str | None:
    """Apply an explicit local-only embedding override for audit runs."""

    normalized = str(provider or "").strip().casefold()
    if not normalized:
        return None
    if normalized in {"configured", "real"}:
        # Leave the runtime-created provider untouched.  This explicit mode
        # makes the audit report distinguish a configured local/remote model
        # from the default no-embedding path without mutating production config.
        return "configured"
    if normalized != "fake":
        raise ValueError(f"unsupported_embedding_override:{provider}")
    runtime.embedding_provider = FakeEmbeddingProvider()
    return normalized


def _snapshot(runtime: Any, outcome: Any) -> dict[str, Any]:
    return {
        "job": outcome.job,
        "doc_id": outcome.job.doc_id,
        "blocks": tuple(outcome.blocks),
        "chunks": tuple(outcome.chunks),
        "provider_registry": runtime.provider_registry(),
        "quality_gate": runtime.quality_gate_config(),
    }


def _audit_one(
    runtime: Any,
    spec: SampleSpec,
    *,
    index: int,
    temp_root: Path,
    full_dir: Path,
    progress: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.perf_counter()
    base = {
        "sample_id": spec.sample_id,
        "category": spec.category,
        "document": str(spec.path),
        "file_name": spec.path.name,
        "profile": spec.profile,
        "requested_page_range": (
            {"start": spec.page_start, "end": spec.page_end}
            if spec.page_start is not None and spec.page_end is not None
            else None
        ),
    }
    if not spec.path.exists():
        return (
            {
                **base,
                "status": "failed",
                "error": "sample_not_found",
                "elapsed_s": round(time.perf_counter() - started, 3),
            },
            [],
        )

    try:
        parse_path = spec.path
        page_offset = 0
        if (
            spec.path.suffix.casefold() == ".pdf"
            and spec.page_start is not None
            and spec.page_end is not None
        ):
            parse_path = temp_root / f"{index:03d}-{_safe_name(spec.sample_id)}.pdf"
            create_pdf_part_file(str(spec.path), str(parse_path), spec.page_start, spec.page_end)
            page_offset = spec.page_start - 1
        doc_id = f"p0-audit-{index:03d}-{_safe_name(spec.sample_id)}"
        options: dict[str, Any] = {
            "profile": spec.profile,
            "requested_profile": spec.profile,
            "p0_quality_audit": True,
        }
        if spec.page_start is not None and spec.page_end is not None:
            options.update(
                {
                    "page_start": spec.page_start,
                    "page_end": spec.page_end,
                    "page_count": spec.page_end - spec.page_start + 1,
                    "page_offset": page_offset,
                }
            )
        if progress:
            print(f"[p0-quality-audit] {index}: {spec.sample_id}", file=sys.stderr, flush=True)
        outcome = runtime.submit(
            ParseRequest(
                doc_id=doc_id,
                file_path=str(parse_path),
                media_type=_media_type(spec.path),
                options=options,
                tenant_id="p0-quality-audit",
                quota_key="audit",
                quota_units=1,
            )
        )
        snapshot = _snapshot(runtime, outcome)
        full = _document_projection(snapshot, projection="full")
        coverage = _document_projection(snapshot, projection="coverage")
        emitted_coverage_pages = [
            dict(page)
            for page in ((coverage.get("coverage") or {}).get("pages") or [])
            if isinstance(page, Mapping)
        ]
        coverage_pages = list(emitted_coverage_pages)
        expected_page_count = _expected_page_count(spec)
        if spec.page_start is not None and spec.page_end is not None:
            expected_page_numbers = list(range(spec.page_start, spec.page_end + 1))
        else:
            expected_page_numbers = list(range(1, expected_page_count + 1)) if expected_page_count else []
        emitted_page_numbers = {
            page_number
            for page in emitted_coverage_pages
            if (page_number := _page_number(page.get("page_number"))) is not None
        }
        missing_page_numbers = [
            page_number for page_number in expected_page_numbers if page_number not in emitted_page_numbers
        ]
        # Blank pages may not produce a Block, but they still need a durable
        # page-level audit record.  Keep the omission visible instead of
        # silently treating the requested range as fully covered.
        probe_reader = None
        if missing_page_numbers and spec.path.suffix.casefold() == ".pdf":
            try:
                from pypdf import PdfReader

                probe_reader = PdfReader(str(spec.path))
            except Exception:
                probe_reader = None
        for page_number in missing_page_numbers:
            page_probe = _pdf_page_extractability(
                spec.path,
                page_number,
                reader=probe_reader,
            )
            is_empty = page_probe.get("status") == "empty"
            coverage_pages.append(
                {
                    "page_number": page_number,
                    "parsed_text_chars": 0,
                    "table_count": 0,
                    "figure_count": 0,
                    "block_count": 0,
                    "unit_ids": [],
                    "indexable_unit_ids": [],
                    "skipped_unit_ids": [],
                    "indexable_unit_count": 0,
                    "chunked_unit_count": 0,
                    "unchunked_unit_ids": [],
                    "unembedded_unit_ids": [],
                    "table_ids_without_units": [],
                    "figure_ids_missing_caption": [],
                    "chunk_ids": [],
                    "embedded": False,
                    "missing_reason": (
                        "page_without_extractable_content"
                        if is_empty
                        else "parser_page_not_emitted"
                    ),
                    "provider_ids": [],
                    "reading_order_confidence": None,
                    "quality_signal_codes": [
                        "page_without_extractable_content"
                        if is_empty
                        else "page_not_emitted"
                    ],
                    "page_probe": page_probe,
                }
            )
        full_path = full_dir / f"{_safe_name(spec.sample_id)}.json"
        full_path.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        coverage_metrics = _coverage_metrics(coverage_pages)
        traceability = traceability_metrics(outcome.blocks, outcome.chunks)
        elapsed_s = round(time.perf_counter() - started, 3)
        quality_signals = [
            item
            for item in (full.get("quality_signals") or [])
            if isinstance(item, Mapping)
        ]
        result = {
            **base,
            "status": str(getattr(outcome.job.state, "value", outcome.job.state)),
            "elapsed_s": elapsed_s,
            "parse_run_id": str(getattr(outcome.job, "job_id", "")),
            "parsed_page_count": len(coverage_pages),
            "emitted_page_count": len(emitted_coverage_pages),
            "requested_page_count": len(expected_page_numbers),
            "missing_page_numbers": missing_page_numbers,
            "block_count": len(outcome.blocks),
            "chunk_count": len(outcome.chunks),
            "table_count": sum(1 for block in outcome.blocks if str(getattr(getattr(block, "type", None), "value", getattr(block, "type", ""))) == "table"),
            "figure_count": sum(1 for block in outcome.blocks if str(getattr(getattr(block, "type", None), "value", getattr(block, "type", ""))) == "figure"),
            "full_projection": str(full_path),
            "quality_signal_count": len(quality_signals),
            "quality_signal_codes": sorted(
                {
                    str(code)
                    for signal in quality_signals
                    for code in (signal.get("codes") or signal.get("quality_signal_codes") or [signal.get("code")])
                    if str(code)
                }
            ),
            "coverage_summary": coverage.get("coverage", {}).get("summary", {}),
            "coverage_metrics": coverage_metrics,
            "traceability": traceability,
        }
        page_rows = [
            {
                "schema_version": SCHEMA_VERSION,
                "sample_id": spec.sample_id,
                "category": spec.category,
                "document": str(spec.path),
                "requested_page_range": base["requested_page_range"],
                **page,
            }
            for page in coverage_pages
        ]
        return result, page_rows
    except Exception as exc:  # pragma: no cover - exercised by real fixture failures
        return (
            {
                **base,
                "status": "failed",
                "elapsed_s": round(time.perf_counter() - started, 3),
                "error": str(exc),
            },
            [],
        )


def _gate(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    checks = [
        {
            "metric": "all_samples_completed",
            "actual": sum(1 for item in results if str(item.get("status")) == "done"),
            "threshold": len(results),
            "passed": all(str(item.get("status")) == "done" for item in results),
        },
        {
            "metric": "all_samples_have_page_coverage",
            "actual": sum(1 for item in results if int(item.get("parsed_page_count") or 0) > 0),
            "threshold": len(results),
            "passed": all(int(item.get("parsed_page_count") or 0) > 0 for item in results),
        },
        {
            "metric": "requested_pages_have_audit_record",
            "actual": sum(
                1
                for item in results
                if int(item.get("requested_page_count") or 0) <= int(item.get("parsed_page_count") or 0)
            ),
            "threshold": len(results),
            "passed": all(
                int(item.get("requested_page_count") or 0) <= int(item.get("parsed_page_count") or 0)
                for item in results
            ),
        },
        {
            "metric": "missing_reason_completeness",
            "actual": sum(
                1
                for item in results
                if bool((item.get("coverage_metrics") or {}).get("missing_reason_complete"))
            ),
            "threshold": len(results),
            "passed": all(bool((item.get("coverage_metrics") or {}).get("missing_reason_complete")) for item in results),
        },
        {
            "metric": "chunk_source_traceability",
            "actual": min(
                (float((item.get("traceability") or {}).get("traceability_ratio", 0.0)) for item in results),
                default=1.0,
            ),
            "threshold": 1.0,
            "passed": all(float((item.get("traceability") or {}).get("traceability_ratio", 0.0)) >= 1.0 for item in results),
        },
    ]
    return {"passed": all(bool(item["passed"]) for item in checks), "checks": checks}


def build_report(
    *,
    config: str | Path,
    manifest: str | Path,
    sample_root: str | Path,
    out_dir: str | Path,
    embedding_provider: str | None = None,
    progress: bool = False,
    sample_ids: Sequence[str] | None = None,
    rerun_sample_ids: Sequence[str] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    all_specs = load_sample_specs(manifest, sample_root=sample_root)
    requested_ids = {
        str(sample_id).strip()
        for sample_id in (sample_ids or ())
        if str(sample_id).strip()
    }
    rerun_ids = {
        str(sample_id).strip()
        for sample_id in (rerun_sample_ids or ())
        if str(sample_id).strip()
    }
    known_ids = {spec.sample_id for spec in all_specs}
    unknown_ids = sorted((requested_ids | rerun_ids) - known_ids)
    if unknown_ids:
        raise ValueError(f"unknown_sample_ids:{','.join(unknown_ids)}")
    target_specs = [
        spec
        for spec in all_specs
        if (not requested_ids or spec.sample_id in requested_ids)
        and (not rerun_ids or spec.sample_id in rerun_ids)
    ] if rerun_ids else [
        spec
        for spec in all_specs
        if not requested_ids or spec.sample_id in requested_ids
    ]
    specs = all_specs if resume else target_specs
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    full_dir = output_dir / "full"
    full_dir.mkdir(parents=True, exist_ok=True)
    spec_positions = {spec.sample_id: index for index, spec in enumerate(all_specs, start=1)}
    specs_by_id = {spec.sample_id: spec for spec in all_specs}
    previous_records: dict[str, dict[str, Any]] = {}
    previous_results: dict[str, dict[str, Any]] = {}
    previous_page_rows: list[dict[str, Any]] = []
    summary_path = output_dir / "summary.json"
    coverage_path = output_dir / "coverage_report.jsonl"
    if resume and summary_path.exists():
        previous_payload = _load_json(summary_path)
        if isinstance(previous_payload, Mapping):
            previous_records = {
                str(item.get("sample_id")): dict(item)
                for item in previous_payload.get("samples", [])
                if isinstance(item, Mapping)
                and str(item.get("sample_id") or "") in known_ids
            }
            previous_results = {
                sample_id: item
                for sample_id, item in previous_records.items()
                if item.get("status") == "done"
                and str(item.get("profile") or "auto")
                == specs_by_id[sample_id].profile
                # Full-document PDF records created before the denominator
                # fix used requested_page_count=0 and must be refreshed so
                # omitted pages become explicit audit rows.
                and (
                    int(item.get("requested_page_count") or 0)
                    == _expected_page_count(specs_by_id[sample_id])
                )
            }
    if resume and coverage_path.exists():
        for raw_line in coverage_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, Mapping)
                and str(row.get("sample_id") or "") in previous_results
            ):
                previous_page_rows.append(dict(row))

    reusable_ids = set(previous_results) - rerun_ids
    pending_specs = [
        spec
        for spec in target_specs
        if spec.sample_id not in reusable_ids
    ]
    result_by_id = {
        spec.sample_id: (
            previous_results[spec.sample_id]
            if spec.sample_id in reusable_ids
            else previous_records[spec.sample_id]
        )
        for spec in specs
        if spec.sample_id in reusable_ids or spec.sample_id in previous_records
    }
    page_rows: list[dict[str, Any]] = [
        row for row in previous_page_rows
        if str(row.get("sample_id") or "") in reusable_ids
    ]
    with TemporaryDirectory(prefix="parsecore-p0-quality-") as temp_dir:
        runtime = build_runtime(config)
        embedding_override = _apply_embedding_override(runtime, embedding_provider)
        for spec in pending_specs:
            result, rows = _audit_one(
                runtime,
                spec,
                index=spec_positions[spec.sample_id],
                temp_root=Path(temp_dir),
                full_dir=full_dir,
                progress=progress,
            )
            result_by_id[spec.sample_id] = result
            page_rows.extend(rows)
    results = [result_by_id[spec.sample_id] for spec in specs if spec.sample_id in result_by_id]
    summary = {
        "sample_count": len(results),
        "completed_sample_count": sum(1 for item in results if item.get("status") == "done"),
        "failed_sample_count": sum(1 for item in results if item.get("status") != "done"),
        "coverage_page_count": sum(int(item.get("parsed_page_count") or 0) for item in results),
        "block_count": sum(int(item.get("block_count") or 0) for item in results),
        "chunk_count": sum(int(item.get("chunk_count") or 0) for item in results),
        "missing_page_count": sum(len(item.get("missing_page_numbers") or []) for item in results),
        "quality_signal_counts": dict(
            sorted(
                Counter(
                    code
                    for item in results
                    for code in item.get("quality_signal_codes") or []
                ).items()
            )
        ),
        "missing_reason_counts": dict(
            sorted(
                Counter(
                    reason
                    for item in results
                    for reason, count in ((item.get("coverage_metrics") or {}).get("missing_reason_counts") or {}).items()
                    for _ in range(int(count or 0))
                ).items()
            )
        ),
    }
    gate = _gate(results)
    configured_embedding = runtime.settings.providers.embedding
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": str(Path(config).resolve()),
        "manifest": str(Path(manifest).resolve()),
        "sample_root": str(Path(sample_root).resolve()),
        "out_dir": str(output_dir.resolve()),
        "embedding_override": embedding_override,
        "configured_embedding_provider": (
            str(configured_embedding.provider or "") or "none"
        ),
        "effective_embedding_provider": (
            embedding_override
            or (
                str(configured_embedding.provider or "")
                if configured_embedding.enabled
                else "none"
            )
        ),
        "summary": summary,
        "gate": gate,
        "samples": results,
        "coverage_report": str((output_dir / "coverage_report.jsonl").resolve()),
    }, page_rows


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the ParseCore P0 read-only quality audit")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--manifest", required=True, help="Portable JSON sample manifest")
    parser.add_argument("--sample-root", required=True, help="Directory containing manifest file_name entries")
    parser.add_argument("--out-dir", default=str(ROOT / "var" / "self-check" / "p0-quality-audit"))
    parser.add_argument(
        "--embedding-provider",
        choices=["fake", "configured"],
        help=(
            "Read-only embedding mode: fake uses a deterministic local provider; "
            "configured uses the provider from the supplied config without changing it"
        ),
    )
    parser.add_argument(
        "--sample-id",
        action="append",
        dest="sample_ids",
        help="Restrict the audit to one or more manifest sample ids; repeat for batches",
    )
    parser.add_argument(
        "--rerun-sample-id",
        action="append",
        dest="rerun_sample_ids",
        help="Force selected samples to rerun when --resume is enabled; repeat for batches",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed samples and coverage rows already present in --out-dir",
    )
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--fail-on-errors", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report, page_rows = build_report(
        config=args.config,
        manifest=args.manifest,
        sample_root=args.sample_root,
        out_dir=args.out_dir,
        embedding_provider=args.embedding_provider,
        progress=bool(args.progress),
        sample_ids=args.sample_ids,
        rerun_sample_ids=args.rerun_sample_ids,
        resume=bool(args.resume),
    )
    output_dir = Path(args.out_dir)
    (output_dir / "coverage_report.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in page_rows),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.fail_on_errors and not bool(report.get("gate", {}).get("passed")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
