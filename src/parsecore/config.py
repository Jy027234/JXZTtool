from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
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
    allow_external_file_paths: bool = False
    staged_upload_max_bytes: int = 0
    staged_upload_retention_seconds: int = 86400
    quota_enforce: bool = False
    quota_window_hours: float = 24.0
    quota_default_limit_units: int = 0
    quota_limits: Mapping[str, int] = field(default_factory=lambda: _EMPTY_MAPPING)
    max_attempts: int = 3
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
class OcrProviderSettings:
    enabled: bool
    provider: str
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 2
    options: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_MAPPING)


@dataclass(slots=True, frozen=True)
class ProviderSettings:
    llm: LlmProviderSettings
    embedding: EmbeddingProviderSettings
    ocr: OcrProviderSettings


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
    providers_raw = data.get("providers", {}) or {}
    llm_raw = providers_raw.get("llm", {}) or {}
    embedding_raw = providers_raw.get("embedding", {}) or {}
    ocr_raw = providers_raw.get("ocr", {}) or {}
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
    ocr_settings = OcrProviderSettings(
        enabled=bool(ocr_raw.get("enabled", True)),
        provider=str(ocr_raw.get("provider", "rapidocr")),
        base_url=str(ocr_raw.get("base_url", "")),
        api_key_env=str(ocr_raw.get("api_key_env", "")),
        timeout_seconds=float(ocr_raw.get("timeout_seconds", 30.0)),
        max_retries=int(ocr_raw.get("max_retries", 2)),
        options=_freeze_mapping(ocr_raw.get("options")),
    )

    return ParseCoreSettings(
        project_name=str(project.get("name", "parsecore")),
        mode=str(project.get("mode", "embedded-sdk")),
        database_url=str(storage.get("database_url", "sqlite:///./var/parsecore.db")),
        object_store=str(storage.get("object_store", "local://./var/uploads")),
        index_mode=str(index.get("mode", "hybrid")),
        translation_enabled=bool(translation.get("enabled", True)),
        translation_strategy=str(translation.get("strategy", "lazy")),
        product_adapter=_normalize_product_adapter(product.get("adapter", "embedded")),
        runtime=RuntimeSettings(
            execution_mode=str(runtime.get("execution_mode", "inline")),
            max_workers=int(runtime.get("max_workers", 2)),
            poll_interval_ms=int(runtime.get("poll_interval_ms", 1000)),
            max_upload_bytes=int(runtime.get("max_upload_bytes", 0)),
            max_inflight_jobs=int(runtime.get("max_inflight_jobs", 0)),
            allow_external_file_paths=bool(runtime.get("allow_external_file_paths", False)),
            staged_upload_max_bytes=int(runtime.get("staged_upload_max_bytes", 0)),
            staged_upload_retention_seconds=int(runtime.get("staged_upload_retention_seconds", 86400)),
            quota_enforce=bool(runtime.get("quota_enforce", False)),
            quota_window_hours=float(runtime.get("quota_window_hours", 24.0)),
            quota_default_limit_units=int(runtime.get("quota_default_limit_units", 0)),
            quota_limits=_freeze_int_mapping(runtime.get("quota_limits")),
            max_attempts=int(runtime.get("max_attempts", 3)),
            log_path=str(runtime.get("log_path", "var/logs/job_events.jsonl")),
            api_key_env=str(runtime.get("api_key_env", "")),
            staged_upload_api_key_env=str(runtime.get("staged_upload_api_key_env", "")),
        ),
        parsers=parser_settings,
        providers=ProviderSettings(
            llm=llm_settings,
            embedding=embedding_settings,
            ocr=ocr_settings,
        ),
    )
