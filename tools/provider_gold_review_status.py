"""Validate a human-reviewed Provider gold queue without changing it.

The command is deliberately read-only.  It reports whether a queue is ready
for evaluation, but it never fills evidence, changes ``review_status`` or
modifies the controlled queue.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "2026-07-provider-gold-review-status"
_REVIEW_FIELDS = ("reviewer", "reviewed_at", "source_screenshot", "notes")
_EXPECTED_FIELDS = (
    "blockKinds",
    "anchors",
    "orderedAnchors",
    "tableAnchors",
    "criticalTokens",
    "mustNotBeHeading",
)
_PLACEHOLDER_NOTE = "Fill all expected fields and then set review_status to approved or rejected."


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _non_empty(value: Any) -> bool:
    return bool(str(value or "").strip())


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _resolve_evidence_path(raw_path: str, evidence_root: Path | None) -> Path | None:
    if evidence_root is None:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = evidence_root / candidate
    return candidate.resolve()


def _validate_iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_review_queue(
    *,
    queue_path: str | Path,
    evidence_root: str | Path | None = None,
    minimum_approved_pages: int | None = None,
) -> dict[str, Any]:
    """Return a read-only status report for a Provider gold review queue."""

    queue_file = Path(queue_path).resolve()
    payload = _load_json(queue_file)
    if not isinstance(payload, Mapping):
        raise ValueError("gold_review_queue_must_be_object")
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("gold_review_queue_pages_must_be_list")

    configured_minimum = payload.get("minimum_approved_pages") or 50
    minimum = max(1, int(minimum_approved_pages or configured_minimum))
    root = Path(evidence_root).resolve() if evidence_root else None
    errors: list[dict[str, Any]] = []
    page_ids: set[str] = set()
    counts = {"total": len(raw_pages), "approved": 0, "pending": 0, "rejected": 0, "invalid": 0}
    approved_page_ids: list[str] = []
    pending_page_ids: list[str] = []
    rejected_page_ids: list[str] = []

    def add_error(page_id: str, code: str, **detail: Any) -> None:
        errors.append({"page_id": page_id, "code": code, **detail})

    for index, raw_page in enumerate(raw_pages):
        if not isinstance(raw_page, Mapping):
            counts["invalid"] += 1
            add_error(f"index-{index}", "page_must_be_object")
            continue

        page_id = str(raw_page.get("id") or "").strip() or f"index-{index}"
        if page_id in page_ids:
            counts["invalid"] += 1
            add_error(page_id, "duplicate_page_id")
        page_ids.add(page_id)

        document_id = str(raw_page.get("document_id") or raw_page.get("documentId") or "").strip()
        try:
            page_number = int(raw_page.get("page_number") or raw_page.get("pageNumber") or 0)
        except (TypeError, ValueError):
            page_number = 0
        if not document_id or page_number <= 0:
            counts["invalid"] += 1
            add_error(page_id, "page_identity_invalid", document_id=document_id, page_number=page_number)

        status = str(raw_page.get("review_status") or "").strip().casefold()
        if status not in {"approved", "pending", "rejected"}:
            counts["invalid"] += 1
            add_error(page_id, "review_status_invalid", review_status=status)
            continue
        counts[status] += 1
        if status == "pending":
            pending_page_ids.append(page_id)
        elif status == "rejected":
            rejected_page_ids.append(page_id)
        else:
            approved_page_ids.append(page_id)

        review = raw_page.get("review")
        if not isinstance(review, Mapping):
            review = {}
        expected = raw_page.get("expected")
        if not isinstance(expected, Mapping):
            expected = {}

        for key in _EXPECTED_FIELDS:
            if key not in expected:
                if status == "approved":
                    add_error(page_id, "expected_field_missing", field=key)
                continue
            if not _is_list(expected.get(key)):
                add_error(page_id, "expected_field_must_be_list", field=key)

        if status != "approved":
            continue

        for key in _REVIEW_FIELDS:
            value = str(review.get(key) or "").strip()
            if not value or (key == "notes" and value == _PLACEHOLDER_NOTE):
                add_error(page_id, "review_field_missing", field=key)
        reviewed_at = str(review.get("reviewed_at") or "").strip()
        if reviewed_at and not _validate_iso8601(reviewed_at):
            add_error(page_id, "reviewed_at_invalid", value=reviewed_at)

        screenshot = str(review.get("source_screenshot") or "").strip()
        resolved_screenshot = _resolve_evidence_path(screenshot, root)
        if resolved_screenshot is not None and not resolved_screenshot.is_file():
            add_error(page_id, "source_screenshot_not_found", path=str(resolved_screenshot))

    if counts["approved"] < minimum:
        status = "incomplete"
    elif errors:
        status = "invalid"
    else:
        status = "ready"

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "queue_path": str(queue_file),
        "evidence_root": str(root) if root else None,
        "minimum_approved_pages": minimum,
        "counts": counts,
        "approved_page_ids": approved_page_ids,
        "pending_page_ids": pending_page_ids,
        "rejected_page_ids": rejected_page_ids,
        "errors": errors,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a human-reviewed Provider gold queue without modifying it")
    parser.add_argument("--queue", required=True, help="Controlled gold review queue JSON")
    parser.add_argument("--evidence-root", help="Evidence packet directory used to verify approved screenshots")
    parser.add_argument("--minimum-approved-pages", type=int, help="Override the queue minimum")
    parser.add_argument("--out-json", help="Optional status report path")
    parser.add_argument(
        "--require-minimum",
        action="store_true",
        help="Return non-zero until the minimum approved page count is met",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = validate_review_queue(
        queue_path=args.queue,
        evidence_root=args.evidence_root,
        minimum_approved_pages=args.minimum_approved_pages,
    )
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[provider-gold-review-status] wrote {output}")
    counts = report["counts"]
    print(
        "[provider-gold-review-status] "
        f"status={report['status']} approved={counts['approved']} "
        f"pending={counts['pending']} rejected={counts['rejected']} errors={len(report['errors'])}"
    )
    if report["errors"] or (args.require_minimum and report["status"] != "ready"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
