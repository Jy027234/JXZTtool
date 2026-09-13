from __future__ import annotations

import json

from tools.evaluate_caac_catalog_golden import (
    REVIEW_TRUTH_SCHEMA_VERSION,
    _build_review_queue,
    _build_review_truth_template,
    _evaluate_review_truth,
    _resolve_review_template_output,
)
from tools.import_caac_catalog_review_workbook import REQUIRED_HEADERS, import_workbook


def test_review_truth_template_does_not_copy_candidate_values() -> None:
    queue = [
        {
            "reviewType": "positive",
            "sectionKey": "tc",
            "pdfPage": 3,
            "rowNumber": 1,
            "expectedSignals": [],
        },
        {
            "reviewType": "positive",
            "sectionKey": "pma_item",
            "pdfPage": 162,
            "rowNumber": 1,
            "expectedSignals": [],
        },
    ]

    template = _build_review_truth_template(
        run_id="run_test",
        source_snapshot_sha256="a" * 64,
        review_queue=queue,
    )

    assert template["schemaVersion"] == REVIEW_TRUTH_SCHEMA_VERSION
    assert template["status"] == "template"
    assert template["entries"][0]["expected"]["approvalNumberOriginal"] is None
    assert template["entries"][0]["expected"]["pmaPartNumberOriginal"] == "not_applicable"
    assert template["entries"][1]["expected"]["pmaPartNumberOriginal"] is None
    assert template["entries"][0]["reviewId"] == "tc:3:1:positive"


def test_completed_review_truth_can_pass_review_subgates() -> None:
    candidate_rows = [
        {"sectionKey": "tc", "pdfPage": 3, "rowNumber": 1},
        {"sectionKey": "tc", "pdfPage": 3, "rowNumber": 2},
    ]
    records = [
        {
            "sectionKey": "tc",
            "rowNumber": 1,
            "approval": {"numberOriginal": "TC001A"},
            "holder": {"nameOriginal": "Holder A"},
            "source": {"pdfPageStart": 3},
        },
        {
            "sectionKey": "tc",
            "rowNumber": 2,
            "approval": {"numberOriginal": "TC002A"},
            "holder": {"nameOriginal": "Holder B"},
            "source": {"pdfPageStart": 3},
        },
    ]
    truth = {
        "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
        "entries": [
            {
                "sectionKey": "tc",
                "pdfPage": 3,
                "rowNumber": 1,
                "reviewType": "positive",
                "expected": {
                    "rowAnchor": True,
                    "approvalNumberOriginal": "TC001A",
                    "holderNameOriginal": "Holder A",
                    "pmaPartNumberOriginal": "not_applicable",
                    "pmaReplacedPartNumberOriginal": "not_applicable",
                    "continuationDisposition": None,
                },
                "review": {"status": "confirmed"},
            },
            {
                "sectionKey": "tc",
                "pdfPage": 3,
                "rowNumber": 2,
                "reviewType": "continuation_or_anomaly",
                "expected": {
                    "rowAnchor": True,
                    "approvalNumberOriginal": "TC002A",
                    "holderNameOriginal": "Holder B",
                    "pmaPartNumberOriginal": "not_applicable",
                    "pmaReplacedPartNumberOriginal": "not_applicable",
                    "continuationDisposition": "keep_separate",
                },
                "review": {"status": "confirmed"},
            },
            {
                "sectionKey": "tc",
                "pdfPage": 3,
                "rowNumber": 99,
                "reviewType": "negative_numeric",
                "expected": {
                    "rowAnchor": False,
                    "continuationDisposition": "reject",
                },
                "review": {"status": "confirmed"},
            },
        ],
    }

    result = _evaluate_review_truth(
        truth=truth,
        candidate_rows=candidate_rows,
        records=records,
    )

    assert result["rowAnchorComplete"] is True
    assert result["exactMatchComplete"] is True
    assert result["negativeNumericComplete"] is True
    assert result["continuationComplete"] is True
    assert result["complete"] is True


