"""One-shot transport smoke test for the configured rerank provider.

This validates only the configured provider's request/response contract.  It
does not require a database, persist a document, or print query text,
candidate text, or credentials.

Run a live Alibaba Qwen check with:

    $env:PARSECORE_ALIYUN_API_KEY = "..."
    d:/个人文件/个人开发/解析管理中台/.venv/Scripts/python.exe \
        tools/_rerank_smoke.py \
        --config parsecore.pgvector.aliyun-rag.toml.example \
        --require-live
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.config import load_settings  # noqa: E402
from parsecore.rerank import (  # noqa: E402
    RerankConfigurationError,
    RerankRequestError,
    build_rerank_provider,
)


_SMOKE_QUERY = "hydraulic pressure warning inspection"
_SMOKE_DOCUMENTS = (
    "Lighting inspection procedure.",
    "WARNING: Release hydraulic pressure before maintenance.",
    "Record the inspection result after the procedure.",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ParseCore rerank provider smoke test")
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="return a non-zero code when the configured remote provider cannot run",
    )
    parser.add_argument(
        "--out-json",
        help="optional path for the credential-free smoke summary",
    )
    return parser


def _emit(payload: dict[str, Any], *, out_json: str | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_json:
        output_path = Path(out_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config_path = Path(args.config)
    try:
        settings = load_settings(config_path)
    except (OSError, ValueError) as exc:
        _emit(
            {
                "status": "failed",
                "scope": "rerank_transport_smoke",
                "config": str(config_path),
                "error_type": type(exc).__name__,
            },
            out_json=args.out_json,
        )
        return 1

    provider_settings = settings.providers.rerank
    provider_name = str(provider_settings.provider or "").strip().lower()
    local_provider = provider_name in {"fake", "test", "stub"}
    identity = {
        "provider": provider_settings.provider,
        "model": provider_settings.model,
        "candidate_count": len(_SMOKE_DOCUMENTS),
    }
    if not provider_settings.enabled:
        _emit(
            {
                "status": "skipped",
                "scope": "rerank_transport_smoke",
                "config": str(config_path),
                "reason": "providers.rerank is disabled",
                **identity,
            },
            out_json=args.out_json,
        )
        return 2 if args.require_live else 0

    if not local_provider and not os.environ.get(provider_settings.api_key_env, "").strip():
        _emit(
            {
                "status": "skipped",
                "scope": "rerank_transport_smoke",
                "config": str(config_path),
                "reason": f"missing env var {provider_settings.api_key_env}",
                **identity,
            },
            out_json=args.out_json,
        )
        return 2 if args.require_live else 0

    try:
        provider = build_rerank_provider(provider_settings)
        if provider is None:
            raise RerankConfigurationError("enabled rerank provider was not built")
        scores = tuple(
            provider.rerank(query=_SMOKE_QUERY, documents=_SMOKE_DOCUMENTS)
        )
    except (RerankConfigurationError, RerankRequestError, OSError, ValueError) as exc:
        _emit(
            {
                "status": "failed",
                "scope": "rerank_transport_smoke",
                "config": str(config_path),
                "error_type": type(exc).__name__,
                **identity,
            },
            out_json=args.out_json,
        )
        return 1

    ranked_indexes = [int(item.index) for item in scores]
    _emit(
        {
            "status": "ok",
            "scope": "rerank_transport_smoke",
            "config": str(config_path),
            "result_count": len(scores),
            "result_indexes": ranked_indexes,
            "top_index": ranked_indexes[0] if ranked_indexes else None,
            "scores": [round(float(item.score), 6) for item in scores],
            **identity,
        },
        out_json=args.out_json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
