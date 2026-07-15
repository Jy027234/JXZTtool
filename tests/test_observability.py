"""Tests for observability and monitoring features (EventAggregator)."""

from __future__ import annotations

import unittest

from parsecore.runtime import EventAggregator


class TestEventAggregator(unittest.TestCase):
    """Test suite for EventAggregator observability events."""

    def test_event_aggregator_records_basic_events(self) -> None:
        """Verify EventAggregator records events with correct dimensions."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        agg.record_event(
            "quota_exceeded",
            tenant_id="tenant-a",
            quota_key="default",
            doc_id="doc-1",
            details={"used_units": 80, "limit_units": 100},
        )
        
        events = agg.get_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "quota_exceeded")
        self.assertEqual(events[0]["tenant_id"], "tenant-a")
        self.assertEqual(events[0]["quota_key"], "default")
        self.assertEqual(events[0]["doc_id"], "doc-1")

    def test_event_aggregator_filters_by_event_type(self) -> None:
        """Verify filtering events by event_type."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("too_many_inflight_jobs", tenant_id="tenant-a", quota_key="default")
        agg.record_event("embedding_retry", tenant_id="tenant-a", quota_key="default")
        
        quota_events = agg.get_events(limit=100, event_type_filter="quota_exceeded")
        self.assertEqual(len(quota_events), 1)
        self.assertEqual(quota_events[0]["event_type"], "quota_exceeded")

    def test_event_aggregator_filters_by_tenant_id(self) -> None:
        """Verify filtering events by tenant_id."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("quota_exceeded", tenant_id="tenant-b", quota_key="default")
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="premium")
        
        tenant_a_events = agg.get_events(limit=100, tenant_id_filter="tenant-a")
        self.assertEqual(len(tenant_a_events), 2)
        for event in tenant_a_events:
            self.assertEqual(event["tenant_id"], "tenant-a")

    def test_event_aggregator_maintains_counters(self) -> None:
        """Verify EventAggregator maintains event counters by dimension."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("too_many_inflight_jobs", tenant_id="tenant-a", quota_key="default")
        agg.record_event("quota_exceeded", tenant_id="tenant-b", quota_key="premium")
        
        counters = agg.get_counters()
        self.assertEqual(counters["tenant-a:default:quota_exceeded"], 2)
        self.assertEqual(counters["tenant-a:default:too_many_inflight_jobs"], 1)
        self.assertEqual(counters["tenant-b:premium:quota_exceeded"], 1)

    def test_event_aggregator_ringbuffer_respects_size_limit(self) -> None:
        """Verify EventAggregator's ringbuffer respects max_ringbuffer_size."""
        agg = EventAggregator(max_ringbuffer_size=5)
        
        for i in range(10):
            agg.record_event(
                "quota_exceeded",
                tenant_id="tenant-a",
                quota_key="default",
                details={"index": i},
            )
        
        # Should only keep last 5 events
        all_events = agg.get_events(limit=100)
        self.assertEqual(len(all_events), 5)
        
        # Verify that recent events are kept (by checking count)
        # The ringbuffer should only have 5 items total
        self.assertEqual(len(agg.ringbuffer), 5)
        
        # Verify counter stays accurate
        counters = agg.get_counters()
        self.assertEqual(counters["tenant-a:default:quota_exceeded"], 10)  # All 10 recorded

    def test_event_aggregator_prometheus_format(self) -> None:
        """Verify EventAggregator generates valid Prometheus format."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("quota_exceeded", tenant_id="tenant-a", quota_key="default")
        agg.record_event("too_many_inflight_jobs", tenant_id="tenant-b", quota_key="premium")
        agg.record_event("embedding_retry", tenant_id="tenant-a", quota_key="default")
        agg.record_event("embedding_skipped", tenant_id="tenant-a", quota_key="default")
        agg.record_event("ocr_attempted", tenant_id="tenant-a", quota_key="default", count=3)
        agg.record_event("ocr_fallback", tenant_id="tenant-a", quota_key="default", count=2)
        agg.record_event("ocr_failed", tenant_id="tenant-b", quota_key="premium", count=1)
        agg.record_event("ocr_rejected", tenant_id="tenant-b", quota_key="premium", count=4)
        agg.record_event(
            "provider_failure",
            tenant_id="tenant-a",
            quota_key="default",
            details={
                "provider_type": "embedding",
                "provider_id": "openai-compatible",
                "failure_category": "invalid_input",
            },
        )
        
        prometheus = agg.get_prometheus_metrics()
        
        # Verify it contains HELP and TYPE lines
        self.assertIn("# HELP parse_quota_exceeded_total", prometheus)
        self.assertIn("# TYPE parse_quota_exceeded_total counter", prometheus)
        self.assertIn("# HELP parse_inflight_full_total", prometheus)
        self.assertIn("# HELP parse_embedding_retry_total", prometheus)
        self.assertIn("# HELP parse_embedding_skipped_total", prometheus)
        self.assertIn("# HELP parse_ocr_attempt_total", prometheus)
        self.assertIn("# HELP parse_ocr_fallback_total", prometheus)
        self.assertIn("# HELP parse_ocr_failed_total", prometheus)
        self.assertIn("# HELP parse_ocr_rejected_total", prometheus)
        self.assertIn("# HELP parse_job_retry_scheduled_total", prometheus)
        self.assertIn("# HELP parse_job_timeout_total", prometheus)
        self.assertIn("# HELP parse_provider_failure_total", prometheus)
        self.assertIn("parse_ringbuffer_size", prometheus)  # gauge type
        
        # Verify it contains metric lines with correct labels and values
        self.assertIn('parse_quota_exceeded_total{tenant_id="tenant-a",quota_key="default"} 2', prometheus)
        self.assertIn('parse_inflight_full_total{tenant_id="tenant-b",quota_key="premium"} 1', prometheus)
        self.assertIn('parse_embedding_retry_total{tenant_id="tenant-a",quota_key="default"} 1', prometheus)
        self.assertIn('parse_embedding_skipped_total{tenant_id="tenant-a",quota_key="default"} 1', prometheus)
        self.assertIn('parse_ocr_attempt_total{tenant_id="tenant-a",quota_key="default"} 3', prometheus)
        self.assertIn('parse_ocr_fallback_total{tenant_id="tenant-a",quota_key="default"} 2', prometheus)
        self.assertIn('parse_ocr_failed_total{tenant_id="tenant-b",quota_key="premium"} 1', prometheus)
        self.assertIn('parse_ocr_rejected_total{tenant_id="tenant-b",quota_key="premium"} 4', prometheus)
        self.assertIn(
            'parse_provider_failure_total{provider_type="embedding",provider_id="openai-compatible",failure_category="invalid_input"} 1',
            prometheus,
        )
        self.assertIn("parse_ringbuffer_size 10", prometheus)

    def test_provider_failure_metric_normalizes_category_and_escapes_labels(self) -> None:
        agg = EventAggregator(max_ringbuffer_size=100)
        agg.record_event(
            "provider_failure",
            details={
                "provider_type": "rerank",
                "provider_id": 'remote"edge\\node\nline',
                "failure_category": "unbounded-custom-category",
            },
        )

        event = agg.get_events(event_type_filter="provider_failure")[0]
        prometheus = agg.get_prometheus_metrics()

        self.assertEqual(event["failure_category"], "provider_failed")
        self.assertEqual(
            agg.provider_failure_counters[("rerank", 'remote"edge\\node\nline', "provider_failed")],
            1,
        )
        self.assertIn(
            'provider_id="remote\\"edge\\\\node\\nline",failure_category="provider_failed"',
            prometheus,
        )

    def test_document_quality_metrics_use_fixed_gate_flags_and_summaries(self) -> None:
        agg = EventAggregator(max_ringbuffer_size=100)
        agg.record_event(
            "document_quality",
            tenant_id="quality-tenant",
            details={
                "quality_gate": "accept_with_warning",
                "quality_flags": ["rag_chunks_not_embedded", "custom-unbounded-flag"],
                "quality_score": 0.9,
                "text_page_coverage_ratio": 1.0,
                "embedded_chunk_ratio": 0.0,
                "quality_signal_count": 2,
                "provider_warning_count": 1,
            },
        )

        event = agg.get_events(event_type_filter="document_quality")[0]
        prometheus = agg.get_prometheus_metrics()

        self.assertEqual(event["quality_gate"], "accept_with_warning")
        self.assertEqual(event["quality_flags"], ["other", "rag_chunks_not_embedded"])
        self.assertIn('parse_document_quality_total{gate="accept_with_warning"} 1', prometheus)
        self.assertIn('parse_document_quality_flag_total{flag="other"} 1', prometheus)
        self.assertIn('parse_document_quality_flag_total{flag="rag_chunks_not_embedded"} 1', prometheus)
        self.assertIn("parse_document_quality_score_sum 0.900000", prometheus)
        self.assertIn("parse_document_quality_score_count 1", prometheus)
        self.assertIn("parse_document_embedded_chunk_ratio_sum 0.000000", prometheus)
        self.assertIn("parse_document_provider_warning_count_sum 1.000000", prometheus)

    def test_event_aggregator_with_wildcard_dimensions(self) -> None:
        """Verify EventAggregator handles default wildcard dimensions."""
        agg = EventAggregator(max_ringbuffer_size=100)
        
        # Record events with default wildcards (not specifying tenant_id/quota_key)
        agg.record_event("quota_exceeded")
        agg.record_event("embedding_retry", tenant_id="tenant-a")
        agg.record_event("too_many_inflight_jobs", quota_key="premium")
        
        counters = agg.get_counters()
        self.assertIn("*:*:quota_exceeded", counters)
        self.assertIn("tenant-a:*:embedding_retry", counters)
        self.assertIn("*:premium:too_many_inflight_jobs", counters)
        self.assertEqual(counters["*:*:quota_exceeded"], 1)


if __name__ == "__main__":
    unittest.main()
