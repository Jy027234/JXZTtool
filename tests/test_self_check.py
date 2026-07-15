from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from parsecore.cli import main as cli_main
from tools import self_check


class SelfCheckTests(unittest.TestCase):
    def test_build_parser_defaults_to_fast_profile(self) -> None:
        args = self_check._build_parser().parse_args([])

        self.assertEqual(args.profile, self_check.FAST_PROFILE)
        self.assertIsNone(args.suite)
        self.assertIsNone(args.out)
        self.assertIsNone(args.compare_report)
        self.assertIsNone(args.regression_timeout_seconds)
        self.assertIsNone(args.provider_suite)
        self.assertIsNone(args.provider_fixture_root)
        self.assertEqual(args.provider_profile, "default")
        self.assertFalse(args.skip_payload_contracts)
        self.assertFalse(args.skip_provider_comparison)
        self.assertIsNone(args.provider_comparison_timeout_seconds)
        self.assertFalse(args.reuse_parser_instances)
        self.assertIsNone(args.large_pdf_benchmark)

    def test_default_out_for_profile_uses_separate_full_snapshot(self) -> None:
        self.assertTrue(self_check._default_out_for_profile(self_check.FAST_PROFILE).endswith("var\\self-check\\latest.json"))
        self.assertTrue(self_check._default_out_for_profile(self_check.FULL_PROFILE).endswith("var\\self-check\\latest.full.json"))
        self.assertTrue(self_check._default_out_for_profile(self_check.PERF_PROFILE).endswith("var\\self-check\\latest.perf.json"))

    def test_default_suite_for_profile_uses_explicit_full_and_perf_suites(self) -> None:
        self.assertTrue(self_check._default_suite_for_profile(self_check.FAST_PROFILE).endswith("var\\regression\\suite.fast.json"))
        self.assertTrue(self_check._default_suite_for_profile(self_check.FULL_PROFILE).endswith("var\\regression\\suite.full.json"))
        self.assertTrue(self_check._default_suite_for_profile(self_check.PERF_PROFILE).endswith("var\\regression\\suite.perf.json"))

    def test_default_provider_suite_for_profile_supports_fast_full_and_perf_lanes(self) -> None:
        self.assertTrue(
            self_check._default_provider_suite_for_profile(self_check.FAST_PROFILE).endswith(
                "var\\regression\\provider-suite.fast.json"
            )
        )
        self.assertTrue(
            self_check._default_provider_suite_for_profile(self_check.FULL_PROFILE).endswith(
                "var\\regression\\provider-suite.full.json"
            )
        )
        self.assertTrue(
            self_check._default_provider_suite_for_profile(self_check.PERF_PROFILE).endswith(
                "var\\regression\\provider-suite.perf.json"
            )
        )

    def test_resolve_regression_profile_maps_slow_alias_to_full(self) -> None:
        profile, include_tags, timeout_seconds = self_check._resolve_regression_profile("slow", None)

        self.assertEqual(profile, self_check.FULL_PROFILE)
        self.assertEqual(include_tags, ())
        self.assertEqual(timeout_seconds, self_check.PROFILE_TIMEOUTS[self_check.FULL_PROFILE])

    def test_resolve_regression_profile_supports_perf_lane(self) -> None:
        profile, include_tags, timeout_seconds = self_check._resolve_regression_profile(self_check.PERF_PROFILE, None)

        self.assertEqual(profile, self_check.PERF_PROFILE)
        self.assertEqual(include_tags, ())
        self.assertEqual(timeout_seconds, self_check.PROFILE_TIMEOUTS[self_check.PERF_PROFILE])

    def test_build_regression_suite_args_appends_include_tag_flags(self) -> None:
        args = self_check._build_regression_suite_args("suite.json", ("slow",))

        self.assertEqual(
            args,
            [
                sys.executable,
                "tools/regression_baseline.py",
                "check-suite",
                "--suite",
                "suite.json",
                "--include-tag",
                "slow",
            ],
        )

    def test_build_provider_comparison_args_passes_suite_fixture_root_and_profile(self) -> None:
        args = self_check._build_provider_comparison_args(
            config="parsecore.toml",
            suite="provider-suite.json",
            fixture_root="fixtures",
            profile="fallback",
        )

        self.assertEqual(
            args,
            [
                sys.executable,
                "tools/provider_comparison_report.py",
                "--config",
                "parsecore.toml",
                "--suite",
                "provider-suite.json",
                "--profile",
                "fallback",
                "--progress",
                "--fixture-root",
                "fixtures",
            ],
        )

    def test_build_provider_comparison_args_can_enable_candidate_parser_reuse(self) -> None:
        args = self_check._build_provider_comparison_args(
            config="parsecore.toml",
            suite="provider-suite.json",
            profile="perf",
            reuse_parser_instances=True,
        )

        self.assertEqual(args[-1], "--reuse-parser-instances")

    def test_fast_provider_suite_preflight_records_declared_page_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = root / "large.pdf"
            fixture.write_bytes(b"%PDF-1.4")
            suite = root / "provider-suite.fast.json"
            suite.write_text(
                json.dumps(
                    {
                        "fast_page_budget": {
                            "max_pages_per_sample": 3,
                            "max_total_pages": 3,
                            "large_pdf_min_page_count": 32,
                        },
                        "entries": [
                            {
                                "name": "representative-window",
                                "path": str(fixture),
                                "page_range": {"start": 10, "end": 12},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("tools.self_check.detect_pdf_page_count", return_value=297) as page_count:
                allowed, details = self_check._default_provider_suite_preflight(
                    str(suite),
                    fixture_root=None,
                    profile=self_check.FAST_PROFILE,
                )

        self.assertTrue(allowed)
        page_count.assert_called_once_with(str(fixture))
        self.assertEqual(details["page_budget"]["selected_pdf_pages"], 3)
        self.assertEqual(details["page_budget"]["max_total_pages"], 3)
        self.assertEqual(details["page_budget"]["samples"][0]["page_range"], {"start": 10, "end": 12})

    def test_fast_provider_suite_preflight_rejects_large_pdf_without_page_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = root / "large.pdf"
            fixture.write_bytes(b"%PDF-1.4")
            suite = root / "provider-suite.fast.json"
            suite.write_text(
                json.dumps(
                    {
                        "fast_page_budget": {
                            "max_pages_per_sample": 3,
                            "max_total_pages": 3,
                            "large_pdf_min_page_count": 32,
                        },
                        "entries": [{"path": str(fixture)}],
                    }
                ),
                encoding="utf-8",
            )
            with patch("tools.self_check.detect_pdf_page_count", return_value=297):
                allowed, details = self_check._default_provider_suite_preflight(
                    str(suite),
                    fixture_root=None,
                    profile=self_check.FAST_PROFILE,
                )

        self.assertFalse(allowed)
        self.assertEqual(details["reason"], "fast_page_budget_violation")
        self.assertEqual(details["violations"][0]["code"], "large_pdf_requires_page_range")
        self.assertIn("sample_page_budget_exceeded", [item["code"] for item in details["violations"]])

    def test_fast_provider_suite_preflight_rejects_total_page_budget_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.pdf"
            second = root / "second.pdf"
            first.write_bytes(b"%PDF-1.4")
            second.write_bytes(b"%PDF-1.4")
            suite = root / "provider-suite.fast.json"
            suite.write_text(
                json.dumps(
                    {
                        "fast_page_budget": {
                            "max_pages_per_sample": 3,
                            "max_total_pages": 5,
                            "large_pdf_min_page_count": 32,
                        },
                        "entries": [
                            {"path": str(first), "page_range": {"start": 1, "end": 3}},
                            {"path": str(second), "page_range": {"start": 1, "end": 3}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("tools.self_check.detect_pdf_page_count", return_value=297):
                allowed, details = self_check._default_provider_suite_preflight(
                    str(suite),
                    fixture_root=None,
                    profile=self_check.FAST_PROFILE,
                )

        self.assertFalse(allowed)
        self.assertEqual(details["page_budget"]["selected_pdf_pages"], 6)
        self.assertEqual(details["violations"][-1]["code"], "total_page_budget_exceeded")

    def test_main_rejects_explicit_fast_provider_suite_that_fails_page_preflight(self) -> None:
        passed_unit_tests = self_check.CheckResult(
            name="unit_tests",
            status="passed",
            exit_code=0,
            elapsed_s=0.1,
            summary="unit tests passed",
            details={},
            tail=[],
        )
        passed_runtime = self_check.CheckResult(
            name="runtime_describe",
            status="passed",
            exit_code=0,
            elapsed_s=0.1,
            summary="runtime describe passed",
            details={},
            tail=[],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "self-check.json"
            with patch(
                "tools.self_check._run_unit_tests",
                return_value=passed_unit_tests,
            ), patch(
                "tools.self_check._run_runtime_describe",
                return_value=passed_runtime,
            ), patch(
                "tools.self_check._default_provider_suite_preflight",
                return_value=(
                    False,
                    {
                        "reason": "fast_page_budget_violation",
                        "message": "fast provider suite page budget rejected",
                    },
                ),
            ), patch("tools.self_check._run_provider_comparison_suite") as run_provider_suite:
                exit_code = self_check.main(
                    [
                        "--profile",
                        "fast",
                        "--provider-suite",
                        "invalid-fast-suite.json",
                        "--skip-regression",
                        "--skip-payload-contracts",
                        "--out",
                        str(out_path),
                    ]
                )
            payload = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        run_provider_suite.assert_not_called()
        provider_check = next(item for item in payload["checks"] if item["name"] == "provider_comparison_suite")
        self.assertEqual(provider_check["status"], "failed")
        self.assertEqual(provider_check["details"]["reason"], "fast_page_budget_violation")

    @patch("tools.self_check.subprocess.run")
    @patch("tools.self_check.subprocess.Popen")
    def test_run_subprocess_timeout_uses_taskkill_tree_on_windows(self, popen: MagicMock, run: MagicMock) -> None:
        process = MagicMock()
        process.pid = 4242
        process.returncode = -9
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["child"], 1, output=b"partial output", stderr=b"partial error"),
            ("final output", "final error"),
        ]
        popen.return_value = process
        run.return_value = subprocess.CompletedProcess(
            ["taskkill.exe", "/PID", "4242", "/T", "/F"],
            0,
            stdout="SUCCESS: tree terminated",
            stderr="",
        )

        with patch.object(self_check.os, "name", "nt"):
            result, output = self_check._run_subprocess("provider_comparison_suite", ["child"], timeout_seconds=1)

        self.assertEqual(result.status, "timeout")
        self.assertIn("final output", output)
        self.assertEqual(result.details["timeout_cleanup"]["strategy"], "taskkill_tree")
        self.assertTrue(result.details["timeout_cleanup"]["succeeded"])
        self.assertEqual(result.details["timeout_output_drain"]["drain_status"], "completed")
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            int(getattr(self_check.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "4242", "/T", "/F"])
        process.kill.assert_not_called()

    @patch("tools.self_check.subprocess.run")
    @patch("tools.self_check.subprocess.Popen")
    def test_run_subprocess_timeout_falls_back_to_root_kill_when_taskkill_fails(
        self,
        popen: MagicMock,
        run: MagicMock,
    ) -> None:
        process = MagicMock()
        process.pid = 4242
        process.returncode = -9
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["child"], 1),
            ("", ""),
        ]
        popen.return_value = process
        run.return_value = subprocess.CompletedProcess(
            ["taskkill.exe", "/PID", "4242", "/T", "/F"],
            1,
            stdout="",
            stderr="ERROR: process not found",
        )

        with patch.object(self_check.os, "name", "nt"):
            result, _output = self_check._run_subprocess("provider_comparison_suite", ["child"], timeout_seconds=1)

        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.details["timeout_cleanup"]["succeeded"])
        self.assertEqual(result.details["timeout_cleanup"]["fallback_root_kill"], "sent")
        process.kill.assert_called_once()

    def test_extract_structure_metrics_from_regression_output(self) -> None:
        lines = [
            "[check] sample-a toc_recog=1.0000 (baseline 1.0000) chapter_cov=0.8000 (baseline 0.8000) noise_ratio=0.0500 (baseline 0.0500) heading_bind=1.0000 (baseline 1.0000) evidence_bind=0.9000 (baseline 0.9000)",
            "[check] sample-b toc_recog=0.5000 (baseline 0.5000) chapter_cov=0.6000 (baseline 0.6000) noise_ratio=0.1000 (baseline 0.1000) heading_bind=0.7000 (baseline 0.7000) evidence_bind=0.8000 (baseline 0.8000)",
        ]

        metrics = self_check._extract_structure_metrics(lines)

        self.assertEqual(metrics["toc_recog"], 0.75)
        self.assertEqual(metrics["chapter_cov"], 0.7)
        self.assertEqual(metrics["noise_ratio"], 0.075)
        self.assertEqual(metrics["heading_bind"], 0.85)
        self.assertEqual(metrics["evidence_bind"], 0.85)

    def test_extract_perf_samples_from_regression_output(self) -> None:
        lines = [
            "[run] flight ops manual.pdf: parsing finished in 44.2s",
            "[check] flight ops manual.pdf blocks=123 (baseline 120) pages=10 (baseline 10) very_short_ratio=0.0100 (baseline 0.0080)",
            "[check][ocr] flight ops manual.pdf layout_s=50.000 (baseline 49.000) ocr_total_s=40.000 (baseline 39.000) call_s=38.500 (baseline 37.500) provider_s=37.000 (baseline 36.000) rec_s=31.000 (baseline 30.000) max_page_ocr_s=5.000 (baseline 4.500)",
        ]

        samples = self_check._extract_perf_samples(lines)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["name"], "flight ops manual.pdf")
        self.assertEqual(samples[0]["elapsed_s"], 44.2)
        self.assertEqual(samples[0]["metrics"]["blocks"]["current"], 123)
        self.assertEqual(samples[0]["metrics"]["blocks"]["delta"], 3)
        self.assertEqual(samples[0]["ocr_metrics"]["ocr_total_s"]["current"], 40.0)
        self.assertEqual(samples[0]["ocr_metrics"]["max_page_ocr_s"]["delta"], 0.5)

    def test_compare_perf_samples_uses_previous_report(self) -> None:
        current_samples = [
            {
                "name": "sample-a.pdf",
                "elapsed_s": 44.2,
                "metrics": {},
                "ocr_metrics": {
                    "ocr_total_s": {"current": 40.0, "baseline": 39.0, "delta": 1.0},
                    "rec_s": {"current": 31.0, "baseline": 30.0, "delta": 1.0},
                },
            }
        ]
        previous_payload = {
            "perf_tracking": {
                "samples": [
                    {
                        "name": "sample-a.pdf",
                        "elapsed_s": 41.0,
                        "metrics": {},
                        "ocr_metrics": {
                            "ocr_total_s": {"current": 35.0, "baseline": 34.0, "delta": 1.0},
                            "rec_s": {"current": 29.0, "baseline": 28.0, "delta": 1.0},
                        },
                    }
                ]
            }
        }

        comparison = self_check._compare_perf_samples(
            current_samples,
            previous_payload,
            compare_report="prev.json",
        )

        self.assertEqual(comparison["compare_report"], "prev.json")
        self.assertTrue(comparison["available"])
        self.assertEqual(comparison["sample_count"], 1)
        self.assertEqual(comparison["samples"][0]["metrics"]["elapsed_s"]["delta"], 3.2)
        self.assertEqual(comparison["samples"][0]["metrics"]["ocr_total_s"]["delta"], 5.0)

    def test_run_regression_suite_reports_profile_and_include_tags(self) -> None:
        with patch(
            "tools.self_check._run_subprocess",
            return_value=(
                self_check.CheckResult(
                    name="regression_suite",
                    status="passed",
                    exit_code=0,
                    elapsed_s=1.23,
                    summary="command completed",
                    details={},
                    tail=[],
                ),
                "[suite] primary-default\n[check] OK: all metrics within budget\n[suite] OK: all baselines passed",
            ),
        ) as run_subprocess:
            result = self_check._run_regression_suite(
                "suite.json",
                120,
                include_tags=(),
                profile=self_check.FULL_PROFILE,
            )

        self.assertEqual(
            run_subprocess.call_args.args[1],
            [
                sys.executable,
                "tools/regression_baseline.py",
                "check-suite",
                "--suite",
                "suite.json",
            ],
        )
        self.assertEqual(result.details["profile"], self_check.FULL_PROFILE)
        self.assertEqual(result.details["include_tags"], [])
        self.assertIn("profile=full", result.summary)

    def test_run_regression_suite_extracts_perf_samples(self) -> None:
        output = "\n".join(
            [
                "[run] sample-a.pdf: parsing finished in 44.2s",
                "[check] sample-a.pdf blocks=123 (baseline 120) pages=10 (baseline 10)",
                "[check][ocr] sample-a.pdf ocr_total_s=40.000 (baseline 39.000) call_s=38.500 (baseline 37.500) provider_s=37.000 (baseline 36.000) rec_s=31.000 (baseline 30.000) max_page_ocr_s=5.000 (baseline 4.500)",
                "[check] OK: all metrics within budget",
                "[suite] OK: all baselines passed",
            ]
        )
        with patch(
            "tools.self_check._run_subprocess",
            return_value=(
                self_check.CheckResult(
                    name="regression_suite",
                    status="passed",
                    exit_code=0,
                    elapsed_s=1.23,
                    summary="command completed",
                    details={},
                    tail=[],
                ),
                output,
            ),
        ):
            result = self_check._run_regression_suite(
                "suite.perf.json",
                120,
                include_tags=(),
                profile=self_check.PERF_PROFILE,
            )

        self.assertEqual(result.details["perf_samples"][0]["name"], "sample-a.pdf")
        self.assertEqual(result.details["perf_overview"]["slowest_sample"]["name"], "sample-a.pdf")
        self.assertIn("slowest=sample-a.pdf:44.2s", result.summary)

    def test_run_provider_comparison_suite_extracts_gate_summary(self) -> None:
        provider_payload = {
            "schema_version": "2026-06-provider-comparison-report",
            "suite": "resolved-provider-suite.json",
            "fixture_root": "resolved-fixtures",
            "measurement": {"parser_lifecycle": "provider_instance_reused"},
            "summary": {
                "sample_count": 2,
                "completed_provider_runs": 3,
                "failed_provider_runs": 0,
                "skipped_provider_runs": 1,
            },
            "gate_summary": {
                "gate": "accept_with_warning",
                "passed": True,
                "warnings": ["provider_runs_skipped"],
                "provider_quality_warning_runs": 3,
                "provider_reading_order_warning_runs": 2,
                "samples_best_provider_differs_from_route_primary": 1,
            },
            "provider_identity_summary": {
                "provider_count": 2,
                "providers_with_multiple_provider_versions": 1,
                "providers_with_multiple_adapter_versions": 0,
                "providers": {
                    "pdf-text": {
                        "provider_versions": ["parsecore-builtin"],
                        "adapter_versions": ["2026-06-local-provider-adapter"],
                    }
                },
            },
            "provider_admission_summary": {
                "schema_version": "2026-06-provider-admission-summary",
                "summary": {
                    "provider_count": 2,
                    "route_ready_count": 1,
                    "providers_requiring_config_update": 1,
                },
                "providers": {
                    "pdf-text": {
                        "recommended_action": "keep_route",
                        "recommended_admission": {
                            "route_mode": "route",
                            "gate_status": "passed",
                            "route_ready": True,
                        },
                    }
                },
            },
        }
        with patch(
            "tools.self_check._run_subprocess",
            return_value=(
                self_check.CheckResult(
                    name="provider_comparison_suite",
                    status="passed",
                    exit_code=0,
                    elapsed_s=1.23,
                    summary="command completed",
                    details={},
                    tail=[],
                ),
                json.dumps(provider_payload),
            ),
        ) as run_subprocess:
            result = self_check._run_provider_comparison_suite(
                config="parsecore.toml",
                suite="provider-suite.json",
                fixture_root="fixtures",
                profile="fallback",
                timeout_seconds=120,
                reuse_parser_instances=True,
            )

        self.assertEqual(
            run_subprocess.call_args.args[1],
            [
                sys.executable,
                "tools/provider_comparison_report.py",
                "--config",
                "parsecore.toml",
                "--suite",
                "provider-suite.json",
                "--profile",
                "fallback",
                "--progress",
                "--fixture-root",
                "fixtures",
                "--reuse-parser-instances",
            ],
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.details["summary"]["sample_count"], 2)
        self.assertEqual(result.details["gate_summary"]["gate"], "accept_with_warning")
        self.assertEqual(result.details["provider_identity_summary"]["provider_count"], 2)
        self.assertEqual(result.details["provider_admission_summary"]["summary"]["route_ready_count"], 1)
        self.assertTrue(result.details["reuse_parser_instances"])
        self.assertEqual(result.details["resolved_suite"], "resolved-provider-suite.json")
        self.assertIn("gate=accept_with_warning", result.summary)
        self.assertIn("quality_warn=3", result.summary)
        self.assertIn("read_order_warn=2", result.summary)
        self.assertIn("route_mismatch=1", result.summary)
        self.assertIn("skipped=1", result.summary)
        self.assertIn("identity_drift=1", result.summary)
        self.assertIn("admission_ready=1", result.summary)
        self.assertIn("admission_update=1", result.summary)
        self.assertIn("parser_lifecycle=provider_instance_reused", result.summary)

    def test_run_payload_contract_check_extracts_summary(self) -> None:
        contract_payload = {
            "schema_version": "2026-06-payload-contract-check",
            "registry_schema_version": "2026-06-payload-schema-registry",
            "status": "passed",
            "summary": {
                "schema_count": 6,
                "payload_count": 6,
                "failed_schema_count": 0,
                "failed_payload_count": 0,
            },
            "schemas": [{"name": "document-ir", "status": "passed"}],
            "payloads": [{"name": "document-ir", "status": "passed"}],
        }
        with patch(
            "tools.self_check._run_subprocess",
            return_value=(
                self_check.CheckResult(
                    name="payload_contracts",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.42,
                    summary="command completed",
                    details={},
                    tail=[],
                ),
                json.dumps(contract_payload),
            ),
        ) as run_subprocess:
            result = self_check._run_payload_contract_check()

        self.assertEqual(
            run_subprocess.call_args.args[1],
            [sys.executable, "-m", "parsecore.cli", "payload-contract-check"],
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.details["status"], "passed")
        self.assertEqual(result.details["registry_schema_version"], "2026-06-payload-schema-registry")
        self.assertEqual(result.details["summary"]["schema_count"], 6)
        self.assertIn("schemas=6", result.summary)
        self.assertIn("payloads=6", result.summary)

    def test_provider_comparison_artifact_paths_follow_profile_name(self) -> None:
        json_path, markdown_path = self_check._provider_comparison_artifact_paths(
            profile=self_check.FULL_PROFILE,
            out_path=r"D:\tmp\latest.full.json",
        )

        self.assertTrue(str(json_path).endswith(r"provider-comparison.full.json"))
        self.assertTrue(str(markdown_path).endswith(r"provider-comparison.full.md"))

    def test_main_perf_profile_runs_default_provider_suite_when_fixtures_are_available(self) -> None:
        with patch(
            "tools.self_check._run_unit_tests",
            return_value=self_check.CheckResult(
                name="unit_tests",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="unit tests passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_runtime_describe",
            return_value=self_check.CheckResult(
                name="runtime_describe",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="runtime describe passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_payload_contract_check",
            return_value=self_check.CheckResult(
                name="payload_contracts",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="payload contracts passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_regression_suite",
            return_value=self_check.CheckResult(
                name="regression_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="regression passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._default_provider_suite_preflight",
            return_value=(True, {"sample_count": 2}),
        ), patch(
            "tools.self_check._run_provider_comparison_suite",
            return_value=self_check.CheckResult(
                name="provider_comparison_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="provider comparison passed",
                details={},
                tail=[],
            ),
        ) as run_provider_suite, patch(
            "pathlib.Path.write_text",
            return_value=0,
        ):
            exit_code = self_check.main(["--profile", "perf"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_provider_suite.call_args.kwargs["suite"],
            self_check._default_provider_suite_for_profile(self_check.PERF_PROFILE),
        )

    def test_main_fast_profile_runs_default_provider_suite_when_fixtures_are_available(self) -> None:
        with patch(
            "tools.self_check._run_unit_tests",
            return_value=self_check.CheckResult(
                name="unit_tests",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="unit tests passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_runtime_describe",
            return_value=self_check.CheckResult(
                name="runtime_describe",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="runtime describe passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_payload_contract_check",
            return_value=self_check.CheckResult(
                name="payload_contracts",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="payload contracts passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_regression_suite",
            return_value=self_check.CheckResult(
                name="regression_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="regression passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._default_provider_suite_preflight",
            return_value=(True, {"sample_count": 3}),
        ), patch(
            "tools.self_check._run_provider_comparison_suite",
            return_value=self_check.CheckResult(
                name="provider_comparison_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="provider comparison passed",
                details={},
                tail=[],
            ),
        ) as run_provider_suite, patch(
            "pathlib.Path.write_text",
            return_value=0,
        ):
            exit_code = self_check.main(["--profile", "fast"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_provider_suite.call_args.kwargs["suite"],
            self_check._default_provider_suite_for_profile(self_check.FAST_PROFILE),
        )

    def test_main_full_profile_runs_default_provider_suite_when_fixtures_are_available(self) -> None:
        with patch(
            "tools.self_check._run_unit_tests",
            return_value=self_check.CheckResult(
                name="unit_tests",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="unit tests passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_runtime_describe",
            return_value=self_check.CheckResult(
                name="runtime_describe",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="runtime describe passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_payload_contract_check",
            return_value=self_check.CheckResult(
                name="payload_contracts",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="payload contracts passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_regression_suite",
            return_value=self_check.CheckResult(
                name="regression_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="regression passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._default_provider_suite_preflight",
            return_value=(True, {"sample_count": 5}),
        ), patch(
            "tools.self_check._run_provider_comparison_suite",
            return_value=self_check.CheckResult(
                name="provider_comparison_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="provider comparison passed",
                details={},
                tail=[],
            ),
        ) as run_provider_suite, patch(
            "pathlib.Path.write_text",
            return_value=0,
        ):
            exit_code = self_check.main(["--profile", "full"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            run_provider_suite.call_args.kwargs["suite"],
            self_check._default_provider_suite_for_profile(self_check.FULL_PROFILE),
        )

    def test_main_writes_provider_comparison_artifacts_when_report_payload_is_available(self) -> None:
        provider_payload = {
            "schema_version": "2026-06-provider-comparison-report",
            "profile": "default",
            "suite": "resolved-provider-suite.full.json",
            "gate_policy": {"max_samples_best_provider_differs_from_route_primary": 0},
            "summary": {
                "sample_count": 1,
                "completed_provider_runs": 1,
                "failed_provider_runs": 0,
                "skipped_provider_runs": 0,
            },
            "gate_summary": {
                "gate": "accept",
                "passed": True,
                "provider_quality_warning_runs": 0,
                "provider_reading_order_warning_runs": 0,
                "samples_best_provider_differs_from_route_primary": 0,
            },
            "samples": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = f"{temp_dir}\\latest.full.json"
            with patch(
                "tools.self_check._run_unit_tests",
                return_value=self_check.CheckResult(
                    name="unit_tests",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.1,
                    summary="unit tests passed",
                    details={},
                    tail=[],
                ),
            ), patch(
                "tools.self_check._run_runtime_describe",
                return_value=self_check.CheckResult(
                    name="runtime_describe",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.1,
                    summary="runtime describe passed",
                    details={},
                    tail=[],
                ),
            ), patch(
                "tools.self_check._run_payload_contract_check",
                return_value=self_check.CheckResult(
                    name="payload_contracts",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.1,
                    summary="payload contracts passed",
                    details={},
                    tail=[],
                ),
            ), patch(
                "tools.self_check._run_regression_suite",
                return_value=self_check.CheckResult(
                    name="regression_suite",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.1,
                    summary="regression passed",
                    details={},
                    tail=[],
                ),
            ), patch(
                "tools.self_check._default_provider_suite_preflight",
                return_value=(True, {"sample_count": 5}),
            ), patch(
                "tools.self_check._run_provider_comparison_suite",
                return_value=self_check.CheckResult(
                    name="provider_comparison_suite",
                    status="passed",
                    exit_code=0,
                    elapsed_s=0.1,
                    summary="provider comparison passed",
                    details={
                        "report_payload": provider_payload,
                        "summary": provider_payload["summary"],
                        "gate_summary": provider_payload["gate_summary"],
                    },
                    tail=[],
                ),
            ), patch(
                "tools.self_check._write_stdout",
                return_value=None,
            ):
                exit_code = self_check.main(["--profile", "full", "--out", out_path])

            self.assertEqual(exit_code, 0)
            provider_json = f"{temp_dir}\\provider-comparison.full.json"
            provider_md = f"{temp_dir}\\provider-comparison.full.md"
            self.assertTrue(Path(provider_json).exists())
            self.assertTrue(Path(provider_md).exists())
            report_payload = json.loads(Path(provider_json).read_text(encoding="utf-8"))
            self.assertEqual(report_payload["schema_version"], "2026-06-provider-comparison-report")
            self.assertIn("ParseCore Local Provider Comparison", Path(provider_md).read_text(encoding="utf-8"))

            self_check_payload = json.loads(Path(out_path).read_text(encoding="utf-8"))
            self.assertEqual(
                self_check_payload["provider_comparison_artifacts"],
                {
                    "json": provider_json,
                    "markdown": provider_md,
                },
            )
            provider_check = next(
                item for item in self_check_payload["checks"] if item["name"] == "provider_comparison_suite"
            )
            self.assertEqual(provider_check["details"]["report_json"], provider_json)
            self.assertEqual(provider_check["details"]["report_markdown"], provider_md)
            self.assertNotIn("report_payload", provider_check["details"])

    def test_main_perf_profile_skips_default_provider_suite_when_fixtures_are_missing(self) -> None:
        with patch(
            "tools.self_check._run_unit_tests",
            return_value=self_check.CheckResult(
                name="unit_tests",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="unit tests passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_runtime_describe",
            return_value=self_check.CheckResult(
                name="runtime_describe",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="runtime describe passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_payload_contract_check",
            return_value=self_check.CheckResult(
                name="payload_contracts",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="payload contracts passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._run_regression_suite",
            return_value=self_check.CheckResult(
                name="regression_suite",
                status="passed",
                exit_code=0,
                elapsed_s=0.1,
                summary="regression passed",
                details={},
                tail=[],
            ),
        ), patch(
            "tools.self_check._default_provider_suite_preflight",
            return_value=(
                False,
                {
                    "reason": "missing_fixtures",
                    "message": "provider suite skipped because 2 fixture(s) are unavailable",
                    "missing_fixtures": ["missing-a.pdf", "missing-b.pdf"],
                },
            ),
        ), patch(
            "tools.self_check._run_provider_comparison_suite",
        ) as run_provider_suite, patch(
            "pathlib.Path.write_text",
            return_value=0,
        ):
            exit_code = self_check.main(["--profile", "perf"])

        self.assertEqual(exit_code, 0)
        run_provider_suite.assert_not_called()

    def test_cli_self_check_delegates_to_default_gate(self) -> None:
        with patch("tools.self_check.main", return_value=0) as run_self_check:
            exit_code = cli_main(["self-check", "--profile", "perf", "--skip-regression"])

        self.assertEqual(exit_code, 0)
        run_self_check.assert_called_once_with(["--profile", "perf", "--skip-regression"])

    def test_cli_payload_contract_check_delegates_to_tool(self) -> None:
        with patch("tools.payload_contract_check.main", return_value=0) as run_contract_check:
            exit_code = cli_main(["payload-contract-check", "--out", "var/self-check/contracts.json"])

        self.assertEqual(exit_code, 0)
        run_contract_check.assert_called_once_with(["--out", "var/self-check/contracts.json"])

    def test_cli_large_pdf_stress_delegates_to_tool(self) -> None:
        with patch("tools.large_pdf_stress.main", return_value=0) as run_stress:
            exit_code = cli_main(["large-pdf-stress", "--generate-pages", "3"])

        self.assertEqual(exit_code, 0)
        run_stress.assert_called_once_with(["--generate-pages", "3"])


if __name__ == "__main__":
    unittest.main()
