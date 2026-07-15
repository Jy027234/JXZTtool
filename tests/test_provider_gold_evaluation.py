from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.provider_gold_evaluation import build_gold_evaluation, evaluate_report, load_gold_corpus, score_page
from tools.provider_gold_review_queue import build_review_queue, evenly_spaced_pages


def _provider(*, provider_id: str = "shadow", table: bool = True, elapsed_s: float = 1.0) -> dict:
    evidence = [
        {
            "position": 1,
            "page_number": 1,
            "block_id": "heading-1",
            "kind": "title",
            "text": "Chapter 1 Training",
            "provider_id": provider_id,
            "source_kind": "native",
        },
        {
            "position": 2,
            "page_number": 1,
            "block_id": "paragraph-1",
            "kind": "paragraph",
            "text": "Accountable Manager approves the required training.",
            "provider_id": provider_id,
            "source_kind": "native",
        },
    ]
    if table:
        evidence.append(
            {
                "position": 3,
                "page_number": 1,
                "block_id": "table-1",
                "kind": "table",
                "text": "Course Required Hours",
                "provider_id": provider_id,
                "source_kind": "native",
            }
        )
    return {"provider_id": provider_id, "status": "done", "elapsed_s": elapsed_s, "gold_evidence": evidence}


class ProviderGoldEvaluationTests(unittest.TestCase):
    def test_gold_corpus_import_accepts_pending_snake_case_review_queue(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            queue = root / "queue.json"
            queue.write_text(json.dumps({
                "pages": [{
                    "id": "review-doc-p3",
                    "document_id": "doc",
                    "page_number": 3,
                    "review_status": "pending",
                    "review": {"reviewer": ""},
                    "expected": {"anchors": []},
                }],
            }), encoding="utf-8")
            corpus = root / "gold.json"
            corpus.write_text(json.dumps({
                "minimum_approved_pages": 50,
                "imports": [{"path": "queue.json", "review_status": "pending"}],
                "pages": [],
            }), encoding="utf-8")

            loaded = load_gold_corpus(corpus)

        self.assertEqual(len(loaded["pages"]), 1)
        self.assertEqual(loaded["pages"][0]["document_id"], "doc")
        self.assertEqual(loaded["pages"][0]["page_number"], 3)
        self.assertEqual(loaded["pages"][0]["review_status"], "pending")

    def test_review_queue_remains_pending_and_evenly_covers_each_document(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_a = root / "a.pdf"
            source_b = root / "b.pdf"
            source_a.write_bytes(b"%PDF")
            source_b.write_bytes(b"%PDF")
            source_map = root / "sources.json"
            source_map.write_text(json.dumps({"doc-a": str(source_a), "doc-b": str(source_b)}), encoding="utf-8")
            counts = {source_a: 10, source_b: 4}
            result = build_review_queue(
                source_map_path=source_map,
                pages_per_document=3,
                page_count_reader=lambda path: counts[path],
            )

        self.assertEqual(evenly_spaced_pages(10, 3), [1, 5, 10])
        self.assertEqual(evenly_spaced_pages(4, 10), [1, 2, 3, 4])
        self.assertEqual(len(result["pages"]), 6)
        self.assertEqual({entry["review_status"] for entry in result["pages"]}, {"pending"})
        self.assertTrue(all(not entry["expected"]["anchors"] for entry in result["pages"]))

    def test_document_filter_selects_a_seed_subset_without_changing_promotion_gate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "sample.txt"
            source.write_text("Accountable Manager", encoding="utf-8")
            corpus = root / "gold.json"
            corpus.write_text(json.dumps({
                "minimum_approved_pages": 50,
                "approved_provider_ids": ["text-native"],
                "pages": [
                    {
                        "id": "seed-a",
                        "document_id": "doc-a",
                        "page_number": 1,
                        "review_status": "seed",
                        "expected": {"anchors": ["Accountable Manager"]},
                    },
                    {
                        "id": "seed-b",
                        "document_id": "doc-b",
                        "page_number": 1,
                        "review_status": "seed",
                        "expected": {"anchors": ["Not selected"]},
                    },
                ],
            }), encoding="utf-8")
            source_map = root / "sources.json"
            source_map.write_text(json.dumps({"doc-a": str(source), "doc-b": str(source)}), encoding="utf-8")
            config = root / "parsecore.toml"
            config.write_text("""
[project]
name = "gold-test"
mode = "embedded-sdk"
[runtime]
execution_mode = "inline"
[storage]
database_url = "sqlite:///./gold.db"
object_store = "local://./uploads"
[index]
mode = "hybrid"
[translation]
enabled = false
strategy = "lazy"
[product]
adapter = "embedded"
[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""", encoding="utf-8")

            observed_sample_names: list[str] = []

            def fake_build_report(*, suite: Path, **_kwargs: object) -> dict:
                suite_payload = json.loads(suite.read_text(encoding="utf-8"))
                observed_sample_names.extend(item["name"] for item in suite_payload["samples"])
                return {
                    "samples": [{
                        "sample_name": "seed-a",
                        "providers": [{
                            "provider_id": "text-native",
                            "status": "done",
                            "elapsed_s": 1.0,
                            "gold_evidence": [{
                                "position": 1,
                                "page_number": 1,
                                "kind": "paragraph",
                                "text": "Accountable Manager",
                                "provider_id": "text-native",
                                "source_kind": "native",
                            }],
                        }],
                    }],
                }

            with patch("tools.provider_gold_evaluation.build_report", side_effect=fake_build_report):
                result = build_gold_evaluation(
                    config=config,
                    corpus_path=corpus,
                    source_map_path=source_map,
                    providers=["text-native"],
                    baseline_provider_id="text-native",
                    include_seed=True,
                    document_ids=["doc-a"],
                )

        self.assertEqual([item["page_id"] for item in result["gold_evaluation"]["pages"]], ["seed-a"])
        self.assertEqual(observed_sample_names, ["seed-a"])
        self.assertEqual(result["gold_evaluation"]["corpus"]["approved_page_count"], 0)

    def test_scores_traceable_provider_and_preserves_table_and_tokens(self) -> None:
        page = {
            "id": "gold-p1",
            "page_number": 1,
            "expected": {
                "blockKinds": ["title", "paragraph", "table"],
                "anchors": ["Chapter 1", "Accountable Manager"],
                "tableAnchors": ["Course", "Hours"],
                "criticalTokens": ["Accountable Manager"],
                "orderedAnchors": ["Chapter 1", "Accountable Manager"],
                "mustNotBeHeading": ["Accountable Manager"],
            },
        }
        result = score_page(
            page=page,
            provider=_provider(),
            baseline_elapsed_s=1.0,
            approved_provider_ids={"shadow"},
        )

        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["vetoes"], [])
        self.assertGreater(result["score"], 95)

    def test_table_loss_and_unapproved_license_are_hard_vetoes(self) -> None:
        result = score_page(
            page={
                "id": "gold-p1",
                "page_number": 1,
                "expected": {"tableAnchors": ["Course"], "anchors": ["Accountable Manager"]},
            },
            provider=_provider(table=False),
            baseline_elapsed_s=1.0,
            approved_provider_ids=set(),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("table_structure_missing_anchor", result["vetoes"])
        self.assertIn("provider_license_not_approved", result["vetoes"])

    def test_promotion_stays_shadow_only_until_approved_gold_coverage_exists(self) -> None:
        page = {
            "id": "gold-p1",
            "document_id": "doc-1",
            "page_number": 1,
            "review_status": "approved",
            "expected": {"anchors": ["Accountable Manager"]},
        }
        comparison = {
            "samples": [{
                "sample_name": "gold-p1",
                "providers": [_provider(provider_id="pdf-text", elapsed_s=1.0), _provider(provider_id="shadow", elapsed_s=0.5)],
            }]
        }
        corpus = {
            "pages": [page],
            "path": "gold.json",
            "minimum_approved_pages": 50,
            "minimum_stable_runs": 3,
            "minimum_score_improvement": 5,
            "approved_provider_ids": {"pdf-text", "shadow"},
        }

        result = evaluate_report(comparison=comparison, corpus=corpus, baseline_provider_id="pdf-text")

        self.assertFalse(result["promotion"]["shadow"]["eligible_for_canary"])
        self.assertIn("insufficient_human_approved_gold_pages", result["promotion"]["shadow"]["blockers"])

    def test_risk_summary_prioritizes_pending_slow_and_structurally_changed_pages(self) -> None:
        page = {
            "id": "gold-p1",
            "document_id": "doc-1",
            "page_number": 1,
            "review_status": "pending",
            "expected": {},
        }
        comparison = {
            "samples": [{
                "sample_name": "gold-p1",
                "providers": [
                    {
                        "provider_id": "pdf-text",
                        "status": "done",
                        "elapsed_s": 1.0,
                        "blocks": 2,
                        "tables": 1,
                        "provider_report": {"summary": {"total_figures": 1}},
                    },
                    {
                        "provider_id": "shadow",
                        "status": "done",
                        "elapsed_s": 2.0,
                        "blocks": 3,
                        "tables": 2,
                        "provider_report": {"summary": {"total_figures": 0}},
                    },
                ],
            }],
        }
        corpus = {
            "pages": [page],
            "path": "gold.json",
            "minimum_approved_pages": 50,
            "minimum_stable_runs": 3,
            "minimum_score_improvement": 5,
            "approved_provider_ids": {"pdf-text"},
        }

        result = evaluate_report(comparison=comparison, corpus=corpus, baseline_provider_id="pdf-text")

        risk = result["risk_summary"]
        self.assertEqual(risk["sample_count"], 1)
        self.assertEqual(risk["provider_metrics"]["shadow"]["average_elapsed_s"], 2.0)
        self.assertEqual(risk["priority_pages"][0]["page_id"], "gold-p1")
        self.assertIn("candidate_slower_than_baseline", risk["priority_pages"][0]["risk_codes"])
        self.assertIn("table_count_changed", risk["priority_pages"][0]["risk_codes"])
        self.assertIn("figure_count_changed", risk["priority_pages"][0]["risk_codes"])
