from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.optimization_result_audit import build_report, render_markdown


class OptimizationResultAuditTests(unittest.TestCase):
    def _write(self, root: Path, name: str, payload: dict) -> Path:
        path = root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _parse_report(self, sample: Path, *, elapsed: float, peak_kb: float | None) -> dict:
        return {
            "status": "ok",
            "config": str(sample.parent / "parsecore.toml"),
            "results": [
                {
                    "document": str(sample),
                    "file_name": sample.name,
                    "status": "done",
                    "elapsed_s": elapsed,
                    "peak_kb": peak_kb,
                    "blocks": 12,
                    "chunks": 10,
                    "tables": 2,
                    "primary_provider_id": "pdf-text",
                    "best_provider_id": "pdf-text",
                    "provider_report": {
                        "summary": {"total_figures": 3, "total_pages": 4},
                        "quality_gate": {
                            "gate": "accept_with_warning",
                            "passed": True,
                            "flags": ["rag_chunks_not_embedded"],
                        },
                    },
                }
            ],
        }

    def _regression_report(self, sample: Path, *, raw_blocks: int, pages: int) -> dict:
        return {
            "fixtures": [
                {
                    "fixture": str(sample),
                    "elapsed_s": 10.0,
                    "block_counts": {"total": raw_blocks, "chunks": 10},
                    "quality": {
                        "total_blocks": 10,
                        "page_count": pages,
                        "very_short_ratio": 0.1,
                    },
                    "table_quality": {"table_block_count": 2},
                    "structure_quality": {
                        "noise_ratio": 0.0,
                        "counts": {
                            "quality_denominator_items": 10,
                            "audit_artifact_items": raw_blocks - 10,
                        },
                    },
                }
            ]
        }

    def test_build_report_passes_gates_and_discloses_tracked_tail(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "sample.pdf"
            sample.write_bytes(b"sample")
            (root / "parsecore.toml").write_text("[project]\nname='audit'\n", encoding="utf-8")
            original = self._write(root, "original.json", self._parse_report(sample, elapsed=20, peak_kb=1000))
            prior = self._write(
                root,
                "prior.json",
                {
                    "summary": {
                        "elapsed_median_s": 18,
                        "quality_counts": {"chunks": 10, "tables": 2, "figures": 3},
                    }
                },
            )
            tracked = [
                self._write(root, f"tracked-{index}.json", self._parse_report(sample, elapsed=value, peak_kb=900))
                for index, value in enumerate((10, 10, 15), start=1)
            ]
            historical_regression = self._write(
                root,
                "historical-regression.json",
                self._regression_report(sample, raw_blocks=10, pages=3),
            )
            historical_latency_payload = self._regression_report(sample, raw_blocks=10, pages=3)
            historical_latency_payload["fixtures"][0]["elapsed_s"] = 14
            historical_latency = self._write(root, "historical-latency.json", historical_latency_payload)
            latency = [
                self._write(root, f"latency-{index}.json", self._parse_report(sample, elapsed=value, peak_kb=None))
                for index, value in enumerate((10, 10.1, 10.2), start=1)
            ]
            current_regression = self._write(
                root,
                "current-regression.json",
                self._regression_report(sample, raw_blocks=12, pages=4),
            )
            self_check = self._write(
                root,
                "self-check.json",
                {"status": "ok", "checks": [{"name": "tests", "status": "passed", "summary": "ok"}]},
            )
            p1 = self._write(
                root,
                "p1.json",
                {
                    "status": "passed",
                    "summary": {
                        "check_count": 8,
                        "passed_check_count": 8,
                        "failed_check_count": 0,
                        "payload_count": 24,
                    },
                },
            )

            payload = build_report(
                sample=sample,
                original_tracked=original,
                prior_tracked_stability=prior,
                current_tracked=tracked,
                historical_latency=historical_latency,
                current_latency=latency,
                historical_regression=historical_regression,
                current_regression=current_regression,
                self_check=self_check,
                p1_acceptance=p1,
            )

        self.assertEqual(payload["status"], "passed_with_observation")
        self.assertEqual(payload["summary"]["failed_gate_count"], 0)
        self.assertEqual(payload["performance"]["tracked_lane"]["elapsed_improvement_vs_original_pct"], 50.0)
        self.assertEqual(payload["performance"]["clean_latency_lane"]["elapsed_improvement_pct"], 27.857)
        self.assertTrue(payload["reliability"]["structural_determinism"])
        self.assertEqual(payload["reliability"]["current_quality"]["audit_artifact_items"], 2)
        codes = {item["code"] for item in payload["observations"]}
        self.assertIn("tracked_memory_instrumentation_tail_outlier", codes)
        self.assertIn("tracked_memory_elapsed_non_sla", codes)
        self.assertIn("physical_page_audit_artifacts", codes)
        self.assertNotIn("tracked_elapsed_vs_original", {item["name"] for item in payload["gates"]})
        markdown = render_markdown(payload)
        self.assertIn("产品优化结果审计", markdown)
        self.assertIn("clean_latency_stability", markdown)

    def test_build_report_flattens_stability_artifacts_and_enforces_policy(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "sample.pdf"
            sample.write_bytes(b"sample")
            (root / "parsecore.toml").write_text("[project]\nname='audit'\n", encoding="utf-8")
            original = self._write(root, "original.json", self._parse_report(sample, elapsed=20, peak_kb=1000))
            prior = self._write(
                root,
                "prior.json",
                {"summary": {"elapsed_median_s": 18, "quality_counts": {"chunks": 10, "tables": 2, "figures": 3}}},
            )

            def stable_result(elapsed: float, peak_kb: float | None) -> dict:
                result = self._parse_report(sample, elapsed=elapsed, peak_kb=peak_kb)["results"][0]
                result.update(
                    {
                        "raw_blocks": 12,
                        "content_blocks": 10,
                        "figures": 3,
                        "pages": 4,
                        "fingerprint": {
                            "raw_blocks": 12,
                            "content_blocks": 10,
                            "chunks": 10,
                            "tables": 2,
                            "figures": 3,
                            "pages": 4,
                        },
                        "parser_lifecycle": {"mode": "reused_runtime", "phase": "warm"},
                        "cache_state": {
                            "requested_mode": "ocr_warm",
                            "parse_cache": {"observed_states": ["disabled"], "observed_hit_blocks": 0},
                            "ocr_cache": {"observed_cache_hit_blocks": 1},
                        },
                        "stage_timings": {"parse": 1.0, "chunk": 0.1},
                        "process_telemetry": {
                            "status": "available",
                            "peak": {"rss_bytes": 100, "working_set_bytes": 100},
                            "delta": {"cpu_total_s": 1.0, "io_read_bytes": 10, "io_write_bytes": 1},
                        },
                    }
                )
                return result

            tracked = self._write(
                root,
                "tracked-stability.json",
                {"status": "ok", "config": str(root / "parsecore.toml"), "results": [stable_result(10, 900) for _ in range(3)]},
            )
            latency = self._write(
                root,
                "latency-stability.json",
                {"status": "ok", "config": str(root / "parsecore.toml"), "results": [stable_result(value, None) for value in (10, 10.1, 10.2, 10.1, 10)]},
            )
            historical_regression = self._write(root, "historical-regression.json", self._regression_report(sample, raw_blocks=10, pages=3))
            historical_latency_payload = self._regression_report(sample, raw_blocks=10, pages=3)
            historical_latency_payload["fixtures"][0]["elapsed_s"] = 14
            historical_latency = self._write(root, "historical-latency.json", historical_latency_payload)
            current_regression = self._write(root, "current-regression.json", self._regression_report(sample, raw_blocks=12, pages=4))
            self_check = self._write(root, "self-check.json", {"status": "ok", "checks": [{"name": "tests", "status": "passed", "summary": "ok"}]})
            p1 = self._write(root, "p1.json", {"status": "passed", "summary": {"failed_check_count": 0, "passed_check_count": 8, "check_count": 8, "payload_count": 24}})
            policy = self._write(
                root,
                "policy.json",
                {
                    "fingerprint": {"raw_blocks": 12, "content_blocks": 10, "chunks": 10, "tables": 2, "figures": 3, "pages": 4},
                    "measurement": {
                        "cache_mode": "ocr_warm",
                        "require_parse_cache_bypass": True,
                        "require_ocr_cache_hit": True,
                    },
                    "latency": {"min_runs": 5, "min_success_rate_pct": 100, "require_uniform_lifecycle": True, "max_p50_s": 24.5, "max_cv_pct": 5},
                    "tracked_memory": {"min_runs": 3, "min_success_rate_pct": 100, "require_uniform_lifecycle": True, "max_mean_peak_kb": 750000},
                },
            )

            payload = build_report(
                sample=sample,
                original_tracked=original,
                prior_tracked_stability=prior,
                current_tracked=[tracked],
                historical_latency=historical_latency,
                current_latency=[latency],
                historical_regression=historical_regression,
                current_regression=current_regression,
                self_check=self_check,
                p1_acceptance=p1,
                stability_policy=policy,
            )

        self.assertEqual(payload["summary"]["failed_gate_count"], 0)
        self.assertEqual(payload["reliability"]["tracked_run_count"], 3)
        self.assertEqual(payload["reliability"]["clean_latency_run_count"], 5)
        policy_gate_names = {gate["name"] for gate in payload["gates"] if gate["name"].startswith("stability_policy_")}
        self.assertIn("stability_policy_latency_p50", policy_gate_names)
        self.assertIn("stability_policy_measurement_parse_cache_bypass", policy_gate_names)
        self.assertIn("stability_policy_measurement_ocr_cache_warm", policy_gate_names)
        self.assertIn("stability_policy_tracked_memory_mean_peak_kb", policy_gate_names)
        self.assertEqual(payload["reliability"]["telemetry"]["clean_latency_lane"]["process"]["available_runs"], 5)


if __name__ == "__main__":
    unittest.main()
