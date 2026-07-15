from __future__ import annotations

import json
from pathlib import Path

from tools.provider_gold_review_status import validate_review_queue


def _write_queue(path: Path, page: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "minimum_approved_pages": 1,
                "pages": [page],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _page(*, status: str = "pending") -> dict:
    return {
        "id": "review-doc-p1",
        "document_id": "doc",
        "page_number": 1,
        "review_status": status,
        "review": {
            "reviewer": "",
            "reviewed_at": "",
            "source_screenshot": "",
            "notes": "Fill all expected fields and then set review_status to approved or rejected.",
        },
        "expected": {
            "blockKinds": [],
            "anchors": [],
            "orderedAnchors": [],
            "tableAnchors": [],
            "criticalTokens": [],
            "mustNotBeHeading": [],
        },
    }


def test_pending_queue_is_incomplete_but_not_invalid(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    _write_queue(queue, _page())

    report = validate_review_queue(queue_path=queue)

    assert report["status"] == "incomplete"
    assert report["counts"] == {"total": 1, "approved": 0, "pending": 1, "rejected": 0, "invalid": 0}
    assert report["errors"] == []


def test_approved_page_requires_named_review_and_expected_fields(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    page = _page(status="approved")
    page["expected"].pop("criticalTokens")
    _write_queue(queue, page)

    report = validate_review_queue(queue_path=queue)

    assert report["status"] == "invalid"
    assert {item["code"] for item in report["errors"]} == {"review_field_missing", "expected_field_missing"}


def test_approved_page_is_ready_when_review_and_evidence_are_complete(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    screenshot = evidence_root / "pages" / "doc" / "p0001.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png")
    queue = tmp_path / "queue.json"
    page = _page(status="approved")
    page["review"] = {
        "reviewer": "reviewer-a",
        "reviewed_at": "2026-07-14T18:30:00+08:00",
        "source_screenshot": "pages/doc/p0001.png",
        "notes": "Confirmed page order and no table on this page.",
    }
    page["expected"]["blockKinds"] = ["paragraph"]
    page["expected"]["anchors"] = ["Confirmed"]
    page["expected"]["criticalTokens"] = ["Confirmed"]
    _write_queue(queue, page)

    report = validate_review_queue(queue_path=queue, evidence_root=evidence_root)

    assert report["status"] == "ready"
    assert report["counts"]["approved"] == 1
    assert report["errors"] == []
