from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from .contracts import ChunkBuilder, EmbeddingProvider, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, TranslationAdapter
from .models import Block, BlockType, Chunk, ParseJob, ParseJobState, ParseOutcome, ParseRequest, SemanticRole
from .record_filters import collect_record_query


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _prefix_insert_index(*, existing_ids: Sequence[str], retained_ids: Sequence[str], prefix: str) -> int:
    if not prefix:
        return len(retained_ids)
    deleted_positions = [
        position
        for position, item_id in enumerate(existing_ids)
        if str(item_id).startswith(prefix)
    ]
    if not deleted_positions:
        return len(retained_ids)
    first_deleted = deleted_positions[0]
    return sum(
        1
        for position, item_id in enumerate(existing_ids)
        if position < first_deleted and not str(item_id).startswith(prefix)
    )


def _view_item_id(view_type: str, item: Mapping[str, object], position: int) -> str:
    if view_type == "records":
        block_id = item.get("block_id")
        if block_id is None:
            source_block_ids = item.get("source_block_ids")
            if isinstance(source_block_ids, (list, tuple)):
                block_id = next((value for value in source_block_ids if str(value or "").strip()), None)
        record_id = str(item.get("record_id") or f"record:{position + 1}")
        if block_id is not None and str(block_id).strip():
            return f"{block_id}:record:{record_id}"
    if view_type == "pages" and item.get("page_number") is not None:
        return f"page:{item.get('page_number')}"
    if view_type == "lines" and item.get("line_id") is not None:
        return str(item.get("line_id"))
    return str(item.get("id") or f"{view_type}:{position + 1}")


def _view_page_range(item: Mapping[str, object]) -> tuple[int | None, int | None]:
    page_start = item.get("page_start")
    page_end = item.get("page_end")
    page_number = item.get("page_number", item.get("page"))
    if page_start is None and page_number is not None:
        page_start = page_number
    if page_end is None:
        page_end = page_start
    return _optional_int(page_start), _optional_int(page_end)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _view_ranges_overlap(item: Mapping[str, object], range_start: int | None, range_end: int | None) -> bool:
    if range_start is None or range_end is None:
        return False
    item_start, item_end = _view_page_range(item)
    if item_start is None and item_end is None:
        return False
    if item_start is None:
        item_start = item_end
    if item_end is None:
        item_end = item_start
    return int(item_start or 0) <= range_end and int(item_end or 0) >= range_start


def _view_bounds(items: Sequence[Mapping[str, object]]) -> tuple[int | None, int | None]:
    values: list[int] = []
    for item in items:
        start, end = _view_page_range(item)
        if start is not None:
            values.append(start)
        if end is not None:
            values.append(end)
    if not values:
        return None, None
    return min(values), max(values)


