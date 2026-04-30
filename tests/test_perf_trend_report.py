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
        self.assertIn("| sample-a.pdf | 3.2 | 5.000 | 1.000 |  |  |  |", markdown)
        self.assertIn("| unit_tests | passed | 10.2 | 202 tests passed, skipped=5 |", markdown)
        self.assertIn("| toc_recog | 1.0000 |", markdown)

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
