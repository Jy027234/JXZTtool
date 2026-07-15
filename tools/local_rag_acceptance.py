"""Run a deterministic self-hosted embedding -> index -> search acceptance.

This probe uses the configured local Transformer provider and a temporary
fixture document.  It proves the complete local RAG path (chunk embedding,
index manifest derivation, query embedding and semantic hit-rate) without
claiming that a remote production gateway or business corpus has passed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.quality import evaluate_chunk_embeddings  # noqa: E402


_DOCX_XML = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body>{paragraphs}</w:body>
</w:document>
"""

_CASES: tuple[dict[str, str], ...] = (
    {
        "query": "hydraulic pressure release before removal",
        "expected": "hydraulic pressure",
    },
    {
        "query": "verify line caps after maintenance",
        "expected": "line caps",
    },
    {
        "query": "record pump pressure values",
        "expected": "pump pressure",
    },
    {
        "query": "inspect the relief valve",
        "expected": "relief valve",
    },
)


def _docx_paragraph(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>"


def _create_fixture_docx(path: Path) -> Path:
    paragraphs = (
        "Hydraulic Pressure Warning Manual",
        "WARNING: Release hydraulic pressure before removal.",
        "NOTE: Verify line caps after maintenance.",
        "Procedure: inspect the pump and record pressure values.",
        "Safety: inspect the relief valve before restarting the pump.",
    )
    document_xml = _DOCX_XML.format(
        paragraphs="".join(_docx_paragraph(item) for item in paragraphs)
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return path


def _text_contains_expected(text: str, expected: str) -> bool:
    token_pattern = re.compile(r"[\w]+", re.UNICODE)
    text_tokens = set(token_pattern.findall(str(text or "").casefold()))
    expected_tokens = set(token_pattern.findall(str(expected or "").casefold()))
    return bool(expected_tokens) and expected_tokens.issubset(text_tokens)


def _rag_manifest_summary(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        return {"present": False}
    rag = manifest.get("rag_coverage")
    if not isinstance(rag, Mapping):
        return {"present": True, "rag_coverage_present": False}
    return {
        "present": True,
        "rag_coverage_present": True,
        "unit_count": int(rag.get("unit_count") or 0),
        "indexable_unit_count": int(rag.get("indexable_unit_count") or 0),
        "skipped_unit_count": int(rag.get("skipped_unit_count") or 0),
        "chunked_unit_count": int(rag.get("chunked_unit_count") or 0),
        "embedded_chunk_count": int(rag.get("embedded_chunk_count") or 0),
        "coverage_score": rag.get("coverage_score"),
        "embedded_unit_count": int(rag.get("embedded_unit_count") or 0),
        "unembedded_unit_count": int(rag.get("unembedded_unit_count") or 0),
    }


def _manifest_embedding_coverage_passed(summary: Mapping[str, Any]) -> bool:
    """Return whether every indexable unit is fully embedded in the acceptance run."""
    indexable_unit_count = int(summary.get("indexable_unit_count") or 0)
    return bool(
        summary.get("present")
        and summary.get("rag_coverage_present")
        and indexable_unit_count > 0
        and int(summary.get("chunked_unit_count") or 0) == indexable_unit_count
        and int(summary.get("embedded_unit_count") or 0) == indexable_unit_count
        and int(summary.get("unembedded_unit_count") or 0) == 0
    )


def run_acceptance(config: str | Path) -> dict[str, Any]:
    config_path = Path(config).resolve()
    runtime = build_runtime(config_path)
    settings = runtime.settings.providers.embedding
    provider_id = str(settings.provider or "").strip().lower()
    if provider_id not in {
        "sentence-transformers-local",
        "transformers-local",
        "local-transformer",
        "huggingface-local",
    }:
        return {
            "schema_version": "2026-07-local-rag-acceptance",
            "status": "failed",
            "scope": "self_hosted_local_embedding_rag_e2e",
            "reason": "configured_provider_is_not_local_transformer",
            "provider": provider_id,
            "config": str(config_path),
        }

    with tempfile.TemporaryDirectory(prefix="parsecore-local-rag-") as tmp:
        fixture = _create_fixture_docx(Path(tmp) / "local-rag-acceptance.docx")
        outcome = runtime.submit(
            ParseRequest(
                doc_id="local-rag-acceptance",
                file_path=str(fixture),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                options={"source": "local-rag-acceptance"},
            )
        )
        embedding_quality = evaluate_chunk_embeddings(outcome.chunks)
        query_rows: list[dict[str, Any]] = []
        for case in _CASES:
            hits, mode = runtime.search_document_with_mode(
                doc_id=outcome.job.doc_id,
                query=case["query"],
                limit=3,
            )
            matched_rank = next(
                (
                    index + 1
                    for index, hit in enumerate(hits)
                    if _text_contains_expected(hit.text, case["expected"])
                ),
                None,
            )
            query_rows.append(
                {
                    "query": case["query"],
                    "expected": case["expected"],
                    "retrieval_mode": mode,
                    "hit_count": len(hits),
                    "matched_rank": matched_rank,
                    "hits": [
                        {
                            "chunk_id": hit.chunk_id,
                            "score": hit.score,
                            "text": hit.text,
                        }
                        for hit in hits
                    ],
                }
            )
        document = runtime.get_document(doc_id=outcome.job.doc_id)
        manifest = document.get("index_manifest") if isinstance(document, Mapping) else None

    matched_count = sum(1 for row in query_rows if row["matched_rank"] is not None)
    hit_rate = matched_count / len(query_rows) if query_rows else 1.0
    reciprocal_rank = sum(
        1.0 / int(row["matched_rank"])
        for row in query_rows
        if row["matched_rank"] is not None
    ) / len(query_rows) if query_rows else 1.0
    manifest_summary = _rag_manifest_summary(manifest)
    manifest_embedding_coverage_passed = _manifest_embedding_coverage_passed(manifest_summary)
    status = "ok" if (
        embedding_quality.embedded_chunk_ratio == 1.0
        and hit_rate >= 0.75
        and manifest_embedding_coverage_passed
    ) else "failed"
    return {
        "schema_version": "2026-07-local-rag-acceptance",
        "status": status,
        "scope": "self_hosted_local_embedding_rag_e2e",
        "config": str(config_path),
        "provider": provider_id,
        "model": str(settings.model or ""),
        "chunks": len(outcome.chunks),
        "embedded_chunks": embedding_quality.embedded_chunks,
        "embedded_chunk_ratio": round(embedding_quality.embedded_chunk_ratio, 6),
        "embedding_dimension": len(outcome.chunks[0].embedding or ()) if outcome.chunks else 0,
        "mean_embedding_dim_norm": round(embedding_quality.mean_embedding_dim_norm, 6),
        "index_manifest": manifest_summary,
        "manifest_embedding_coverage_passed": manifest_embedding_coverage_passed,
        "query_count": len(query_rows),
        "matched_query_count": matched_count,
        "hit_rate_at_3": round(hit_rate, 6),
        "mean_reciprocal_rank_at_3": round(reciprocal_rank, 6),
        "queries": query_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out-json")
    args = parser.parse_args(argv)
    result = run_acceptance(args.config)
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
