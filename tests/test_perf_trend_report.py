from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import perf_trend_report


class PerfTrendReportTests(unittest.TestCase):
    def _sample_payload(self) -> dict:
        return {
            "status": "ok",
            "profile": "perf",
            "suite": "var/regression/suite.perf.json",
            "generated_at": "2026-04-30T12:00:00+0800",
            "regression_timeout_seconds": 4200,
            "checks": [
                {
                    "name": "unit_tests",
                    "status": "passed",
                    "elapsed_s": 10.2,
                    "summary": "202 tests passed, skipped=5",
                    "details": {},
                    "tail": [],
                },
                {
                    "name": "regression_suite",
                    "status": "passed",
                    "elapsed_s": 44.2,
                    "summary": "profile=perf ok=2 skipped=0",
                    "details": {
                        "structure_metrics": {
                            "toc_recog": 1.0,
                            "chapter_cov": 0.8,
                            "noise_ratio": 0.05,
                        }
                    },
                    "tail": [],
                },
            ],
            "perf_tracking": {
                "overview": {
                    "sample_count": 1,
                    "slowest_sample": {"name": "sample-a.pdf", "elapsed_s": 44.2},
                    "highest_ocr_total_sample": {
                        "name": "sample-a.pdf",
                        "ocr_total_s": 40.0,
                    },
                },
                "samples": [
                    {
                        "name": "sample-a.pdf",
                        "elapsed_s": 44.2,
                        "metrics": {},
                        "ocr_metrics": {
                            "ocr_total_s": {"current": 40.0},
                            "call_s": {"current": 38.5},
                            "provider_s": {"current": 37.0},
                            "rec_s": {"current": 31.0},
                            "max_page_ocr_s": {"current": 5.0},
                        },
                    }
                ],
                "comparison": {
                    "available": True,
                    "compare_report": "previous.json",
                    "samples": [
                        {
                            "name": "sample-a.pdf",
                            "metrics": {
                                "elapsed_s": {"delta": 3.2},
                                "ocr_total_s": {"delta": 5.0},
                                "call_s": {"delta": 1.0},
                            },
                        }
                    ],
                },
            },
        }

    def test_build_summary_preserves_perf_tracking(self) -> None:
        summary = perf_trend_report.build_summary(self._sample_payload())

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["overview"]["sample_count"], 1)
        self.assertEqual(summary["samples"][0]["name"], "sample-a.pdf")
        self.assertTrue(summary["comparison"]["available"])

    def test_render_markdown_includes_samples_deltas_and_checks(self) -> None:
        markdown = perf_trend_report.render_markdown(self._sample_payload())

        self.assertIn("# ParseCore Perf Gate", markdown)
        self.assertIn("| sample-a.pdf | 44.2 | 40.000 | 38.500 | 37.000 | 31.000 | 5.000 |", markdown)
        self.assertIn("| sample-a.pdf | 3.2 | 5.000 | 1.000 |", markdown)
        self.assertIn("| unit_tests | passed | 10.2 | 202 tests passed, skipped=5 |", markdown)
        self.assertIn("| toc_recog | 1.0000 |", markdown)

    def test_perf_columns_extended_with_three_new_metrics(self) -> None:
        self.assertEqual(len(perf_trend_report.PERF_COLUMNS), 9)
        self.assertIn("peak_memory_mb", perf_trend_report.PERF_COLUMNS)
        self.assertIn("throughput_mb_s", perf_trend_report.PERF_COLUMNS)
        self.assertIn("part_throughput_s", perf_trend_report.PERF_COLUMNS)

    def test_build_trend_summary_with_3_versions(self) -> None:
        reports = [
            {"status": "ok", "generated_at": "v1", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 10.0}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v2", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 8.5}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v3", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 7.0}], "comparison": {},
            }, "checks": []},
        ]
        trend = perf_trend_report.build_trend_summary(reports)
        self.assertTrue(trend["available"])
        self.assertEqual(trend["version_count"], 3)
        self.assertEqual(trend["trend_direction"], "improving")

    def test_build_trend_summary_with_1_report_returns_unavailable(self) -> None:
        trend = perf_trend_report.build_trend_summary([{"status": "ok"}])
        self.assertFalse(trend["available"])
        self.assertEqual(trend["reason"], "need_at_least_2_reports")

    def test_build_trend_summary_detects_regression(self) -> None:
        reports = [
            {"status": "ok", "generated_at": "v1", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 10.0}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v2", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 12.0}], "comparison": {},
            }, "checks": []},
        ]
        trend = perf_trend_report.build_trend_summary(reports)
        self.assertTrue(trend["available"])
        self.assertEqual(trend["trend_direction"], "regressing")
        self.assertIsNotNone(trend["elapsed_s_p50_change_pct"])
        self.assertGreater(trend["elapsed_s_p50_change_pct"], 0)

    def test_build_trend_summary_detects_improvement(self) -> None:
        reports = [
            {"status": "ok", "generated_at": "v1", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 10.0}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v2", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 8.0}], "comparison": {},
            }, "checks": []},
        ]
        trend = perf_trend_report.build_trend_summary(reports)
        self.assertTrue(trend["available"])
        self.assertEqual(trend["trend_direction"], "improving")
        self.assertLess(trend["elapsed_s_p50_change_pct"], 0)

    def test_render_markdown_includes_trend_section_when_3plus_reports(self) -> None:
        reports = [
            {"status": "ok", "generated_at": "v1", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 10.0}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v2", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 8.5}], "comparison": {},
            }, "checks": []},
            {"status": "ok", "generated_at": "v3", "perf_tracking": {
                "overview": {}, "samples": [{"name": "a", "elapsed_s": 7.0}], "comparison": {},
            }, "checks": []},
        ]
        markdown = perf_trend_report.render_markdown(reports[0], trend_reports=reports)
        self.assertIn("## Multi-Version Trend", markdown)
        self.assertIn("v1", markdown)
        self.assertIn("v2", markdown)
        self.assertIn("v3", markdown)
        self.assertIn("improving", markdown)

    def test_cli_writes_markdown_and_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            out_md = root / "report.md"
            out_json = root / "summary.json"
            report.write_text(json.dumps(self._sample_payload()), encoding="utf-8")

            exit_code = perf_trend_report.main(
                [str(report), "--out-md", str(out_md), "--out-json", str(out_json)]
            )

            self.assertEqual(exit_code, 0)
            self.assertIn("ParseCore Perf Gate", out_md.read_text(encoding="utf-8"))
            summary = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(summary["profile"], "perf")


if __name__ == "__main__":
    unittest.main()
