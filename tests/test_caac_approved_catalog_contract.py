from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from parsecore.catalog_contracts import (
    CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    CatalogContractError,
    catalog_contract_descriptor,
    validate_catalog_record_shape,
)


FIXTURE = Path(__file__).parent / "fixtures" / "caac-approved-catalog-v1.pma-item.json"


def _record() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_catalog_contract_descriptor_exposes_shared_vocabulary() -> None:
    descriptor = catalog_contract_descriptor()

    assert descriptor["schemaVersion"] == CAAC_APPROVED_CATALOG_SCHEMA_VERSION
    assert "pma" in descriptor["approvalTypes"]
    assert "ctsoa" in descriptor["approvalTypes"]
    assert "pma_item" in descriptor["sectionKeys"]


def test_catalog_record_shape_accepts_pma_fixture() -> None:
    validate_catalog_record_shape(_record())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schemaVersion", "old-generic-record.v1"),
        ("approvalType", "unknown"),
        ("sectionKey", "pma"),
    ],
)
def test_catalog_record_shape_rejects_vocabularies_outside_contract(field: str, value: str) -> None:
    record = copy.deepcopy(_record())
    record[field] = value

    with pytest.raises(CatalogContractError):
        validate_catalog_record_shape(record)


def test_catalog_record_shape_rejects_empty_evidence() -> None:
    record = _record()
    record["fieldEvidence"] = []

    with pytest.raises(CatalogContractError, match="fieldEvidence"):
        validate_catalog_record_shape(record)
