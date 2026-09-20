"""Capability truthfulness for P0-01.

ParseCore must never silently substitute a placeholder implementation for a
configured parser, embedding backend, reranker, or index.  Historically an
unknown ``[[parsers]]`` name fell through to :class:`~parsecore.stubs.StubParser`
and the job still completed as ``done``; a ``pgvector`` index mode paired with a
SQLite database silently became :class:`~parsecore.stubs.NullIndex`.

This module is the single source of truth for three things:

* which implementation names ParseCore actually ships (and their aliases);
* which configuration combinations are rejected at startup, with stable error
  codes that callers can assert on;
* the capability report exposed by ``/health``, ``/v1/runtime`` and
  ``runtime.capability_report()`` so a degradation is always visible instead of
  being confused with a healthy deployment.

Required vs optional vs disabled
--------------------------------
Every capability is one of:

``required``
    Failure to initialise blocks readiness and job acceptance.
``optional``
    Declared as optional in ``[runtime] capability_optional``.  A degradation is
    reported through ``degraded_capabilities`` and never blocks delivery.
``disabled``
    Turned off in configuration on purpose.  Reported as ``disabled`` and never
    as a fault, so a non-RAG deployment may legitimately disable embedding.

``strictness``
--------------
``strict`` (default) rejects unknown implementation names, mock providers, and
placeholder parsers at startup.  ``lenient`` keeps the historical permissive
behaviour for hosts that must load a legacy config, but ParseCore still reports
the resulting placeholder/degraded state instead of claiming a healthy parse.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a config import cycle
    from .config import ParseCoreSettings

SCHEMA_VERSION = "2026-09-capability-report-v1"

# Capabilities that are optional unless the deployment says otherwise.  A failed
# optional provider degrades a delivery without blocking it.
#
# ``index`` is optional by default because the starter configuration pairs
# ``index.mode = "hybrid"`` with SQLite, which legitimately yields a non-durable
# in-memory index; that is reported as a degradation, not as a reason to reject
# every job.  ``pgvector`` without PostgreSQL is rejected outright at load time.
DEFAULT_OPTIONAL_CAPABILITIES: tuple[str, ...] = ("rerank", "ocr", "index")

STRICTNESS_MODES: tuple[str, ...] = ("strict", "lenient")

# ``<capability>.<name>`` -> stable error code.  These codes are part of the
# P0-01 acceptance contract; tests, CI gates and readiness probes assert on them
# instead of on message text.
ERROR_CODES: dict[str, str] = {
    "project.strictness": "invalid_strictness_mode",
    "project.allow_placeholder_parsers": "placeholder_parsers_not_allowed",
    "runtime.capability_optional": "unknown_capability_name",
    "providers.mock": "mock_providers_not_allowed",
    "parsers.name": "unknown_parser_name",
    "index.mode": "unknown_index_mode",
    "index.backend": "index_backend_mismatch",
    "storage.database_url": "unknown_database_scheme",
    "providers.llm.provider": "unknown_llm_provider",
    "providers.embedding.provider": "unknown_embedding_provider",
    "providers.rerank.provider": "unknown_rerank_provider",
    "providers.ocr.provider": "unknown_ocr_provider",
}

# Capability names a mock provider may satisfy.  A mock parser is never allowed:
# it would return placeholder blocks for real documents.
_MOCKABLE_CAPABILITIES: frozenset[str] = frozenset({"llm", "embedding", "rerank"})

# Capability names accepted in ``[runtime] capability_optional``.
_KNOWN_CAPABILITY_NAMES: frozenset[str] = frozenset(
    {"llm", "embedding", "rerank", "ocr", "index", "translation"}
)


class CapabilityConfigurationError(RuntimeError, ValueError):
    """Raised when configuration does not match a real implementation.

    ``code`` is stable across releases so hosts, CI gates and readiness probes
    can branch on it.  It inherits from :class:`ValueError` as well so existing
    callers that treated invalid configuration as a value error keep working.
    """

    def __init__(self, *, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": str(self)}


# ---------------------------------------------------------------------------
# Implementation registry
# ---------------------------------------------------------------------------

# Canonical parser name -> accepted aliases.  The canonical name is what the
# pipeline registry, provider routing and payload provenance expose.
PARSER_IMPLEMENTATIONS: dict[str, tuple[str, ...]] = {
    "pdf-text": (),
    "docx-native": (),
    "excel-native": (),
    "text-native": (),
    "docling-local": ("docling",),
    "pymupdf4llm-local": ("pymupdf4llm",),
    "image-ocr": ("ocr",),
}

INDEX_IMPLEMENTATIONS: dict[str, tuple[str, ...]] = {
    "pgvector": ("vector",),
    "hybrid": (),
    "null": ("none", "memory"),
}

# ``(official providers, mock providers)``
LLM_IMPLEMENTATIONS: tuple[frozenset[str], frozenset[str]] = (
    frozenset({"qwen-dashscope", "dashscope", "qwen"}),
    frozenset({"test", "stub"}),
)

EMBEDDING_IMPLEMENTATIONS: tuple[frozenset[str], frozenset[str]] = (
    frozenset(
        {
            "openai-compatible",
            "openai",
            "dashscope",
            "qwen",
            "sentence-transformers-local",
            "transformers-local",
            "local-transformer",
            "huggingface-local",
        }
    ),
    frozenset({"fake", "test", "stub"}),
)

RERANK_IMPLEMENTATIONS: tuple[frozenset[str], frozenset[str]] = (
    frozenset(
        {
            "dashscope-compatible",
            "dashscope-rerank",
            "qwen-rerank",
            "aliyun-rerank",
            "model-router-rerank",
        }
    ),
    frozenset({"fake", "test", "stub"}),
)

OCR_IMPLEMENTATIONS: tuple[frozenset[str], frozenset[str]] = (
    frozenset(
        {
            "rapidocr",
            "rapidocr-onnxruntime",
            "rapidocr_onnxruntime",
            "http",
            "http-json",
            "remote-http",
            "remote-http-json",
        }
    ),
    frozenset(),
)

DATABASE_IMPLEMENTATIONS: tuple[str, ...] = ("sqlite", "postgresql", "postgres", "memory")

# Index modes that promise a durable, queryable vector backend.
_DURABLE_INDEX_MODES: frozenset[str] = frozenset({"pgvector"})

_PROVIDER_REGISTRIES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "llm": LLM_IMPLEMENTATIONS,
    "embedding": EMBEDDING_IMPLEMENTATIONS,
    "rerank": RERANK_IMPLEMENTATIONS,
    "ocr": OCR_IMPLEMENTATIONS,
}


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def canonical_parser_name(name: str) -> str:
    """Return the canonical implementation name for a configured parser name.

    Unknown names are returned unchanged (lower-cased) so callers can report the
    configured value verbatim in diagnostics.
    """

    normalized = _normalize(name)
    if normalized in PARSER_IMPLEMENTATIONS:
        return normalized
    for canonical, aliases in PARSER_IMPLEMENTATIONS.items():
        if normalized in aliases:
            return canonical
    return normalized


def known_parser_names() -> tuple[str, ...]:
    names: set[str] = set()
    for canonical, aliases in PARSER_IMPLEMENTATIONS.items():
        names.add(canonical)
        names.update(aliases)
    return tuple(sorted(names))


def is_known_parser_name(name: str) -> bool:
    return _normalize(name) in known_parser_names()


def canonical_index_mode(mode: str) -> str:
    normalized = _normalize(mode)
    if normalized in INDEX_IMPLEMENTATIONS:
        return normalized
    for canonical, aliases in INDEX_IMPLEMENTATIONS.items():
        if normalized in aliases:
            return canonical
    return normalized


def known_index_modes() -> tuple[str, ...]:
    names: set[str] = set()
    for canonical, aliases in INDEX_IMPLEMENTATIONS.items():
        names.add(canonical)
        names.update(aliases)
    return tuple(sorted(names))


def database_scheme(database_url: str) -> str:
    raw = str(database_url or "").strip()
    if not raw or raw.startswith("memory"):
        return "memory"
    if "://" not in raw:
        return raw.lower()
    return raw.split("://", 1)[0].strip().lower()


def index_backend_for(database_url: str, index_mode: str) -> str:
    """Resolve the concrete index backend for a configuration.

    Returns ``pgvector`` or ``null``.  ``null`` is a degraded, non-durable
    backend and must never be presented as a publishable index.
    """

    mode = canonical_index_mode(index_mode)
    scheme = database_scheme(database_url)
    if mode == "pgvector" and scheme in {"postgresql", "postgres"}:
        return "pgvector"
    return "null"


def is_mock_provider(capability: str, provider: str) -> bool:
    registry = _PROVIDER_REGISTRIES.get(str(capability or "").strip().lower())
    if registry is None:
        return False
    return _normalize(provider) in registry[1]


def _lookup(capability: str, provider: str) -> tuple[bool, bool]:
    """Return ``(known, is_mock)`` for a provider name."""

    registry = _PROVIDER_REGISTRIES.get(capability)
    if registry is None:
        return False, False
    normalized = _normalize(provider)
    official, mocks = registry
    if normalized in mocks:
        return True, True
    return (normalized in official), False


def _error(*, field: str, message: str) -> CapabilityConfigurationError:
    return CapabilityConfigurationError(code=ERROR_CODES[field], field=field, message=message)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def _validate_capability_optional(settings: ParseCoreSettings) -> None:
    for name in settings.capability_optional:
        normalized = _normalize(name)
        if normalized in _KNOWN_CAPABILITY_NAMES:
            continue
        if normalized.startswith("parser:") and is_known_parser_name(normalized.split(":", 1)[1]):
            continue
        raise _error(
            field="runtime.capability_optional",
            message=(
                f"unknown capability {name!r} in runtime.capability_optional. Supported: "
                + ", ".join(sorted(_KNOWN_CAPABILITY_NAMES))
                + ", parser:<name>"
            ),
        )


def _validate_parser_names(settings: ParseCoreSettings) -> None:
    unknown = sorted(
        {
            str(item.name)
            for item in settings.parsers
            if not is_known_parser_name(item.name)
        }
    )
    if not unknown:
        return
    if settings.allow_placeholder_parsers:
        # Development escape hatch: the placeholder is kept, but the runtime
        # reports it as a placeholder capability and never as a formal result.
        return
    raise _error(
        field="parsers.name",
        message=(
            "unknown parser implementation(s): "
            + ", ".join(unknown)
            + ". Known implementations: "
            + ", ".join(known_parser_names())
            + ". Set project.allow_placeholder_parsers = true to run an explicit "
            "placeholder parser for development instead."
        ),
    )


def _validate_index(settings: ParseCoreSettings) -> None:
    mode = canonical_index_mode(settings.index_mode)
    if mode not in INDEX_IMPLEMENTATIONS:
        raise _error(
            field="index.mode",
            message=(
                f"unknown index mode: {settings.index_mode!r}. Supported: "
                + ", ".join(known_index_modes())
            ),
        )
    scheme = database_scheme(settings.database_url)
    if scheme not in DATABASE_IMPLEMENTATIONS:
        raise _error(
            field="storage.database_url",
            message=(
                f"unsupported database scheme: {scheme!r}. Supported: "
                + ", ".join(DATABASE_IMPLEMENTATIONS)
            ),
        )
    if mode in _DURABLE_INDEX_MODES and scheme not in {"postgresql", "postgres"}:
        raise _error(
            field="index.backend",
            message=(
                f"index.mode = {settings.index_mode!r} requires a postgresql "
                f"database_url; got {scheme!r}. ParseCore will not silently fall back "
                "to an in-memory index that cannot serve published results."
            ),
        )


def _validate_providers(settings: ParseCoreSettings) -> None:
    providers = settings.providers
    checks = (
        ("llm", providers.llm.enabled, providers.llm.provider),
        ("embedding", providers.embedding.enabled, providers.embedding.provider),
        ("rerank", providers.rerank.enabled, providers.rerank.provider),
        ("ocr", providers.ocr.enabled, providers.ocr.provider),
    )
    for capability, enabled, provider in checks:
        if not str(provider or "").strip():
            # Blank provider names keep their documented defaults (rapidocr for
            # OCR, openai-compatible for embedding, ...).
            continue
        known, is_mock = _lookup(capability, provider)
        if not known:
            official, mocks = _PROVIDER_REGISTRIES[capability]
            raise _error(
                field=f"providers.{capability}.provider",
                message=(
                    f"unknown {capability} provider: {provider!r}. Supported: "
                    + ", ".join(sorted(official | mocks))
                ),
            )
        if not (enabled and is_mock):
            continue
        if capability not in _MOCKABLE_CAPABILITIES:
            raise _error(
                field="providers.mock",
                message=f"capability {capability!r} cannot be satisfied by a mock provider",
            )
        if not settings.runtime.allow_mock_providers:
            raise _error(
                field="providers.mock",
                message=(
                    f"providers.{capability}.provider = {provider!r} is a mock provider and "
                    "produces non-production results. Set runtime.allow_mock_providers = true "
                    "to acknowledge that this deployment does not serve formal results."
                ),
            )


def validate_capability_configuration(settings: ParseCoreSettings) -> None:
    """Reject configuration that no real implementation can serve.

    Raises :class:`CapabilityConfigurationError`; the exception carries a stable
    ``code`` so callers can branch on it.
    """

    if settings.strictness not in STRICTNESS_MODES:
        raise _error(
            field="project.strictness",
            message=(
                f"unknown strictness mode: {settings.strictness!r}. Supported: "
                + ", ".join(STRICTNESS_MODES)
            ),
        )
    _validate_capability_optional(settings)
    _validate_parser_names(settings)
    _validate_index(settings)
    _validate_providers(settings)


# ---------------------------------------------------------------------------
# Capability report
# ---------------------------------------------------------------------------


def _requirement(name: str, *, enabled: bool, optional: frozenset[str]) -> str:
    if not enabled:
        return "disabled"
    return "optional" if name in optional else "required"


def _provider_requirement(capability: str, *, optional: frozenset[str]) -> str:
    """Capabilities that are only *enabled* when a provider is configured.

    Embedding, rerank, LLM and OCR are legitimate no-op capabilities for a
    non-RAG or native-only deployment, so leaving them disabled must never
    block job acceptance.  Declaring one of them explicitly in
    ``capability_optional`` still reports it as a visible degradation instead of
    a silent disable.  Translation is a core delivery capability and stays
    required.
    """

    if capability == "translation":
        return "required"
    return "optional" if capability in optional else "disabled"


def _entry(
    *,
    name: str,
    kind: str,
    enabled: bool,
    available: bool,
    requirement: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not enabled:
        status = "disabled"
    elif available:
        status = "available"
    elif requirement == "optional":
        status = "degraded"
    else:
        status = "unavailable"
    entry: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "requirement": requirement,
        "enabled": bool(enabled),
        "available": bool(available),
        "status": status,
        "reason": str(reason or ""),
    }
    if detail:
        entry["detail"] = detail
    return entry


def _credential_present(env_name: str) -> bool:
    if not str(env_name or "").strip():
        return True
    return bool(os.environ.get(str(env_name), "").strip())


def _parser_entries(
    settings: ParseCoreSettings,
    *,
    parser_names: tuple[str, ...],
    placeholder_names: tuple[str, ...],
    optional: frozenset[str],
) -> list[dict[str, Any]]:
    registered = {_normalize(name) for name in parser_names}
    placeholders = {_normalize(name) for name in placeholder_names}
    entries: list[dict[str, Any]] = []
    for configured in sorted({_normalize(item.name) for item in settings.parsers}):
        capability = f"parser:{configured}"
        is_placeholder = configured in placeholders
        available = configured in registered and not is_placeholder
        if is_placeholder:
            reason = ERROR_CODES["project.allow_placeholder_parsers"]
        elif available:
            reason = "ok"
        else:
            reason = ERROR_CODES["parsers.name"]
        entries.append(
            _entry(
                name=capability,
                kind="parser",
                enabled=True,
                available=available,
                requirement=_requirement(capability, enabled=True, optional=optional),
                reason=reason,
                detail={
                    "configured_name": configured,
                    "canonical_name": canonical_parser_name(configured),
                    "placeholder": is_placeholder,
                    "registered": configured in registered,
                },
            )
        )
    return entries


def _index_entry(
    settings: ParseCoreSettings,
    *,
    backend: str,
    optional: frozenset[str],
) -> dict[str, Any]:
    """Describe the configured index backend truthfully.

    ``pgvector`` without PostgreSQL is already rejected at load time, so this
    entry reports the remaining honest cases: a durable backend, or an
    in-memory backend that cannot serve published results.
    """

    mode = canonical_index_mode(settings.index_mode)
    durable = backend in _DURABLE_INDEX_MODES
    enabled = mode != "null"
    if mode == "null":
        reason = "disabled"
    elif durable:
        reason = "ok"
    else:
        reason = "in_memory_index"
    return _entry(
        name="index",
        kind="index",
        enabled=enabled,
        available=enabled and durable,
        requirement=_requirement("index", enabled=enabled, optional=optional),
        reason=reason,
        detail={
            "configured_mode": mode,
            "backend": backend,
            "durable": durable,
            "database_scheme": database_scheme(settings.database_url),
        },
    )


def build_capability_report(
    settings: ParseCoreSettings,
    *,
    parser_names: tuple[str, ...] = (),
    placeholder_names: tuple[str, ...] = (),
    index_backend: str | None = None,
    embedding_provider: str | None = None,
    rerank_provider: str | None = None,
    embedding_configured: bool | None = None,
    rerank_configured: bool | None = None,
    degraded: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    """Build the capability report exposed to health/readiness consumers.

    ``parser_names`` and ``placeholder_names`` are taken from the parsers the
    runtime actually built, so a stub can never be reported as an implementation.
    ``degraded`` carries runtime degradations discovered while building the
    runtime; each item is ``{"capability", "reason", "error_code"}``.
    """

    providers = settings.providers
    optional = frozenset(settings.capability_optional) | frozenset(
        DEFAULT_OPTIONAL_CAPABILITIES
    )
    backend = index_backend or index_backend_for(settings.database_url, settings.index_mode)

    entries: list[dict[str, Any]] = []
    entries.extend(
        _parser_entries(
            settings,
            parser_names=parser_names,
            placeholder_names=placeholder_names,
            optional=optional,
        )
    )
    entries.append(_index_entry(settings, backend=backend, optional=optional))

    embedding_enabled = bool(providers.embedding.enabled)
    embedding_ready = (
        bool(embedding_configured)
        if embedding_configured is not None
        else embedding_enabled
        and _credential_present(providers.embedding.api_key_env)
        and bool(str(providers.embedding.base_url or providers.embedding.model or "").strip())
    )
    entries.append(
        _entry(
            name="embedding",
            kind="provider",
            enabled=embedding_enabled,
            available=embedding_ready,
            requirement=_provider_requirement("embedding", optional=optional),
            reason="ok"
            if embedding_ready
            else ("disabled" if not embedding_enabled else "missing_embedding_credential"),
            detail={
                "configured_provider": providers.embedding.provider,
                "active_provider": str(embedding_provider or ""),
                "api_key_env": providers.embedding.api_key_env,
                "mock": is_mock_provider("embedding", providers.embedding.provider),
            },
        )
    )

    rerank_enabled = bool(providers.rerank.enabled)
    rerank_ready = (
        bool(rerank_configured)
        if rerank_configured is not None
        else rerank_enabled
        and _credential_present(providers.rerank.api_key_env)
        and bool(str(providers.rerank.base_url or "").strip())
    )
    entries.append(
        _entry(
            name="rerank",
            kind="provider",
            enabled=rerank_enabled,
            available=rerank_ready,
            requirement=_provider_requirement("rerank", optional=optional),
            reason="ok"
            if rerank_ready
            else ("disabled" if not rerank_enabled else "missing_rerank_credential"),
            detail={
                "configured_provider": providers.rerank.provider,
                "active_provider": str(rerank_provider or ""),
                "api_key_env": providers.rerank.api_key_env,
                "mock": is_mock_provider("rerank", providers.rerank.provider),
            },
        )
    )

    ocr_enabled = bool(providers.ocr.enabled)
    entries.append(
        _entry(
            name="ocr",
            kind="provider",
            enabled=ocr_enabled,
            available=ocr_enabled,
            requirement=_provider_requirement("ocr", optional=optional),
            reason="ok" if ocr_enabled else "disabled",
            detail={
                "configured_provider": providers.ocr.provider,
                "api_key_env": providers.ocr.api_key_env,
            },
        )
    )

    llm_enabled = bool(providers.llm.enabled)
    llm_ready = llm_enabled and (
        _credential_present(providers.llm.api_key_env)
        and bool(str(providers.llm.base_url or "").strip())
    )
    entries.append(
        _entry(
            name="llm",
            kind="provider",
            enabled=llm_enabled,
            available=llm_ready,
            requirement=_provider_requirement("llm", optional=optional),
            reason="ok" if llm_ready else ("disabled" if not llm_enabled else "missing_llm_credential"),
            detail={
                "configured_provider": providers.llm.provider,
                "api_key_env": providers.llm.api_key_env,
                "mock": is_mock_provider("llm", providers.llm.provider),
            },
        )
    )

    translation_enabled = bool(settings.translation_enabled)
    entries.append(
        _entry(
            name="translation",
            kind="provider",
            enabled=translation_enabled,
            available=translation_enabled,
            requirement=_provider_requirement("translation", optional=optional),
            reason="ok" if translation_enabled else "disabled",
            detail={"strategy": settings.translation_strategy},
        )
    )

    for item in degraded:
        capability = str(item.get("capability") or "").strip()
        if not capability:
            continue
        entries.append(
            _entry(
                name=capability,
                kind="runtime",
                enabled=True,
                available=False,
                requirement=_provider_requirement(capability, optional=optional)
                if capability in _PROVIDER_REGISTRIES
                else _requirement(capability, enabled=True, optional=optional),
                reason=str(item.get("error_code") or item.get("reason") or "degraded"),
                detail=dict(item),
            )
        )

    unavailable = [
        entry
        for entry in entries
        if entry["status"] == "unavailable"
        and not bool((entry.get("detail") or {}).get("placeholder"))
    ]
    placeholder_entries = [
        entry
        for entry in entries
        if entry["kind"] == "parser" and bool((entry.get("detail") or {}).get("placeholder"))
    ]
    degraded_optional = [entry for entry in entries if entry["status"] == "degraded"]
    active_placeholders = sorted(
        str((entry.get("detail") or {}).get("configured_name") or "")
        for entry in placeholder_entries
    )
    non_durable_index = [
        entry
        for entry in entries
        if entry["name"] == "index" and entry["status"] == "degraded"
    ]

    def _as_reason(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": entry["name"],
            "requirement": entry["requirement"],
            "reason": entry["reason"],
        }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": settings.project_name,
        "strictness": settings.strictness,
        "allow_placeholder_parsers": bool(settings.allow_placeholder_parsers),
        "allow_mock_providers": bool(settings.runtime.allow_mock_providers),
        "capabilities": entries,
        "required_capabilities": sorted(
            entry["name"] for entry in entries if entry["requirement"] == "required"
        ),
        "enabled_capabilities": sorted(entry["name"] for entry in entries if entry["enabled"]),
        "disabled_capabilities": sorted(
            entry["name"] for entry in entries if entry["status"] == "disabled"
        ),
        "degraded_capabilities": sorted(
            (_as_reason(entry) for entry in degraded_optional),
            key=lambda item: item["name"],
        ),
        "unavailable_capabilities": sorted(
            (_as_reason(entry) for entry in unavailable), key=lambda item: item["name"]
        ),
        "placeholder_parsers": active_placeholders,
        "mock_capabilities": sorted(
            entry["name"]
            for entry in entries
            if bool((entry.get("detail") or {}).get("mock"))
        ),
        # A placeholder parser is an explicit development opt-in: it blocks
        # *formal* results, not job acceptance.
        "accepting_jobs": not unavailable,
        "formal_parse_ready": not unavailable and not active_placeholders,
        "durable_index": not non_durable_index,
        "blocking_reasons": [
            {"capability": entry["name"], "reason": entry["reason"]} for entry in unavailable
        ]
        + [
            {
                "capability": f"parser:{name}",
                "reason": ERROR_CODES["project.allow_placeholder_parsers"],
            }
            for name in active_placeholders
        ],
    }
    report["fingerprint"] = capability_fingerprint(report)
    return report


def capability_fingerprint(report: dict[str, Any]) -> str:
    """Stable short hash of the capability identity of a deployment."""

    parts: list[str] = [str(report.get("schema_version") or "")]
    for entry in report.get("capabilities") or []:
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        detail = entry.get("detail") or {}
        parts.append(
            "|".join(
                [
                    str(entry.get("name") or ""),
                    str(entry.get("status") or ""),
                    str(detail.get("configured_provider") or detail.get("configured_name") or ""),
                    str(detail.get("configured_mode") or ""),
                    str(detail.get("backend") or ""),
                ]
            )
        )
    return sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def capability_error_payload(error: CapabilityConfigurationError) -> dict[str, Any]:
    """Machine-readable description of a rejected configuration."""

    return {
        "schema_version": SCHEMA_VERSION,
        "error": error.code,
        "code": error.code,
        "field": error.field,
        "message": str(error),
    }


def config_fingerprint(path: str | Path) -> str:
    """Hash of the config file bytes, for run/provenance correlation."""

    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return sha256(data).hexdigest()[:16]


__all__ = [
    "CapabilityConfigurationError",
    "DEFAULT_OPTIONAL_CAPABILITIES",
    "ERROR_CODES",
    "SCHEMA_VERSION",
    "STRICTNESS_MODES",
    "build_capability_report",
    "canonical_index_mode",
    "canonical_parser_name",
    "capability_error_payload",
    "capability_fingerprint",
    "config_fingerprint",
    "database_scheme",
    "index_backend_for",
    "is_known_parser_name",
    "is_mock_provider",
    "known_index_modes",
    "known_parser_names",
    "validate_capability_configuration",
]
