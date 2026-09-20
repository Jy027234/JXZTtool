"""P0-01: configuration and capabilities must be truthful.

Covers the four acceptance points from
``docs/parsecore-reliability-mineru-todo-20260920.md`` P0-01:

* unknown parser/index/provider names fail at startup with a stable error code;
* a missing credential degrades a declared capability in a visible way;
* an index backend mismatch is rejected instead of silently becoming NullIndex;
* the placeholder-parser probe can no longer produce a result presented as a
  formal parse result.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from parsecore.api_health import (
    clear_probe_cache,
    health_capabilities,
    health_service_details,
)
from parsecore.bootstrap import _build_index, build_runtime
from parsecore.capabilities import (
    CapabilityConfigurationError,
    build_capability_report,
    index_backend_for,
)
from parsecore.config import load_settings
from parsecore.models import ParseRequest
from parsecore.stubs import NullIndex, StubParser
from tests.support import TemporaryWorkspace


BASE_CONFIG = """
[project]
name = "test-capabilities"
mode = "embedded-sdk"
{project_extra}

[runtime]
execution_mode = "inline"
{resource_extra}

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "{index_mode}"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "{parser_name}"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
{extra}
"""

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _config(
    *,
    parser_name: str = "docx-native",
    index_mode: str = "hybrid",
    project_extra: str = "",
    resource_extra: str = "",
    extra: str = "",
) -> str:
    return BASE_CONFIG.format(
        parser_name=parser_name,
        index_mode=index_mode,
        project_extra=project_extra,
        resource_extra=resource_extra,
        extra=extra,
    ).strip()


def _write_config(workspace: TemporaryWorkspace, text: str) -> Path:
    path = workspace.root / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class CapabilityConfigValidationTests(unittest.TestCase):
    def test_unknown_parser_name_is_rejected_with_stable_code(self) -> None:
        with TemporaryWorkspace(_config(parser_name="mineru-local")) as workspace:
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(workspace.config_path)

        error = captured.exception
        self.assertEqual(error.code, "unknown_parser_name")
        self.assertEqual(error.field, "parsers.name")
        self.assertIn("mineru-local", str(error))
        self.assertEqual(
            error.as_dict()["code"], "unknown_parser_name"
        )

    def test_placeholder_parser_requires_explicit_opt_in(self) -> None:
        with TemporaryWorkspace(
            _config(
                parser_name="mineru-local",
                project_extra="allow_placeholder_parsers = true",
            )
        ) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        self.assertEqual(runtime.placeholder_parser_names(), ("mineru-local",))
        self.assertEqual(report["placeholder_parsers"], ["mineru-local"])
        self.assertFalse(report["formal_parse_ready"])
        self.assertTrue(report["accepting_jobs"])
        self.assertEqual(
            report["blocking_reasons"][0]["reason"], "placeholder_parsers_not_allowed"
        )

    def test_unknown_index_mode_is_rejected(self) -> None:
        with TemporaryWorkspace(_config(index_mode="elastic")) as workspace:
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(workspace.config_path)

        self.assertEqual(captured.exception.code, "unknown_index_mode")

    def test_pgvector_with_sqlite_is_rejected_instead_of_silent_null_index(self) -> None:
        with TemporaryWorkspace(_config(index_mode="pgvector")) as workspace:
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(workspace.config_path)

        self.assertEqual(captured.exception.code, "index_backend_mismatch")
        self.assertEqual(captured.exception.field, "index.backend")

    def test_build_index_rejects_pgvector_without_postgres(self) -> None:
        with self.assertRaises(CapabilityConfigurationError) as captured:
            _build_index("sqlite:///:memory:", "pgvector")
        self.assertEqual(captured.exception.code, "index_backend_mismatch")

        # The documented non-durable configurations still resolve to NullIndex.
        self.assertIsInstance(_build_index("sqlite:///./var/x.db", "hybrid"), NullIndex)
        self.assertIsInstance(_build_index("postgresql://localhost/db", "null"), NullIndex)

    def test_unknown_database_scheme_is_rejected(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            text = workspace.config_path.read_text(encoding="utf-8")
            text = text.replace(
                'database_url = "sqlite:///',
                'database_url = "mysql://',
            )
            self.assertIn("mysql://", text)
            path = _write_config(workspace, text)
            with self.assertRaises(CapabilityConfigurationError) as captured:
                load_settings(path)

        self.assertEqual(captured.exception.code, "unknown_database_scheme")

    def test_mock_provider_requires_explicit_opt_in(self) -> None:
        embedding_block = """
