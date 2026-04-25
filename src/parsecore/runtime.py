from __future__ import annotations

import re
import time
from math import sqrt
from pathlib import Path
from typing import Any, Sequence

from .config import ParseCoreSettings
from .contracts import ChunkBuilder, EmbeddingProvider, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, TranslationAdapter
from .events import JobEventLogger
from .models import Chunk, ChunkSearchHit, ParseJobState, ParseOutcome, ParseRequest


_RERUN_CHUNKS_ONLY_MODE = "rerun_chunks_only"
_RERUN_EMBEDDINGS_ONLY_MODE = "rerun_embeddings_only"
_SEARCH_VECTOR_WEIGHT = 0.7
_SEARCH_KEYWORD_WEIGHT = 0.3
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

    def describe(self) -> dict[str, object]:
        return {
            "project": self.settings.project_name,
            "mode": self.settings.mode,
            "database_url": self.settings.database_url,
            "object_store": self.settings.object_store,
            "index_mode": self.settings.index_mode,
            "runtime": {
                "execution_mode": self.settings.runtime.execution_mode,
                "max_workers": self.settings.runtime.max_workers,
                "poll_interval_ms": self.settings.runtime.poll_interval_ms,
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
        }

    def submit(self, request: ParseRequest) -> ParseOutcome:
        job = self.start(request)
        return self.execute(job_id=job.job_id)

    def start(self, request: ParseRequest):
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
            if not self._is_rerun_chunks_only(request):
                self.job_store.save_blocks(doc_id=request.doc_id, blocks=blocks)
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.STRUCTURING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.STRUCTURING.value,
            )

            chunks = tuple(self._load_chunks_for_request(request, blocks=blocks))
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.EMBEDDING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.EMBEDDING.value,
            )
            chunks = tuple(self._embed_chunks(doc_id=request.doc_id, chunks=chunks))

            self.job_store.save_chunks(doc_id=request.doc_id, chunks=chunks)
            self.index.upsert(doc_id=request.doc_id, chunks=chunks)
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

    def get_document(self, *, doc_id: str) -> dict[str, Any]:
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        return {
            "doc_id": doc_id,
            "job": latest_job,
            "blocks": tuple(self.job_store.get_blocks(doc_id=doc_id)),
            "chunks": tuple(self.job_store.get_chunks(doc_id=doc_id)),
        }

    def search_document(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
    ) -> tuple[ChunkSearchHit, ...]:
        hits, _mode = self.search_document_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=semantic_roles,
        )
        return hits

    def search_document_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
    ) -> tuple[tuple[ChunkSearchHit, ...], str]:
        chunks = tuple(self.job_store.get_chunks(doc_id=doc_id))
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
        return tuple(ranked[: max(1, int(limit))]), retrieval_mode

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

    def retry_latest(self, *, doc_id: str) -> ParseOutcome:
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.submit(self._request_from_job(latest_job))

    def restart_latest(self, *, doc_id: str):
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.start(self._request_from_job(latest_job))

    def rechunk_latest(self, *, doc_id: str):
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_CHUNKS_ONLY_MODE
        return self.start(request)

    def reembed_latest(self, *, doc_id: str):
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_EMBEDDINGS_ONLY_MODE
        return self.start(request)

    def list_jobs(self, *, doc_id: str | None = None):
        return tuple(self.job_store.list_jobs(doc_id=doc_id))

    def claim_next_job(self):
        return self.job_store.claim_next_job()

    def _request_from_job(self, job):
        return ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
        )

    def _load_blocks_for_request(self, request: ParseRequest):
        if self._is_rerun_chunks_only(request) or self._is_rerun_embeddings_only(request):
            blocks = tuple(self.job_store.get_blocks(doc_id=request.doc_id))
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

        parser = self._resolve_parser(request)
        return tuple(parser.parse(request))

    def _load_chunks_for_request(self, request: ParseRequest, *, blocks: Sequence[Any]):
        if self._is_rerun_embeddings_only(request):
            chunks = tuple(self.job_store.get_chunks(doc_id=request.doc_id))
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
        return self.chunk_builder.build(doc_id=request.doc_id, blocks=blocks)

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

    def _embed_chunks(self, *, doc_id: str, chunks: Sequence[Any]) -> Sequence[Any]:
        try:
            return self.embedding_provider.embed(doc_id=doc_id, chunks=chunks)
        except Exception as exc:
            self.event_logger.log(
                "embedding_skipped",
                doc_id=doc_id,
                error=str(exc),
                chunks=len(chunks),
            )
            return tuple(chunks)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_PATTERN.findall(text or "") if token.strip()]


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


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = sqrt(sum(a * a for a in vec_a))
    norm_b = sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
