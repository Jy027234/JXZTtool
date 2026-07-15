"""Run a production-profile RAG acceptance on an external document or PDF slice.

The query suite owns the relevance budget.  A suite may be marked as a draft;
passing a draft proves the configured technical path but does not represent an
independent business approval.
"""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.pdf_parts import create_pdf_part_file  # noqa: E402
from parsecore.quality import evaluate_chunk_embeddings  # noqa: E402


_TOKEN_PATTERN = re.compile(r"[\w]+", re.UNICODE)


def _normalized_tokens(value: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(str(value or "").casefold()))


def _text_matches_any(text: str, expected_any: Sequence[str]) -> tuple[bool, str | None]:
    text_tokens = _normalized_tokens(text)
    for expected in expected_any:
        expected_tokens = _normalized_tokens(expected)
        if expected_tokens and expected_tokens.issubset(text_tokens):
            return True, expected
    return False, None


def _load_query_suite(path: str | Path) -> dict[str, Any]:
    suite_path = Path(path).resolve()
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("query_suite_must_be_an_object")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("query_suite_requires_cases")
    normalized_cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(cases, start=1):
        if not isinstance(raw_case, dict):
            raise ValueError(f"query_case_{index}_must_be_an_object")
        case_id = str(raw_case.get("id") or "").strip()
        query = str(raw_case.get("query") or "").strip()
        raw_expected = raw_case.get("expected_any")
        if not case_id or case_id in seen_ids:
            raise ValueError(f"query_case_{index}_invalid_or_duplicate_id")
        if not query:
            raise ValueError(f"query_case_{case_id}_requires_query")
        if not isinstance(raw_expected, list):
            raise ValueError(f"query_case_{case_id}_requires_expected_any")
        expected_any = [str(item).strip() for item in raw_expected if str(item).strip()]
        if not expected_any:
            raise ValueError(f"query_case_{case_id}_requires_expected_any")
        seen_ids.add(case_id)
        normalized_cases.append(
            {"id": case_id, "query": query, "expected_any": expected_any}
        )
    top_k = int(payload.get("top_k", 3))
    min_hit_rate = float(payload.get("min_hit_rate_at_k", 1.0))
    if top_k <= 0:
        raise ValueError("query_suite_top_k_must_be_positive")
    if not 0.0 <= min_hit_rate <= 1.0:
        raise ValueError("query_suite_min_hit_rate_must_be_between_zero_and_one")
    return {
        "path": str(suite_path),
        "schema_version": str(payload.get("schema_version") or ""),
        "approval_status": str(payload.get("approval_status") or "unreviewed"),
        "approved_at": str(payload.get("approved_at") or ""),
        "approved_by_role": str(payload.get("approved_by_role") or ""),
        "acceptance_standard": str(payload.get("acceptance_standard") or ""),
        "top_k": top_k,
        "min_hit_rate_at_k": min_hit_rate,
        "require_rerank": bool(payload.get("require_rerank", False)),
        "cases": normalized_cases,
    }


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _rag_manifest_summary(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        return {"present": False}
    rag = manifest.get("rag_coverage")
    if not isinstance(rag, Mapping):
        return {"present": True, "rag_coverage_present": False}
    return {
        "present": True,
        "rag_coverage_present": True,
        "indexable_unit_count": int(rag.get("indexable_unit_count") or 0),
        "chunked_unit_count": int(rag.get("chunked_unit_count") or 0),
        "embedded_chunk_count": int(rag.get("embedded_chunk_count") or 0),
        "embedded_unit_count": int(rag.get("embedded_unit_count") or 0),
        "unembedded_unit_count": int(rag.get("unembedded_unit_count") or 0),
    }


def _manifest_embedding_coverage_passed(summary: Mapping[str, Any]) -> bool:
    indexable = int(summary.get("indexable_unit_count") or 0)
    return bool(
        summary.get("present")
        and summary.get("rag_coverage_present")
        and indexable > 0
        and int(summary.get("chunked_unit_count") or 0) == indexable
        and int(summary.get("embedded_unit_count") or 0) == indexable
        and int(summary.get("unembedded_unit_count") or 0) == 0
    )


def _provider_failure_observability(runtime: Any) -> dict[str, Any]:
    events = runtime.event_aggregator.get_events(
        limit=1000,
        event_type_filter="provider_failure",
    )
    counts = Counter(
        (
            str(event.get("provider_type") or "unknown"),
            str(event.get("provider_id") or "unknown"),
            str(event.get("failure_category") or "provider_failed"),
            str(event.get("operation") or "unknown"),
        )
        for event in events
    )
    prometheus_lines = [
        line
        for line in runtime.event_aggregator.get_prometheus_metrics().splitlines()
        if line.startswith("parse_provider_failure_total{")
    ]
    return {
        "terminal_failure_count": len(events),
        "summary": [
            {
                "provider_type": provider_type,
                "provider_id": provider_id,
                "failure_category": failure_category,
                "operation": operation,
                "count": count,
            }
            for (provider_type, provider_id, failure_category, operation), count in sorted(counts.items())
        ],
        "prometheus_samples": prometheus_lines,
        "raw_errors_persisted": False,
    }


def run_acceptance(
    *,
    config: str | Path,
    document: str | Path,
    query_suite: str | Path,
    doc_id: str,
    media_type: str,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict[str, Any]:
    source_path = Path(document).resolve()
    if not source_path.is_file():
        raise ValueError("document_not_found")
    if (page_start is None) != (page_end is None):
        raise ValueError("page_range_requires_start_and_end")
    if page_start is not None and (page_start <= 0 or page_end is None or page_end < page_start):
        raise ValueError("invalid_page_range")

    suite = _load_query_suite(query_suite)
    runtime = build_runtime(Path(config).resolve())
    parse_path = source_path
    page_range = None

    with tempfile.TemporaryDirectory(prefix="parsecore-production-rag-") as tmp:
        if page_start is not None and page_end is not None:
            if media_type != "application/pdf":
                raise ValueError("page_range_requires_pdf")
            parse_path = Path(tmp) / f"{source_path.stem}-pages-{page_start}-{page_end}.pdf"
            create_pdf_part_file(str(source_path), str(parse_path), page_start, page_end)
            page_range = {"start": page_start, "end": page_end}

        outcome = runtime.submit(
            ParseRequest(
                doc_id=doc_id,
                file_path=str(parse_path),
                media_type=media_type,
                options={"source": "production-rag-acceptance", "source_page_range": page_range},
            )
        )
        embedding_quality = evaluate_chunk_embeddings(outcome.chunks)
        query_rows: list[dict[str, Any]] = []
        for case in suite["cases"]:
            hits, retrieval_mode = runtime.search_document_with_mode(
                doc_id=doc_id,
                query=case["query"],
                limit=suite["top_k"],
            )
            matched_rank = None
            matched_expected = None
            hit_rows: list[dict[str, Any]] = []
            for rank, hit in enumerate(hits, start=1):
                matched, expected = _text_matches_any(hit.text, case["expected_any"])
                if matched_rank is None and matched:
                    matched_rank = rank
                    matched_expected = expected
                hit_rows.append(
                    {
                        "rank": rank,
                        "chunk_id": hit.chunk_id,
                        "semantic_role": hit.semantic_role,
                        "score": hit.score,
                        "retrieval_score": hit.retrieval_score,
                        "rerank_score": hit.rerank_score,
                        "expected_match": matched,
                    }
                )
            query_rows.append(
                {
                    "id": case["id"],
                    "retrieval_mode": retrieval_mode,
                    "hit_count": len(hits),
                    "matched_rank": matched_rank,
                    "matched_expected": matched_expected,
                    "hits": hit_rows,
                }
            )
        document_payload = runtime.get_document(doc_id=doc_id)
        manifest = (
            document_payload.get("index_manifest")
            if isinstance(document_payload, Mapping)
            else None
        )
        provider_observability = _provider_failure_observability(runtime)

    matched_count = sum(row["matched_rank"] is not None for row in query_rows)
    hit_rate = matched_count / len(query_rows)
    reciprocal_rank = sum(
        1.0 / int(row["matched_rank"])
        for row in query_rows
        if row["matched_rank"] is not None
    ) / len(query_rows)
    all_reranked = all(
        str(row["retrieval_mode"]).endswith("+rerank") for row in query_rows
    )
    manifest_summary = _rag_manifest_summary(manifest)
    manifest_passed = _manifest_embedding_coverage_passed(manifest_summary)
    passed = bool(
        embedding_quality.embedded_chunk_ratio == 1.0
        and manifest_passed
        and hit_rate >= suite["min_hit_rate_at_k"]
        and (not suite["require_rerank"] or all_reranked)
    )
    return {
        "schema_version": "2026-07-production-rag-acceptance",
        "status": "ok" if passed else "failed",
        "scope": "external_document_production_profile",
        "approval_status": suite["approval_status"],
        "approval": {
            "approved_at": suite["approved_at"],
            "approved_by_role": suite["approved_by_role"],
            "acceptance_standard": suite["acceptance_standard"],
        },
        "config": str(Path(config).resolve()),
        "source": {
            "file_name": source_path.name,
            "sha256": _sha256_file(source_path),
            "page_range": page_range,
        },
        "doc_id": doc_id,
        "embedding": {
            "provider": runtime.settings.providers.embedding.provider,
            "model": runtime.settings.providers.embedding.model,
            "chunks": len(outcome.chunks),
            "embedded_chunks": embedding_quality.embedded_chunks,
            "embedded_chunk_ratio": round(embedding_quality.embedded_chunk_ratio, 6),
            "dimension": len(outcome.chunks[0].embedding or ()) if outcome.chunks else 0,
            "mean_dimension_norm": round(embedding_quality.mean_embedding_dim_norm, 6),
        },
        "rerank": {
            "enabled": runtime.settings.providers.rerank.enabled,
            "provider": runtime.settings.providers.rerank.provider,
            "model": runtime.settings.providers.rerank.model,
            "all_queries_reranked": all_reranked,
        },
        "provider_failure_observability": provider_observability,
        "index_manifest": manifest_summary,
        "manifest_embedding_coverage_passed": manifest_passed,
        "query_suite": {
            "path": suite["path"],
            "schema_version": suite["schema_version"],
            "top_k": suite["top_k"],
            "min_hit_rate_at_k": suite["min_hit_rate_at_k"],
            "require_rerank": suite["require_rerank"],
        },
        "query_count": len(query_rows),
        "matched_query_count": matched_count,
        "hit_rate_at_k": round(hit_rate, 6),
        "mean_reciprocal_rank_at_k": round(reciprocal_rank, 6),
        "queries": query_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--query-suite", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--media-type", default="application/pdf")
    parser.add_argument("--page-start", type=int)
    parser.add_argument("--page-end", type=int)
    parser.add_argument("--out-json")
    args = parser.parse_args(argv)
    result = run_acceptance(
        config=args.config,
        document=args.document,
        query_suite=args.query_suite,
        doc_id=args.doc_id,
        media_type=args.media_type,
        page_start=args.page_start,
        page_end=args.page_end,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out_json:
        output_path = Path(args.out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
