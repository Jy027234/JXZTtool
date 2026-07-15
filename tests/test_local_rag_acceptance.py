from __future__ import annotations

from tools.local_rag_acceptance import (
    _manifest_embedding_coverage_passed,
    _rag_manifest_summary,
    _text_contains_expected,
)


def test_local_rag_acceptance_normalizes_expected_phrase() -> None:
    assert _text_contains_expected("WARNING:  Release hydraulic pressure.", "hydraulic pressure")
    assert _text_contains_expected("inspect the pump and record pressure values", "pump pressure")
    assert not _text_contains_expected("line caps are missing", "relief valve")


def test_local_rag_acceptance_summarizes_manifest_without_assuming_shape() -> None:
    assert _rag_manifest_summary(None) == {"present": False}
    assert _rag_manifest_summary({"rag_coverage": {"unit_count": 2, "coverage_score": 1.0}}) == {
        "present": True,
        "rag_coverage_present": True,
        "unit_count": 2,
        "indexable_unit_count": 0,
        "skipped_unit_count": 0,
        "chunked_unit_count": 0,
        "embedded_chunk_count": 0,
        "coverage_score": 1.0,
        "embedded_unit_count": 0,
        "unembedded_unit_count": 0,
    }


def test_local_rag_acceptance_requires_unit_level_embedding_coverage() -> None:
    summary = _rag_manifest_summary(
        {
            "rag_coverage": {
                "unit_count": 3,
                "indexable_unit_count": 2,
                "skipped_unit_count": 1,
                "chunked_unit_count": 2,
                "embedded_chunk_count": 2,
                "embedded_unit_count": 2,
                "unembedded_unit_count": 0,
                "coverage_score": 1.0,
            }
        }
    )

    assert _manifest_embedding_coverage_passed(summary)
    assert not _manifest_embedding_coverage_passed({**summary, "embedded_unit_count": 0})
