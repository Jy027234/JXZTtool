from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import unquote
from uuid import uuid4

from .contracts import IndexAdapter, JobStore
from .models import Block, BlockType, Chunk, ParseJob, ParseJobState, ParseRequest


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Unsupported sqlite database url: {database_url!r}")
    raw_path = unquote(database_url[len(prefix):])
    return Path(raw_path)


class SQLiteJobStore(JobStore):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.database_path = _sqlite_path(database_url)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create(self, request: ParseRequest) -> ParseJob:
        now = _utc_now()
        job = ParseJob(
            job_id=f"job-{uuid4().hex[:12]}",
            doc_id=request.doc_id,
            file_path=request.file_path,
            media_type=request.media_type,
            options=dict(request.options),
            state=ParseJobState.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO parse_jobs (
                    job_id, doc_id, file_path, media_type, options_json,
                    state, created_at, updated_at, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.doc_id,
                    job.file_path,
                    job.media_type,
                    json.dumps(job.options, ensure_ascii=False),
                    job.state.value,
                    job.created_at,
                    job.updated_at,
                    job.failure_reason,
                ),
            )
        return job

    def update_state(
        self,
        *,
        job_id: str,
        state: ParseJobState,
        failure_reason: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE parse_jobs
                SET state = ?, failure_reason = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (state.value, failure_reason, now, job_id),
            )
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM blocks WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                """
                INSERT INTO blocks (doc_id, position, block_id, type, content, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        doc_id,
                        position,
                        block.block_id,
                        block.type.value,
                        block.content,
                        json.dumps(block.metadata, ensure_ascii=False),
                    )
                    for position, block in enumerate(blocks)
                ],
            )

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                """
                INSERT INTO chunks (doc_id, position, chunk_id, block_ids_json, text, language, embedding_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        doc_id,
                        position,
                        chunk.chunk_id,
                        json.dumps(chunk.block_ids, ensure_ascii=False),
                        chunk.text,
                        chunk.language,
                        json.dumps(chunk.embedding, ensure_ascii=False) if chunk.embedding is not None else None,
                    )
                    for position, chunk in enumerate(chunks)
                ],
            )

    def claim_next_job(self) -> ParseJob | None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs
                WHERE state = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (ParseJobState.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE parse_jobs
                SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ?
                """,
                (ParseJobState.PARSING.value, now, row[0], ParseJobState.PENDING.value),
            )
            if updated.rowcount == 0:
                return None
            claimed = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs
                WHERE job_id = ?
                """,
                (row[0],),
            ).fetchone()
        if claimed is None:
            return None
        return self._row_to_job(claimed)

    def get_job(self, *, job_id: str) -> ParseJob | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def get_latest_job(self, *, doc_id: str) -> ParseJob | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs
                WHERE doc_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self, *, doc_id: str | None = None) -> Sequence[ParseJob]:
        query = (
            "SELECT job_id, doc_id, file_path, media_type, options_json, "
            "state, created_at, updated_at, failure_reason, attempt_count, dead_lettered_at FROM parse_jobs"
        )
        params: tuple[str, ...] = ()
        if doc_id is not None:
            query += " WHERE doc_id = ?"
            params = (doc_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def get_blocks(self, *, doc_id: str) -> Sequence[Block]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT block_id, doc_id, type, content, metadata_json
                FROM blocks
                WHERE doc_id = ?
                ORDER BY position ASC
                """,
                (doc_id,),
            ).fetchall()
        return tuple(
            Block(
                block_id=row[0],
                doc_id=row[1],
                type=BlockType(row[2]),
                content=row[3],
                metadata=json.loads(row[4]),
            )
            for row in rows
        )

    def get_chunks(self, *, doc_id: str) -> Sequence[Chunk]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, doc_id, block_ids_json, text, language, embedding_json
                FROM chunks
                WHERE doc_id = ?
                ORDER BY position ASC
                """,
                (doc_id,),
            ).fetchall()
        return tuple(
            Chunk(
                chunk_id=row[0],
                doc_id=row[1],
                block_ids=tuple(json.loads(row[2])),
                text=row[3],
                language=row[4],
                embedding=tuple(json.loads(row[5])) if row[5] is not None else None,
            )
            for row in rows
        )

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS parse_jobs (
                    job_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    media_type TEXT,
                    options_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_parse_jobs_doc_created
                ON parse_jobs (doc_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS blocks (
                    block_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_blocks_doc_position
                ON blocks (doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    block_ids_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    embedding_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_doc_position
                ON chunks (doc_id, position ASC);
                """
            )
            # Lightweight migrations for B1 columns. SQLite cannot use
            # ``ADD COLUMN IF NOT EXISTS`` so we probe pragma first.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(parse_jobs)").fetchall()}
            if "attempt_count" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if "dead_lettered_at" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN dead_lettered_at TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> ParseJob:
        attempt_count = 0
        dead_lettered_at: str | None = None
        try:
            attempt_count = int(row["attempt_count"]) if row["attempt_count"] is not None else 0
        except (IndexError, KeyError):
            attempt_count = 0
        try:
            dead_lettered_at = row["dead_lettered_at"]
        except (IndexError, KeyError):
            dead_lettered_at = None
        return ParseJob(
            job_id=row[0],
            doc_id=row[1],
            file_path=row[2],
            media_type=row[3],
            options=json.loads(row[4]),
            state=ParseJobState(row[5]),
            created_at=row[6],
            updated_at=row[7],
            failure_reason=row[8],
            attempt_count=attempt_count,
            dead_lettered_at=dead_lettered_at,
        )


# ---------------------------------------------------------------------------
# Postgres + pgvector backends
# ---------------------------------------------------------------------------


def _normalize_postgres_url(database_url: str) -> str:
    """psycopg accepts both ``postgres://`` and ``postgresql://`` forms.

    We normalize to ``postgresql://`` to keep error messages and connection
    behavior consistent across SQLAlchemy/psycopg ecosystems.
    """

    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


class PostgresJobStore(JobStore):
    """Postgres-backed JobStore mirroring the SQLite schema.

    Connections are created per-call via psycopg with autocommit. This keeps
    the implementation simple and dependency-free (no extra pool); for high
    QPS the worker can be wrapped with ``psycopg_pool`` later without
    changing the public surface.
    """

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "PostgresJobStore requires the 'storage' extras: "
                "pip install 'parsecore-starter[storage]'"
            ) from exc
        self.database_url = _normalize_postgres_url(database_url)
        self._lock = threading.Lock()
        self._ensure_schema()

    # -- schema -----------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS parse_jobs (
                    job_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    media_type TEXT,
                    options_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_reason TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    dead_lettered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_parse_jobs_doc_created
                    ON parse_jobs (doc_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS blocks (
                    block_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_blocks_doc_position
                    ON blocks (doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    block_ids_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    embedding_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc_position
                    ON chunks (doc_id, position ASC);
                """
            )

    # -- write paths ------------------------------------------------------

    def create(self, request: ParseRequest) -> ParseJob:
        now = _utc_now()
        job = ParseJob(
            job_id=f"job-{uuid4().hex[:12]}",
            doc_id=request.doc_id,
            file_path=request.file_path,
            media_type=request.media_type,
            options=dict(request.options),
            state=ParseJobState.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO parse_jobs (
                    job_id, doc_id, file_path, media_type, options_json,
                    state, created_at, updated_at, failure_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.job_id,
                    job.doc_id,
                    job.file_path,
                    job.media_type,
                    json.dumps(job.options, ensure_ascii=False),
                    job.state.value,
                    job.created_at,
                    job.updated_at,
                    job.failure_reason,
                ),
            )
        return job

    def update_state(
        self,
        *,
        job_id: str,
        state: ParseJobState,
        failure_reason: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE parse_jobs
                SET state = %s, failure_reason = %s, updated_at = %s
                WHERE job_id = %s
                """,
                (state.value, failure_reason, now, job_id),
            )
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block]) -> None:
        rows = [
            (
                block.block_id,
                doc_id,
                position,
                block.type.value,
                block.content,
                json.dumps(block.metadata, ensure_ascii=False),
            )
            for position, block in enumerate(blocks)
        ]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM blocks WHERE doc_id = %s", (doc_id,))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO blocks (block_id, doc_id, position, type, content, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk]) -> None:
        rows = [
            (
                chunk.chunk_id,
                doc_id,
                position,
                json.dumps(chunk.block_ids, ensure_ascii=False),
                chunk.text,
                chunk.language,
                json.dumps(chunk.embedding, ensure_ascii=False)
                if chunk.embedding is not None
                else None,
            )
            for position, chunk in enumerate(chunks)
        ]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO chunks (chunk_id, doc_id, position, block_ids_json, text, language, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    # -- claim / read -----------------------------------------------------

    def claim_next_job(self) -> ParseJob | None:
        # SELECT ... FOR UPDATE SKIP LOCKED gives us safe multi-worker claim
        # semantics natively in Postgres.
        now = _utc_now()
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id FROM parse_jobs
                WHERE state = %s
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (ParseJobState.PENDING.value,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            job_id = row[0]
            cur.execute(
                """
                UPDATE parse_jobs
                SET state = %s, updated_at = %s
                WHERE job_id = %s AND state = %s
                """,
                (ParseJobState.PARSING.value, now, job_id, ParseJobState.PENDING.value),
            )
            if cur.rowcount == 0:
                return None
            cur.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs WHERE job_id = %s
                """,
                (job_id,),
            )
            claimed = cur.fetchone()
        if claimed is None:
            return None
        return self._row_to_job(claimed)

    def get_job(self, *, job_id: str) -> ParseJob | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def get_latest_job(self, *, doc_id: str) -> ParseJob | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at
                FROM parse_jobs
                WHERE doc_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (doc_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(self, *, doc_id: str | None = None) -> Sequence[ParseJob]:
        query = (
            "SELECT job_id, doc_id, file_path, media_type, options_json, "
            "state, created_at, updated_at, failure_reason, attempt_count, dead_lettered_at "
            "FROM parse_jobs"
        )
        params: tuple[Any, ...] = ()
        if doc_id is not None:
            query += " WHERE doc_id = %s"
            params = (doc_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def get_blocks(self, *, doc_id: str) -> Sequence[Block]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT block_id, doc_id, type, content, metadata_json
                FROM blocks
                WHERE doc_id = %s
                ORDER BY position ASC
                """,
                (doc_id,),
            )
            rows = cur.fetchall()
        return tuple(
            Block(
                block_id=row[0],
                doc_id=row[1],
                type=BlockType(row[2]),
                content=row[3],
                metadata=json.loads(row[4]),
            )
            for row in rows
        )

    def get_chunks(self, *, doc_id: str) -> Sequence[Chunk]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, doc_id, block_ids_json, text, language, embedding_json
                FROM chunks
                WHERE doc_id = %s
                ORDER BY position ASC
                """,
                (doc_id,),
            )
            rows = cur.fetchall()
        return tuple(
            Chunk(
                chunk_id=row[0],
                doc_id=row[1],
                block_ids=tuple(json.loads(row[2])),
                text=row[3],
                language=row[4],
                embedding=tuple(json.loads(row[5])) if row[5] is not None else None,
            )
            for row in rows
        )

    # -- helpers ----------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg

        conn = psycopg.connect(self.database_url, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row_to_job(row: Sequence[Any]) -> ParseJob:
        attempt_count = int(row[9]) if row[9] is not None else 0
        dead_lettered_at = row[10] if len(row) > 10 else None
        return ParseJob(
            job_id=row[0],
            doc_id=row[1],
            file_path=row[2],
            media_type=row[3],
            options=json.loads(row[4]),
            state=ParseJobState(row[5]),
            created_at=row[6],
            updated_at=row[7],
            failure_reason=row[8],
            attempt_count=attempt_count,
            dead_lettered_at=dead_lettered_at,
        )


class PgVectorIndex(IndexAdapter):
    """pgvector-backed index for chunk embeddings.

    The index keeps one row per chunk. Chunks without a populated embedding
    are skipped so the table only contains real vectors. The vector column
    width is set on first write per `(doc_id)` and validated on subsequent
    upserts to fail loudly on dimension drift.
    """

    def __init__(self, database_url: str, *, dim: int = 1536) -> None:
        try:
            import psycopg  # noqa: F401
            from pgvector.psycopg import register_vector  # noqa: F401
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "PgVectorIndex requires the 'storage' extras: "
                "pip install 'parsecore-starter[storage]'"
            ) from exc
        self.database_url = _normalize_postgres_url(database_url)
        self.dim = int(dim)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS chunk_embeddings (
                        chunk_id TEXT PRIMARY KEY,
                        doc_id   TEXT NOT NULL,
                        embedding vector({self.dim}) NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_doc
                        ON chunk_embeddings (doc_id);
                    """
                )

    def upsert(self, *, doc_id: str, chunks: Sequence[Chunk]) -> None:
        import psycopg
        from pgvector.psycopg import register_vector

        rows: list[tuple[str, str, list[float], str]] = []
        now = _utc_now()
        for chunk in chunks:
            if chunk.embedding is None:
                continue
            vec = list(float(v) for v in chunk.embedding)
            if len(vec) != self.dim:
                raise ValueError(
                    f"chunk {chunk.chunk_id} embedding dim={len(vec)} "
                    f"mismatch with index dim={self.dim}"
                )
            rows.append((chunk.chunk_id, doc_id, vec, now))

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chunk_embeddings WHERE doc_id = %s",
                    (doc_id,),
                )
                if rows:
                    cur.executemany(
                        """
                        INSERT INTO chunk_embeddings (chunk_id, doc_id, embedding, updated_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        rows,
                    )

    def increment_attempt(self, *, job_id: str) -> int:
        """Atomically bump the per-job attempt counter and return the new value."""

        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE parse_jobs SET attempt_count = attempt_count + 1, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            row = conn.execute(
                "SELECT attempt_count FROM parse_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def mark_dead_letter(self, *, job_id: str, reason: str) -> ParseJob:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE parse_jobs
                SET state = ?, failure_reason = ?, dead_lettered_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (ParseJobState.FAILED.value, reason, now, now, job_id),
            )
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job