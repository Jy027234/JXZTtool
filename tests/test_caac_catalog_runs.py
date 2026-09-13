from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from parsecore.catalog_runs import (
    CatalogRunError,
    build_catalog_run_ledger,
    compare_catalog_record_sets,
    initialize_catalog_run,
    load_catalog_run,
    recover_interrupted_catalog_parts,
    select_catalog_parts,
)


def _ledger(
    source: Path,
    output: Path,
    *,
    section_key: str = "pma_item",
    page_start: int = 162,
    page_end: int = 16785,
) -> dict[str, object]:
    return build_catalog_run_ledger(
        source=source,
        output=output,
        run_id="caac-pma-2025-test",
        expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        expected_page_count=17101,
        data_cutoff_on="2024-12-31",
        section_key=section_key,
        page_start=page_start,
        page_end=page_end,
        profile_name="large-pdf-catalog",
        target_pages_per_part=200,
        part_context_pages=1,
        max_active_parts_per_doc=2,
        record_schema="caac-approved-catalog.v1",
        verify_source=False,
    )


def test_m3_plan_has_84_owned_parts_and_one_preceding_context_page() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "catalog.pdf"
        source.write_bytes(b"not-read-in-plan-test")
        ledger = _ledger(source, root / "run")

    parts = ledger["parts"]
    assert isinstance(parts, list)
    assert len(parts) == 84
    assert parts[0]["owned_page_start"] == 162
    assert parts[0]["owned_page_end"] == 361
    assert parts[0]["input_page_start"] == 162
    assert parts[1]["owned_page_start"] == 362
    assert parts[1]["input_page_start"] == 361
    assert parts[-1]["owned_page_end"] == 16785
    assert sum(int(part["page_count"]) for part in parts) == 16624
    assert ledger["publicationAllowed"] is False


