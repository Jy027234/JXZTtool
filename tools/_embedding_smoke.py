"""One-shot end-to-end smoke test for the configured embedding provider.

Run:
    $env:PARSECORE_EMBEDDING_API_KEY="..."
    d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe tools/_embedding_smoke.py

Behavior:
    - loads parsecore.toml
    - forces providers.embedding.enabled = true for this run only
    - builds a temporary DOCX fixture
    - runs full runtime submit -> chunk -> embedding
    - prints a JSON summary including embedding coverage and a sample search result

If a remote API key is missing, the script prints a SKIPPED summary and exits
0.  Local Transformer providers do not require an API key.  Use
``--require-live`` to fail instead when live credentials are unavailable.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.bootstrap import build_runtime  # noqa: E402
from parsecore.embeddings import (  # noqa: E402
    EmbeddingConfigurationError,
    build_embedding_provider,
)
from parsecore.models import ParseRequest  # noqa: E402
from parsecore.quality import evaluate_chunk_embeddings  # noqa: E402


_DOCX_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{paragraphs}</w:body>
</w:document>
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ParseCore live embedding smoke test")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--out-json")
    return parser


def _emit(payload: dict[str, object], *, out_json: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_json:
        output_path = Path(out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def _docx_paragraph(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>"


def _create_fixture_docx(path: Path) -> Path:
    paragraphs = [
        "Hydraulic Pressure Warning Manual",
        "WARNING: Release hydraulic pressure before removal.",
        "NOTE: Verify line caps after maintenance.",
        "Procedure: inspect the pump and record pressure values.",
    ]
    document_xml = _DOCX_XML.format(
        paragraphs="".join(_docx_paragraph(item) for item in paragraphs)
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    runtime = build_runtime(args.config)
    provider_settings = dataclasses.replace(runtime.settings.providers.embedding, enabled=True)
    api_key_env = provider_settings.api_key_env
    remote_provider = str(provider_settings.provider or "").strip().lower() in {
        "",
        "openai-compatible",
        "openai",
        "dashscope",
        "qwen",
    }
    if remote_provider and not os.environ.get(api_key_env, "").strip():
        payload = {
            "status": "skipped",
            "reason": f"missing env var {api_key_env}",
            "config": str(args.config),
        }
        _emit(payload, out_json=args.out_json)
        return 2 if args.require_live else 0

    try:
        runtime.embedding_provider = build_embedding_provider(provider_settings)
    except EmbeddingConfigurationError as exc:
        _emit(
            {
                "status": "failed",
                "reason": str(exc),
                "config": str(args.config),
            },
            out_json=args.out_json,
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="parsecore-embedding-smoke-") as tmp:
        fixture = _create_fixture_docx(Path(tmp) / "embedding-smoke.docx")
        outcome = runtime.submit(
            ParseRequest(
                doc_id="embedding-smoke",
                file_path=str(fixture),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                options={"source": "embedding-smoke"},
            )
        )
        embedding_quality = evaluate_chunk_embeddings(outcome.chunks)
        hits, retrieval_mode = runtime.search_document_with_mode(
            doc_id="embedding-smoke",
            query="hydraulic pressure warning",
            limit=3,
            semantic_roles=["title", "warning", "note", "paragraph"],
        )

    payload = {
        "status": "ok",
        "doc_id": outcome.job.doc_id,
        "chunks": len(outcome.chunks),
        "embedded_chunks": embedding_quality.embedded_chunks,
        "embedded_chunk_ratio": round(embedding_quality.embedded_chunk_ratio, 4),
        "mean_embedding_dim_norm": round(embedding_quality.mean_embedding_dim_norm, 4),
        "embedding_dim": len(outcome.chunks[0].embedding or ()),
        "retrieval_mode": retrieval_mode,
        "search_hits": [dataclasses.asdict(hit) for hit in hits],
    }
    _emit(payload, out_json=args.out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
