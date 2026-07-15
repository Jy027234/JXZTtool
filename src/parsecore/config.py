from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import tomllib


_EMPTY_MAPPING: Mapping[str, Any] = MappingProxyType({})


@dataclass(slots=True, frozen=True)
class ParserSettings:
    name: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class RuntimeSettings:
    execution_mode: str
    max_workers: int
    poll_interval_ms: int
    max_upload_bytes: int = 0
    max_inflight_jobs: int = 0
    max_active_parts_per_doc: int = 0
    allow_external_file_paths: bool = False
    staged_upload_max_bytes: int = 0
    staged_upload_retention_seconds: int = 86400
    part_artifact_retention_seconds: int = 604800
    export_artifact_retention_seconds: int = 2592000
    provider_comparison_artifact_retention_seconds: int = 2592000
    quota_enforce: bool = False
    quota_window_hours: float = 24.0
    quota_default_limit_units: int = 0
    quota_limits: Mapping[str, int] = field(default_factory=lambda: _EMPTY_MAPPING)
    max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    retry_backoff_max_seconds: float = 60.0
    job_timeout_seconds: int = 0
    part_timeout_seconds: int = 0
    log_path: str = "var/logs/job_events.jsonl"
    api_key_env: str = ""
    staged_upload_api_key_env: str = ""


@dataclass(slots=True, frozen=True)
class LlmProviderSettings:
    """LLM provider settings used by ParseCore for semantic block decisions.

    Designed for the embedded-SDK mode: when ParseCore is hosted by another
    application, the host can override `enabled` / `api_key_env` to inject its
    own credential management instead of the standalone toml config.
    """

    enabled: bool
    provider: str
    model: str
    base_url: str
    api_key_env: str
    timeout_seconds: float
    max_retries: int
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class EmbeddingProviderSettings:
    enabled: bool
    provider: str
    model: str
    base_url: str
    api_key_env: str
    timeout_seconds: float
    max_retries: int
    batch_size: int
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class RerankProviderSettings:
    """Optional second-stage retrieval reranker configuration.

    The defaults keep reranking disabled, so an existing embedded ParseCore
    deployment never acquires a remote model dependency until it explicitly
    opts in through ``[providers.rerank]``.
    """

    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_env: str = "PARSECORE_RERANK_API_KEY"
    timeout_seconds: float = 30.0
    max_retries: int = 2
    candidate_limit: int = 30
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class OcrProviderSettings:
    enabled: bool
    provider: str
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 2
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class LocalParserProviderSettings:
    id: str
    enabled: bool
    priority: int
    media_types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    route_mode: str = "route"
    gate_status: str = "passed"
    gate_checks: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class LocalParserRoutingSettings:
    enabled: bool = False
    fallback_to_default: bool = True
    include_disabled: bool = False


@dataclass(slots=True, frozen=True)
class ProviderSettings:
    llm: LlmProviderSettings
    embedding: EmbeddingProviderSettings
    ocr: OcrProviderSettings
    rerank: RerankProviderSettings = field(default_factory=RerankProviderSettings)
    local_parsers: tuple[LocalParserProviderSettings, ...] = ()
    local_parser_routing: LocalParserRoutingSettings = field(default_factory=LocalParserRoutingSettings)


@dataclass(slots=True, frozen=True)
class QualityGateSettings:
    enabled: bool = True
    min_text_page_coverage: float = 0.98
    min_table_unit_coverage: float = 0.95
    min_unit_chunk_coverage: float = 0.98
    min_reading_order_confidence: float = 0.75
    allow_local_rerun: bool = True
    allow_manual_review: bool = True


@dataclass(slots=True, frozen=True)
class ParseCoreSettings:
    project_name: str
    mode: str
    database_url: str
    object_store: str
    index_mode: str
    translation_enabled: bool
    translation_strategy: str
    product_adapter: str
    runtime: RuntimeSettings
    parsers: tuple[ParserSettings, ...]
    providers: ProviderSettings
    index_embedding_dimension: int = 1536
    quality_gate: QualityGateSettings = field(default_factory=QualityGateSettings)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item) for item in value)


