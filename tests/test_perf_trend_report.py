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

    def _baseline_payload(
        self,
        *,
        version: str = "baseline-1",
        lane: str = "clean_latency",
        track_python_memory: bool = False,
        cache_mode: str = "ocr_warm",
        p50_elapsed_s: float = 10.0,
        peak_rss_mib: float = 800.0,
        telemetry_status: str = "available",
    ) -> dict:
        def telemetry(peak_mib: float) -> dict:
            if telemetry_status != "available":
                return {"status": telemetry_status}
            peak_rss_bytes = int(peak_mib * 1024 * 1024)
            return {
                "status": "available",
                "collector": "psutil",
                "scope": "runtime.submit_end_to_end",
                "sample_interval_ms": 100,
                "working_set_semantics": "Windows working set (rss)",
                "peak": {
                    "rss_bytes": peak_rss_bytes,
                    "working_set_bytes": peak_rss_bytes,
                    "vms_bytes": peak_rss_bytes + 64 * 1024 * 1024,
                },
                "delta": {
                    "cpu_total_s": 10.0,
                    "io_read_bytes": 32 * 1024 * 1024,
                    "io_write_bytes": 16 * 1024 * 1024,
                },
            }

        return {
            "schema_version": "2026-07-parse-perf-stability",
            "version": version,
            "status": "ok",
            "generated_at": version,
            "measurement": {
                "elapsed_scope": "runtime.submit_end_to_end",
                "track_python_memory": track_python_memory,
                "lane": lane,
                "cache": {"mode": cache_mode},
                "runtime_lifecycle": {"reuse_runtime": True, "warmup_runs": 1},
                "process_telemetry": {"enabled": True, "sample_interval_ms": 100},
            },
            "summary": {
                "documents": 2,
                "p50_elapsed_s": p50_elapsed_s,
                "p95_elapsed_s": p50_elapsed_s + 1.0,
                "max_elapsed_s": p50_elapsed_s + 2.0,
                "max_peak_kb": 1024.0 if track_python_memory else None,
            },
            "results": [
                {
                    "status": "done",
                    "file_name": "heavy.pdf",
                    "size_bytes": 2048,
                    "process_telemetry": telemetry(peak_rss_mib - 50.0),
                },
                {
                    "status": "done",
                    "file_name": "heavy.pdf",
                    "size_bytes": 2048,
                    "process_telemetry": telemetry(peak_rss_mib),
                },
            ],
            "stability": {
                "gates": [],
                "stage_timings_s": {
                    "parse": {"count": 2, "p50": 8.0, "p95": 9.0, "max": 9.5, "cv_pct": 4.0},
                    "chunk": {"count": 2, "p50": 1.0, "p95": 1.1, "max": 1.2, "cv_pct": 5.0},
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

    def test_build_summary_aggregates_baseline_process_telemetry(self) -> None:
        summary = perf_trend_report.build_summary(
            self._baseline_payload(peak_rss_mib=900.0)
        )

        self.assertEqual(summary["report_kind"], "parse_perf_baseline")
        self.assertEqual(summary["overview"]["p50_elapsed_s"], 10.0)
        telemetry = summary["process_telemetry"]
        self.assertEqual(telemetry["status"], "available")
        self.assertEqual(telemetry["available_runs"], 2)
        self.assertEqual(telemetry["peak_rss_bytes"]["max"], 900 * 1024 * 1024)
        self.assertEqual(telemetry["cpu_total_s"]["sum"], 20)

    def test_baseline_trend_keeps_incompatible_memory_lanes_separate(self) -> None:
        clean = self._baseline_payload(version="clean", track_python_memory=False)
        tracked = self._baseline_payload(
            version="tracked",
            lane="python_allocation_tracked",
            track_python_memory=True,
        )

        trend = perf_trend_report.build_trend_summary([clean, tracked])

        self.assertFalse(trend["available"])
        self.assertEqual(trend["reason"], "incompatible_measurement_channels")
        self.assertEqual(len(trend["versions"]), 2)
        self.assertFalse(trend["process_telemetry"]["available"])
        self.assertEqual(
            trend["process_telemetry"]["reason"],
            "incompatible_process_telemetry_channels",
        )

    def test_process_telemetry_trend_is_observation_only(self) -> None:
        first = self._baseline_payload(version="v1", peak_rss_mib=800.0)
        last = self._baseline_payload(
            version="v2", p50_elapsed_s=9.0, peak_rss_mib=1000.0
        )

        trend = perf_trend_report.build_process_telemetry_trend([first, last])

        self.assertTrue(trend["available"])
        self.assertTrue(trend["observation_only"])
        self.assertEqual(trend["peak_rss_direction"], "increased")
        self.assertEqual(trend["peak_rss_bytes_max_change_pct"], 25.0)

    def test_process_telemetry_trend_handles_missing_telemetry(self) -> None:
        first = self._baseline_payload(version="v1")
        missing = self._baseline_payload(version="v2", telemetry_status="unavailable")

        trend = perf_trend_report.build_process_telemetry_trend([first, missing])

        self.assertFalse(trend["available"])
        self.assertEqual(trend["reason"], "process_telemetry_unavailable")

    def test_stage_timing_trend_compares_only_identical_channels(self) -> None:
        first = self._baseline_payload(version="v1")
        last = self._baseline_payload(version="v2", p50_elapsed_s=9.0)
        last["stability"]["stage_timings_s"]["parse"]["p50"] = 6.0

        trend = perf_trend_report.build_stage_timing_trend([first, last])

        self.assertTrue(trend["available"])
        self.assertTrue(trend["observation_only"])
        self.assertEqual(trend["common_stage_count"], 2)
        self.assertEqual(trend["stages"]["parse"]["change_pct"], -25.0)
        self.assertEqual(trend["stages"]["parse"]["direction"], "improving")

    def test_stage_timing_trend_rejects_incompatible_channels(self) -> None:
        clean = self._baseline_payload(version="clean")
        tracked = self._baseline_payload(
            version="tracked",
            lane="python_allocation_tracked",
            track_python_memory=True,
        )

        trend = perf_trend_report.build_stage_timing_trend([clean, tracked])

        self.assertFalse(trend["available"])
        self.assertEqual(trend["reason"], "incompatible_measurement_channels")

    def test_stage_timing_trend_reports_missing_stages(self) -> None:
        first = self._baseline_payload(version="v1")
        last = self._baseline_payload(version="v2")
        last["stability"]["stage_timings_s"].pop("chunk")

        trend = perf_trend_report.build_stage_timing_trend([first, last])

        self.assertTrue(trend["available"])
        self.assertFalse(trend["stage_set_consistent"])
        self.assertEqual(trend["missing_stages_by_version"], {"v2": ["chunk"]})

    def test_baseline_markdown_marks_incompatible_channels_as_observations(self) -> None:
        clean = self._baseline_payload(version="clean", track_python_memory=False)
        tracked = self._baseline_payload(
            version="tracked",
            lane="python_allocation_tracked",
            track_python_memory=True,
        )

        markdown = perf_trend_report.render_markdown(
            clean, trend_reports=[clean, tracked]
        )

        self.assertIn("# ParseCore Parse Performance Trend", markdown)
        self.assertIn("## Process Telemetry (Observation Only)", markdown)
        self.assertIn("### Stage Timing Trend", markdown)
        self.assertIn("incompatible_measurement_channels", markdown)
        self.assertIn("does not create an alert", markdown)

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
