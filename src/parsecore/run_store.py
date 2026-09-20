"""Run-scoped artifact storage and atomic publication (P0-03).

Implements the contract frozen in
``docs/adr-p0-02-run-publication-contract.md``: artifacts are written into a
run-isolated staging area, sealed with a verified manifest, and only then
switched into the live read tier by a single transaction that also moves the
``active_run_id`` pointer.

The load-bearing property is: **the live tier and the active pointer change
together or not at all.**  A failure at any step leaves the previously published
version complete and readable, and leaves the new run ``unpublished``.

Scope of this first slice
-------------------------
* SQLite only.  ``postgres`` and ``memory`` advertise no run-store capabilities,
  so :func:`publication_ready` is false for them and the runtime keeps the
  legacy single-version behaviour.
* The existing inline runtime path is untouched.  Publication is driven through
  this store, and the read tier is only switched by :meth:`SQLiteRunStore.publish_run`.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, UTC
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import unquote
from uuid import uuid4

from .publication import (
    ARTIFACT_PROFILES,
    SQLITE_RUN_STORE_CAPABILITIES,
    ArtifactRequirement,
    DEFAULT_VALIDATION_POLICY,
    ExecutionStatus,
    PublicationStatus,
    RunStoreCapabilities,
    ValidationStatus,
    assert_transition,
)


RUN_STORE_SCHEMA_VERSION = "2026-09-run-store-v1"

#: Error code returned when a publish request lost a race or used stale inputs.
PUBLICATION_REJECTED_STALE = "publication_rejected_stale"

#: Policy version recorded on runs imported from pre-P0-03 artifacts.  A run
#: carrying it has no validation conclusion and may only be published through the
#: constrained ``legacy_import`` path.
LEGACY_POLICY_VERSION = "legacy-import-no-validation"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Unsupported sqlite database url: {database_url!r}")
    return Path(unquote(database_url[len(prefix):]))


def _normalize_tenant(tenant_id: str | None) -> str:
    return str(tenant_id or "").strip() or "default"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_items(items: Sequence[Any]) -> str:
    digest = sha256()
    for item in items:
        digest.update(_canonical(item).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class RunStoreError(RuntimeError):
    """Base class for run-store failures."""

    code = "run_store_error"


class RunNotFoundError(RunStoreError):
    code = "run_not_found"


class SealError(RunStoreError):
    """Artifacts are missing, inconsistent, or already sealed."""

    code = "run_seal_failed"


class PublishError(RunStoreError):
    """A publish precondition failed; the live tier was left untouched."""

    code = "run_publish_failed"


class RunNotSealableError(SealError):
    """The run is in a state whose artifacts must not be frozen."""

    code = "run_not_sealable"


class StalePublicationError(PublishError):
    """The publish request lost a race or used a stale source/generation."""

    code = PUBLICATION_REJECTED_STALE


@dataclass(slots=True, frozen=True)
class RunRecord:
    """One execution attempt."""

    parse_run_id: str
    job_id: str
    tenant_id: str
    doc_id: str
    execution_status: str
    validation_status: str
    publication_status: str
    artifact_profile: str = "default"
    base_run_id: str | None = None
    attempt_count: int = 0
    source_version: str | None = None
    source_bytes: int = 0
    profile: str = "default"
    claim_token: str | None = None
    expected_generation: int | None = None
    version_sources: Mapping[str, Any] = field(default_factory=dict)
    validation_report: Mapping[str, Any] | None = None
    manifest: Mapping[str, Any] | None = None
    sealed_at: str | None = None
    published_at: str | None = None
    withdrawn_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_STORE_SCHEMA_VERSION,
            "parse_run_id": self.parse_run_id,
            "job_id": self.job_id,
            "tenant_id": self.tenant_id,
            "doc_id": self.doc_id,
            "base_run_id": self.base_run_id,
            "attempt_count": self.attempt_count,
            "execution_status": self.execution_status,
            "validation_status": self.validation_status,
            "publication_status": self.publication_status,
            "artifact_profile": self.artifact_profile,
            "source_version": self.source_version,
            "source_bytes": self.source_bytes,
            "profile": self.profile,
            "version_sources": dict(self.version_sources),
            "validation_report": dict(self.validation_report) if self.validation_report else None,
            "manifest": dict(self.manifest) if self.manifest else None,
            "sealed_at": self.sealed_at,
            "published_at": self.published_at,
            "withdrawn_reason": self.withdrawn_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True, frozen=True)
class RunArtifacts:
    """Artifacts staged for one run.

    ``views`` is keyed by view type (``pages`` / ``lines`` / ``records``).
    """

    blocks: Sequence[Any] = ()
    chunks: Sequence[Any] = ()
    views: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SealResult:
    parse_run_id: str
    sealed_at: str
    manifest: Mapping[str, Any]
    required_artifacts: tuple[str, ...]
    degraded_artifacts: tuple[Mapping[str, Any], ...]


#: Artifact family -> the staged views that can satisfy it.  ``blocks`` is
#: satisfied by the staged block rows, ``chunks`` by the staged chunk rows.
#: ``export`` and ``assets`` have no staging surface yet, so a profile that
#: requires them stays unsealable until P0-03 stages those artifacts.
_ARTIFACT_STAGE_KEYS: Mapping[str, tuple[str, ...]] = {
    "blocks": (),
    "chunks": (),
    "reader": ("pages",),
    "records": ("records",),
    "rag": ("chunks",),
    "assets": ("assets",),
    "export": ("export",),
}


def _artifact_count(artifact: str, counts: Mapping[str, int]) -> int:
    """How many staged items satisfy ``artifact`` under a profile."""

    normalized = str(artifact or "").strip().lower()
    if normalized == "blocks":
        return int(counts.get("blocks", 0))
    if normalized == "chunks":
        return int(counts.get("chunks", 0))
    keys = _ARTIFACT_STAGE_KEYS.get(normalized, (normalized,))
    if not keys:
        return int(counts.get(normalized, 0))
    return min(int(counts.get(key, 0)) for key in keys)


@dataclass(slots=True, frozen=True)
class PublishResult:
    published: bool
    parse_run_id: str
    doc_id: str
    tenant_id: str
    active_run_id: str | None
    publication_generation: int
    publication_status: str
    superseded_run_id: str | None
    reason: str
    consumer_signal: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_STORE_SCHEMA_VERSION,
            "published": self.published,
            "parse_run_id": self.parse_run_id,
            "tenant_id": self.tenant_id,
            "doc_id": self.doc_id,
            "active_run_id": self.active_run_id,
            "publication_generation": self.publication_generation,
            "publication_status": self.publication_status,
            "superseded_run_id": self.superseded_run_id,
            "reason": self.reason,
            "consumer_signal": self.consumer_signal,
        }


def publication_ready(store: Any) -> bool:
    """Whether ``store`` can publish atomically.

    A store that cannot must not be treated as authoritative: the runtime keeps
    the legacy behaviour and the capability report says so.
    """

    if store is None:
        return False
    getter = getattr(store, "run_store_capabilities", None)
    if not callable(getter):
        return False
    try:
        capabilities = getter()
    except Exception:  # pragma: no cover - defensive
        return False
    if isinstance(capabilities, RunStoreCapabilities):
        return capabilities.strict_publication_ready
    return False


def run_store_for(database_url: str) -> Any:
    """Build the run store for a database url, or ``None`` when unsupported."""

    if str(database_url or "").startswith("sqlite:///"):
        return SQLiteRunStore(database_url)
    return None


class SQLiteRunStore:
    """SQLite implementation of the run/publication store contract."""

    def __init__(self, database_url: str) -> None:
        if not str(database_url).startswith("sqlite:///"):
            raise ValueError(
                f"SQLiteRunStore requires a sqlite database url; got {database_url!r}"
            )
        self.database_url = database_url
        self._path = _sqlite_path(database_url)
        self._ensure_schema()

    # -- plumbing ---------------------------------------------------------

    def run_store_capabilities(self) -> RunStoreCapabilities:
        return SQLITE_RUN_STORE_CAPABILITIES

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock up front, which is what makes
        the publish compare-and-set safe: a competing publisher blocks here
        instead of interleaving with the pointer switch.
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _ensure_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS parse_runs (
                    parse_run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    base_run_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    execution_status TEXT NOT NULL,
                    validation_status TEXT NOT NULL DEFAULT 'not_run',
                    publication_status TEXT NOT NULL DEFAULT 'unpublished',
                    artifact_profile TEXT NOT NULL DEFAULT 'default',
                    profile TEXT NOT NULL DEFAULT 'default',
                    source_version TEXT,
                    source_bytes INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    expected_generation INTEGER,
                    version_sources_json TEXT NOT NULL DEFAULT '{}',
                    validation_report_json TEXT,
                    manifest_json TEXT,
                    sealed_at TEXT,
                    published_at TEXT,
                    withdrawn_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_parse_runs_doc
                ON parse_runs (tenant_id, doc_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS document_publication (
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    active_run_id TEXT,
                    publication_generation INTEGER NOT NULL DEFAULT 0,
                    publication_status TEXT NOT NULL DEFAULT 'unpublished',
                    source_version TEXT,
                    profile TEXT NOT NULL DEFAULT 'default',
                    policy_version TEXT,
                    published_at TEXT,
                    withdrawn_reason TEXT,
                    PRIMARY KEY (tenant_id, doc_id)
                );

                CREATE TABLE IF NOT EXISTS publication_events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    parse_run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    publication_generation INTEGER,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    dispatched_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (tenant_id, doc_id, parse_run_id, event_type)
                );

                CREATE TABLE IF NOT EXISTS run_blocks (
                    parse_run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    block_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (parse_run_id, position)
                );

                CREATE TABLE IF NOT EXISTS run_chunks (
                    parse_run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    chunk_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (parse_run_id, position)
                );

                CREATE TABLE IF NOT EXISTS run_document_views (
                    parse_run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    view_type TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    page_start INTEGER,
                    page_end INTEGER,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (parse_run_id, view_type, position)
                );
                """
            )

    # -- run lifecycle ----------------------------------------------------

    def begin_run(
        self,
        *,
        job_id: str,
        doc_id: str,
        tenant_id: str | None = None,
        source_version: str | None = None,
        source_bytes: int = 0,
        profile: str = "default",
        artifact_profile: str = "default",
        claim_token: str | None = None,
        expected_generation: int | None = None,
        version_sources: Mapping[str, Any] | None = None,
        parse_run_id: str | None = None,
        attempt_count: int = 0,
        base_run_id: str | None = None,
    ) -> RunRecord:
        """Allocate a new run.

        A retry calls this again and gets a new ``parse_run_id``; it never
        reuses the previous attempt's identity or artifacts.
        """

        tenant = _normalize_tenant(tenant_id)
        run_id = str(parse_run_id or "").strip() or f"run-{uuid4().hex[:16]}"
        now = _utc_now()
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO parse_runs (
                    parse_run_id, job_id, tenant_id, doc_id, base_run_id, attempt_count,
                    execution_status, validation_status, publication_status,
                    artifact_profile, profile, source_version, source_bytes, claim_token,
                    expected_generation, version_sources_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(job_id),
                    tenant,
                    str(doc_id),
                    base_run_id,
                    int(attempt_count),
                    ExecutionStatus.PENDING.value,
                    ValidationStatus.NOT_RUN.value,
                    PublicationStatus.UNPUBLISHED.value,
                    str(artifact_profile or "default"),
                    str(profile or "default"),
                    source_version,
                    int(source_bytes or 0),
                    claim_token,
                    None if expected_generation is None else int(expected_generation),
                    _canonical(dict(version_sources or {})),
                    now,
                    now,
                ),
            )
            self._record_event(
                conn,
                tenant_id=tenant,
                doc_id=str(doc_id),
                parse_run_id=run_id,
                event_type="run_begun",
                payload={"job_id": str(job_id), "source_version": source_version},
            )
            return self._row_to_run(
                conn.execute(
                    "SELECT * FROM parse_runs WHERE parse_run_id = ?", (run_id,)
                ).fetchone()
            )

    def set_execution_status(
        self,
        *,
        parse_run_id: str,
        status: Any,
        claim_token: str | None = None,
    ) -> RunRecord:
        """Move ``execution_status`` using the frozen transition table."""

        with self._write_transaction() as conn:
            row = self._require_run(conn, parse_run_id)
            assert_transition("execution_status", row["execution_status"], status)
            self._assert_claim(row, claim_token)
            self._update_run(
                conn,
                parse_run_id=parse_run_id,
                execution_status=self._status_value(status),
            )
            return self._row_to_run(self._require_run(conn, parse_run_id))

    def save_run_artifacts(
        self,
        *,
        parse_run_id: str,
        artifacts: RunArtifacts,
        claim_token: str | None = None,
    ) -> dict[str, int]:
        """Write the run's artifacts into its isolated staging area.

        Staging is idempotent per run: a retry inside the same run replaces the
        staged rows instead of appending to them.
        """

        with self._write_transaction() as conn:
            row = self._require_run(conn, parse_run_id)
            self._assert_claim(row, claim_token)
            if row["sealed_at"]:
                raise SealError(
                    f"run {parse_run_id} is sealed; artifacts are immutable"
                )
            conn.execute("DELETE FROM run_blocks WHERE parse_run_id = ?", (parse_run_id,))
            conn.execute("DELETE FROM run_chunks WHERE parse_run_id = ?", (parse_run_id,))
            conn.execute(
                "DELETE FROM run_document_views WHERE parse_run_id = ?", (parse_run_id,)
            )
            for position, block in enumerate(artifacts.blocks):
                payload = _block_payload(block)
                conn.execute(
                    "INSERT INTO run_blocks (parse_run_id, tenant_id, doc_id, position, block_id, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        parse_run_id,
                        row["tenant_id"],
                        row["doc_id"],
                        position,
                        str(payload.get("block_id") or f"block:{position}"),
                        _canonical(payload),
                    ),
                )
            for position, chunk in enumerate(artifacts.chunks):
                payload = _chunk_payload(chunk)
                conn.execute(
                    "INSERT INTO run_chunks (parse_run_id, tenant_id, doc_id, position, chunk_id, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        parse_run_id,
                        row["tenant_id"],
                        row["doc_id"],
                        position,
                        str(payload.get("chunk_id") or f"chunk:{position}"),
                        _canonical(payload),
                    ),
                )
            view_counts: dict[str, int] = {}
            for view_type, items in (artifacts.views or {}).items():
                normalized_view = str(view_type or "").strip().lower()
                view_counts[normalized_view] = len(list(items))
                for position, item in enumerate(items):
                    payload = dict(item)
                    page_start, page_end = _view_page_range(payload)
                    conn.execute(
                        "INSERT INTO run_document_views ("
                        " parse_run_id, tenant_id, doc_id, view_type, position, item_id,"
                        " page_start, page_end, payload_json"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            parse_run_id,
                            row["tenant_id"],
                            row["doc_id"],
                            normalized_view,
                            position,
                            _view_item_id(normalized_view, payload, position),
                            page_start,
                            page_end,
                            _canonical(payload),
                        ),
                    )
            self._record_event(
                conn,
                tenant_id=row["tenant_id"],
                doc_id=row["doc_id"],
                parse_run_id=parse_run_id,
                event_type="artifacts_saved",
                payload={
                    "blocks": len(artifacts.blocks),
                    "chunks": len(artifacts.chunks),
                    "views": view_counts,
                },
            )
        return {
            "blocks": len(artifacts.blocks),
            "chunks": len(artifacts.chunks),
            **{f"views_{name}": count for name, count in view_counts.items()},
        }

    def seal_run(
        self,
        *,
        parse_run_id: str,
        claim_token: str | None = None,
        required_artifacts: Sequence[str] | None = None,
    ) -> SealResult:
        """Verify counts and references, then freeze the run's artifacts.

        Verification is what makes the later publish step cheap: the manifest
        recorded here is compared against the staged rows and the live tier at
        publish time.
        """

        with self._write_transaction() as conn:
            row = self._require_run(conn, parse_run_id)
            self._assert_claim(row, claim_token)
            if row["sealed_at"]:
                raise SealError(f"run {parse_run_id} is already sealed")
            # Only a run that finished executing may freeze its artifacts.  A
            # failed or cancelled run must not be sealable, because sealing is
            # the last step before publication.
            if row["execution_status"] != ExecutionStatus.SUCCEEDED.value:
                raise RunNotSealableError(
                    f"run {parse_run_id} execution_status is {row['execution_status']!r};"
                    " only a succeeded run may be sealed"
                )

            blocks = conn.execute(
                "SELECT block_id, payload_json FROM run_blocks WHERE parse_run_id = ? ORDER BY position",
                (parse_run_id,),
            ).fetchall()
            chunks = conn.execute(
                "SELECT chunk_id, payload_json FROM run_chunks WHERE parse_run_id = ? ORDER BY position",
                (parse_run_id,),
            ).fetchall()
            view_rows = conn.execute(
                "SELECT view_type, item_id, payload_json FROM run_document_views"
                " WHERE parse_run_id = ? ORDER BY view_type, position",
                (parse_run_id,),
            ).fetchall()
            view_counts: dict[str, int] = {}
            for view_row in view_rows:
                view_counts[view_row["view_type"]] = view_counts.get(view_row["view_type"], 0) + 1

            profile_name = str(row["artifact_profile"] or "default")
            profile = ARTIFACT_PROFILES.get(profile_name)
            if profile is None:
                raise SealError(f"unknown artifact profile: {profile_name!r}")

            required = tuple(required_artifacts) if required_artifacts else profile.required_artifacts()
            counts = {
                "blocks": len(blocks),
                "chunks": len(chunks),
                **{name: count for name, count in view_counts.items()},
            }
            missing_required: list[str] = []
            degraded: list[dict[str, Any]] = []
            for artifact in required:
                if _artifact_count(artifact, counts) <= 0:
                    missing_required.append(str(artifact))
            for artifact in sorted(profile.requirements):
                requirement = profile.requirement_for(artifact)
                if artifact in required or requirement is not ArtifactRequirement.OPTIONAL:
                    continue
                if _artifact_count(artifact, counts) <= 0:
                    degraded.append(
                        {
                            "artifact": artifact,
                            "reason": "artifact_empty",
                            "requirement": requirement.value,
                        }
                    )
            if missing_required:
                raise SealError(
                    "required artifacts are missing or empty: "
                    + ", ".join(sorted(missing_required))
                )

            # Referential integrity: every chunk must point at a staged block.
            block_ids = {row_item["block_id"] for row_item in blocks}
            dangling: list[str] = []
            for chunk_row in chunks:
                payload = json.loads(chunk_row["payload_json"])
                for block_id in payload.get("block_ids") or ():
                    if str(block_id) not in block_ids:
                        dangling.append(str(block_id))
            if dangling:
                raise SealError(
                    "chunks reference blocks that are not part of this run: "
                    + ", ".join(sorted(set(dangling))[:5])
                )

            manifest = {
                "schema_version": RUN_STORE_SCHEMA_VERSION,
                "parse_run_id": parse_run_id,
                "artifact_profile": profile_name,
                "counts": counts,
                "required_artifacts": list(required),
                "degraded_artifacts": degraded,
                "block_hash": _hash_items([row_item["payload_json"] for row_item in blocks]),
                "chunk_hash": _hash_items([row_item["payload_json"] for row_item in chunks]),
                "view_hash": _hash_items([row_item["payload_json"] for row_item in view_rows]),
                "source_version": row["source_version"],
            }
            sealed_at = _utc_now()
            self._update_run(
                conn,
                parse_run_id=parse_run_id,
                sealed_at=sealed_at,
                manifest_json=_canonical(manifest),
            )
            self._record_event(
                conn,
                tenant_id=row["tenant_id"],
                doc_id=row["doc_id"],
                parse_run_id=parse_run_id,
                event_type="run_sealed",
                payload=manifest,
            )
            return SealResult(
                parse_run_id=parse_run_id,
                sealed_at=sealed_at,
                manifest=manifest,
                required_artifacts=required,
                degraded_artifacts=tuple(degraded),
            )

    def record_validation(
        self,
        *,
        parse_run_id: str,
        status: Any,
        reason: str = "",
        policy_version: str | None = None,
        rule_version: str = "",
        warning_codes: Sequence[str] = (),
        metrics: Mapping[str, Any] | None = None,
        evidence_refs: Sequence[str] = (),
    ) -> RunRecord:
        """Persist the validation conclusion together with the policy version."""

        with self._write_transaction() as conn:
            row = self._require_run(conn, parse_run_id)
            target = self._status_value(status)
            assert_transition("validation_status", row["validation_status"], target)
            report = {
                "status": target,
                "reason": str(reason or ""),
                "policy_version": str(policy_version or DEFAULT_VALIDATION_POLICY.version),
                "rule_version": str(rule_version or ""),
                "warning_codes": [str(code) for code in warning_codes],
                "metrics": dict(metrics or {}),
                "evidence_refs": [str(ref) for ref in evidence_refs],
                "validated_at": _utc_now(),
            }
            self._update_run(
                conn,
                parse_run_id=parse_run_id,
                validation_status=target,
                validation_report_json=_canonical(report),
            )
            self._record_event(
                conn,
                tenant_id=row["tenant_id"],
                doc_id=row["doc_id"],
                parse_run_id=parse_run_id,
                event_type="validation_recorded",
                payload=report,
            )
            return self._row_to_run(self._require_run(conn, parse_run_id))

    # -- publication ------------------------------------------------------

    def publish_run(
        self,
        *,
        parse_run_id: str,
        expected_source_version: str | None = None,
        expected_generation: int | None = None,
        claim_token: str | None = None,
        durable_index: bool = True,
        profile: str | None = None,
        actor: str = "runtime",
        legacy_import: bool = False,
    ) -> PublishResult:
        """Atomically make ``parse_run_id`` the published version.

        The whole decision happens inside one ``BEGIN IMMEDIATE`` transaction:

        1. run state is ``succeeded`` and sealed;
        2. the recorded validation policy allows publication;
        3. the claim still belongs to this publisher (claim fencing);
        4. ``source_version`` matches the expected value;
        5. ``publication_generation`` matches the expected generation;
        6. the staged manifest still matches the staged rows;
        7. the live tier is rewritten from the staged rows, the active pointer
           moves, and the previously active run becomes ``superseded``.

        Any failure rolls back, so the previously published version stays
        complete and the new run stays ``unpublished``.

        ``legacy_import`` is the single documented exception to step 2, used by
        ``tools/legacy_snapshot_import.py`` to give artifacts that were already
        live before P0-03 an active pointer.  Such a run must carry
        ``validation_status = not_run`` and its policy version must be
        ``legacy-import-no-validation``; the flag republishes nothing that was
        ever validated or rejected, and it cannot be used to bypass a failed
        validation because a failed run is not ``not_run``.
        """

        with self._write_transaction() as conn:
            row = self._require_run(conn, parse_run_id)
            tenant = row["tenant_id"]
            doc_id = row["doc_id"]

            if row["execution_status"] != ExecutionStatus.SUCCEEDED.value:
                raise PublishError(
                    f"run {parse_run_id} execution_status is {row['execution_status']!r};"
                    " only a succeeded run may be published"
                )
            if not row["sealed_at"] or not row["manifest_json"]:
                raise PublishError(f"run {parse_run_id} is not sealed")

            self._assert_claim(row, claim_token)

            if expected_source_version is not None:
                actual = str(row["source_version"] or "")
                if str(expected_source_version) != actual:
                    raise StalePublicationError(
                        f"source_version changed: expected {expected_source_version!r},"
                        f" run has {actual!r}",
                    )

            pointer = conn.execute(
                "SELECT * FROM document_publication WHERE tenant_id = ? AND doc_id = ?",
                (tenant, doc_id),
            ).fetchone()
            current_generation = int(pointer["publication_generation"]) if pointer else 0
            if expected_generation is not None and int(expected_generation) != current_generation:
                raise StalePublicationError(
                    f"publication_generation moved: expected {expected_generation},"
                    f" current is {current_generation}",
                )

            report = (
                json.loads(row["validation_report_json"])
                if row["validation_report_json"]
                else None
            )
            policy_version = (
                str(report.get("policy_version") or "") if report else ""
            ) or DEFAULT_VALIDATION_POLICY.version
            if legacy_import:
                self._assert_legacy_import_publishable(row=row, policy_version=policy_version)
                allowed, reason = True, "legacy_import_exempt_from_validation_policy"
            else:
                allowed, reason = self._policy_for(policy_version).may_publish(
                    validation_status=row["validation_status"],
                    warning_codes=tuple(
                        str(code) for code in ((report or {}).get("warning_codes") or ())
                    ),
                    profile=str(profile or row["profile"] or "default"),
                    durable_index=durable_index,
                )
            if not allowed:
                raise PublishError(
                    f"run {parse_run_id} may not be published: {reason}"
                    f" (validation_status={row['validation_status']})"
                )

            manifest = json.loads(row["manifest_json"])
            self._verify_staged_manifest(conn, parse_run_id=parse_run_id, manifest=manifest)
            counts = self._copy_staged_to_live(
                conn,
                parse_run_id=parse_run_id,
                tenant_id=tenant,
                doc_id=doc_id,
                manifest=manifest,
            )

            superseded_run_id = (
                str(pointer["active_run_id"])
                if pointer and pointer["active_run_id"] and pointer["active_run_id"] != parse_run_id
                else None
            )
            generation = current_generation + 1
            published_at = _utc_now()

            if superseded_run_id:
                self._update_run(
                    conn,
                    parse_run_id=superseded_run_id,
                    publication_status=PublicationStatus.SUPERSEDED.value,
                )
                self._record_event(
                    conn,
                    tenant_id=tenant,
                    doc_id=doc_id,
                    parse_run_id=superseded_run_id,
                    event_type="run_superseded",
                    payload={"superseded_by": parse_run_id, "generation": generation},
                    generation=generation,
                )

            self._update_run(
                conn,
                parse_run_id=parse_run_id,
                publication_status=PublicationStatus.PUBLISHED.value,
                published_at=published_at,
            )
            conn.execute(
                """
                INSERT INTO document_publication (
                    tenant_id, doc_id, active_run_id, publication_generation,
                    publication_status, source_version, profile, policy_version, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, doc_id) DO UPDATE SET
                    active_run_id = excluded.active_run_id,
                    publication_generation = excluded.publication_generation,
                    publication_status = excluded.publication_status,
                    source_version = excluded.source_version,
                    profile = excluded.profile,
                    policy_version = excluded.policy_version,
                    published_at = excluded.published_at,
                    withdrawn_reason = NULL
                """,
                (
                    tenant,
                    doc_id,
                    parse_run_id,
                    generation,
                    PublicationStatus.PUBLISHED.value,
                    row["source_version"],
                    str(profile or row["profile"] or "default"),
                    policy_version,
                    published_at,
                ),
            )
            self._record_event(
                conn,
                tenant_id=tenant,
                doc_id=doc_id,
                parse_run_id=parse_run_id,
                event_type="run_published",
                payload={
                    "actor": str(actor),
                    "generation": generation,
                    "counts": counts,
                    "superseded_run_id": superseded_run_id,
                },
                generation=generation,
            )
            return PublishResult(
                published=True,
                parse_run_id=parse_run_id,
                tenant_id=tenant,
                doc_id=doc_id,
                active_run_id=parse_run_id,
                publication_generation=generation,
                publication_status=PublicationStatus.PUBLISHED.value,
                superseded_run_id=superseded_run_id,
                reason=reason,
                consumer_signal="published",
            )

    def get_active_run(
        self, *, doc_id: str, tenant_id: str | None = None
    ) -> RunRecord | None:
        tenant = _normalize_tenant(tenant_id)
        with self._connect() as conn:
            pointer = conn.execute(
                "SELECT active_run_id FROM document_publication WHERE tenant_id = ? AND doc_id = ?",
                (tenant, str(doc_id)),
            ).fetchone()
            if pointer is None or not pointer["active_run_id"]:
                return None
            row = conn.execute(
                "SELECT * FROM parse_runs WHERE parse_run_id = ?",
                (str(pointer["active_run_id"]),),
            ).fetchone()
            return self._row_to_run(row) if row else None

    def get_run(self, *, parse_run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM parse_runs WHERE parse_run_id = ?", (str(parse_run_id),)
            ).fetchone()
            return self._row_to_run(row) if row else None

    def list_runs(
        self, *, doc_id: str, tenant_id: str | None = None, limit: int = 50
    ) -> tuple[RunRecord, ...]:
        tenant = _normalize_tenant(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM parse_runs WHERE tenant_id = ? AND doc_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (tenant, str(doc_id), max(1, int(limit))),
            ).fetchall()
            return tuple(self._row_to_run(row) for row in rows)

    def restore_live_from_run(
        self, *, tenant_id: str | None = None, doc_id: str | None = None
    ) -> dict[str, Any]:
        """Rebuild the live read tier from the currently active run.

        This is the rollback primitive required by the design: because the
        publish transaction rewrites the live tables, a rollback must be able to
        put the active run's sealed artifacts back.  It reads only the sealed
        staging area, so it cannot resurrect a partially written document.
        """

        doc = str(doc_id or "").strip()
        if not doc:
            raise ValueError("doc_id is required")
        tenant = _normalize_tenant(tenant_id)
        with self._write_transaction() as conn:
            pointer = conn.execute(
                "SELECT * FROM document_publication WHERE tenant_id = ? AND doc_id = ?",
                (tenant, doc),
            ).fetchone()
            if pointer is None or not pointer["active_run_id"]:
                return {
                    "restored": False,
                    "reason": "no_active_run",
                    "doc_id": doc,
                    "tenant_id": tenant,
                }
            active_run_id = str(pointer["active_run_id"])
            row = self._require_run(conn, active_run_id)
            if not row["sealed_at"] or not row["manifest_json"]:
                return {
                    "restored": False,
                    "reason": "active_run_not_sealed",
                    "doc_id": doc,
                    "tenant_id": tenant,
                    "active_run_id": active_run_id,
                }
            manifest = json.loads(row["manifest_json"])
            self._verify_staged_manifest(conn, parse_run_id=active_run_id, manifest=manifest)
            counts = self._copy_staged_to_live(
                conn,
                parse_run_id=active_run_id,
                tenant_id=tenant,
                doc_id=doc,
                manifest=manifest,
            )
            return {
                "restored": True,
                "reason": "restored_from_active_run",
                "doc_id": doc,
                "tenant_id": tenant,
                "active_run_id": active_run_id,
                "counts": counts,
            }

    def publication_state(
        self, *, doc_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        """Pointer state for one document, including the "no formal version" case."""

        tenant = _normalize_tenant(tenant_id)
        with self._connect() as conn:
            pointer = conn.execute(
                "SELECT * FROM document_publication WHERE tenant_id = ? AND doc_id = ?",
                (tenant, str(doc_id)),
            ).fetchone()
            active = None
            if pointer is not None and pointer["active_run_id"]:
                active_row = conn.execute(
                    "SELECT * FROM parse_runs WHERE parse_run_id = ?",
                    (str(pointer["active_run_id"]),),
                ).fetchone()
                active = self._row_to_run(active_row) if active_row else None
        if pointer is None or active is None:
            return {
                "active_run_id": None,
                "publication_generation": int(pointer["publication_generation"]) if pointer else 0,
                "publication_status": PublicationStatus.UNPUBLISHED.value,
                "formal_version_available": False,
                "consumer_signal": "not_ready",
                "source_version": None,
                "profile": "default",
                "policy_version": None,
                "published_at": None,
                "run": None,
            }
        return {
            "active_run_id": active.parse_run_id,
            "publication_generation": int(pointer["publication_generation"]),
            "publication_status": active.publication_status,
            "formal_version_available": active.publication_status
            == PublicationStatus.PUBLISHED.value,
            "consumer_signal": (
                "published"
                if active.publication_status == PublicationStatus.PUBLISHED.value
                else "withdrawn"
                if active.publication_status == PublicationStatus.WITHDRAWN.value
                else "not_ready"
            ),
            "source_version": active.source_version,
            "profile": active.profile,
            "policy_version": pointer["policy_version"],
            "published_at": pointer["published_at"],
            "run": active,
        }

    def pending_publication_events(self, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
        """Undispatched publication events (the outbox read side)."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM publication_events WHERE dispatched_at IS NULL"
                " ORDER BY created_at ASC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return tuple(
                {
                    "event_id": row["event_id"],
                    "tenant_id": row["tenant_id"],
                    "doc_id": row["doc_id"],
                    "parse_run_id": row["parse_run_id"],
                    "event_type": row["event_type"],
                    "publication_generation": row["publication_generation"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            )

    def mark_publication_event_dispatched(self, *, event_id: str) -> bool:
        with self._write_transaction() as conn:
            cursor = conn.execute(
                "UPDATE publication_events SET dispatched_at = ? WHERE event_id = ? AND dispatched_at IS NULL",
                (_utc_now(), str(event_id)),
            )
            return cursor.rowcount > 0

    def record_publication_event(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        parse_run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        publication_generation: int | None = None,
    ) -> str:
        """Append one event, returning its id.

        The unique key is ``(tenant, doc, run, event_type)``, so a repeated
        append is ignored rather than producing a second notification.
        """

        event_id = f"evt-{uuid4().hex[:16]}"
        with self._write_transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO publication_events ("
                " event_id, tenant_id, doc_id, parse_run_id, event_type,"
                " publication_generation, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    str(tenant_id or "default"),
                    str(doc_id),
                    str(parse_run_id),
                    str(event_type),
                    publication_generation,
                    _canonical(dict(payload or {})),
                    _utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT event_id FROM publication_events"
                " WHERE tenant_id = ? AND doc_id = ? AND parse_run_id = ? AND event_type = ?",
                (
                    str(tenant_id or "default"),
                    str(doc_id),
                    str(parse_run_id),
                    str(event_type),
                ),
            ).fetchone()
            return str(row["event_id"]) if row is not None else event_id

    def dispatch_pending_publication_events(
        self,
        *,
        handler: Any,
        limit: int = 100,
        failure_retry_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Deliver pending events to ``handler`` and mark the successful ones.

        The ordering is deliberate: the handler runs **first**, and only a
        successful return marks the event dispatched.  A crash between the two
        redelivers the event (at-least-once), which is why the uniqueness key is
        ``(tenant, doc, run, event_type)`` and consumers are expected to be
        idempotent.  A handler failure leaves the event pending and records the
        attempt instead of dropping the notification.

        A ``False`` return from the handler is treated as a delivery failure:
        handlers must not report success for a notification they did not deliver.
        """

        pending = self.pending_publication_events(limit=limit)
        delivered: list[str] = []
        failed: list[dict[str, Any]] = []
        for event in pending:
            try:
                outcome = handler(dict(event))
            except Exception as exc:
                outcome = False
                error = f"{type(exc).__name__}: {exc}"
            else:
                error = ""
            if outcome:
                if self.mark_publication_event_dispatched(event_id=event["event_id"]):
                    delivered.append(event["event_id"])
                continue
            failed.append(
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "error": error or "handler_returned_false",
                }
            )
            self.record_publication_event(
                tenant_id=event["tenant_id"],
                doc_id=event["doc_id"],
                parse_run_id=event["parse_run_id"],
                event_type="publication_event_dispatch_failed",
                payload={
                    "failed_event_id": event["event_id"],
                    "failed_event_type": event["event_type"],
                    "error": error or "handler_returned_false",
                    "retry_after_seconds": float(failure_retry_seconds),
                },
                publication_generation=event.get("publication_generation"),
            )
        return {
            "schema_version": RUN_STORE_SCHEMA_VERSION,
            "pending_count": len(pending),
            "delivered_count": len(delivered),
            "failed_count": len(failed),
            "delivered_event_ids": delivered,
            "failures": failed,
            "remaining_pending": len(self.pending_publication_events(limit=limit)),
        }

    def close(self) -> None:
        return None

    # -- internals --------------------------------------------------------

    @staticmethod
    def _assert_legacy_import_publishable(*, row: Mapping[str, Any], policy_version: str) -> None:
        """Constrain the one documented bypass of the validation policy.

        A legacy snapshot may be published only when it carries no validation
        conclusion at all and declares the legacy policy.  A run that was
        validated (passed, warning, needs_review, rejected or error) can never
        take this path, so the bypass cannot republish a rejected document.
        """

        if policy_version != LEGACY_POLICY_VERSION:
            raise PublishError(
                "legacy_import publish requires the run's policy version to be "
                f"{LEGACY_POLICY_VERSION!r}; run has {policy_version!r}"
            )
        if row["validation_status"] != ValidationStatus.NOT_RUN.value:
            raise PublishError(
                "legacy_import publish requires validation_status = 'not_run'; run has "
                f"{row['validation_status']!r}"
            )

    def _policy_for(self, policy_version: str):        # P0-04 will look policies up by version. Until then only the default
        # policy exists, and an unknown version must not silently become it.
        if policy_version not in {"", DEFAULT_VALIDATION_POLICY.version, "legacy-import-no-validation"}:
            raise PublishError(f"unknown validation policy version: {policy_version!r}")
        if policy_version == LEGACY_POLICY_VERSION:
            raise PublishError(
                "legacy imported runs carry no validation conclusion and may not be "
                "published through the normal path; use the legacy snapshot import tool"
            )
        return DEFAULT_VALIDATION_POLICY

    @staticmethod
    def _status_value(status: Any) -> str:
        value = getattr(status, "value", status)
        return str(value or "").strip().lower()

    @staticmethod
    def _assert_claim(row: Mapping[str, Any], claim_token: str | None) -> None:
        if claim_token is None:
            return
        stored = row["claim_token"]
        if stored is None or str(stored) != str(claim_token):
            raise PublishError(
                "claim fencing rejected this writer: the run is owned by a different claim"
            )

    @staticmethod
    def _require_run(conn: sqlite3.Connection, parse_run_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM parse_runs WHERE parse_run_id = ?", (str(parse_run_id),)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"unknown parse_run_id: {parse_run_id!r}")
        return row

    @staticmethod
    def _update_run(conn: sqlite3.Connection, *, parse_run_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [_utc_now(), str(parse_run_id)]
        conn.execute(
            f"UPDATE parse_runs SET {assignments}, updated_at = ? WHERE parse_run_id = ?",
            values,
        )

    @staticmethod
    def _record_event(
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        doc_id: str,
        parse_run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        generation: int | None = None,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO publication_events ("
            " event_id, tenant_id, doc_id, parse_run_id, event_type,"
            " publication_generation, payload_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"evt-{uuid4().hex[:16]}",
                str(tenant_id),
                str(doc_id),
                str(parse_run_id),
                str(event_type),
                generation,
                _canonical(dict(payload)),
                _utc_now(),
            ),
        )

    @staticmethod
    def _verify_staged_manifest(
        conn: sqlite3.Connection, *, parse_run_id: str, manifest: Mapping[str, Any]
    ) -> None:
        blocks = [
            row["payload_json"]
            for row in conn.execute(
                "SELECT payload_json FROM run_blocks WHERE parse_run_id = ? ORDER BY position",
                (parse_run_id,),
            ).fetchall()
        ]
        chunks = [
            row["payload_json"]
            for row in conn.execute(
                "SELECT payload_json FROM run_chunks WHERE parse_run_id = ? ORDER BY position",
                (parse_run_id,),
            ).fetchall()
        ]
        views = [
            row["payload_json"]
            for row in conn.execute(
                "SELECT payload_json FROM run_document_views WHERE parse_run_id = ?"
                " ORDER BY view_type, position",
                (parse_run_id,),
            ).fetchall()
        ]
        counts = manifest.get("counts") or {}
        checks = (
            ("blocks", len(blocks), manifest.get("block_hash")),
            ("chunks", len(chunks), manifest.get("chunk_hash")),
        )
        for name, actual_count, expected_hash in checks:
            if int(counts.get(name, 0)) != actual_count:
                raise SealError(
                    f"staged {name} changed after sealing: manifest has"
                    f" {counts.get(name, 0)}, staging has {actual_count}"
                )
            if str(expected_hash or "") != _hash_items(
                blocks if name == "blocks" else chunks
            ):
                raise SealError(f"staged {name} content hash does not match the sealed manifest")
        expected_view_hash = str(manifest.get("view_hash") or "")
        if expected_view_hash and expected_view_hash != _hash_items(views):
            raise SealError("staged views content hash does not match the sealed manifest")

    @staticmethod
    def _copy_staged_to_live(
        conn: sqlite3.Connection,
        *,
        parse_run_id: str,
        tenant_id: str,
        doc_id: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, int]:
        """Rewrite the live read tier from the sealed staged rows.

        The live tables keep their existing primary keys, so the copy is a
        delete-then-insert inside the same transaction as the pointer switch.
        Existing readers therefore never observe a partially written document.
        """

        block_rows = conn.execute(
            "SELECT block_id, position, payload_json FROM run_blocks"
            " WHERE parse_run_id = ? ORDER BY position",
            (parse_run_id,),
        ).fetchall()
        chunk_rows = conn.execute(
            "SELECT chunk_id, position, payload_json FROM run_chunks"
            " WHERE parse_run_id = ? ORDER BY position",
            (parse_run_id,),
        ).fetchall()
        view_rows = conn.execute(
            "SELECT view_type, item_id, page_start, page_end, position, payload_json"
            " FROM run_document_views WHERE parse_run_id = ? ORDER BY view_type, position",
            (parse_run_id,),
        ).fetchall()

        conn.execute(
            "DELETE FROM blocks WHERE tenant_id = ? AND doc_id = ?", (tenant_id, doc_id)
        )
        conn.execute(
            "DELETE FROM chunks WHERE tenant_id = ? AND doc_id = ?", (tenant_id, doc_id)
        )
        conn.execute(
            "DELETE FROM document_views WHERE tenant_id = ? AND doc_id = ?",
            (tenant_id, doc_id),
        )
        # ``block_id`` / ``chunk_id`` are globally unique in the live tier while
        # readers scope by ``tenant/doc``. When an id collides across documents
        # (which a pre-P0-03 database could produce, and which synthetic part
        # documents do by construction), the colliding row is removed at the
        # moment it would break the insert, so a document that is not being
        # published keeps its artifacts. The displacement is logged as an event
        # rather than silently ignored.
        displaced: list[str] = []
        for row in block_rows:
            removed = conn.execute(
                "DELETE FROM blocks WHERE block_id = ? AND NOT (tenant_id = ? AND doc_id = ?)",
                (row["block_id"], tenant_id, doc_id),
            ).rowcount
            if removed:
                displaced.append(f"block:{row['block_id']}")
            payload = json.loads(row["payload_json"])
            conn.execute(
                "INSERT INTO blocks (block_id, doc_id, tenant_id, run_id, position, type, content, metadata_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["block_id"],
                    doc_id,
                    tenant_id,
                    parse_run_id,
                    int(row["position"]),
                    str(payload.get("type") or "paragraph"),
                    str(payload.get("content") or ""),
                    _canonical(payload.get("metadata") or {}),
                ),
            )
        for row in chunk_rows:
            removed = conn.execute(
                "DELETE FROM chunks WHERE chunk_id = ? AND NOT (tenant_id = ? AND doc_id = ?)",
                (row["chunk_id"], tenant_id, doc_id),
            ).rowcount
            if removed:
                displaced.append(f"chunk:{row['chunk_id']}")
            payload = json.loads(row["payload_json"])
            conn.execute(
                "INSERT INTO chunks ("
                " chunk_id, doc_id, tenant_id, run_id, position, block_ids_json, text,"
                " language, semantic_role, embedding_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["chunk_id"],
                    doc_id,
                    tenant_id,
                    parse_run_id,
                    int(row["position"]),
                    _canonical(list(payload.get("block_ids") or [])),
                    str(payload.get("text") or ""),
                    str(payload.get("language") or "und"),
                    str(payload.get("semantic_role") or "paragraph"),
                    _canonical(list(payload["embedding"]))
                    if payload.get("embedding") is not None
                    else None,
                ),
            )
        for row in view_rows:
            conn.execute(
                "INSERT INTO document_views ("
                " doc_id, tenant_id, view_type, position, item_id, page_start, page_end,"
                " payload_json, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    tenant_id,
                    row["view_type"],
                    int(row["position"]),
                    row["item_id"],
                    row["page_start"],
                    row["page_end"],
                    row["payload_json"],
                    _utc_now(),
                ),
            )
        if displaced:
            conn.execute(
                "INSERT OR IGNORE INTO publication_events ("
                " event_id, tenant_id, doc_id, parse_run_id, event_type,"
                " publication_generation, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"evt-{uuid4().hex[:16]}",
                    tenant_id,
                    doc_id,
                    parse_run_id,
                    "artifact_id_displaced",
                    None,
                    _canonical({"displaced": sorted(set(displaced))}),
                    _utc_now(),
                ),
            )
        return {
            "blocks": len(block_rows),
            "chunks": len(chunk_rows),
            "views": len(view_rows),
            "manifest_blocks": int((manifest.get("counts") or {}).get("blocks", 0)),
            "displaced_ids": sorted(set(displaced)),
        }

    @staticmethod
    def _row_to_run(row: sqlite3.Row | None) -> RunRecord:
        if row is None:  # pragma: no cover - guarded by _require_run
            raise RunNotFoundError("run row is missing")
        keys = set(row.keys())

        def value(name: str, default: Any = None) -> Any:
            return row[name] if name in keys else default

        return RunRecord(
            parse_run_id=str(row["parse_run_id"]),
            job_id=str(row["job_id"]),
            tenant_id=str(row["tenant_id"]),
            doc_id=str(row["doc_id"]),
            base_run_id=value("base_run_id"),
            attempt_count=int(value("attempt_count", 0) or 0),
            execution_status=str(row["execution_status"]),
            validation_status=str(row["validation_status"]),
            publication_status=str(row["publication_status"]),
            artifact_profile=str(value("artifact_profile", "default") or "default"),
            profile=str(value("profile", "default") or "default"),
            source_version=value("source_version"),
            source_bytes=int(value("source_bytes", 0) or 0),
            claim_token=value("claim_token"),
            expected_generation=value("expected_generation"),
            version_sources=json.loads(value("version_sources_json") or "{}"),
            validation_report=(
                json.loads(row["validation_report_json"])
                if value("validation_report_json")
                else None
            ),
            manifest=json.loads(row["manifest_json"]) if value("manifest_json") else None,
            sealed_at=value("sealed_at"),
            published_at=value("published_at"),
            withdrawn_reason=value("withdrawn_reason"),
            created_at=str(value("created_at", "")),
            updated_at=str(value("updated_at", "")),
        )