[providers.embedding]
enabled = true
provider = "fake"
model = "fake-embedding"
base_url = "http://localhost:9/v1"
"""
        with TemporaryWorkspace(_config(extra=embedding_block)) as workspace:
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(workspace.config_path)
        self.assertEqual(captured.exception.code, "mock_providers_not_allowed")

        with TemporaryWorkspace(
            _config(extra=embedding_block, resource_extra="allow_mock_providers = true")
        ) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        self.assertTrue(report["accepting_jobs"])
        self.assertIn("embedding", report["mock_capabilities"])

    def test_unknown_provider_name_is_rejected(self) -> None:
        embedding_block = """
[providers.embedding]
enabled = true
provider = "not-a-provider"
"""
        with TemporaryWorkspace(_config(extra=embedding_block)) as workspace:
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(workspace.config_path)

        self.assertEqual(captured.exception.code, "unknown_embedding_provider")

    def test_unknown_capability_name_in_capability_optional_is_rejected(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            text = workspace.config_path.read_text(encoding="utf-8").replace(
                'execution_mode = "inline"',
                'execution_mode = "inline"\ncapability_optional = ["telepathy"]',
            )
            path = _write_config(workspace, text)
            with self.assertRaises(CapabilityConfigurationError) as captured:
                load_settings(path)

        self.assertEqual(captured.exception.code, "unknown_capability_name")

    def test_capability_optional_accepts_parser_capability_names(self) -> None:
        with TemporaryWorkspace(
            _config(resource_extra='capability_optional = ["parser:docx-native"]')
        ) as workspace:
            settings = load_settings(workspace.config_path)
            report = build_capability_report(settings, parser_names=("docx-native",))

        entry = next(
            item for item in report["capabilities"] if item["name"] == "parser:docx-native"
        )
        self.assertEqual(entry["requirement"], "optional")

    def test_database_url_override_revalidates_index_backend(self) -> None:
        with TemporaryWorkspace(_config(index_mode="pgvector")) as workspace:
            text = workspace.config_path.read_text(encoding="utf-8").replace(
                f'database_url = "sqlite:///{workspace.root.as_posix()}/parsecore.db"',
                'database_url = "postgresql://localhost/parsecore"',
            )
            self.assertIn("postgresql://", text)
            workspace.config_path.write_text(text, encoding="utf-8")
            with self.assertRaises(CapabilityConfigurationError) as captured:
                build_runtime(
                    workspace.config_path, database_url_override="sqlite:///./var/x.db"
                )

        self.assertEqual(captured.exception.code, "index_backend_mismatch")


class CapabilityReportTests(unittest.TestCase):
    def test_disabled_optional_capability_is_not_reported_as_a_fault(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        self.assertTrue(report["accepting_jobs"])
        self.assertTrue(report["formal_parse_ready"])
        self.assertIn("embedding", report["disabled_capabilities"])
        self.assertEqual(report["unavailable_capabilities"], [])
        self.assertEqual(
            [item["name"] for item in report["degraded_capabilities"]], ["index"]
        )

    def test_optional_rerank_degradation_is_visible_but_does_not_block(self) -> None:
        rerank_block = """
[providers.rerank]
enabled = true
provider = "dashscope-compatible"
model = "qwen/qwen3-vl-rerank"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_TEST_RERANK_KEY"
"""
        with TemporaryWorkspace(_config(extra=rerank_block)) as workspace:
            with patch.dict(
                os.environ, {"PARSECORE_TEST_RERANK_KEY": ""}, clear=False
            ):
                runtime = build_runtime(workspace.config_path)
                report = runtime.capability_report()
                health = health_capabilities(runtime)

        self.assertTrue(report["accepting_jobs"])
        self.assertTrue(report["formal_parse_ready"])
        degraded = [item["name"] for item in report["degraded_capabilities"]]
        self.assertIn("rerank", degraded)
        self.assertEqual(health["status"], "degraded")

    def test_required_capability_without_credential_reports_not_ready(self) -> None:
        embedding_block = """
