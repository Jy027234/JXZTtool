from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from typing import Sequence

from .asgi import create_app
from .bootstrap import build_runtime
from .models import ParseRequest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parsecore")
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser("describe", help="Describe the current ParseCore runtime")
    describe.add_argument("--config", default="parsecore.toml")

    submit = sub.add_parser("submit", help="Submit a parse request to the local runtime")
    submit.add_argument("--config", default="parsecore.toml")
    submit.add_argument("--doc-id", required=True)
    submit.add_argument("--file-path", required=True)
    submit.add_argument("--media-type")
    submit.add_argument(
        "--mode",
        choices=("default", "rerun_chunks_only"),
        default="default",
    )

    serve = sub.add_parser("serve", help="Run the ASGI API server")
    serve.add_argument("--config", default="parsecore.toml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)

    worker = sub.add_parser("worker", help="Run the queue worker")
    worker.add_argument("--config", default="parsecore.toml")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--max-jobs", type=int)

    batch_reindex = sub.add_parser("batch-reindex", help="Rebuild chunk/index outputs for a batch of documents")
    batch_reindex.add_argument("--config", default="parsecore.toml")
    batch_reindex.add_argument("--tenant-id")
    batch_reindex.add_argument("--doc-id", action="append", dest="doc_ids")
    batch_reindex.add_argument("--since-hours", type=float)
    batch_reindex.add_argument("--include-embeddings", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    runtime = build_runtime(args.config)

    if args.command == "describe":
        print(json.dumps(runtime.describe(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "serve":
        import uvicorn

        app = create_app(args.config)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command == "worker":
        from .worker import run_worker

        processed = run_worker(args.config, once=args.once, max_jobs=args.max_jobs)
        print(json.dumps({"processed": processed}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "batch-reindex":
        payload = runtime.batch_reindex(
            tenant_id=args.tenant_id,
            doc_ids=args.doc_ids,
            since_hours=args.since_hours,
            include_embeddings=bool(args.include_embeddings),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    options: dict[str, str] = {}
    if getattr(args, "mode", "default") != "default":
        options["mode"] = args.mode
    request = ParseRequest(
        doc_id=args.doc_id,
        file_path=args.file_path,
        media_type=args.media_type,
        options=options,
    )
    outcome = runtime.submit(request)
    payload = {
        "job": asdict(outcome.job),
        "blocks": [asdict(item) for item in outcome.blocks],
        "chunks": [asdict(item) for item in outcome.chunks],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
