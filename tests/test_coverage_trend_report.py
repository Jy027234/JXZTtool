"""Tests for P6-T04/T05: coverage / reader metric trend report."""

from __future__ import annotations

import unittest

from tools.coverage_trend_report import (
    COVERAGE_METRICS,
    READER_METRICS,
    build_trend_report,
    render_markdown,
)


def _make_report(
    *,
    version: str = "v1",
    samples: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "2026-06-self-check",
        "version": version,
        "generated_at": "2026-06-25T10:00:00Z",
        "samples": samples or [],
    }


def _sample_with_coverage(
    *,
    text_cov: float = 0.99,
    table_cov: float = 0.97,
    chunk_cov: float = 0.99,
    visible: int = 50,
    hidden: int = 5,
    table_blocks: int = 3,
    reading_order_warning: int = 0,
    reading_order_confidence: float = 0.95,
) -> dict:
    return {
        "coverage": {
            "text_page_coverage_ratio": text_cov,
            "table_unit_coverage_ratio": table_cov,
            "unit_chunk_coverage_ratio": chunk_cov,
        },
        "reader": {
            "visible_block_count": visible,
            "hidden_block_count": hidden,
            "table_block_count": table_blocks,
            "reading_order_warning_count": reading_order_warning,
            "reading_order_confidence_avg": reading_order_confidence,
        },
    }


class CoverageTrendReportTests(unittest.TestCase):
    """P6-T04/T05: coverage / reader metric trend gate."""

    def test_empty_reports_returns_error(self) -> None:
        result = build_trend_report([])
        self.assertEqual(result["error"], "no_reports")

    def test_single_report_produces_no_flags(self) -> None:
        report = _make_report(
            samples=[_sample_with_coverage()],
        )
        result = build_trend_report([report])
        self.assertTrue(result["passed"])
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["report_count"], 1)

    def test_two_reports_no_regression_passes(self) -> None:
        r1 = _make_report(version="v1", samples=[_sample_with_coverage(text_cov=0.98)])
        r2 = _make_report(version="v2", samples=[_sample_with_coverage(text_cov=0.98)])
        result = build_trend_report([r1, r2])
        self.assertTrue(result["passed"])
        self.assertEqual(result["flags"], [])

    def test_coverage_regression_detected(self) -> None:
        r1 = _make_report(version="v1", samples=[_sample_with_coverage(text_cov=0.99)])
        r2 = _make_report(version="v2", samples=[_sample_with_coverage(text_cov=0.90)])
        result = build_trend_report([r1, r2], coverage_threshold=-0.02)
        self.assertFalse(result["passed"])
        self.assertTrue(any("coverage_regression:text_page_coverage_ratio" in f for f in result["flags"]))

    def test_reader_regression_detected(self) -> None:
        r1 = _make_report(
            version="v1",
            samples=[_sample_with_coverage(visible=100, reading_order_confidence=0.95)],
        )
        r2 = _make_report(
            version="v2",
            samples=[_sample_with_coverage(visible=50, reading_order_confidence=0.80)],
        )
        result = build_trend_report([r1, r2], reader_threshold=-0.05)
        self.assertFalse(result["passed"])
        self.assertTrue(any("reader_regression:" in f for f in result["flags"]))

    def test_render_markdown_produces_output(self) -> None:
        report = _make_report(samples=[_sample_with_coverage()])
        payload = build_trend_report([report])
        md = render_markdown(payload)
        self.assertIn("Coverage / Reader Trend Report", md)
        self.assertIn("text_page_coverage_ratio", md)
        self.assertIn("visible_block_count", md)

    def test_custom_threshold_no_regression(self) -> None:
        r1 = _make_report(version="v1", samples=[_sample_with_coverage(text_cov=0.99)])
        r2 = _make_report(version="v2", samples=[_sample_with_coverage(text_cov=0.97)])
        # drop is ~-0.02, threshold -0.025 should pass
        result = build_trend_report([r1, r2], coverage_threshold=-0.025)
        self.assertTrue(result["passed"])

    def test_multiple_samples_aggregated(self) -> None:
        samples = [
            _sample_with_coverage(text_cov=0.98),
            _sample_with_coverage(text_cov=0.96),
            _sample_with_coverage(text_cov=0.99),
        ]
        report = _make_report(samples=samples)
        result = build_trend_report([report])
        version_entry = result["versions"][0]
        text_stats = version_entry["coverage"]["text_page_coverage_ratio"]
        self.assertAlmostEqual(text_stats["min"], 0.96)
        self.assertAlmostEqual(text_stats["max"], 0.99)
        self.assertAlmostEqual(text_stats["avg"], (0.98 + 0.96 + 0.99) / 3)
        self.assertEqual(text_stats["count"], 3)

    def test_schema_version_present(self) -> None:
        result = build_trend_report([_make_report(samples=[_sample_with_coverage()])])
        self.assertEqual(result["schema_version"], "2026-06-coverage-trend-report")