[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_TEST_EMBED_KEY"
"""
        with TemporaryWorkspace(_config(extra=embedding_block)) as workspace:
            with patch.dict(
                os.environ, {"PARSECORE_TEST_EMBED_KEY": ""}, clear=False
            ):
                runtime = build_runtime(workspace.config_path)
                report = runtime.capability_report()
                health = health_capabilities(runtime)

        self.assertFalse(report["accepting_jobs"])
        self.assertFalse(report["formal_parse_ready"])
        self.assertEqual(health["status"], "not_ready")
        self.assertIn(
            "embedding", [item["name"] for item in report["unavailable_capabilities"]]
        )

    def test_declared_optional_embedding_degrades_instead_of_blocking(self) -> None:
        embedding_block = """
[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_TEST_EMBED_KEY"
"""
        with TemporaryWorkspace(
            _config(
                extra=embedding_block,
                resource_extra='capability_optional = ["embedding"]',
            )
        ) as workspace:
            with patch.dict(
                os.environ, {"PARSECORE_TEST_EMBED_KEY": ""}, clear=False
            ):
                runtime = build_runtime(workspace.config_path)
                report = runtime.capability_report()

        self.assertTrue(report["accepting_jobs"])
        self.assertTrue(report["formal_parse_ready"])
        self.assertEqual(
            [item["name"] for item in report["degraded_capabilities"]],
            ["embedding", "index"],
        )

    def test_in_memory_index_is_reported_as_non_durable(self) -> None:
        with TemporaryWorkspace(_config(index_mode="hybrid")) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        index_entry = next(
            entry for entry in report["capabilities"] if entry["name"] == "index"
        )
        self.assertFalse(index_entry["detail"]["durable"])
        self.assertEqual(index_entry["detail"]["backend"], "null")
        self.assertFalse(index_entry["available"])
        # Reported as a degradation, not as a reason to reject every job.
        self.assertEqual(index_entry["status"], "degraded")
        self.assertEqual(index_entry["requirement"], "optional")
        self.assertFalse(report["durable_index"])
        self.assertTrue(report["accepting_jobs"])

    def test_index_backend_for_resolves_only_postgres_as_durable(self) -> None:
        self.assertEqual(index_backend_for("postgresql://h/db", "pgvector"), "pgvector")
        self.assertEqual(index_backend_for("sqlite:///x.db", "pgvector"), "null")
        self.assertEqual(index_backend_for("memory://", "hybrid"), "null")

    def test_capability_fingerprint_is_stable_and_content_sensitive(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            settings = load_settings(workspace.config_path)
            first = build_capability_report(settings, parser_names=("docx-native",))
            second = build_capability_report(settings, parser_names=("docx-native",))
            third = build_capability_report(
                settings,
                parser_names=("docx-native",),
                placeholder_names=("docx-native",),
            )

        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["fingerprint"], third["fingerprint"])

    def test_describe_exposes_capability_report(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        capabilities = description["capabilities"]
        self.assertEqual(capabilities["schema_version"], "2026-09-capability-report-v1")
        self.assertIn("parser:docx-native", capabilities["enabled_capabilities"])
        self.assertEqual(
            description["runtime"]["capability_fingerprint"], capabilities["fingerprint"]
        )
        self.assertTrue(description["runtime"]["accepting_jobs"])
        self.assertTrue(description["runtime"]["formal_parse_ready"])


class PlaceholderResultTests(unittest.TestCase):
    def test_placeholder_parse_is_flagged_and_not_a_formal_result(self) -> None:
        with TemporaryWorkspace(
            _config(
                parser_name="mineru-local",
                project_extra="allow_placeholder_parsers = true",
            )
        ) as workspace:
            document_path = workspace.create_docx(
                "spec.docx", ["Maintenance Manual", "Apply torque"]
            )
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-placeholder",
                    file_path=str(document_path),
                    media_type=DOCX_MEDIA_TYPE,
                )
            )
            stored_job = runtime.get_job(job_id=outcome.job.job_id)
            report = runtime.capability_report()

        self.assertIsNotNone(outcome.placeholder_result)
        assert outcome.placeholder_result is not None
        self.assertEqual(
            outcome.placeholder_result["placeholder_parser"], "mineru-local"
        )
        self.assertFalse(outcome.placeholder_result["formal_result"])
        self.assertEqual(outcome.placeholder_result["result_grade"], "placeholder")
        self.assertIn("placeholder_result", (stored_job.options or {}))
        self.assertIn("mineru-local", report["placeholder_parsers"])
        self.assertFalse(report["formal_parse_ready"])

    def test_placeholder_result_is_attached_to_api_payload(self) -> None:
        from parsecore.api_payloads import _parse_success_response

        with TemporaryWorkspace(
            _config(
                parser_name="mineru-local",
                project_extra="allow_placeholder_parsers = true",
            )
        ) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-placeholder-api",
                    file_path=str(document_path),
                    media_type=DOCX_MEDIA_TYPE,
                )
            )

        payload = _parse_success_response(
            outcome, file_name="spec.docx", mime_type=DOCX_MEDIA_TYPE, enable_ocr=False
        )
        self.assertEqual(
            payload["placeholder_result"],
            {
                "placeholder_parser": "mineru-local",
                "result_grade": "placeholder",
                "formal_result": False,
                "error_code": "placeholder_parsers_not_allowed",
            },
        )

    def test_real_parser_result_has_no_placeholder_flag(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            document_path = workspace.create_docx(
                "spec.docx", ["Maintenance Manual", "Apply torque"]
            )
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-real",
                    file_path=str(document_path),
                    media_type=DOCX_MEDIA_TYPE,
                )
            )
            report = runtime.capability_report()

        self.assertIsNone(outcome.placeholder_result)
        self.assertTrue(report["formal_parse_ready"])
        self.assertNotIsInstance(runtime.parsers[0], StubParser)


class HealthProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_probe_cache()

    def tearDown(self) -> None:
        clear_probe_cache()

    def test_health_capabilities_includes_report_and_placeholder_state(self) -> None:
        with TemporaryWorkspace(
            _config(
                parser_name="mineru-local",
                project_extra="allow_placeholder_parsers = true",
            )
        ) as workspace:
            runtime = build_runtime(workspace.config_path)
            health = health_capabilities(runtime)

        self.assertEqual(health["status"], "not_ready")
        self.assertFalse(health["formal_parse_ready"])
        self.assertEqual(health["placeholder_parsers"], ["mineru-local"])
        self.assertEqual(
            health["blocking_reasons"][0]["reason"], "placeholder_parsers_not_allowed"
        )

    def test_injected_ocr_probe_is_never_cached(self) -> None:
        calls: list[int] = []

        def probe(_runtime: object) -> bool:
            calls.append(1)
            return True

        ocr_block = """
[[parsers]]
name = "image-ocr"
media_types = ["image/png"]
extensions = [".png"]

[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://example.invalid"
"""
        with TemporaryWorkspace(_config(extra=ocr_block)) as workspace:
            runtime = build_runtime(workspace.config_path)
            health_service_details(runtime, ocr_probe=probe)
            health_service_details(runtime, ocr_probe=probe)

        self.assertEqual(len(calls), 2)


class HealthEndpointTests(unittest.TestCase):
    def test_health_reports_delivery_status_and_capabilities(self) -> None:
        from starlette.testclient import TestClient

        from parsecore.asgi import create_app

        with TemporaryWorkspace(_config()) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        # The starter deployment pairs a hybrid index with SQLite, so delivery is
        # honestly reported as degraded rather than "ok".
        self.assertEqual(body["delivery_status"], "degraded")
        self.assertTrue(body["capabilities"]["formal_parse_ready"])
        self.assertEqual(
            body["capabilities"]["report"]["schema_version"],
            "2026-09-capability-report-v1",
        )
        json.dumps(body)

    def test_health_marks_placeholder_deployment_not_ready_without_failing_liveness(
        self,
    ) -> None:
        from starlette.testclient import TestClient

        from parsecore.asgi import create_app

        with TemporaryWorkspace(
            _config(
                parser_name="mineru-local",
                project_extra="allow_placeholder_parsers = true",
            )
        ) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["delivery_status"], "not_ready")
        self.assertEqual(body["capabilities"]["placeholder_parsers"], ["mineru-local"])


if __name__ == "__main__":
    unittest.main()
