from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parsecore.config import (
    load_settings,
    local_provider_registry_payload,
    local_provider_route_plan_payload,
    quality_gate_payload,
)


_TOML = """
[project]
name = "p"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 1
poll_interval_ms = 500
staged_upload_max_bytes = 104857600
max_active_parts_per_doc = 3
job_timeout_seconds = 120
part_timeout_seconds = 30
retry_backoff_seconds = 2.5
retry_backoff_max_seconds = 45
provider_comparison_artifact_retention_seconds = 172800
api_key_env = "PARSECORE_API_KEY"

[storage]
database_url = "sqlite:///./var/x.db"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"
embedding_dimension = 384

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[quality_gate]
enabled = true
min_text_page_coverage = 0.99
min_table_unit_coverage = 0.96
min_unit_chunk_coverage = 0.97
min_reading_order_confidence = 0.8
allow_local_rerun = false
allow_manual_review = true

[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
batch_size = 8

[providers.rerank]
enabled = true
provider = "dashscope-compatible"
model = "qwen/qwen3-vl-rerank"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_RERANK_API_KEY"
timeout_seconds = 12.5
max_retries = 3
candidate_limit = 24
options = { enable_truncation = true }

[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://example.invalid"
api_key_env = "PARSECORE_OCR_API_KEY"
timeout_seconds = 9.5
max_retries = 4
options = { endpoint_path = "/ocr/v1", det_use_dilation = true }

[providers.local_parser_routing]
enabled = true
fallback_to_default = false
include_disabled = false

[[providers.local_parsers]]
id = "pdf-text"
enabled = true
priority = 100
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default", "table-heavy"]
capabilities = ["native-text", "tables", "layout"]
route_mode = "route"
gate_status = "passed"
gate_checks = ["samples", "license", "performance", "observability"]
options = { adapter = "builtin", local = { command = "builtin" } }

[[providers.local_parsers]]
id = "pymupdf4llm-local"
enabled = false
priority = 80
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default"]
capabilities = ["markdown", "rag-baseline"]
route_mode = "evaluate"
gate_status = "pending"
gate_checks = ["samples", "license", "performance", "observability"]

[[parsers]]
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
options = { post_process = { strip_headers_footers = false, short_block_min_length = 20 } }

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
"""


