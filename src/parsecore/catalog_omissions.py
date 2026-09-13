"""Validation helpers for explicitly reviewed source-cell omissions.

The catalog contract normally requires a named holder.  The narrowly scoped
V2 exception exists only when a reviewer has documented that the source cell
is visibly empty.  This module keeps the allow-list validation independent of
both the PDF extractor and the run orchestration code.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any


SOURCE_OMISSION_REVIEW_SCHEMA_VERSION = (
    "caac-approved-catalog.source-omission-review.v1"
)
SOURCE_OMISSION_FIELD_PATH = "holder.nameOriginal"
SOURCE_OMISSION_REASON_CODE = "empty_source_cell"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceOmissionReviewError(ValueError):
    """Raised when an omission-review artifact is not safe to apply."""


def _required_non_empty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise SourceOmissionReviewError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise SourceOmissionReviewError(f"{label} must be a non-empty string")
    return normalized


def _required_positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceOmissionReviewError(f"{label} must be a positive integer")
    return value


def _normalized_bbox(value: Any, *, label: str) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise SourceOmissionReviewError(f"{label} must contain four finite numbers")
    coordinates: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise SourceOmissionReviewError(f"{label} must contain four finite numbers")
        normalized = float(coordinate)
        if not math.isfinite(normalized):
            raise SourceOmissionReviewError(f"{label} must contain four finite numbers")
        coordinates.append(normalized)
    if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
        raise SourceOmissionReviewError(f"{label} has an invalid coordinate order")
    return coordinates


def normalize_source_omission_review(
    value: Mapping[str, Any],
    *,
    source_snapshot_sha256: str,
    section_key: str | None = None,
) -> dict[str, Any]:
    """Return a canonical, fail-closed reviewed omission allow-list.

    Every review can authorize exactly one empty ``holder.nameOriginal`` cell.
    The caller supplies the immutable document digest and, for a bounded run,
    the only section that the review may cover.
    """

    if not isinstance(value, Mapping):
        raise SourceOmissionReviewError("source omission review must be an object")
    if value.get("schemaVersion") != SOURCE_OMISSION_REVIEW_SCHEMA_VERSION:
        raise SourceOmissionReviewError("unsupported source omission review schemaVersion")
    expected_hash = _required_non_empty_string(
        source_snapshot_sha256,
        label="source_snapshot_sha256",
    ).lower()
    if not _SHA256_RE.fullmatch(expected_hash):
        raise SourceOmissionReviewError("source_snapshot_sha256 must be a lowercase SHA-256")
    review_hash = _required_non_empty_string(
        value.get("sourceSnapshotSha256"),
        label="source omission review sourceSnapshotSha256",
    ).lower()
    if review_hash != expected_hash:
        raise SourceOmissionReviewError(
            "source omission review sourceSnapshotSha256 does not match the source document"
        )
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)) or not raw_entries:
        raise SourceOmissionReviewError("source omission review entries must be a non-empty array")

    normalized_entries: list[dict[str, Any]] = []
    seen_review_ids: set[str] = set()
    seen_cells: set[tuple[str, int, int, str]] = set()
    expected_section = str(section_key or "").strip()
    for position, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, Mapping):
            raise SourceOmissionReviewError(
                f"source omission review entry {position} must be an object"
            )
        review_id = _required_non_empty_string(
            raw_entry.get("reviewId"),
            label=f"source omission review entry {position} reviewId",
        )
        if len(review_id) > 160:
            raise SourceOmissionReviewError(
                f"source omission review entry {position} reviewId is too long"
            )
        entry_section = _required_non_empty_string(
            raw_entry.get("sectionKey"),
            label=f"source omission review entry {position} sectionKey",
        )
        if expected_section and entry_section != expected_section:
            raise SourceOmissionReviewError(
                "source omission review entry sectionKey is outside the bounded run"
            )
        row_number = _required_positive_int(
            raw_entry.get("rowNumber"),
            label=f"source omission review entry {position} rowNumber",
        )
        pdf_page = _required_positive_int(
            raw_entry.get("pdfPage"),
            label=f"source omission review entry {position} pdfPage",
        )
        if raw_entry.get("fieldPath") != SOURCE_OMISSION_FIELD_PATH:
            raise SourceOmissionReviewError(
                "source omission reviews may only authorize holder.nameOriginal"
            )
        if raw_entry.get("reasonCode") != SOURCE_OMISSION_REASON_CODE:
            raise SourceOmissionReviewError(
                "source omission reviews must use reasonCode empty_source_cell"
            )
        reviewed_on = _required_non_empty_string(
            raw_entry.get("reviewedOn"),
            label=f"source omission review entry {position} reviewedOn",
        )
        try:
            date.fromisoformat(reviewed_on)
        except ValueError as exc:
            raise SourceOmissionReviewError(
                f"source omission review entry {position} reviewedOn must be an ISO date"
            ) from exc
        bbox = _normalized_bbox(
            raw_entry.get("bbox"),
            label=f"source omission review entry {position} bbox",
        )
        cell_key = (entry_section, row_number, pdf_page, SOURCE_OMISSION_FIELD_PATH)
        if review_id in seen_review_ids:
            raise SourceOmissionReviewError("source omission review contains duplicate reviewId")
        if cell_key in seen_cells:
            raise SourceOmissionReviewError("source omission review contains duplicate source cell")
        seen_review_ids.add(review_id)
        seen_cells.add(cell_key)
        normalized_entries.append(
            {
                "reviewId": review_id,
                "sectionKey": entry_section,
                "rowNumber": row_number,
                "pdfPage": pdf_page,
                "fieldPath": SOURCE_OMISSION_FIELD_PATH,
                "reasonCode": SOURCE_OMISSION_REASON_CODE,
                "bbox": bbox,
                "reviewedOn": reviewed_on,
            }
        )
    normalized_entries.sort(
        key=lambda item: (
            str(item["sectionKey"]),
            int(item["rowNumber"]),
            int(item["pdfPage"]),
            str(item["fieldPath"]),
            str(item["reviewId"]),
        )
    )
    return {
        "schemaVersion": SOURCE_OMISSION_REVIEW_SCHEMA_VERSION,
        "sourceSnapshotSha256": expected_hash,
        "entries": normalized_entries,
    }


def source_omission_review_index(
    review: Mapping[str, Any],
) -> dict[tuple[str, int, int, str], Mapping[str, Any]]:
    """Index an already normalized review by the one source cell it permits."""

    entries = review.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise SourceOmissionReviewError("normalized source omission review entries are invalid")
    result: dict[tuple[str, int, int, str], Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise SourceOmissionReviewError("normalized source omission review entry is invalid")
        key = (
            str(entry.get("sectionKey") or ""),
            int(entry.get("rowNumber") or 0),
            int(entry.get("pdfPage") or 0),
            str(entry.get("fieldPath") or ""),
        )
        if key in result:
            raise SourceOmissionReviewError("normalized source omission review has duplicate source cell")
        result[key] = entry
    return result


__all__ = [
    "SOURCE_OMISSION_FIELD_PATH",
    "SOURCE_OMISSION_REASON_CODE",
    "SOURCE_OMISSION_REVIEW_SCHEMA_VERSION",
    "SourceOmissionReviewError",
    "normalize_source_omission_review",
    "source_omission_review_index",
]
