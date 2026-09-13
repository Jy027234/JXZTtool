"""Interfaces for opt-in, versioned catalog extractors.

Generic ParseCore records remain the default.  A catalog extractor is selected
only when the caller explicitly supplies the matching ``recordSchema`` option.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..catalog_contracts import (
    CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
)


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """Minimal page input shared by section and row extractors."""

    pdf_page: int
    text: str = ""
    words: tuple[Mapping[str, Any], ...] = ()
    tables: tuple[Mapping[str, Any], ...] = ()
    printed_page_label: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.pdf_page < 1:
            raise ValueError("CatalogPage.pdf_page must be positive")


@dataclass(frozen=True, slots=True)
class CatalogExtractionResult:
    """Streaming-friendly result from one catalog extraction window."""

    records: tuple[Mapping[str, Any], ...] = ()
    field_evidence: tuple[Mapping[str, Any], ...] = ()
    candidate_rows: tuple[Mapping[str, Any], ...] = ()
    rejected_rows: tuple[Mapping[str, Any], ...] = ()
    quality_signals: tuple[Mapping[str, Any], ...] = ()
    continuation_state: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": [dict(record) for record in self.records],
            "fieldEvidence": [dict(item) for item in self.field_evidence],
            "candidateRows": [dict(item) for item in self.candidate_rows],
            "rejectedRows": [dict(item) for item in self.rejected_rows],
            "qualitySignals": [dict(item) for item in self.quality_signals],
            "continuationState": dict(self.continuation_state),
        }


class CatalogExtractor(Protocol):
    """Protocol implemented by each versioned structured catalog extractor."""

    schema_version: str
    extractor_name: str
    extractor_version: str

    def extract(
        self,
        *,
        pages: Sequence[CatalogPage],
        source_snapshot_sha256: str,
        parser_version: str,
        continuation_state: Mapping[str, Any] | None = None,
    ) -> CatalogExtractionResult: ...


class CatalogExtractorRegistry:
    """Explicit registry; no implicit fallback from catalog to generic records."""

    def __init__(self, extractors: Sequence[CatalogExtractor] = ()) -> None:
        self._extractors: dict[str, CatalogExtractor] = {}
        for extractor in extractors:
            self.register(extractor)

    def register(self, extractor: CatalogExtractor) -> None:
        schema_version = str(getattr(extractor, "schema_version", "")).strip()
        if not schema_version:
            raise ValueError("catalog extractor must declare schema_version")
        if schema_version in self._extractors:
            raise ValueError(f"catalog extractor already registered: {schema_version}")
        self._extractors[schema_version] = extractor

    def get(self, record_schema: str) -> CatalogExtractor:
        normalized = str(record_schema or "").strip()
        try:
            return self._extractors[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown catalog recordSchema: {normalized!r}") from exc

    def maybe_get(self, record_schema: str | None) -> CatalogExtractor | None:
        normalized = str(record_schema or "").strip()
        if not normalized:
            return None
        return self._extractors.get(normalized)

    def descriptors(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "schemaVersion": schema_version,
                "extractor": str(getattr(extractor, "extractor_name", "")),
                "extractorVersion": str(getattr(extractor, "extractor_version", "")),
            }
            for schema_version, extractor in sorted(self._extractors.items())
        )


def default_catalog_extractor_registry() -> CatalogExtractorRegistry:
    """Build the currently supported registry without changing old defaults."""

    from .caac_approved_catalog_v1 import (
        CaacApprovedCatalogV1Extractor,
        CaacApprovedCatalogV2Extractor,
    )

    return CatalogExtractorRegistry(
        (CaacApprovedCatalogV1Extractor(), CaacApprovedCatalogV2Extractor())
    )


__all__ = [
    "CAAC_APPROVED_CATALOG_SCHEMA_VERSION",
    "CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION",
    "CatalogExtractionResult",
    "CatalogExtractor",
    "CatalogExtractorRegistry",
    "CatalogPage",
    "default_catalog_extractor_registry",
]
