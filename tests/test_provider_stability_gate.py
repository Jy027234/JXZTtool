from __future__ import annotations

from tools.provider_stability_gate import _load_json, evaluate_reports


def _report(*, changed: bool = False) -> dict:
    def provider(provider_id: str, blocks: int) -> dict:
        return {
            "provider_id": provider_id,
            "status": "done",
            "elapsed_s": 1.0 if provider_id == "pdf-text" else 0.5,
            "blocks": blocks,
            "chunks": blocks,
            "tables": 1 if provider_id == "pdf-text" else 2,
            "ir_summary": {"pages": 2, "knowledge_units": blocks},
            "coverage_summary": {
                "text_page_coverage_ratio": 1.0,
                "table_unit_coverage_ratio": 1.0,
                "unit_chunk_coverage_ratio": 1.0,
                "pages_with_coverage_gaps": 0,
                "pages_missing_chunks": 0,
                "pages_chunks_not_embedded": 0,
                "embedded_unit_count": blocks,
                "unembedded_unit_count": 0,
            },
            "rag_coverage_quality": {"score": 1.0, "gate": "accept", "flags": []},
        }

    candidate_blocks = 4 if changed else 3
    return {
        "samples": [
            {
                "sample_name": "sample-a",
                "providers": [provider("pdf-text", 3), provider("candidate", candidate_blocks)],
            }
        ]
    }


def test_three_stable_runs_pass_and_baseline_difference_is_warning() -> None:
    result = evaluate_reports(
        [_report(), _report(), _report()],
        provider_id="candidate",
        baseline_provider_id="pdf-text",
    )

    assert result["status"] == "passed"
    assert result["gate"] == "accept_with_warning"
    assert result["quality_signature_stable"] is True
    assert result["observed_stable_runs"] == 3
    assert result["baseline_differences"][0]["sample_names"] == ["sample-a"]


def test_quality_signature_change_fails() -> None:
    result = evaluate_reports(
        [_report(), _report(), _report(changed=True)],
        provider_id="candidate",
    )

    assert result["status"] == "failed"
    assert result["gate"] == "fail"
    assert "quality_signature_changed:sample-a" in result["errors"]


def test_insufficient_runs_fails() -> None:
    result = evaluate_reports([_report(), _report()], provider_id="candidate")

    assert result["status"] == "failed"
    assert "insufficient_stable_runs" in result["errors"]


def test_gold_evaluation_wrapper_can_be_unwrapped(tmp_path) -> None:
    path = tmp_path / "gold.json"
    path.write_text('{"comparison": {"samples": []}}', encoding="utf-8")

    assert _load_json(path) == {"samples": []}
