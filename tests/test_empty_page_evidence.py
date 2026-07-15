from __future__ import annotations

from tools.empty_page_evidence import _is_empty_page_row


def test_empty_page_evidence_accepts_legacy_missing_reason() -> None:
    assert _is_empty_page_row(
        {"missing_reason": "page_without_extractable_content"}
    ) is True


def test_empty_page_evidence_accepts_explicit_empty_page_artifact() -> None:
    assert _is_empty_page_row(
        {"parsed_text_chars": 0, "quality_signal_codes": ["empty_page"]}
    ) is True


def test_empty_page_evidence_does_not_select_textual_page() -> None:
    assert _is_empty_page_row(
        {"parsed_text_chars": 12, "quality_signal_codes": ["empty_page"]}
    ) is False
