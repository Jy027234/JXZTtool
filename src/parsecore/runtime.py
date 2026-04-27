from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from math import sqrt
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import ParseCoreSettings
from .contracts import ChunkBuilder, EmbeddingProvider, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, TranslationAdapter
from .events import JobEventLogger
from .models import Chunk, ChunkSearchHit, ParseJobState, ParseOutcome, ParseRequest, StructureSearchHit
from .pipelines import ParsedDocumentArtifact, PipelineRegistry


_RERUN_CHUNKS_ONLY_MODE = "rerun_chunks_only"
_RERUN_EMBEDDINGS_ONLY_MODE = "rerun_embeddings_only"
_SEARCH_VECTOR_WEIGHT = 0.7
_SEARCH_KEYWORD_WEIGHT = 0.3
_SEARCH_METRICS_HISTORY_LIMIT = 5000
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_SEMANTIC_ROLE_WEIGHTS: dict[str, float] = {
    "title": 1.35,
    "warning": 1.25,
    "caution": 1.15,
    "note": 1.1,
    "table": 1.05,
    "paragraph": 1.0,
    "highlights_entry": 0.95,
    "toc_entry": 0.7,
    "lep_entry": 0.55,
}
_TASK_LIKE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]|\([a-z]\)|step\s+\d+|task\s+\d+)",
    re.IGNORECASE,
)


