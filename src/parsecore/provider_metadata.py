"""Shared provider and adapter provenance metadata helpers."""

from __future__ import annotations

from typing import Any, Mapping

_BUILTIN_PROVIDER_VERSION = "parsecore-builtin"
_BUILTIN_ADAPTER_VERSION = "2026-06-local-provider-adapter"


def _provider_metadata(
    metadata: Mapping[str, Any],
    *,
    parser_name: str,
    provider_id: str | None = None,
    provider_version: Any = None,
    adapter_version: str = _BUILTIN_ADAPTER_VERSION,
) -> dict[str, Any]:
    payload = dict(metadata)
    payload["parser"] = str(payload.get("parser") or parser_name)
    resolved_provider_id = str(payload.get("provider_id") or provider_id or parser_name).strip()
    if resolved_provider_id:
        payload["provider_id"] = resolved_provider_id
    resolved_provider_version = (
        payload.get("provider_version")
        or payload.get("parser_version")
        or provider_version
        or _BUILTIN_PROVIDER_VERSION
    )
    if resolved_provider_version:
        payload["provider_version"] = str(resolved_provider_version)
    if adapter_version and not payload.get("adapter_version"):
        payload["adapter_version"] = adapter_version
    return payload
