"""Import existing single-version artifacts as explicitly unvalidated legacy runs.

Before P0-03 the live tables held one version per ``tenant/doc`` with no run
identity, so after the upgrade those documents have artifacts and no
``active_run_id``.  This tool gives each of them a legacy snapshot run and moves
the pointer to it, and it is deliberately loud about what that snapshot is:

* ``validation_status = not_run`` — no validation was ever recorded, and the
  design forbids presenting a backfilled run as ``passed``;
* ``manifest.legacy_import = true`` and ``manifest.validated = false``;
* the run's artifacts are copied from the live tier as they are, so the pointer
  and the live tier agree;
* ``--apply`` is required to write anything; the default is a dry-run report.

The operator keeps the decision: the report lists exactly which documents would
be published as legacy snapshots and why.
"""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from parsecore.config import load_settings
from parsecore.run_store import (
    LEGACY_POLICY_VERSION,
    RunArtifacts,
    publication_ready,
    run_store_for,
)
from parsecore.stores import PostgresJobStore, SQLiteJobStore


IMPORT_SCHEMA_VERSION = "2026-09-legacy-snapshot-import"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_items(items: Sequence[Any]) -> str:
    digest = sha256()
    for item in items:
        digest.update(_canonical(item).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_job_store(database_url: str):
    if database_url.startswith("sqlite:///"):
        return SQLiteJobStore(database_url)
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresJobStore(database_url)
    raise ValueError(
        f"legacy import requires a persistent database_url; got {database_url!r}"
    )


def _block_payload(block: Any) -> dict[str, Any]:
    block_type = getattr(block, "type", "paragraph")
    return {
        "block_id": str(getattr(block, "block_id", "")),
        "doc_id": str(getattr(block, "doc_id", "")),
        "type": getattr(block_type, "value", block_type) or "paragraph",
        "content": str(getattr(block, "content", "") or ""),
        "metadata": dict(getattr(block, "metadata", None) or {}),
    }


def _chunk_payload(chunk: Any) -> dict[str, Any]:
    embedding = getattr(chunk, "embedding", None)
    return {
        "chunk_id": str(getattr(chunk, "chunk_id", "")),
        "doc_id": str(getattr(chunk, "doc_id", "")),
        "block_ids": [str(item) for item in (getattr(chunk, "block_ids", ()) or ())],
        "text": str(getattr(chunk, "text", "") or ""),
        "language": str(getattr(chunk, "language", "und") or "und"),
        "semantic_role": str(getattr(chunk, "semantic_role", "paragraph") or "paragraph"),
        "embedding": list(embedding) if embedding is not None else None,
    }


def snapshot_document(*, job_store: Any, tenant_id: str, doc_id: str) -> dict[str, Any]:
    """Read the live artifacts of one document and hash them.

    The hash is what makes a migration verifiable: comparing it before and after
    the import proves the artifacts were not rewritten.
    """

    blocks = tuple(job_store.get_blocks(doc_id=doc_id, tenant_id=tenant_id))
    chunks = tuple(job_store.get_chunks(doc_id=doc_id, tenant_id=tenant_id))
    views = job_store.get_document_views(doc_id=doc_id, tenant_id=tenant_id)
    block_payloads = [_block_payload(block) for block in blocks]
    chunk_payloads = [_chunk_payload(chunk) for chunk in chunks]
    view_payloads = [
        dict(item)
        for view_type in sorted(views or {})
        for item in (views[view_type] or ())
    ]
    return {
        "doc_id": doc_id,
        "tenant_id": tenant_id,
        "block_count": len(blocks),
        "chunk_count": len(chunks),
        "view_count": len(view_payloads),
        "block_hash": _hash_items(block_payloads),
        "chunk_hash": _hash_items(chunk_payloads),
        "view_hash": _hash_items(view_payloads),
        "blocks": block_payloads,
        "chunks": chunk_payloads,
        "views": {str(key): [dict(item) for item in value] for key, value in (views or {}).items()},
    }


def _candidate_documents(*, job_store: Any, tenant_id: str | None) -> list[tuple[str, str]]:
    """``(tenant_id, doc_id)`` pairs that hold live artifacts."""

    jobs = job_store.list_jobs()
    seen: dict[tuple[str, str], None] = {}
    for job in jobs:
        job_tenant = str(getattr(job, "tenant_id", "") or "default")
        if tenant_id and job_tenant != tenant_id:
            continue
        seen[(job_tenant, str(getattr(job, "doc_id", "")))] = None
    return sorted(seen)


def import_legacy_documents(
    *,
    job_store: Any,
    run_store: Any,
    tenant_id: str | None = None,
    doc_ids: Sequence[str] | None = None,
    apply: bool = False,
    profile: str = "default",
) -> dict[str, Any]:
    """Import legacy artifacts as unvalidated snapshot runs.

    Returns a report; with ``apply=False`` (the default) nothing is written and
    ``imported_count`` stays 0 so a caller cannot mistake a dry run for a
    migration.
    """

    wanted = {str(doc).strip() for doc in (doc_ids or ()) if str(doc).strip()}
    candidates = [
        (tenant, doc)
        for tenant, doc in _candidate_documents(job_store=job_store, tenant_id=tenant_id)
        if not wanted or doc in wanted
    ]

    documents: list[dict[str, Any]] = []
    imported = 0
    skipped = 0
    for tenant, doc in candidates:
        state = run_store.publication_state(doc_id=doc, tenant_id=tenant)
        active_run_id = state.get("active_run_id")
        if active_run_id:
            skipped += 1
            documents.append(
                {
                    "doc_id": doc,
                    "tenant_id": tenant,
                    "status": "skipped_already_published",
                    "active_run_id": active_run_id,
                }
            )
            continue

        snapshot = snapshot_document(job_store=job_store, tenant_id=tenant, doc_id=doc)
        entry: dict[str, Any] = {
            "doc_id": doc,
            "tenant_id": tenant,
            "status": "would_import" if not apply else "imported",
            "block_count": snapshot["block_count"],
            "chunk_count": snapshot["chunk_count"],
            "view_count": snapshot["view_count"],
            "block_hash": snapshot["block_hash"],
            "validation_status": "not_run",
        }
        if not apply:
            documents.append(entry)
            continue

        run = run_store.begin_run(
            job_id=f"legacy-{doc}",
            doc_id=doc,
            tenant_id=tenant,
            source_version=None,
            source_bytes=0,
            profile=profile,
            artifact_profile=profile,
            version_sources={
                "engine": "legacy-import",
                "imported_from": "live_tier",
                "import_schema_version": IMPORT_SCHEMA_VERSION,
            },
        )
        run_store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        run_store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=tuple(snapshot["blocks"]),
                chunks=tuple(snapshot["chunks"]),
                views=snapshot["views"],
            ),
        )
        run_store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")
        seal = run_store.seal_run(parse_run_id=run.parse_run_id)
        # Record the honest conclusion *before* publishing: the snapshot is
        # exempt from the publish gate (it is already live) but it is never
        # presented as validated.
        run_store.record_validation(
            parse_run_id=run.parse_run_id,
            status="not_run",
            reason="legacy_import_without_validation",
            policy_version=LEGACY_POLICY_VERSION,
            rule_version=IMPORT_SCHEMA_VERSION,
            metrics={
                "block_count": snapshot["block_count"],
                "chunk_count": snapshot["chunk_count"],
                "block_hash": snapshot["block_hash"],
                "legacy_import": True,
                "validated": False,
            },
        )
        run_store.publish_run(
            parse_run_id=run.parse_run_id,
            durable_index=False,
            profile=profile,
            actor="legacy-snapshot-import",
            legacy_import=True,
        )
        imported += 1
        entry["parse_run_id"] = run.parse_run_id
        entry["manifest"] = _canonical(
            {**dict(seal.manifest), "legacy_import": True, "validated": False}
        )
        documents.append(entry)

    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "applied": bool(apply),
        "candidate_count": len(candidates),
        "imported_count": imported,
        "skipped_count": skipped,
        "documents": documents,
        "notes": [
            "imported runs carry validation_status = not_run and can never be re-published",
            "the active pointer and the live tier are copied from the same read, so they agree",
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import pre-P0-03 artifacts as unvalidated legacy snapshot runs"
    )
    parser.add_argument("--config", default="parsecore.toml")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--doc-id", action="append", dest="doc_ids")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the import; without it the command only reports what it would do",
    )
    parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(args.config)
    job_store = build_job_store(settings.database_url)
    run_store = run_store_for(settings.database_url)
    if run_store is None or not publication_ready(run_store):
        print(
            json.dumps(
                {
                    "schema_version": IMPORT_SCHEMA_VERSION,
                    "status": "unsupported_backend",
                    "database_url": settings.database_url,
                    "message": (
                        "this backend cannot publish atomically, so legacy artifacts "
                        "cannot be given an active run pointer"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    report = import_legacy_documents(
        job_store=job_store,
        run_store=run_store,
        tenant_id=args.tenant_id,
        doc_ids=args.doc_ids,
        apply=bool(args.apply),
    )
    report["database_url"] = settings.database_url
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
