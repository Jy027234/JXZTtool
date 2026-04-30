from __future__ import annotations

import unittest

from tools import gray_baseline_snapshot


class GrayBaselineSnapshotTests(unittest.TestCase):
    def test_endpoint_builds_query_and_skips_empty_values(self) -> None:
        url = gray_baseline_snapshot._endpoint(
            "http://127.0.0.1:8090/",
            "/v1/parse/metrics",
            {"tenant_id": "tenant-a", "since_hours": 24, "unused": None},
        )

        self.assertEqual(
            url,
            "http://127.0.0.1:8090/v1/parse/metrics?tenant_id=tenant-a&since_hours=24",
        )

    def test_build_snapshot_extracts_runtime_and_latency_baseline(self) -> None:
        payload = gray_baseline_snapshot.build_snapshot(
            base_url="http://localhost:8090",
            tenant_id="tenant-a",
            since_hours=24.0,
            sample_size=200,
            runtime={
                "project": "parsecore-starter",
                "mode": "embedded-sdk",
                "index_mode": "pgvector",
                "runtime": {
                    "execution_mode": "queue-worker",
                    "max_workers": 2,
                    "max_upload_bytes": 52428800,
                    "max_inflight_jobs": 8,
                    "api_auth_enabled": True,
                },
                "parsers": ["pdf-text", "docx-native"],
            },
            metrics={
                "total_jobs": 20,
                "done_jobs": 19,
                "failed_jobs": 1,
                "active_jobs": 0,
                "failure_rate": 0.05,
                "durations_s": {
                    "count": 20,
                    "mean": 2.5,
                    "p50": 1.2,
                    "p90": 4.5,
                    "p99": 6.7,
                    "max": 7.0,
                },
            },
            index_metrics={"high_precision": {"document_coverage": 0.9}},
            events={"events": [{"event_type": "ocr_failed"}], "counters": {"tenant-a:ocr_failed": 1}},
        )

        self.assertEqual(payload["runtime"]["index_mode"], "pgvector")
        self.assertEqual(payload["runtime"]["api_auth_enabled"], True)
        self.assertEqual(payload["metrics"]["failure_rate"], 0.05)
        self.assertEqual(payload["metrics"]["durations_s"]["p99"], 6.7)
        self.assertEqual(payload["observability"]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