def test_enabled_section_plans_are_enabled_only_for_registered_spans() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "catalog.pdf"
        source.write_bytes(b"not-read-in-plan-test")
        ledger = _ledger(
            source,
            root / "run",
            section_key="ctsoa",
            page_start=16786,
            page_end=16799,
        )

        assert ledger["scope"] == {
            "sectionKey": "ctsoa",
            "ownedPageStart": 16786,
            "ownedPageEnd": 16799,
            "ownedPageCount": 14,
        }
        assert len(ledger["parts"]) == 1
        assert ledger["parts"][0]["input_page_start"] == 16786
        assert ledger["parts"][0]["owned_page_end"] == 16799

        foreign_ledger = _ledger(
            source,
            root / "foreign-recognition-run",
            section_key="foreign_tso_recognition",
            page_start=16800,
            page_end=16803,
        )
        assert foreign_ledger["scope"] == {
            "sectionKey": "foreign_tso_recognition",
            "ownedPageStart": 16800,
            "ownedPageEnd": 16803,
            "ownedPageCount": 4,
        }
        assert len(foreign_ledger["parts"]) == 1
        assert foreign_ledger["parts"][0]["input_page_start"] == 16800
        assert foreign_ledger["parts"][0]["owned_page_end"] == 16803

        vtc_ledger = _ledger(
            source,
            root / "vtc-run",
            section_key="vtc",
            page_start=16804,
            page_end=16826,
        )
        assert vtc_ledger["scope"] == {
            "sectionKey": "vtc",
            "ownedPageStart": 16804,
            "ownedPageEnd": 16826,
            "ownedPageCount": 23,
        }
        assert len(vtc_ledger["parts"]) == 1
        assert vtc_ledger["parts"][0]["input_page_start"] == 16804
        assert vtc_ledger["parts"][0]["owned_page_end"] == 16826

        tc_ledger = _ledger(
            source,
            root / "tc-run",
            section_key="tc",
            page_start=3,
            page_end=7,
        )
        assert tc_ledger["scope"] == {
            "sectionKey": "tc",
            "ownedPageStart": 3,
            "ownedPageEnd": 7,
            "ownedPageCount": 5,
        }
        assert len(tc_ledger["parts"]) == 1
        assert tc_ledger["parts"][0]["input_page_start"] == 3
        assert tc_ledger["parts"][0]["owned_page_end"] == 7

        foreign_product_ledger = _ledger(
            source,
            root / "foreign-product-run",
            section_key="foreign_product_recognition",
            page_start=8,
            page_end=9,
        )
        assert foreign_product_ledger["scope"] == {
            "sectionKey": "foreign_product_recognition",
            "ownedPageStart": 8,
            "ownedPageEnd": 9,
            "ownedPageCount": 2,
        }
        assert len(foreign_product_ledger["parts"]) == 1
        assert foreign_product_ledger["parts"][0]["input_page_start"] == 8
        assert foreign_product_ledger["parts"][0]["owned_page_end"] == 9

        tda_ledger = _ledger(
            source,
            root / "tda-run",
            section_key="tda",
            page_start=10,
            page_end=10,
        )
        assert tda_ledger["scope"] == {
            "sectionKey": "tda",
            "ownedPageStart": 10,
            "ownedPageEnd": 10,
            "ownedPageCount": 1,
        }
        assert len(tda_ledger["parts"]) == 1
        assert tda_ledger["parts"][0]["input_page_start"] == 10
        assert tda_ledger["parts"][0]["owned_page_end"] == 10

        pc_ledger = _ledger(
            source,
            root / "pc-run",
            section_key="pc",
            page_start=11,
            page_end=26,
        )
        assert pc_ledger["scope"] == {
            "sectionKey": "pc",
            "ownedPageStart": 11,
            "ownedPageEnd": 26,
            "ownedPageCount": 16,
        }
        assert len(pc_ledger["parts"]) == 1
        assert pc_ledger["parts"][0]["input_page_start"] == 11
        assert pc_ledger["parts"][0]["owned_page_end"] == 26

        holder_ledger = _ledger(
            source,
            root / "pma-holder-run",
            section_key="pma_holder",
            page_start=155,
            page_end=161,
        )
        assert holder_ledger["scope"] == {
            "sectionKey": "pma_holder",
            "ownedPageStart": 155,
            "ownedPageEnd": 161,
            "ownedPageCount": 7,
        }
        assert len(holder_ledger["parts"]) == 1
        assert holder_ledger["parts"][0]["input_page_start"] == 155
        assert holder_ledger["parts"][0]["owned_page_end"] == 161

        stc_ledger = _ledger(
            source,
            root / "stc-run",
            section_key="stc",
            page_start=28,
            page_end=54,
        )
        assert stc_ledger["scope"] == {
            "sectionKey": "stc",
            "ownedPageStart": 28,
            "ownedPageEnd": 54,
            "ownedPageCount": 27,
        }
        assert len(stc_ledger["parts"]) == 1
        assert stc_ledger["parts"][0]["input_page_start"] == 28
        assert stc_ledger["parts"][0]["owned_page_end"] == 54

        mda_ledger = _ledger(
            source,
            root / "mda-run",
            section_key="mda",
            page_start=55,
            page_end=154,
        )
        assert mda_ledger["scope"] == {
            "sectionKey": "mda",
            "ownedPageStart": 55,
            "ownedPageEnd": 154,
            "ownedPageCount": 100,
        }
        assert len(mda_ledger["parts"]) == 1
        assert mda_ledger["parts"][0]["input_page_start"] == 55
        assert mda_ledger["parts"][0]["owned_page_end"] == 154

        vstc_ledger = _ledger(
            source,
            root / "vstc-run",
            section_key="vstc",
            page_start=16827,
            page_end=17050,
        )
        assert vstc_ledger["scope"] == {
            "sectionKey": "vstc",
            "ownedPageStart": 16827,
            "ownedPageEnd": 17050,
            "ownedPageCount": 224,
        }
        assert len(vstc_ledger["parts"]) == 2
        assert vstc_ledger["parts"][0]["input_page_start"] == 16827
        assert vstc_ledger["parts"][1]["owned_page_end"] == 17050

        vda_ledger = _ledger(
            source,
            root / "vda-run",
            section_key="vda",
            page_start=17051,
            page_end=17100,
        )
        assert vda_ledger["scope"] == {
            "sectionKey": "vda",
            "ownedPageStart": 17051,
            "ownedPageEnd": 17100,
            "ownedPageCount": 50,
        }
        assert len(vda_ledger["parts"]) == 1
        assert vda_ledger["parts"][0]["input_page_start"] == 17051
        assert vda_ledger["parts"][0]["owned_page_end"] == 17100

        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "wrong-range",
                section_key="ctsoa",
                page_start=16786,
                page_end=16798,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "foreign-wrong-range",
                section_key="foreign_tso_recognition",
                page_start=16800,
                page_end=16802,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "vtc-wrong-range",
                section_key="vtc",
                page_start=16804,
                page_end=16825,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "pma-holder-wrong-range",
                section_key="pma_holder",
                page_start=155,
                page_end=160,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "tc-wrong-range",
                section_key="tc",
                page_start=3,
                page_end=6,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "foreign-product-wrong-range",
                section_key="foreign_product_recognition",
                page_start=8,
                page_end=8,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "tda-wrong-range",
                section_key="tda",
                page_start=10,
                page_end=11,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "pc-wrong-range",
                section_key="pc",
                page_start=11,
                page_end=25,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "stc-wrong-range",
                section_key="stc",
                page_start=28,
                page_end=53,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "mda-wrong-range",
                section_key="mda",
                page_start=55,
                page_end=153,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "vstc-wrong-range",
                section_key="vstc",
                page_start=16827,
                page_end=17049,
            )
        with pytest.raises(CatalogRunError, match="must match the registered section span"):
            _ledger(
                source,
                root / "vda-wrong-range",
                section_key="vda",
                page_start=17051,
                page_end=17099,
            )
        with pytest.raises(CatalogRunError, match="must be one of"):
            _ledger(
                source,
                root / "unsupported",
                section_key="unsupported",
                page_start=1,
                page_end=1,
            )


