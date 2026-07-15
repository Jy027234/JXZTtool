from __future__ import annotations

import json
from pathlib import Path

from tools.p0_release_readiness import build_readiness


def test_release_readiness_separates_local_and_external_gates(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "summary": {"sample_count": 1, "completed_sample_count": 1, "missing_page_count": 0},
                "gate": {"passed": True},
            }
        ),
        encoding="utf-8",
    )
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "decision": "approved",
                "requires_business_review_count": 0,
                "selected_page_count": 1,
                "approved_non_indexable_count": 1,
                "scope": "ai_assisted_review_not_business_signoff",
            }
        ),
        encoding="utf-8",
    )
    stability = tmp_path / "stability.json"
    stability.write_text(
        json.dumps(
            {
                "status": "passed",
                "quality_signature_stable": True,
                "observed_stable_runs": 3,
                "required_stable_runs": 3,
                "sample_count": 1,
                "gate": "accept_with_warning",
            }
        ),
        encoding="utf-8",
    )
    license_audit = tmp_path / "license.json"
    license_audit.write_text(json.dumps({"review_required": ["candidate"]}), encoding="utf-8")
    config = tmp_path / "parsecore.toml"
    config.write_text(
        "[providers.embedding]\nenabled = false\nprovider = 'openai-compatible'\napi_key_env = 'TEST_EMBED_KEY'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)

    result = build_readiness(
        audit_summary=audit,
        empty_review=review,
        stability=stability,
        license_audit=license_audit,
        config=config,
    )

    assert result["local_closed_count"] == 3
    assert result["required_blocker_count"] == 2
    assert result["optional_open_count"] == 1
    assert result["release_ready"] is False


def test_p0_core_scope_does_not_block_default_route_on_future_deployment_gates(tmp_path: Path, monkeypatch) -> None:
    def write(name: str, payload: object) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    audit = write(
        "audit.json",
        {"summary": {"sample_count": 1, "completed_sample_count": 1, "missing_page_count": 0}, "gate": {"passed": True}},
    )
    review = write(
        "review.json",
        {"decision": "approved", "requires_business_review_count": 0, "selected_page_count": 1},
    )
    stability = write(
        "stability.json",
        {"status": "passed", "quality_signature_stable": True, "observed_stable_runs": 3, "required_stable_runs": 3},
    )
    licenses = write("license.json", {"review_required_provider_ids": ["candidate"]})
    local_rag = write(
        "local-rag.json",
        {
            "status": "ok",
            "scope": "self_hosted_local_embedding_rag_e2e",
            "provider": "sentence-transformers-local",
            "embedded_chunk_ratio": 1.0,
            "hit_rate_at_3": 1.0,
            "mean_reciprocal_rank_at_3": 1.0,
            "index_manifest": {"present": True, "coverage_score": 1.0},
        },
    )
    config = tmp_path / "parsecore.toml"
    config.write_text(
        "[providers.embedding]\nenabled = false\nprovider = 'openai-compatible'\napi_key_env = 'TEST_EMBED_KEY'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)

    result = build_readiness(
        audit_summary=audit,
        empty_review=review,
        stability=stability,
        license_audit=licenses,
        config=config,
        local_rag=local_rag,
        scope="p0-core",
    )

    assert result["scope"] == "p0-core"
    assert result["local_closed_count"] == 4
    assert result["required_blocker_count"] == 0
    assert result["external_open_count"] == 2
    assert result["release_ready"] is True
    assert all(item["blocking"] is False for item in result["external_checks"])
