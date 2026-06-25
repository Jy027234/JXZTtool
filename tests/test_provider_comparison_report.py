from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import TemporaryWorkspace
from tools.provider_comparison_report import (
    _comparison_gate_summary,
    _provider_admission_summary,
    build_report,
    main as provider_comparison_main,
    render_markdown,
)


PROVIDER_COMPARE_CONFIG = """
[project]
name = "test-provider-comparison"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 1
poll_interval_ms = 25

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[providers.local_parsers]]
id = "text-native"
enabled = true
priority = 100
media_types = ["text/plain"]
extensions = [".txt", ".md"]
profiles = ["default"]
capabilities = ["native-text", "rag-baseline"]

[[providers.local_parsers]]
id = "pymupdf4llm-local"
enabled = false
priority = 80
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default"]
capabilities = ["markdown", "rag-baseline"]

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt", ".md"]
""".strip()

PDF_PROVIDER_COMPARE_CONFIG = (
    PROVIDER_COMPARE_CONFIG
    + """

[[providers.local_parsers]]
id = "pdf-text"
enabled = true
priority = 120
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default"]
capabilities = ["native-text", "layout", "tables", "local-ocr-fallback"]

[[parsers]]
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
""".rstrip()
)

DOCLING_PROVIDER_COMPARE_CONFIG = (
    PROVIDER_COMPARE_CONFIG
    + """

[[providers.local_parsers]]
id = "docling-local"
enabled = true
priority = 90
media_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".pdf", ".docx"]
profiles = ["default", "table-heavy"]
capabilities = ["layout", "tables", "reading-order"]

[[parsers]]
name = "docling-local"
media_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".pdf", ".docx"]
options = { detect_tables = true }
""".rstrip()
)


def _install_fake_docling(converter_cls: type[object]) -> dict[str, ModuleType]:
    docling_module = ModuleType("docling")
    docling_module.__version__ = "2.test"
    converter_module = ModuleType("docling.document_converter")
    converter_module.DocumentConverter = converter_cls
    docling_module.document_converter = converter_module
    return {
        "docling": docling_module,
        "docling.document_converter": converter_module,
    }


