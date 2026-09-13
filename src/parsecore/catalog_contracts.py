"""Stable constants and lightweight checks for the CAAC catalog contract.

The authoritative JSON Schema lives in the CAACresource repository.  ParseCore
keeps these constants locally so an extractor cannot silently emit a different
section or approval vocabulary when the two projects are developed separately.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


CAAC_APPROVED_CATALOG_SCHEMA_VERSION = "caac-approved-catalog.v1"
CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION = "caac-approved-catalog.v2"
CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS = frozenset(
    {
        CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
        CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
    }
)
CAAC_APPROVED_CATALOG_EXTRACTOR = "caac-approved-catalog"
CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION = "1.0.0"
CAAC_APPROVED_CATALOG_V2_EXTRACTOR_VERSION = "2.0.0"

CAAC_APPROVAL_TYPES = frozenset(
    {
        "maintenance",
        "tc",
        "foreign_product_recognition",
        "tda",
        "pc",
        "stc",
        "mda",
        "pma",
        "ctsoa",
        "foreign_tso_recognition",
        "vtc",
        "vstc",
        "vda",
    }
)

CAAC_SECTION_KEYS = frozenset(
    {
        "maintenance",
        "tc",
        "foreign_product_recognition",
        "tda",
        "pc",
        "stc",
        "mda",
        "pma_holder",
        "pma_item",
        "ctsoa",
        "foreign_tso_recognition",
        "vtc",
        "vstc",
        "vda",
    }
)

CAAC_RECORD_KINDS = frozenset(
    {"certificate", "project", "recognition", "holder_approval", "approved_item"}
)
CAAC_QUALITY_STATUSES = frozenset({"candidate", "internal_preview", "public", "rejected"})

CAAC_RECORD_REQUIRED_FIELDS = (
    "schemaVersion",
    "recordId",
    "sectionKey",
    "approvalType",
    "recordKind",
    "rowNumber",
    "approval",
    "holder",
    "item",
    "source",
    "quality",
    "fieldEvidence",
)


class CatalogContractError(ValueError):
    """Raised when a record violates the cross-project contract invariants."""


def validate_catalog_record_shape(record: Mapping[str, Any]) -> None:
    """Validate invariants needed before JSON Schema validation.

    This deliberately checks only vocabulary, identity and evidence presence.
    The CAACresource JSON Schema remains authoritative for detailed types and
    nested field requirements.
    """

    missing = [field for field in CAAC_RECORD_REQUIRED_FIELDS if field not in record]
    if missing:
        raise CatalogContractError(f"catalog record missing required fields: {', '.join(missing)}")
    if record.get("schemaVersion") not in CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS:
        raise CatalogContractError("catalog record schemaVersion is unsupported")
    if record.get("sectionKey") not in CAAC_SECTION_KEYS:
        raise CatalogContractError(f"unknown catalog sectionKey: {record.get('sectionKey')!r}")
    if record.get("approvalType") not in CAAC_APPROVAL_TYPES:
        raise CatalogContractError(f"unknown catalog approvalType: {record.get('approvalType')!r}")
    if record.get("recordKind") not in CAAC_RECORD_KINDS:
        raise CatalogContractError(f"unknown catalog recordKind: {record.get('recordKind')!r}")
    quality = record.get("quality")
    if not isinstance(quality, Mapping) or quality.get("status") not in CAAC_QUALITY_STATUSES:
        raise CatalogContractError("catalog record quality.status is not a supported publication status")
    evidence = record.get("fieldEvidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or not evidence:
        raise CatalogContractError("catalog record must contain fieldEvidence")


def catalog_contract_descriptor() -> dict[str, Any]:
    """Return a small descriptor suitable for registry/health payloads."""

    return {
        "schemaVersion": CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
        "supportedSchemaVersions": sorted(CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS),
        "extractor": CAAC_APPROVED_CATALOG_EXTRACTOR,
        "extractorVersion": CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION,
        "approvalTypes": sorted(CAAC_APPROVAL_TYPES),
        "sectionKeys": sorted(CAAC_SECTION_KEYS),
        "recordKinds": sorted(CAAC_RECORD_KINDS),
    }