def test_resume_recovers_running_parts_and_failed_only_is_exact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "catalog.pdf"
        source.write_bytes(b"not-read-in-plan-test")
        output = root / "run"
        ledger = _ledger(source, output)
        parts = ledger["parts"]
        assert isinstance(parts, list)
        parts[0]["state"] = "done"
        parts[1]["state"] = "running"
        parts[2]["state"] = "failed"
        initialize_catalog_run(output, ledger)

        loaded = load_catalog_run(output)
        assert recover_interrupted_catalog_parts(loaded) == 1
        assert loaded["parts"][1]["state"] == "pending"
        selected_failed = select_catalog_parts(
            loaded,
            failed_only=True,
            max_parts=10,
        )
        selected_resume = select_catalog_parts(
            loaded,
            failed_only=False,
            max_parts=3,
        )

    assert [part["part_index"] for part in selected_failed] == [3]
    assert [part["part_index"] for part in selected_resume] == [2, 3, 4]


def test_profile_gates_reject_ocr_and_more_than_two_active_parts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "catalog.pdf"
        source.write_bytes(b"not-read-in-plan-test")
        common = {
            "source": source,
            "output": root / "run",
            "run_id": "caac-pma-2025-test",
            "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "expected_page_count": 17101,
            "data_cutoff_on": "2024-12-31",
            "section_key": "pma_item",
            "page_start": 162,
            "page_end": 16785,
            "profile_name": "large-pdf-catalog",
            "target_pages_per_part": 200,
            "part_context_pages": 1,
            "record_schema": "caac-approved-catalog.v1",
            "verify_source": False,
        }
        with pytest.raises(CatalogRunError, match="must be 1 or 2"):
            build_catalog_run_ledger(
                **common,
                max_active_parts_per_doc=3,
            )
        with pytest.raises(CatalogRunError, match="requires enable_ocr=false"):
            build_catalog_run_ledger(
                **common,
                max_active_parts_per_doc=2,
                enable_ocr=True,
            )


def test_v2_ledger_requires_and_materializes_the_reviewed_omission_allow_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "catalog.pdf"
        source.write_bytes(b"not-read-in-plan-test")
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        review_path = root / "vstc-source-omissions.json"
        review_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "caac-approved-catalog.source-omission-review.v1",
                    "sourceSnapshotSha256": source_sha256,
                    "entries": [
                        {
                            "reviewId": "vstc-test-holder-empty",
                            "sectionKey": "vstc",
                            "rowNumber": 875,
                            "pdfPage": 17023,
                            "fieldPath": "holder.nameOriginal",
                            "reasonCode": "empty_source_cell",
                            "bbox": [111.0, 231.0, 203.0, 409.0],
                            "reviewedOn": "2026-08-17",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        common = {
            "source": source,
            "output": root / "vstc-run",
            "run_id": "caac-vstc-2025-v2-test",
            "expected_source_sha256": source_sha256,
            "expected_page_count": 17101,
            "data_cutoff_on": "2024-12-31",
            "section_key": "vstc",
            "page_start": 16827,
            "page_end": 17050,
            "profile_name": "large-pdf-catalog-v2",
            "target_pages_per_part": 200,
            "part_context_pages": 1,
            "max_active_parts_per_doc": 2,
            "record_schema": "caac-approved-catalog.v2",
            "verify_source": False,
        }
        with pytest.raises(CatalogRunError, match="requires a source_omission_review"):
            build_catalog_run_ledger(**common)

        ledger = build_catalog_run_ledger(
            **common,
            source_omission_review=review_path,
        )
        assert ledger["parser"]["extractorVersion"] == "2.0.0"
        review = ledger["sourceOmissionReview"]
        assert review["artifact"]["path"] == "quality/source-omission-review.json"
        initialize_catalog_run(root / "vstc-run", ledger)
        materialized = root / "vstc-run" / "quality" / "source-omission-review.json"
        assert materialized.is_file()
        assert hashlib.sha256(materialized.read_bytes()).hexdigest() == review["artifact"]["sha256"]


def test_record_set_comparison_is_exact_but_ignores_json_key_order() -> None:
    source_sha256 = "a" * 64
    first = {
        "recordId": "source:ctsoa:1",
        "sectionKey": "ctsoa",
        "source": {"sourceSnapshotSha256": source_sha256},
        "item": {"nameOriginal": "test"},
    }
    second = {
        "item": {"nameOriginal": "test"},
        "source": {"sourceSnapshotSha256": source_sha256},
        "sectionKey": "ctsoa",
        "recordId": "source:ctsoa:1",
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        primary = root / "primary.jsonl"
        repeat = root / "repeat.jsonl"
        primary.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
        repeat.write_text(json.dumps(second, ensure_ascii=False) + "\n", encoding="utf-8")

        report = compare_catalog_record_sets(
            record_paths={"primary": primary, "repeat": repeat},
            section_key="ctsoa",
            expected_source_sha256=source_sha256,
            max_records=10,
        )

    assert report["hardGatePassed"] is True
    assert report["comparisons"] == [
        {
            "baseline": "primary",
            "candidate": "repeat",
            "recordSetMatches": True,
            "recordOrderMatches": True,
            "rawBytesMatch": False,
            "missingFromCandidate": [],
            "missingFromBaseline": [],
            "changedRecordIds": [],
        }
    ]
