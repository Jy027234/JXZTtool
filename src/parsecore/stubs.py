from __future__ import annotations

from dataclasses import asdict
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .contracts import ChunkBuilder, EmbeddingProvider, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, TranslationAdapter
from .models import Block, BlockType, Chunk, ParseJob, ParseJobState, ParseOutcome, ParseRequest, SemanticRole


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    """Deterministic test helper that stamps a tiny embedding per chunk."""

    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        embedded: list[Chunk] = []
        for index, chunk in enumerate(chunks, start=1):
            embedded.append(
                replace(chunk, embedding=(float(index), float(len(chunk.text))))
            )
        return tuple(embedded)


class NullIndex(IndexAdapter):
    def __init__(self) -> None:
        self.upserts: list[dict[str, object]] = []

    def upsert(self, *, doc_id: str, chunks: Sequence[Chunk]) -> None:
        self.upserts.append({"doc_id": doc_id, "chunks": len(chunks)})


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
        self.blocks_by_doc: dict[str, tuple[Block, ...]] = {}
        self.chunks_by_doc: dict[str, tuple[Chunk, ...]] = {}

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
        self.jobs[job.job_id] = job
        return job

    def update_state(
        self,
        *,
        job_id: str,
        state: ParseJobState,
        failure_reason: str | None = None,
    ) -> ParseJob:
        job = self.jobs[job_id]
        job.state = state
        job.failure_reason = failure_reason
        job.updated_at = _utc_now()
        return job

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block]) -> None:
        self.blocks_by_doc[doc_id] = tuple(blocks)

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk]) -> None:
        self.chunks_by_doc[doc_id] = tuple(chunks)

    def claim_next_job(self) -> ParseJob | None:
        pending = [job for job in self.jobs.values() if job.state == ParseJobState.PENDING]
        if not pending:
            return None
        job = sorted(pending, key=lambda item: item.created_at)[0]
        job.state = ParseJobState.PARSING
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

    def get_blocks(self, *, doc_id: str) -> Sequence[Block]:
        return self.blocks_by_doc.get(doc_id, ())

    def get_chunks(self, *, doc_id: str) -> Sequence[Chunk]:
        return self.chunks_by_doc.get(doc_id, ())

    def increment_attempt(self, *, job_id: str) -> int:
        job = self.jobs[job_id]
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.updated_at = _utc_now()
        return job.attempt_count

    def mark_dead_letter(self, *, job_id: str, reason: str) -> ParseJob:
        job = self.jobs[job_id]
        job.state = ParseJobState.FAILED
        job.failure_reason = reason
        job.dead_lettered_at = _utc_now()
        job.updated_at = job.dead_lettered_at
        return job

    def snapshot(self) -> dict[str, object]:
        return {
            "jobs": {job_id: asdict(job) for job_id, job in self.jobs.items()},
            "documents": sorted(self.blocks_by_doc.keys()),
        }

