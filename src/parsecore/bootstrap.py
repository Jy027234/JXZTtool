from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import load_settings
from .embeddings import EmbeddingConfigurationError, build_embedding_provider
from .llm import LlmBoundaryRefiner, LlmConfigurationError, build_llm_client
from .parsers import build_parser
from .pipelines import build_pipeline_registry
from .rerank import RerankConfigurationError, build_rerank_provider
from .runtime import ParseRuntime
from .stores import PgVectorIndex, PostgresJobStore, SQLiteJobStore
from .stubs import EchoTranslator, EmbeddedProductAdapter, InMemoryJobStore, NullEmbeddingProvider, NullIndex, NullRerankProvider, ParagraphChunkBuilder


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


def _build_index(
    database_url: str,
    index_mode: str,
    *,
    embedding_dimension: int = 1536,
):
    mode = (index_mode or "").strip().lower()
    if mode in ("pgvector", "hybrid") and database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        return PgVectorIndex(database_url, dim=embedding_dimension)
    # "null" / "hybrid" without pg / "memory" all fall back to NullIndex.
    return NullIndex()


def build_runtime(
    config_path: str | Path,
    *,
    semantic_refiner: Any = None,
    database_url_override: str | None = None,
) -> ParseRuntime:
    settings = load_settings(config_path)
    if database_url_override is not None:
        normalized_database_url = str(database_url_override).strip()
        if not normalized_database_url:
            raise ValueError("database_url_override must not be blank")
        settings = replace(settings, database_url=normalized_database_url)

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
            ocr_provider_settings=settings.providers.ocr,
            boundary_refiner=boundary_refiner if item.name == "pdf-text" else None,
            semantic_refiner=semantic_refiner if item.name == "pdf-text" else None,
        )
        for item in settings.parsers
    ]
    chunk_builder = ParagraphChunkBuilder()
    pipeline_registry = build_pipeline_registry(
        parser_settings=settings.parsers,
        parsers=parsers,
        chunk_builder=chunk_builder,
    )
    pipeline_registry.warmup()
    job_store = _build_job_store(settings.database_url)
    index = _build_index(
        settings.database_url,
        settings.index_mode,
        embedding_dimension=settings.index_embedding_dimension,
    )
    product_adapter = EmbeddedProductAdapter()
    try:
        embedding_provider = (
            build_embedding_provider(settings.providers.embedding)
            or NullEmbeddingProvider()
        )
    except EmbeddingConfigurationError:
        embedding_provider = NullEmbeddingProvider()
    try:
        rerank_provider = (
            build_rerank_provider(settings.providers.rerank)
            or NullRerankProvider()
        )
    except RerankConfigurationError:
        rerank_provider = NullRerankProvider()
    return ParseRuntime(
        settings=settings,
        parsers=parsers,
        chunk_builder=chunk_builder,
        embedding_provider=embedding_provider,
        rerank_provider=rerank_provider,
        index=index,
        translator=EchoTranslator(),
        product_adapter=product_adapter,
        job_store=job_store,
        pipeline_registry=pipeline_registry,
    )
