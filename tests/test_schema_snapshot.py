"""Schema snapshot tests — lock down the 6 frozen document projection schemas.

Each test asserts:
- ``$schema`` / ``$id`` metadata
- ``x-parsecore`` descriptor (name, category, projection, schema_version)
- ``required`` field list (exact, frozen)
- ``properties`` keys (exact, frozen)
- ``schema_version`` const value
- ``additionalProperties`` is False
"""
from __future__ import annotations

import unittest

from parsecore.api_payloads import (
    DOCUMENT_SCHEMA_VERSION,
    PROVIDER_USAGE_SCHEMA_VERSION,
    READER_SCHEMA_VERSION,
)
from parsecore.ir import COVERAGE_SCHEMA_VERSION, IR_SCHEMA_VERSION
from parsecore.payload_schemas import (
    JSON_SCHEMA_DRAFT,
    _SCHEMA_BASE_URI,
    payload_schema,
    payload_schema_names,
)


_SCHEMA_EXPECTATIONS: dict[str, dict[str, object]] = {
    "document-coverage": {
        "title": "ParseCore Coverage Projection",
        "projection": "coverage",
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "state",
            "coverage",
            "quality_signals",
            "quality_summary",
            "rag_coverage_quality",
            "index_manifest",
            "local_provider_routing",
            "quality_gate",
                    "publication",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "state",
            "coverage",
            "quality_signals",
            "quality_summary",
            "rag_coverage_quality",
            "index_manifest",
            "local_provider_routing",
            "quality_gate",
                    "publication",
        ],
    },
    "document-ir": {
        "title": "ParseCore Document IR Projection",
        "projection": "ir",
        "schema_version": IR_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "profile_resolution",
            "state",
            "provider_registry",
            "local_provider_routing",
            "providers",
            "pages",
            "blocks",
            "tables",
            "figures",
            "knowledge_units",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "coverage",
            "coverage_quality_signals",
            "rag_coverage_quality",
            "ocr_decision_trace",
            "index_manifest",
            "quality_gate",
                    "publication",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "profile_resolution",
            "state",
            "provider_registry",
            "local_provider_routing",
            "providers",
            "pages",
            "blocks",
            "tables",
            "figures",
            "knowledge_units",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "coverage",
            "coverage_quality_signals",
            "rag_coverage_quality",
            "ocr_decision_trace",
            "index_manifest",
            "quality_gate",
                    "publication",
        ],
    },
    "document-parts": {
        "title": "ParseCore Parts Projection",
        "projection": "parts",
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "state",
            "state_filter",
            "parts",
            "part_summary",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "state",
            "state_filter",
            "parts",
            "part_summary",
        ],
    },
    "document-providers": {
        "title": "ParseCore Provider Usage Projection",
        "projection": "providers",
        "schema_version": PROVIDER_USAGE_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "provider_registry",
            "summary",
            "providers",
            "pages",
            "comparison_report",
            "comparison_actions",
            "rag_coverage_quality",
            "quality_gate",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "provider_registry",
            "summary",
            "providers",
            "pages",
            "comparison_report",
            "comparison_actions",
            "rag_coverage_quality",
            "quality_gate",
        ],
    },
    "document-quality": {
        "title": "ParseCore Quality Projection",
        "projection": "quality",
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "quality_summary",
            "coverage_summary",
            "rag_coverage_quality",
            "quality_gate",
            "ocr_decision_trace",
            "parse_units",
            "provider_diagnostics",
            "parts_diagnostics",
            "attention_summary",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "quality",
            "raw_quality",
            "output_quality",
            "quality_signals",
            "quality_summary",
            "coverage_summary",
            "rag_coverage_quality",
            "quality_gate",
            "ocr_decision_trace",
            "parse_units",
            "provider_diagnostics",
            "parts_diagnostics",
            "attention_summary",
        ],
    },
    "document-reader": {
        "title": "ParseCore Reader Projection",
        "projection": "reader",
        "schema_version": READER_SCHEMA_VERSION,
        "required": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "pages",
            "blocks",
            "reader_summary",
            "quality_signals",
            "quality_gate",
            "rag_coverage_quality",
            "index_manifest",
                    "publication",
        ],
        "properties": [
            "schema_version",
            "projection",
            "doc_id",
            "parse_run_id",
            "source_integrity",
            "knowledge_unit_diff",
            "profile",
            "profile_resolution",
            "local_provider_routing",
            "state",
            "pages",
            "blocks",
            "reader_summary",
            "quality_signals",
            "quality_gate",
            "rag_coverage_quality",
            "index_manifest",
                    "publication",
        ],
    },
}


