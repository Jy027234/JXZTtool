from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import Block, Chunk, ParseJob, ParseJobState, ParseOutcome, ParseRequest


@runtime_checkable
class ParserAdapter(Protocol):
    name: str

    def supports(self, *, media_type: str | None, suffix: str) -> bool: ...

    def parse(self, request: ParseRequest) -> Sequence[Block]: ...


@runtime_checkable
class ChunkBuilder(Protocol):
    def build(self, *, doc_id: str, blocks: Sequence[Block]) -> Sequence[Chunk]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]: ...


@runtime_checkable
class IndexAdapter(Protocol):
    def upsert(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        tenant_id: str | None = None,
        document: Any | None = None,
        index_manifest: Mapping[str, Any] | None = None,
    ) -> None: ...

    def describe_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
    ) -> Mapping[str, Any] | None: ...

    def get_layer_chunks(
        self,
        *,
        doc_id: str,
        layer: str,
        tenant_id: str | None = None,
    ) -> Sequence[Chunk] | None: ...

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
        document: Any | None = None,
        index_manifest: Mapping[str, Any] | None = None,
    ) -> None: ...


@runtime_checkable
class TranslationAdapter(Protocol):
    def translate(self, *, text: str, target_lang: str) -> str: ...


@runtime_checkable
class BoundaryRefiner(Protocol):
    """Optional LLM-driven boundary refiner for low-confidence paragraphs.

    Hosts that want LLM-based boundary repair inject an implementation; the
    parsecore library never instantiates LLM clients itself.
    """

    def refine(self, *, paragraph: str, context: Mapping[str, Any] | None = None) -> str: ...


@runtime_checkable
class SemanticRefiner(Protocol):
    """Optional vector-based semantic refiner for paragraph boundary decisions.

    Hosts inject a callable that, given two adjacent paragraph candidates,
    returns a similarity score in [0, 1].  Parsecore uses the score to decide
    whether to merge or split without ever loading a vector model directly.

    This keeps parsecore embeddable: the host product owns model loading,
    GPU/CPU placement, batching, and credential management.
    """

    def similarity(self, *, left: str, right: str) -> float: ...


@runtime_checkable
class ProductAdapter(Protocol):
    def before_parse(self, *, request: ParseRequest, job: ParseJob) -> None: ...

    def after_parse(self, *, outcome: ParseOutcome) -> None: ...

    def on_failure(self, *, request: ParseRequest, job: ParseJob, error: Exception) -> None: ...


@runtime_checkable
class JobStore(Protocol):
    def create(self, request: ParseRequest) -> ParseJob: ...

    def update_state(
        self,
        *,
        job_id: str,
        state: ParseJobState,
        failure_reason: str | None = None,
        expected_claim_token: str | None = None,
        clear_claim: bool = False,
        next_attempt_at: str | None = None,
    ) -> ParseJob: ...

    def save_blocks(self, *, doc_id: str, blocks: Sequence[Block], tenant_id: str | None = None) -> None: ...

    def save_chunks(self, *, doc_id: str, chunks: Sequence[Chunk], tenant_id: str | None = None) -> None: ...

    def replace_blocks_by_prefix(
        self,
        *,
        doc_id: str,
        blocks: Sequence[Block],
        block_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None: ...

    def replace_chunks_by_prefix(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Chunk],
        chunk_id_prefix: str,
        tenant_id: str | None = None,
    ) -> None: ...

    def claim_next_job(self) -> ParseJob | None: ...

    def claim_job(self, *, job_id: str, lease_expires_at: str | None = None) -> ParseJob | None: ...

    def update_options(self, *, job_id: str, options: Mapping[str, Any]) -> ParseJob: ...

    def get_job(self, *, job_id: str) -> ParseJob | None: ...

    def get_latest_job(self, *, doc_id: str) -> ParseJob | None: ...

    def list_jobs(self, *, doc_id: str | None = None) -> Sequence[ParseJob]: ...

    def get_blocks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Block]: ...

    def get_chunks(self, *, doc_id: str, tenant_id: str | None = None) -> Sequence[Chunk]: ...

    def record_layer_search_hit(
        self,
        *,
        tenant_id: str | None,
        layer: str,
        hit_count: int,
    ) -> None: ...

    def aggregate_layer_search_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
    ) -> Mapping[str, Mapping[str, int]]: ...
