from __future__ import annotations

import json
from pathlib import Path

import pytest

from parsecore.runtime import EventAggregator
from tools.production_rag_acceptance import (
    _load_query_suite,
    _provider_failure_observability,
    _text_matches_any,
)


def test_text_matches_any_ignores_case_and_punctuation() -> None:
    matched, expected = _text_matches_any(
        "CAUTION: Use ALUMEL contact (3-142) on the conductor.",
        ["chromel contact 3-143", "alumel contact 3-142"],
    )

    assert matched is True
    assert expected == "alumel contact 3-142"


def test_query_suite_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "same", "query": "one", "expected_any": ["one"]},
                    {"id": "same", "query": "two", "expected_any": ["two"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid_or_duplicate_id"):
        _load_query_suite(path)


def test_repository_draft_query_suite_is_valid() -> None:
    suite = _load_query_suite(
        Path("fixtures/rag/safran-cmm-pages-204-206.draft.json")
    )

    assert suite["approval_status"] == "agent_derived_draft_requires_business_review"
    assert suite["top_k"] == 3
    assert suite["min_hit_rate_at_k"] == 1.0
    assert suite["require_rerank"] is True
    assert len(suite["cases"]) == 4


def test_repository_approved_query_suite_records_approval() -> None:
    suite = _load_query_suite(
        Path("fixtures/rag/safran-cmm-pages-204-206.approved.json")
    )

    assert suite["approval_status"] == "business_owner_approved"
    assert suite["approved_at"] == "2026-07-15"
    assert suite["approved_by_role"] == "project_acceptance_owner"
    assert suite["min_hit_rate_at_k"] == 1.0


def test_provider_failure_observability_persists_only_sanitized_summary() -> None:
    class RuntimeStub:
        event_aggregator = EventAggregator()

    runtime = RuntimeStub()
    runtime.event_aggregator.record_event(
        "provider_failure",
        tenant_id="tenant-secret",
        doc_id="doc-secret",
        details={
            "provider_type": "embedding",
            "provider_id": "openai-compatible",
            "failure_category": "invalid_input",
            "operation": "embed_batch",
        },
    )

    result = _provider_failure_observability(runtime)

    assert result["terminal_failure_count"] == 1
    assert result["summary"] == [
        {
            "provider_type": "embedding",
            "provider_id": "openai-compatible",
            "failure_category": "invalid_input",
            "operation": "embed_batch",
            "count": 1,
        }
    ]
    assert result["raw_errors_persisted"] is False
    assert "tenant-secret" not in json.dumps(result)
    assert "doc-secret" not in json.dumps(result)
