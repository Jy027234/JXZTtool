"""Versioned extractors for structured CAAC catalog documents."""

from .base import (
    CatalogExtractionResult,
    CatalogExtractor,
    CatalogExtractorRegistry,
    CatalogPage,
    default_catalog_extractor_registry,
)
from .caac_approved_catalog_v1 import (
    build_candidate_record,
    CaacApprovedCatalogV1Extractor,
    CaacApprovedCatalogV2Extractor,
    CatalogSectionSpan,
    ColumnBoundary,
    RowAnchor,
    anchor_row_numbers,
    detect_catalog_sections,
    extract_candidate_rows,
    infer_catalog_header_columns,
    map_words_to_columns,
)

__all__ = [
    "CatalogExtractionResult",
    "CatalogExtractor",
    "CatalogExtractorRegistry",
    "CatalogPage",
    "CaacApprovedCatalogV1Extractor",
    "CaacApprovedCatalogV2Extractor",
    "CatalogSectionSpan",
    "ColumnBoundary",
    "RowAnchor",
    "anchor_row_numbers",
    "build_candidate_record",
    "default_catalog_extractor_registry",
    "detect_catalog_sections",
    "extract_candidate_rows",
    "infer_catalog_header_columns",
    "map_words_to_columns",
]
