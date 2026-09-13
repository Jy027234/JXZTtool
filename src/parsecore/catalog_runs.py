"""Bounded, resumable execution for versioned structured catalog extractors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog_contracts import (
    CAAC_APPROVED_CATALOG_EXTRACTOR,
    CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
    validate_catalog_record_shape,
)
from .catalog_omissions import (
    SourceOmissionReviewError,
    normalize_source_omission_review,
    source_omission_review_index,
)
from .catalog_extractors import (
    CatalogPage,
    anchor_row_numbers,
    default_catalog_extractor_registry,
    detect_catalog_sections,
)
from .pdf_parts import detect_pdf_page_count, plan_pdf_parts


CATALOG_RUN_LEDGER_VERSION = "parsecore-caac-catalog-run.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PART_STATES = frozenset({"pending", "running", "done", "failed"})


@dataclass(frozen=True, slots=True)
class _CatalogRunSection:
    """A deliberately enabled full-run section and its immutable PDF span."""

    title: str
    approval_type: str
    page_start: int
    page_end: int
    core_field_paths: tuple[str, ...]


_CATALOG_RUN_SECTIONS: dict[str, _CatalogRunSection] = {
    "tc": _CatalogRunSection(
        title="型号合格证（TC）",
        approval_type="tc",
        page_start=3,
        page_end=7,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.modelOriginal",
            "approval.latestApprovedOn",
        ),
    ),
    "foreign_product_recognition": _CatalogRunSection(
        title="获得国外认可的民用航空产品",
        approval_type="foreign_product_recognition",
        page_start=8,
        page_end=9,
        core_field_paths=(
            "holder.nameOriginal",
            "item.nameOriginal",
            "item.modelOriginal",
            "approval.numberOriginal",
            "attributes.foreignCountry",
            "attributes.foreignApprovalNumber",
            "approval.latestApprovedOn",
        ),
    ),
    "tda": _CatalogRunSection(
        title="型号设计批准书（TDA）",
        approval_type="tda",
        page_start=10,
        page_end=10,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.modelOriginal",
            "approval.latestApprovedOn",
        ),
    ),
    "pc": _CatalogRunSection(
        title="生产许可证（PC）",
        approval_type="pc",
        page_start=11,
        page_end=26,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.modelOriginal",
            "item.descriptionOriginal",
            "approval.latestApprovedOn",
        ),
    ),
    "stc": _CatalogRunSection(
        title="补充型号合格证（STC）",
        approval_type="stc",
        page_start=28,
        page_end=54,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "attributes.originalModel",
            "attributes.originalApprovalNumber",
            "item.modelOriginal",
            "approval.latestApprovedOn",
        ),
    ),
    "mda": _CatalogRunSection(
        title="改装设计批准书（MDA）",
        approval_type="mda",
        page_start=55,
        page_end=154,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "attributes.modelCertificateNumber",
            "item.productTypeOriginal",
            "item.descriptionOriginal",
            "approval.latestApprovedOn",
        ),
    ),
    "pma_holder": _CatalogRunSection(
        title="零部件制造人批准书（PMA）",
        approval_type="pma",
        page_start=155,
        page_end=161,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.descriptionOriginal",
            "attributes.qualityManualNumber",
            "attributes.qualityManualRevision",
            "approval.latestApprovedOn",
        ),
    ),
    "pma_item": _CatalogRunSection(
        title="已获批准的PMA产品目录清单",
        approval_type="pma",
        page_start=162,
        page_end=16785,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.nameOriginal",
            "item.partNumberOriginal",
        ),
    ),
    "ctsoa": _CatalogRunSection(
        title="技术标准规定项目批准书（CTSOA）",
        approval_type="ctsoa",
        page_start=16786,
        page_end=16799,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.nameOriginal",
            "item.ctsoCodesOriginal",
        ),
    ),
    # Historical compatibility key only.  This is not a foreign-TSO catalog:
    # it contains Chinese CTSOA/CTSO projects recognised by foreign authorities.
    "foreign_tso_recognition": _CatalogRunSection(
        title="获得国外当局认可的技术标准规定项目",
        approval_type="foreign_tso_recognition",
        page_start=16800,
        page_end=16803,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.nameOriginal",
            "item.modelOriginal",
            "item.ctsoCodesOriginal",
        ),
    ),
    "vtc": _CatalogRunSection(
        title="型号认可证（VTC）",
        approval_type="vtc",
        page_start=16804,
        page_end=16826,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.productTypeOriginal",
            "item.modelOriginal",
            "attributes.authority",
            "attributes.foreignApprovalNumber",
            "approval.latestApprovedOn",
        ),
    ),
    "vstc": _CatalogRunSection(
        title="补充型号认可证（VSTC）",
        approval_type="vstc",
        page_start=16827,
        page_end=17050,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.productTypeOriginal",
            "item.modelOriginal",
            "item.descriptionOriginal",
            "attributes.authority",
            "attributes.foreignApprovalNumber",
            "approval.latestApprovedOn",
        ),
    ),
    "vda": _CatalogRunSection(
        title="设计批准认可证（VDA）",
        approval_type="vda",
        page_start=17051,
        page_end=17100,
        core_field_paths=(
            "approval.numberOriginal",
            "holder.nameOriginal",
            "item.productTypeOriginal",
            "item.nameOriginal",
            "item.modelOriginal",
            "item.ctsoCodesOriginal",
            "attributes.authority",
            "attributes.foreignApprovalNumber",
            "approval.latestApprovedOn",
        ),
    ),
}


class CatalogRunError(ValueError):
    """A catalog run violated a bounded-execution or integrity invariant."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    return text.encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    payload = b"".join(_json_bytes(dict(row), pretty=False) for row in rows)
    _atomic_write_bytes(path, payload)


def _artifact_descriptor(
    path: Path,
    *,
    root: Path,
    record_count: int,
) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "records": record_count,
        "sha256": file_sha256(path),
    }


