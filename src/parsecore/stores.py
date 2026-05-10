from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
from datetime import datetime, UTC, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import unquote
from uuid import uuid4

from .contracts import IndexAdapter, JobStore
from .models import Block, BlockType, Chunk, ParseJob, ParseJobState, ParseRequest
from .record_filters import collect_record_query, normalize_field_filters


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError(f"Unsupported sqlite database url: {database_url!r}")
    raw_path = unquote(database_url[len(prefix):])
    return Path(raw_path)


def _normalize_tenant_id(tenant_id: str | None) -> str:
    value = str(tenant_id or "").strip()
    return value or "default"


def _document_view_rows(
    *,
    doc_id: str,
    tenant_id: str,
    view_type: str,
    items: Sequence[Mapping[str, Any]],
    updated_at: str,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    normalized_view_type = str(view_type or "").strip().lower()
    for position, item in enumerate(items):
        payload = dict(item)
        page_start, page_end = _document_view_page_range(payload)
        rows.append(
            (
                doc_id,
                tenant_id,
                normalized_view_type,
                position,
                _document_view_item_id(
                    view_type=normalized_view_type,
                    item=payload,
                    position=position,
                ),
                page_start,
                page_end,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                updated_at,
            )
        )
    return rows


def _document_view_item_id(*, view_type: str, item: Mapping[str, Any], position: int) -> str:
    if view_type == "records":
        block_id = item.get("block_id")
        if block_id is None:
            source_block_ids = item.get("source_block_ids")
            if isinstance(source_block_ids, (list, tuple)):
                block_id = next((block_id for block_id in source_block_ids if str(block_id or "").strip()), None)
        record_id = str(item.get("record_id") or f"record:{position + 1}")
        if block_id is not None and str(block_id).strip():
            return f"{block_id}:record:{record_id}"
    candidate_keys = {
        "pages": ("page_id", "page_number"),
        "lines": ("line_id", "id"),
        "records": ("record_id", "id"),
    }.get(view_type, ("id",))
    for key in candidate_keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            if key == "page_number":
                return f"page:{value}"
            return str(value)
    return f"{view_type}:{position + 1}"


def _document_view_page_range(item: Mapping[str, Any]) -> tuple[int | None, int | None]:
    page_start = item.get("page_start")
    page_end = item.get("page_end")
    page_number = item.get("page_number", item.get("page"))
    if page_start is None and page_number is not None:
        page_start = page_number
    if page_end is None:
        page_end = page_start
    return _optional_int_value(page_start), _optional_int_value(page_end)


def _optional_int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_document_view_payloads(rows: Sequence[Sequence[Any]]) -> tuple[dict[str, Any], ...]:
    payloads: list[dict[str, Any]] = []
    for payload in _iter_document_view_payloads(rows):
        payloads.append(payload)
    return tuple(payloads)


def _iter_document_view_payloads(rows: Sequence[Sequence[Any]]) -> Iterator[dict[str, Any]]:
    for row in rows:
        payload = json.loads(row[0])
        if isinstance(payload, dict):
            yield payload


def _iter_document_view_payloads_from_cursor(cursor: Any, *, batch_size: int = 512) -> Iterator[dict[str, Any]]:
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        yield from _iter_document_view_payloads(rows)


def _record_query_requires_payload_filter(
    *,
    query: str | None,
    table_id: str | None,
    quality_signal: str | None,
    field_filters: Mapping[str, Any] | None,
) -> bool:
    return any(
        str(value or "").strip()
        for value in (query, table_id, quality_signal)
    ) or bool(normalize_field_filters(field_filters))


def _document_view_bounds(rows: Sequence[Sequence[Any]]) -> tuple[int | None, int | None]:
    starts = [row[5] for row in rows if row[5] is not None]
    ends = [row[6] for row in rows if row[6] is not None]
    if not starts and not ends:
        return None, None
    start_values = [int(value) for value in starts + ends]
    end_values = [int(value) for value in ends + starts]
    return min(start_values), max(end_values)


def _with_document_view_position(row: Sequence[Any], position: int) -> tuple[Any, ...]:
    values = list(row)
    values[3] = position
    return tuple(values)


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
            tenant_id=request.tenant_id,
            quota_key=request.quota_key,
            quota_units=max(1, int(request.quota_units or 1)),
            state=ParseJobState.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO parse_jobs (
                    job_id, doc_id, file_path, media_type, options_json,
                    tenant_id, quota_key, quota_units,
                    state, created_at, updated_at, failure_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.doc_id,
                    job.file_path,
                    job.media_type,
                    json.dumps(job.options, ensure_ascii=False),
                    job.tenant_id,
                    job.quota_key,
                    int(job.quota_units),
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
        expected_claim_token: str | None = None,
        clear_claim: bool = False,
        next_attempt_at: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        assignments = ["state = ?", "failure_reason = ?", "updated_at = ?"]
        params: list[Any] = [state.value, failure_reason, now]
        if clear_claim or next_attempt_at is not None:
            assignments.append("next_attempt_at = ?")
            params.append(next_attempt_at)
        if clear_claim:
            assignments.extend(["claim_token = NULL", "claimed_at = NULL", "lease_expires_at = NULL"])
        where = "job_id = ?"
        params.append(job_id)
        if expected_claim_token is not None:
            where += " AND claim_token = ?"
            params.append(expected_claim_token)
        with self._connect() as conn:
            updated = conn.execute(
                f"UPDATE parse_jobs SET {', '.join(assignments)} WHERE {where}",
                tuple(params),
            )
            if expected_claim_token is not None and updated.rowcount == 0:
                raise RuntimeError("stale_claim")
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block], tenant_id: str | None = None) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM blocks WHERE doc_id = ? AND tenant_id = ?",
                (doc_id, normalized_tenant),
            )
            conn.executemany(
                """
                INSERT INTO blocks (doc_id, tenant_id, position, block_id, type, content, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        doc_id,
                        normalized_tenant,
                        position,
                        block.block_id,
                        block.type.value,
                        block.content,
                        json.dumps(block.metadata, ensure_ascii=False),
                    )
                    for position, block in enumerate(blocks)
                ],
            )

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk], tenant_id: str | None = None) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE doc_id = ? AND tenant_id = ?",
                (doc_id, normalized_tenant),
            )
            conn.executemany(
                """
                INSERT INTO chunks (doc_id, tenant_id, position, chunk_id, block_ids_json, text, language, semantic_role, embedding_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        doc_id,
                        normalized_tenant,
                        position,
                        chunk.chunk_id,
                        json.dumps(chunk.block_ids, ensure_ascii=False),
                        chunk.text,
                        chunk.language,
                        chunk.semantic_role,
                        json.dumps(chunk.embedding, ensure_ascii=False) if chunk.embedding is not None else None,
                    )
                    for position, chunk in enumerate(chunks)
                ],
            )

    def save_document_views(
        self,
        *,
        doc_id: str,
        pages: Sequence[Mapping[str, Any]] = (),
        lines: Sequence[Mapping[str, Any]] = (),
        records: Sequence[Mapping[str, Any]] = (),
        tenant_id: str | None = None,
    ) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        updated_at = _utc_now()
        rows: list[tuple[Any, ...]] = []
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="pages",
                items=pages,
                updated_at=updated_at,
            )
        )
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="lines",
                items=lines,
                updated_at=updated_at,
            )
        )
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="records",
                items=records,
                updated_at=updated_at,
            )
        )
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM document_views WHERE doc_id = ? AND tenant_id = ?",
                (doc_id, normalized_tenant),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO document_views (
                        doc_id, tenant_id, view_type, position, item_id,
                        page_start, page_end, payload_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def replace_document_views_by_prefix(
        self,
        *,
        doc_id: str,
        item_id_prefix: str,
        pages: Sequence[Mapping[str, Any]] = (),
        lines: Sequence[Mapping[str, Any]] = (),
        records: Sequence[Mapping[str, Any]] = (),
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(item_id_prefix or "")
        if not prefix:
            self.save_document_views(
                doc_id=doc_id,
                pages=pages,
                lines=lines,
                records=records,
                tenant_id=tenant_id,
            )
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        updated_at = _utc_now()
        rows_by_view = {
            "pages": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="pages",
                items=pages,
                updated_at=updated_at,
            ),
            "lines": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="lines",
                items=lines,
                updated_at=updated_at,
            ),
            "records": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="records",
                items=records,
                updated_at=updated_at,
            ),
        }
        with self._connect() as conn:
            for view_type, rows in rows_by_view.items():
                self._replace_document_view_rows(
                    conn=conn,
                    doc_id=doc_id,
                    tenant_id=normalized_tenant,
                    view_type=view_type,
                    item_id_prefix=prefix,
                    rows=rows,
                )

    def get_document_views(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> Mapping[str, tuple[dict[str, Any], ...]]:
        return {
            "pages": self.get_document_pages(doc_id=doc_id, tenant_id=tenant_id),
            "lines": self.get_document_lines(doc_id=doc_id, tenant_id=tenant_id),
            "records": self.get_document_records(doc_id=doc_id, tenant_id=tenant_id),
        }

    def get_document_pages(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="pages")

    def get_document_lines(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="lines")

    def get_document_records(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="records")

    def query_document_records(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
        query: str | None = None,
        table_id: str | None = None,
        quality_signal: str | None = None,
        field_filters: Mapping[str, Any] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> Mapping[str, Any]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        start = _optional_int_value(page_start)
        end = _optional_int_value(page_end)
        if start is not None and end is not None and start > end:
            raise ValueError("invalid_page_range")

        where = ["doc_id = ?", "tenant_id = ?", "view_type = ?"]
        params: list[Any] = [doc_id, normalized_tenant, "records"]
        if start is not None:
            where.append("COALESCE(page_end, page_start, 1) >= ?")
            params.append(start)
        if end is not None:
            where.append("COALESCE(page_start, page_end, 1) <= ?")
            params.append(end)
        where_sql = " AND ".join(where)

        payload_filter = _record_query_requires_payload_filter(
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=field_filters,
        )
        normalized_limit = None if limit is None else max(1, int(limit))
        normalized_offset = max(0, int(offset or 0))

        with self._connect() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM document_views WHERE {where_sql}",
                tuple(params),
            ).fetchone()
            candidate_count = int(count_row[0] if count_row is not None else 0)
            if candidate_count <= 0:
                return {
                    "items": (),
                    "total": 0,
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                    "persisted": False,
                }

            select_sql = (
                "SELECT payload_json FROM document_views "
                f"WHERE {where_sql} "
                "ORDER BY COALESCE(page_start, 0) ASC, position ASC"
            )
            select_params = list(params)
            if not payload_filter:
                if normalized_limit is not None:
                    select_sql += " LIMIT ? OFFSET ?"
                    select_params.extend([normalized_limit, normalized_offset])
                elif normalized_offset:
                    select_sql += " LIMIT -1 OFFSET ?"
                    select_params.append(normalized_offset)
                rows = conn.execute(select_sql, tuple(select_params)).fetchall()
                return {
                    "items": _load_document_view_payloads(rows),
                    "total": candidate_count,
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                    "persisted": True,
                }

            cursor = conn.execute(select_sql, tuple(select_params))
            result = collect_record_query(
                _iter_document_view_payloads_from_cursor(cursor),
                limit=normalized_limit,
                offset=normalized_offset,
                query=query,
                table_id=table_id,
                quality_signal=quality_signal,
                field_filters=field_filters,
                page_start=start,
                page_end=end,
            )
        result["persisted"] = True
        return result

    def _get_document_view_items(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        view_type: str,
    ) -> tuple[dict[str, Any], ...]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_view_type = str(view_type or "").strip().lower()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM document_views
                WHERE doc_id = ? AND tenant_id = ? AND view_type = ?
                ORDER BY COALESCE(page_start, 0) ASC, position ASC
                """,
                (doc_id, normalized_tenant, normalized_view_type),
            ).fetchall()
        return _load_document_view_payloads(rows)

    def _replace_document_view_rows(
        self,
        *,
        conn: sqlite3.Connection,
        doc_id: str,
        tenant_id: str,
        view_type: str,
        item_id_prefix: str,
        rows: Sequence[Sequence[Any]],
    ) -> None:
        page_start, page_end = _document_view_bounds(rows)
        params: list[Any] = [doc_id, tenant_id, view_type, len(item_id_prefix), item_id_prefix]
        range_clause = ""
        if page_start is not None and page_end is not None:
            range_clause = " OR (page_start IS NOT NULL AND page_end IS NOT NULL AND page_start <= ? AND page_end >= ?)"
            params.extend([page_end, page_start])
        row = conn.execute(
            f"""
            SELECT MIN(position), MAX(position), COUNT(*)
            FROM document_views
            WHERE doc_id = ? AND tenant_id = ? AND view_type = ?
              AND (substr(item_id, 1, ?) = ?{range_clause})
            """,
            tuple(params),
        ).fetchone()
        min_position = row[0] if row is not None else None
        old_count = int(row[2] or 0) if row is not None else 0
        conn.execute(
            f"""
            DELETE FROM document_views
            WHERE doc_id = ? AND tenant_id = ? AND view_type = ?
              AND (substr(item_id, 1, ?) = ?{range_clause})
            """,
            tuple(params),
        )
        if not rows:
            return
        if min_position is None:
            max_row = conn.execute(
                """
                SELECT MAX(position)
                FROM document_views
                WHERE doc_id = ? AND tenant_id = ? AND view_type = ?
                """,
                (doc_id, tenant_id, view_type),
            ).fetchone()
            insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
        else:
            insert_at = int(min_position)
        delta = len(rows) - old_count
        if delta and min_position is not None:
            conn.execute(
                """
                UPDATE document_views
                SET position = position + ?
                WHERE doc_id = ? AND tenant_id = ? AND view_type = ? AND position > ?
                """,
                (delta, doc_id, tenant_id, view_type, int(min_position) + old_count - 1),
            )
        conn.executemany(
            """
            INSERT INTO document_views (
                doc_id, tenant_id, view_type, position, item_id,
                page_start, page_end, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_with_document_view_position(row, insert_at + index) for index, row in enumerate(rows)],
        )

    def replace_blocks_by_prefix(
        self,
        *,
        doc_id: str,
        blocks: Sequence[Block],
        block_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(block_id_prefix or "")
        if not prefix:
            self.save_blocks(doc_id=doc_id, blocks=blocks, tenant_id=tenant_id)
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(position), MAX(position), COUNT(*)
                FROM blocks
                WHERE doc_id = ? AND tenant_id = ? AND substr(block_id, 1, ?) = ?
                """,
                (doc_id, normalized_tenant, len(prefix), prefix),
            ).fetchone()
            min_position = row[0] if row is not None else None
            max_position = row[1] if row is not None else None
            old_count = int(row[2] or 0) if row is not None else 0
            if min_position is None:
                max_row = conn.execute(
                    "SELECT MAX(position) FROM blocks WHERE doc_id = ? AND tenant_id = ?",
                    (doc_id, normalized_tenant),
                ).fetchone()
                insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
            else:
                insert_at = int(min_position)
            conn.execute(
                "DELETE FROM blocks WHERE doc_id = ? AND tenant_id = ? AND substr(block_id, 1, ?) = ?",
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            delta = len(blocks) - old_count
            if delta and max_position is not None:
                conn.execute(
                    """
                    UPDATE blocks
                    SET position = position + ?
                    WHERE doc_id = ? AND tenant_id = ? AND position > ?
                    """,
                    (delta, doc_id, normalized_tenant, int(max_position)),
                )
            if blocks:
                conn.executemany(
                    """
                    INSERT INTO blocks (doc_id, tenant_id, position, block_id, type, content, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            doc_id,
                            normalized_tenant,
                            insert_at + position,
                            block.block_id,
                            block.type.value,
                            block.content,
                            json.dumps(block.metadata, ensure_ascii=False),
                        )
                        for position, block in enumerate(blocks)
                    ],
                )

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(chunk_id_prefix or "")
        if not prefix:
            self.save_chunks(doc_id=doc_id, chunks=chunks, tenant_id=tenant_id)
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(position), MAX(position), COUNT(*)
                FROM chunks
                WHERE doc_id = ? AND tenant_id = ? AND substr(chunk_id, 1, ?) = ?
                """,
                (doc_id, normalized_tenant, len(prefix), prefix),
            ).fetchone()
            min_position = row[0] if row is not None else None
            max_position = row[1] if row is not None else None
            old_count = int(row[2] or 0) if row is not None else 0
            if min_position is None:
                max_row = conn.execute(
                    "SELECT MAX(position) FROM chunks WHERE doc_id = ? AND tenant_id = ?",
                    (doc_id, normalized_tenant),
                ).fetchone()
                insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
            else:
                insert_at = int(min_position)
            conn.execute(
                "DELETE FROM chunks WHERE doc_id = ? AND tenant_id = ? AND substr(chunk_id, 1, ?) = ?",
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            delta = len(chunks) - old_count
            if delta and max_position is not None:
                conn.execute(
                    """
                    UPDATE chunks
                    SET position = position + ?
                    WHERE doc_id = ? AND tenant_id = ? AND position > ?
                    """,
                    (delta, doc_id, normalized_tenant, int(max_position)),
                )
            if chunks:
                conn.executemany(
                    """
                    INSERT INTO chunks (doc_id, tenant_id, position, chunk_id, block_ids_json, text, language, semantic_role, embedding_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            doc_id,
                            normalized_tenant,
                            insert_at + position,
                            chunk.chunk_id,
                            json.dumps(chunk.block_ids, ensure_ascii=False),
                            chunk.text,
                            chunk.language,
                            chunk.semantic_role,
                            json.dumps(chunk.embedding, ensure_ascii=False) if chunk.embedding is not None else None,
                        )
                        for position, chunk in enumerate(chunks)
                    ],
                )

    def claim_next_job(self) -> ParseJob | None:
        now = _utc_now()
        claim_token = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT job_id
                FROM parse_jobs
                WHERE state = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (ParseJobState.PENDING.value, now),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """
                UPDATE parse_jobs
                SET state = ?, updated_at = ?, claimed_at = ?, lease_expires_at = NULL,
                    next_attempt_at = NULL, claim_token = ?, failure_reason = NULL,
                    attempt_count = attempt_count + 1
                WHERE job_id = ? AND state = ?
                """,
                (ParseJobState.PARSING.value, now, now, claim_token, row[0], ParseJobState.PENDING.value),
            )
            if updated.rowcount == 0:
                return None
            claimed = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
                FROM parse_jobs
                WHERE job_id = ?
                """,
                (row[0],),
            ).fetchone()
        if claimed is None:
            return None
        return self._row_to_job(claimed)

    def claim_job(self, *, job_id: str, lease_expires_at: str | None = None) -> ParseJob | None:
        now = _utc_now()
        claim_token = uuid4().hex
        return self._claim_job(job_id=job_id, now=now, claim_token=claim_token, lease_expires_at=lease_expires_at)

    def _claim_job(
        self,
        *,
        job_id: str,
        now: str,
        claim_token: str,
        lease_expires_at: str | None,
    ) -> ParseJob | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE parse_jobs
                SET state = ?, updated_at = ?, claimed_at = ?, lease_expires_at = ?,
                    next_attempt_at = NULL, claim_token = ?, failure_reason = NULL,
                    attempt_count = attempt_count + 1
                WHERE job_id = ? AND state = ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """,
                (
                    ParseJobState.PARSING.value,
                    now,
                    now,
                    lease_expires_at,
                    claim_token,
                    job_id,
                    ParseJobState.PENDING.value,
                    now,
                ),
            )
            if updated.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
                FROM parse_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def update_options(self, *, job_id: str, options: Mapping[str, Any]) -> ParseJob:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE parse_jobs
                SET options_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(dict(options), ensure_ascii=False), now, job_id),
            )
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def get_job(self, *, job_id: str) -> ParseJob | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
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
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
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
            "tenant_id, quota_key, quota_units, "
            "state, created_at, updated_at, failure_reason, attempt_count, dead_lettered_at, "
            "claimed_at, lease_expires_at, next_attempt_at, claim_token FROM parse_jobs"
        )
        params: tuple[str, ...] = ()
        if doc_id is not None:
            query += " WHERE doc_id = ?"
            params = (doc_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def get_blocks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Block]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT block_id, doc_id, type, content, metadata_json
                FROM blocks
                WHERE doc_id = ? AND tenant_id = ?
                ORDER BY position ASC
                """,
                (doc_id, normalized_tenant),
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

    def get_chunks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Chunk]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, doc_id, block_ids_json, text, language, semantic_role, embedding_json
                FROM chunks
                WHERE doc_id = ? AND tenant_id = ?
                ORDER BY position ASC
                """,
                (doc_id, normalized_tenant),
            ).fetchall()
        return tuple(
            Chunk(
                chunk_id=row[0],
                doc_id=row[1],
                block_ids=tuple(json.loads(row[2])),
                text=row[3],
                language=row[4],
                semantic_role=row[5] or "paragraph",
                embedding=tuple(json.loads(row[6])) if row[6] is not None else None,
            )
            for row in rows
        )

    def increment_attempt(self, *, job_id: str) -> int:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE parse_jobs SET attempt_count = attempt_count + 1, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            row = conn.execute(
                "SELECT attempt_count FROM parse_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def mark_dead_letter(
        self,
        *,
        job_id: str,
        reason: str,
        expected_claim_token: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        where = "job_id = ?"
        params: list[Any] = [ParseJobState.FAILED.value, reason, now, now, job_id]
        if expected_claim_token is not None:
            where += " AND claim_token = ?"
            params.append(expected_claim_token)
        with self._connect() as conn:
            updated = conn.execute(
                f"""
                UPDATE parse_jobs
                SET state = ?, failure_reason = ?, dead_lettered_at = ?, updated_at = ?,
                    claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL, next_attempt_at = NULL
                WHERE {where}
                """,
                tuple(params),
            )
            if expected_claim_token is not None and updated.rowcount == 0:
                raise RuntimeError("stale_claim")
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

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
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    quota_key TEXT NOT NULL DEFAULT 'default',
                    quota_units INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_reason TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    dead_lettered_at TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    claim_token TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_parse_jobs_doc_created
                ON parse_jobs (doc_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS blocks (
                    block_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    position INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_blocks_doc_position
                ON blocks (tenant_id, doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    position INTEGER NOT NULL,
                    block_ids_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    semantic_role TEXT NOT NULL DEFAULT 'paragraph',
                    embedding_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_doc_position
                ON chunks (tenant_id, doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS document_views (
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    view_type TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    page_start INTEGER,
                    page_end INTEGER,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, doc_id, view_type, position)
                );

                CREATE INDEX IF NOT EXISTS idx_document_views_lookup
                ON document_views (tenant_id, doc_id, view_type, page_start, page_end);

                CREATE TABLE IF NOT EXISTS search_layer_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    layer TEXT NOT NULL,
                    hit_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_search_layer_metrics_tenant_layer_created
                ON search_layer_metrics (tenant_id, layer, created_at DESC);
                """
            )
            # Lightweight migrations for B1 columns. SQLite cannot use
            # ``ADD COLUMN IF NOT EXISTS`` so we probe pragma first.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(parse_jobs)").fetchall()}
            if "attempt_count" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if "dead_lettered_at" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN dead_lettered_at TEXT")
            if "claimed_at" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN claimed_at TEXT")
            if "lease_expires_at" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN lease_expires_at TEXT")
            if "next_attempt_at" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN next_attempt_at TEXT")
            if "claim_token" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN claim_token TEXT")
            if "tenant_id" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            if "quota_key" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN quota_key TEXT NOT NULL DEFAULT 'default'")
            if "quota_units" not in existing:
                conn.execute("ALTER TABLE parse_jobs ADD COLUMN quota_units INTEGER NOT NULL DEFAULT 1")
            chunk_columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
            block_columns = {row[1] for row in conn.execute("PRAGMA table_info(blocks)").fetchall()}
            if "tenant_id" not in block_columns:
                conn.execute("ALTER TABLE blocks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            if "semantic_role" not in chunk_columns:
                conn.execute(
                    "ALTER TABLE chunks ADD COLUMN semantic_role TEXT NOT NULL DEFAULT 'paragraph'"
                )
            if "tenant_id" not in chunk_columns:
                conn.execute("ALTER TABLE chunks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_parse_jobs_tenant_doc_created ON parse_jobs (tenant_id, doc_id, created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_parse_jobs_state_created ON parse_jobs (state, created_at ASC)"
            )

    def record_layer_search_hit(
        self,
        *,
        tenant_id: str | None,
        layer: str,
        hit_count: int,
    ) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_layer = str(layer or "primary").strip().lower() or "primary"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO search_layer_metrics (tenant_id, layer, hit_count, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_tenant, normalized_layer, max(0, int(hit_count)), _utc_now()),
            )

    def aggregate_layer_search_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
    ) -> Mapping[str, Mapping[str, int]]:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_tenant = (tenant_id or "").strip()
        if normalized_tenant:
            clauses.append("tenant_id = ?")
            params.append(normalized_tenant)
        if since_hours is not None and float(since_hours) > 0:
            threshold = (datetime.now(UTC) - timedelta(hours=float(since_hours))).isoformat()
            clauses.append("created_at >= ?")
            params.append(threshold)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    layer,
                    COUNT(*) AS queries,
                    SUM(CASE WHEN hit_count > 0 THEN 1 ELSE 0 END) AS hit_queries,
                    SUM(hit_count) AS total_hits,
                    MAX(hit_count) AS max_hits
                FROM search_layer_metrics
                {where_sql}
                GROUP BY layer
                """,
                tuple(params),
            ).fetchall()
        metrics: dict[str, dict[str, int]] = {}
        for row in rows:
            layer = str(row[0] or "primary")
            metrics[layer] = {
                "queries": int(row[1] or 0),
                "hit_queries": int(row[2] or 0),
                "total_hits": int(row[3] or 0),
                "max_hits": int(row[4] or 0),
            }
        return metrics

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
        try:
            claimed_at = row["claimed_at"]
        except (IndexError, KeyError):
            claimed_at = None
        try:
            lease_expires_at = row["lease_expires_at"]
        except (IndexError, KeyError):
            lease_expires_at = None
        try:
            next_attempt_at = row["next_attempt_at"]
        except (IndexError, KeyError):
            next_attempt_at = None
        try:
            claim_token = row["claim_token"]
        except (IndexError, KeyError):
            claim_token = None
        return ParseJob(
            job_id=row[0],
            doc_id=row[1],
            file_path=row[2],
            media_type=row[3],
            options=json.loads(row[4]),
            tenant_id=row[5] or "default",
            quota_key=row[6] or "default",
            quota_units=max(1, int(row[7] or 1)),
            state=ParseJobState(row[8]),
            created_at=row[9],
            updated_at=row[10],
            failure_reason=row[11],
            attempt_count=attempt_count,
            dead_lettered_at=dead_lettered_at,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
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

    Prefer ``psycopg_pool`` when available to amortize connection setup
    overhead under concurrent worker/API traffic. If the pool dependency is
    not installed we transparently fall back to per-call connections.
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
        self._pool: Any | None = None
        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=1,
                max_size=10,
                timeout=10,
                kwargs={"autocommit": True},
                open=True,
            )
        except ImportError:
            self._pool = None
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
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    quota_key TEXT NOT NULL DEFAULT 'default',
                    quota_units INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    failure_reason TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    dead_lettered_at TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    claim_token TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_parse_jobs_doc_created
                    ON parse_jobs (doc_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS blocks (
                    block_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    position INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_blocks_doc_position
                    ON blocks (tenant_id, doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    position INTEGER NOT NULL,
                    block_ids_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    semantic_role TEXT NOT NULL DEFAULT 'paragraph',
                    embedding_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc_position
                    ON chunks (doc_id, position ASC);

                CREATE TABLE IF NOT EXISTS document_views (
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    view_type TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    page_start INTEGER,
                    page_end INTEGER,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, doc_id, view_type, position)
                );
                CREATE INDEX IF NOT EXISTS idx_document_views_lookup
                    ON document_views (tenant_id, doc_id, view_type, page_start, page_end);

                CREATE TABLE IF NOT EXISTS search_layer_metrics (
                    id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    layer TEXT NOT NULL,
                    hit_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_search_layer_metrics_tenant_layer_created
                    ON search_layer_metrics (tenant_id, layer, created_at DESC);
                """
            )
            cur.execute(
                "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS semantic_role TEXT NOT NULL DEFAULT 'paragraph'"
            )
            cur.execute("ALTER TABLE blocks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
            cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS quota_key TEXT NOT NULL DEFAULT 'default'")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS quota_units INTEGER NOT NULL DEFAULT 1")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS claimed_at TEXT")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TEXT")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS next_attempt_at TEXT")
            cur.execute("ALTER TABLE parse_jobs ADD COLUMN IF NOT EXISTS claim_token TEXT")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_parse_jobs_tenant_doc_created ON parse_jobs (tenant_id, doc_id, created_at DESC)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_parse_jobs_state_created ON parse_jobs (state, created_at ASC)"
            )

    def record_layer_search_hit(
        self,
        *,
        tenant_id: str | None,
        layer: str,
        hit_count: int,
    ) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_layer = str(layer or "primary").strip().lower() or "primary"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_layer_metrics (tenant_id, layer, hit_count, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (normalized_tenant, normalized_layer, max(0, int(hit_count)), _utc_now()),
            )

    def aggregate_layer_search_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
    ) -> Mapping[str, Mapping[str, int]]:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_tenant = (tenant_id or "").strip()
        if normalized_tenant:
            clauses.append("tenant_id = %s")
            params.append(normalized_tenant)
        if since_hours is not None and float(since_hours) > 0:
            threshold = (datetime.now(UTC) - timedelta(hours=float(since_hours))).isoformat()
            clauses.append("created_at >= %s")
            params.append(threshold)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    layer,
                    COUNT(*) AS queries,
                    SUM(CASE WHEN hit_count > 0 THEN 1 ELSE 0 END) AS hit_queries,
                    SUM(hit_count) AS total_hits,
                    MAX(hit_count) AS max_hits
                FROM search_layer_metrics
                {where_sql}
                GROUP BY layer
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        metrics: dict[str, dict[str, int]] = {}
        for row in rows:
            layer = str(row[0] or "primary")
            metrics[layer] = {
                "queries": int(row[1] or 0),
                "hit_queries": int(row[2] or 0),
                "total_hits": int(row[3] or 0),
                "max_hits": int(row[4] or 0),
            }
        return metrics

    # -- write paths ------------------------------------------------------

    def create(self, request: ParseRequest) -> ParseJob:
        now = _utc_now()
        job = ParseJob(
            job_id=f"job-{uuid4().hex[:12]}",
            doc_id=request.doc_id,
            file_path=request.file_path,
            media_type=request.media_type,
            options=dict(request.options),
            tenant_id=request.tenant_id,
            quota_key=request.quota_key,
            quota_units=max(1, int(request.quota_units or 1)),
            state=ParseJobState.PENDING,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO parse_jobs (
                    job_id, doc_id, file_path, media_type, options_json,
                    tenant_id, quota_key, quota_units,
                    state, created_at, updated_at, failure_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.job_id,
                    job.doc_id,
                    job.file_path,
                    job.media_type,
                    json.dumps(job.options, ensure_ascii=False),
                    job.tenant_id,
                    job.quota_key,
                    int(job.quota_units),
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
        expected_claim_token: str | None = None,
        clear_claim: bool = False,
        next_attempt_at: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        assignments = ["state = %s", "failure_reason = %s", "updated_at = %s"]
        params: list[Any] = [state.value, failure_reason, now]
        if clear_claim or next_attempt_at is not None:
            assignments.append("next_attempt_at = %s")
            params.append(next_attempt_at)
        if clear_claim:
            assignments.extend(["claim_token = NULL", "claimed_at = NULL", "lease_expires_at = NULL"])
        where = "job_id = %s"
        params.append(job_id)
        if expected_claim_token is not None:
            where += " AND claim_token = %s"
            params.append(expected_claim_token)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE parse_jobs SET {', '.join(assignments)} WHERE {where}",
                tuple(params),
            )
            if expected_claim_token is not None and cur.rowcount == 0:
                raise RuntimeError("stale_claim")
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block], tenant_id: str | None = None) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        rows = [
            (
                block.block_id,
                doc_id,
                normalized_tenant,
                position,
                block.type.value,
                block.content,
                json.dumps(block.metadata, ensure_ascii=False),
            )
            for position, block in enumerate(blocks)
        ]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM blocks WHERE doc_id = %s AND tenant_id = %s", (doc_id, normalized_tenant))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO blocks (block_id, doc_id, tenant_id, position, type, content, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk], tenant_id: str | None = None) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        rows = [
            (
                chunk.chunk_id,
                doc_id,
                normalized_tenant,
                position,
                json.dumps(chunk.block_ids, ensure_ascii=False),
                chunk.text,
                chunk.language,
                chunk.semantic_role,
                json.dumps(chunk.embedding, ensure_ascii=False)
                if chunk.embedding is not None
                else None,
            )
            for position, chunk in enumerate(chunks)
        ]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE doc_id = %s AND tenant_id = %s", (doc_id, normalized_tenant))
            if rows:
                cur.executemany(
                    """
                    INSERT INTO chunks (chunk_id, doc_id, tenant_id, position, block_ids_json, text, language, semantic_role, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    def save_document_views(
        self,
        *,
        doc_id: str,
        pages: Sequence[Mapping[str, Any]] = (),
        lines: Sequence[Mapping[str, Any]] = (),
        records: Sequence[Mapping[str, Any]] = (),
        tenant_id: str | None = None,
    ) -> None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        updated_at = _utc_now()
        rows: list[tuple[Any, ...]] = []
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="pages",
                items=pages,
                updated_at=updated_at,
            )
        )
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="lines",
                items=lines,
                updated_at=updated_at,
            )
        )
        rows.extend(
            _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="records",
                items=records,
                updated_at=updated_at,
            )
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM document_views WHERE doc_id = %s AND tenant_id = %s",
                (doc_id, normalized_tenant),
            )
            if rows:
                cur.executemany(
                    """
                    INSERT INTO document_views (
                        doc_id, tenant_id, view_type, position, item_id,
                        page_start, page_end, payload_json, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    def replace_document_views_by_prefix(
        self,
        *,
        doc_id: str,
        item_id_prefix: str,
        pages: Sequence[Mapping[str, Any]] = (),
        lines: Sequence[Mapping[str, Any]] = (),
        records: Sequence[Mapping[str, Any]] = (),
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(item_id_prefix or "")
        if not prefix:
            self.save_document_views(
                doc_id=doc_id,
                pages=pages,
                lines=lines,
                records=records,
                tenant_id=tenant_id,
            )
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        updated_at = _utc_now()
        rows_by_view = {
            "pages": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="pages",
                items=pages,
                updated_at=updated_at,
            ),
            "lines": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="lines",
                items=lines,
                updated_at=updated_at,
            ),
            "records": _document_view_rows(
                doc_id=doc_id,
                tenant_id=normalized_tenant,
                view_type="records",
                items=records,
                updated_at=updated_at,
            ),
        }
        with self._connect() as conn, conn.cursor() as cur:
            for view_type, rows in rows_by_view.items():
                self._replace_document_view_rows(
                    cur=cur,
                    doc_id=doc_id,
                    tenant_id=normalized_tenant,
                    view_type=view_type,
                    item_id_prefix=prefix,
                    rows=rows,
                )

    def get_document_views(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> Mapping[str, tuple[dict[str, Any], ...]]:
        return {
            "pages": self.get_document_pages(doc_id=doc_id, tenant_id=tenant_id),
            "lines": self.get_document_lines(doc_id=doc_id, tenant_id=tenant_id),
            "records": self.get_document_records(doc_id=doc_id, tenant_id=tenant_id),
        }

    def get_document_pages(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="pages")

    def get_document_lines(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="lines")

    def get_document_records(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, Any], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="records")

    def query_document_records(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
        query: str | None = None,
        table_id: str | None = None,
        quality_signal: str | None = None,
        field_filters: Mapping[str, Any] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> Mapping[str, Any]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        start = _optional_int_value(page_start)
        end = _optional_int_value(page_end)
        if start is not None and end is not None and start > end:
            raise ValueError("invalid_page_range")

        where = ["doc_id = %s", "tenant_id = %s", "view_type = %s"]
        params: list[Any] = [doc_id, normalized_tenant, "records"]
        if start is not None:
            where.append("COALESCE(page_end, page_start, 1) >= %s")
            params.append(start)
        if end is not None:
            where.append("COALESCE(page_start, page_end, 1) <= %s")
            params.append(end)
        where_sql = " AND ".join(where)

        payload_filter = _record_query_requires_payload_filter(
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=field_filters,
        )
        normalized_limit = None if limit is None else max(1, int(limit))
        normalized_offset = max(0, int(offset or 0))

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM document_views WHERE {where_sql}", tuple(params))
            row = cur.fetchone()
            candidate_count = int(row[0] if row is not None else 0)
            if candidate_count <= 0:
                return {
                    "items": (),
                    "total": 0,
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                    "persisted": False,
                }

            select_sql = (
                "SELECT payload_json FROM document_views "
                f"WHERE {where_sql} "
                "ORDER BY COALESCE(page_start, 0) ASC, position ASC"
            )
            select_params = list(params)
            if not payload_filter:
                if normalized_limit is not None:
                    select_sql += " LIMIT %s OFFSET %s"
                    select_params.extend([normalized_limit, normalized_offset])
                elif normalized_offset:
                    select_sql += " OFFSET %s"
                    select_params.append(normalized_offset)
                cur.execute(select_sql, tuple(select_params))
                rows = cur.fetchall()
                return {
                    "items": _load_document_view_payloads(rows),
                    "total": candidate_count,
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                    "persisted": True,
                }

            cur.execute(select_sql, tuple(select_params))
            result = collect_record_query(
                _iter_document_view_payloads_from_cursor(cur),
                limit=normalized_limit,
                offset=normalized_offset,
                query=query,
                table_id=table_id,
                quality_signal=quality_signal,
                field_filters=field_filters,
                page_start=start,
                page_end=end,
            )
        result["persisted"] = True
        return result

    def _get_document_view_items(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        view_type: str,
    ) -> tuple[dict[str, Any], ...]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_view_type = str(view_type or "").strip().lower()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json
                FROM document_views
                WHERE doc_id = %s AND tenant_id = %s AND view_type = %s
                ORDER BY COALESCE(page_start, 0) ASC, position ASC
                """,
                (doc_id, normalized_tenant, normalized_view_type),
            )
            rows = cur.fetchall()
        return _load_document_view_payloads(rows)

    def _replace_document_view_rows(
        self,
        *,
        cur: Any,
        doc_id: str,
        tenant_id: str,
        view_type: str,
        item_id_prefix: str,
        rows: Sequence[Sequence[Any]],
    ) -> None:
        page_start, page_end = _document_view_bounds(rows)
        params: list[Any] = [doc_id, tenant_id, view_type, len(item_id_prefix), item_id_prefix]
        range_clause = ""
        if page_start is not None and page_end is not None:
            range_clause = " OR (page_start IS NOT NULL AND page_end IS NOT NULL AND page_start <= %s AND page_end >= %s)"
            params.extend([page_end, page_start])
        cur.execute(
            f"""
            SELECT MIN(position), MAX(position), COUNT(*)
            FROM document_views
            WHERE doc_id = %s AND tenant_id = %s AND view_type = %s
              AND (LEFT(item_id, %s) = %s{range_clause})
            """,
            tuple(params),
        )
        row = cur.fetchone()
        min_position = row[0] if row is not None else None
        old_count = int(row[2] or 0) if row is not None else 0
        cur.execute(
            f"""
            DELETE FROM document_views
            WHERE doc_id = %s AND tenant_id = %s AND view_type = %s
              AND (LEFT(item_id, %s) = %s{range_clause})
            """,
            tuple(params),
        )
        if not rows:
            return
        if min_position is None:
            cur.execute(
                """
                SELECT MAX(position)
                FROM document_views
                WHERE doc_id = %s AND tenant_id = %s AND view_type = %s
                """,
                (doc_id, tenant_id, view_type),
            )
            max_row = cur.fetchone()
            insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
        else:
            insert_at = int(min_position)
        delta = len(rows) - old_count
        if delta and min_position is not None:
            cur.execute(
                """
                UPDATE document_views
                SET position = position + %s
                WHERE doc_id = %s AND tenant_id = %s AND view_type = %s AND position > %s
                """,
                (delta, doc_id, tenant_id, view_type, int(min_position) + old_count - 1),
            )
        cur.executemany(
            """
            INSERT INTO document_views (
                doc_id, tenant_id, view_type, position, item_id,
                page_start, page_end, payload_json, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [_with_document_view_position(row, insert_at + index) for index, row in enumerate(rows)],
        )

    def replace_blocks_by_prefix(
        self,
        *,
        doc_id: str,
        blocks: Sequence[Block],
        block_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(block_id_prefix or "")
        if not prefix:
            self.save_blocks(doc_id=doc_id, blocks=blocks, tenant_id=tenant_id)
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(position), MAX(position), COUNT(*)
                FROM blocks
                WHERE doc_id = %s AND tenant_id = %s AND LEFT(block_id, %s) = %s
                """,
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            row = cur.fetchone()
            min_position = row[0] if row is not None else None
            max_position = row[1] if row is not None else None
            old_count = int(row[2] or 0) if row is not None else 0
            if min_position is None:
                cur.execute(
                    "SELECT MAX(position) FROM blocks WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                )
                max_row = cur.fetchone()
                insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
            else:
                insert_at = int(min_position)
            cur.execute(
                "DELETE FROM blocks WHERE doc_id = %s AND tenant_id = %s AND LEFT(block_id, %s) = %s",
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            delta = len(blocks) - old_count
            if delta and max_position is not None:
                cur.execute(
                    """
                    UPDATE blocks
                    SET position = position + %s
                    WHERE doc_id = %s AND tenant_id = %s AND position > %s
                    """,
                    (delta, doc_id, normalized_tenant, int(max_position)),
                )
            if blocks:
                cur.executemany(
                    """
                    INSERT INTO blocks (block_id, doc_id, tenant_id, position, type, content, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            block.block_id,
                            doc_id,
                            normalized_tenant,
                            insert_at + position,
                            block.type.value,
                            block.content,
                            json.dumps(block.metadata, ensure_ascii=False),
                        )
                        for position, block in enumerate(blocks)
                    ],
                )

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        prefix = str(chunk_id_prefix or "")
        if not prefix:
            self.save_chunks(doc_id=doc_id, chunks=chunks, tenant_id=tenant_id)
            return
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(position), MAX(position), COUNT(*)
                FROM chunks
                WHERE doc_id = %s AND tenant_id = %s AND LEFT(chunk_id, %s) = %s
                """,
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            row = cur.fetchone()
            min_position = row[0] if row is not None else None
            max_position = row[1] if row is not None else None
            old_count = int(row[2] or 0) if row is not None else 0
            if min_position is None:
                cur.execute(
                    "SELECT MAX(position) FROM chunks WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                )
                max_row = cur.fetchone()
                insert_at = int(max_row[0] + 1) if max_row is not None and max_row[0] is not None else 0
            else:
                insert_at = int(min_position)
            cur.execute(
                "DELETE FROM chunks WHERE doc_id = %s AND tenant_id = %s AND LEFT(chunk_id, %s) = %s",
                (doc_id, normalized_tenant, len(prefix), prefix),
            )
            delta = len(chunks) - old_count
            if delta and max_position is not None:
                cur.execute(
                    """
                    UPDATE chunks
                    SET position = position + %s
                    WHERE doc_id = %s AND tenant_id = %s AND position > %s
                    """,
                    (delta, doc_id, normalized_tenant, int(max_position)),
                )
            if chunks:
                cur.executemany(
                    """
                    INSERT INTO chunks (chunk_id, doc_id, tenant_id, position, block_ids_json, text, language, semantic_role, embedding_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            chunk.chunk_id,
                            doc_id,
                            normalized_tenant,
                            insert_at + position,
                            json.dumps(chunk.block_ids, ensure_ascii=False),
                            chunk.text,
                            chunk.language,
                            chunk.semantic_role,
                            json.dumps(chunk.embedding, ensure_ascii=False)
                            if chunk.embedding is not None
                            else None,
                        )
                        for position, chunk in enumerate(chunks)
                    ],
                )

    # -- claim / read -----------------------------------------------------

    def claim_next_job(self) -> ParseJob | None:
        # SELECT ... FOR UPDATE SKIP LOCKED gives us safe multi-worker claim
        # semantics natively in Postgres.
        now = _utc_now()
        claim_token = uuid4().hex
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id FROM parse_jobs
                WHERE state = %s
                  AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (ParseJobState.PENDING.value, now),
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
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
                FROM parse_jobs WHERE job_id = %s
                """,
                (job_id,),
            )
            claimed = cur.fetchone()
        if claimed is None:
            return None
        return self._row_to_job(claimed)

    def claim_job(self, *, job_id: str, lease_expires_at: str | None = None) -> ParseJob | None:
        now = _utc_now()
        claim_token = uuid4().hex
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE parse_jobs
                SET state = %s, updated_at = %s, claimed_at = %s, lease_expires_at = %s,
                    next_attempt_at = NULL, claim_token = %s, failure_reason = NULL,
                    attempt_count = attempt_count + 1
                WHERE job_id = %s AND state = %s
                  AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                """,
                (
                    ParseJobState.PARSING.value,
                    now,
                    now,
                    lease_expires_at,
                    claim_token,
                    job_id,
                    ParseJobState.PENDING.value,
                    now,
                ),
            )
            if cur.rowcount == 0:
                return None
            cur.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
                FROM parse_jobs WHERE job_id = %s
                """,
                (job_id,),
            )
            claimed = cur.fetchone()
        if claimed is None:
            return None
        return self._row_to_job(claimed)

    def update_options(self, *, job_id: str, options: Mapping[str, Any]) -> ParseJob:
        now = _utc_now()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE parse_jobs
                SET options_json = %s, updated_at = %s
                WHERE job_id = %s
                """,
                (json.dumps(dict(options), ensure_ascii=False), now, job_id),
            )
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def get_job(self, *, job_id: str) -> ParseJob | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, doc_id, file_path, media_type, options_json,
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
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
                      tenant_id, quota_key, quota_units,
                       state, created_at, updated_at, failure_reason,
                       attempt_count, dead_lettered_at,
                       claimed_at, lease_expires_at, next_attempt_at, claim_token
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
            "tenant_id, quota_key, quota_units, "
            "state, created_at, updated_at, failure_reason, attempt_count, dead_lettered_at, "
            "claimed_at, lease_expires_at, next_attempt_at, claim_token "
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

    def get_blocks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Block]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT block_id, doc_id, type, content, metadata_json
                FROM blocks
                WHERE doc_id = %s AND tenant_id = %s
                ORDER BY position ASC
                """,
                (doc_id, normalized_tenant),
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

    def get_chunks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Chunk]:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, doc_id, block_ids_json, text, language, semantic_role, embedding_json
                FROM chunks
                WHERE doc_id = %s AND tenant_id = %s
                ORDER BY position ASC
                """,
                (doc_id, normalized_tenant),
            )
            rows = cur.fetchall()
        return tuple(
            Chunk(
                chunk_id=row[0],
                doc_id=row[1],
                block_ids=tuple(json.loads(row[2])),
                text=row[3],
                language=row[4],
                semantic_role=row[5] or "paragraph",
                embedding=tuple(json.loads(row[6])) if row[6] is not None else None,
            )
            for row in rows
        )

    # -- helpers ----------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg

        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
            return

        conn = psycopg.connect(self.database_url, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    @staticmethod
    def _row_to_job(row: Sequence[Any]) -> ParseJob:
        attempt_count = int(row[12]) if row[12] is not None else 0
        dead_lettered_at = row[13] if len(row) > 13 else None
        claimed_at = row[14] if len(row) > 14 else None
        lease_expires_at = row[15] if len(row) > 15 else None
        next_attempt_at = row[16] if len(row) > 16 else None
        claim_token = row[17] if len(row) > 17 else None
        return ParseJob(
            job_id=row[0],
            doc_id=row[1],
            file_path=row[2],
            media_type=row[3],
            options=json.loads(row[4]),
            tenant_id=row[5] or "default",
            quota_key=row[6] or "default",
            quota_units=max(1, int(row[7] or 1)),
            state=ParseJobState(row[8]),
            created_at=row[9],
            updated_at=row[10],
            failure_reason=row[11],
            attempt_count=attempt_count,
            dead_lettered_at=dead_lettered_at,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
        )

    def increment_attempt(self, *, job_id: str) -> int:
        now = _utc_now()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE parse_jobs
                SET attempt_count = attempt_count + 1, updated_at = %s
                WHERE job_id = %s
                """,
                (now, job_id),
            )
            cur.execute(
                "SELECT attempt_count FROM parse_jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row is not None else 0

    def mark_dead_letter(
        self,
        *,
        job_id: str,
        reason: str,
        expected_claim_token: str | None = None,
    ) -> ParseJob:
        now = _utc_now()
        where = "job_id = %s"
        params: list[Any] = [ParseJobState.FAILED.value, reason, now, now, job_id]
        if expected_claim_token is not None:
            where += " AND claim_token = %s"
            params.append(expected_claim_token)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE parse_jobs
                SET state = %s, failure_reason = %s, dead_lettered_at = %s, updated_at = %s,
                    claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL, next_attempt_at = NULL
                WHERE {where}
                """,
                tuple(params),
            )
            if expected_claim_token is not None and cur.rowcount == 0:
                raise RuntimeError("stale_claim")
        job = self.get_job(job_id=job_id)
        if job is None:
            raise KeyError(job_id)
        return job


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
        self._pool: Any | None = None
        try:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=1,
                max_size=10,
                timeout=10,
                kwargs={"autocommit": True},
                open=True,
            )
        except ImportError:
            self._pool = None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS chunk_embeddings (
                        chunk_id TEXT PRIMARY KEY,
                        doc_id   TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        embedding vector({self.dim}) NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_doc
                        ON chunk_embeddings (tenant_id, doc_id);

                    CREATE TABLE IF NOT EXISTS high_precision_chunk_embeddings (
                        entry_id TEXT PRIMARY KEY,
                        chunk_id TEXT NOT NULL,
                        doc_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        block_ids_json TEXT NOT NULL,
                        text TEXT NOT NULL,
                        language TEXT NOT NULL,
                        semantic_role TEXT NOT NULL,
                        embedding vector({self.dim}),
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_hp_chunk_embeddings_doc
                        ON high_precision_chunk_embeddings (tenant_id, doc_id);

                    CREATE TABLE IF NOT EXISTS structure_index_entries (
                        entry_id TEXT PRIMARY KEY,
                        doc_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        item_id TEXT NOT NULL,
                        semantic_role TEXT NOT NULL,
                        page_number INTEGER,
                        tags_json TEXT NOT NULL,
                        text TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_structure_index_doc
                        ON structure_index_entries (tenant_id, doc_id, semantic_role);

                    CREATE TABLE IF NOT EXISTS index_manifests (
                        doc_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        manifest_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (tenant_id, doc_id)
                    );
                    """
                )
                cur.execute("ALTER TABLE chunk_embeddings ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
                cur.execute("ALTER TABLE high_precision_chunk_embeddings ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
                cur.execute("ALTER TABLE structure_index_entries ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")
                cur.execute("ALTER TABLE index_manifests ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'")

    def upsert(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        tenant_id: str | None = None,
        document: object | None = None,
        index_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        from pgvector.psycopg import register_vector

        normalized_tenant = _normalize_tenant_id(tenant_id)
        rows: list[tuple[str, str, str, list[float], str]] = []
        high_precision_rows: list[tuple[str, str, str, str, str, str, str, str, list[float] | None, str]] = []
        structure_rows: list[tuple[str, str, str, str, str, int | None, str, str, str]] = []
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
            rows.append((chunk.chunk_id, doc_id, normalized_tenant, vec, now))

        high_precision_ids: set[str] = set()
        if isinstance(index_manifest, Mapping):
            for layer in tuple(index_manifest.get("layers", ())):
                if not isinstance(layer, Mapping):
                    continue
                if str(layer.get("name") or "") != "high_precision":
                    continue
                for chunk_id in tuple(layer.get("chunk_ids", ())):
                    normalized = str(chunk_id).strip()
                    if normalized:
                        high_precision_ids.add(normalized)

        if high_precision_ids:
            for chunk in chunks:
                if chunk.chunk_id not in high_precision_ids:
                    continue
                if chunk.embedding is not None:
                    chunk_embedding = list(float(value) for value in chunk.embedding)
                    if len(chunk_embedding) != self.dim:
                        raise ValueError(
                            f"chunk {chunk.chunk_id} embedding dim={len(chunk_embedding)} "
                            f"mismatch with index dim={self.dim}"
                        )
                else:
                    chunk_embedding = None
                high_precision_rows.append(
                    (
                        f"{normalized_tenant}:{doc_id}:{chunk.chunk_id}",
                        chunk.chunk_id,
                        doc_id,
                        normalized_tenant,
                        json.dumps(tuple(chunk.block_ids), ensure_ascii=False),
                        str(chunk.text or ""),
                        str(chunk.language or "unknown"),
                        str(chunk.semantic_role or "paragraph"),
                        chunk_embedding,
                        now,
                    )
                )

        if document is not None:
            for item in tuple(getattr(document, "items", ()) or ()):
                item_id = str(getattr(item, "item_id", "")).strip()
                if not item_id:
                    continue
                semantic_role = str(getattr(item, "semantic_role", "") or "paragraph")
                tags = list(getattr(item, "metadata", {}).get("structure_tags") or [])
                page_number_raw = getattr(item, "page_number", None)
                try:
                    page_number = int(page_number_raw) if page_number_raw is not None else None
                except (TypeError, ValueError):
                    page_number = None
                structure_rows.append(
                    (
                        f"{normalized_tenant}:{doc_id}:{item_id}",
                        doc_id,
                        normalized_tenant,
                        item_id,
                        semantic_role,
                        page_number,
                        json.dumps(tags, ensure_ascii=False),
                        str(getattr(item, "text", "") or ""),
                        now,
                    )
                )

        with self._connect() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chunk_embeddings WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                )
                if rows:
                    cur.executemany(
                        """
                        INSERT INTO chunk_embeddings (chunk_id, doc_id, tenant_id, embedding, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        rows,
                    )
                cur.execute(
                    "DELETE FROM high_precision_chunk_embeddings WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                )
                if high_precision_rows:
                    cur.executemany(
                        """
                        INSERT INTO high_precision_chunk_embeddings (
                            entry_id, chunk_id, doc_id, tenant_id,
                            block_ids_json, text, language, semantic_role,
                            embedding, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        high_precision_rows,
                    )
                cur.execute(
                    "DELETE FROM structure_index_entries WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                )
                if structure_rows:
                    cur.executemany(
                        """
                        INSERT INTO structure_index_entries (
                            entry_id, doc_id, tenant_id, item_id, semantic_role,
                            page_number, tags_json, text, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        structure_rows,
                    )
                if index_manifest is not None:
                    cur.execute(
                        "DELETE FROM index_manifests WHERE doc_id = %s AND tenant_id = %s",
                        (doc_id, normalized_tenant),
                    )
                    cur.execute(
                        """
                        INSERT INTO index_manifests (doc_id, tenant_id, manifest_json, updated_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (doc_id, normalized_tenant, json.dumps(dict(index_manifest), ensure_ascii=False), now),
                    )

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
        document: object | None = None,
        index_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        from pgvector.psycopg import register_vector

        prefix = str(chunk_id_prefix or "")
        if not prefix:
            self.upsert(
                doc_id=doc_id,
                chunks=chunks,
                tenant_id=tenant_id,
                document=document,
                index_manifest=index_manifest,
            )
            return

        normalized_tenant = _normalize_tenant_id(tenant_id)
        rows: list[tuple[str, str, str, list[float], str]] = []
        high_precision_rows: list[tuple[str, str, str, str, str, str, str, str, list[float] | None, str]] = []
        structure_rows: list[tuple[str, str, str, str, str, int | None, str, str, str]] = []
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
            rows.append((chunk.chunk_id, doc_id, normalized_tenant, vec, now))

        high_precision_ids: set[str] = set()
        if isinstance(index_manifest, Mapping):
            for layer in tuple(index_manifest.get("layers", ())):
                if not isinstance(layer, Mapping):
                    continue
                if str(layer.get("name") or "") != "high_precision":
                    continue
                for chunk_id in tuple(layer.get("chunk_ids", ())):
                    normalized = str(chunk_id).strip()
                    if normalized:
                        high_precision_ids.add(normalized)

        if high_precision_ids:
            for chunk in chunks:
                if chunk.chunk_id not in high_precision_ids:
                    continue
                if chunk.embedding is not None:
                    chunk_embedding = list(float(value) for value in chunk.embedding)
                    if len(chunk_embedding) != self.dim:
                        raise ValueError(
                            f"chunk {chunk.chunk_id} embedding dim={len(chunk_embedding)} "
                            f"mismatch with index dim={self.dim}"
                        )
                else:
                    chunk_embedding = None
                high_precision_rows.append(
                    (
                        f"{normalized_tenant}:{doc_id}:{chunk.chunk_id}",
                        chunk.chunk_id,
                        doc_id,
                        normalized_tenant,
                        json.dumps(tuple(chunk.block_ids), ensure_ascii=False),
                        str(chunk.text or ""),
                        str(chunk.language or "unknown"),
                        str(chunk.semantic_role or "paragraph"),
                        chunk_embedding,
                        now,
                    )
                )

        if document is not None:
            for item in tuple(getattr(document, "items", ()) or ()):
                item_id = str(getattr(item, "item_id", "")).strip()
                if not item_id:
                    continue
                semantic_role = str(getattr(item, "semantic_role", "") or "paragraph")
                tags = list(getattr(item, "metadata", {}).get("structure_tags") or [])
                page_number_raw = getattr(item, "page_number", None)
                try:
                    page_number = int(page_number_raw) if page_number_raw is not None else None
                except (TypeError, ValueError):
                    page_number = None
                structure_rows.append(
                    (
                        f"{normalized_tenant}:{doc_id}:{item_id}",
                        doc_id,
                        normalized_tenant,
                        item_id,
                        semantic_role,
                        page_number,
                        json.dumps(tags, ensure_ascii=False),
                        str(getattr(item, "text", "") or ""),
                        now,
                    )
                )

        with self._connect() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chunk_embeddings WHERE doc_id = %s AND tenant_id = %s AND LEFT(chunk_id, %s) = %s",
                    (doc_id, normalized_tenant, len(prefix), prefix),
                )
                if rows:
                    cur.executemany(
                        """
                        INSERT INTO chunk_embeddings (chunk_id, doc_id, tenant_id, embedding, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        rows,
                    )
                cur.execute(
                    """
                    DELETE FROM high_precision_chunk_embeddings
                    WHERE doc_id = %s AND tenant_id = %s AND LEFT(chunk_id, %s) = %s
                    """,
                    (doc_id, normalized_tenant, len(prefix), prefix),
                )
                if high_precision_rows:
                    cur.executemany(
                        """
                        INSERT INTO high_precision_chunk_embeddings (
                            entry_id, chunk_id, doc_id, tenant_id,
                            block_ids_json, text, language, semantic_role,
                            embedding, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        high_precision_rows,
                    )
                if document is not None:
                    cur.execute(
                        "DELETE FROM structure_index_entries WHERE doc_id = %s AND tenant_id = %s",
                        (doc_id, normalized_tenant),
                    )
                    if structure_rows:
                        cur.executemany(
                            """
                            INSERT INTO structure_index_entries (
                                entry_id, doc_id, tenant_id, item_id, semantic_role,
                                page_number, tags_json, text, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            structure_rows,
                        )
                if index_manifest is not None:
                    cur.execute(
                        "DELETE FROM index_manifests WHERE doc_id = %s AND tenant_id = %s",
                        (doc_id, normalized_tenant),
                    )
                    cur.execute(
                        """
                        INSERT INTO index_manifests (doc_id, tenant_id, manifest_json, updated_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (doc_id, normalized_tenant, json.dumps(dict(index_manifest), ensure_ascii=False), now),
                    )

    def describe_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                row = cur.execute(
                    "SELECT manifest_json FROM index_manifests WHERE doc_id = %s AND tenant_id = %s",
                    (doc_id, normalized_tenant),
                ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def get_layer_chunks(
        self,
        *,
        doc_id: str,
        layer: str,
        tenant_id: str | None = None,
    ) -> tuple[Chunk, ...] | None:
        normalized_tenant = _normalize_tenant_id(tenant_id)
        normalized_layer = str(layer or "primary").strip().lower()
        if normalized_layer != "high_precision":
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                rows = tuple(
                    cur.execute(
                        """
                        SELECT chunk_id, block_ids_json, text, language, semantic_role, embedding
                        FROM high_precision_chunk_embeddings
                        WHERE doc_id = %s AND tenant_id = %s
                        ORDER BY updated_at DESC, chunk_id ASC
                        """,
                        (doc_id, normalized_tenant),
                    ).fetchall()
                )
        chunks: list[Chunk] = []
        for row in rows:
            raw_embedding = row[5]
            embedding: tuple[float, ...] | None
            if raw_embedding is None:
                embedding = None
            else:
                embedding = tuple(float(value) for value in raw_embedding)
            chunks.append(
                Chunk(
                    chunk_id=str(row[0]),
                    doc_id=doc_id,
                    block_ids=tuple(json.loads(row[1]) if row[1] else ()),
                    text=str(row[2] or ""),
                    language=str(row[3] or "unknown"),
                    semantic_role=str(row[4] or "paragraph"),
                    embedding=embedding,
                )
            )
        return tuple(chunks)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg

        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
            return

        conn = psycopg.connect(self.database_url, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