class QuotaExceededError(RuntimeError):
    def __init__(
        self,
        *,
        tenant_id: str,
        quota_key: str,
        limit_units: int,
        used_units: int,
        requested_units: int,
        window_hours: float | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.quota_key = quota_key
        self.limit_units = int(limit_units)
        self.used_units = int(used_units)
        self.requested_units = int(requested_units)
        self.window_hours = float(window_hours) if window_hours is not None else None
        super().__init__(
            f"quota exceeded for {tenant_id}:{quota_key} "
            f"used={used_units}, requested={requested_units}, limit={limit_units}"
        )


class EventAggregator:
    """
    In-memory aggregator for observability events (quota_exceeded, too_many_inflight_jobs, etc.).
    Maintains ringbuffer for recent events and counters by dimension (tenant_id, quota_key, event_type).
    """
    def __init__(self, max_ringbuffer_size: int = 1000) -> None:
        self.max_ringbuffer_size = max_ringbuffer_size
        self.ringbuffer: list[dict[str, Any]] = []
        self.counters: dict[tuple[str, str, str], int] = {}  # (tenant_id, quota_key, event_type) -> count

    def record_event(
        self,
        event_type: str,
        *,
        tenant_id: str = "*",
        quota_key: str = "*",
        doc_id: str | None = None,
        details: dict[str, Any] | None = None,
        count: int = 1,
    ) -> None:
        """Record an observability event."""
        now = datetime.now(timezone.utc).isoformat()
        event: dict[str, Any] = {
            "timestamp": now,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "quota_key": quota_key,
        }
        if doc_id is not None:
            event["doc_id"] = doc_id
        if details:
            event.update(details)

        # Add to ringbuffer (FIFO with max size)
        self.ringbuffer.append(event)
        if len(self.ringbuffer) > self.max_ringbuffer_size:
            self.ringbuffer.pop(0)

        # Update counters
        key = (tenant_id, quota_key, event_type)
        self.counters[key] = self.counters.get(key, 0) + max(1, int(count))

    def get_events(
        self,
        limit: int = 100,
        event_type_filter: str | None = None,
        tenant_id_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent events with optional filtering."""
        filtered = self.ringbuffer
        if event_type_filter:
            filtered = [e for e in filtered if e["event_type"] == event_type_filter]
        if tenant_id_filter:
            filtered = [e for e in filtered if e["tenant_id"] == tenant_id_filter]
        # Return most recent first (reverse order)
        return list(reversed(filtered[-limit:]))

    def get_counters(
        self,
        event_type_filter: str | None = None,
        tenant_id_filter: str | None = None,
    ) -> dict[str, int]:
        """Get event counters with optional filtering."""
        result = {}
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type_filter and event_type != event_type_filter:
                continue
            if tenant_id_filter and tenant_id != tenant_id_filter:
                continue
            key = f"{tenant_id}:{quota_key}:{event_type}"
            result[key] = count
        return result

    def get_prometheus_metrics(self) -> str:
        """Generate Prometheus-format metrics."""
        lines = [
            "# HELP parse_quota_exceeded_total Total quota exceeded errors",
            "# TYPE parse_quota_exceeded_total counter",
        ]
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "quota_exceeded":
                lines.append(
                    f'parse_quota_exceeded_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_inflight_full_total Total inflight backpressure rejections",
            "# TYPE parse_inflight_full_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "too_many_inflight_jobs":
                lines.append(
                    f'parse_inflight_full_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_embedding_retry_total Total embedding retry attempts",
            "# TYPE parse_embedding_retry_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "embedding_retry":
                lines.append(
                    f'parse_embedding_retry_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_embedding_skipped_total Total embedding skip failures",
            "# TYPE parse_embedding_skipped_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "embedding_skipped":
                lines.append(
                    f'parse_embedding_skipped_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_attempt_total Total pages where OCR was attempted",
            "# TYPE parse_ocr_attempt_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_attempted":
                lines.append(
                    f'parse_ocr_attempt_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_fallback_total Total pages where OCR fallback succeeded",
            "# TYPE parse_ocr_fallback_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_fallback":
                lines.append(
                    f'parse_ocr_fallback_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_failed_total Total pages where OCR was attempted but failed",
            "# TYPE parse_ocr_failed_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_failed":
                lines.append(
                    f'parse_ocr_failed_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ringbuffer_size Current event ringbuffer size",
            "# TYPE parse_ringbuffer_size gauge",
            f"parse_ringbuffer_size {len(self.ringbuffer)}",
        ])

        return "\n".join(lines)


class ParseRuntime:
    def __init__(
        self,
        *,
        settings: ParseCoreSettings,
        parsers: Sequence[ParserAdapter],
        chunk_builder: ChunkBuilder,
        embedding_provider: EmbeddingProvider,
        index: IndexAdapter,
        translator: TranslationAdapter,
        product_adapter: ProductAdapter,
        job_store: JobStore,
        event_logger: JobEventLogger | None = None,
        pipeline_registry: PipelineRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.parsers = tuple(parsers)
        self.chunk_builder = chunk_builder
        self.embedding_provider = embedding_provider
        self.index = index
        self.translator = translator
        self.product_adapter = product_adapter
        self.job_store = job_store
        self.event_logger = event_logger or JobEventLogger(settings.runtime.log_path)
        self.event_aggregator = EventAggregator(max_ringbuffer_size=1000)
        self.pipeline_registry = pipeline_registry
        self._search_layer_query_hits: dict[tuple[str, str], list[int]] = {}

    def describe(self) -> dict[str, object]:
        description: dict[str, object] = {
            "project": self.settings.project_name,
            "mode": self.settings.mode,
            "database_url": self.settings.database_url,
            "object_store": self.settings.object_store,
            "index_mode": self.settings.index_mode,
            "runtime": {
                "execution_mode": self.settings.runtime.execution_mode,
                "max_workers": self.settings.runtime.max_workers,
                "poll_interval_ms": self.settings.runtime.poll_interval_ms,
                "max_inflight_jobs": self.settings.runtime.max_inflight_jobs,
            },
            "translation": {
                "enabled": self.settings.translation_enabled,
                "strategy": self.settings.translation_strategy,
            },
            "embedding": {
                "enabled": self.settings.providers.embedding.enabled,
                "provider": self.settings.providers.embedding.provider,
                "model": self.settings.providers.embedding.model,
            },
            "product_adapter": self.settings.product_adapter,
            "parsers": [parser.name for parser in self.parsers],
            "index_layers": ["primary", "structure", "high_precision"],
            "embedding_tiers": ["small", "large"],
        }
        if self.pipeline_registry is not None:
            registry_description = self.pipeline_registry.describe()
            description["pipelines"] = registry_description["pipelines"]
            description["pipeline_cache"] = registry_description["cache"]
        return description

    def submit(self, request: ParseRequest) -> ParseOutcome:
        job = self.start(request)
        return self.execute(job_id=job.job_id)

    def start(self, request: ParseRequest):
        self._check_quota_limit(request)
        job = self.job_store.create(request)
        self.product_adapter.before_parse(request=request, job=job)

        return job

    def execute(self, *, job_id: str) -> ParseOutcome:
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            raise LookupError(f"No parse job found for job_id={job_id!r}")
        if job.dead_lettered_at is not None:
            raise RuntimeError(
                f"job {job.job_id} is dead-lettered ({job.failure_reason!r}); refusing to execute"
            )
        attempt = self._increment_attempt(job_id=job.job_id)
        request = ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
            tenant_id=job.tenant_id,
            quota_key=job.quota_key,
            quota_units=job.quota_units,
        )
        started_at = time.monotonic()
        self.event_logger.log(
            "started",
            job_id=job.job_id,
            doc_id=job.doc_id,
            attempt=attempt,
            file_path=job.file_path,
        )
        try:
            if job.state == ParseJobState.PENDING:
                self.job_store.update_state(job_id=job.job_id, state=ParseJobState.PARSING)
                self.event_logger.log(
                    "state_changed",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    state=ParseJobState.PARSING.value,
                )
            blocks = self._load_blocks_for_request(request)
            self._record_ocr_observability(request=request, blocks=blocks)
            if not self._is_rerun_chunks_only(request):
                self.job_store.save_blocks(
                    doc_id=request.doc_id,
                    blocks=blocks,
                    tenant_id=request.tenant_id,
                )
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.STRUCTURING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.STRUCTURING.value,
            )

            document = self._load_document_for_request(request, blocks=blocks)
            chunks = tuple(self._load_chunks_for_request(request, blocks=blocks, document=document))
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.EMBEDDING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.EMBEDDING.value,
            )
            chunks = tuple(self._embed_chunks(doc_id=request.doc_id, chunks=chunks))

            index_manifest = self._build_index_manifest(
                request=request,
                job_id=job.job_id,
                document=document,
                chunks=chunks,
            )
            self.job_store.save_chunks(
                doc_id=request.doc_id,
                chunks=chunks,
                tenant_id=request.tenant_id,
            )
            self.index.upsert(
                doc_id=request.doc_id,
                chunks=chunks,
                tenant_id=request.tenant_id,
                document=document,
                index_manifest=index_manifest,
            )
            final_job = self.job_store.update_state(job_id=job.job_id, state=ParseJobState.DONE)
            outcome = ParseOutcome(job=final_job, blocks=blocks, chunks=chunks)
            self.event_logger.log(
                "completed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                attempt=attempt,
                duration_s=round(time.monotonic() - started_at, 3),
                blocks=len(blocks),
                chunks=len(chunks),
            )
            self.product_adapter.after_parse(outcome=outcome)
            return outcome
        except Exception as exc:
            max_attempts = max(1, int(self.settings.runtime.max_attempts))
            if attempt >= max_attempts:
                dead_letter = getattr(self.job_store, "mark_dead_letter", None)
                if callable(dead_letter):
                    failed_job = dead_letter(job_id=job.job_id, reason=str(exc))
                else:
                    failed_job = self.job_store.update_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                    )
                self.event_logger.log(
                    "dead_letter",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                failed_job = self.job_store.update_state(
                    job_id=job.job_id,
                    state=ParseJobState.FAILED,
                    failure_reason=str(exc),
                )
                self.event_logger.log(
                    "failed",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            self.product_adapter.on_failure(request=request, job=failed_job, error=exc)
            raise

    def _increment_attempt(self, *, job_id: str) -> int:
        increment = getattr(self.job_store, "increment_attempt", None)
        if callable(increment):
            return int(increment(job_id=job_id))
        # In-memory store path: read-modify-write the in-place job.
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            return 1
        job.attempt_count = int(job.attempt_count or 0) + 1
        return job.attempt_count

    def get_job(self, *, job_id: str):
        return self.job_store.get_job(job_id=job_id)

    def get_document(self, *, doc_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        normalized_tenant = str(tenant_id or getattr(latest_job, "tenant_id", "default") or "default")
        if latest_job is None:
            blocks: tuple[Any, ...] = ()
            chunks: tuple[Any, ...] = ()
        else:
            blocks = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
            chunks = tuple(self.job_store.get_chunks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
        describe_document = getattr(self.index, "describe_document", None)
        index_manifest = None
        if callable(describe_document):
            index_manifest = describe_document(doc_id=doc_id, tenant_id=normalized_tenant)
        if index_manifest is None and latest_job is not None:
            index_manifest = self._derive_index_manifest_from_snapshot(
                job=latest_job,
                blocks=blocks,
                chunks=chunks,
            )
        return {
            "doc_id": doc_id,
            "job": latest_job,
            "blocks": blocks,
            "chunks": chunks,
            "index_manifest": index_manifest,
        }

    def _derive_index_manifest_from_snapshot(
        self,
        *,
        job: Any,
        blocks: Sequence[Any],
        chunks: Sequence[Chunk],
    ) -> dict[str, Any]:
        semantic_roles = sorted(
            {
                str((getattr(block, "metadata", {}) or {}).get("semantic_role") or "paragraph")
                for block in blocks
            }
        )
        chunk_roles = sorted({str(chunk.semantic_role or "paragraph") for chunk in chunks})
        options_hash = ""
        pipeline_name = ""
        if self.pipeline_registry is not None:
            try:
                pipeline = self.pipeline_registry.resolve(self._request_from_job(job), purpose="parse")
                pipeline_name = str(getattr(pipeline, "name", "") or "")
            except Exception:
                pipeline_name = ""
        embedding_tiers = _resolve_embedding_tiers(getattr(job, "options", {}) or {})
        high_precision_chunks = _select_high_precision_chunks(chunks)
        layers: list[dict[str, Any]] = [
            {
                "name": "primary",
                "kind": "chunk",
                "version": job.job_id,
                "item_count": len(chunks),
                "semantic_roles": chunk_roles,
                "embedding_tier": embedding_tiers[0],
            },
            {
                "name": "structure",
                "kind": "typed-item",
                "version": job.job_id,
                "item_count": len(blocks),
                "semantic_roles": semantic_roles,
            },
        ]
        if "large" in embedding_tiers:
            layers.append(
                {
                    "name": "high_precision",
                    "kind": "chunk",
                    "version": job.job_id,
                    "item_count": len(high_precision_chunks),
                    "semantic_roles": sorted({str(chunk.semantic_role or "paragraph") for chunk in high_precision_chunks}),
                    "embedding_tier": "large",
                    "chunk_ids": [chunk.chunk_id for chunk in high_precision_chunks],
                }
            )
        return {
            "doc_id": job.doc_id,
            "tenant_id": job.tenant_id,
            "pipeline_name": pipeline_name,
            "options_hash": options_hash,
            "index_version": job.job_id,
            "embedding_tiers": embedding_tiers,
            "layers": layers,
        }

    def search_document(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[ChunkSearchHit, ...]:
        hits, _mode = self.search_document_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=semantic_roles,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        tenant_id: str | None = None,
        index_layer: str = "primary",
    ) -> tuple[tuple[ChunkSearchHit, ...], str]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            return (), "keyword-fallback"
        requested_layer = _normalize_chunk_index_layer(index_layer)
        chunks = self._load_chunks_for_index_layer(
            doc_id=doc_id,
            tenant_id=latest_job.tenant_id,
            layer=requested_layer,
        )
        if not chunks:
            return (), "keyword-fallback"

        query_embedding = self._try_embed_search_query(query=query)
        retrieval_mode = _resolve_retrieval_mode(
            query_embedding=query_embedding,
            chunks=chunks,
        )
        allowed_roles = {
            str(role).strip().lower()
            for role in (semantic_roles or ())
            if str(role).strip()
        }
        scored: list[ChunkSearchHit] = []
        for chunk in chunks:
            role = str(chunk.semantic_role or "paragraph").strip().lower()
            if allowed_roles and role not in allowed_roles:
                continue
            score = _score_chunk(
                query=query,
                query_embedding=query_embedding,
                chunk=chunk,
            )
            if score <= 0:
                continue
            scored.append(
                ChunkSearchHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    block_ids=chunk.block_ids,
                    text=chunk.text,
                    semantic_role=role,
                    score=round(score, 4),
                )
            )

        ranked = sorted(scored, key=lambda item: (-item.score, item.chunk_id))
        result = tuple(ranked[: max(1, int(limit))])
        self._record_search_effectiveness(
            tenant_id=latest_job.tenant_id,
            layer=requested_layer,
            hit_count=len(result),
        )
        return result, retrieval_mode

    def _record_search_effectiveness(self, *, tenant_id: str | None, layer: str, hit_count: int) -> None:
        normalized_tenant = str(tenant_id or "default").strip() or "default"
        normalized_layer = _normalize_chunk_index_layer(layer)
        key = (normalized_tenant, normalized_layer)
        history = self._search_layer_query_hits.setdefault(key, [])
        history.append(max(0, int(hit_count)))
        overflow = len(history) - _SEARCH_METRICS_HISTORY_LIMIT
        if overflow > 0:
            del history[:overflow]
        record_layer_search_hit = getattr(self.job_store, "record_layer_search_hit", None)
        if callable(record_layer_search_hit):
            record_layer_search_hit(
                tenant_id=normalized_tenant,
                layer=normalized_layer,
                hit_count=max(0, int(hit_count)),
            )

    def _load_chunks_for_index_layer(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        layer: str,
    ) -> tuple[Chunk, ...]:
        chunks = tuple(self.job_store.get_chunks(doc_id=doc_id, tenant_id=tenant_id))
        normalized_layer = _normalize_chunk_index_layer(layer)
        if normalized_layer == "primary":
            return chunks

        get_layer_chunks = getattr(self.index, "get_layer_chunks", None)
        if callable(get_layer_chunks):
            indexed_chunks = get_layer_chunks(doc_id=doc_id, layer=normalized_layer, tenant_id=tenant_id)
            if indexed_chunks is not None:
                return tuple(indexed_chunks)

        return _select_high_precision_chunks(chunks)

    def search_document_structure(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        structure_tags: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[StructureSearchHit, ...]:
        hits, _mode = self.search_document_structure_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=semantic_roles,
            structure_tags=structure_tags,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_structure_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        structure_tags: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[tuple[StructureSearchHit, ...], str]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            return (), "structure-keyword"
        blocks = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
        if not blocks:
            return (), "structure-keyword"
        entries = self._structure_entries_from_blocks(doc_id=doc_id, blocks=blocks)
        allowed_roles = {
            str(role).strip().lower() for role in (semantic_roles or ()) if str(role).strip()
        }
        allowed_tags = {
            str(tag).strip().lower() for tag in (structure_tags or ()) if str(tag).strip()
        }
        scored: list[StructureSearchHit] = []
        for entry in entries:
            role = str(entry["semantic_role"] or "paragraph").strip().lower()
            tags = tuple(str(tag).strip().lower() for tag in entry["structure_tags"])
            if allowed_roles and role not in allowed_roles:
                continue
            if allowed_tags and not any(tag in allowed_tags for tag in tags):
                continue
            score = _score_structure_entry(query=query, text=str(entry["text"]), semantic_role=role, tags=tags)
            if score <= 0.0:
                continue
            scored.append(
                StructureSearchHit(
                    item_id=str(entry["item_id"]),
                    doc_id=doc_id,
                    block_ids=(str(entry["block_id"]),),
                    text=str(entry["text"]),
                    semantic_role=role,
                    structure_tags=tuple(tags),
                    page_number=entry["page_number"],
                    score=round(score, 4),
                )
            )
        ranked = sorted(scored, key=lambda item: (-item.score, item.item_id))
        return tuple(ranked[: max(1, int(limit))]), "structure-keyword"

    def search_document_tasks(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> tuple[StructureSearchHit, ...]:
        hits, _mode = self.search_document_tasks_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_tasks_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> tuple[tuple[StructureSearchHit, ...], str]:
        hits, mode = self.search_document_structure_with_mode(
            doc_id=doc_id,
            query=query,
            limit=max(limit * 3, 10),
            semantic_roles=("paragraph", "warning", "caution", "note"),
            tenant_id=tenant_id,
        )
        task_hits = tuple(
            hit for hit in hits if _is_task_like_entry(hit.text, hit.semantic_role, hit.structure_tags)
        )
        return task_hits[: max(1, int(limit))], mode

    def _try_embed_search_query(self, *, query: str) -> tuple[float, ...] | None:
        if not query.strip():
            return None
        probe = Chunk(
            chunk_id="query-probe",
            doc_id="search-query",
            block_ids=(),
            text=query,
        )
        try:
            embedded = tuple(self.embedding_provider.embed(doc_id="search-query", chunks=[probe]))
        except Exception as exc:
            self.event_logger.log(
                "search_embedding_skipped",
                error=str(exc),
            )
            return None
        if not embedded or embedded[0].embedding is None:
            return None
        return tuple(float(item) for item in embedded[0].embedding)

    def retry_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseOutcome:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.submit(self._request_from_job(latest_job))

    def restart_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.start(self._request_from_job(latest_job))

    def rechunk_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_CHUNKS_ONLY_MODE
        return self.start(request)

    def reembed_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_EMBEDDINGS_ONLY_MODE
        return self.start(request)

    def list_jobs(
        self,
        *,
        doc_id: str | None = None,
        tenant_id: str | None = None,
        quota_key: str | None = None,
        since_hours: float | None = None,
    ):
        jobs = tuple(self.job_store.list_jobs(doc_id=doc_id))
        normalized_tenant = (tenant_id or "").strip()
        normalized_quota = (quota_key or "").strip()
        if normalized_tenant:
            jobs = tuple(job for job in jobs if (job.tenant_id or "default") == normalized_tenant)
        if normalized_quota:
            jobs = tuple(job for job in jobs if (job.quota_key or "default") == normalized_quota)
        return _filter_jobs_by_since_hours(jobs, since_hours=since_hours)

    def quota_usage(self, *, tenant_id: str | None = None, since_hours: float | None = None) -> dict[str, Any]:
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        by_bucket: dict[tuple[str, str], dict[str, Any]] = {}
        for job in jobs:
            t_id = job.tenant_id or "default"
            q_key = job.quota_key or "default"
            units = max(1, int(job.quota_units or 1))
            key = (t_id, q_key)
            bucket = by_bucket.get(key)
            if bucket is None:
                bucket = {
                    "tenant_id": t_id,
                    "quota_key": q_key,
                    "job_count": 0,
                    "total_quota_units": 0,
                    "done_jobs": 0,
                    "failed_jobs": 0,
                }
                by_bucket[key] = bucket
            bucket["job_count"] += 1
            bucket["total_quota_units"] += units
            if job.state == ParseJobState.DONE:
                bucket["done_jobs"] += 1
            if job.state == ParseJobState.FAILED:
                bucket["failed_jobs"] += 1

        items = sorted(
            by_bucket.values(),
            key=lambda item: (str(item["tenant_id"]), str(item["quota_key"])),
        )
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "total_jobs": len(jobs),
            "total_quota_units": sum(int(item["total_quota_units"]) for item in items),
            "items": items,
        }

    def index_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
        trend_windows_hours: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        normalized_tenant_filter = (tenant_id or "").strip() or None
        manifests: list[dict[str, Any]] = []
        seen_docs: set[tuple[str, str]] = set()
        for job in jobs:
            key = (job.tenant_id or "default", job.doc_id)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            snapshot = self.get_document(doc_id=job.doc_id, tenant_id=job.tenant_id)
            manifest = snapshot.get("index_manifest")
            if isinstance(manifest, dict):
                manifests.append(manifest)
        layer_counts: dict[str, int] = {}
        layer_items: dict[str, int] = {}
        semantic_roles: dict[str, int] = {}
        versions: dict[str, int] = {}
        for manifest in manifests:
            versions[str(manifest.get("index_version") or "")] = versions.get(str(manifest.get("index_version") or ""), 0) + 1
            for layer in manifest.get("layers", []):
                name = str(layer.get("name") or "unknown")
                layer_counts[name] = layer_counts.get(name, 0) + 1
                layer_items[name] = layer_items.get(name, 0) + int(layer.get("item_count") or 0)
                for role in layer.get("semantic_roles", []):
                    role_name = str(role or "paragraph")
                    semantic_roles[role_name] = semantic_roles.get(role_name, 0) + 1

        document_total = len(manifests)
        high_precision_docs = int(layer_counts.get("high_precision", 0))
        high_precision_items = int(layer_items.get("high_precision", 0))
        primary_items = int(layer_items.get("primary", 0))
        document_coverage = 0.0
        if document_total > 0:
            document_coverage = round(high_precision_docs / document_total, 4)
        item_ratio_vs_primary = 0.0
        if primary_items > 0:
            item_ratio_vs_primary = round(high_precision_items / primary_items, 4)

        search_effectiveness = self._build_search_effectiveness_snapshot(
            tenant_id=normalized_tenant_filter,
            since_hours=since_hours,
        )
        normalized_trend_windows = _normalize_trend_windows_hours(trend_windows_hours)
        search_effectiveness_trends: dict[str, dict[str, dict[str, Any]]] = {}
        for window_hours in normalized_trend_windows:
            window_label = _format_trend_window_label(window_hours)
            search_effectiveness_trends[window_label] = self._build_search_effectiveness_snapshot(
                tenant_id=normalized_tenant_filter,
                since_hours=window_hours,
            )

        high_precision_search = search_effectiveness.get("high_precision", {})
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "trend_windows_hours": list(normalized_trend_windows),
            "documents": document_total,
            "layer_counts": layer_counts,
            "layer_items": layer_items,
            "semantic_role_coverage": semantic_roles,
            "index_versions": {key: value for key, value in versions.items() if key},
            "search_effectiveness": search_effectiveness,
            "search_effectiveness_trends": search_effectiveness_trends,
            "high_precision": {
                "documents": high_precision_docs,
                "document_coverage": document_coverage,
                "items": high_precision_items,
                "item_ratio_vs_primary": item_ratio_vs_primary,
                "query_count": int(high_precision_search.get("queries") or 0),
                "query_hit_rate": float(high_precision_search.get("hit_rate") or 0.0),
                "query_avg_hits": float(high_precision_search.get("avg_hits") or 0.0),
            },
        }

    def _build_search_effectiveness_snapshot(
        self,
        *,
        tenant_id: str | None,
        since_hours: float | None,
    ) -> dict[str, dict[str, Any]]:
        aggregate_layer_search_metrics = getattr(self.job_store, "aggregate_layer_search_metrics", None)
        raw_metrics: Mapping[str, Mapping[str, int]] | None = None
        if callable(aggregate_layer_search_metrics):
            raw_metrics = aggregate_layer_search_metrics(tenant_id=tenant_id, since_hours=since_hours)
        if raw_metrics is None:
            raw_metrics = self._aggregate_search_effectiveness_from_memory(tenant_id=tenant_id)

        snapshot: dict[str, dict[str, Any]] = {}
        for layer, bucket in raw_metrics.items():
            queries = max(0, int(bucket.get("queries") or 0))
            if queries <= 0:
                continue
            hit_queries = max(0, int(bucket.get("hit_queries") or 0))
            total_hits = max(0, int(bucket.get("total_hits") or 0))
            max_hits = max(0, int(bucket.get("max_hits") or 0))
            snapshot[str(layer)] = {
                "queries": queries,
                "hit_queries": hit_queries,
                "zero_hit_queries": max(0, queries - hit_queries),
                "hit_rate": round(hit_queries / queries, 4),
                "avg_hits": round(total_hits / queries, 4),
                "max_hits": max_hits,
            }
        return snapshot

    def _aggregate_search_effectiveness_from_memory(
        self,
        *,
        tenant_id: str | None,
    ) -> dict[str, dict[str, int]]:
        tenant_filter = (tenant_id or "").strip()
        metrics: dict[str, dict[str, int]] = {}
        for (search_tenant_id, layer), history in self._search_layer_query_hits.items():
            if tenant_filter and search_tenant_id != tenant_filter:
                continue
            if not history:
                continue
            bucket = metrics.setdefault(layer, {"queries": 0, "hit_queries": 0, "total_hits": 0, "max_hits": 0})
            bucket["queries"] += len(history)
            bucket["hit_queries"] += sum(1 for count in history if count > 0)
            bucket["total_hits"] += sum(history)
            bucket["max_hits"] = max(bucket["max_hits"], max(history))
        return metrics

    def batch_reindex(
        self,
        *,
        tenant_id: str | None = None,
        doc_ids: Sequence[str] | None = None,
        since_hours: float | None = None,
        include_embeddings: bool = False,
    ) -> dict[str, Any]:
        candidates = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        requested_docs = {str(doc_id).strip() for doc_id in (doc_ids or ()) if str(doc_id).strip()}
        latest_by_doc: dict[tuple[str, str], Any] = {}
        for job in candidates:
            key = (job.tenant_id or "default", job.doc_id)
            if key not in latest_by_doc:
                latest_by_doc[key] = job
        processed: list[dict[str, Any]] = []
        for job in latest_by_doc.values():
            if requested_docs and job.doc_id not in requested_docs:
                continue
            request = self._request_from_job(job)
            request.options["mode"] = _RERUN_CHUNKS_ONLY_MODE
            outcome = self.submit(request)
            processed.append(
                {
                    "doc_id": job.doc_id,
                    "tenant_id": job.tenant_id,
                    "job_id": outcome.job.job_id,
                    "mode": _RERUN_CHUNKS_ONLY_MODE,
                    "chunks": len(outcome.chunks),
                }
            )
            if include_embeddings:
                embed_request = self._request_from_job(outcome.job)
                embed_request.options["mode"] = _RERUN_EMBEDDINGS_ONLY_MODE
                embedded = self.submit(embed_request)
                processed.append(
                    {
                        "doc_id": job.doc_id,
                        "tenant_id": job.tenant_id,
                        "job_id": embedded.job.job_id,
                        "mode": _RERUN_EMBEDDINGS_ONLY_MODE,
                        "chunks": len(embedded.chunks),
                    }
                )
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "include_embeddings": bool(include_embeddings),
            "processed": processed,
            "documents": len({(item["tenant_id"], item["doc_id"]) for item in processed}),
        }

    def _check_quota_limit(self, request: ParseRequest) -> None:
        settings = self.settings.runtime
        if not bool(settings.quota_enforce):
            return
        tenant_id = (request.tenant_id or "default").strip() or "default"
        quota_key = (request.quota_key or "default").strip() or "default"
        limit_units = _resolve_quota_limit(
            quota_limits=settings.quota_limits,
            tenant_id=tenant_id,
            quota_key=quota_key,
            default_limit_units=int(settings.quota_default_limit_units),
        )
        if limit_units <= 0:
            return
        requested_units = max(1, int(request.quota_units or 1))
        since_hours: float | None = None
        if float(settings.quota_window_hours) > 0:
            since_hours = float(settings.quota_window_hours)
        usage = self.quota_usage(tenant_id=tenant_id, since_hours=since_hours)
        used_units = 0
        for item in usage.get("items", []):
            if str(item.get("quota_key") or "default") == quota_key:
                used_units = int(item.get("total_quota_units") or 0)
                break
        if used_units + requested_units > limit_units:
            self.event_aggregator.record_event(
                "quota_exceeded",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=request.doc_id,
                details={
                    "used_units": used_units,
                    "requested_units": requested_units,
                    "limit_units": limit_units,
                },
            )
            raise QuotaExceededError(
                tenant_id=tenant_id,
                quota_key=quota_key,
                limit_units=limit_units,
                used_units=used_units,
                requested_units=requested_units,
                window_hours=since_hours,
            )

    def runtime_metrics(
        self,
        *,
        tenant_id: str | None = None,
        sample_size: int = 200,
        since_hours: float | None = None,
    ) -> dict[str, Any]:
        bounded_size = max(1, min(5000, int(sample_size)))
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)[:bounded_size]
        if not jobs:
            return {
                "tenant_id": (tenant_id or "").strip() or None,
                "since_hours": float(since_hours) if since_hours is not None else None,
                "sample_size": bounded_size,
                "total_jobs": 0,
                "done_jobs": 0,
                "failed_jobs": 0,
                "active_jobs": 0,
                "failure_rate": 0.0,
                "durations_s": {
                    "count": 0,
                    "mean": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "p50": 0.0,
                    "p90": 0.0,
                    "p99": 0.0,
                },
            }

        done_jobs = [job for job in jobs if job.state == ParseJobState.DONE]
        failed_jobs = [job for job in jobs if job.state == ParseJobState.FAILED]
        active_jobs = [
            job
            for job in jobs
            if job.state in (ParseJobState.PENDING, ParseJobState.PARSING, ParseJobState.STRUCTURING, ParseJobState.EMBEDDING)
        ]
        durations = [duration for duration in (_job_duration_seconds(job) for job in done_jobs) if duration is not None]
        duration_stats = _duration_summary(durations)
        total_terminal = len(done_jobs) + len(failed_jobs)
        failure_rate = round((len(failed_jobs) / total_terminal), 4) if total_terminal > 0 else 0.0

        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "sample_size": bounded_size,
            "total_jobs": len(jobs),
            "done_jobs": len(done_jobs),
            "failed_jobs": len(failed_jobs),
            "active_jobs": len(active_jobs),
            "failure_rate": failure_rate,
            "durations_s": duration_stats,
        }

    def tenant_dashboard(
        self,
        *,
        tenant_id: str | None = None,
        sample_size: int = 200,
        recent_limit: int = 5,
        since_hours: float | None = None,
    ) -> dict[str, Any]:
        bounded_recent = max(1, min(100, int(recent_limit)))
        normalized_tenant = (tenant_id or "").strip() or None
        usage = self.quota_usage(tenant_id=normalized_tenant, since_hours=since_hours)
        metrics = self.runtime_metrics(
            tenant_id=normalized_tenant,
            sample_size=sample_size,
            since_hours=since_hours,
        )
        recent_jobs = [
            {
                "job_id": job.job_id,
                "doc_id": job.doc_id,
                "state": job.state.value,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "quota_key": job.quota_key,
                "quota_units": int(job.quota_units or 1),
            }
            for job in self.list_jobs(tenant_id=normalized_tenant, since_hours=since_hours)[:bounded_recent]
        ]
        # Add observability data
        recent_events = self.event_aggregator.get_events(
            limit=10,
            tenant_id_filter=normalized_tenant,
        )
        event_counters = self.event_aggregator.get_counters(
            tenant_id_filter=normalized_tenant,
        )
        return {
            "tenant_id": normalized_tenant,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "usage": usage,
            "metrics": metrics,
            "recent_jobs": recent_jobs,
            "observability": {
                "recent_events": recent_events,
                "event_counters": event_counters,
            },
        }

    def claim_next_job(self):
        return self.job_store.claim_next_job()

    def _resolve_latest_job(self, *, doc_id: str, tenant_id: str | None = None):
        jobs = self.list_jobs(doc_id=doc_id, tenant_id=tenant_id)
        if not jobs:
            return None
        return jobs[0]

    def _request_from_job(self, job):
        return ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
            tenant_id=job.tenant_id,
            quota_key=job.quota_key,
            quota_units=job.quota_units,
        )

    def _load_blocks_for_request(self, request: ParseRequest):
        if self._is_rerun_chunks_only(request) or self._is_rerun_embeddings_only(request):
            purpose = "reembed" if self._is_rerun_embeddings_only(request) else "rechunk"
            self._resolve_pipeline(request, purpose=purpose)
            blocks = tuple(
                self.job_store.get_blocks(
                    doc_id=request.doc_id,
                    tenant_id=request.tenant_id,
                )
            )
            if not blocks:
                raise LookupError(
                    f"No existing blocks found for doc_id={request.doc_id!r}; cannot rerun derived outputs"
                )
            self.event_logger.log(
                "reused_blocks",
                doc_id=request.doc_id,
                blocks=len(blocks),
            )
            return blocks

        pipeline = self._resolve_pipeline(request, purpose="parse")
        return tuple(pipeline.parse_blocks(request=request))

    def _load_document_for_request(
        self,
        request: ParseRequest,
        *,
        blocks: Sequence[Any],
    ) -> ParsedDocumentArtifact | None:
        if self.pipeline_registry is None:
            return None
        purpose = "reembed" if self._is_rerun_embeddings_only(request) else (
            "rechunk" if self._is_rerun_chunks_only(request) else "parse"
        )
        pipeline = self._resolve_pipeline(request, purpose=purpose)
        return pipeline.build_document(request=request, blocks=blocks)

    def _load_chunks_for_request(
        self,
        request: ParseRequest,
        *,
        blocks: Sequence[Any],
        document: ParsedDocumentArtifact | None = None,
    ):
        if self._is_rerun_embeddings_only(request):
            chunks = tuple(
                self.job_store.get_chunks(
                    doc_id=request.doc_id,
                    tenant_id=request.tenant_id,
                )
            )
            if not chunks:
                raise LookupError(
                    f"No existing chunks found for doc_id={request.doc_id!r}; cannot rerun embeddings only"
                )
            self.event_logger.log(
                "reused_chunks",
                doc_id=request.doc_id,
                chunks=len(chunks),
            )
            return chunks
        if document is not None:
            pipeline = self._resolve_pipeline(
                request,
                purpose="rechunk" if self._is_rerun_chunks_only(request) else "parse",
            )
            return tuple(pipeline.chunker.build(document=document))
        purpose = "rechunk" if self._is_rerun_chunks_only(request) else "parse"
        pipeline = self._resolve_pipeline(request, purpose=purpose)
        return pipeline.build_chunks(request=request, blocks=blocks)

    def _build_index_manifest(
        self,
        *,
        request: ParseRequest,
        job_id: str,
        document: ParsedDocumentArtifact | None,
        chunks: Sequence[Chunk],
    ) -> dict[str, Any]:
        pipeline_name = str(getattr(document, "pipeline_name", "") or "")
        options_hash = str(getattr(document, "options_hash", "") or "")
        structure_roles = sorted(
            {
                str(getattr(item, "semantic_role", "") or "paragraph")
                for item in tuple(getattr(document, "items", ()) or ())
            }
        )
        chunk_roles = sorted({str(chunk.semantic_role or "paragraph") for chunk in chunks})
        index_version = options_hash or job_id
        embedding_tiers = _resolve_embedding_tiers(request.options)
        high_precision_chunks = _select_high_precision_chunks(chunks)
        layers: list[dict[str, Any]] = [
            {
                "name": "primary",
                "kind": "chunk",
                "version": index_version,
                "item_count": len(chunks),
                "semantic_roles": chunk_roles,
                "embedding_tier": embedding_tiers[0],
            },
            {
                "name": "structure",
                "kind": "typed-item",
                "version": index_version,
                "item_count": len(tuple(getattr(document, "items", ()) or ())),
                "semantic_roles": structure_roles,
            },
        ]
        if "large" in embedding_tiers:
            layers.append(
                {
                    "name": "high_precision",
                    "kind": "chunk",
                    "version": index_version,
                    "item_count": len(high_precision_chunks),
                    "semantic_roles": sorted({str(chunk.semantic_role or "paragraph") for chunk in high_precision_chunks}),
                    "embedding_tier": "large",
                    "chunk_ids": [chunk.chunk_id for chunk in high_precision_chunks],
                }
            )
        return {
            "doc_id": request.doc_id,
            "tenant_id": request.tenant_id,
            "pipeline_name": pipeline_name,
            "options_hash": options_hash,
            "index_version": index_version,
            "embedding_tiers": embedding_tiers,
            "layers": layers,
        }

    @staticmethod
    def _structure_entries_from_blocks(*, doc_id: str, blocks: Sequence[Any]) -> tuple[dict[str, Any], ...]:
        entries: list[dict[str, Any]] = []
        for index, block in enumerate(blocks, start=1):
            metadata = getattr(block, "metadata", {}) or {}
            semantic_role = str(metadata.get("semantic_role") or "paragraph").strip().lower()
            tags = _structure_tags_from_block_metadata(metadata=metadata, semantic_role=semantic_role)
            page_raw = metadata.get("page")
            try:
                page_number = int(page_raw) if page_raw is not None else None
            except (TypeError, ValueError):
                page_number = None
            entries.append(
                {
                    "item_id": f"itm-{index}",
                    "doc_id": doc_id,
                    "block_id": getattr(block, "block_id", f"blk-{index}"),
                    "text": getattr(block, "content", ""),
                    "semantic_role": semantic_role,
                    "structure_tags": tags,
                    "page_number": page_number,
                }
            )
        return tuple(entries)

    @staticmethod
    def _is_rerun_chunks_only(request: ParseRequest) -> bool:
        mode = str(request.options.get("mode", "")).strip().lower()
        return mode == _RERUN_CHUNKS_ONLY_MODE

    @staticmethod
    def _is_rerun_embeddings_only(request: ParseRequest) -> bool:
        mode = str(request.options.get("mode", "")).strip().lower()
        return mode == _RERUN_EMBEDDINGS_ONLY_MODE

    def _resolve_parser(self, request: ParseRequest) -> ParserAdapter:
        suffix = Path(request.file_path).suffix.lower()
        for parser in self.parsers:
            if parser.supports(media_type=request.media_type, suffix=suffix):
                return parser
        raise LookupError(f"No parser registered for media_type={request.media_type!r}, suffix={suffix!r}")

    def _resolve_pipeline(self, request: ParseRequest, *, purpose: str = "parse"):
        if self.pipeline_registry is not None:
            return self.pipeline_registry.resolve(request, purpose=purpose)
        parser = self._resolve_parser(request)
        if purpose not in {"parse", "rechunk", "reembed"}:
            raise LookupError(f"Unsupported pipeline purpose={purpose!r}")
        raise LookupError(
            f"No pipeline registry available for parser={parser.name!r}; rebuild runtime with pipeline registry support"
        )

    def _record_ocr_observability(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Any],
    ) -> None:
        attempted_pages: set[int] = set()
        fallback_pages: set[int] = set()
        failed_pages: set[int] = set()
        attempted_blocks = 0
        fallback_blocks = 0
        failed_blocks = 0
        attempt_reasons: set[str] = set()
        error_reasons: set[str] = set()

        for block in blocks:
            metadata = getattr(block, "metadata", {}) or {}
            try:
                page_number = int(metadata.get("page", 0) or 0)
            except (TypeError, ValueError):
                page_number = 0

            if bool(metadata.get("ocr_attempted")):
                attempted_pages.add(page_number)
                attempted_blocks += 1
                attempt_reason = metadata.get("ocr_attempt_reason")
                if attempt_reason:
                    attempt_reasons.add(str(attempt_reason))

            if bool(metadata.get("ocr_fallback_used")):
                fallback_pages.add(page_number)
                fallback_blocks += 1

            error_reason = metadata.get("ocr_error_reason")
            if error_reason:
                failed_pages.add(page_number)
                failed_blocks += 1
                error_reasons.add(str(error_reason))

        if attempted_pages:
            self.event_aggregator.record_event(
                "ocr_attempted",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": len(attempted_pages),
                    "block_count": attempted_blocks,
                    "attempt_reasons": sorted(attempt_reasons),
                },
                count=len(attempted_pages),
            )

        if fallback_pages:
            self.event_aggregator.record_event(
                "ocr_fallback",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": len(fallback_pages),
                    "block_count": fallback_blocks,
                },
                count=len(fallback_pages),
            )

        if failed_pages:
            self.event_aggregator.record_event(
                "ocr_failed",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": len(failed_pages),
                    "block_count": failed_blocks,
                    "error_reasons": sorted(error_reasons),
                },
                count=len(failed_pages),
            )

    def _embed_chunks(self, *, doc_id: str, chunks: Sequence[Any]) -> Sequence[Any]:
        if not chunks:
            return tuple(chunks)
        provider_settings = self.settings.providers.embedding
        batch_size = max(1, int(provider_settings.batch_size or len(chunks)))
        max_retries = max(0, int(provider_settings.max_retries))
        base_backoff_seconds = 0.05
        embedded: list[Any] = []
        failed_batches = 0
        for batch_index, start in enumerate(range(0, len(chunks), batch_size), start=1):
            batch = tuple(chunks[start : start + batch_size])
            result = self._embed_batch_with_retry(
                doc_id=doc_id,
                batch=batch,
                batch_index=batch_index,
                max_retries=max_retries,
                base_backoff_seconds=base_backoff_seconds,
            )
            if result is None:
                failed_batches += 1
                embedded.extend(batch)
            else:
                embedded.extend(result)

        if failed_batches > 0:
            self.event_logger.log(
                "embedding_skipped",
                doc_id=doc_id,
                chunks=len(chunks),
                failed_batches=failed_batches,
                total_batches=((len(chunks) + batch_size - 1) // batch_size),
            )
        return tuple(embedded)

    def _embed_batch_with_retry(
        self,
        *,
        doc_id: str,
        batch: Sequence[Any],
        batch_index: int,
        max_retries: int,
        base_backoff_seconds: float,
    ) -> tuple[Any, ...] | None:
        attempts = max(1, max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                embedded_batch = tuple(self.embedding_provider.embed(doc_id=doc_id, chunks=batch))
                if len(embedded_batch) != len(batch):
                    raise RuntimeError(
                        f"embedding_provider_mismatch: got {len(embedded_batch)} for {len(batch)}"
                    )
                return embedded_batch
            except Exception as exc:
                if attempt < attempts:
                    delay = min(base_backoff_seconds * (2 ** (attempt - 1)), 0.5)
                    self.event_logger.log(
                        "embedding_retry",
                        doc_id=doc_id,
                        batch_index=batch_index,
                        attempt=attempt,
                        max_attempts=attempts,
                        delay_s=round(delay, 3),
                        error=str(exc),
                    )
                    self.event_aggregator.record_event(
                        "embedding_retry",
                        doc_id=doc_id,
                        details={
                            "batch_index": batch_index,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "delay_s": round(delay, 3),
                        },
                    )
                    time.sleep(delay)
                    continue
                self.event_logger.log(
                    "embedding_batch_skipped",
                    doc_id=doc_id,
                    batch_index=batch_index,
                    attempts=attempts,
                    chunks=len(batch),
                    error=str(exc),
                )
                self.event_aggregator.record_event(
                    "embedding_skipped",
                    doc_id=doc_id,
                    details={
                        "batch_index": batch_index,
                        "attempts": attempts,
                        "chunks": len(batch),
                    },
                )
                return None


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_PATTERN.findall(text or "") if token.strip()]


def _job_duration_seconds(job: Any) -> float | None:
    created_at = getattr(job, "created_at", None)
    updated_at = getattr(job, "updated_at", None)
    if not created_at or not updated_at:
        return None
    try:
        started = datetime.fromisoformat(str(created_at))
        finished = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return None
    delta = (finished - started).total_seconds()
    if delta < 0:
        return None
    return float(delta)


def _duration_summary(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {
            "count": 0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
        }
    ordered = sorted(float(item) for item in values)
    count = len(ordered)
    mean = sum(ordered) / count
    return {
        "count": count,
        "mean": round(mean, 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p90": round(_percentile(ordered, 0.90), 3),
        "p99": round(_percentile(ordered, 0.99), 3),
    }


def _percentile(sorted_values: Sequence[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    target = max(0.0, min(1.0, float(ratio)))
    index = int(round(target * (len(sorted_values) - 1)))
    return float(sorted_values[index])


def _filter_jobs_by_since_hours(jobs: Sequence[Any], *, since_hours: float | None) -> tuple[Any, ...]:
    if since_hours is None:
        return tuple(jobs)
    hours = float(since_hours)
    if hours <= 0:
        return ()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered: list[Any] = []
    for job in jobs:
        updated = _parse_datetime(getattr(job, "updated_at", None))
        if updated is None:
            continue
        if updated >= cutoff:
            filtered.append(job)
    return tuple(filtered)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_quota_limit(
    *,
    quota_limits: dict[str, Any] | Any,
    tenant_id: str,
    quota_key: str,
    default_limit_units: int,
) -> int:
    mapping = dict(quota_limits or {})
    candidates = (
        f"{tenant_id}:{quota_key}",
        f"{tenant_id}:*",
        f"*:{quota_key}",
        "*:*",
    )
    for key in candidates:
        if key in mapping:
            try:
                return int(mapping[key])
            except (TypeError, ValueError):
                continue
    return int(default_limit_units)


def _score_chunk(
    *,
    query: str,
    query_embedding: tuple[float, ...] | None,
    chunk: Chunk,
) -> float:
    keyword_score = _keyword_relevance_score(query=query, chunk=chunk)
    vector_score = _vector_relevance_score(query_embedding=query_embedding, chunk=chunk)

    if vector_score is not None:
        combined = (_SEARCH_VECTOR_WEIGHT * vector_score) + (
            _SEARCH_KEYWORD_WEIGHT * keyword_score
        )
    else:
        combined = keyword_score

    if combined <= 0:
        return 0.0

    role = str(chunk.semantic_role or "paragraph").strip().lower()
    role_weight = _SEMANTIC_ROLE_WEIGHTS.get(role, 1.0)
    return combined * role_weight


def _keyword_relevance_score(*, query: str, chunk: Chunk) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    text = str(chunk.text or "")
    text_lower = text.lower()
    text_tokens = _tokenize(text)
    if not text_tokens:
        return 0.0

    overlap = 0.0
    for token in query_tokens:
        count = text_tokens.count(token)
        if count > 0:
            overlap += 1.0 + min(count - 1, 2) * 0.25
    if overlap <= 0:
        return 0.0

    token_coverage = overlap / max(len(query_tokens), 1)
    phrase_boost = 0.25 if query.strip().lower() in text_lower else 0.0
    density_boost = min(overlap / max(len(text_tokens), 1), 0.25)
    return min(token_coverage + phrase_boost + density_boost, 1.0)


def _vector_relevance_score(
    *,
    query_embedding: tuple[float, ...] | None,
    chunk: Chunk,
) -> float | None:
    if query_embedding is None or chunk.embedding is None:
        return None
    chunk_embedding = tuple(float(item) for item in chunk.embedding)
    if len(query_embedding) != len(chunk_embedding):
        return None
    similarity = _cosine_similarity(query_embedding, chunk_embedding)
    return max(0.0, float(similarity))


def _resolve_retrieval_mode(
    *,
    query_embedding: tuple[float, ...] | None,
    chunks: Sequence[Chunk],
) -> str:
    if query_embedding is None:
        return "keyword-fallback"
    for chunk in chunks:
        if chunk.embedding is None:
            continue
        if len(chunk.embedding) == len(query_embedding):
            return "hybrid"
    return "keyword-fallback"



def _structure_tags_from_block_metadata(*, metadata: dict[str, Any], semantic_role: str) -> tuple[str, ...]:
    tags = [f"role:{semantic_role}"]
    page_type = str(metadata.get("page_type") or "").strip().lower()
    if page_type:
        tags.append(f"page:{page_type}")
    parser_name = str(metadata.get("parser") or "").strip().lower()
    if parser_name:
        tags.append(f"parser:{parser_name}")
    kind = str(metadata.get("kind") or "").strip().lower()
    if kind:
        tags.append(f"kind:{kind}")
    return tuple(dict.fromkeys(tags))


def _score_structure_entry(
    *,
    query: str,
    text: str,
    semantic_role: str,
    tags: Sequence[str],
) -> float:
    keyword_score = _keyword_match_score(query=query, text=text)
    if keyword_score <= 0.0:
        return 0.0
    role_weight = _SEMANTIC_ROLE_WEIGHTS.get(semantic_role, 1.0)
    tag_bonus = 0.02 * len(tuple(tags))
    return keyword_score * role_weight + tag_bonus


def _keyword_match_score(*, query: str, text: str) -> float:
    query_tokens = _tokenize(query)
    text_tokens = _tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = sum(1 for token in query_tokens if token in text_tokens)
    if overlap <= 0:
        return 0.0
    return overlap / max(len(query_tokens), 1)


def _tokenize(text: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN_PATTERN.findall(str(text or "")))


def _is_task_like_entry(text: str, semantic_role: str, tags: Sequence[str]) -> bool:
    normalized_role = str(semantic_role or "paragraph").strip().lower()
    if normalized_role in {"warning", "caution", "note"}:
        return True
    if any(str(tag).strip().lower().startswith("page:toc") for tag in tags):
        return False
    return _TASK_LIKE_PATTERN.search(str(text or "").strip()) is not None


def _resolve_embedding_tiers(options: dict[str, Any]) -> list[str]:
    index_options = options.get("index") if isinstance(options, dict) else None
    raw_tiers = None
    if isinstance(index_options, dict):
        raw_tiers = index_options.get("embedding_tiers")
    if raw_tiers is None:
        return ["small"]
    if isinstance(raw_tiers, (list, tuple)):
        tiers = [str(item).strip().lower() for item in raw_tiers if str(item).strip()]
    else:
        tiers = [str(raw_tiers).strip().lower()]
    normalized = [tier for tier in tiers if tier in {"small", "large"}]
    return normalized or ["small"]


def _normalize_chunk_index_layer(value: str | None) -> str:
    normalized = str(value or "primary").strip().lower()
    if normalized in {"primary", "high_precision"}:
        return normalized
    return "primary"


def _normalize_trend_windows_hours(values: Sequence[float] | None) -> tuple[float, ...]:
    if not values:
        return (1.0, 6.0, 24.0)
    normalized: list[float] = []
    for raw in values:
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            continue
        if hours <= 0:
            continue
        rounded = round(hours, 3)
        if rounded in normalized:
            continue
        normalized.append(rounded)
    if not normalized:
        return (1.0, 6.0, 24.0)
    return tuple(sorted(normalized))


def _format_trend_window_label(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)}h"
    return f"{hours:g}h"


def _select_high_precision_chunks(chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
    selected: list[Chunk] = []
    for chunk in chunks:
        role = str(chunk.semantic_role or "paragraph").strip().lower()
        text = str(chunk.text or "").strip()
        if role in {"title", "warning", "caution", "note", "table"}:
            selected.append(chunk)
            continue
        if len(text) >= 120:
            selected.append(chunk)
    return tuple(selected)


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = sqrt(sum(a * a for a in vec_a))
    norm_b = sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