def _freeze_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return _EMPTY_MAPPING
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            frozen[str(key)] = _freeze_mapping(item)
        else:
            frozen[str(key)] = item
    return MappingProxyType(frozen)


def _freeze_int_mapping(value: Any) -> Mapping[str, int]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    normalized: dict[str, int] = {}
    for key, item in value.items():
        try:
            normalized[str(key)] = int(item)
        except (TypeError, ValueError):
            continue
    return MappingProxyType(normalized)


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return value


def _normalize_local_provider_route_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"route", "evaluate"}:
        return normalized
    return "route"


def _normalize_local_provider_gate_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"passed", "pending", "failed"}:
        return normalized
    return "passed"


def _normalize_local_provider_gate_checks(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        values = (value,)
    else:
        values = value or ()
    return tuple(
        str(item).strip().lower()
        for item in values
        if str(item).strip()
    )


def _local_parser_provider_payload(
    providers: Sequence[LocalParserProviderSettings],
) -> list[dict[str, Any]]:
    return [
        {
            "id": provider.id,
            "enabled": provider.enabled,
            "priority": provider.priority,
            "media_types": list(provider.media_types),
            "extensions": list(provider.extensions),
            "profiles": list(provider.profiles),
            "capabilities": list(provider.capabilities),
            "admission": {
                "route_mode": provider.route_mode,
                "gate_status": provider.gate_status,
                "gate_checks": list(provider.gate_checks),
                "route_ready": (
                    provider.enabled
                    and provider.route_mode == "route"
                    and provider.gate_status == "passed"
                ),
            },
            "options": _plain_value(provider.options),
        }
        for provider in sorted(providers, key=lambda item: (-item.priority, item.id))
    ]


def _normalize_extension(value: str | None) -> str:
    extension = str(value or "").strip().lower()
    if not extension:
        return ""
    if not extension.startswith("."):
        extension = f".{extension}"
    return extension


def _normalize_token_set(values: Sequence[str]) -> set[str]:
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def local_provider_registry_payload(settings: ProviderSettings) -> dict[str, Any]:
    local_parsers = _local_parser_provider_payload(settings.local_parsers)
    enabled = [provider for provider in local_parsers if bool(provider.get("enabled"))]
    evaluation_only = [
        provider
        for provider in local_parsers
        if str((provider.get("admission") or {}).get("route_mode") or "") == "evaluate"
    ]
    gate_pending = [
        provider
        for provider in local_parsers
        if str((provider.get("admission") or {}).get("gate_status") or "") == "pending"
    ]
    gate_failed = [
        provider
        for provider in local_parsers
        if str((provider.get("admission") or {}).get("gate_status") or "") == "failed"
    ]
    route_ready = [
        provider
        for provider in local_parsers
        if bool((provider.get("admission") or {}).get("route_ready"))
    ]
    return {
        "schema_version": "2026-06-local-provider-registry",
        "routing": {
            "enabled": settings.local_parser_routing.enabled,
            "fallback_to_default": settings.local_parser_routing.fallback_to_default,
            "include_disabled": settings.local_parser_routing.include_disabled,
            "routing_policy": "priority_desc_then_id",
        },
        "local_parsers": local_parsers,
        "summary": {
            "total": len(local_parsers),
            "enabled": len(enabled),
            "disabled": len(local_parsers) - len(enabled),
            "route_ready": len(route_ready),
            "evaluation_only": len(evaluation_only),
            "gate_pending": len(gate_pending),
            "gate_failed": len(gate_failed),
        },
    }


def local_provider_route_plan_payload(
    settings: ProviderSettings,
    *,
    media_type: str | None = None,
    extension: str | None = None,
    file_name: str | None = None,
    profile: str | None = None,
    required_capabilities: Sequence[str] = (),
    include_disabled: bool = True,
) -> dict[str, Any]:
    """Return a read-only routing plan for local parser providers."""

    requested_media_type = str(media_type or "").strip().lower()
    requested_extension = _normalize_extension(extension or Path(str(file_name or "")).suffix)
    requested_profile = str(profile or "").strip().lower()
    requested_capabilities = sorted(_normalize_token_set(required_capabilities))
    registry = local_provider_registry_payload(settings)
    candidates: list[dict[str, Any]] = []

    for provider in registry["local_parsers"]:
        provider_media_types = _normalize_token_set(provider.get("media_types") or [])
        provider_extensions = {
            _normalize_extension(item)
            for item in provider.get("extensions") or []
            if _normalize_extension(item)
        }
        provider_profiles = _normalize_token_set(provider.get("profiles") or [])
        provider_capabilities = _normalize_token_set(provider.get("capabilities") or [])
        admission = provider.get("admission") if isinstance(provider.get("admission"), Mapping) else {}
        route_mode = _normalize_local_provider_route_mode(admission.get("route_mode"))
        gate_status = _normalize_local_provider_gate_status(admission.get("gate_status"))
        gate_checks = list(_normalize_local_provider_gate_checks(admission.get("gate_checks")))
        media_type_match = not requested_media_type or not provider_media_types or requested_media_type in provider_media_types
        extension_match = not requested_extension or not provider_extensions or requested_extension in provider_extensions
        profile_match = not requested_profile or not provider_profiles or requested_profile in provider_profiles
        capability_match = all(item in provider_capabilities for item in requested_capabilities)
        enabled = bool(provider.get("enabled"))
        exclusion_reasons: list[str] = []
        if not enabled:
            exclusion_reasons.append("disabled")
        if route_mode != "route":
            exclusion_reasons.append("evaluation_only")
        if gate_status != "passed":
            exclusion_reasons.append(f"gate_{gate_status}")
        if not media_type_match:
            exclusion_reasons.append("media_type_mismatch")
        if not extension_match:
            exclusion_reasons.append("extension_mismatch")
        if not profile_match:
            exclusion_reasons.append("profile_mismatch")
        if not capability_match:
            exclusion_reasons.append("capability_mismatch")
        eligible = not exclusion_reasons
        candidate = {
            "id": provider["id"],
            "enabled": enabled,
            "priority": provider["priority"],
            "eligible": eligible,
            "route_role": "candidate",
            "exclusion_reasons": exclusion_reasons,
            "matches": {
                "media_type": media_type_match,
                "extension": extension_match,
                "profile": profile_match,
                "capabilities": capability_match,
            },
            "admission": {
                "route_mode": route_mode,
                "gate_status": gate_status,
                "gate_checks": gate_checks,
                "route_ready": enabled and route_mode == "route" and gate_status == "passed",
            },
            "media_types": provider.get("media_types") or [],
            "extensions": provider.get("extensions") or [],
            "profiles": provider.get("profiles") or [],
            "capabilities": provider.get("capabilities") or [],
            "options": provider.get("options") or {},
        }
        if include_disabled or enabled:
            candidates.append(candidate)

    eligible_candidates = sorted(
        [item for item in candidates if item["eligible"]],
        key=lambda item: (-int(item.get("priority") or 0), str(item.get("id") or "")),
    )
    for index, candidate in enumerate(eligible_candidates):
        candidate["route_role"] = "primary" if index == 0 else "fallback"
        candidate["selection_rank"] = index + 1
        candidate["selection_reason"] = "highest_priority_eligible" if index == 0 else "eligible_fallback"
    for candidate in candidates:
        if not candidate["eligible"]:
            candidate["route_role"] = "excluded"
            candidate["selection_rank"] = None
            candidate["selection_reason"] = "excluded:" + ",".join(candidate["exclusion_reasons"])

    selected_provider_ids = [candidate["id"] for candidate in eligible_candidates]
    primary_provider_id = selected_provider_ids[0] if selected_provider_ids else None
    fallback_provider_ids = selected_provider_ids[1:]
    return {
        "schema_version": "2026-06-local-provider-route-plan",
        "routing_policy": "priority_desc_then_id",
        "requested": {
            "media_type": requested_media_type or None,
            "extension": requested_extension or None,
            "file_name": str(file_name or "").strip() or None,
            "profile": requested_profile or None,
            "required_capabilities": requested_capabilities,
            "include_disabled": include_disabled,
        },
        "summary": {
            "candidate_count": len(candidates),
            "eligible_count": len(selected_provider_ids),
            "fallback_count": len(fallback_provider_ids),
            "enabled_count": len([candidate for candidate in candidates if candidate["enabled"]]),
            "disabled_count": len([candidate for candidate in candidates if not candidate["enabled"]]),
            "route_ready_count": len(
                [candidate for candidate in candidates if bool((candidate.get("admission") or {}).get("route_ready"))]
            ),
            "evaluation_only_count": len(
                [
                    candidate
                    for candidate in candidates
                    if str((candidate.get("admission") or {}).get("route_mode") or "") == "evaluate"
                ]
            ),
            "gate_pending_count": len(
                [
                    candidate
                    for candidate in candidates
                    if str((candidate.get("admission") or {}).get("gate_status") or "") == "pending"
                ]
            ),
            "gate_failed_count": len(
                [
                    candidate
                    for candidate in candidates
                    if str((candidate.get("admission") or {}).get("gate_status") or "") == "failed"
                ]
            ),
        },
        "selection": {
            "primary_provider_id": primary_provider_id,
            "fallback_provider_ids": fallback_provider_ids,
            "eligible_provider_ids": selected_provider_ids,
            "excluded_provider_ids": [candidate["id"] for candidate in candidates if not candidate["eligible"]],
        },
        "candidates": candidates,
        "provider_registry": registry,
    }


def quality_gate_payload(settings: QualityGateSettings) -> dict[str, Any]:
    return {
        "schema_version": "2026-06-quality-gate-config",
        "enabled": settings.enabled,
        "thresholds": {
            "min_text_page_coverage": settings.min_text_page_coverage,
            "min_table_unit_coverage": settings.min_table_unit_coverage,
            "min_unit_chunk_coverage": settings.min_unit_chunk_coverage,
            "min_reading_order_confidence": settings.min_reading_order_confidence,
        },
        "actions": {
            "allow_local_rerun": settings.allow_local_rerun,
            "allow_manual_review": settings.allow_manual_review,
        },
        "enforcement": "report_only",
    }


def _normalize_product_adapter(value: Any) -> str:
    # Jobcard compatibility wiring has been removed from the mainline runtime.
    # Keep parsing the field so old configs still load, but collapse all values
    # to the only supported adapter contract: embedded.
    _ = str(value or "").strip().lower()
    return "embedded"


def load_settings(path: str | Path) -> ParseCoreSettings:
    config_path = Path(path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    project = data.get("project", {})
    storage = data.get("storage", {})
    index = data.get("index", {})
    translation = data.get("translation", {})
    product = data.get("product", {})
    runtime = data.get("runtime", {})
    quality_gate_raw = data.get("quality_gate", {}) or {}
    providers_raw = data.get("providers", {}) or {}
    llm_raw = providers_raw.get("llm", {}) or {}
    embedding_raw = providers_raw.get("embedding", {}) or {}
    rerank_raw = providers_raw.get("rerank", {}) or {}
    ocr_raw = providers_raw.get("ocr", {}) or {}
    local_parser_routing_raw = providers_raw.get("local_parser_routing", {}) or {}
    local_parsers_raw = providers_raw.get("local_parsers", ()) or ()
    parser_settings = tuple(
        ParserSettings(
            name=str(item["name"]),
            media_types=_as_tuple(item.get("media_types")),
            extensions=_as_tuple(item.get("extensions")),
            options=_freeze_mapping(item.get("options")),
        )
        for item in data.get("parsers", [])
    )
    llm_settings = LlmProviderSettings(
        enabled=bool(llm_raw.get("enabled", False)),
        provider=str(llm_raw.get("provider", "")),
        model=str(llm_raw.get("model", "")),
        base_url=str(llm_raw.get("base_url", "")),
        api_key_env=str(llm_raw.get("api_key_env", "PARSECORE_LLM_API_KEY")),
        timeout_seconds=float(llm_raw.get("timeout_seconds", 30.0)),
        max_retries=int(llm_raw.get("max_retries", 2)),
        options=_freeze_mapping(llm_raw.get("options")),
    )
    embedding_settings = EmbeddingProviderSettings(
        enabled=bool(embedding_raw.get("enabled", False)),
        provider=str(embedding_raw.get("provider", "")),
        model=str(embedding_raw.get("model", "")),
        base_url=str(embedding_raw.get("base_url", "")),
        api_key_env=str(
            embedding_raw.get("api_key_env", "PARSECORE_EMBEDDING_API_KEY")
        ),
        timeout_seconds=float(embedding_raw.get("timeout_seconds", 30.0)),
        max_retries=int(embedding_raw.get("max_retries", 2)),
        batch_size=int(embedding_raw.get("batch_size", 16)),
        options=_freeze_mapping(embedding_raw.get("options")),
    )
    raw_rerank_candidate_limit = rerank_raw.get("candidate_limit", 30)
    if isinstance(raw_rerank_candidate_limit, bool) or isinstance(
        raw_rerank_candidate_limit, float
    ):
        raise ValueError("providers.rerank.candidate_limit must be a positive integer")
    try:
        rerank_candidate_limit = int(raw_rerank_candidate_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "providers.rerank.candidate_limit must be a positive integer"
        ) from exc
    rerank_settings = RerankProviderSettings(
        enabled=bool(rerank_raw.get("enabled", False)),
        provider=str(rerank_raw.get("provider", "")),
        model=str(rerank_raw.get("model", "")),
        base_url=str(rerank_raw.get("base_url", "")),
        api_key_env=str(rerank_raw.get("api_key_env", "PARSECORE_RERANK_API_KEY")),
        timeout_seconds=float(rerank_raw.get("timeout_seconds", 30.0)),
        max_retries=int(rerank_raw.get("max_retries", 2)),
        candidate_limit=rerank_candidate_limit,
        options=_freeze_mapping(rerank_raw.get("options")),
    )
    if rerank_settings.candidate_limit <= 0:
        raise ValueError("providers.rerank.candidate_limit must be a positive integer")
    ocr_settings = OcrProviderSettings(
        enabled=bool(ocr_raw.get("enabled", True)),
        provider=str(ocr_raw.get("provider", "rapidocr")),
        base_url=str(ocr_raw.get("base_url", "")),
        api_key_env=str(ocr_raw.get("api_key_env", "")),
        timeout_seconds=float(ocr_raw.get("timeout_seconds", 30.0)),
        max_retries=int(ocr_raw.get("max_retries", 2)),
        options=_freeze_mapping(ocr_raw.get("options")),
    )
    local_parser_settings = tuple(
        LocalParserProviderSettings(
            id=str(item.get("id") or item.get("name") or "").strip(),
            enabled=bool(item.get("enabled", False)),
            priority=int(item.get("priority", 0)),
            media_types=_as_tuple(item.get("media_types")),
            extensions=_as_tuple(item.get("extensions")),
            profiles=_as_tuple(item.get("profiles")),
            capabilities=_as_tuple(item.get("capabilities")),
            route_mode=_normalize_local_provider_route_mode(item.get("route_mode")),
            gate_status=_normalize_local_provider_gate_status(item.get("gate_status")),
            gate_checks=_normalize_local_provider_gate_checks(item.get("gate_checks")),
            options=_freeze_mapping(item.get("options")),
        )
        for item in local_parsers_raw
        if isinstance(item, dict) and str(item.get("id") or item.get("name") or "").strip()
    )

    index_embedding_dimension = int(index.get("embedding_dimension", 1536))
    if index_embedding_dimension <= 0:
        raise ValueError("index.embedding_dimension must be a positive integer")

    configured_database_url = str(
        storage.get("database_url", "sqlite:///./var/parsecore.db")
    )
    database_url = (
        os.environ.get("PARSECORE_DATABASE_URL", "").strip()
        or configured_database_url
    )

    return ParseCoreSettings(
        project_name=str(project.get("name", "parsecore")),
        mode=str(project.get("mode", "embedded-sdk")),
        database_url=database_url,
        object_store=str(storage.get("object_store", "local://./var/uploads")),
        index_mode=str(index.get("mode", "hybrid")),
        index_embedding_dimension=index_embedding_dimension,
        translation_enabled=bool(translation.get("enabled", True)),
        translation_strategy=str(translation.get("strategy", "lazy")),
        product_adapter=_normalize_product_adapter(product.get("adapter", "embedded")),
        runtime=RuntimeSettings(
            execution_mode=str(runtime.get("execution_mode", "inline")),
            max_workers=int(runtime.get("max_workers", 2)),
            poll_interval_ms=int(runtime.get("poll_interval_ms", 1000)),
            max_upload_bytes=int(runtime.get("max_upload_bytes", 0)),
            max_inflight_jobs=int(runtime.get("max_inflight_jobs", 0)),
            max_active_parts_per_doc=int(runtime.get("max_active_parts_per_doc", 0)),
            allow_external_file_paths=bool(runtime.get("allow_external_file_paths", False)),
            staged_upload_max_bytes=int(runtime.get("staged_upload_max_bytes", 0)),
            staged_upload_retention_seconds=int(runtime.get("staged_upload_retention_seconds", 86400)),
            part_artifact_retention_seconds=int(runtime.get("part_artifact_retention_seconds", 604800)),
            export_artifact_retention_seconds=int(runtime.get("export_artifact_retention_seconds", 2592000)),
            provider_comparison_artifact_retention_seconds=int(
                runtime.get("provider_comparison_artifact_retention_seconds", 2592000)
            ),
            quota_enforce=bool(runtime.get("quota_enforce", False)),
            quota_window_hours=float(runtime.get("quota_window_hours", 24.0)),
            quota_default_limit_units=int(runtime.get("quota_default_limit_units", 0)),
            quota_limits=_freeze_int_mapping(runtime.get("quota_limits")),
            max_attempts=int(runtime.get("max_attempts", 3)),
            retry_backoff_seconds=float(runtime.get("retry_backoff_seconds", 1.0)),
            retry_backoff_max_seconds=float(runtime.get("retry_backoff_max_seconds", 60.0)),
            job_timeout_seconds=int(runtime.get("job_timeout_seconds", 0)),
            part_timeout_seconds=int(runtime.get("part_timeout_seconds", 0)),
            log_path=str(runtime.get("log_path", "var/logs/job_events.jsonl")),
            api_key_env=str(runtime.get("api_key_env", "")),
            staged_upload_api_key_env=str(runtime.get("staged_upload_api_key_env", "")),
        ),
        parsers=parser_settings,
        providers=ProviderSettings(
            llm=llm_settings,
            embedding=embedding_settings,
            ocr=ocr_settings,
            rerank=rerank_settings,
            local_parsers=local_parser_settings,
            local_parser_routing=LocalParserRoutingSettings(
                enabled=bool(local_parser_routing_raw.get("enabled", False)),
                fallback_to_default=bool(local_parser_routing_raw.get("fallback_to_default", True)),
                include_disabled=bool(local_parser_routing_raw.get("include_disabled", False)),
            ),
        ),
        quality_gate=QualityGateSettings(
            enabled=bool(quality_gate_raw.get("enabled", True)),
            min_text_page_coverage=float(quality_gate_raw.get("min_text_page_coverage", 0.98)),
            min_table_unit_coverage=float(quality_gate_raw.get("min_table_unit_coverage", 0.95)),
            min_unit_chunk_coverage=float(quality_gate_raw.get("min_unit_chunk_coverage", 0.98)),
            min_reading_order_confidence=float(quality_gate_raw.get("min_reading_order_confidence", 0.75)),
            allow_local_rerun=bool(quality_gate_raw.get("allow_local_rerun", True)),
            allow_manual_review=bool(quality_gate_raw.get("allow_manual_review", True)),
        ),
    )