def _block_payload(block: Any) -> dict[str, Any]:
    if isinstance(block, Mapping):
        return dict(block)
    block_type = getattr(block, "type", "paragraph")
    return {
        "block_id": str(getattr(block, "block_id", "")),
        "doc_id": str(getattr(block, "doc_id", "")),
        "type": getattr(block_type, "value", block_type) or "paragraph",
        "content": str(getattr(block, "content", "") or ""),
        "metadata": dict(getattr(block, "metadata", None) or {}),
    }


def _chunk_payload(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, Mapping):
        return dict(chunk)
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


def _view_item_id(view_type: str, item: Mapping[str, Any], position: int) -> str:
    if view_type == "records":
        block_id = item.get("block_id")
        if block_id is None:
            source_block_ids = item.get("source_block_ids")
            if isinstance(source_block_ids, (list, tuple)):
                block_id = next(
                    (value for value in source_block_ids if str(value or "").strip()), None
                )
        record_id = str(item.get("record_id") or f"record:{position + 1}")
        if block_id is not None and str(block_id).strip():
            return f"{block_id}:record:{record_id}"
    if view_type == "pages" and item.get("page_number") is not None:
        return f"page:{item.get('page_number')}"
    if view_type == "lines" and item.get("line_id") is not None:
        return str(item.get("line_id"))
    return str(item.get("id") or f"{view_type}:{position + 1}")


def _view_page_range(item: Mapping[str, Any]) -> tuple[int | None, int | None]:
    page_start = item.get("page_start")
    page_end = item.get("page_end")
    page_number = item.get("page_number", item.get("page"))
    if page_start is None and page_number is not None:
        page_start = page_number
    if page_end is None:
        page_end = page_start
    return _optional_int(page_start), _optional_int(page_end)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "PUBLICATION_REJECTED_STALE",
    "PublishError",
    "PublishResult",
    "RUN_STORE_SCHEMA_VERSION",
    "RunArtifacts",
    "RunNotFoundError",
    "RunNotSealableError",
    "RunRecord",
    "LEGACY_POLICY_VERSION",
    "RunStoreError",
    "SQLITE_RUN_STORE_CAPABILITIES",
    "SQLiteRunStore",
    "SealError",
    "SealResult",
    "StalePublicationError",
    "publication_ready",
    "run_store_for",
]