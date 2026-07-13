"""Gold-corpus gate for ParseCore shadow provider comparisons.

This command is intentionally read-only: it executes parsers on local fixture
copies and writes a report, but it never changes provider configuration, route
priority, active artifacts, or production jobs.  A provider may only receive a
*canary recommendation* after all hard gates are met; applying that recommendation
is a separate reviewed configuration change.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.provider_comparison_report import build_report, render_markdown  # noqa: E402


SCHEMA_VERSION = "2026-07-provider-gold-evaluation"
WEIGHTS = {
    "completeness": 25,
    "reading_order": 20,
    "table_structure": 25,
    "heading_hierarchy": 15,
    "key_tokens": 10,
    "runtime_cost": 5,
}


def _compact(value: Any) -> str:
    return "".join(str(value or "").casefold().split())


def _ratio(matches: int, total: int, *, empty: float = 1.0) -> float:
    return empty if total <= 0 else max(0.0, min(1.0, matches / total))


def _expected_list(expected: Mapping[str, Any], key: str) -> list[str]:
    raw = expected.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve_imported_pages(payload: Mapping[str, Any], corpus_path: Path) -> list[dict[str, Any]]:
    pages = [dict(item) for item in payload.get("pages", []) if isinstance(item, Mapping)]
    for imported in payload.get("imports", []) or []:
        if not isinstance(imported, Mapping):
            continue
        source = str(imported.get("path") or "").strip()
        if not source:
            continue
        imported_payload = _load_json((corpus_path.parent / source).resolve())
        status = str(imported.get("review_status") or "seed")
        for sample in imported_payload.get("samples", []) or []:
            if not isinstance(sample, Mapping):
                continue
            page = {
                "id": str(sample.get("id") or ""),
                "document_id": str(sample.get("documentId") or ""),
                "page_number": int(sample.get("pageNumber") or 0),
                "title": str(sample.get("title") or ""),
                "expected": dict(sample.get("expected") or {}),
                "review_status": status,
                "source": f"import:{source}",
            }
            if page["id"] and page["document_id"] and page["page_number"] > 0:
                pages.append(page)
    return pages


def load_gold_corpus(path: str | Path) -> dict[str, Any]:
    corpus_path = Path(path).resolve()
    payload = _load_json(corpus_path)
    if not isinstance(payload, Mapping):
        raise ValueError("gold_corpus_must_be_object")
    pages = _resolve_imported_pages(payload, corpus_path)
    return {
        "schema_version": str(payload.get("schema_version") or SCHEMA_VERSION),
        "minimum_approved_pages": max(1, int(payload.get("minimum_approved_pages") or 50)),
        "minimum_stable_runs": max(1, int(payload.get("minimum_stable_runs") or 3)),
        "minimum_score_improvement": float(payload.get("minimum_score_improvement") or 5),
        "approved_provider_ids": {
            str(item) for item in payload.get("approved_provider_ids", []) or [] if str(item)
        },
        "pages": pages,
        "path": str(corpus_path),
    }


def _evidence_text(entries: Sequence[Mapping[str, Any]], *, kinds: set[str] | None = None) -> str:
    return " ".join(
        str(entry.get("text") or "")
        for entry in entries
        if kinds is None or str(entry.get("kind") or "") in kinds
    )


def _find_anchor_positions(text: str, anchors: Sequence[str]) -> list[int]:
    normalized = _compact(text)
    return [normalized.find(_compact(anchor)) for anchor in anchors if _compact(anchor)]


def score_page(
    *,
    page: Mapping[str, Any],
    provider: Mapping[str, Any],
    baseline_elapsed_s: float | None,
    approved_provider_ids: set[str],
) -> dict[str, Any]:
    expected = page.get("expected") if isinstance(page.get("expected"), Mapping) else {}
    evidence = [entry for entry in provider.get("gold_evidence", []) or [] if isinstance(entry, Mapping)]
    target_page = int(page.get("page_number") or 0)
    evidence = [entry for entry in evidence if int(entry.get("page_number") or 0) == target_page]
    provider_id = str(provider.get("provider_id") or "")
    vetoes: list[str] = []
    if str(provider.get("status") or "") != "done":
        vetoes.append("provider_not_completed")
    if not evidence:
        vetoes.append("missing_expected_page")
    positions = [int(entry.get("position") or 0) for entry in evidence]
    if positions != sorted(positions) or len(positions) != len(set(positions)):
        vetoes.append("duplicate_or_unordered_blocks")
    if any(not entry.get("provider_id") or not entry.get("source_kind") for entry in evidence):
        vetoes.append("missing_block_provenance")
    if provider_id not in approved_provider_ids:
        vetoes.append("provider_license_not_approved")

    all_text = _evidence_text(evidence)
    table_text = _evidence_text(evidence, kinds={"table"})
    heading_text = _evidence_text(evidence, kinds={"title"})
    anchors = _expected_list(expected, "anchors")
    table_anchors = _expected_list(expected, "tableAnchors")
    must_not_be_heading = _expected_list(expected, "mustNotBeHeading")
    expected_kinds = {item.casefold() for item in _expected_list(expected, "blockKinds")}
    found_kinds = {str(entry.get("kind") or "").casefold() for entry in evidence}
    anchor_hits = sum(position >= 0 for position in _find_anchor_positions(all_text, anchors))
    table_hits = sum(position >= 0 for position in _find_anchor_positions(table_text, table_anchors))
    forbidden_heading_hits = sum(position >= 0 for position in _find_anchor_positions(heading_text, must_not_be_heading))
    kind_hits = len(expected_kinds & found_kinds)
    expected_order = _expected_list(expected, "orderedAnchors")
    order_positions = _find_anchor_positions(all_text, expected_order)
    order_ok = all(position >= 0 for position in order_positions) and order_positions == sorted(order_positions)
    if table_anchors and table_hits < len(table_anchors):
        vetoes.append("table_structure_missing_anchor")
    critical_tokens = _expected_list(expected, "criticalTokens") or anchors
    critical_hits = sum(position >= 0 for position in _find_anchor_positions(all_text, critical_tokens))
    if critical_tokens and critical_hits < len(critical_tokens):
        vetoes.append("critical_token_missing")

    elapsed_s = float(provider.get("elapsed_s") or 0)
    runtime_score = (
        min(1.0, baseline_elapsed_s / elapsed_s)
        if baseline_elapsed_s and baseline_elapsed_s > 0 and elapsed_s > 0
        else 0.0
    )
    axes = {
        "completeness": _ratio(anchor_hits, len(anchors)),
        "reading_order": 1.0 if not expected_order else (1.0 if order_ok else 0.0),
        "table_structure": _ratio(table_hits, len(table_anchors)),
        "heading_hierarchy": min(
            _ratio(kind_hits, len(expected_kinds)),
            1.0 - _ratio(forbidden_heading_hits, len(must_not_be_heading), empty=0.0),
        ),
        "key_tokens": _ratio(critical_hits, len(critical_tokens)),
        "runtime_cost": runtime_score,
    }
    score = sum(WEIGHTS[axis] * value for axis, value in axes.items())
    return {
        "page_id": str(page.get("id") or ""),
        "provider_id": provider_id,
        "status": "blocked" if vetoes else "scored",
        "score": round(score, 3),
        "axes": {axis: round(value * 100, 3) for axis, value in axes.items()},
        "vetoes": list(dict.fromkeys(vetoes)),
        "evidence_block_count": len(evidence),
        "elapsed_s": elapsed_s,
    }


def evaluate_report(
    *,
    comparison: Mapping[str, Any],
    corpus: Mapping[str, Any],
    baseline_provider_id: str,
) -> dict[str, Any]:
    page_by_id = {str(page.get("id") or ""): page for page in corpus.get("pages", []) if str(page.get("id") or "")}
    approved_pages = [page for page in page_by_id.values() if str(page.get("review_status") or "") == "approved"]
    page_results: list[dict[str, Any]] = []
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in comparison.get("samples", []) or []:
        if not isinstance(sample, Mapping):
            continue
        page = page_by_id.get(str(sample.get("sample_name") or ""))
        if page is None:
            continue
        providers = [item for item in sample.get("providers", []) or [] if isinstance(item, Mapping)]
        baseline = next((item for item in providers if str(item.get("provider_id") or "") == baseline_provider_id), None)
        baseline_elapsed = float(baseline.get("elapsed_s") or 0) if baseline else None
        for provider in providers:
            result = score_page(
                page=page,
                provider=provider,
                baseline_elapsed_s=baseline_elapsed,
                approved_provider_ids=set(corpus.get("approved_provider_ids") or set()),
            )
            page_results.append(result)
            by_provider[result["provider_id"]].append(result)

    provider_summary: dict[str, Any] = {}
    for provider_id, results in sorted(by_provider.items()):
        scored = [result for result in results if result["status"] == "scored"]
        provider_summary[provider_id] = {
            "page_count": len(results),
            "scored_page_count": len(scored),
            "vetoed_page_count": len(results) - len(scored),
            "average_score": round(statistics.mean([result["score"] for result in scored]), 3) if scored else None,
            "average_axes": {
                axis: round(statistics.mean([result["axes"][axis] for result in scored]), 3) if scored else None
                for axis in WEIGHTS
            },
            "vetoes": sorted({veto for result in results for veto in result["vetoes"]}),
        }

    baseline_summary = provider_summary.get(baseline_provider_id, {})
    promotions: dict[str, Any] = {}
    approved_count = len(approved_pages)
    for provider_id, summary in provider_summary.items():
        if provider_id == baseline_provider_id:
            continue
        blockers: list[str] = []
        if approved_count < int(corpus["minimum_approved_pages"]):
            blockers.append("insufficient_human_approved_gold_pages")
        if summary["vetoed_page_count"]:
            blockers.append("hard_veto_present")
        if summary["scored_page_count"] != approved_count:
            blockers.append("incomplete_approved_gold_coverage")
        if summary["average_score"] is None or baseline_summary.get("average_score") is None:
            blockers.append("baseline_or_candidate_score_missing")
        else:
            if summary["average_score"] < baseline_summary["average_score"] + float(corpus["minimum_score_improvement"]):
                blockers.append("score_improvement_below_threshold")
            for axis in ("reading_order", "table_structure"):
                if (summary["average_axes"].get(axis) or 0) < (baseline_summary.get("average_axes", {}).get(axis) or 0):
                    blockers.append(f"{axis}_regressed")
        promotions[provider_id] = {
            "eligible_for_canary": not blockers,
            "blockers": list(dict.fromkeys(blockers)),
            "required_stable_runs": int(corpus["minimum_stable_runs"]),
            "recommendation": "manual_canary_config_review" if not blockers else "remain_shadow_only",
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "weights": WEIGHTS,
        "corpus": {
            "path": corpus.get("path"),
            "page_count": len(page_by_id),
            "approved_page_count": approved_count,
            "minimum_approved_pages": corpus["minimum_approved_pages"],
        },
        "baseline_provider_id": baseline_provider_id,
        "pages": page_results,
        "providers": provider_summary,
        "promotion": promotions,
    }


def build_gold_evaluation(
    *,
    config: str | Path,
    corpus_path: str | Path,
    source_map_path: str | Path,
    providers: Sequence[str],
    baseline_provider_id: str = "pdf-text",
    include_seed: bool = False,
    document_ids: Sequence[str] = (),
    progress: bool = False,
) -> dict[str, Any]:
    corpus = load_gold_corpus(corpus_path)
    source_map_file = Path(source_map_path).resolve()
    source_map = _load_json(source_map_file)
    if not isinstance(source_map, Mapping):
        raise ValueError("source_map_must_be_object")
    selected_pages = [
        page for page in corpus["pages"]
        if include_seed or str(page.get("review_status") or "") == "approved"
    ]
    requested_document_ids = {str(value).strip() for value in document_ids if str(value).strip()}
    if requested_document_ids:
        selected_pages = [
            page for page in selected_pages
            if str(page.get("document_id") or "") in requested_document_ids
        ]
    if not selected_pages:
        raise ValueError("no_gold_pages_selected")
    resolved_sources: dict[str, str] = {}
    missing_sources: list[str] = []
    for page in selected_pages:
        document_id = str(page["document_id"])
        raw_source = str(source_map.get(document_id) or "").strip()
        if not raw_source:
            missing_sources.append(document_id)
            continue
        source = Path(raw_source)
        if not source.is_absolute():
            source = (source_map_file.parent / source).resolve()
        if not source.exists():
            raise ValueError(f"gold_source_not_found:{document_id}:{source}")
        resolved_sources[document_id] = str(source)
    if missing_sources:
        raise ValueError(f"gold_source_map_missing:{','.join(sorted(set(missing_sources)))}")
    with TemporaryDirectory(prefix="parsecore-gold-suite-") as temp_root:
        suite_path = Path(temp_root) / "suite.json"
        suite_path.write_text(json.dumps({
            "samples": [
                {
                    "name": page["id"],
                    "path": resolved_sources[page["document_id"]],
                    "page_range": {"start": page["page_number"], "end": page["page_number"]},
                    "providers": list(dict.fromkeys([baseline_provider_id, *providers])),
                }
                for page in selected_pages
            ]
        }, ensure_ascii=False), encoding="utf-8")
        comparison = build_report(
            config=config,
            suite=suite_path,
            providers=list(dict.fromkeys([baseline_provider_id, *providers])),
            progress=progress,
        )
    evaluation = evaluate_report(comparison=comparison, corpus=corpus, baseline_provider_id=baseline_provider_id)
    return {"comparison": comparison, "gold_evaluation": evaluation}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate shadow ParseCore providers against a gold corpus")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--gold-corpus", required=True)
    parser.add_argument("--source-map", required=True, help="JSON mapping document_id to an absolute or relative source file path")
    parser.add_argument("--provider", action="append", required=True)
    parser.add_argument("--baseline-provider", default="pdf-text")
    parser.add_argument("--include-seed", action="store_true", help="Evaluate unapproved seed labels; never permits promotion")
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="Evaluate one or more document IDs from the corpus; a subset never permits promotion by itself",
    )
    parser.add_argument("--out-json")
    parser.add_argument("--progress", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    providers = [part.strip() for raw in args.provider for part in str(raw).split(",") if part.strip()]
    payload = build_gold_evaluation(
        config=args.config,
        corpus_path=args.gold_corpus,
        source_map_path=args.source_map,
        providers=providers,
        baseline_provider_id=args.baseline_provider,
        include_seed=bool(args.include_seed),
        document_ids=args.document_id,
        progress=bool(args.progress),
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"[provider-gold-evaluation] wrote {output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