def test_review_truth_reports_exact_mismatch_and_pending_entry() -> None:
    result = _evaluate_review_truth(
        truth={
            "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
            "entries": [
                {
                    "sectionKey": "tc",
                    "pdfPage": 3,
                    "rowNumber": 1,
                    "reviewType": "positive",
                    "expected": {
                        "rowAnchor": True,
                        "approvalNumberOriginal": "WRONG",
                        "holderNameOriginal": "Holder A",
                        "pmaPartNumberOriginal": "not_applicable",
                        "pmaReplacedPartNumberOriginal": "not_applicable",
                    },
                    "review": {"status": "pending"},
                }
            ],
        },
        candidate_rows=[{"sectionKey": "tc", "pdfPage": 3, "rowNumber": 1}],
        records=[
            {
                "sectionKey": "tc",
                "rowNumber": 1,
                "approval": {"numberOriginal": "TC001A"},
                "holder": {"nameOriginal": "Holder A"},
                "source": {"pdfPageStart": 3},
            }
        ],
    )

    assert result["exactMatchComplete"] is False
    assert result["unconfirmed"] == [["tc", 3, 1, "positive"]]
    assert result["exactMismatches"][0]["field"] == "approval.numberOriginal"


def test_review_truth_accepts_explicit_source_missing_holder() -> None:
    result = _evaluate_review_truth(
        truth={
            "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
            "entries": [
                {
                    "sectionKey": "vstc",
                    "pdfPage": 17023,
                    "rowNumber": 875,
                    "reviewType": "positive",
                    "expected": {
                        "rowAnchor": True,
                        "approvalNumberOriginal": "VSTC0996",
                        "holderNameOriginal": None,
                        "holderStatus": "source_missing",
                        "pmaPartNumberOriginal": "not_applicable",
                        "pmaReplacedPartNumberOriginal": "not_applicable",
                        "continuationDisposition": None,
                    },
                    "review": {"status": "confirmed"},
                }
            ],
        },
        candidate_rows=[{"sectionKey": "vstc", "pdfPage": 17023, "rowNumber": 875}],
        records=[
            {
                "sectionKey": "vstc",
                "rowNumber": 875,
                "approval": {"numberOriginal": "VSTC0996"},
                "holder": {"status": "source_missing", "nameOriginal": None},
                "source": {"pdfPageStart": 17023},
            }
        ],
    )

    assert result["rowAnchorComplete"] is True
    assert result["exactMatchComplete"] is True


def test_review_truth_source_snapshot_mismatch_fails_closed() -> None:
    result = _evaluate_review_truth(
        truth={
            "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
            "sourceSnapshotSha256": "b" * 64,
            "entries": [],
        },
        candidate_rows=[],
        records=[],
        expected_source_snapshot_sha256="a" * 64,
    )

    assert result["sourceSnapshotExpected"] == "a" * 64
    assert result["sourceSnapshotObserved"] == "b" * 64
    assert result["sourceSnapshotMatches"] is False
    assert result["complete"] is False


def test_positive_and_continuation_assertions_can_share_one_locator() -> None:
    base_expected = {
        "rowAnchor": True,
        "approvalNumberOriginal": "TC001A",
        "holderNameOriginal": "Holder A",
        "pmaPartNumberOriginal": "not_applicable",
        "pmaReplacedPartNumberOriginal": "not_applicable",
    }
    truth = {
        "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
        "entries": [
            {
                "sectionKey": "tc",
                "pdfPage": 3,
                "rowNumber": 1,
                "reviewType": "positive",
                "expected": {**base_expected},
                "review": {"status": "confirmed"},
            },
            {
                "sectionKey": "tc",
                "pdfPage": 3,
                "rowNumber": 1,
                "reviewType": "continuation_or_anomaly",
                "expected": {
                    **base_expected,
                    "continuationDisposition": "keep_separate",
                },
                "review": {"status": "confirmed"},
            },
        ],
    }
    result = _evaluate_review_truth(
        truth=truth,
        candidate_rows=[{"sectionKey": "tc", "pdfPage": 3, "rowNumber": 1}],
        records=[
            {
                "sectionKey": "tc",
                "rowNumber": 1,
                "approval": {"numberOriginal": "TC001A"},
                "holder": {"nameOriginal": "Holder A"},
                "source": {"pdfPageStart": 3},
            }
        ],
    )

    assert result["duplicateEntries"] == []
    assert result["rowAnchorComplete"] is True
    assert result["exactMatchComplete"] is True
    assert result["continuationComplete"] is True