class StubParser(ParserAdapter):
    def __init__(self, *, name: str, media_types: Sequence[str], extensions: Sequence[str]) -> None:
        self.name = name
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        file_name = Path(request.file_path).name
        return (
            Block(
                block_id=f"blk-{uuid4().hex[:12]}",
                doc_id=request.doc_id,
                type=BlockType.TITLE,
                content=file_name,
                metadata={
                    "parser": self.name,
                    "page": 1,
                    "kind": "stub-title",
                    "semantic_role": SemanticRole.TITLE.value,
                },
            ),
            Block(
                block_id=f"blk-{uuid4().hex[:12]}",
                doc_id=request.doc_id,
                type=BlockType.PARAGRAPH,
                content=(
                    "This is a placeholder parse result. Replace StubParser with a real parser "
                    "before connecting production documents."
                ),
                metadata={
                    "parser": self.name,
                    "page": 1,
                    "kind": "stub-body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
        )


class ParagraphChunkBuilder(ChunkBuilder):
    def build(self, *, doc_id: str, blocks: Sequence[Block]) -> Sequence[Chunk]:
        chunks: list[Chunk] = []
        for block in blocks:
            semantic_role = str(
                block.metadata.get("semantic_role")
                or _default_semantic_role_for_block(block)
            )
            chunks.append(
                Chunk(
                    chunk_id=f"chk-{uuid4().hex[:12]}",
                    doc_id=doc_id,
                    block_ids=(block.block_id,),
                    text=block.content,
                    semantic_role=semantic_role,
                )
            )
        return tuple(chunks)


def _default_semantic_role_for_block(block: Block) -> str:
    return {
        BlockType.TITLE: SemanticRole.TITLE.value,
        BlockType.TABLE: SemanticRole.TABLE.value,
        BlockType.IMAGE: SemanticRole.IMAGE.value,
    }.get(block.type, SemanticRole.PARAGRAPH.value)


class NullEmbeddingProvider(EmbeddingProvider):
    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        return tuple(chunks)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic helper that stamps pgvector-compatible embeddings."""

    def __init__(self, *, dim: int = 1536) -> None:
        self.dim = max(2, int(dim))

    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        embedded: list[Chunk] = []
        for index, chunk in enumerate(chunks, start=1):
            base = [0.0] * self.dim
            base[0] = float(index)
            base[1] = float(len(chunk.text))
            embedded.append(
                replace(chunk, embedding=tuple(base))
            )
        return tuple(embedded)


class NullIndex(IndexAdapter):
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []
        self.documents: dict[tuple[str, str], dict[str, object]] = {}
        self.layer_chunks: dict[tuple[str, str, str], tuple[Chunk, ...]] = {}

    def upsert(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        tenant_id: str | None = None,
        document: object | None = None,
        index_manifest: dict[str, object] | None = None,
    ) -> None:
        normalized_tenant = tenant_id or "default"
        structure_items = tuple(getattr(document, "items", ()) or ()) if document is not None else ()
        payload = {
            "doc_id": doc_id,
            "tenant_id": normalized_tenant,
            "chunks": len(chunks),
            "structure_items": len(structure_items),
            "index_manifest": dict(index_manifest or {}),
        }
        self.upserts.append(payload)
        self.documents[(normalized_tenant, doc_id)] = dict(index_manifest or {})
        self.layer_chunks[(normalized_tenant, doc_id, "primary")] = tuple(chunks)

        high_precision_ids: set[str] = set()
        if isinstance(index_manifest, dict):
            for layer in tuple(index_manifest.get("layers", ())):
                if not isinstance(layer, dict):
                    continue
                if str(layer.get("name") or "") != "high_precision":
                    continue
                for chunk_id in tuple(layer.get("chunk_ids") or ()):
                    normalized = str(chunk_id).strip()
                    if normalized:
                        high_precision_ids.add(normalized)
        if high_precision_ids:
            selected = tuple(chunk for chunk in chunks if chunk.chunk_id in high_precision_ids)
        else:
            selected = ()
        self.layer_chunks[(normalized_tenant, doc_id, "high_precision")] = selected

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
        document: object | None = None,
        index_manifest: dict[str, object] | None = None,
    ) -> None:
        normalized_tenant = tenant_id or "default"
        prefix = str(chunk_id_prefix or "")
        old_primary = self.layer_chunks.get((normalized_tenant, doc_id, "primary"), ())
        retained_primary = tuple(chunk for chunk in old_primary if not str(chunk.chunk_id).startswith(prefix))
        insert_at = _prefix_insert_index(
            existing_ids=[str(chunk.chunk_id) for chunk in old_primary],
            retained_ids=[str(chunk.chunk_id) for chunk in retained_primary],
            prefix=prefix,
        )
        primary = retained_primary[:insert_at] + tuple(chunks) + retained_primary[insert_at:]
        structure_items = tuple(getattr(document, "items", ()) or ()) if document is not None else ()
        payload = {
            "doc_id": doc_id,
            "tenant_id": normalized_tenant,
            "mode": "replace_by_prefix",
            "chunk_id_prefix": prefix,
            "chunks": len(chunks),
            "structure_items": len(structure_items),
            "index_manifest": dict(index_manifest or {}),
        }
        self.upserts.append(payload)
        self.documents[(normalized_tenant, doc_id)] = dict(index_manifest or {})
        self.layer_chunks[(normalized_tenant, doc_id, "primary")] = primary

        high_precision_ids: set[str] = set()
        if isinstance(index_manifest, dict):
            for layer in tuple(index_manifest.get("layers", ())):
                if not isinstance(layer, dict):
                    continue
                if str(layer.get("name") or "") != "high_precision":
                    continue
                for chunk_id in tuple(layer.get("chunk_ids") or ()):
                    normalized = str(chunk_id).strip()
                    if normalized:
                        high_precision_ids.add(normalized)
        old_high_precision = self.layer_chunks.get((normalized_tenant, doc_id, "high_precision"), ())
        retained_high_precision = tuple(
            chunk for chunk in old_high_precision if not str(chunk.chunk_id).startswith(prefix)
        )
        selected = tuple(chunk for chunk in chunks if chunk.chunk_id in high_precision_ids)
        self.layer_chunks[(normalized_tenant, doc_id, "high_precision")] = retained_high_precision + selected

    def describe_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, object] | None:
        return self.documents.get(((tenant_id or "default"), doc_id))

    def get_layer_chunks(
        self,
        *,
        doc_id: str,
        layer: str,
        tenant_id: str | None = None,
    ) -> tuple[Chunk, ...] | None:
        normalized_tenant = tenant_id or "default"
        normalized_layer = str(layer or "primary").strip().lower()
        if normalized_layer not in {"primary", "high_precision"}:
            return None
        return self.layer_chunks.get((normalized_tenant, doc_id, normalized_layer), ())


class EchoTranslator(TranslationAdapter):
    def translate(self, *, text: str, target_lang: str) -> str:
        return f"[{target_lang}] {text}"


class EmbeddedProductAdapter(ProductAdapter):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def before_parse(self, *, request: ParseRequest, job: ParseJob) -> None:
        self.events.append({"event": "before_parse", "doc_id": request.doc_id, "job_id": job.job_id})

    def after_parse(self, *, outcome: ParseOutcome) -> None:
        self.events.append(
            {
                "event": "after_parse",
                "doc_id": outcome.job.doc_id,
                "job_id": outcome.job.job_id,
                "blocks": len(outcome.blocks),
                "chunks": len(outcome.chunks),
            }
        )

    def on_failure(self, *, request: ParseRequest, job: ParseJob, error: Exception) -> None:
        self.events.append(
            {
                "event": "on_failure",
                "doc_id": request.doc_id,
                "job_id": job.job_id,
                "error": str(error),
            }
        )


class InMemoryJobStore(JobStore):
    def __init__(self) -> None:
        self.jobs: dict[str, ParseJob] = {}
        self.blocks_by_doc: dict[tuple[str, str], tuple[Block, ...]] = {}
        self.chunks_by_doc: dict[tuple[str, str], tuple[Chunk, ...]] = {}
        self.document_views_by_doc: dict[tuple[str, str, str], tuple[dict[str, object], ...]] = {}
        self.search_layer_metrics: list[dict[str, object]] = []

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
        self.jobs[job.job_id] = job
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
        job = self.jobs[job_id]
        if expected_claim_token is not None and job.claim_token != expected_claim_token:
            raise RuntimeError("stale_claim")
        job.state = state
        job.failure_reason = failure_reason
        if next_attempt_at is not None:
            job.next_attempt_at = next_attempt_at
        elif clear_claim and state != ParseJobState.PENDING:
            job.next_attempt_at = None
        if clear_claim:
            job.claimed_at = None
            job.lease_expires_at = None
            job.claim_token = None
        job.updated_at = _utc_now()
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block], tenant_id: str | None = None) -> None:
        key = ((tenant_id or "default"), doc_id)
        self.blocks_by_doc[key] = tuple(blocks)

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk], tenant_id: str | None = None) -> None:
        key = ((tenant_id or "default"), doc_id)
        self.chunks_by_doc[key] = tuple(chunks)

    def save_document_views(
        self,
        *,
        doc_id: str,
        pages: Sequence[Mapping[str, object]] = (),
        lines: Sequence[Mapping[str, object]] = (),
        records: Sequence[Mapping[str, object]] = (),
        tenant_id: str | None = None,
    ) -> None:
        tenant = tenant_id or "default"
        self.document_views_by_doc[(tenant, doc_id, "pages")] = tuple(dict(item) for item in pages)
        self.document_views_by_doc[(tenant, doc_id, "lines")] = tuple(dict(item) for item in lines)
        self.document_views_by_doc[(tenant, doc_id, "records")] = tuple(dict(item) for item in records)

    def replace_document_views_by_prefix(
        self,
        *,
        doc_id: str,
        item_id_prefix: str,
        pages: Sequence[Mapping[str, object]] = (),
        lines: Sequence[Mapping[str, object]] = (),
        records: Sequence[Mapping[str, object]] = (),
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
        self._replace_document_view_items(
            doc_id=doc_id,
            tenant_id=tenant_id,
            view_type="pages",
            items=tuple(dict(item) for item in pages),
            item_id_prefix=prefix,
        )
        self._replace_document_view_items(
            doc_id=doc_id,
            tenant_id=tenant_id,
            view_type="lines",
            items=tuple(dict(item) for item in lines),
            item_id_prefix=prefix,
        )
        self._replace_document_view_items(
            doc_id=doc_id,
            tenant_id=tenant_id,
            view_type="records",
            items=tuple(dict(item) for item in records),
            item_id_prefix=prefix,
        )

    def get_document_views(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> Mapping[str, tuple[dict[str, object], ...]]:
        return {
            "pages": self.get_document_pages(doc_id=doc_id, tenant_id=tenant_id),
            "lines": self.get_document_lines(doc_id=doc_id, tenant_id=tenant_id),
            "records": self.get_document_records(doc_id=doc_id, tenant_id=tenant_id),
        }

    def get_document_pages(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, object], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="pages")

    def get_document_lines(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, object], ...]:
        return self._get_document_view_items(doc_id=doc_id, tenant_id=tenant_id, view_type="lines")

    def get_document_records(self, *, doc_id: str, tenant_id: str | None = None) -> tuple[dict[str, object], ...]:
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
        field_filters: Mapping[str, object] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> Mapping[str, object]:
        key = ((tenant_id or "default"), doc_id, "records")
        records = self.get_document_records(doc_id=doc_id, tenant_id=tenant_id)
        result = collect_record_query(
            records,
            limit=limit,
            offset=offset,
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=field_filters,
            page_start=page_start,
            page_end=page_end,
        )
        result["persisted"] = key in self.document_views_by_doc
        return result

    def _get_document_view_items(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        view_type: str,
    ) -> tuple[dict[str, object], ...]:
        key = ((tenant_id or "default"), doc_id, str(view_type or "").strip().lower())
        rows = tuple(dict(item) for item in self.document_views_by_doc.get(key, ()))
        return tuple(
            sorted(
                rows,
                key=lambda item: (
                    _view_page_range(item)[0] if _view_page_range(item)[0] is not None else 0,
                    _view_item_id(str(view_type or "").strip().lower(), item, 0),
                ),
            )
        )

    def _replace_document_view_items(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        view_type: str,
        items: tuple[dict[str, object], ...],
        item_id_prefix: str,
    ) -> None:
        tenant = tenant_id or "default"
        key = (tenant, doc_id, view_type)
        existing = tuple(dict(item) for item in self.document_views_by_doc.get(key, ()))
        range_start, range_end = _view_bounds(items)
        retained = tuple(
            item
            for index, item in enumerate(existing)
            if not _view_item_id(view_type, item, index).startswith(item_id_prefix)
            and not _view_ranges_overlap(item, range_start, range_end)
        )
        deleted_ids = [
            _view_item_id(view_type, item, index)
            for index, item in enumerate(existing)
            if _view_item_id(view_type, item, index).startswith(item_id_prefix)
            or _view_ranges_overlap(item, range_start, range_end)
        ]
        insert_at = _prefix_insert_index(
            existing_ids=[_view_item_id(view_type, item, index) for index, item in enumerate(existing)],
            retained_ids=[_view_item_id(view_type, item, index) for index, item in enumerate(retained)],
            prefix=item_id_prefix,
        )
        if not deleted_ids and range_start is not None:
            insert_at = len([item for item in retained if (_view_page_range(item)[0] or 0) < range_start])
        self.document_views_by_doc[key] = retained[:insert_at] + items + retained[insert_at:]

    def replace_blocks_by_prefix(
        self,
        *,
        doc_id: str,
        blocks: Sequence[Block],
        block_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        key = ((tenant_id or "default"), doc_id)
        prefix = str(block_id_prefix or "")
        existing = self.blocks_by_doc.get(key, ())
        retained = tuple(block for block in existing if not str(block.block_id).startswith(prefix))
        insert_at = _prefix_insert_index(
            existing_ids=[str(block.block_id) for block in existing],
            retained_ids=[str(block.block_id) for block in retained],
            prefix=prefix,
        )
        replacement = tuple(blocks)
        self.blocks_by_doc[key] = retained[:insert_at] + replacement + retained[insert_at:]

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None:
        key = ((tenant_id or "default"), doc_id)
        prefix = str(chunk_id_prefix or "")
        existing = self.chunks_by_doc.get(key, ())
        retained = tuple(chunk for chunk in existing if not str(chunk.chunk_id).startswith(prefix))
        insert_at = _prefix_insert_index(
            existing_ids=[str(chunk.chunk_id) for chunk in existing],
            retained_ids=[str(chunk.chunk_id) for chunk in retained],
            prefix=prefix,
        )
        replacement = tuple(chunks)
        self.chunks_by_doc[key] = retained[:insert_at] + replacement + retained[insert_at:]

    def claim_next_job(self) -> ParseJob | None:
        pending = [job for job in self.jobs.values() if job.state == ParseJobState.PENDING]
        if not pending:
            return None
        job = sorted(pending, key=lambda item: item.created_at)[0]
        job.state = ParseJobState.PARSING
        now = _utc_now()
        job.updated_at = now
        job.claimed_at = now
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.claim_token = uuid4().hex
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.failure_reason = None
        return job

    def claim_job(self, *, job_id: str, lease_expires_at: str | None = None) -> ParseJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.state != ParseJobState.PENDING:
            return None
        job.state = ParseJobState.PARSING
        now = _utc_now()
        job.updated_at = now
        job.claimed_at = now
        job.lease_expires_at = lease_expires_at
        job.next_attempt_at = None
        job.claim_token = uuid4().hex
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.failure_reason = None
        return job

    def update_options(self, *, job_id: str, options: Mapping[str, object]) -> ParseJob:
        job = self.jobs[job_id]
        job.options = dict(options)
        job.updated_at = _utc_now()
        return job

    def get_job(self, *, job_id: str) -> ParseJob | None:
        return self.jobs.get(job_id)

    def get_latest_job(self, *, doc_id: str) -> ParseJob | None:
        matches = [job for job in self.jobs.values() if job.doc_id == doc_id]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.created_at)[-1]

    def list_jobs(self, *, doc_id: str | None = None) -> Sequence[ParseJob]:
        jobs = list(self.jobs.values())
        if doc_id is not None:
            jobs = [job for job in jobs if job.doc_id == doc_id]
        return tuple(sorted(jobs, key=lambda item: item.created_at, reverse=True))

    def get_blocks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Block]:
        key = ((tenant_id or "default"), doc_id)
        return self.blocks_by_doc.get(key, ())

    def get_chunks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Chunk]:
        key = ((tenant_id or "default"), doc_id)
        return self.chunks_by_doc.get(key, ())

    def increment_attempt(self, *, job_id: str) -> int:
        job = self.jobs[job_id]
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.updated_at = _utc_now()
        return job.attempt_count

    def mark_dead_letter(
        self,
        *,
        job_id: str,
        reason: str,
        expected_claim_token: str | None = None,
    ) -> ParseJob:
        job = self.jobs[job_id]
        if expected_claim_token is not None and job.claim_token != expected_claim_token:
            raise RuntimeError("stale_claim")
        job.state = ParseJobState.FAILED
        job.failure_reason = reason
        job.dead_lettered_at = _utc_now()
        job.updated_at = job.dead_lettered_at
        job.claimed_at = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.claim_token = None
        return job

    def record_layer_search_hit(
        self,
        *,
        tenant_id: str | None,
        layer: str,
        hit_count: int,
    ) -> None:
        self.search_layer_metrics.append(
            {
                "tenant_id": str(tenant_id or "default"),
                "layer": str(layer or "primary").strip().lower() or "primary",
                "hit_count": max(0, int(hit_count)),
                "created_at": _utc_now(),
            }
        )

    def aggregate_layer_search_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
    ) -> dict[str, dict[str, int]]:
        tenant_filter = (tenant_id or "").strip()
        threshold_dt = None
        if since_hours is not None and float(since_hours) > 0:
            threshold_dt = datetime.now(UTC).timestamp() - float(since_hours) * 3600.0
        metrics: dict[str, dict[str, int]] = {}
        for event in self.search_layer_metrics:
            event_tenant = str(event.get("tenant_id") or "default")
            if tenant_filter and event_tenant != tenant_filter:
                continue
            if threshold_dt is not None:
                created_at_raw = str(event.get("created_at") or "")
                try:
                    created_at_dt = datetime.fromisoformat(created_at_raw)
                except ValueError:
                    continue
                if created_at_dt.tzinfo is None:
                    created_at_ts = created_at_dt.replace(tzinfo=UTC).timestamp()
                else:
                    created_at_ts = created_at_dt.timestamp()
                if created_at_ts < threshold_dt:
                    continue
            layer = str(event.get("layer") or "primary")
            bucket = metrics.setdefault(layer, {"queries": 0, "hit_queries": 0, "total_hits": 0, "max_hits": 0})
            hits = max(0, int(event.get("hit_count") or 0))
            bucket["queries"] += 1
            bucket["total_hits"] += hits
            if hits > 0:
                bucket["hit_queries"] += 1
            bucket["max_hits"] = max(bucket["max_hits"], hits)
        return metrics

    def snapshot(self) -> dict[str, object]:
        return {
            "jobs": {job_id: asdict(job) for job_id, job in self.jobs.items()},
            "documents": sorted(self.blocks_by_doc.keys()),
        }

