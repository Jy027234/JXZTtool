from __future__ import annotations

from pathlib import Path

from .config import load_settings
from .embeddings import EmbeddingConfigurationError, build_embedding_provider
from .jobcard import JobcardProductAdapter
from .llm import LlmBoundaryRefiner, LlmConfigurationError, build_llm_client
from .parsers import build_parser
from .runtime import ParseRuntime
from .stores import PgVectorIndex, PostgresJobStore, SQLiteJobStore
from .stubs import EchoTranslator, EmbeddedProductAdapter, InMemoryJobStore, NullEmbeddingProvider, NullIndex, ParagraphChunkBuilder


def _build_job_store(database_url: str):
    if database_url.startswith("sqlite:///"):
        return SQLiteJobStore(database_url)
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresJobStore(database_url)
    if database_url in ("", "memory://", "memory"):
        return InMemoryJobStore()
    raise ValueError(
        f"Unsupported database_url scheme: {database_url!r}. "
        "Expected sqlite:///..., postgresql://..., or memory://"
    )


def _build_index(database_url: str, index_mode: str):
    mode = (index_mode or "").strip().lower()
    if mode in ("pgvector", "hybrid") and database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        return PgVectorIndex(database_url)
    # "null" / "hybrid" without pg / "memory" all fall back to NullIndex.
    return NullIndex()


def build_runtime(config_path: str | Path) -> ParseRuntime:
    settings = load_settings(config_path)

    boundary_refiner = None
    if settings.providers.llm.enabled:
        try:
            client = build_llm_client(settings.providers.llm)
        except LlmConfigurationError:
            client = None
        if client is not None:
            max_calls = int(
                settings.providers.llm.options.get("max_calls_per_doc", 50)
            )
            boundary_refiner = LlmBoundaryRefiner(
                client, max_calls_per_doc=max_calls
            )

    parsers = [
        build_parser(
            item.name,
            media_types=item.media_types,
            extensions=item.extensions,
            options=item.options,
            boundary_refiner=boundary_refiner if item.name == "pdf-text" else None,
        )
        for item in settings.parsers
    ]
    job_store = _build_job_store(settings.database_url)
    index = _build_index(settings.database_url, settings.index_mode)
    if settings.product_adapter == "jobcard":
        product_adapter = JobcardProductAdapter()
    else:
        product_adapter = EmbeddedProductAdapter()
    try:
        embedding_provider = (
            build_embedding_provider(settings.providers.embedding)
            or NullEmbeddingProvider()
        )
    except EmbeddingConfigurationError:
        embedding_provider = NullEmbeddingProvider()
    return ParseRuntime(
        settings=settings,
        parsers=parsers,
        chunk_builder=ParagraphChunkBuilder(),
        embedding_provider=embedding_provider,
        index=index,
        translator=EchoTranslator(),
        product_adapter=product_adapter,
        job_store=job_store,
    )