def test_review_evaluation_never_overwrites_completed_truth_input(tmp_path) -> None:
    completed = tmp_path / "quality" / "review-truth.template.json"
    assert _resolve_review_template_output(
        run=tmp_path,
        requested=None,
        review_truth_path=completed,
    ) == tmp_path / "quality" / "review-truth.template.next.json"


def test_review_workbook_import_populates_only_source_truth_columns(tmp_path) -> None:
    from openpyxl import Workbook

    queue = [
        {
            "reviewType": "positive",
            "sectionKey": "tc",
            "pdfPage": 3,
            "rowNumber": 1,
            "expectedSignals": [],
        }
    ]
    template = _build_review_truth_template(
        run_id="run_test",
        source_snapshot_sha256="a" * 64,
        review_queue=queue,
    )
    template_path = tmp_path / "review-truth.template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Review Queue"
    for _ in range(4):
        sheet.append([])
    headers = sorted(REQUIRED_HEADERS)
    sheet.append(headers)
    values = {header: "" for header in headers}
    values.update(
        {
            "Review ID": "tc:3:1:positive",
            "Review status": "confirmed",
            "Reviewer": "reviewer-a",
            "Truth row anchor": "true",
            "Truth approval original": "TC001A",
            "Truth holder original": "Holder A",
            "Truth PMA part original": "not_applicable",
            "Truth replaced part original": "not_applicable",
            "Notes": "checked against source PDF",
        }
    )
    sheet.append([values[header] for header in headers])
    workbook_path = tmp_path / "review.xlsx"
    workbook.save(workbook_path)

    result = import_workbook(workbook_path=workbook_path, template_path=template_path)
    entry = result["entries"][0]
    assert result["status"] == "completed"
    assert entry["expected"]["rowAnchor"] is True
    assert entry["expected"]["approvalNumberOriginal"] == "TC001A"
    assert entry["expected"]["holderNameOriginal"] == "Holder A"
    assert entry["expected"]["pmaPartNumberOriginal"] == "not_applicable"
    assert entry["review"]["status"] == "confirmed"
    assert "candidateRecord" not in entry


def test_negative_numeric_examples_are_queued_and_candidate_rows_fail_closed() -> None:
    queue = _build_review_queue(
        {
            "sections": [],
            "negativeNumericExamples": [
                {
                    "sectionKey": "document",
                    "pdfPage": 1,
                    "rowNumber": None,
                    "tokenText": "2024",
                    "reason": "cover cutoff",
                }
            ],
        },
        candidate_rows=[],
        records=[],
    )
    assert queue[0]["reviewType"] == "negative_numeric"
    assert "token=2024" in queue[0]["expectedSignals"]

    result = _evaluate_review_truth(
        truth={
            "schemaVersion": REVIEW_TRUTH_SCHEMA_VERSION,
            "entries": [
                {
                    "sectionKey": "document",
                    "pdfPage": 1,
                    "rowNumber": None,
                    "reviewType": "negative_numeric",
                    "expected": {"rowAnchor": False},
                    "review": {"status": "confirmed"},
                }
            ],
        },
        candidate_rows=[{"sectionKey": "document", "pdfPage": 1, "rowNumber": 0}],
        records=[],
    )
    assert result["negativeNumericComplete"] is False
    assert result["negativeNumericMismatches"]
