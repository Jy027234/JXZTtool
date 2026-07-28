"""Run the KnowledgeUnit contract gate against authorized real documents.

The tool deliberately accepts corpus paths at runtime instead of committing
private or licensed documents to the repository.  Its JSON output contains
only caller-provided aliases, source hashes, selected page ranges, aggregate
metrics, and check results; extracted document text and local paths are never
written to the report.

Sample syntax::

    --sample "regulation|regulation|D:\\corpus\\rule.pdf"
    --sample "mixed-cmm|mixed_pdf|D:\\corpus\\cmm.pdf|1-12"

Kinds are descriptive except for ``regulation``, ``scanned_manual``, and
``table_dense`` which add conservative content-shape assertions.

Exit codes:
0 -> every sample and contract check passed
1 -> at least one check failed
2 -> invalid command input or no sample supplied
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from parsecore.api_payloads import _document_projection
from parsecore.bootstrap import build_runtime
from parsecore.models import ParseRequest
from parsecore.payload_schemas import payload_schema


REPORT_SCHEMA_VERSION = "2026-07-knowledge-contract-real-regression-v1"
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_KINDS = {
    "regulation",
    "scanned_manual",
    "mixed_pdf",
    "table_dense",
    "manual",
    "other",
}


@dataclass(frozen=True, slots=True)
class SampleSpec:
    alias: str
    kind: str
    path: Path
    page_start: int | None = None
    page_end: int | None = None

    @property
    def page_selection(self) -> str:
        if self.page_start is None or self.page_end is None:
            return "all"
        return f"{self.page_start}-{self.page_end}"


def _parse_sample(raw: str) -> SampleSpec:
    parts = [part.strip() for part in str(raw or "").split("|", 3)]
    if len(parts) not in {3, 4}:
        raise ValueError("sample must use alias|kind|path or alias|kind|path|page_start-page_end")
    alias, kind, raw_path = parts[:3]
    if not alias or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", alias):
        raise ValueError("sample alias must contain only letters, digits, dot, underscore, or dash")
    kind = kind.lower()
    if kind not in _SUPPORTED_KINDS:
        raise ValueError(f"unsupported sample kind: {kind}")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"sample source does not exist: {path.name}")
    page_start: int | None = None
    page_end: int | None = None
    if len(parts) == 4 and parts[3]:
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", parts[3])
        if match is None:
            raise ValueError("page selection must be one-based N or N-M")
        page_start = int(match.group(1))
        page_end = int(match.group(2) or page_start)
        if page_start < 1 or page_end < page_start:
            raise ValueError("page selection must be a positive ascending range")
        if path.suffix.lower() != ".pdf":
            raise ValueError("page selection is supported only for PDF samples")
    return SampleSpec(
        alias=alias,
        kind=kind,
        path=path,
        page_start=page_start,
        page_end=page_end,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _materialize_source(spec: SampleSpec, root: Path) -> tuple[Path, int | None]:
    if spec.page_start is None or spec.page_end is None:
        return spec.path, None

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(spec.path))
    if spec.page_end > len(reader.pages):
        raise ValueError(
            f"requested page {spec.page_end} exceeds source page count {len(reader.pages)}"
        )
    writer = PdfWriter()
    for index in range(spec.page_start - 1, spec.page_end):
        writer.add_page(reader.pages[index])
    target = root / f"{spec.alias}.pages-{spec.page_selection}.pdf"
    with target.open("wb") as output:
        writer.write(output)
    return target, len(reader.pages)


def _runtime_config(root: Path) -> Path:
    database_path = (root / "parsecore.db").as_posix()
    object_store_path = (root / "object-store").as_posix()
    log_path = (root / "job-events.jsonl").as_posix()
    (root / "object-store").mkdir(parents=True, exist_ok=True)
    config = f'''[project]
name = "knowledge-contract-real-regression"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 1
allow_external_file_paths = true
max_upload_bytes = 0
staged_upload_max_bytes = 0
log_path = "{log_path}"

[storage]
database_url = "sqlite:///{database_path}"
object_store = "local:///{object_store_path}"

[index]
mode = "hybrid"

[translation]
enabled = false
strategy = "lazy"

[product]
adapter = "embedded"

[quality_gate]
enabled = true
min_text_page_coverage = 0.98
min_table_unit_coverage = 0.95
min_unit_chunk_coverage = 0.98
min_reading_order_confidence = 0.75
allow_local_rerun = true
allow_manual_review = true

[providers.ocr]
enabled = true
provider = "rapidocr"

[[parsers]]
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]

[parsers.options.post_process]
dual_channel = true
layout_reading_order = true
adaptive_dual_channel = true
adaptive_dual_channel_max_page_ratio = 0.85
adaptive_dual_channel_min_pages = 8
adaptive_ocr_cache_fast_path = true
parse_cache = true
parse_cache_max_entries = 2
dual_table_min_rows = 2
dual_table_min_cols = 2
ocr_bad_pages = true
ocr_bad_page_min_cid_tokens = 5
ocr_bad_page_min_cid_char_ratio = 0.25
ocr_render_resolution = 110
ocr_confidence_threshold = 0.5
ocr_merge_line_gap_ratio = 1.6
'''
    config_path = root / "parsecore.toml"
    config_path.write_text(config, encoding="utf-8")
    return config_path


def _fingerprints_valid(items: Iterable[Mapping[str, Any]], field: str) -> bool:
    values = [str(item.get(field) or "") for item in items]
    return bool(values) and all(_FINGERPRINT_PATTERN.fullmatch(value) for value in values)


def _source_spans_valid(items: Iterable[Mapping[str, Any]]) -> bool:
    materialized = list(items)
    if not materialized:
        return True
    for item in materialized:
        span = item.get("source_span")
        if not isinstance(span, Mapping):
            return False
        start = int(span.get("page_start") or 0)
        end = int(span.get("page_end") or 0)
        precision = str(span.get("precision") or "")
        if start < 1 or end < start or precision not in {"page", "region"}:
            return False
        if precision == "region" and span.get("bbox") is None:
            return False
        if precision == "page" and not str(span.get("degraded_reason") or ""):
            return False
    return True


def _add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    summary: str,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "passed" if passed else "failed",
            "summary": summary,
        }
    )


def _validate_projection(
    checks: list[dict[str, Any]],
    *,
    schema_name: str,
    payload: Mapping[str, Any],
) -> None:
    try:
        Draft202012Validator(payload_schema(schema_name)).validate(dict(payload))
    except Exception as exc:
        _add_check(
            checks,
            f"schema:{schema_name}",
            False,
            f"schema validation failed ({type(exc).__name__})",
        )
    else:
        _add_check(checks, f"schema:{schema_name}", True, "payload matches frozen schema")


def _profile_for(kind: str) -> str:
    return "table-heavy" if kind == "table_dense" else "default"


def _integer(mapping: Mapping[str, Any], key: str, *, default: int = -1) -> int:
    value = mapping.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sample_result(spec: SampleSpec, root: Path) -> dict[str, Any]:
    started = perf_counter()
    checks: list[dict[str, Any]] = []
    corpus_hash = _sha256(spec.path)
    source_path, corpus_page_count = _materialize_source(spec, root)
    selected_hash = _sha256(source_path)
    runtime = build_runtime(_runtime_config(root))
    doc_key = hashlib.sha256(
        f"{spec.alias}|{corpus_hash}|{spec.page_selection}".encode("utf-8")
    ).hexdigest()[:20]
    doc_id = f"real-regression-{doc_key}"

    first = runtime.submit(
        ParseRequest(
            doc_id=doc_id,
            file_path=str(source_path),
            media_type="application/pdf",
            options={"profile": _profile_for(spec.kind)},
        )
    )
    second_job = runtime.restart_latest(doc_id=doc_id)
    second = runtime.execute(job_id=second_job.job_id)
    snapshot = runtime.get_document(doc_id=doc_id)
    ir = _document_projection(snapshot, projection="ir")
    coverage = _document_projection(snapshot, projection="coverage")
    reader = _document_projection(snapshot, projection="reader")

    _validate_projection(checks, schema_name="document-ir", payload=ir)
    _validate_projection(checks, schema_name="document-coverage", payload=coverage)
    _validate_projection(checks, schema_name="document-reader", payload=reader)

    blocks = [item for item in ir.get("blocks", []) if isinstance(item, Mapping)]
    tables = [item for item in ir.get("tables", []) if isinstance(item, Mapping)]
    figures = [item for item in ir.get("figures", []) if isinstance(item, Mapping)]
    units = [item for item in ir.get("knowledge_units", []) if isinstance(item, Mapping)]
    integrity = ir.get("source_integrity") if isinstance(ir.get("source_integrity"), Mapping) else {}
    summary = (
        coverage.get("coverage", {}).get("summary", {})
        if isinstance(coverage.get("coverage"), Mapping)
        else {}
    )
    diff = ir.get("knowledge_unit_diff") if isinstance(ir.get("knowledge_unit_diff"), Mapping) else {}
    diff_counts = diff.get("counts") if isinstance(diff.get("counts"), Mapping) else {}

    _add_check(
        checks,
        "source_integrity",
        integrity.get("status") == "verified"
        and integrity.get("hash_algorithm") == "sha256"
        and integrity.get("source_hash") == selected_hash,
        "streamed source SHA-256 is verified and matches the parsed bytes",
    )
    _add_check(checks, "non_empty_blocks", bool(blocks), f"{len(blocks)} IR blocks emitted")
    _add_check(checks, "non_empty_units", bool(units), f"{len(units)} KnowledgeUnits emitted")
    _add_check(
        checks,
        "stable_block_fingerprints",
        _fingerprints_valid(blocks, "block_fingerprint")
        and len({str(item.get("stable_block_id")) for item in blocks}) == len(blocks),
        "all blocks have unique stable ids and SHA-256 fingerprints",
    )
    _add_check(
        checks,
        "stable_table_fingerprints",
        (not tables)
        or (
            _fingerprints_valid(tables, "table_fingerprint")
            and len({str(item.get("stable_table_id")) for item in tables}) == len(tables)
        ),
        f"{len(tables)} tables have stable identities (vacuous when no table is detected)",
    )
    _add_check(
        checks,
        "stable_figure_fingerprints",
        (not figures)
        or (
            _fingerprints_valid(figures, "figure_fingerprint")
            and len({str(item.get("stable_figure_id")) for item in figures}) == len(figures)
        ),
        f"{len(figures)} figures have stable identities (vacuous when no figure is detected)",
    )
    _add_check(
        checks,
        "stable_unit_fingerprints",
        _fingerprints_valid(units, "unit_fingerprint")
        and _fingerprints_valid(units, "content_fingerprint")
        and _fingerprints_valid(units, "structure_fingerprint")
        and len({str(item.get("stable_unit_id")) for item in units}) == len(units),
        "all units have unique stable ids and content/structure/unit fingerprints",
    )
    _add_check(
        checks,
        "source_spans",
        _source_spans_valid([*blocks, *tables, *figures, *units]),
        "every emitted object has a valid region- or page-precision source span",
    )
    _add_check(
        checks,
        "coverage_conservation",
        _integer(summary, "total_unit_count") == len(units)
        and _integer(summary, "accounted_unit_count") == len(units)
        and _integer(summary, "unaccounted_unit_count") == 0
        and sum(int(value or 0) for value in (summary.get("processing_status_counts") or {}).values())
        == len(units),
        "coverage accounts for every emitted KnowledgeUnit exactly once",
    )
    unchanged_only = (
        not bool(diff.get("baseline"))
        and str(diff.get("previous_parse_run_id") or "") == first.job.job_id
        and str(diff.get("current_parse_run_id") or "") == second.job.job_id
        and int(diff_counts.get("unchanged") or 0) == len(units)
        and all(
            int(diff_counts.get(status) or 0) == 0
            for status in ("added", "changed", "removed", "relocated", "unknown")
        )
    )
    _add_check(
        checks,
        "identical_reparse_diff",
        unchanged_only,
        "identical reparse maps every unit as unchanged with no ambiguous residue",
    )

    table_unit_ids = {
        str(table_id)
        for unit in units
        for table_id in unit.get("source_table_ids", [])
        if str(table_id)
    }
    emitted_table_ids = {str(table.get("table_id") or "") for table in tables if str(table.get("table_id") or "")}
    _add_check(
        checks,
        "table_to_unit_mapping",
        emitted_table_ids.issubset(table_unit_ids),
        f"{len(emitted_table_ids)} detected tables are represented by KnowledgeUnits",
    )

    roles = Counter(str(unit.get("semantic_role") or "unknown") for unit in units)
    providers = Counter(
        str((block.get("provenance") or {}).get("provider_id") or "unknown")
        for block in blocks
    )
    source_kinds = Counter(str(block.get("source_kind") or "unknown") for block in blocks)
    section_ids = {str(unit.get("section_id") or "") for unit in units if str(unit.get("section_id") or "")}
    hierarchy_units = [unit for unit in units if str(unit.get("section_id") or "")]
    nested_section_count = len(
        {
            str(unit.get("section_id") or "")
            for unit in hierarchy_units
            if str(unit.get("parent_section_id") or "")
        }
    )
    _add_check(
        checks,
        "section_hierarchy_contract",
        all(
            int(unit.get("section_level") or 0) > 0
            and bool(unit.get("title_path"))
            for unit in hierarchy_units
        ),
        f"{len(section_ids)} sections carry levels and title paths",
    )
    if spec.kind in {"regulation", "table_dense", "manual"}:
        _add_check(
            checks,
            "real_hierarchy_detected",
            bool(section_ids) and nested_section_count > 0,
            f"{len(section_ids)} sections detected, including {nested_section_count} nested sections",
        )
    if spec.kind == "regulation":
        structured_roles = sum(roles[role] for role in ("clause", "definition", "list_item", "procedure", "procedure_step"))
        _add_check(
            checks,
            "regulation_structural_roles",
            structured_roles > 0,
            f"{structured_roles} regulation-oriented generic structural roles detected",
        )
    if spec.kind == "scanned_manual":
        ocr_blocks = sum(
            count
            for name, count in [*providers.items(), *source_kinds.items()]
            if "ocr" in name.lower()
        )
        _add_check(
            checks,
            "scanned_manual_ocr",
            ocr_blocks > 0,
            f"OCR provenance/source-kind observations: {ocr_blocks}",
        )
    if spec.kind == "table_dense":
        _add_check(
            checks,
            "table_dense_detection",
            bool(tables),
            f"{len(tables)} structured tables detected",
        )

    precision_counts = Counter(
        str((unit.get("source_span") or {}).get("precision") or "unknown")
        for unit in units
    )
    continuity_counts = Counter(
        str((unit.get("continuity") or {}).get("kind") or "none")
        for unit in units
    )
    quality_codes = Counter(
        str(signal.get("code") or "unknown")
        for signal in coverage.get("quality_signals", [])
        if isinstance(signal, Mapping)
    )
    status = "passed" if all(check["status"] == "passed" for check in checks) else "failed"
    return {
        "alias": spec.alias,
        "kind": spec.kind,
        "status": status,
        "corpus_sha256": corpus_hash,
        "parsed_source_sha256": selected_hash,
        "source_size_bytes": spec.path.stat().st_size,
        "page_selection": spec.page_selection,
        "corpus_page_count": corpus_page_count,
        "elapsed_seconds": round(perf_counter() - started, 3),
        "metrics": {
            "page_count": len(ir.get("pages", [])),
            "block_count": len(blocks),
            "table_count": len(tables),
            "figure_count": len(figures),
            "knowledge_unit_count": len(units),
            "section_count": len(section_ids),
            "nested_section_count": nested_section_count,
            "semantic_role_counts": dict(sorted(roles.items())),
            "provider_counts": dict(sorted(providers.items())),
            "source_kind_counts": dict(sorted(source_kinds.items())),
            "source_precision_counts": dict(sorted(precision_counts.items())),
            "continuity_counts": dict(sorted(continuity_counts.items())),
            "processing_status_counts": dict(summary.get("processing_status_counts") or {}),
            "quality_signal_counts": dict(sorted(quality_codes.items())),
            "diff_counts": dict(diff_counts),
        },
        "checks": checks,
    }


def _sanitized_failure(spec: SampleSpec, exc: Exception) -> dict[str, Any]:
    message = str(exc).replace(str(spec.path), "<source>")
    return {
        "alias": spec.alias,
        "kind": spec.kind,
        "status": "failed",
        "corpus_sha256": _sha256(spec.path),
        "source_size_bytes": spec.path.stat().st_size,
        "page_selection": spec.page_selection,
        "error": {"type": type(exc).__name__, "message": message[:500]},
        "checks": [],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="ALIAS|KIND|PATH[|PAGES]",
        help="repeatable authorized real-corpus sample",
    )
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.sample:
        print("at least one --sample is required", file=sys.stderr)
        return 2
    try:
        specs = [_parse_sample(raw) for raw in args.sample]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    aliases = [spec.alias for spec in specs]
    if len(set(aliases)) != len(aliases):
        print("sample aliases must be unique", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    for spec in specs:
        with TemporaryDirectory(prefix=f"parsecore-real-{spec.alias}-") as temp_dir:
            try:
                result = _sample_result(spec, Path(temp_dir))
            except Exception as exc:  # pragma: no cover - real corpus/provider dependent
                result = _sanitized_failure(spec, exc)
        results.append(result)
        print(f"{spec.alias}: {result['status']}")

    passed = sum(1 for result in results if result["status"] == "passed")
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "privacy": {
            "source_paths_included": False,
            "extracted_text_included": False,
            "corpus_content_committed": False,
        },
        "summary": {
            "status": "passed" if passed == len(results) else "failed",
            "sample_count": len(results),
            "passed_sample_count": passed,
            "failed_sample_count": len(results) - passed,
        },
        "samples": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
