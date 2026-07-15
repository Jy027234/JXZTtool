from __future__ import annotations

from tools.empty_page_review import _classification, _review_row


def test_empty_page_review_classifies_pure_white_page() -> None:
    assert _classification({"visual_blank": True, "non_white_ratio": 0}) == "pure_blank_page"


def test_empty_page_review_approves_near_blank_vector_page() -> None:
    row = _review_row(
        {
            "evidence_id": "sample-p2",
            "text_chars_probe": 0,
            "image_count_probe": 0,
            "visual_blank": False,
            "non_white_ratio": 0.004,
        },
        reviewer="test",
    )
    assert row["decision"] == "approved_non_indexable"
    assert row["classification"] == "near_blank_vector_page"


def test_empty_page_review_rejects_visual_content() -> None:
    row = _review_row(
        {
            "evidence_id": "sample-p2",
            "text_chars_probe": 0,
            "image_count_probe": 0,
            "visual_blank": False,
            "non_white_ratio": 0.2,
        },
        reviewer="test",
    )
    assert row["decision"] == "requires_business_review"
