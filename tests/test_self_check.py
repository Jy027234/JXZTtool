from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

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

    def test_default_out_for_profile_uses_separate_full_snapshot(self) -> None:
        self.assertTrue(self_check._default_out_for_profile(self_check.FAST_PROFILE).endswith("var\\self-check\\latest.json"))
        self.assertTrue(self_check._default_out_for_profile(self_check.FULL_PROFILE).endswith("var\\self-check\\latest.full.json"))
        self.assertTrue(self_check._default_out_for_profile(self_check.PERF_PROFILE).endswith("var\\self-check\\latest.perf.json"))

    def test_default_suite_for_profile_uses_explicit_full_and_perf_suites(self) -> None:
        self.assertTrue(self_check._default_suite_for_profile(self_check.FAST_PROFILE).endswith("var\\regression\\suite.fast.json"))
        self.assertTrue(self_check._default_suite_for_profile(self_check.FULL_PROFILE).endswith("var\\regression\\suite.full.json"))
        self.assertTrue(self_check._default_suite_for_profile(self_check.PERF_PROFILE).endswith("var\\regression\\suite.perf.json"))

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

    def test_cli_self_check_delegates_to_default_gate(self) -> None:
        with patch("tools.self_check.main", return_value=0) as run_self_check:
            exit_code = cli_main(["self-check", "--profile", "perf", "--skip-regression"])

        self.assertEqual(exit_code, 0)
        run_self_check.assert_called_once_with(["--profile", "perf", "--skip-regression"])

    def test_cli_large_pdf_stress_delegates_to_tool(self) -> None:
        with patch("tools.large_pdf_stress.main", return_value=0) as run_stress:
            exit_code = cli_main(["large-pdf-stress", "--generate-pages", "3"])

        self.assertEqual(exit_code, 0)
        run_stress.assert_called_once_with(["--generate-pages", "3"])


if __name__ == "__main__":
    unittest.main()