class LoadSettingsParserOptionsTests(unittest.TestCase):
    def test_database_url_can_be_overridden_by_environment(self) -> None:
        override = "postgresql://parsecore:secret@127.0.0.1:55433/parsecore"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML, encoding="utf-8")
            with patch.dict(
                "os.environ", {"PARSECORE_DATABASE_URL": override}, clear=False
            ):
                settings = load_settings(path)

        self.assertEqual(settings.database_url, override)

    def test_blank_database_url_environment_falls_back_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML, encoding="utf-8")
            with patch.dict(
                "os.environ", {"PARSECORE_DATABASE_URL": "   "}, clear=False
            ):
                settings = load_settings(path)

        self.assertEqual(settings.database_url, "sqlite:///./var/x.db")

    def test_parser_options_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML, encoding="utf-8")
            settings = load_settings(path)

        parsers = {p.name: p for p in settings.parsers}
        pdf = parsers["pdf-text"]
        self.assertEqual(
            dict(pdf.options.get("post_process")),
            {"strip_headers_footers": False, "short_block_min_length": 20},
        )

        docx = parsers["docx-native"]
        self.assertEqual(dict(docx.options), {})
        self.assertTrue(settings.providers.embedding.enabled)
        self.assertEqual(settings.providers.embedding.batch_size, 8)
        self.assertEqual(settings.providers.embedding.model, "text-embedding-3-small")
        self.assertTrue(settings.providers.rerank.enabled)
        self.assertEqual(settings.providers.rerank.provider, "dashscope-compatible")
        self.assertEqual(settings.providers.rerank.model, "qwen/qwen3-vl-rerank")
        self.assertEqual(settings.providers.rerank.candidate_limit, 24)
        self.assertEqual(settings.providers.rerank.timeout_seconds, 12.5)
        self.assertEqual(settings.providers.rerank.max_retries, 3)
        self.assertEqual(
            dict(settings.providers.rerank.options), {"enable_truncation": True}
        )
        self.assertEqual(settings.index_embedding_dimension, 384)
        self.assertTrue(settings.providers.ocr.enabled)
        self.assertEqual(settings.providers.ocr.provider, "remote-http")
        self.assertEqual(settings.providers.ocr.base_url, "https://example.invalid")
        self.assertEqual(settings.providers.ocr.api_key_env, "PARSECORE_OCR_API_KEY")
        self.assertEqual(settings.providers.ocr.timeout_seconds, 9.5)
        self.assertEqual(settings.providers.ocr.max_retries, 4)
        self.assertEqual(settings.runtime.api_key_env, "PARSECORE_API_KEY")
        self.assertFalse(settings.runtime.allow_external_file_paths)
        self.assertEqual(settings.runtime.staged_upload_max_bytes, 104857600)
        self.assertEqual(settings.runtime.max_active_parts_per_doc, 3)
        self.assertEqual(settings.runtime.job_timeout_seconds, 120)
        self.assertEqual(settings.runtime.part_timeout_seconds, 30)
        self.assertEqual(settings.runtime.retry_backoff_seconds, 2.5)
        self.assertEqual(settings.runtime.retry_backoff_max_seconds, 45)
        self.assertEqual(settings.runtime.provider_comparison_artifact_retention_seconds, 172800)
        self.assertTrue(settings.quality_gate.enabled)
        self.assertEqual(settings.quality_gate.min_text_page_coverage, 0.99)
        self.assertEqual(settings.quality_gate.min_table_unit_coverage, 0.96)
        self.assertEqual(settings.quality_gate.min_unit_chunk_coverage, 0.97)
        self.assertEqual(settings.quality_gate.min_reading_order_confidence, 0.8)
        self.assertFalse(settings.quality_gate.allow_local_rerun)
        self.assertTrue(settings.quality_gate.allow_manual_review)

        self.assertEqual(
            dict(settings.providers.ocr.options),
            {"endpoint_path": "/ocr/v1", "det_use_dilation": True},
        )
        self.assertTrue(settings.providers.local_parser_routing.enabled)
        self.assertFalse(settings.providers.local_parser_routing.fallback_to_default)
        self.assertFalse(settings.providers.local_parser_routing.include_disabled)
        local_providers = settings.providers.local_parsers
        self.assertEqual([provider.id for provider in local_providers], ["pdf-text", "pymupdf4llm-local"])
        self.assertTrue(local_providers[0].enabled)
        self.assertEqual(local_providers[0].priority, 100)
        self.assertEqual(local_providers[0].profiles, ("default", "table-heavy"))
        self.assertEqual(local_providers[0].capabilities, ("native-text", "tables", "layout"))
        self.assertEqual(local_providers[0].route_mode, "route")
        self.assertEqual(local_providers[0].gate_status, "passed")
        self.assertEqual(
            local_providers[0].gate_checks,
            ("samples", "license", "performance", "observability"),
        )
        self.assertEqual(local_providers[0].options["adapter"], "builtin")
        self.assertEqual(dict(local_providers[0].options["local"]), {"command": "builtin"})
        self.assertFalse(local_providers[1].enabled)
        self.assertEqual(local_providers[1].route_mode, "evaluate")
        self.assertEqual(local_providers[1].gate_status, "pending")

        registry = local_provider_registry_payload(settings.providers)
        self.assertEqual(registry["schema_version"], "2026-06-local-provider-registry")
        self.assertEqual(
            registry["routing"],
            {
                "enabled": True,
                "fallback_to_default": False,
                "include_disabled": False,
                "routing_policy": "priority_desc_then_id",
            },
        )
        self.assertEqual(
            registry["summary"],
            {
                "total": 2,
                "enabled": 1,
                "disabled": 1,
                "route_ready": 1,
                "evaluation_only": 1,
                "gate_pending": 1,
                "gate_failed": 0,
            },
        )
        self.assertEqual(registry["local_parsers"][0]["id"], "pdf-text")
        self.assertEqual(registry["local_parsers"][0]["admission"]["route_mode"], "route")
        self.assertEqual(registry["local_parsers"][0]["options"]["local"], {"command": "builtin"})
        route_plan = local_provider_route_plan_payload(
            settings.providers,
            media_type="application/pdf",
            extension=".pdf",
            profile="table-heavy",
            required_capabilities=["tables"],
        )
        self.assertEqual(route_plan["schema_version"], "2026-06-local-provider-route-plan")
        self.assertEqual(route_plan["routing_policy"], "priority_desc_then_id")
        self.assertEqual(route_plan["selection"]["primary_provider_id"], "pdf-text")
        self.assertEqual(route_plan["selection"]["eligible_provider_ids"], ["pdf-text"])
        self.assertEqual(route_plan["selection"]["fallback_provider_ids"], [])
        self.assertEqual(route_plan["summary"]["eligible_count"], 1)
        candidates_by_id = {candidate["id"]: candidate for candidate in route_plan["candidates"]}
        self.assertEqual(candidates_by_id["pdf-text"]["route_role"], "primary")
        self.assertTrue(candidates_by_id["pdf-text"]["admission"]["route_ready"])
        self.assertEqual(candidates_by_id["pdf-text"]["matches"]["capabilities"], True)
        self.assertEqual(candidates_by_id["pymupdf4llm-local"]["route_role"], "excluded")
        self.assertIn("disabled", candidates_by_id["pymupdf4llm-local"]["exclusion_reasons"])
        self.assertIn("evaluation_only", candidates_by_id["pymupdf4llm-local"]["exclusion_reasons"])
        self.assertIn("gate_pending", candidates_by_id["pymupdf4llm-local"]["exclusion_reasons"])
        self.assertIn("profile_mismatch", candidates_by_id["pymupdf4llm-local"]["exclusion_reasons"])
        self.assertIn("capability_mismatch", candidates_by_id["pymupdf4llm-local"]["exclusion_reasons"])
        enabled_only_plan = local_provider_route_plan_payload(
            settings.providers,
            file_name="manual.pdf",
            profile="default",
            include_disabled=False,
        )
        self.assertEqual([candidate["id"] for candidate in enabled_only_plan["candidates"]], ["pdf-text"])
        gate = quality_gate_payload(settings.quality_gate)
        self.assertEqual(gate["schema_version"], "2026-06-quality-gate-config")
        self.assertEqual(gate["thresholds"]["min_text_page_coverage"], 0.99)
        self.assertFalse(gate["actions"]["allow_local_rerun"])
        self.assertEqual(gate["enforcement"], "report_only")

    def test_index_embedding_dimension_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(
                _TOML.replace("embedding_dimension = 384", "embedding_dimension = 0"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"index\.embedding_dimension"):
                load_settings(path)

    def test_index_embedding_dimension_defaults_to_1536(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(
                _TOML.replace("embedding_dimension = 384\n", ""),
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertEqual(settings.index_embedding_dimension, 1536)

    def test_rerank_candidate_limit_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(
                _TOML.replace("candidate_limit = 24", "candidate_limit = 0"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"providers\.rerank\.candidate_limit"):
                load_settings(path)

    def test_rerank_candidate_limit_must_be_an_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(
                _TOML.replace("candidate_limit = 24", "candidate_limit = 24.5"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"providers\.rerank\.candidate_limit"):
                load_settings(path)

    def test_provider_comparison_artifact_retention_defaults_to_30_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(
                _TOML.replace(
                    "provider_comparison_artifact_retention_seconds = 172800\n",
                    "",
                ),
                encoding="utf-8",
            )

            settings = load_settings(path)

        self.assertEqual(settings.runtime.provider_comparison_artifact_retention_seconds, 2592000)

    def test_local_provider_route_plan_excludes_evaluation_only_and_gate_failed_candidates(self) -> None:
        config = _TOML.replace(
            'enabled = false\npriority = 80',
            'enabled = true\npriority = 180',
            1,
        ).replace(
            'route_mode = "evaluate"\ngate_status = "pending"',
            'route_mode = "evaluate"\ngate_status = "failed"',
            1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(config, encoding="utf-8")
            settings = load_settings(path)

        route_plan = local_provider_route_plan_payload(
            settings.providers,
            file_name="manual.pdf",
            profile="default",
            required_capabilities=["rag-baseline"],
        )

        self.assertIsNone(route_plan["selection"]["primary_provider_id"])
        self.assertEqual(route_plan["summary"]["route_ready_count"], 1)
        self.assertEqual(route_plan["summary"]["evaluation_only_count"], 1)
        self.assertEqual(route_plan["summary"]["gate_failed_count"], 1)
        candidate = {item["id"]: item for item in route_plan["candidates"]}["pymupdf4llm-local"]
        self.assertEqual(candidate["admission"]["route_mode"], "evaluate")
        self.assertEqual(candidate["admission"]["gate_status"], "failed")
        self.assertFalse(candidate["admission"]["route_ready"])
        self.assertIn("evaluation_only", candidate["exclusion_reasons"])
        self.assertIn("gate_failed", candidate["exclusion_reasons"])

    def test_local_provider_route_plan_ranks_eligible_candidates_by_priority(self) -> None:
        config = _TOML.replace(
            '[[parsers]]\nname = "pdf-text"',
            """
[[providers.local_parsers]]
id = "docling-local"
enabled = true
priority = 140
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["table-heavy"]
capabilities = ["native-text", "tables", "layout"]
route_mode = "route"
gate_status = "passed"

[[parsers]]
name = "pdf-text"
""",
            1,
        ).replace(
            'priority = 100\nmedia_types = ["application/pdf"]',
            'priority = 90\nmedia_types = ["application/pdf"]',
            1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(config, encoding="utf-8")
            settings = load_settings(path)

        route_plan = local_provider_route_plan_payload(
            settings.providers,
            file_name="manual.pdf",
            profile="table-heavy",
            required_capabilities=["tables"],
        )

        self.assertEqual(route_plan["routing_policy"], "priority_desc_then_id")
        self.assertEqual(route_plan["selection"]["primary_provider_id"], "docling-local")
        self.assertEqual(route_plan["selection"]["fallback_provider_ids"], ["pdf-text"])
        self.assertEqual(route_plan["selection"]["eligible_provider_ids"], ["docling-local", "pdf-text"])
        self.assertEqual(route_plan["summary"]["fallback_count"], 1)
        candidates_by_id = {candidate["id"]: candidate for candidate in route_plan["candidates"]}
        self.assertEqual(candidates_by_id["docling-local"]["route_role"], "primary")
        self.assertEqual(candidates_by_id["docling-local"]["selection_rank"], 1)
        self.assertEqual(candidates_by_id["docling-local"]["selection_reason"], "highest_priority_eligible")
        self.assertEqual(candidates_by_id["pdf-text"]["route_role"], "fallback")
        self.assertEqual(candidates_by_id["pdf-text"]["selection_rank"], 2)

    def test_legacy_jobcard_adapter_is_normalized_to_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parsecore.toml"
            path.write_text(_TOML.replace('adapter = "embedded"', 'adapter = "jobcard"'), encoding="utf-8")
            settings = load_settings(path)

        self.assertEqual(settings.product_adapter, "embedded")


if __name__ == "__main__":
    unittest.main()
