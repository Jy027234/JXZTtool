from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .capabilities import (
    CapabilityConfigurationError,
    canonical_index_mode,
    database_scheme,
    index_backend_for,
    known_index_modes,
    validate_capability_configuration,
)
from .config import load_settings
from .embeddings import EmbeddingConfigurationError, build_embedding_provider
from .llm import LlmBoundaryRefiner, LlmConfigurationError, build_llm_client
from .parsers import build_parser, build_parser_or_placeholder
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
    raise CapabilityConfigurationError(
        code="unknown_database_scheme",
        field="storage.database_url",
        message=(
            f"Unsupported database_url scheme: {database_url!r}. "
            "Expected sqlite:///..., postgresql://..., or memory://"
        ),
    )


def _build_index(
    database_url: str,
    index_mode: str,
    *,
    embedding_dimension: int = 1536,
):
    """Build the configured index backend.

    P0-01: an unknown index mode is rejected, and a mode that promises a durable
    vector backend (``pgvector``) is never silently replaced by an in-memory
    index.  A ``null`` result is legitimate only for ``null``/``hybrid``/
    ``memory`` configurations and is reported as a degraded capability.
    """

    mode = canonical_index_mode(index_mode)
    if mode not in ("pgvector", "hybrid", "null"):
        raise CapabilityConfigurationError(
            code="unknown_index_mode",
            field="index.mode",
            message=(
                f"unknown index mode: {index_mode!r}. Supported: "
                + ", ".join(known_index_modes())
            ),
        )
    if mode == "pgvector" and index_backend_for(database_url, mode) != "pgvector":
        raise CapabilityConfigurationError(
            code="index_backend_mismatch",
            field="index.backend",
            message=(
                f"index.mode = 'pgvector' requires a postgresql database_url; got "
                f"{database_scheme(database_url)!r}. Refusing to substitute a "
                "non-durable in-memory index."
            ),
        )
    if mode == "pgvector":
        return PgVectorIndex(database_url, dim=embedding_dimension)
    # "null" / "hybrid" without pg / "memory" all use the in-memory index.
    return NullIndex()


def _build_parsers(
    settings,
    *,
    boundary_refiner: Any,
    semantic_refiner: Any,
) -> list[Any]:
    """Build one parser per ``[[parsers]]`` entry.

    P0-01: an unknown name is rejected unless the deployment explicitly allowed
    placeholder parsers.  A placeholder is then a real, reported state: the
    runtime marks it through ``is_placeholder`` so it can never be presented as
    a formal implementation.
    """

    parsers = []
    for item in settings.parsers:
        try:
            parsers.append(
                build_parser(
                    item.name,
                    media_types=item.media_types,
                    extensions=item.extensions,
                    options=item.options,
                    ocr_provider_settings=settings.providers.ocr,
                    boundary_refiner=boundary_refiner if item.name == "pdf-text" else None,
                    semantic_refiner=semantic_refiner if item.name == "pdf-text" else None,
                )
            )
        except KeyError:
            if not settings.allow_placeholder_parsers:
                raise CapabilityConfigurationError(
                    code="unknown_parser_name",
                    field="parsers.name",
                    message=(
                        f"unknown parser implementation {item.name!r}; set "
                        "project.allow_placeholder_parsers = true only for a "
                        "development deployment that does not serve formal results"
                    ),
                ) from None
            # No extra degradation record is needed: build_capability_report
            # reports the placeholder from the parser that was actually built.
            parsers.append(
                build_parser_or_placeholder(
                    item.name,
                    media_types=item.media_types,
                    extensions=item.extensions,
                    options=item.options,
                    ocr_provider_settings=settings.providers.ocr,
                )
            )
    return parsers


def _build_embedding_provider(settings):
    """Build the embedding provider, or a no-op when it is unavailable.

    P0-01: a failed build is not hidden here -- ``build_capability_report``
    derives the degradation from the provider that was actually created, so the
    report stays the single place that explains capability state.
    """

    provider = settings.providers.embedding
    # Always go through the factory: hosts and tests inject an embedding
    # provider by patching ``build_embedding_provider``, and that injection must
    # keep working even when the config has embedding disabled.
    try:
        built = build_embedding_provider(provider)
    except EmbeddingConfigurationError:
        return NullEmbeddingProvider()
    return built if built is not None else NullEmbeddingProvider()


def _build_rerank_provider(settings):
    provider = settings.providers.rerank
    try:
        built = build_rerank_provider(provider)
    except RerankConfigurationError:
        return NullRerankProvider()
    return built if built is not None else NullRerankProvider()


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
        # ``database_url_override`` may reintroduce a mismatch (for example a
        # SQLite test URL with ``index.mode = 'pgvector'``), so re-validate.
        validate_capability_configuration(settings)

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

    parsers = _build_parsers(
        settings,
        boundary_refiner=boundary_refiner,
        semantic_refiner=semantic_refiner,
    )
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
    embedding_provider = _build_embedding_provider(settings)
    rerank_provider = _build_rerank_provider(settings)
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