class ProviderComparisonReportTests(unittest.TestCase):
    def test_gate_summary_warns_on_low_reading_order_confidence(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-layout",
                    "route_selection": {"primary_provider_id": "pdf-text"},
                    "best_provider_id": "pdf-text",
                    "providers": [
                        {
                            "provider_id": "pdf-text",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                            "provider_report": {
                                "comparison_report": {
                                    "rankings": [
                                        {
                                            "provider_id": "pdf-text",
                                            "axes": {
                                                "reading_order": {
                                                    "status": "warning",
                                                    "reading_order_confidence": 0.61,
                                                    "threshold": 0.75,
                                                }
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        )

        self.assertEqual(gate_summary["gate"], "accept_with_warning")
        self.assertTrue(gate_summary["passed"])
        self.assertIn("provider_reading_order_warnings", gate_summary["warnings"])
        self.assertIn("provider_quality_warnings", gate_summary["warnings"])
        self.assertEqual(gate_summary["provider_reading_order_warning_runs"], 1)
        self.assertEqual(gate_summary["provider_quality_warning_runs"], 1)
        self.assertEqual(gate_summary["findings"][0]["code"], "provider_reading_order_warning")

    def test_gate_summary_fails_when_reading_order_warning_budget_is_exceeded(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-layout",
                    "route_selection": {"primary_provider_id": "pdf-text"},
                    "best_provider_id": "pdf-text",
                    "providers": [
                        {
                            "provider_id": "pdf-text",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                            "provider_report": {
                                "comparison_report": {
                                    "rankings": [
                                        {
                                            "provider_id": "pdf-text",
                                            "axes": {
                                                "reading_order": {
                                                    "status": "warning",
                                                    "reading_order_confidence": 0.61,
                                                    "threshold": 0.75,
                                                }
                                            },
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                }
            ],
            gate_policy={"max_provider_reading_order_warning_runs": 0},
        )

        self.assertEqual(gate_summary["gate"], "fail")
        self.assertFalse(gate_summary["passed"])
        self.assertIn("provider_reading_order_warning_budget_exceeded", gate_summary["flags"])
        self.assertEqual(gate_summary["gate_policy"]["max_provider_reading_order_warning_runs"], 0)

    def test_gate_summary_fails_when_provider_quality_warning_budget_is_exceeded(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-quality",
                    "route_selection": {"primary_provider_id": "pdf-text"},
                    "best_provider_id": "pdf-text",
                    "providers": [
                        {
                            "provider_id": "pdf-text",
                            "status": "done",
                            "rag_coverage_quality": {
                                "gate": "accept_with_warning",
                                "flags": ["rag_units_without_chunks"],
                            },
                        }
                    ],
                }
            ],
            gate_policy={"max_provider_quality_warning_runs": 0},
        )

        self.assertEqual(gate_summary["gate"], "fail")
        self.assertFalse(gate_summary["passed"])
        self.assertIn("provider_quality_warning_budget_exceeded", gate_summary["flags"])
        self.assertEqual(gate_summary["provider_quality_warning_runs"], 1)
        self.assertEqual(gate_summary["gate_policy"]["max_provider_quality_warning_runs"], 0)

    def test_gate_summary_fails_when_best_provider_mismatch_budget_is_exceeded(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-route",
                    "route_selection": {"primary_provider_id": "pdf-text"},
                    "best_provider_id": "docling-local",
                    "providers": [
                        {
                            "provider_id": "pdf-text",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        },
                        {
                            "provider_id": "docling-local",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        },
                    ],
                }
            ],
            gate_policy={"max_samples_best_provider_differs_from_route_primary": 0},
        )

        self.assertEqual(gate_summary["gate"], "fail")
        self.assertFalse(gate_summary["passed"])
        self.assertIn("best_provider_differs_from_route_primary_budget_exceeded", gate_summary["flags"])
        self.assertEqual(gate_summary["samples_best_provider_differs_from_route_primary"], 1)
        self.assertEqual(
            gate_summary["gate_policy"]["max_samples_best_provider_differs_from_route_primary"],
            0,
        )

    def test_gate_summary_warns_on_provider_identity_drift(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-a",
                    "route_selection": {"primary_provider_id": "docling-local"},
                    "best_provider_id": "docling-local",
                    "providers": [
                        {
                            "provider_id": "docling-local",
                            "provider_version": "2.18.0",
                            "adapter_version": "2026-06-local-provider-adapter",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        }
                    ],
                },
                {
                    "sample_name": "manual-b",
                    "route_selection": {"primary_provider_id": "docling-local"},
                    "best_provider_id": "docling-local",
                    "providers": [
                        {
                            "provider_id": "docling-local",
                            "provider_version": "2.19.0",
                            "adapter_version": "2026-06-local-provider-adapter",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        }
                    ],
                },
            ]
        )

        self.assertEqual(gate_summary["gate"], "accept_with_warning")
        self.assertTrue(gate_summary["passed"])
        self.assertIn("provider_version_drift", gate_summary["warnings"])
        self.assertEqual(gate_summary["providers_with_multiple_provider_versions"], 1)
        self.assertEqual(gate_summary["providers_with_multiple_adapter_versions"], 0)

    def test_gate_summary_fails_when_provider_identity_drift_budget_is_exceeded(self) -> None:
        gate_summary = _comparison_gate_summary(
            [
                {
                    "sample_name": "manual-a",
                    "route_selection": {"primary_provider_id": "docling-local"},
                    "best_provider_id": "docling-local",
                    "providers": [
                        {
                            "provider_id": "docling-local",
                            "provider_version": "2.18.0",
                            "adapter_version": "adapter-a",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        }
                    ],
                },
                {
                    "sample_name": "manual-b",
                    "route_selection": {"primary_provider_id": "docling-local"},
                    "best_provider_id": "docling-local",
                    "providers": [
                        {
                            "provider_id": "docling-local",
                            "provider_version": "2.19.0",
                            "adapter_version": "adapter-b",
                            "status": "done",
                            "rag_coverage_quality": {"gate": "accept", "flags": []},
                        }
                    ],
                },
            ],
            gate_policy={
                "max_providers_with_multiple_provider_versions": 0,
                "max_providers_with_multiple_adapter_versions": 0,
            },
        )

        self.assertEqual(gate_summary["gate"], "fail")
        self.assertFalse(gate_summary["passed"])
        self.assertIn("provider_version_drift_budget_exceeded", gate_summary["flags"])
        self.assertIn("provider_adapter_version_drift_budget_exceeded", gate_summary["flags"])
        self.assertEqual(gate_summary["gate_policy"]["max_providers_with_multiple_provider_versions"], 0)
        self.assertEqual(gate_summary["gate_policy"]["max_providers_with_multiple_adapter_versions"], 0)

    def test_provider_admission_summary_recommends_promote_keep_and_block_actions(self) -> None:
        class ProviderSettingsStub:
            def __init__(self) -> None:
                self.local_parsers = (
                    SimpleNamespace(
                        id="pdf-text",
                        enabled=True,
                        priority=120,
                        media_types=("application/pdf",),
                        extensions=(".pdf",),
                        profiles=("default",),
                        capabilities=("native-text",),
                        route_mode="route",
                        gate_status="passed",
                        gate_checks=("samples", "license", "performance", "observability"),
                        options={},
                    ),
                    SimpleNamespace(
                        id="docling-local",
                        enabled=False,
                        priority=100,
                        media_types=("application/pdf",),
                        extensions=(".pdf",),
                        profiles=("default",),
                        capabilities=("layout", "tables"),
                        route_mode="evaluate",
                        gate_status="pending",
                        gate_checks=("samples", "license", "performance", "observability"),
                        options={},
                    ),
                    SimpleNamespace(
                        id="broken-local",
                        enabled=False,
                        priority=80,
                        media_types=("application/pdf",),
                        extensions=(".pdf",),
                        profiles=("default",),
                        capabilities=("layout",),
                        route_mode="evaluate",
                        gate_status="pending",
                        gate_checks=("samples", "license", "performance", "observability"),
                        options={},
                    ),
                )
                self.local_parser_routing = SimpleNamespace(
                    enabled=False,
                    fallback_to_default=True,
                    include_disabled=False,
                )

        settings = SimpleNamespace(providers=ProviderSettingsStub())
        sample_reports = [
            {
                "sample_name": "manual-a",
                "provider_selection_mode": "route_plan",
                "route_selection": {"primary_provider_id": "pdf-text"},
                "best_provider_id": "docling-local",
                "providers": [
                    {
                        "provider_id": "pdf-text",
                        "provider_version": "parsecore-builtin",
                        "adapter_version": "2026-06-local-provider-adapter",
                        "status": "done",
                        "rag_coverage_quality": {"gate": "accept", "flags": []},
                    },
                    {
                        "provider_id": "docling-local",
                        "provider_version": "2.19.0",
                        "adapter_version": "2026-06-local-provider-adapter",
                        "status": "done",
                        "rag_coverage_quality": {"gate": "accept", "flags": []},
                    },
                    {
                        "provider_id": "broken-local",
                        "provider_version": "0.test",
                        "adapter_version": "2026-06-local-provider-adapter",
                        "status": "failed",
                        "error": "crashed",
                    },
                ],
            }
        ]
        gate_summary = _comparison_gate_summary(sample_reports)

        summary = _provider_admission_summary(
            settings=settings,
            sample_reports=sample_reports,
            gate_summary=gate_summary,
        )

        self.assertEqual(summary["schema_version"], "2026-06-provider-admission-summary")
        self.assertEqual(summary["suite_gate"]["gate"], "fail")
        self.assertEqual(summary["summary"]["provider_count"], 3)
        self.assertEqual(summary["summary"]["route_ready_count"], 2)
        self.assertEqual(summary["summary"]["providers_requiring_config_update"], 2)
        self.assertEqual(summary["summary"]["providers_with_route_mode_drift"], 1)
        self.assertEqual(summary["summary"]["providers_with_gate_status_drift"], 2)
        self.assertEqual(summary["summary"]["providers_with_gate_checks_drift"], 0)
        self.assertEqual(summary["summary"]["providers_with_route_ready_drift"], 1)
        self.assertEqual(summary["summary"]["provider_ids_requiring_config_update"], ["broken-local", "docling-local"])
        pdf_text = summary["providers"]["pdf-text"]
        self.assertEqual(pdf_text["recommended_action"], "review_priority_order")
        self.assertEqual(pdf_text["recommended_admission"]["route_mode"], "route")
        self.assertTrue(pdf_text["recommended_admission"]["route_ready"])
        self.assertIn("route_primary_best_mismatch", pdf_text["reason_codes"])
        self.assertEqual(pdf_text["drift_fields"], [])
        docling = summary["providers"]["docling-local"]
        self.assertEqual(docling["recommended_action"], "promote_to_route_candidate")
        self.assertEqual(docling["recommended_admission"]["gate_status"], "passed")
        self.assertTrue(docling["requires_config_update"])
        self.assertIn("route_mode", docling["drift_fields"])
        self.assertIn("gate_status", docling["drift_fields"])
        self.assertEqual(docling["config_patch"][0], "[[providers.local_parsers]]")
        broken = summary["providers"]["broken-local"]
        self.assertEqual(broken["recommended_action"], "block_until_fixed")
        self.assertEqual(broken["recommended_admission"]["gate_status"], "failed")
        self.assertEqual(broken["recommended_admission"]["route_mode"], "evaluate")
        self.assertIn("provider_runs_failed", broken["reason_codes"])
        self.assertIn("gate_status", broken["drift_fields"])
        self.assertIn('gate_status = "failed"', broken["config_patch"][3])

    def test_provider_admission_summary_keeps_current_admission_for_unsupported_only_samples(self) -> None:
        class ProviderSettingsStub:
            def __init__(self) -> None:
                self.local_parsers = (
                    SimpleNamespace(
                        id="pdf-text",
                        enabled=True,
                        priority=120,
                        media_types=("application/pdf",),
                        extensions=(".pdf",),
                        profiles=("default",),
                        capabilities=("native-text",),
                        route_mode="route",
                        gate_status="passed",
                        gate_checks=("samples", "license", "performance", "observability"),
                        options={},
                    ),
                    SimpleNamespace(
                        id="docx-native",
                        enabled=True,
                        priority=100,
                        media_types=("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
                        extensions=(".docx",),
                        profiles=("default",),
                        capabilities=("native-structure",),
                        route_mode="route",
                        gate_status="passed",
                        gate_checks=("samples", "license", "performance", "observability"),
                        options={},
                    ),
                )
                self.local_parser_routing = SimpleNamespace(
                    enabled=False,
                    fallback_to_default=True,
                    include_disabled=False,
                )

        settings = SimpleNamespace(providers=ProviderSettingsStub())
        sample_reports = [
            {
                "sample_name": "manual-a",
                "provider_selection_mode": "route_plan",
                "route_selection": {"primary_provider_id": "pdf-text"},
                "best_provider_id": "pdf-text",
                "providers": [
                    {
                        "provider_id": "pdf-text",
                        "provider_version": "parsecore-builtin",
                        "adapter_version": "2026-06-local-provider-adapter",
                        "status": "done",
                        "rag_coverage_quality": {"gate": "accept", "flags": []},
                    },
                    {
                        "provider_id": "docx-native",
                        "status": "skipped",
                        "reason": "unsupported_media_type_or_extension",
                    },
                ],
            }
        ]
        gate_summary = _comparison_gate_summary(sample_reports)

        summary = _provider_admission_summary(
            settings=settings,
            sample_reports=sample_reports,
            gate_summary=gate_summary,
        )

        docx = summary["providers"]["docx-native"]
        self.assertEqual(docx["recommended_action"], "keep_current_admission")
        self.assertEqual(docx["recommended_admission"]["route_mode"], "route")
        self.assertEqual(docx["recommended_admission"]["gate_status"], "passed")
        self.assertTrue(docx["recommended_admission"]["route_ready"])
        self.assertEqual(docx["drift_fields"], [])
        self.assertFalse(docx["requires_config_update"])
        self.assertIn("no_relevant_samples", docx["reason_codes"])

    def test_build_report_can_compare_pdf_page_range_with_original_page_numbers(self) -> None:
        with TemporaryWorkspace(PDF_PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_pdf("manual.pdf", [["page 1"], ["page 2"], ["page 3"]])
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
                providers=["pdf-text"],
                page_start=2,
                page_end=3,
            )

        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["page_range"], {"start": 2, "end": 3})
        self.assertEqual(sample_report["document"], str(sample.resolve()))
        provider = sample_report["providers"][0]
        self.assertEqual(provider["status"], "done")
        provider_pages = provider["provider_report"]["pages"]
        self.assertEqual([page["page_number"] for page in provider_pages], [2, 3])
        self.assertEqual(provider["coverage_summary"]["total_pages"], 2)

        markdown = render_markdown(payload)
        self.assertIn("pages=`2-3`", markdown)

    def test_build_report_can_execute_docling_local_provider(self) -> None:
        class FakeDocumentConverter:
            def convert(self, _file_path: str, **_kwargs: object) -> object:
                page = SimpleNamespace(page_no=1, export_to_markdown=lambda: "# Manual\n\nDocling paragraph.")
                return SimpleNamespace(document=SimpleNamespace(pages=[page]))

        fake_modules = _install_fake_docling(FakeDocumentConverter)
        with TemporaryWorkspace(DOCLING_PROVIDER_COMPARE_CONFIG) as workspace, patch.dict(sys.modules, fake_modules):
            sample = workspace.create_docx("manual.docx", ["Placeholder"])
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
                providers=["docling-local"],
            )

        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["requested_provider_ids"], ["docling-local"])
        provider = sample_report["providers"][0]
        self.assertEqual(provider["provider_id"], "docling-local")
        self.assertEqual(provider["provider_version"], "2.test")
        self.assertEqual(provider["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(provider["status"], "done")
        self.assertEqual(provider["provider_report"]["schema_version"], "2026-06-provider-usage")
        self.assertEqual(provider["coverage_summary"]["total_pages"], 1)
        self.assertEqual(provider["provider_report"]["providers"][0]["provider_id"], "docling-local")
        identity_summary = payload["provider_identity_summary"]
        self.assertEqual(identity_summary["provider_count"], 1)
        self.assertEqual(identity_summary["providers"]["docling-local"]["provider_versions"], ["2.test"])
        self.assertEqual(
            identity_summary["providers"]["docling-local"]["adapter_versions"],
            ["2026-06-local-provider-adapter"],
        )

        markdown = render_markdown(payload)
        self.assertIn("## Provider Identities", markdown)
        self.assertIn("2.test", markdown)
        self.assertIn("2026-06-local-provider-adapter", markdown)

    def test_build_report_compares_configured_and_missing_providers(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
                providers=["text-native", "pymupdf4llm-local"],
            )

        self.assertEqual(payload["schema_version"], "2026-06-provider-comparison-report")
        self.assertEqual(payload["summary"]["sample_count"], 1)
        self.assertEqual(payload["summary"]["completed_provider_runs"], 1)
        self.assertEqual(payload["summary"]["skipped_provider_runs"], 1)
        self.assertEqual(payload["provider_identity_summary"]["provider_count"], 2)
        self.assertEqual(payload["provider_admission_summary"]["summary"]["provider_count"], 2)
        self.assertEqual(
            payload["provider_admission_summary"]["providers"]["text-native"]["recommended_action"],
            "keep_route",
        )
        self.assertEqual(
            payload["provider_admission_summary"]["providers"]["pymupdf4llm-local"]["recommended_action"],
            "keep_evaluate",
        )
        self.assertEqual(
            payload["provider_admission_summary"]["summary"]["provider_ids_requiring_config_update"],
            ["pymupdf4llm-local", "text-native"],
        )
        self.assertEqual(payload["gate_summary"]["gate"], "accept_with_warning")
        self.assertTrue(payload["gate_summary"]["passed"])
        self.assertIn("provider_runs_skipped", payload["gate_summary"]["warnings"])
        self.assertIn("provider_admission_config_updates", payload["gate_summary"]["warnings"])
        self.assertIn("provider_admission_gate_checks_drift", payload["gate_summary"]["warnings"])
        self.assertEqual(payload["gate_summary"]["providers_requiring_config_update"], 2)
        self.assertEqual(payload["gate_summary"]["provider_reading_order_warning_runs"], 0)
        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["best_provider_id"], "text-native")
        self.assertEqual(sample_report["provider_selection_mode"], "explicit")
        providers = {provider["provider_id"]: provider for provider in sample_report["providers"]}
        self.assertEqual(providers["text-native"]["status"], "done")
        self.assertEqual(providers["text-native"]["provider_version"], "parsecore-builtin")
        self.assertEqual(providers["text-native"]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(providers["text-native"]["provider_report"]["schema_version"], "2026-06-provider-usage")
        self.assertEqual(
            providers["text-native"]["provider_report"]["comparison_report"]["schema_version"],
            "2026-06-provider-comparison",
        )
        self.assertEqual(providers["text-native"]["coverage_summary"]["pages_with_coverage_gaps"], 0)
        self.assertEqual(providers["pymupdf4llm-local"]["status"], "skipped")
        self.assertEqual(providers["pymupdf4llm-local"]["reason"], "parser_not_configured")
        self.assertEqual(sample_report["ranking"][0]["provider_id"], "text-native")

        markdown = render_markdown(payload)
        self.assertIn("ParseCore Local Provider Comparison", markdown)
        self.assertIn("manual.txt", markdown)
        self.assertIn("text-native", markdown)
        self.assertIn("pymupdf4llm-local", markdown)
        self.assertIn("reading_order_warning_runs: 0", markdown)
        self.assertIn("gate: `accept_with_warning`", markdown)
        self.assertIn("parsecore-builtin", markdown)
        self.assertIn("## Provider Admission Recommendations", markdown)
        self.assertIn("## Provider Admission Patches", markdown)
        self.assertIn("keep_evaluate", markdown)

    def test_build_report_gate_fails_when_route_plan_has_no_primary_provider(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_pdf("manual.pdf", [["Heading", "Inspect pump."]])
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
            )

        self.assertEqual(payload["summary"]["completed_provider_runs"], 0)
        self.assertEqual(payload["gate_summary"]["gate"], "fail")
        self.assertFalse(payload["gate_summary"]["passed"])
        self.assertIn("route_primary_missing", payload["gate_summary"]["flags"])
        self.assertIn("sample_without_completed_provider", payload["gate_summary"]["flags"])
        self.assertEqual(payload["gate_summary"]["samples_without_route_primary"], 1)
        self.assertEqual(payload["gate_summary"]["samples_without_completed_provider"], 1)

    def test_build_report_uses_route_plan_priority_for_automatic_provider_order(self) -> None:
        config = PROVIDER_COMPARE_CONFIG.replace(
            '[[providers.local_parsers]]\nid = "pymupdf4llm-local"',
            """
[[providers.local_parsers]]
id = "text-alt"
enabled = true
priority = 140
media_types = ["text/plain"]
extensions = [".txt", ".md"]
profiles = ["default"]
capabilities = ["native-text", "rag-baseline"]

[[providers.local_parsers]]
id = "pymupdf4llm-local"
""",
            1,
        ).replace(
            '[[parsers]]\nname = "text-native"',
            """
[[parsers]]
name = "text-alt"
media_types = ["text/plain"]
extensions = [".txt", ".md"]

[[parsers]]
name = "text-native"
""",
            1,
        )
        with TemporaryWorkspace(config) as workspace:
            sample = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
            )

        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["routing_policy"], "priority_desc_then_id")
        self.assertEqual(sample_report["route_selection"]["primary_provider_id"], "text-alt")
        self.assertEqual(sample_report["route_selection"]["fallback_provider_ids"], ["text-native"])
        self.assertEqual(
            sample_report["requested_provider_ids"],
            ["text-alt", "text-native", "pymupdf4llm-local"],
        )
        self.assertEqual([provider["provider_id"] for provider in sample_report["providers"]], sample_report["requested_provider_ids"])

        markdown = render_markdown(payload)
        self.assertIn("primary=`text-alt`", markdown)
        self.assertIn("policy=`priority_desc_then_id`", markdown)

    def test_build_report_auto_route_plan_skips_disabled_candidates_without_gate_warning(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
            )

        self.assertEqual(payload["summary"]["completed_provider_runs"], 1)
        self.assertEqual(payload["summary"]["skipped_provider_runs"], 1)
        self.assertEqual(payload["gate_summary"]["gate"], "accept_with_warning")
        self.assertIn("provider_admission_config_updates", payload["gate_summary"]["warnings"])
        self.assertEqual(payload["gate_summary"]["providers_requiring_config_update"], 2)
        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["provider_selection_mode"], "route_plan")
        providers = {provider["provider_id"]: provider for provider in sample_report["providers"]}
        self.assertEqual(providers["text-native"]["status"], "done")
        self.assertEqual(providers["pymupdf4llm-local"]["status"], "skipped")
        self.assertEqual(providers["pymupdf4llm-local"]["reason"], "parser_not_configured")

    def test_build_report_route_plan_fallback_when_primary_unsupported(self) -> None:
        """P3-T09: verify fallback provider is selected when primary does not support the sample media type."""
        with TemporaryWorkspace(PDF_PROVIDER_COMPARE_CONFIG) as workspace:
            text_sample = workspace.create_text_file("notes.txt", "Summary\n\nReview action items.")
            pdf_sample = workspace.create_pdf("manual.pdf", [["Heading", "Inspect pump."]])
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[text_sample, pdf_sample],
            )

        # text_sample: text-native supports .txt (priority 100), pdf-text does not support .txt
        text_report = payload["samples"][0]
        text_selection = text_report["route_selection"]
        self.assertEqual(text_selection["primary_provider_id"], "text-native")
        text_providers = {p["provider_id"]: p for p in text_report["providers"]}
        self.assertEqual(text_providers["text-native"]["status"], "done")
        self.assertEqual(text_providers["pdf-text"]["status"], "skipped")
        self.assertEqual(text_providers["pdf-text"]["reason"], "unsupported_media_type_or_extension")

        # pdf_sample: pdf-text is primary (priority 120, supports PDF), text-native is fallback
        pdf_report = payload["samples"][1]
        pdf_selection = pdf_report["route_selection"]
        self.assertEqual(pdf_selection["primary_provider_id"], "pdf-text")
        pdf_providers = {p["provider_id"]: p for p in pdf_report["providers"]}
        self.assertEqual(pdf_providers["pdf-text"]["status"], "done")
        self.assertEqual(pdf_providers["text-native"]["status"], "skipped")
        self.assertEqual(pdf_providers["text-native"]["reason"], "unsupported_media_type_or_extension")

    def test_build_report_fallback_provider_captured_in_route_selection_and_markdown(self) -> None:
        """P3-T09: verify fallback providers are listed in route_selection and rendered in markdown."""
        config = PROVIDER_COMPARE_CONFIG.replace(
            '[[providers.local_parsers]]\nid = "pymupdf4llm-local"',
            """
[[providers.local_parsers]]
id = "text-alt"
enabled = true
priority = 140
media_types = ["text/plain"]
extensions = [".txt", ".md"]
profiles = ["default"]
capabilities = ["native-text", "rag-baseline"]

[[providers.local_parsers]]
id = "pymupdf4llm-local"
""",
            1,
        ).replace(
            '[[parsers]]\nname = "text-native"',
            """
[[parsers]]
name = "text-alt"
media_types = ["text/plain"]
extensions = [".txt", ".md"]

[[parsers]]
name = "text-native"
""",
            1,
        )
        with TemporaryWorkspace(config) as workspace:
            sample = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None

            payload = build_report(
                config=workspace.config_path,
                samples=[sample],
            )

        sample_report = payload["samples"][0]
        selection = sample_report["route_selection"]
        # text-alt (priority 140) is primary, text-native (priority 100) is fallback
        self.assertEqual(selection["primary_provider_id"], "text-alt")
        self.assertEqual(selection["fallback_provider_ids"], ["text-native"])

        # Both providers should complete since both support text/plain
        providers = {p["provider_id"]: p for p in sample_report["providers"]}
        self.assertEqual(providers["text-alt"]["status"], "done")
        self.assertEqual(providers["text-native"]["status"], "done")

        # Markdown should render fallback info
        markdown = render_markdown(payload)
        self.assertIn("fallback=`text-native`", markdown)
        self.assertIn("primary=`text-alt`", markdown)

    def test_build_report_suite_skips_disabled_entries(self) -> None:
        """P3-T07: verify suite loader skips entries with disabled=true."""
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("fixtures/manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None
            assert workspace.root is not None
            suite = workspace.root / "provider-suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "samples": [
                            {
                                "name": "active-sample",
                                "fixture_relative_path": "fixtures/manual.txt",
                                "providers": ["text-native"],
                            },
                            {
                                "name": "disabled-future-sample",
                                "disabled": True,
                                "fixture_relative_path": "fixtures/manual.txt",
                                "providers": ["text-native"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
            )

        # Only the active sample should be processed
        self.assertEqual(payload["summary"]["sample_count"], 1)
        self.assertEqual(payload["samples"][0]["sample_name"], "active-sample")

    def test_build_report_gate_fails_when_admission_update_budget_is_exceeded(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("fixtures/manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None
            assert workspace.root is not None
            suite = workspace.root / "provider-suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "gate_policy": {
                            "max_providers_requiring_config_update": 0,
                            "max_providers_with_gate_checks_drift": 0,
                        },
                        "samples": [
                            {
                                "name": "manual-suite",
                                "fixture_relative_path": "fixtures/manual.txt",
                                "providers": ["text-native", "pymupdf4llm-local"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
            )

        self.assertEqual(payload["gate_summary"]["gate"], "fail")
        self.assertFalse(payload["gate_summary"]["passed"])
        self.assertIn("provider_admission_config_update_budget_exceeded", payload["gate_summary"]["flags"])
        self.assertIn("provider_admission_gate_checks_drift_budget_exceeded", payload["gate_summary"]["flags"])
        self.assertEqual(payload["gate_summary"]["providers_requiring_config_update"], 2)
        self.assertEqual(payload["gate_summary"]["providers_with_gate_checks_drift"], 2)

    def test_build_report_reads_suite_samples_with_relative_fixture_root(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("fixtures/manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None
            assert workspace.root is not None
            suite = workspace.root / "provider-suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "samples": [
                            {
                                "name": "manual-suite",
                                "fixture_relative_path": "fixtures/manual.txt",
                                "providers": ["text-native", "pymupdf4llm-local"],
                                "profile": "default",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
                providers=["pymupdf4llm-local"],
                profile="fallback",
            )

        self.assertEqual(payload["summary"]["sample_count"], 1)
        self.assertEqual(payload["suite"], str(suite.resolve()))
        self.assertEqual(payload["fixture_root"], str(workspace.root))
        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["sample_name"], "manual-suite")
        self.assertEqual(sample_report["source"], "suite:provider-suite.json")
        self.assertEqual(sample_report["document"], str(sample.resolve()))
        self.assertEqual(sample_report["profile"], "default")
        self.assertEqual(sample_report["requested_provider_ids"], ["text-native", "pymupdf4llm-local"])

    def test_build_report_exposes_suite_gate_policy(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("fixtures/manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None
            assert workspace.root is not None
            suite = workspace.root / "provider-suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "gate_policy": {
                            "max_provider_reading_order_warning_runs": 0,
                            "max_provider_quality_warning_runs": 0,
                            "max_samples_best_provider_differs_from_route_primary": 0,
                            "max_providers_with_multiple_provider_versions": 0,
                            "max_providers_with_multiple_adapter_versions": 0,
                        },
                        "samples": [
                            {
                                "name": "manual-suite",
                                "fixture_relative_path": "fixtures/manual.txt",
                                "providers": ["text-native"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
            )

        self.assertEqual(
            payload["gate_policy"],
            {
                "max_provider_reading_order_warning_runs": 0,
                "max_provider_quality_warning_runs": 0,
                "max_samples_best_provider_differs_from_route_primary": 0,
                "max_providers_with_multiple_provider_versions": 0,
                "max_providers_with_multiple_adapter_versions": 0,
            },
        )
        self.assertEqual(
            payload["gate_summary"]["gate_policy"]["max_provider_reading_order_warning_runs"],
            0,
        )
        self.assertEqual(
            payload["gate_summary"]["gate_policy"]["max_provider_quality_warning_runs"],
            0,
        )
        self.assertEqual(
            payload["gate_summary"]["gate_policy"]["max_samples_best_provider_differs_from_route_primary"],
            0,
        )
        self.assertEqual(
            payload["gate_summary"]["gate_policy"]["max_providers_with_multiple_provider_versions"],
            0,
        )
        self.assertEqual(
            payload["gate_summary"]["gate_policy"]["max_providers_with_multiple_adapter_versions"],
            0,
        )
        markdown = render_markdown(payload)
        self.assertIn('"max_provider_reading_order_warning_runs": 0', markdown)
        self.assertIn('"max_provider_quality_warning_runs": 0', markdown)
        self.assertIn('"max_samples_best_provider_differs_from_route_primary": 0', markdown)
        self.assertIn('"max_providers_with_multiple_provider_versions": 0', markdown)
        self.assertIn('"max_providers_with_multiple_adapter_versions": 0', markdown)
        self.assertIn("provider_quality_warning_runs: 0", markdown)
        self.assertIn("best_provider_route_mismatches: 0", markdown)

    def test_main_progress_writes_to_stderr_without_breaking_json_output(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("manual.txt", "hello provider comparison")
            out_json = workspace.root / "provider-report.json"
            stderr = io.StringIO()
            stdout = io.StringIO()

            with redirect_stderr(stderr), redirect_stdout(stdout):
                exit_code = provider_comparison_main(
                    [
                        "--config",
                        str(workspace.config_path),
                        "--sample",
                        str(sample),
                        "--provider",
                        "text-native",
                        "--out-json",
                        str(out_json),
                        "--progress",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("[provider-comparison-report] starting samples=1", stderr.getvalue())
            self.assertIn("sample 1/1", stderr.getvalue())
            self.assertIn("[provider-comparison-report] wrote", stdout.getvalue())
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["sample_count"], 1)

    def test_build_report_reuses_regression_baseline_suite_entries(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("fixtures/manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None
            assert workspace.root is not None
            baseline = workspace.root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "fixture_root_env": "PARSECORE_TEST_PROVIDER_FIXTURE_ROOT",
                        "fixtures": [
                            {
                                "fixture": r"D:\legacy\uploads\manual.txt",
                                "fixture_name": "manual.txt",
                                "fixture_relative_path": "fixtures/manual.txt",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            suite = workspace.root / "suite.fast.json"
            suite.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "name": "primary-default",
                                "baseline": "baseline.json",
                                "providers": ["text-native"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
            )

        self.assertEqual(payload["summary"]["sample_count"], 1)
        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["source"], "baseline:baseline.json")
        self.assertEqual(sample_report["document"], str(sample.resolve()))
        self.assertEqual(sample_report["requested_provider_ids"], ["text-native"])
        self.assertEqual(sample_report["providers"][0]["status"], "done")

    def test_build_report_applies_global_page_range_to_suite_samples(self) -> None:
        with TemporaryWorkspace(PDF_PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_pdf("fixtures/manual.pdf", [["page 1"], ["page 2"], ["page 3"]])
            assert workspace.config_path is not None
            assert workspace.root is not None
            suite = workspace.root / "provider-suite.json"
            suite.write_text(
                json.dumps(
                    {
                        "samples": [
                            {
                                "name": "manual-suite",
                                "fixture_relative_path": "fixtures/manual.pdf",
                                "providers": ["pdf-text"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = build_report(
                config=workspace.config_path,
                suite=suite,
                fixture_root=workspace.root,
                page_start=2,
                page_end=2,
            )

        sample_report = payload["samples"][0]
        self.assertEqual(sample_report["page_range"], {"start": 2, "end": 2})
        self.assertEqual(sample_report["document"], str(sample.resolve()))
        provider_pages = sample_report["providers"][0]["provider_report"]["pages"]
        self.assertEqual([page["page_number"] for page in provider_pages], [2])

    def test_build_report_rejects_page_range_for_non_pdf_samples(self) -> None:
        with TemporaryWorkspace(PROVIDER_COMPARE_CONFIG) as workspace:
            sample = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            assert workspace.config_path is not None

            with self.assertRaisesRegex(ValueError, "page_range_requires_pdf"):
                build_report(
                    config=workspace.config_path,
                    samples=[sample],
                    page_start=1,
                    page_end=1,
                )


if __name__ == "__main__":
    unittest.main()
