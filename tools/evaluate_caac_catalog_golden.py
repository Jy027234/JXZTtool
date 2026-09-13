"""Evaluate a bounded CAAC approved-catalog golden run.

The evaluator separates machine-checkable contract/source gates from review
gates that require an independently curated exact-match truth set.  It never
marks a candidate run publishable; a hard gate can only be passed when every
required check is independently evidenced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from parsecore.catalog_contracts import (
    CAAC_APPROVAL_TYPES,
    CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
    validate_catalog_record_shape,
)
from parsecore.catalog_omissions import (
    SOURCE_OMISSION_FIELD_PATH,
    SourceOmissionReviewError,
    normalize_source_omission_review,
    source_omission_review_index,
)


CORE_FIELDS_BY_SECTION: dict[str, tuple[str, ...]] = {
    "tc": ("approval.numberOriginal", "holder.nameOriginal", "item.modelOriginal"),
    "foreign_product_recognition": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.nameOriginal",
        "item.modelOriginal",
    ),
    "tda": ("approval.numberOriginal", "holder.nameOriginal", "item.modelOriginal"),
    "pc": ("approval.numberOriginal", "holder.nameOriginal", "item.modelOriginal"),
    "stc": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "attributes.originalModel",
        "attributes.originalApprovalNumber",
        "item.modelOriginal",
        "approval.latestApprovedOn",
    ),
    "mda": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "attributes.modelCertificateNumber",
        "item.productTypeOriginal",
        "item.descriptionOriginal",
        "approval.latestApprovedOn",
    ),
    "pma_holder": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.descriptionOriginal",
        "attributes.qualityManualNumber",
        "attributes.qualityManualRevision",
        "approval.latestApprovedOn",
    ),
    "pma_item": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.nameOriginal",
        "item.partNumberOriginal",
    ),
    "ctsoa": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.nameOriginal",
        "item.modelOriginal",
        "item.ctsoCodesOriginal",
    ),
    "foreign_tso_recognition": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.nameOriginal",
        "item.modelOriginal",
    ),
    "vtc": ("approval.numberOriginal", "holder.nameOriginal", "item.modelOriginal"),
    "vstc": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.productTypeOriginal",
        "item.modelOriginal",
        "item.descriptionOriginal",
        "attributes.authority",
        "attributes.foreignApprovalNumber",
        "approval.latestApprovedOn",
    ),
    "vda": (
        "approval.numberOriginal",
        "holder.nameOriginal",
        "item.nameOriginal",
        "item.modelOriginal",
    ),
}

DATE_FIELDS = (
    "approval.latestApprovedOn",
    "approval.expiresOn",
)
REVIEW_TRUTH_SCHEMA_VERSION = "caac-approved-catalog.v1.review-truth"
REVIEW_REQUIRED_GATES = (
    "row_anchor_recall",
    "non_row_numeric_false_positives",
    "approval_exact_match",
    "holder_exact_match",
    "pma_part_exact_match",
    "cross_page_continuation",
    "deterministic_rerun",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def _check(passed: bool, *, status: str | None = None, **details: Any) -> dict[str, Any]:
    result = {"passed": bool(passed), **details}
    if status is not None:
        result["status"] = status
    return result


def _field_value(record: Mapping[str, Any], field_path: str) -> Any:
    value: Any = record
    for part in field_path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _evidence_by_field(record: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for evidence in record.get("fieldEvidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        field_path = str(evidence.get("fieldPath") or "")
        if field_path:
            result.setdefault(field_path, []).append(evidence)
    return result


def _is_reviewed_empty_holder_evidence(
    record: Mapping[str, Any], evidence: Mapping[str, Any]
) -> bool:
    """Recognize the one V2 evidence shape that deliberately has no words."""

    holder = record.get("holder")
    if not isinstance(holder, Mapping) or holder.get("status") != "source_missing":
        return False
    omission = holder.get("sourceOmission")
    if not isinstance(omission, Mapping):
        return False
    return (
        evidence.get("fieldPath") == SOURCE_OMISSION_FIELD_PATH
        and evidence.get("textOriginal") == ""
        and evidence.get("pdfPage") == omission.get("pdfPage")
        and list(evidence.get("bbox") or ()) == list(omission.get("bbox") or ())
        and list(evidence.get("sourceWordIds") or ()) == []
        and evidence.get("reviewStatus") == "reviewed"
        and evidence.get("sourceCellId")
        == f"source-omission:{omission.get('reviewId')}"
    )


def _evaluate_source_omission_review(
    *,
    run: Path,
    manifest: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fail closed when a V2 empty-holder exception loses its source binding."""

    if manifest.get("schemaVersion") != CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        return {
            "required": False,
            "reviewedCells": 0,
            "sourceMissingRecords": 0,
            "usedReviewIds": [],
            "errors": [],
            "passed": True,
        }

    errors: list[dict[str, Any]] = []
    review_path = run / "quality" / "source-omission-review.json"
    if not review_path.exists():
        return {
            "required": True,
            "reviewedCells": 0,
            "sourceMissingRecords": 0,
            "usedReviewIds": [],
            "errors": [{"reason": "source_omission_review_artifact_missing"}],
            "passed": False,
        }
    try:
        raw_review = _load_json(review_path)
        if not isinstance(raw_review, Mapping):
            raise SourceOmissionReviewError("source omission review must be an object")
        review = normalize_source_omission_review(
            raw_review,
            source_snapshot_sha256=str(manifest.get("document", {}).get("sha256") or ""),
        )
        if review != raw_review:
            raise SourceOmissionReviewError("source omission review is not canonical")
        review_index = source_omission_review_index(review)
    except (OSError, ValueError, SourceOmissionReviewError) as exc:
        return {
            "required": True,
            "reviewedCells": 0,
            "sourceMissingRecords": 0,
            "usedReviewIds": [],
            "errors": [{"reason": "source_omission_review_invalid", "message": str(exc)}],
            "passed": False,
        }

    artifact_entries = [
        artifact
        for artifact in manifest.get("artifacts") or ()
        if isinstance(artifact, Mapping) and artifact.get("dataset") == "source-omission-review"
    ]
    if len(artifact_entries) != 1:
        errors.append(
            {
                "reason": "source_omission_review_artifact_descriptor_count",
                "observed": len(artifact_entries),
            }
        )
    else:
        artifact = artifact_entries[0]
        observed = {
            "path": artifact.get("path"),
            "bytes": artifact.get("bytes"),
            "records": artifact.get("records"),
            "sha256": artifact.get("sha256"),
        }
        expected = {
            "path": "quality/source-omission-review.json",
            "bytes": review_path.stat().st_size,
            "records": len(review["entries"]),
            "sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        }
        if observed != expected:
            errors.append(
                {
                    "reason": "source_omission_review_artifact_descriptor_mismatch",
                    "expected": expected,
                    "observed": observed,
                }
            )

    expected_source_hash = str(manifest.get("document", {}).get("sha256") or "")
    used_review_ids: set[str] = set()
    source_missing_records = 0
    for record in records:
        holder = record.get("holder")
        if not isinstance(holder, Mapping) or holder.get("status") != "source_missing":
            continue
        source_missing_records += 1
        record_id = str(record.get("recordId") or "")
        omission = holder.get("sourceOmission")
        if not isinstance(omission, Mapping):
            errors.append({"recordId": record_id, "reason": "source_omission_missing"})
            continue
        try:
            key = (
                str(record.get("sectionKey") or ""),
                int(record.get("rowNumber") or 0),
                int(omission.get("pdfPage") or 0),
                str(omission.get("fieldPath") or ""),
            )
        except (TypeError, ValueError):
            errors.append({"recordId": record_id, "reason": "source_omission_locator_invalid"})
            continue
        review_entry = review_index.get(key)
        if review_entry is None:
            errors.append({"recordId": record_id, "reason": "source_omission_unreviewed", "key": key})
            continue
        expected_omission = {
            "reviewId": review_entry["reviewId"],
            "fieldPath": review_entry["fieldPath"],
            "reasonCode": review_entry["reasonCode"],
            "pdfPage": review_entry["pdfPage"],
            "bbox": list(review_entry["bbox"]),
            "sourceSnapshotSha256": expected_source_hash,
            "reviewedOn": review_entry["reviewedOn"],
        }
        if dict(omission) != expected_omission:
            errors.append(
                {
                    "recordId": record_id,
                    "reason": "source_omission_payload_mismatch",
                }
            )
        review_id = str(review_entry["reviewId"])
        if review_id in used_review_ids:
            errors.append(
                {
                    "recordId": record_id,
                    "reason": "source_omission_review_reused",
                    "reviewId": review_id,
                }
            )
        used_review_ids.add(review_id)
        evidence = [
            item
            for item in record.get("fieldEvidence") or ()
            if isinstance(item, Mapping) and _is_reviewed_empty_holder_evidence(record, item)
        ]
        if len(evidence) != 1:
            errors.append(
                {
                    "recordId": record_id,
                    "reason": "source_omission_evidence_invalid",
                    "observed": len(evidence),
                }
            )
        source = record.get("source")
        source_cells = source.get("cells") if isinstance(source, Mapping) else ()
        exact_source_cell = [
            cell
            for cell in source_cells or ()
            if isinstance(cell, Mapping)
            and cell.get("columnKey") == SOURCE_OMISSION_FIELD_PATH
            and cell.get("textOriginal") == ""
            and cell.get("pdfPage") == review_entry["pdfPage"]
            and list(cell.get("bbox") or ()) == list(review_entry["bbox"])
            and list(cell.get("sourceWordIds") or ()) == []
        ]
        if len(exact_source_cell) != 1:
            errors.append(
                {
                    "recordId": record_id,
                    "reason": "source_omission_source_cell_invalid",
                    "observed": len(exact_source_cell),
                }
            )
        quality = record.get("quality")
        reviewed_signals = [
            signal
            for signal in (quality.get("signals") if isinstance(quality, Mapping) else ()) or ()
            if isinstance(signal, Mapping)
            and signal.get("code") == "source_omission_reviewed"
            and signal.get("pdfPage") == review_entry["pdfPage"]
        ]
        if len(reviewed_signals) != 1:
            errors.append(
                {
                    "recordId": record_id,
                    "reason": "source_omission_signal_invalid",
                    "observed": len(reviewed_signals),
                }
            )

    expected_review_ids = {str(entry["reviewId"]) for entry in review["entries"]}
    unused_review_ids = sorted(expected_review_ids - used_review_ids)
    if unused_review_ids:
        errors.append(
            {"reason": "source_omission_review_unused", "reviewIds": unused_review_ids}
        )
    return {
        "required": True,
        "reviewedCells": len(review["entries"]),
        "sourceMissingRecords": source_missing_records,
        "usedReviewIds": sorted(used_review_ids),
        "errors": errors[:50],
        "passed": not errors,
    }


def _positive_locator_coverage(
    matrix: Mapping[str, Any], candidate_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    index: set[tuple[str, int, int | None]] = set()
    for row in candidate_rows:
        section_key = str(row.get("sectionKey") or "")
        page = int(row.get("pdfPage") or 0)
        row_number = row.get("rowNumber")
        index.add((section_key, page, int(row_number) if row_number is not None else None))

    expected = 0
    matched = 0
    missing: list[dict[str, Any]] = []
    for section in matrix.get("sections") or ():
        section_key = str(section.get("sectionKey") or "")
        for locator in section.get("positiveExamples") or ():
            expected += 1
            page = int(locator.get("pdfPage") or 0)
            row_number = locator.get("rowNumber")
            key = (section_key, page, int(row_number) if row_number is not None else None)
            if key in index or (section_key, page, None) in index:
                matched += 1
            else:
                missing.append({"sectionKey": section_key, **dict(locator)})
    return {
        "expected": expected,
        "matched": matched,
        "recall": matched / expected if expected else 0.0,
        "missing": missing,
    }


def _build_review_queue(
    matrix: Mapping[str, Any],
    candidate_rows: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    row_index = {
        (
            str(row.get("sectionKey") or ""),
            int(row.get("pdfPage") or 0),
            int(row.get("rowNumber") or 0),
        ): row
        for row in candidate_rows
    }
    record_index = {
        (
            str(record.get("sectionKey") or ""),
            int(record.get("source", {}).get("pdfPageStart") or 0),
            int(record.get("rowNumber") or 0),
        ): record
        for record in records
    }
    queue: list[dict[str, Any]] = []
    for section in matrix.get("sections") or ():
        section_key = str(section.get("sectionKey") or "")
        examples = [
            ("positive", locator, ())
            for locator in section.get("positiveExamples") or ()
        ] + [
            (
                "continuation_or_anomaly",
                locator,
                tuple(str(signal) for signal in locator.get("expectedSignals") or ()),
            )
            for locator in section.get("continuationOrAnomalyExamples") or ()
        ]
        for review_type, locator, expected_signals in examples:
            page = int(locator.get("pdfPage") or 0)
            row_number = int(locator.get("rowNumber") or 0)
            key = (section_key, page, row_number)
            row = row_index.get(key)
            record = record_index.get(key)
            candidate = None
            if record is not None:
                candidate = {
                    "recordId": record.get("recordId"),
                    "approval": record.get("approval"),
                    "holder": record.get("holder"),
                    "item": record.get("item"),
                    "attributes": record.get("attributes", {}),
                }
            queue.append(
                {
                    "reviewId": f"{section_key}:{page}:{row_number if row_number else 'page'}:{review_type}",
                    "reviewType": review_type,
                    "sectionKey": section_key,
                    "pdfPage": page,
                    "rowNumber": row_number if row_number else None,
                    "expectedSignals": list(expected_signals),
                    "candidateRowPresent": row is not None,
                    "candidateRecord": candidate,
                    "reviewStatus": "pending",
                    "reviewFields": {
                        "approvalExact": None,
                        "holderExact": None,
                        "pmaPartExact": None,
                        "continuationDisposition": None,
                        "notes": "",
                    },
                }
            )
    for locator in matrix.get("negativeNumericExamples") or ():
        section_key = str(locator.get("sectionKey") or "document")
        page = int(locator.get("pdfPage") or 0)
        row_number = int(locator.get("rowNumber") or 0)
        key = (section_key, page, row_number)
        expected_signals = [
            str(signal) for signal in locator.get("expectedSignals") or ("non_row_numeric",)
        ]
        token_text = str(locator.get("tokenText") or "")
        reason = str(locator.get("reason") or "")
        if token_text:
            expected_signals.append(f"token={token_text}")
        if reason:
            expected_signals.append(f"reason={reason}")
        queue.append(
            {
                "reviewId": f"{section_key}:{page}:{row_number if row_number else 'page'}:negative_numeric",
                "reviewType": "negative_numeric",
                "sectionKey": section_key,
                "pdfPage": page,
                "rowNumber": row_number if row_number else None,
                "expectedSignals": expected_signals,
                "numericToken": token_text,
                "negativeReason": reason,
                "candidateRowPresent": key in row_index,
                "candidateRecord": None,
                "reviewStatus": "pending",
                "reviewFields": {
                    "approvalExact": None,
                    "holderExact": None,
                    "pmaPartExact": None,
                    "continuationDisposition": None,
                    "notes": "",
                },
            }
        )
    return queue


def _build_review_truth_template(
    *,
    run_id: str,
    source_snapshot_sha256: str,
    review_queue: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a blank, source-reviewable truth template.

    Candidate values are deliberately excluded from ``expected``.  A reviewer
    must transcribe the source PDF independently before the exact-match gates
    can be evaluated.
    """

    entries: list[dict[str, Any]] = []
    for item in review_queue:
        section_key = str(item.get("sectionKey") or "")
        pma_fields = section_key == "pma_item"
        entries.append(
            {
                "reviewId": str(
                    item.get("reviewId")
                    or f"{section_key}:{int(item.get('pdfPage') or 0)}:{item.get('rowNumber') or 'page'}:{item.get('reviewType') or ''}"
                ),
                "sectionKey": section_key,
                "pdfPage": int(item.get("pdfPage") or 0),
                "rowNumber": item.get("rowNumber"),
                "reviewType": str(item.get("reviewType") or ""),
                "expectedSignals": [str(signal) for signal in item.get("expectedSignals") or ()],
                "expected": {
                    "rowAnchor": None,
                    "approvalNumberOriginal": None,
                    "holderNameOriginal": None,
                    "holderStatus": None,
                    "pmaPartNumberOriginal": None if pma_fields else "not_applicable",
                    "pmaReplacedPartNumberOriginal": None if pma_fields else "not_applicable",
                    "continuationDisposition": None,
                },
                "review": {
                    "status": "pending",
                    "reviewer": None,
                    "reviewedAt": None,
                    "notes": "",
                },
            }
        )
    return {
        "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
        "runId": run_id,
        "sourceSnapshotSha256": source_snapshot_sha256,
        "status": "template",
        "entries": entries,
    }


def _review_entry_key(entry: Mapping[str, Any]) -> tuple[str, int, int | None, str]:
    """Return the stable identity of one review assertion.

    A positive locator and a continuation/anomaly locator may intentionally
    point at the same physical row.  ``reviewType`` therefore belongs in the
    truth identity; using only section/page/row would silently overwrite one
    assertion when the template is loaded.
    """

    row_number = entry.get("rowNumber")
    return (
        str(entry.get("sectionKey") or ""),
        int(entry.get("pdfPage") or 0),
        int(row_number) if row_number is not None else None,
        str(entry.get("reviewType") or ""),
    )


def _candidate_locator_key(entry: Mapping[str, Any]) -> tuple[str, int, int]:
    row_number = entry.get("rowNumber")
    return (
        str(entry.get("sectionKey") or ""),
        int(entry.get("pdfPage") or 0),
        int(row_number or 0),
    )


def _evaluate_review_truth(
    *,
    truth: Mapping[str, Any],
    candidate_rows: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    expected_source_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Compare a completed truth file with candidate rows/records."""

    expected_source = str(expected_source_snapshot_sha256 or "").strip().lower()
    observed_source = str(truth.get("sourceSnapshotSha256") or "").strip().lower()
    source_snapshot_matches = (
        not expected_source_snapshot_sha256
        or (bool(observed_source) and observed_source == expected_source)
    )
    truth_entries = [item for item in truth.get("entries") or () if isinstance(item, Mapping)]
    truth_index: dict[tuple[str, int, int | None, str], Mapping[str, Any]] = {}
    duplicate_truth: list[tuple[str, int, int | None, str]] = []
    for entry in truth_entries:
        key = _review_entry_key(entry)
        if key in truth_index:
            duplicate_truth.append(key)
        truth_index[key] = entry
    row_index = {
        (
            str(row.get("sectionKey") or ""),
            int(row.get("pdfPage") or 0),
            int(row.get("rowNumber") or 0),
        ): row
        for row in candidate_rows
    }
    record_index = {
        (
            str(record.get("sectionKey") or ""),
            int(record.get("source", {}).get("pdfPageStart") or 0),
            int(record.get("rowNumber") or 0),
        ): record
        for record in records
    }
    missing_truth: list[tuple[str, int, int | None, str]] = []
    row_anchor_missing: list[tuple[str, int, int | None, str]] = []
    exact_missing: list[tuple[str, int, int | None, str]] = []
    unconfirmed: list[tuple[str, int, int | None, str]] = []
    mismatches: list[dict[str, Any]] = []
    row_anchor_mismatches: list[dict[str, Any]] = []
    exact_mismatches: list[dict[str, Any]] = []
    row_anchor_entries = [
        entry for entry in truth_entries if str(entry.get("reviewType")) != "negative_numeric"
    ]
    for entry in row_anchor_entries:
        key = _review_entry_key(entry)
        candidate_key = _candidate_locator_key(entry)
        expected = entry.get("expected") if isinstance(entry.get("expected"), Mapping) else {}
        review = entry.get("review") if isinstance(entry.get("review"), Mapping) else {}
        if "rowAnchor" not in expected or not isinstance(expected.get("rowAnchor"), bool):
            missing_truth.append(key)
            row_anchor_missing.append(key)
        if str(review.get("status") or "pending") != "confirmed":
            unconfirmed.append(key)
        candidate_present = candidate_key in row_index
        if isinstance(expected.get("rowAnchor"), bool) and candidate_present != expected["rowAnchor"]:
            mismatch = {
                "key": key,
                "field": "rowAnchor",
                "expected": expected["rowAnchor"],
                "observed": candidate_present,
            }
            mismatches.append(mismatch)
            row_anchor_mismatches.append(mismatch)
        record = record_index.get(candidate_key)
        if record is None:
            continue
        expected_holder_status = expected.get("holderStatus")
        if expected_holder_status is not None:
            if expected_holder_status not in {"identified", "source_missing"}:
                missing_truth.append(key)
                exact_missing.append(key)
            elif _field_value(record, "holder.status") != expected_holder_status:
                mismatch = {
                    "key": key,
                    "field": "holder.status",
                    "expected": expected_holder_status,
                    "observed": _field_value(record, "holder.status"),
                }
                mismatches.append(mismatch)
                exact_mismatches.append(mismatch)
        expected_fields = {
            "approval.numberOriginal": expected.get("approvalNumberOriginal"),
            "holder.nameOriginal": expected.get("holderNameOriginal"),
        }
        if str(entry.get("sectionKey")) == "pma_item":
            expected_fields.update(
                {
                    "item.partNumberOriginal": expected.get("pmaPartNumberOriginal"),
                    "item.replacedPartNumberOriginal": expected.get("pmaReplacedPartNumberOriginal"),
                }
            )
        for field_path, expected_value in expected_fields.items():
            expects_reviewed_missing_holder = (
                field_path == "holder.nameOriginal"
                and expected_holder_status == "source_missing"
                and "holderNameOriginal" in expected
            )
            if expected_value is None and not expects_reviewed_missing_holder:
                missing_truth.append(key)
                exact_missing.append(key)
                continue
            if expected_value == "not_applicable":
                continue
            actual_value = _field_value(record, field_path)
            if actual_value != expected_value:
                mismatch = {
                    "key": key,
                    "field": field_path,
                    "expected": expected_value,
                    "observed": actual_value,
                }
                mismatches.append(mismatch)
                exact_mismatches.append(mismatch)

    negative_entries = [
        entry for entry in truth_entries if str(entry.get("reviewType")) == "negative_numeric"
    ]
    negative_missing = [
        _review_entry_key(entry)
        for entry in negative_entries
        if not isinstance((entry.get("expected") or {}).get("rowAnchor"), bool)
        or str((entry.get("review") or {}).get("status") or "pending") != "confirmed"
    ]
    negative_mismatches: list[dict[str, Any]] = []
    for entry in negative_entries:
        key = _review_entry_key(entry)
        candidate_present = _candidate_locator_key(entry) in row_index
        expected_anchor = (entry.get("expected") or {}).get("rowAnchor")
        if isinstance(expected_anchor, bool) and expected_anchor is not False:
            negative_mismatches.append(
                {
                    "key": key,
                    "field": "rowAnchor",
                    "expected": False,
                    "observedTruth": expected_anchor,
                }
            )
        elif expected_anchor is False and candidate_present:
            negative_mismatches.append(
                {
                    "key": key,
                    "field": "rowAnchor",
                    "expected": False,
                    "observedCandidateRow": True,
                }
            )
    continuation_entries = [
        entry
        for entry in truth_entries
        if str(entry.get("reviewType")) == "continuation_or_anomaly"
    ]
    continuation_missing = [
        _review_entry_key(entry)
        for entry in continuation_entries
        if not (entry.get("expected") or {}).get("continuationDisposition")
        or str((entry.get("review") or {}).get("status") or "pending") != "confirmed"
    ]
    def _unique_keys(keys: Iterable[tuple[Any, ...]]) -> list[list[Any]]:
        unique: list[tuple[Any, ...]] = []
        seen: set[tuple[Any, ...]] = set()
        for key in keys:
            if key not in seen:
                seen.add(key)
                unique.append(key)
        return [list(key) for key in unique[:50]]

    return {
        "schemaVersion": truth.get("schemaVersion"),
        "sourceSnapshotExpected": expected_source or None,
        "sourceSnapshotObserved": observed_source or None,
        "sourceSnapshotMatches": source_snapshot_matches,
        "entryCount": len(truth_entries),
        "duplicateEntries": _unique_keys(duplicate_truth),
        "missingTruth": _unique_keys(missing_truth),
        "rowAnchorMissing": _unique_keys(row_anchor_missing),
        "exactMissing": _unique_keys(exact_missing),
        "unconfirmed": _unique_keys(unconfirmed),
        "mismatches": mismatches[:50],
        "rowAnchorMismatches": row_anchor_mismatches[:50],
        "exactMismatches": exact_mismatches[:50],
        "negativeNumericEntries": len(negative_entries),
        "negativeNumericMissing": _unique_keys(negative_missing),
        "negativeNumericMismatches": negative_mismatches[:50],
        "continuationEntries": len(continuation_entries),
        "continuationMissing": _unique_keys(continuation_missing),
        "rowAnchorComplete": not row_anchor_missing and not row_anchor_mismatches and not unconfirmed,
        "exactMatchComplete": not exact_missing and not exact_mismatches and not unconfirmed,
        "negativeNumericComplete": bool(negative_entries) and not negative_missing and not negative_mismatches,
        "continuationComplete": not continuation_missing,
        "complete": (
            truth.get("schemaVersion") == REVIEW_TRUTH_SCHEMA_VERSION
            and source_snapshot_matches
            and not duplicate_truth
            and not missing_truth
            and not unconfirmed
            and not mismatches
            and bool(negative_entries)
            and not negative_missing
            and not continuation_missing
        ),
    }


def _load_record_files(run: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((run / "records").glob("*.jsonl")):
        records.extend(_load_jsonl(path))
    return records


def _compare_records(
    left: Iterable[Mapping[str, Any]], right: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    def index(records: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        return {
            str(record.get("recordId")): json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            for record in records
        }

    left_index = index(left)
    right_index = index(right)
    missing = sorted(set(left_index) - set(right_index))
    extra = sorted(set(right_index) - set(left_index))
    changed = sorted(
        record_id
        for record_id in set(left_index) & set(right_index)
        if left_index[record_id] != right_index[record_id]
    )
    return {
        "passed": not missing and not extra and not changed,
        "leftRecords": len(left_index),
        "rightRecords": len(right_index),
        "missingRecordIds": missing[:20],
        "extraRecordIds": extra[:20],
        "changedRecordIds": changed[:20],
    }


def evaluate_run(
    *,
    run: Path,
    schema_path: Path,
    matrix_path: Path,
    compare_run: Path | None = None,
    review_truth_path: Path | None = None,
) -> dict[str, Any]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise RuntimeError("jsonschema is required for golden-run evaluation") from exc

    manifest = _load_json(run / "manifest.json")
    schema = _load_json(schema_path)
    matrix = _load_json(matrix_path)
    validator = Draft202012Validator(schema)
    manifest_errors = [error.message for error in validator.iter_errors(manifest)]

    records: list[dict[str, Any]] = []
    record_errors: list[dict[str, Any]] = []
    record_files = sorted((run / "records").glob("*.jsonl"))
    for path in record_files:
        for line_number, record in enumerate(_load_jsonl(path), 1):
            records.append(record)
            errors = list(validator.iter_errors(record))
            try:
                validate_catalog_record_shape(record)
            except ValueError as exc:
                record_errors.append({"file": path.name, "line": line_number, "message": str(exc)})
            for error in errors:
                record_errors.append(
                    {"file": path.name, "line": line_number, "message": error.message}
                )

    source_omission_review = _evaluate_source_omission_review(
        run=run,
        manifest=manifest,
        records=records,
    )
    quality_summary = _load_json(run / "quality" / "summary.json")
    candidate_rows = _load_jsonl(run / "quality" / "candidate-rows.jsonl")
    rejected_rows = _load_jsonl(run / "quality" / "rejected-rows.jsonl")
    signals = _load_jsonl(run / "quality" / "signals.jsonl")
    signal_counts = Counter(str(signal.get("code")) for signal in signals)
    expected_sections = {
        str(section.get("sectionKey")) for section in matrix.get("sections") or ()
    }
    observed_sections = {
        str(section_key) for section_key in (quality_summary.get("sectionPageHits") or {})
    }

    physical_pages = set(int(page) for page in manifest.get("sample", {}).get("pages") or ())
    invalid_pages: list[dict[str, Any]] = []
    evidence_errors: list[dict[str, Any]] = []
    core_missing: list[dict[str, Any]] = []
    core_evidence_count = 0
    core_value_count = 0
    for record in records:
        source = record.get("source")
        if not isinstance(source, Mapping):
            source = {}
        source_page = int(source.get("pdfPageStart") or 0)
        if source_page not in physical_pages:
            invalid_pages.append({"recordId": record.get("recordId"), "pdfPage": source_page})
        if source.get("sourceSnapshotSha256") != manifest.get("document", {}).get("sha256"):
            invalid_pages.append({"recordId": record.get("recordId"), "reason": "source_hash_mismatch"})
        evidence_by_field = _evidence_by_field(record)
        for field_path in CORE_FIELDS_BY_SECTION.get(str(record.get("sectionKey")), ()):
            value = _field_value(record, field_path)
            if not _has_value(value):
                continue
            core_value_count += 1
            matching = evidence_by_field.get(field_path, [])
            valid_matching = [
                evidence
                for evidence in matching
                if evidence.get("pdfPage")
                and evidence.get("bbox")
                and len(evidence.get("bbox") or ()) == 4
                and evidence.get("sourceWordIds")
            ]
            if valid_matching:
                core_evidence_count += 1
            else:
                core_missing.append({"recordId": record.get("recordId"), "fieldPath": field_path})
        for evidence in record.get("fieldEvidence") or ():
            if not isinstance(evidence, Mapping):
                evidence_errors.append({"recordId": record.get("recordId"), "reason": "not_object"})
                continue
            if _is_reviewed_empty_holder_evidence(record, evidence):
                continue
            if not evidence.get("pdfPage") or not evidence.get("bbox") or not evidence.get("sourceWordIds"):
                evidence_errors.append(
                    {"recordId": record.get("recordId"), "fieldPath": evidence.get("fieldPath")}
                )

    date_total = 0
    date_failed = 0
    for row in candidate_rows:
        cells = row.get("cells") if isinstance(row.get("cells"), Mapping) else {}
        for field_path in DATE_FIELDS:
            if _has_value(cells.get(field_path)):
                date_total += 1
    date_failed = int(signal_counts.get("date_parse_failed", 0))
    date_parse_rate = (date_total - date_failed) / date_total if date_total else 0.0

    duplicate_record_ids = [key for key, count in Counter(record.get("recordId") for record in records).items() if count > 1]
    rerun_comparison = (
        _compare_records(records, _load_record_files(compare_run)) if compare_run is not None else None
    )
    locator_coverage = _positive_locator_coverage(matrix, candidate_rows)
    review_queue = _build_review_queue(matrix, candidate_rows, records)
    review_truth_evaluation: dict[str, Any] | None = None
    if review_truth_path is not None:
        review_truth_evaluation = _evaluate_review_truth(
            truth=_load_json(review_truth_path),
            candidate_rows=candidate_rows,
            records=records,
            expected_source_snapshot_sha256=str(
                manifest.get("document", {}).get("sha256") or ""
            ),
        )
    review_truth_source_matches = bool(
        review_truth_evaluation
        and review_truth_evaluation.get("sourceSnapshotMatches")
    )
    row_anchor_review_passed = bool(
        review_truth_source_matches
        and review_truth_evaluation
        and review_truth_evaluation.get("rowAnchorComplete")
    )
    exact_review_passed = bool(
        review_truth_source_matches
        and review_truth_evaluation
        and review_truth_evaluation.get("exactMatchComplete")
    )
    negative_numeric_review_passed = bool(
        review_truth_source_matches
        and review_truth_evaluation
        and review_truth_evaluation.get("negativeNumericComplete")
    )
    continuation_review_passed = bool(
        review_truth_source_matches
        and review_truth_evaluation
        and review_truth_evaluation.get("continuationComplete")
    )
    gates: dict[str, dict[str, Any]] = {
        "section_recognition": _check(
            expected_sections == observed_sections,
            expected=sorted(expected_sections),
            observed=sorted(observed_sections),
        ),
        "row_anchor_recall": _check(
            row_anchor_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "matrix contains locators but no independently curated complete row truth set",
                }
            ),
            positive_locator_coverage=locator_coverage,
        ),
        "non_row_numeric_false_positives": _check(
            negative_numeric_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "requires a negative numeric-token truth set",
                }
            ),
        ),
        "approval_exact_match": _check(
            exact_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "requires independently curated expected field values",
                }
            ),
        ),
        "holder_exact_match": _check(
            exact_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "requires independently curated expected field values",
                }
            ),
        ),
        "pma_part_exact_match": _check(
            exact_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "requires independently curated PMA part/replaced-part values",
                }
            ),
        ),
        "physical_page_source": _check(not invalid_pages, invalid=invalid_pages[:20]),
        "cross_page_continuation": _check(
            continuation_review_passed,
            **(
                {"reviewTruth": review_truth_evaluation}
                if review_truth_evaluation is not None
                else {
                    "status": "review_required",
                    "reason": "requires manually reviewed continuation/merged-cell assertions",
                }
            ),
        ),
        "date_normalization": _check(
            date_parse_rate >= 0.995,
            total=date_total,
            failed=date_failed,
            parseRate=date_parse_rate,
        ),
        "deterministic_rerun": _check(
            rerun_comparison["passed"] if rerun_comparison is not None else False,
            **(
                {"comparison": rerun_comparison, "compareRun": str(compare_run)}
                if rerun_comparison is not None
                else {
                    "status": "review_required",
                    "reason": "rerun comparison was not supplied",
                }
            ),
            duplicateRecordIds=duplicate_record_ids,
        ),
        "core_field_evidence": _check(
            not core_missing and not evidence_errors,
            valuedCoreFields=core_value_count,
            evidencedCoreFields=core_evidence_count,
            missing=core_missing[:20],
            invalidEvidence=evidence_errors[:20],
        ),
        "schema_contract": _check(
            not manifest_errors and not record_errors,
            manifestErrors=manifest_errors[:20],
            recordErrors=record_errors[:20],
            recordFiles=len(record_files),
            records=len(records),
        ),
    }
    if manifest.get("schemaVersion") == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        gates["source_omission_review"] = {
            **source_omission_review,
            "passed": bool(source_omission_review.get("passed")),
        }
    for gate_name in (
        "row_anchor_recall",
        "non_row_numeric_false_positives",
        "approval_exact_match",
        "holder_exact_match",
        "pma_part_exact_match",
        "cross_page_continuation",
    ):
        gate = gates[gate_name]
        if not gate.get("passed") and "status" not in gate:
            gate["status"] = "review_required"
    hard_gate_passed = all(gate.get("passed") for gate in gates.values())
    return {
        "schemaVersion": manifest.get("schemaVersion"),
        "runId": manifest.get("runId"),
        "sourceSha256": manifest.get("document", {}).get("sha256"),
        "sampledPages": len(physical_pages),
        "candidateRows": len(candidate_rows),
        "candidateRecords": len(records),
        "rejectedRows": len(rejected_rows),
        "reviewQueueCount": len(review_queue),
        "reviewQueueMissing": sum(1 for item in review_queue if not item["candidateRowPresent"]),
        "qualitySignalCount": len(signals),
        "qualitySignalCodes": dict(sorted(signal_counts.items())),
        "gates": gates,
        "hardGatePassed": hard_gate_passed,
        "publicationAllowed": False,
        "reviewRequired": [name for name, gate in gates.items() if gate.get("status") == "review_required"],
        "reviewTruth": review_truth_evaluation,
        "sourceOmissionReview": source_omission_review,
        "observations": {
            "coreFieldEvidenceCoverage": core_evidence_count / core_value_count if core_value_count else 0.0,
            "candidateRecordIdsUnique": not duplicate_record_ids,
            "expectedApprovalTypes": sorted(CAAC_APPROVAL_TYPES),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--compare-run", type=Path)
    parser.add_argument("--review-truth", type=Path)
    parser.add_argument("--review-output", type=Path)
    parser.add_argument("--review-template-output", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def _resolve_review_template_output(
    *, run: Path, requested: Path | None, review_truth_path: Path | None
) -> Path:
    """Choose a generated template path without overwriting completed truth."""

    output = requested or run / "quality" / "review-truth.template.json"
    if review_truth_path is not None and output.resolve() == review_truth_path.resolve():
        return run / "quality" / "review-truth.template.next.json"
    return output


def main() -> int:
    args = build_parser().parse_args()
    result = evaluate_run(
        run=args.run,
        schema_path=args.schema,
        matrix_path=args.matrix,
        compare_run=args.compare_run,
        review_truth_path=args.review_truth,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    review_output = args.review_output or args.run / "quality" / "review-queue.jsonl"
    review_queue = _build_review_queue(
        _load_json(args.matrix),
        _load_jsonl(args.run / "quality" / "candidate-rows.jsonl"),
        _load_record_files(args.run),
    )
    review_output.parent.mkdir(parents=True, exist_ok=True)
    review_output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in review_queue),
        encoding="utf-8",
    )
    review_template_output = _resolve_review_template_output(
        run=args.run,
        requested=args.review_template_output,
        review_truth_path=args.review_truth,
    )
    review_template_output.parent.mkdir(parents=True, exist_ok=True)
    manifest = _load_json(args.run / "manifest.json")
    review_template = _build_review_truth_template(
        run_id=str(manifest.get("runId") or result.get("runId") or ""),
        source_snapshot_sha256=str(
            manifest.get("document", {}).get("sha256") or result.get("sourceSha256") or ""
        ),
        review_queue=review_queue,
    )
    review_template_output.write_text(
        json.dumps(review_template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    artifact_specs = [
        (
            "golden-evaluation",
            args.output,
            "application/json",
            1,
        ),
        (
            "review-queue",
            review_output,
            "application/x-ndjson",
            len(review_queue),
        ),
        (
            "review-truth-template",
            review_template_output,
            "application/json",
            len(review_template.get("entries") or ()),
        ),
    ]
    artifacts = manifest.setdefault("artifacts", [])
    for dataset, path, content_type, records in artifact_specs:
        if path is None:
            continue
        try:
            relative_path = path.relative_to(args.run).as_posix()
        except ValueError:
            relative_path = str(path)
        artifacts[:] = [item for item in artifacts if item.get("dataset") != dataset]
        artifacts.append(
            {
                "dataset": dataset,
                "format": "json" if path.suffix == ".json" else "jsonl",
                "path": relative_path,
                "contentType": content_type,
                "bytes": path.stat().st_size,
                "records": records,
            }
        )
    _write_json(args.run / "manifest.json", manifest)
    print(payload, end="")
    # Review-required gates intentionally keep the run non-publishable but do
    # not make this bounded diagnostic command fail like a parser crash.
    return 0 if result["gates"]["schema_contract"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