def _required_sha256(value: str, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise CatalogRunError(f"{label} must be a lowercase SHA-256")
    return normalized


def _required_positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CatalogRunError(f"{label} must be a positive integer")
    return value


def _catalog_run_section(section_key: str) -> tuple[str, _CatalogRunSection]:
    normalized = str(section_key or "").strip()
    section = _CATALOG_RUN_SECTIONS.get(normalized)
    if section is None:
        allowed = ", ".join(sorted(_CATALOG_RUN_SECTIONS))
        raise CatalogRunError(
            f"catalog full-run section_key must be one of: {allowed}"
        )
    return normalized, section


def _record_field_value(record: Mapping[str, Any], field_path: str) -> Any:
    """Return a supported contract field without silently traversing arbitrary data."""

    parent, separator, key = field_path.partition(".")
    if not separator or parent not in {"approval", "holder", "item", "attributes"} or not key:
        raise CatalogRunError(f"unsupported catalog core field path: {field_path}")
    container = record.get(parent)
    return container.get(key) if isinstance(container, Mapping) else None


def _profile_payload(
    *,
    profile_name: str,
    target_pages_per_part: int,
    part_context_pages: int,
    max_active_parts_per_doc: int,
    record_schema: str,
    stream_pages: bool,
    stream_lines: bool,
    stream_records: bool,
    enable_record_fts: bool,
    enable_ocr: bool,
) -> dict[str, Any]:
    if target_pages_per_part < 1:
        raise CatalogRunError("target_pages_per_part must be positive")
    if part_context_pages < 0:
        raise CatalogRunError("part_context_pages must be non-negative")
    if not 1 <= max_active_parts_per_doc <= 2:
        raise CatalogRunError("max_active_parts_per_doc must be 1 or 2")
    normalized_schema = str(record_schema or "").strip()
    try:
        default_catalog_extractor_registry().get(normalized_schema)
    except KeyError as exc:
        raise CatalogRunError(f"unsupported record_schema: {normalized_schema}") from exc
    if enable_ocr:
        raise CatalogRunError("CAAC approved-catalog requires enable_ocr=false")
    if not (stream_pages and stream_lines and stream_records):
        raise CatalogRunError(
            "catalog profile must enable stream_pages, stream_lines, and stream_records"
        )
    return {
        "name": str(profile_name or "").strip(),
        "targetPagesPerPart": target_pages_per_part,
        "partContextPages": part_context_pages,
        "maxActivePartsPerDoc": max_active_parts_per_doc,
        "recordSchema": normalized_schema,
        "streamPages": stream_pages,
        "streamLines": stream_lines,
        "streamRecords": stream_records,
        "enableRecordFts": enable_record_fts,
        "enableOcr": enable_ocr,
    }


def _build_source_omission_review(
    *,
    source_omission_review: Path | None,
    record_schema: str,
    source_snapshot_sha256: str,
    section_key: str,
    page_start: int,
    page_end: int,
) -> dict[str, Any] | None:
    """Load the V2-only omission allow-list into an immutable run ledger."""

    if record_schema != CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        if source_omission_review is not None:
            raise CatalogRunError(
                "source_omission_review is only supported by caac-approved-catalog.v2"
            )
        return None
    if source_omission_review is None:
        raise CatalogRunError(
            "caac-approved-catalog.v2 requires a source_omission_review artifact"
        )
    review_path = source_omission_review.resolve(strict=True)
    try:
        raw_review = _read_json_object(review_path)
        review = normalize_source_omission_review(
            raw_review,
            source_snapshot_sha256=source_snapshot_sha256,
            section_key=section_key,
        )
    except (SourceOmissionReviewError, OSError, json.JSONDecodeError) as exc:
        raise CatalogRunError(f"invalid source omission review: {review_path}: {exc}") from exc
    for entry in review["entries"]:
        pdf_page = int(entry["pdfPage"])
        if not page_start <= pdf_page <= page_end:
            raise CatalogRunError(
                "source omission review entry pdfPage is outside the bounded run"
            )
    payload = _json_bytes(review)
    return {
        "artifact": {
            "path": "quality/source-omission-review.json",
            "bytes": len(payload),
            "records": len(review["entries"]),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "payload": review,
    }


def _source_omission_review_from_ledger(
    ledger: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate the immutable V2 review payload retained in a run ledger."""

    profile = ledger.get("profile")
    document = ledger.get("document")
    scope = ledger.get("scope")
    if not all(isinstance(item, Mapping) for item in (profile, document, scope)):
        raise CatalogRunError("catalog run ledger metadata is incomplete")
    record_schema = str(profile.get("recordSchema") or "")
    raw_review = ledger.get("sourceOmissionReview")
    if record_schema != CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        if raw_review is not None:
            raise CatalogRunError("only V2 catalog runs may contain sourceOmissionReview")
        return None
    if not isinstance(raw_review, Mapping):
        raise CatalogRunError("V2 catalog run ledger is missing sourceOmissionReview")
    raw_payload = raw_review.get("payload")
    artifact = raw_review.get("artifact")
    if not isinstance(raw_payload, Mapping) or not isinstance(artifact, Mapping):
        raise CatalogRunError("V2 sourceOmissionReview is incomplete")
    try:
        review = normalize_source_omission_review(
            raw_payload,
            source_snapshot_sha256=str(document.get("sha256") or ""),
            section_key=str(scope.get("sectionKey") or ""),
        )
    except SourceOmissionReviewError as exc:
        raise CatalogRunError(f"V2 sourceOmissionReview is invalid: {exc}") from exc
    if review != raw_payload:
        raise CatalogRunError("V2 sourceOmissionReview is not canonical")
    payload = _json_bytes(review)
    expected = {
        "path": "quality/source-omission-review.json",
        "bytes": len(payload),
        "records": len(review["entries"]),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    if dict(artifact) != expected:
        raise CatalogRunError("V2 sourceOmissionReview artifact descriptor is invalid")
    return {"artifact": expected, "payload": review}


def build_catalog_run_ledger(
    *,
    source: Path,
    output: Path,
    run_id: str,
    expected_source_sha256: str,
    expected_page_count: int,
    data_cutoff_on: str,
    section_key: str,
    page_start: int,
    page_end: int,
    profile_name: str,
    target_pages_per_part: int,
    part_context_pages: int,
    max_active_parts_per_doc: int,
    record_schema: str,
    stream_pages: bool = True,
    stream_lines: bool = True,
    stream_records: bool = True,
    enable_record_fts: bool = True,
    enable_ocr: bool = False,
    parser_version: str = "0.1.0",
    verify_source: bool = True,
    source_omission_review: Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic ledger; write nothing and publish nothing."""

    source = source.resolve(strict=True)
    output = output.resolve()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id or not re.fullmatch(r"[A-Za-z0-9._-]+", normalized_run_id):
        raise CatalogRunError("run_id must contain only A-Z, a-z, 0-9, dot, underscore, or dash")
    expected_hash = _required_sha256(
        expected_source_sha256,
        label="expected_source_sha256",
    )
    page_count = _required_positive_int(expected_page_count, label="expected_page_count")
    if page_start < 1 or page_end < page_start or page_end > page_count:
        raise CatalogRunError("invalid catalog page range")
    normalized_section_key, section = _catalog_run_section(section_key)
    if (page_start, page_end) != (section.page_start, section.page_end):
        raise CatalogRunError(
            "catalog full-run page range must match the registered section span: "
            f"{normalized_section_key}={section.page_start}-{section.page_end}"
        )
    profile = _profile_payload(
        profile_name=profile_name,
        target_pages_per_part=target_pages_per_part,
        part_context_pages=part_context_pages,
        max_active_parts_per_doc=max_active_parts_per_doc,
        record_schema=record_schema,
        stream_pages=stream_pages,
        stream_lines=stream_lines,
        stream_records=stream_records,
        enable_record_fts=enable_record_fts,
        enable_ocr=enable_ocr,
    )
    omission_review = _build_source_omission_review(
        source_omission_review=source_omission_review,
        record_schema=str(profile["recordSchema"]),
        source_snapshot_sha256=expected_hash,
        section_key=normalized_section_key,
        page_start=page_start,
        page_end=page_end,
    )
    if verify_source:
        actual_hash = file_sha256(source)
        if actual_hash != expected_hash:
            raise CatalogRunError(
                f"source SHA-256 mismatch: expected {expected_hash}, received {actual_hash}"
            )
        actual_pages = detect_pdf_page_count(str(source))
        if actual_pages != page_count:
            raise CatalogRunError(
                f"source page count mismatch: expected {page_count}, received {actual_pages}"
            )
    specs = plan_pdf_parts(
        normalized_run_id,
        page_count,
        target_pages_per_part=target_pages_per_part,
        part_context_pages=part_context_pages,
        profile=profile_name,
        page_start=page_start,
        page_end=page_end,
        file_size_bytes=source.stat().st_size,
    )
    parts = [
        {
            **spec,
            "state": "pending",
            "attemptCount": 0,
            "lastError": None,
            "latestAttemptPath": None,
        }
        for spec in specs
    ]
    created_at = utc_now()
    extractor = default_catalog_extractor_registry().get(str(profile["recordSchema"]))
    ledger = {
        "schemaVersion": CATALOG_RUN_LEDGER_VERSION,
        "runId": normalized_run_id,
        "state": "planned",
        "createdAt": created_at,
        "updatedAt": created_at,
        "publicationAllowed": False,
        "document": {
            "fileName": source.name,
            "filePath": str(source),
            "sha256": expected_hash,
            "byteLength": source.stat().st_size,
            "pageCount": page_count,
            "dataCutoffOn": str(data_cutoff_on),
        },
        "outputPath": str(output),
        "parser": {
            "name": "parsecore",
            "version": str(parser_version),
            "extractor": CAAC_APPROVED_CATALOG_EXTRACTOR,
            "extractorVersion": str(getattr(extractor, "extractor_version", "")),
        },
        "profile": profile,
        "scope": {
            "sectionKey": normalized_section_key,
            "ownedPageStart": page_start,
            "ownedPageEnd": page_end,
            "ownedPageCount": page_end - page_start + 1,
        },
        "parts": parts,
        "summary": {},
    }
    if omission_review is not None:
        ledger["sourceOmissionReview"] = omission_review
    _refresh_ledger_summary(ledger)
    return ledger


def initialize_catalog_run(output: Path, ledger: Mapping[str, Any]) -> Path:
    output = output.resolve()
    ledger_path = output / "run-ledger.json"
    if ledger_path.exists():
        raise CatalogRunError(f"catalog run already exists: {ledger_path}")
    if output.exists() and any(output.iterdir()):
        raise CatalogRunError(
            "output directory is not empty and has no resumable run-ledger.json"
        )
    output.mkdir(parents=True, exist_ok=True)
    omission_review = _source_omission_review_from_ledger(ledger)
    if omission_review is not None:
        review_artifact = omission_review["artifact"]
        review_path = output / str(review_artifact["path"])
        _atomic_write_bytes(review_path, _json_bytes(omission_review["payload"]))
        if (
            review_path.stat().st_size != int(review_artifact["bytes"])
            or file_sha256(review_path) != str(review_artifact["sha256"])
        ):
            raise CatalogRunError("failed to materialize immutable V2 source omission review")
    _atomic_write_json(ledger_path, dict(ledger))
    return ledger_path


def load_catalog_run(output: Path) -> dict[str, Any]:
    ledger_path = output.resolve(strict=True) / "run-ledger.json"
    try:
        value = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogRunError(f"invalid catalog run ledger: {ledger_path}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != CATALOG_RUN_LEDGER_VERSION:
        raise CatalogRunError("unsupported catalog run ledger")
    parts = value.get("parts")
    if not isinstance(parts, list) or not parts:
        raise CatalogRunError("catalog run ledger has no parts")
    for part in parts:
        if not isinstance(part, dict) or part.get("state") not in _PART_STATES:
            raise CatalogRunError("catalog run ledger contains an invalid part")
    return value


def _save_catalog_run(output: Path, ledger: Mapping[str, Any]) -> None:
    _atomic_write_json(output.resolve() / "run-ledger.json", dict(ledger))


def recover_interrupted_catalog_parts(ledger: dict[str, Any]) -> int:
    recovered = 0
    for part in ledger["parts"]:
        if part.get("state") != "running":
            continue
        part["state"] = "pending"
        part["lastError"] = "interrupted_before_checkpoint"
        part["interruptedAt"] = utc_now()
        recovered += 1
    if recovered:
        ledger["updatedAt"] = utc_now()
        _refresh_ledger_summary(ledger)
    return recovered


def select_catalog_parts(
    ledger: Mapping[str, Any],
    *,
    failed_only: bool,
    max_parts: int,
) -> list[dict[str, Any]]:
    _required_positive_int(max_parts, label="max_parts")
    allowed = {"failed"} if failed_only else {"pending", "failed"}
    selected = [
        part
        for part in ledger.get("parts") or ()
        if isinstance(part, dict) and part.get("state") in allowed
    ]
    selected.sort(key=lambda part: int(part.get("part_index") or 0))
    return selected[:max_parts]


def _load_catalog_pages(
    source: Path,
    *,
    input_page_start: int,
    input_page_end: int,
    deadline: float,
) -> tuple[tuple[CatalogPage, ...], list[dict[str, Any]]]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - parser extra is required in production
        raise CatalogRunError("pdfplumber is required for catalog runs") from exc

    pages: list[CatalogPage] = []
    observations: list[dict[str, Any]] = []
    with pdfplumber.open(str(source)) as document:
        for page_number in range(input_page_start, input_page_end + 1):
            if time.monotonic() >= deadline:
                raise TimeoutError("catalog_part_runtime_limit_exceeded")
            page = document.pages[page_number - 1]
            text = page.extract_text() or ""
            raw_words = tuple(page.extract_words() or ())
            words = tuple(
                {**dict(word), "id": f"p{page_number:05d}w{index:05d}"}
                for index, word in enumerate(raw_words, start=1)
            )
            catalog_page = CatalogPage(
                pdf_page=page_number,
                text=text,
                words=words,
                printed_page_label=None,
                metadata={
                    "rects": tuple(page.rects or ()),
                    "pageHeight": float(page.height),
                    "pageWidth": float(page.width),
                },
            )
            pages.append(catalog_page)
            sections = detect_catalog_sections((catalog_page,))
            anchors, _ = anchor_row_numbers(catalog_page)
            observations.append(
                {
                    "pdfPage": page_number,
                    "wordCount": len(words),
                    "sections": [section.section_key for section in sections],
                    "rowAnchorCount": len(anchors),
                }
            )
    return tuple(pages), observations


def execute_catalog_part_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Process one part in an isolated worker and write an immutable attempt."""

    started = time.monotonic()
    started_at = utc_now()
    source = Path(str(task["sourcePath"])).resolve(strict=True)
    output = Path(str(task["outputPath"])).resolve()
    spec = dict(task["part"])
    part_id = str(spec["part_id"])
    attempt_number = int(task["attemptNumber"])
    attempt_root = output / "parts" / part_id / f"attempt-{attempt_number:03d}"
    if attempt_root.exists():
        raise CatalogRunError(f"attempt directory already exists: {attempt_root}")
    attempt_root.mkdir(parents=True, exist_ok=False)
    max_runtime_seconds = float(task["maxRuntimeSeconds"])
    deadline = started + max_runtime_seconds

    pages, observations = _load_catalog_pages(
        source,
        input_page_start=int(spec["input_page_start"]),
        input_page_end=int(spec["input_page_end"]),
        deadline=deadline,
    )
    extractor = default_catalog_extractor_registry().get(str(task["recordSchema"]))
    continuation_state: dict[str, Any] = {
        "ownedPageStart": int(spec["owned_page_start"]),
        "ownedPageEnd": int(spec["owned_page_end"]),
        "sectionKey": str(task["sectionKey"]),
    }
    if task.get("sourceOmissionReview") is not None:
        continuation_state["sourceOmissionReview"] = task["sourceOmissionReview"]
    result = extractor.extract(
        pages=pages,
        source_snapshot_sha256=str(task["sourceSha256"]),
        parser_version=str(task["parserVersion"]),
        continuation_state=continuation_state,
    )
    max_records = int(task["maxRecordsPerPart"])
    if len(result.candidate_rows) > max_records:
        raise CatalogRunError(
            f"part candidate-row limit exceeded: {len(result.candidate_rows)} > {max_records}"
        )
    record_ids: set[str] = set()
    for record in result.records:
        validate_catalog_record_shape(record)
        record_id = str(record.get("recordId") or "")
        if record_id in record_ids:
            raise CatalogRunError(f"duplicate recordId inside part: {record_id}")
        record_ids.add(record_id)
    if len(result.candidate_rows) != len(result.records) + len(result.rejected_rows):
        raise CatalogRunError("candidate row accounting mismatch inside part")

    paths = {
        "records": attempt_root / "records" / f"{task['sectionKey']}.jsonl",
        "fieldEvidence": attempt_root / "evidence" / "fields.jsonl",
        "candidateRows": attempt_root / "quality" / "candidate-rows.jsonl",
        "rejectedRows": attempt_root / "quality" / "rejected-rows.jsonl",
        "signals": attempt_root / "quality" / "signals.jsonl",
        "observations": attempt_root / "quality" / "observations.json",
    }
    _atomic_write_jsonl(paths["records"], result.records)
    _atomic_write_jsonl(paths["fieldEvidence"], result.field_evidence)
    _atomic_write_jsonl(paths["candidateRows"], result.candidate_rows)
    _atomic_write_jsonl(paths["rejectedRows"], result.rejected_rows)
    _atomic_write_jsonl(paths["signals"], result.quality_signals)
    _atomic_write_json(paths["observations"], observations)
    counts = {
        "inputPages": len(pages),
        "ownedPages": int(spec["page_count"]),
        "candidateRows": len(result.candidate_rows),
        "records": len(result.records),
        "rejectedRows": len(result.rejected_rows),
        "fieldEvidence": len(result.field_evidence),
        "qualitySignals": len(result.quality_signals),
    }
    artifacts = {
        key: _artifact_descriptor(
            path,
            root=attempt_root,
            record_count=(
                len(observations)
                if key == "observations"
                else counts[
                    {
                        "records": "records",
                        "fieldEvidence": "fieldEvidence",
                        "candidateRows": "candidateRows",
                        "rejectedRows": "rejectedRows",
                        "signals": "qualitySignals",
                    }[key]
                ]
            ),
        )
        for key, path in paths.items()
    }
    completed_at = utc_now()
    checkpoint = {
        "schemaVersion": CATALOG_RUN_LEDGER_VERSION,
        "runId": str(task["runId"]),
        "partId": part_id,
        "partIndex": int(spec["part_index"]),
        "attempt": attempt_number,
        "state": "done",
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationSeconds": round(time.monotonic() - started, 3),
        "publicationAllowed": False,
        "inputPageRange": {
            "start": int(spec["input_page_start"]),
            "end": int(spec["input_page_end"]),
        },
        "ownedPageRange": {
            "start": int(spec["owned_page_start"]),
            "end": int(spec["owned_page_end"]),
        },
        "counts": counts,
        "qualitySignalCodes": dict(
            sorted(
                Counter(
                    str(signal.get("code") or "unknown")
                    for signal in result.quality_signals
                ).items()
            )
        ),
        "continuationState": dict(result.continuation_state),
        "artifacts": artifacts,
    }
    checkpoint_path = attempt_root / "checkpoint.json"
    _atomic_write_json(checkpoint_path, checkpoint)
    return {
        "partId": part_id,
        "partIndex": int(spec["part_index"]),
        "attempt": attempt_number,
        "attemptPath": attempt_root.relative_to(output).as_posix(),
        "checkpointSha256": file_sha256(checkpoint_path),
        "completedAt": completed_at,
        "durationSeconds": checkpoint["durationSeconds"],
        "counts": counts,
        "qualitySignalCodes": checkpoint["qualitySignalCodes"],
    }


def _refresh_ledger_summary(ledger: dict[str, Any]) -> None:
    states = Counter(str(part.get("state") or "pending") for part in ledger["parts"])
    counts: Counter[str] = Counter()
    for part in ledger["parts"]:
        if part.get("state") != "done" or not isinstance(part.get("counts"), Mapping):
            continue
        counts.update(
            {
                key: int(value)
                for key, value in part["counts"].items()
                if isinstance(value, int)
            }
        )
    if states["done"] == len(ledger["parts"]):
        state = "completed"
    elif states["running"]:
        state = "running"
    elif states["done"] or states["failed"]:
        state = "partial"
    else:
        state = "planned"
    ledger["state"] = state
    ledger["summary"] = {
        "partCount": len(ledger["parts"]),
        "partStates": dict(sorted(states.items())),
        "completedCounts": dict(sorted(counts.items())),
    }


def _verify_catalog_run_source(ledger: Mapping[str, Any], source: Path) -> None:
    source = source.resolve(strict=True)
    document = ledger.get("document")
    if not isinstance(document, Mapping):
        raise CatalogRunError("catalog run document metadata is missing")
    expected_hash = _required_sha256(str(document.get("sha256") or ""), label="source sha256")
    if source.stat().st_size != int(document.get("byteLength") or 0):
        raise CatalogRunError("catalog run source byte length changed")
    actual_hash = file_sha256(source)
    if actual_hash != expected_hash:
        raise CatalogRunError("catalog run source SHA-256 changed")
    if str(source) != str(document.get("filePath") or ""):
        raise CatalogRunError("catalog run source path changed")


def execute_catalog_run(
    *,
    output: Path,
    source: Path,
    resume: bool,
    failed_only: bool,
    max_parts: int,
    max_runtime_seconds: float,
    max_records_per_part: int = 20_000,
    part_worker: Callable[[Mapping[str, Any]], dict[str, Any]] = execute_catalog_part_task,
) -> dict[str, Any]:
    """Execute a bounded selection with at most two active child processes."""

    output = output.resolve(strict=True)
    ledger = load_catalog_run(output)
    _verify_catalog_run_source(ledger, source)
    if max_runtime_seconds <= 0:
        raise CatalogRunError("max_runtime_seconds must be positive")
    _required_positive_int(max_records_per_part, label="max_records_per_part")
    if any(part.get("state") == "running" for part in ledger["parts"]):
        if not resume:
            raise CatalogRunError("run has interrupted running parts; use --resume")
        recover_interrupted_catalog_parts(ledger)
        _save_catalog_run(output, ledger)
    if ledger.get("state") != "planned" and not resume and not failed_only:
        raise CatalogRunError("existing run requires --resume or --failed-only")
    selected = select_catalog_parts(
        ledger,
        failed_only=failed_only,
        max_parts=max_parts,
    )
    if not selected:
        return {
            "runId": ledger["runId"],
            "state": ledger["state"],
            "selectedParts": 0,
            "message": "no eligible parts",
            "summary": ledger["summary"],
        }
    profile = ledger.get("profile")
    parser = ledger.get("parser")
    document = ledger.get("document")
    scope = ledger.get("scope")
    if not all(isinstance(item, Mapping) for item in (profile, parser, document, scope)):
        raise CatalogRunError("catalog run ledger metadata is incomplete")
    omission_review = _source_omission_review_from_ledger(ledger)
    max_active = int(profile.get("maxActivePartsPerDoc") or 0)
    if not 1 <= max_active <= 2:
        raise CatalogRunError("catalog run active-part limit is not 1 or 2")
    deadline = time.monotonic() + max_runtime_seconds
    selected_queue = list(selected)
    active: dict[Future[dict[str, Any]], dict[str, Any]] = {}
    completed_this_call = 0
    failed_this_call = 0
    started_this_call = 0

    executor = ProcessPoolExecutor(max_workers=max_active)
    try:
        while selected_queue or active:
            while selected_queue and len(active) < max_active and time.monotonic() < deadline:
                part = selected_queue.pop(0)
                part["state"] = "running"
                part["attemptCount"] = int(part.get("attemptCount") or 0) + 1
                part["startedAt"] = utc_now()
                part["lastError"] = None
                ledger["updatedAt"] = utc_now()
                _refresh_ledger_summary(ledger)
                _save_catalog_run(output, ledger)
                remaining = max(1.0, deadline - time.monotonic())
                task = {
                    "runId": ledger["runId"],
                    "sourcePath": str(source.resolve()),
                    "outputPath": str(output),
                    "sourceSha256": document["sha256"],
                    "recordSchema": profile["recordSchema"],
                    "parserVersion": parser["version"],
                    "sectionKey": scope["sectionKey"],
                    "part": dict(part),
                    "attemptNumber": part["attemptCount"],
                    "maxRuntimeSeconds": remaining,
                    "maxRecordsPerPart": max_records_per_part,
                }
                if omission_review is not None:
                    task["sourceOmissionReview"] = omission_review["payload"]
                active[executor.submit(part_worker, task)] = part
                started_this_call += 1
            if not active:
                break
            done, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
            for future in done:
                part = active.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - persisted as a failed part
                    part["state"] = "failed"
                    part["lastError"] = f"{type(exc).__name__}: {exc}"[:2000]
                    part["failedAt"] = utc_now()
                    failed_this_call += 1
                else:
                    part["state"] = "done"
                    part["latestAttemptPath"] = result["attemptPath"]
                    part["checkpointSha256"] = result["checkpointSha256"]
                    part["completedAt"] = result["completedAt"]
                    part["durationSeconds"] = result["durationSeconds"]
                    part["counts"] = result["counts"]
                    part["qualitySignalCodes"] = result["qualitySignalCodes"]
                    part["lastError"] = None
                    completed_this_call += 1
                ledger["updatedAt"] = utc_now()
                _refresh_ledger_summary(ledger)
                _save_catalog_run(output, ledger)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    ledger["updatedAt"] = utc_now()
    _refresh_ledger_summary(ledger)
    _save_catalog_run(output, ledger)
    return {
        "runId": ledger["runId"],
        "state": ledger["state"],
        "selectedParts": len(selected),
        "startedParts": started_this_call,
        "completedParts": completed_this_call,
        "failedParts": failed_this_call,
        "deferredByRuntimeLimit": len(selected_queue),
        "summary": ledger["summary"],
        "publicationAllowed": False,
    }


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / str(relative)).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise CatalogRunError(f"artifact path escapes catalog run: {relative}")
    return candidate


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogRunError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise CatalogRunError(f"JSON artifact must contain an object: {path}")
    return value


def _iter_jsonl(path: Path) -> Sequence[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CatalogRunError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise CatalogRunError(f"JSONL row must be an object at {path}:{line_number}")
            rows.append(value)
    return rows


def compare_catalog_record_sets(
    *,
    record_paths: Mapping[str, Path],
    section_key: str,
    expected_source_sha256: str,
    max_records: int,
) -> dict[str, Any]:
    """Compare full record objects across bounded catalog artifacts.

    The comparison deliberately ignores JSON key ordering and transport-level
    whitespace, but requires every record object, record ID, and record order
    to be identical. It is read-only and never changes publication state.
    """

    _required_positive_int(max_records, label="max_records")
    normalized_section_key, _ = _catalog_run_section(section_key)
    source_sha256 = _required_sha256(
        expected_source_sha256,
        label="expected_source_sha256",
    )
    if len(record_paths) < 2:
        raise CatalogRunError("record comparison requires at least two inputs")

    normalized_paths: list[tuple[str, Path]] = []
    for raw_label, raw_path in record_paths.items():
        label = str(raw_label or "").strip()
        if not label:
            raise CatalogRunError("record comparison input labels must be non-empty")
        if not isinstance(raw_path, Path):
            raise CatalogRunError(f"record comparison input {label} must be a Path")
        normalized_paths.append((label, raw_path.resolve(strict=True)))

    record_sets: dict[str, dict[str, Any]] = {}
    canonical_records_by_label: dict[str, dict[str, str]] = {}
    record_orders: dict[str, list[str]] = {}
    for label, path in normalized_paths:
        rows = _iter_jsonl(path)
        if len(rows) > max_records:
            raise CatalogRunError(
                f"record comparison limit exceeded for {label}: "
                f"{len(rows)} > {max_records}"
            )
        canonical_by_id: dict[str, str] = {}
        record_order: list[str] = []
        for line_number, record in enumerate(rows, start=1):
            record_id = str(record.get("recordId") or "").strip()
            if not record_id:
                raise CatalogRunError(
                    f"record comparison {label}:{line_number} lacks recordId"
                )
            if record_id in canonical_by_id:
                raise CatalogRunError(
                    f"record comparison {label}:{line_number} duplicates {record_id}"
                )
            source = record.get("source")
            if (
                record.get("sectionKey") != normalized_section_key
                or not isinstance(source, Mapping)
                or source.get("sourceSnapshotSha256") != source_sha256
            ):
                raise CatalogRunError(
                    f"record comparison {label}:{line_number} violates section/source identity"
                )
            canonical_by_id[record_id] = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            record_order.append(record_id)

        sequence_payload = "\n".join(
            canonical_by_id[record_id] for record_id in record_order
        ).encode("utf-8")
        set_payload = "\n".join(
            f"{record_id}\t{canonical_by_id[record_id]}"
            for record_id in sorted(canonical_by_id)
        ).encode("utf-8")
        record_sets[label] = {
            "path": str(path),
            "rawSha256": file_sha256(path),
            "recordCount": len(rows),
            "canonicalSequenceSha256": hashlib.sha256(sequence_payload).hexdigest(),
            "canonicalRecordSetSha256": hashlib.sha256(set_payload).hexdigest(),
        }
        canonical_records_by_label[label] = canonical_by_id
        record_orders[label] = record_order

    baseline_label = normalized_paths[0][0]
    baseline_records = canonical_records_by_label[baseline_label]
    baseline_order = record_orders[baseline_label]
    comparisons: list[dict[str, Any]] = []
    for label, _ in normalized_paths[1:]:
        candidate_records = canonical_records_by_label[label]
        missing_from_candidate = sorted(set(baseline_records) - set(candidate_records))
        missing_from_baseline = sorted(set(candidate_records) - set(baseline_records))
        changed_record_ids = sorted(
            record_id
            for record_id in set(baseline_records) & set(candidate_records)
            if baseline_records[record_id] != candidate_records[record_id]
        )
        comparisons.append(
            {
                "baseline": baseline_label,
                "candidate": label,
                "recordSetMatches": not (
                    missing_from_candidate
                    or missing_from_baseline
                    or changed_record_ids
                ),
                "recordOrderMatches": baseline_order == record_orders[label],
                "rawBytesMatch": (
                    record_sets[baseline_label]["rawSha256"]
                    == record_sets[label]["rawSha256"]
                ),
                "missingFromCandidate": missing_from_candidate[:100],
                "missingFromBaseline": missing_from_baseline[:100],
                "changedRecordIds": changed_record_ids[:100],
            }
        )

    hard_gate_passed = all(
        comparison["recordSetMatches"] and comparison["recordOrderMatches"]
        for comparison in comparisons
    )
    return {
        "schemaVersion": "parsecore-caac-catalog-record-comparison.v1",
        "comparedAt": utc_now(),
        "sectionKey": normalized_section_key,
        "sourceSnapshotSha256": source_sha256,
        "baseline": baseline_label,
        "recordSets": record_sets,
        "comparisons": comparisons,
        "hardGatePassed": hard_gate_passed,
        "publicationAllowed": False,
    }


def _validate_source_missing_holder_record(
    *,
    record: Mapping[str, Any],
    review_index: Mapping[tuple[str, int, int, str], Mapping[str, Any]],
    source_snapshot_sha256: str,
) -> tuple[str | None, list[str]]:
    """Validate the V2-only internal holder omission on one record."""

    holder = record.get("holder")
    if not isinstance(holder, Mapping):
        return None, ["holder is not an object"]
    holder_status = holder.get("status")
    if holder_status == "identified":
        if not str(holder.get("nameOriginal") or "").strip():
            return None, ["identified holder has no nameOriginal"]
        if not str(holder.get("nameNormalized") or "").strip():
            return None, ["identified holder has no nameNormalized"]
        if "sourceOmission" in holder:
            return None, ["identified holder unexpectedly contains sourceOmission"]
        return None, []
    if holder_status != "source_missing":
        return None, ["holder status is neither identified nor source_missing"]
    errors: list[str] = []
    if holder.get("nameOriginal") is not None or holder.get("nameNormalized") is not None:
        errors.append("source_missing holder must not contain a holder name")
    omission = holder.get("sourceOmission")
    if not isinstance(omission, Mapping):
        return None, [*errors, "source_missing holder has no sourceOmission"]
    try:
        pdf_page = int(omission.get("pdfPage") or 0)
    except (TypeError, ValueError):
        pdf_page = 0
    key = (
        str(record.get("sectionKey") or ""),
        int(record.get("rowNumber") or 0),
        pdf_page,
        str(omission.get("fieldPath") or ""),
    )
    review = review_index.get(key)
    if review is None:
        return None, [*errors, "source_missing holder is not present in the omission review"]
    expected_omission = {
        "reviewId": review["reviewId"],
        "fieldPath": review["fieldPath"],
        "reasonCode": review["reasonCode"],
        "pdfPage": review["pdfPage"],
        "bbox": review["bbox"],
        "sourceSnapshotSha256": source_snapshot_sha256,
        "reviewedOn": review["reviewedOn"],
    }
    if dict(omission) != expected_omission:
        errors.append("source_missing holder sourceOmission does not exactly match review")
    review_id = str(review["reviewId"])
    expected_cell = {
        "columnKey": "holder.nameOriginal",
        "textOriginal": "",
        "pdfPage": review["pdfPage"],
        "bbox": review["bbox"],
        "sourceWordIds": [],
    }
    source = record.get("source")
    source_cells = source.get("cells") if isinstance(source, Mapping) else None
    if not isinstance(source_cells, Sequence) or isinstance(source_cells, (str, bytes)):
        errors.append("source_missing holder has no source cells")
    else:
        holder_cells = [
            dict(cell)
            for cell in source_cells
            if isinstance(cell, Mapping) and cell.get("columnKey") == "holder.nameOriginal"
        ]
        if holder_cells != [expected_cell]:
            errors.append("source_missing holder source cell does not exactly document the empty cell")
    evidence = record.get("fieldEvidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        errors.append("source_missing holder has no field evidence")
    else:
        holder_evidence = [
            item
            for item in evidence
            if isinstance(item, Mapping) and item.get("fieldPath") == "holder.nameOriginal"
        ]
        if len(holder_evidence) != 1:
            errors.append("source_missing holder must have exactly one holder field-evidence entry")
        else:
            item = holder_evidence[0]
            if not (
                item.get("textOriginal") == ""
                and item.get("pdfPage") == review["pdfPage"]
                and item.get("bbox") == review["bbox"]
                and item.get("sourceWordIds") == []
                and item.get("sourceCellId") == f"source-omission:{review_id}"
                and item.get("reviewStatus") == "reviewed"
            ):
                errors.append("source_missing holder field evidence does not exactly document the empty cell")
    quality = record.get("quality")
    if not isinstance(quality, Mapping) or quality.get("status") not in {
        "candidate",
        "internal_preview",
    }:
        errors.append("source_missing holder record is not internal-only")
    return review_id, errors


def audit_catalog_run(
    *,
    output: Path,
    schema_path: Path,
    max_records: int,
    max_runtime_seconds: float,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Verify immutable part checkpoints, record schema, ownership, and dedupe."""

    _required_positive_int(max_records, label="max_records")
    if max_runtime_seconds <= 0:
        raise CatalogRunError("max_runtime_seconds must be positive")
    started = time.monotonic()
    output = output.resolve(strict=True)
    ledger = load_catalog_run(output)
    schema_path = schema_path.resolve(strict=True)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover - audit dependency is explicit
        raise CatalogRunError("jsonschema is required for catalog run audit") from exc
    schema = _read_json_object(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    document = ledger.get("document")
    profile = ledger.get("profile")
    scope = ledger.get("scope")
    if not all(isinstance(item, Mapping) for item in (document, profile, scope)):
        raise CatalogRunError("catalog run ledger metadata is incomplete")
    source_sha256 = str(document.get("sha256") or "")
    section_key, section = _catalog_run_section(str(scope.get("sectionKey") or ""))
    record_schema = str(profile.get("recordSchema") or "")
    omission_review = _source_omission_review_from_ledger(ledger)
    omission_review_index = (
        source_omission_review_index(omission_review["payload"])
        if omission_review is not None
        else {}
    )
    record_ids: set[str] = set()
    row_numbers: set[int] = set()
    duplicate_row_numbers: list[int] = []
    duplicate_record_ids: list[str] = []
    schema_errors: list[dict[str, Any]] = []
    ownership_errors: list[dict[str, Any]] = []
    artifact_errors: list[str] = []
    source_omission_errors: list[dict[str, Any]] = []
    used_omission_review_ids: set[str] = set()
    signal_counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    audited_parts: list[int] = []
    core_value_count = 0
    core_evidence_count = 0
    core_evidence_missing: list[dict[str, Any]] = []
    date_value_count = 0

    if omission_review is not None:
        review_artifact = omission_review["artifact"]
        try:
            review_path = _safe_child(output, str(review_artifact["path"]))
        except (CatalogRunError, FileNotFoundError) as exc:
            artifact_errors.append(f"source omission review: {exc}")
        else:
            if (
                review_path.stat().st_size != int(review_artifact["bytes"])
                or file_sha256(review_path) != str(review_artifact["sha256"])
            ):
                artifact_errors.append("source omission review: immutable artifact mismatch")

    for part in ledger["parts"]:
        if part.get("state") != "done":
            continue
        if time.monotonic() - started > max_runtime_seconds:
            raise TimeoutError("catalog_run_audit_runtime_limit_exceeded")
        part_index = int(part.get("part_index") or 0)
        attempt_relative = str(part.get("latestAttemptPath") or "")
        attempt_root = _safe_child(output, attempt_relative)
        checkpoint_path = _safe_child(attempt_root, "checkpoint.json")
        actual_checkpoint_hash = file_sha256(checkpoint_path)
        if actual_checkpoint_hash != str(part.get("checkpointSha256") or ""):
            artifact_errors.append(f"part {part_index}: checkpoint SHA-256 mismatch")
            continue
        checkpoint = _read_json_object(checkpoint_path)
        if (
            checkpoint.get("state") != "done"
            or checkpoint.get("runId") != ledger.get("runId")
            or int(checkpoint.get("partIndex") or 0) != part_index
        ):
            artifact_errors.append(f"part {part_index}: checkpoint identity mismatch")
            continue
        input_range = checkpoint.get("inputPageRange") or {}
        owned_range = checkpoint.get("ownedPageRange") or {}
        input_start = int(input_range.get("start") or 0)
        input_end = int(input_range.get("end") or 0)
        owned_start = int(owned_range.get("start") or 0)
        owned_end = int(owned_range.get("end") or 0)
        expected_ranges = (
            int(part.get("input_page_start") or 0),
            int(part.get("input_page_end") or 0),
            int(part.get("owned_page_start") or 0),
            int(part.get("owned_page_end") or 0),
        )
        if (input_start, input_end, owned_start, owned_end) != expected_ranges:
            artifact_errors.append(f"part {part_index}: checkpoint page range mismatch")
            continue
        raw_artifacts = checkpoint.get("artifacts")
        if not isinstance(raw_artifacts, Mapping):
            artifact_errors.append(f"part {part_index}: artifact descriptors missing")
            continue
        part_artifacts: dict[str, Path] = {}
        for key, raw_descriptor in raw_artifacts.items():
            if not isinstance(raw_descriptor, Mapping):
                artifact_errors.append(f"part {part_index}: invalid {key} descriptor")
                continue
            try:
                artifact = _safe_child(attempt_root, str(raw_descriptor.get("path") or ""))
            except (CatalogRunError, FileNotFoundError) as exc:
                artifact_errors.append(f"part {part_index}: {exc}")
                continue
            declared_bytes = raw_descriptor.get("bytes")
            if (
                isinstance(declared_bytes, bool)
                or not isinstance(declared_bytes, int)
                or artifact.stat().st_size != declared_bytes
            ):
                artifact_errors.append(f"part {part_index}: {key} byte length mismatch")
                continue
            if file_sha256(artifact) != str(raw_descriptor.get("sha256") or ""):
                artifact_errors.append(f"part {part_index}: {key} SHA-256 mismatch")
                continue
            part_artifacts[str(key)] = artifact
        required_artifacts = {
            "records",
            "fieldEvidence",
            "candidateRows",
            "rejectedRows",
            "signals",
            "observations",
        }
        if set(part_artifacts) != required_artifacts:
            artifact_errors.append(f"part {part_index}: required artifacts incomplete")
            continue

        records = _iter_jsonl(part_artifacts["records"])
        candidate_rows = _iter_jsonl(part_artifacts["candidateRows"])
        rejected_rows = _iter_jsonl(part_artifacts["rejectedRows"])
        evidence_rows = _iter_jsonl(part_artifacts["fieldEvidence"])
        signals = _iter_jsonl(part_artifacts["signals"])
        if len(record_ids) + len(records) > max_records:
            raise CatalogRunError("catalog run audit record limit exceeded")
        declared_counts = checkpoint.get("counts") or {}
        observed_counts = {
            "records": len(records),
            "candidateRows": len(candidate_rows),
            "rejectedRows": len(rejected_rows),
            "fieldEvidence": len(evidence_rows),
            "qualitySignals": len(signals),
        }
        for key, observed in observed_counts.items():
            if observed != int(declared_counts.get(key) or 0):
                artifact_errors.append(
                    f"part {part_index}: {key} count mismatch "
                    f"({observed} != {declared_counts.get(key)})"
                )
        if len(candidate_rows) != len(records) + len(rejected_rows):
            artifact_errors.append(f"part {part_index}: candidate accounting mismatch")

        for line_number, record in enumerate(records, start=1):
            record_id = str(record.get("recordId") or "")
            if record_id in record_ids:
                duplicate_record_ids.append(record_id)
            record_ids.add(record_id)
            record_row_number = int(record.get("rowNumber") or 0)
            if record_row_number in row_numbers:
                duplicate_row_numbers.append(record_row_number)
            row_numbers.add(record_row_number)
            error = next(iter(validator.iter_errors(record)), None)
            if error is not None and len(schema_errors) < 100:
                schema_errors.append(
                    {
                        "partIndex": part_index,
                        "line": line_number,
                        "recordId": record_id,
                        "path": "/" + "/".join(str(item) for item in error.path),
                        "message": error.message,
                    }
                )
            if record.get("schemaVersion") != record_schema and len(schema_errors) < 100:
                schema_errors.append(
                    {
                        "partIndex": part_index,
                        "line": line_number,
                        "recordId": record_id,
                        "path": "/schemaVersion",
                        "message": "record schemaVersion does not match the immutable run profile",
                    }
                )
            if record_schema == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
                review_id, omission_errors = _validate_source_missing_holder_record(
                    record=record,
                    review_index=omission_review_index,
                    source_snapshot_sha256=source_sha256,
                )
                if omission_errors and len(source_omission_errors) < 100:
                    source_omission_errors.append(
                        {
                            "partIndex": part_index,
                            "line": line_number,
                            "recordId": record_id,
                            "errors": omission_errors,
                        }
                    )
                if review_id is not None:
                    if review_id in used_omission_review_ids and len(source_omission_errors) < 100:
                        source_omission_errors.append(
                            {
                                "partIndex": part_index,
                                "line": line_number,
                                "recordId": record_id,
                                "errors": ["source omission review is applied by more than one record"],
                            }
                        )
                    used_omission_review_ids.add(review_id)
            source_info = record.get("source") or {}
            page_start = int(source_info.get("pdfPageStart") or 0)
            page_end = int(source_info.get("pdfPageEnd") or 0)
            if (
                record.get("sectionKey") != section_key
                or source_info.get("sourceSnapshotSha256") != source_sha256
                or page_start < input_start
                or page_end < owned_start
                or page_end > owned_end
                or page_start > page_end
            ):
                ownership_errors.append(
                    {
                        "partIndex": part_index,
                        "recordId": record_id,
                        "sourcePageStart": page_start,
                        "sourcePageEnd": page_end,
                    }
                )
            evidence_by_field: dict[str, list[Mapping[str, Any]]] = {}
            for evidence in record.get("fieldEvidence") or ():
                if not isinstance(evidence, Mapping):
                    continue
                evidence_by_field.setdefault(
                    str(evidence.get("fieldPath") or ""),
                    [],
                ).append(evidence)
            core_values = {
                field_path: _record_field_value(record, field_path)
                for field_path in section.core_field_paths
            }
            for field_path, value in core_values.items():
                if value in (None, "", [], {}):
                    continue
                core_value_count += 1
                valid_evidence = any(
                    evidence.get("pdfPage")
                    and isinstance(evidence.get("bbox"), list)
                    and len(evidence.get("bbox") or ()) == 4
                    and evidence.get("sourceWordIds")
                    for evidence in evidence_by_field.get(field_path, ())
                )
                if valid_evidence:
                    core_evidence_count += 1
                elif len(core_evidence_missing) < 100:
                    core_evidence_missing.append(
                        {"recordId": record_id, "fieldPath": field_path}
                    )
        for row in candidate_rows:
            row_page = int(row.get("pdfPage") or 0)
            if not owned_start <= row_page <= owned_end:
                ownership_errors.append(
                    {
                        "partIndex": part_index,
                        "candidateRowPage": row_page,
                    }
                )
            cells = row.get("cells") if isinstance(row.get("cells"), Mapping) else {}
            for field_path in (
                "approval.latestApprovedOn",
                "approval.expiresOn",
            ):
                if cells.get(field_path) not in (None, "", [], {}):
                    date_value_count += 1
        signal_counts.update(str(signal.get("code") or "unknown") for signal in signals)
        totals.update(observed_counts)
        totals["ownedPages"] += owned_end - owned_start + 1
        totals["inputPages"] += input_end - input_start + 1
        audited_parts.append(part_index)

    if omission_review is not None and all(
        part.get("state") == "done" for part in ledger["parts"]
    ):
        expected_review_ids = {
            str(entry["reviewId"])
            for entry in omission_review["payload"]["entries"]
        }
        unused_review_ids = sorted(expected_review_ids - used_omission_review_ids)
        if unused_review_ids and len(source_omission_errors) < 100:
            source_omission_errors.append(
                {
                    "errors": ["source omission review is not applied by a record"],
                    "reviewIds": unused_review_ids[:100],
                }
            )

    critical_signal_codes = {
        "approval_identifier_pattern_unmatched",
        "column_shift_suspected",
        "duplicate_business_key",
        "header_not_recognized",
        "identifier_pattern_mismatch",
        "record_boundary_uncertain",
        "required_field_missing",
        "row_anchor_out_of_column",
        "section_boundary_uncertain",
    }
    critical_signals = {
        code: count
        for code, count in sorted(signal_counts.items())
        if code in critical_signal_codes and count
    }
    review_required: list[dict[str, Any]] = []
    sorted_row_numbers = sorted(row_numbers)
    missing_row_numbers: list[int] = []
    if sorted_row_numbers:
        expected_row = sorted_row_numbers[0]
        for row_number in sorted_row_numbers:
            while expected_row < row_number and len(missing_row_numbers) < 100:
                missing_row_numbers.append(expected_row)
                expected_row += 1
            expected_row = row_number + 1
    expected_context_signals = max(0, len(audited_parts) - 1)
    actual_context_signals = int(signal_counts.get("cross_part_context_used", 0))
    date_failures = int(signal_counts.get("date_parse_failed", 0))
    date_parse_rate = (
        (date_value_count - date_failures) / date_value_count
        if date_value_count
        else 1.0
    )
    for code, count in (
        ("artifact_errors", len(artifact_errors)),
        ("schema_errors", len(schema_errors)),
        ("ownership_errors", len(ownership_errors)),
        ("duplicate_record_ids", len(duplicate_record_ids)),
        ("duplicate_row_numbers", len(duplicate_row_numbers)),
        ("missing_row_numbers", len(missing_row_numbers)),
        ("core_evidence_missing", len(core_evidence_missing)),
        ("source_omission_errors", len(source_omission_errors)),
        ("rejected_rows", totals["rejectedRows"]),
    ):
        if count:
            review_required.append({"code": code, "count": count})
    if sorted_row_numbers and sorted_row_numbers[0] != 1:
        review_required.append(
            {
                "code": "row_sequence_does_not_start_at_one",
                "firstRowNumber": sorted_row_numbers[0],
            }
        )
    if actual_context_signals != expected_context_signals:
        review_required.append(
            {
                "code": "cross_part_context_count_mismatch",
                "expected": expected_context_signals,
                "actual": actual_context_signals,
            }
        )
    if date_parse_rate < 0.995:
        review_required.append(
            {
                "code": "date_parse_rate_below_gate",
                "rate": date_parse_rate,
                "minimum": 0.995,
            }
        )
    review_required.extend(
        {"code": code, "count": count} for code, count in critical_signals.items()
    )
    done_parts = sum(1 for part in ledger["parts"] if part.get("state") == "done")
    report = {
        "schemaVersion": CATALOG_RUN_LEDGER_VERSION,
        "runId": ledger["runId"],
        "auditedAt": utc_now(),
        "durationSeconds": round(time.monotonic() - started, 3),
        "scope": "completed-parts",
        "auditedParts": audited_parts,
        "auditedPartCount": len(audited_parts),
        "runPartCount": len(ledger["parts"]),
        "fullRunComplete": done_parts == len(ledger["parts"]),
        "totals": dict(sorted(totals.items())),
        "qualitySignalCodes": dict(sorted(signal_counts.items())),
        "artifactErrors": artifact_errors[:100],
        "sourceOmissionReview": {
            "required": record_schema == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
            "reviewedCells": (
                len(omission_review["payload"]["entries"])
                if omission_review is not None
                else 0
            ),
            "usedReviewIds": sorted(used_omission_review_ids),
            "errors": source_omission_errors,
            "passed": not source_omission_errors,
        },
        "schemaErrors": schema_errors,
        "ownershipErrors": ownership_errors[:100],
        "duplicateRecordIds": duplicate_record_ids[:100],
        "rowSequence": {
            "first": sorted_row_numbers[0] if sorted_row_numbers else None,
            "last": sorted_row_numbers[-1] if sorted_row_numbers else None,
            "unique": len(row_numbers),
            "duplicates": duplicate_row_numbers[:100],
            "missing": missing_row_numbers,
            "complete": bool(sorted_row_numbers)
            and sorted_row_numbers[0] == 1
            and not duplicate_row_numbers
            and not missing_row_numbers,
        },
        "crossPartContext": {
            "expected": expected_context_signals,
            "actual": actual_context_signals,
            "passed": actual_context_signals == expected_context_signals,
        },
        "dateNormalization": {
            "values": date_value_count,
            "failed": date_failures,
            "parseRate": date_parse_rate,
            "passed": date_parse_rate >= 0.995,
        },
        "coreFieldEvidence": {
            "valued": core_value_count,
            "evidenced": core_evidence_count,
            "missing": core_evidence_missing,
            "coverage": (
                core_evidence_count / core_value_count if core_value_count else 1.0
            ),
            "passed": not core_evidence_missing,
        },
        "reviewRequired": review_required,
        "hardGatePassed": not review_required,
        "publicationAllowed": False,
    }
    if report_path is not None:
        _atomic_write_json(report_path.resolve(), report)
    return report


def finalize_catalog_run(
    *,
    output: Path,
    audit_report_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    """Create a deterministic, sharded import manifest after the full hard gate."""

    output = output.resolve(strict=True)
    ledger = load_catalog_run(output)
    audit_report_path = audit_report_path.resolve(strict=True)
    if not audit_report_path.is_relative_to(output):
        raise CatalogRunError("audit report must be inside the catalog run output")
    audit = _read_json_object(audit_report_path)
    if (
        audit.get("runId") != ledger.get("runId")
        or audit.get("hardGatePassed") is not True
        or audit.get("fullRunComplete") is not True
        or audit.get("reviewRequired") != []
    ):
        raise CatalogRunError("full catalog audit has not passed without review")
    if ledger.get("state") != "completed":
        raise CatalogRunError("catalog run is not complete")
    document = ledger.get("document")
    parser = ledger.get("parser")
    profile = ledger.get("profile")
    scope = ledger.get("scope")
    if not all(isinstance(item, Mapping) for item in (document, parser, profile, scope)):
        raise CatalogRunError("catalog run ledger metadata is incomplete")
    record_schema = str(profile.get("recordSchema") or "")
    omission_review = _source_omission_review_from_ledger(ledger)
    section_key, section = _catalog_run_section(str(scope.get("sectionKey") or ""))
    totals = audit.get("totals")
    if not isinstance(totals, Mapping):
        raise CatalogRunError("catalog audit totals are missing")
    record_count = int(totals.get("records") or 0)
    if record_count < 1 or record_count != int(totals.get("candidateRows") or 0):
        raise CatalogRunError("catalog audit record accounting is invalid")
    if int(totals.get("rejectedRows") or 0) != 0:
        raise CatalogRunError("catalog audit still contains rejected rows")

    manifest_run_id = str(ledger["runId"])
    if not manifest_run_id.startswith("run_"):
        manifest_run_id = f"run_{manifest_run_id}"
    evaluation = {
        "schemaVersion": record_schema,
        "runId": manifest_run_id,
        "sourceSha256": document["sha256"],
        "sampledPages": int(scope["ownedPageCount"]),
        "candidateRows": int(totals["candidateRows"]),
        "candidateRecords": record_count,
        "rejectedRows": int(totals["rejectedRows"]),
        "qualitySignalCount": int(totals["qualitySignals"]),
        "qualitySignalCodes": dict(audit.get("qualitySignalCodes") or {}),
        "gates": {
            "full_part_coverage": {
                "passed": int(audit["auditedPartCount"])
                == int(audit["runPartCount"]),
                "auditedParts": int(audit["auditedPartCount"]),
                "plannedParts": int(audit["runPartCount"]),
            },
            "record_schema": {
                "passed": not audit.get("schemaErrors"),
                "errors": len(audit.get("schemaErrors") or ()),
            },
            "record_identity": {
                "passed": not audit.get("duplicateRecordIds"),
                "duplicates": len(audit.get("duplicateRecordIds") or ()),
            },
            "row_sequence": dict(audit.get("rowSequence") or {}),
            "physical_page_ownership": {
                "passed": not audit.get("ownershipErrors"),
                "errors": len(audit.get("ownershipErrors") or ()),
            },
            "cross_part_context": dict(audit.get("crossPartContext") or {}),
            "date_normalization": dict(audit.get("dateNormalization") or {}),
            "core_field_evidence": dict(audit.get("coreFieldEvidence") or {}),
            "source_omission_review": dict(audit.get("sourceOmissionReview") or {}),
            "artifact_integrity": {
                "passed": not audit.get("artifactErrors"),
                "errors": len(audit.get("artifactErrors") or ()),
            },
        },
        "hardGatePassed": True,
        "publicationAllowed": False,
        "reviewRequired": [],
        "observations": {
            "recordsRemainCandidate": True,
            "scope": f"{section_key}_internal_preview",
            "fullRunAudit": audit_report_path.relative_to(output).as_posix(),
        },
    }
    evaluation_path = output / "quality" / "evaluation.json"
    _atomic_write_json(evaluation_path, evaluation)

    artifacts: list[dict[str, Any]] = []
    summed_records = 0
    for part in sorted(ledger["parts"], key=lambda item: int(item["part_index"])):
        if part.get("state") != "done":
            raise CatalogRunError("cannot finalize a run with incomplete parts")
        attempt_root = _safe_child(output, str(part.get("latestAttemptPath") or ""))
        checkpoint = _read_json_object(_safe_child(attempt_root, "checkpoint.json"))
        record_descriptor = (checkpoint.get("artifacts") or {}).get("records")
        if not isinstance(record_descriptor, Mapping):
            raise CatalogRunError(
                f"part {part.get('part_index')} record descriptor is missing"
            )
        record_path = _safe_child(
            attempt_root,
            str(record_descriptor.get("path") or ""),
        )
        part_records = int(record_descriptor.get("records") or 0)
        summed_records += part_records
        artifacts.append(
            {
                "dataset": f"{section_key}.part-{int(part['part_index']):03d}",
                "format": "jsonl",
                "path": record_path.relative_to(output).as_posix(),
                "contentType": "application/x-ndjson",
                "bytes": int(record_descriptor["bytes"]),
                "records": part_records,
                "sha256": str(record_descriptor["sha256"]),
            }
        )
    if summed_records != record_count:
        raise CatalogRunError(
            f"record shard count mismatch: {summed_records} != {record_count}"
        )
    for dataset, path in (
        ("golden-evaluation", evaluation_path),
        ("full-run-audit", audit_report_path),
        ("run-ledger", output / "run-ledger.json"),
    ):
        artifacts.append(
            {
                "dataset": dataset,
                "format": "json",
                "path": path.relative_to(output).as_posix(),
                "contentType": "application/json",
                "bytes": path.stat().st_size,
                "records": 1,
                "sha256": file_sha256(path),
            }
        )
    if omission_review is not None:
        review_artifact = omission_review["artifact"]
        review_path = _safe_child(output, str(review_artifact["path"]))
        if (
            review_path.stat().st_size != int(review_artifact["bytes"])
            or file_sha256(review_path) != str(review_artifact["sha256"])
        ):
            raise CatalogRunError("immutable V2 source omission review artifact mismatch")
        artifacts.append(
            {
                "dataset": "source-omission-review",
                "format": "json",
                "path": str(review_artifact["path"]),
                "contentType": "application/json",
                "bytes": int(review_artifact["bytes"]),
                "records": int(review_artifact["records"]),
                "sha256": str(review_artifact["sha256"]),
            }
        )
    signal_counts = dict(audit.get("qualitySignalCodes") or {})
    manifest = {
        "schemaVersion": record_schema,
        "runId": manifest_run_id,
        "document": {
            "fileName": document["fileName"],
            "sha256": document["sha256"],
            "byteLength": int(document["byteLength"]),
            "pageCount": int(document["pageCount"]),
            "dataCutoffOn": document["dataCutoffOn"],
        },
        "parser": dict(parser),
        "sections": [
            {
                "sectionKey": section_key,
                "title": section.title,
                "pdfPageStart": int(scope["ownedPageStart"]),
                "pdfPageEnd": int(scope["ownedPageEnd"]),
                "recordCount": record_count,
                "approvalType": section.approval_type,
            }
        ],
        "qualitySummary": {
            "status": "passed",
            "recordCount": record_count,
            "candidateCount": record_count,
            "warningCount": int(totals["qualitySignals"]),
            "errorCount": 0,
            "signals": [
                {
                    "code": str(code),
                    "severity": "warning",
                    "message": (
                        f"{count} occurrences in full {section_key} candidate run"
                    ),
                    "details": {"count": int(count)},
                }
                for code, count in sorted(signal_counts.items())
            ],
        },
        "artifacts": artifacts,
    }
    schema_path = schema_path.resolve(strict=True)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:  # pragma: no cover
        raise CatalogRunError("jsonschema is required for catalog finalization") from exc
    schema = _read_json_object(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    error = next(iter(validator.iter_errors(manifest)), None)
    if error is not None:
        location = "/" + "/".join(str(item) for item in error.path)
        raise CatalogRunError(
            f"final manifest violates contract at {location}: {error.message}"
        )
    manifest_path = output / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return {
        "runId": manifest_run_id,
        "manifestPath": str(manifest_path),
        "manifestSha256": file_sha256(manifest_path),
        "recordCount": record_count,
        "recordShardCount": len(ledger["parts"]),
        "hardGatePassed": True,
        "publicationAllowed": False,
    }


__all__ = [
    "CATALOG_RUN_LEDGER_VERSION",
    "CatalogRunError",
    "audit_catalog_run",
    "build_catalog_run_ledger",
    "compare_catalog_record_sets",
    "execute_catalog_part_task",
    "execute_catalog_run",
    "finalize_catalog_run",
    "file_sha256",
    "initialize_catalog_run",
    "load_catalog_run",
    "recover_interrupted_catalog_parts",
    "select_catalog_parts",
]
