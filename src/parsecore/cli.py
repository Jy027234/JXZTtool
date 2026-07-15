from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .asgi import create_app
from .bootstrap import build_runtime
from .config import load_settings
from .models import ParseRequest
from .parts import cleanup_provider_comparison_artifacts


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

    self_check = sub.add_parser("self-check", help="Run the default ParseCore self-check gate")
    self_check.add_argument("self_check_args", nargs=argparse.REMAINDER)

    payload_contract_check = sub.add_parser(
        "payload-contract-check",
        help="Validate frozen ParseCore payload contracts",
    )
    payload_contract_check.add_argument("payload_contract_check_args", nargs=argparse.REMAINDER)

    p1_contract_acceptance = sub.add_parser(
        "p1-contract-acceptance",
        help="Run the P1 contract-freeze and host-integration acceptance gate",
    )
    p1_contract_acceptance.add_argument("p1_contract_acceptance_args", nargs=argparse.REMAINDER)

    large_pdf_stress = sub.add_parser("large-pdf-stress", help="Run the large PDF part scheduling stress tool")
    large_pdf_stress.add_argument("stress_args", nargs=argparse.REMAINDER)

    batch_reindex = sub.add_parser("batch-reindex", help="Rebuild chunk/index outputs for a batch of documents")
    batch_reindex.add_argument("--config", default="parsecore.toml")
    batch_reindex.add_argument("--tenant-id")
    batch_reindex.add_argument("--doc-id", action="append", dest="doc_ids")
    batch_reindex.add_argument("--since-hours", type=float)
    batch_reindex.add_argument("--include-embeddings", action="store_true")

    comparison_cleanup = sub.add_parser(
        "cleanup-provider-comparison-artifacts",
        help="Dry-run cleanup for self-check Provider comparison artifacts",
    )
    comparison_cleanup.add_argument("--config", default="parsecore.toml")
    comparison_cleanup.add_argument("--root", default="var/self-check")
    comparison_cleanup.add_argument("--retention-seconds", type=int)
    comparison_cleanup.add_argument(
        "--execute",
        action="store_true",
        help="remove candidates after reviewing the default dry-run report",
    )
    comparison_cleanup.add_argument("--out", default=None)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "self-check":
        from tools import self_check as self_check_tool

        return int(self_check_tool.main(raw_args[1:]))
    if raw_args and raw_args[0] == "payload-contract-check":
        from tools import payload_contract_check

        return int(payload_contract_check.main(raw_args[1:]))
    if raw_args and raw_args[0] == "p1-contract-acceptance":
        from tools import p1_contract_acceptance

        return int(p1_contract_acceptance.main(raw_args[1:]))
    if raw_args and raw_args[0] == "large-pdf-stress":
        from tools import large_pdf_stress

        return int(large_pdf_stress.main(raw_args[1:]))

    parser = _build_parser()
    args = parser.parse_args(raw_args)

    if args.command == "describe":
        runtime = build_runtime(args.config)
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
        runtime = build_runtime(args.config)
        payload = runtime.batch_reindex(
            tenant_id=args.tenant_id,
            doc_ids=args.doc_ids,
            since_hours=args.since_hours,
            include_embeddings=bool(args.include_embeddings),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "cleanup-provider-comparison-artifacts":
        settings = load_settings(args.config)
        retention_seconds = (
            args.retention_seconds
            if args.retention_seconds is not None
            else settings.runtime.provider_comparison_artifact_retention_seconds
        )
        try:
            payload = cleanup_provider_comparison_artifacts(
                args.root,
                retention_seconds=retention_seconds,
                dry_run=not bool(args.execute),
            )
        except ValueError as exc:
            parser.error(str(exc))
        payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.out:
            output_path = Path(args.out)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(payload_text + "\n", encoding="utf-8")
        print(payload_text)
        return 1 if payload["errors"] else 0

    options: dict[str, str] = {}
    if getattr(args, "mode", "default") != "default":
        options["mode"] = args.mode
    runtime = build_runtime(args.config)
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