_RUN_SCHEMA_EXPECTATIONS: dict[str, dict[str, object]] = {
    "parse-run": {
        "title": "ParseCore Run Record",
        "schema_version": "2026-09-parse-run-v1",
        "required": [
            "schema_version",
            "contract_version",
            "run_identity",
            "execution_status",
            "validation",
            "publication",
            "artifact_profile",
            "required_artifacts",
            "degraded_artifacts",
            "version_sources",
            "capabilities",
            "legacy",
            "transitions",
        ],
        "properties": [
            "schema_version",
            "contract_version",
            "contract_status",
            "run_identity",
            "execution_status",
            "validation",
            "publication",
            "artifact_profile",
            "required_artifacts",
            "optional_artifacts",
            "disabled_artifacts",
            "degraded_artifacts",
            "version_sources",
            "capabilities",
            "legacy",
            "transitions",
        ],
    },
    "document-publication": {
        "title": "ParseCore Document Publication State",
        "schema_version": "2026-09-document-publication-v1",
        "required": [
            "schema_version",
            "contract_version",
            "tenant_id",
            "doc_id",
            "active_run_id",
            "publication_generation",
            "publication_status",
            "formal_version_available",
            "consumer_signal",
            "source_version",
            "profile",
            "validation",
            "readiness",
            "policy",
            "legacy",
            # Parsed-but-unpublished content is named explicitly, so it is neither
            # mistaken for a formal version nor for "nothing parsed yet".
            "preview_run_id",
            "preview",
        ],
        "properties": [
            "schema_version",
            "contract_version",
            "tenant_id",
            "doc_id",
            "active_run_id",
            "publication_generation",
            "publication_status",
            "formal_version_available",
            "consumer_signal",
            "source_version",
            "profile",
            "validation",
            "readiness",
            "policy",
            "preview_run_id",
            "preview",
            "runs",
            "legacy",
            "diagnostics",
        ],
    },
}


class SchemaSnapshotTests(unittest.TestCase):
    """Lock down the frozen schema definitions — any change requires a schema version bump."""

    def test_schema_names_are_frozen(self) -> None:
        self.assertEqual(
            payload_schema_names(),
            (
                "document-coverage",
                "document-ir",
                "document-parts",
                "document-providers",
                "document-quality",
                "document-reader",
                # P0-02 run/publication contracts, versioned separately from the
                # document projections so a run-contract change does not renumber
                # reader/IR payloads.
                "parse-run",
                "document-publication",
            ),
        )

    def test_run_contract_schemas_have_correct_metadata(self) -> None:
        for name, expect in _RUN_SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                self.assertEqual(schema["$schema"], JSON_SCHEMA_DRAFT)
                self.assertEqual(schema["$id"], f"{_SCHEMA_BASE_URI}/{name}.json")
                self.assertEqual(schema["title"], expect["title"])
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                meta = schema["x-parsecore"]
                self.assertEqual(meta["name"], name)
                self.assertEqual(meta["category"], "run_publication")
                self.assertEqual(meta["schema_version"], expect["schema_version"])
                self.assertEqual(
                    schema["properties"]["schema_version"]["const"],
                    expect["schema_version"],
                )
                self.assertEqual(schema["required"], expect["required"])
                self.assertEqual(
                    sorted(schema["properties"]), sorted(expect["properties"])
                )

    def test_each_schema_has_correct_json_schema_metadata(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                self.assertEqual(schema["$schema"], JSON_SCHEMA_DRAFT)
                self.assertEqual(schema["$id"], f"{_SCHEMA_BASE_URI}/{name}.json")
                self.assertEqual(schema["title"], expect["title"])
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)

    def test_each_schema_x_parsecore_descriptor_is_frozen(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                meta = schema["x-parsecore"]
                self.assertEqual(meta["name"], name)
                self.assertEqual(meta["category"], "document_projection")
                self.assertEqual(meta["projection"], expect["projection"])
                self.assertEqual(meta["schema_version"], expect["schema_version"])

    def test_each_schema_required_fields_are_frozen(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                self.assertEqual(schema["required"], expect["required"])

    def test_each_schema_property_keys_are_frozen(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                self.assertEqual(sorted(schema["properties"]), sorted(expect["properties"]))

    def test_each_schema_version_const_matches_x_parsecore(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                version_const = schema["properties"]["schema_version"]["const"]
                self.assertEqual(version_const, expect["schema_version"])
                self.assertEqual(schema["x-parsecore"]["schema_version"], expect["schema_version"])

    def test_each_schema_projection_const_is_correct(self) -> None:
        for name, expect in _SCHEMA_EXPECTATIONS.items():
            with self.subTest(name=name):
                schema = payload_schema(name)
                self.assertEqual(
                    schema["properties"]["projection"]["const"],
                    expect["projection"],
                )


if __name__ == "__main__":
    unittest.main()
