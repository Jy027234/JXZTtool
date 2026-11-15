"""Structured per-job event logging (B2).

Each event is a single JSON object on its own line. The schema is intentionally
flat so it can be ingested by ``jq`` / Loki / DuckDB without preprocessing.

Required keys:
    timestamp    UTC ISO-8601
    event        one of: started, state_changed, retry, dead_letter, failed, completed
    job_id       parse-job id (when known)
    doc_id       document id (when known)

Optional keys (set per event):
    state        new ParseJobState value (state_changed)
    attempt      current attempt counter (retry, failed, dead_letter)
    error        error message (failed, dead_letter)
    duration_s   wall-clock seconds (completed)
    blocks       block count (completed)
    chunks       chunk count (completed)

Logger writes are append-only; if the destination directory does not exist it
is created lazily. Failures inside the logger are swallowed so logging never
breaks the parse pipeline.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, UTC, timezone
from math import isfinite
from pathlib import Path
from typing import Any

from .provider_failures import PROVIDER_FAILURE_CATEGORIES


SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)
REDACTED_VALUE = "[redacted]"


def _field_is_sensitive(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return any(marker in normalized for marker in SENSITIVE_FIELD_MARKERS)


def _redact_event_value(key: str, value: Any) -> Any:
    if _field_is_sensitive(key):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {str(child_key): _redact_event_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_event_value(key, item) for item in value]
    return value


class JobEventLogger:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **fields: Any) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
            }
            for key, value in fields.items():
                if value is None:
                    continue
                payload[key] = _redact_event_value(key, value)
            line = json.dumps(payload, ensure_ascii=False)
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.write("\n")
        except OSError:
            # Logging must never break parsing.
            return

    # P7-T02: stage timing helpers ----------------------------------------

    def log_stage_start(
        self,
        stage: str,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.log(
            "stage_started",
            stage=stage,
            job_id=job_id,
            doc_id=doc_id,
            part_id=part_id,
            tenant_id=tenant_id,
        )

    def log_stage_end(
        self,
        stage: str,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
        duration_s: float | None = None,
        error_category: str | None = None,
    ) -> None:
        self.log(
            "stage_completed" if error_category is None else "stage_failed",
            stage=stage,
            job_id=job_id,
            doc_id=doc_id,
            part_id=part_id,
            tenant_id=tenant_id,
            duration_s=duration_s,
            error_category=error_category,
        )


class ParseStageTimer:
    """Lightweight context-manager that times a parse stage and logs it.

    Usage::

        timer = ParseStageTimer(logger, job_id="j1", doc_id="d1")
        with timer.stage("parse"):
            ...
        with timer.stage("chunk"):
            ...
    """

    def __init__(
        self,
        logger: JobEventLogger,
        *,
        job_id: str | None = None,
        doc_id: str | None = None,
        part_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._logger = logger
        self._job_id = job_id
        self._doc_id = doc_id
        self._part_id = part_id
        self._tenant_id = tenant_id
        self._stages: dict[str, float] = {}

    def stage(self, name: str) -> "_StageContext":
        return _StageContext(self, name)

    @property
    def elapsed(self) -> dict[str, float]:
        return dict(self._stages)

    def _start(self, name: str) -> None:
        import time
        self._stages[name] = 0.0
        self._logger.log_stage_start(
            name,
            job_id=self._job_id,
            doc_id=self._doc_id,
            part_id=self._part_id,
            tenant_id=self._tenant_id,
        )
        self._start_ns = time.perf_counter_ns()

    def _end(self, name: str, *, error_category: str | None = None) -> None:
        import time
        duration_s = (time.perf_counter_ns() - getattr(self, "_start_ns", 0)) / 1e9
        self._stages[name] = round(duration_s, 4)
        self._logger.log_stage_end(
            name,
            job_id=self._job_id,
            doc_id=self._doc_id,
            part_id=self._part_id,
            tenant_id=self._tenant_id,
            duration_s=round(duration_s, 4),
            error_category=error_category,
        )


class _StageContext:
    def __init__(self, timer: ParseStageTimer, name: str) -> None:
        self._timer = timer
        self._name = name

    def __enter__(self) -> "_StageContext":
        self._timer._start(self._name)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        error_cat = None
        if exc is not None:
            error_cat = str(getattr(exc, "error_category", None) or "parser_failed")
        self._timer._end(self._name, error_category=error_cat)


_QUALITY_GATE_VALUES = {
    "disabled",
    "accept",
    "accept_with_warning",
    "local_rerun",
    "manual_review",
}
_QUALITY_GATE_FLAG_VALUES = {
    "text_page_coverage_below_threshold",
    "table_unit_coverage_below_threshold",
    "unit_chunk_coverage_below_threshold",
    "reading_order_confidence_below_threshold",
    "rag_empty_text_page",
    "rag_units_without_chunks",
    "rag_chunks_not_embedded",
    "rag_table_without_unit",
    "rag_figure_caption_missing",
}
_QUALITY_SUMMARY_METRICS = {
    "quality_score": "parse_document_quality_score",
    "text_page_coverage_ratio": "parse_document_text_page_coverage_ratio",
    "table_unit_coverage_ratio": "parse_document_table_unit_coverage_ratio",
    "unit_chunk_coverage_ratio": "parse_document_unit_chunk_coverage_ratio",
    "embedded_chunk_ratio": "parse_document_embedded_chunk_ratio",
    "quality_signal_count": "parse_document_quality_signal_count",
    "provider_warning_count": "parse_document_provider_warning_count",
}


def _prometheus_label_value(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class EventAggregator:
    """
    In-memory aggregator for observability events (quota_exceeded, too_many_inflight_jobs, etc.).
    Maintains ringbuffer for recent events and counters by dimension (tenant_id, quota_key, event_type).
    """
    def __init__(self, max_ringbuffer_size: int = 1000) -> None:
        self.max_ringbuffer_size = max_ringbuffer_size
        self.ringbuffer: list[dict[str, Any]] = []
        self.counters: dict[tuple[str, str, str], int] = {}  # (tenant_id, quota_key, event_type) -> count
        self.provider_failure_counters: dict[tuple[str, str, str], int] = {}
        self.quality_gate_counters: dict[str, int] = {}
        self.quality_flag_counters: dict[str, int] = {}
        self.quality_metric_sums: dict[str, float] = {}
        self.quality_metric_counts: dict[str, int] = {}

    def record_event(
        self,
        event_type: str,
        *,
        tenant_id: str = "*",
        quota_key: str = "*",
        doc_id: str | None = None,
        details: dict[str, Any] | None = None,
        count: int = 1,
    ) -> None:
        """Record an observability event."""
        now = datetime.now(timezone.utc).isoformat()
        event: dict[str, Any] = {
            "timestamp": now,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "quota_key": quota_key,
        }
        if doc_id is not None:
            event["doc_id"] = doc_id
        event_details = dict(details or {})
        if event_type == "provider_failure":
            event_details["provider_type"] = str(event_details.get("provider_type") or "unknown")
            event_details["provider_id"] = str(event_details.get("provider_id") or "unknown")
            failure_category = str(event_details.get("failure_category") or "provider_failed")
            event_details["failure_category"] = (
                failure_category
                if failure_category in PROVIDER_FAILURE_CATEGORIES
                else "provider_failed"
            )
        elif event_type == "document_quality":
            quality_gate = str(event_details.get("quality_gate") or "other")
            event_details["quality_gate"] = (
                quality_gate if quality_gate in _QUALITY_GATE_VALUES else "other"
            )
            raw_flags = event_details.get("quality_flags")
            event_details["quality_flags"] = sorted(
                {
                    flag if flag in _QUALITY_GATE_FLAG_VALUES else "other"
                    for flag in (
                        str(item)
                        for item in (raw_flags if isinstance(raw_flags, (list, tuple, set)) else ())
                    )
                    if flag
                }
            )
        if event_details:
            event.update(event_details)

        # Add to ringbuffer (FIFO with max size)
        self.ringbuffer.append(event)
        if len(self.ringbuffer) > self.max_ringbuffer_size:
            self.ringbuffer.pop(0)

        # Update counters
        key = (tenant_id, quota_key, event_type)
        self.counters[key] = self.counters.get(key, 0) + max(1, int(count))
        if event_type == "provider_failure":
            provider_type = event_details["provider_type"]
            provider_id = event_details["provider_id"]
            failure_category = event_details["failure_category"]
            provider_key = (provider_type, provider_id, failure_category)
            self.provider_failure_counters[provider_key] = (
                self.provider_failure_counters.get(provider_key, 0) + max(1, int(count))
            )
        elif event_type == "document_quality":
            quality_gate = event_details["quality_gate"]
            self.quality_gate_counters[quality_gate] = self.quality_gate_counters.get(quality_gate, 0) + 1
            for flag in event_details["quality_flags"]:
                self.quality_flag_counters[flag] = self.quality_flag_counters.get(flag, 0) + 1
            for metric_key in _QUALITY_SUMMARY_METRICS:
                raw_value = event_details.get(metric_key)
                if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                    continue
                value = float(raw_value)
                if not isfinite(value):
                    continue
                self.quality_metric_sums[metric_key] = self.quality_metric_sums.get(metric_key, 0.0) + value
                self.quality_metric_counts[metric_key] = self.quality_metric_counts.get(metric_key, 0) + 1

    def get_events(
        self,
        limit: int = 100,
        event_type_filter: str | None = None,
        tenant_id_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent events with optional filtering."""
        filtered = self.ringbuffer
        if event_type_filter:
            filtered = [e for e in filtered if e["event_type"] == event_type_filter]
        if tenant_id_filter:
            filtered = [e for e in filtered if e["tenant_id"] == tenant_id_filter]
        # Return most recent first (reverse order)
        return list(reversed(filtered[-limit:]))

    def get_counters(
        self,
        event_type_filter: str | None = None,
        tenant_id_filter: str | None = None,
    ) -> dict[str, int]:
        """Get event counters with optional filtering."""
        result = {}
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type_filter and event_type != event_type_filter:
                continue
            if tenant_id_filter and tenant_id != tenant_id_filter:
                continue
            key = f"{tenant_id}:{quota_key}:{event_type}"
            result[key] = count
        return result

    def get_prometheus_metrics(self) -> str:
        """Generate Prometheus-format metrics."""
        lines = [
            "# HELP parse_quota_exceeded_total Total quota exceeded errors",
            "# TYPE parse_quota_exceeded_total counter",
        ]
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "quota_exceeded":
                lines.append(
                    f'parse_quota_exceeded_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_inflight_full_total Total inflight backpressure rejections",
            "# TYPE parse_inflight_full_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "too_many_inflight_jobs":
                lines.append(
                    f'parse_inflight_full_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_embedding_retry_total Total embedding retry attempts",
            "# TYPE parse_embedding_retry_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "embedding_retry":
                lines.append(
                    f'parse_embedding_retry_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_embedding_skipped_total Total embedding skip failures",
            "# TYPE parse_embedding_skipped_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "embedding_skipped":
                lines.append(
                    f'parse_embedding_skipped_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_provider_failure_total Total terminal provider failures by stable category",
            "# TYPE parse_provider_failure_total counter",
        ])
        for (provider_type, provider_id, failure_category), count in self.provider_failure_counters.items():
            lines.append(
                "parse_provider_failure_total{"
                f'provider_type="{_prometheus_label_value(provider_type)}",'
                f'provider_id="{_prometheus_label_value(provider_id)}",'
                f'failure_category="{_prometheus_label_value(failure_category)}"'
                f"}} {count}"
            )

        lines.extend([
            "# HELP parse_document_quality_total Total completed document quality observations by gate",
            "# TYPE parse_document_quality_total counter",
        ])
        for quality_gate, count in sorted(self.quality_gate_counters.items()):
            lines.append(
                f'parse_document_quality_total{{gate="{_prometheus_label_value(quality_gate)}"}} {count}'
            )

        lines.extend([
            "# HELP parse_document_quality_flag_total Total document quality gate flags",
            "# TYPE parse_document_quality_flag_total counter",
        ])
        for flag, count in sorted(self.quality_flag_counters.items()):
            lines.append(
                f'parse_document_quality_flag_total{{flag="{_prometheus_label_value(flag)}"}} {count}'
            )

        for metric_key, metric_name in _QUALITY_SUMMARY_METRICS.items():
            lines.extend([
                f"# HELP {metric_name} Distribution summary for {metric_key}",
                f"# TYPE {metric_name} summary",
                f"{metric_name}_sum {self.quality_metric_sums.get(metric_key, 0.0):.6f}",
                f"{metric_name}_count {self.quality_metric_counts.get(metric_key, 0)}",
            ])

        lines.extend([
            "# HELP parse_ocr_attempt_total Total pages where OCR was attempted",
            "# TYPE parse_ocr_attempt_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_attempted":
                lines.append(
                    f'parse_ocr_attempt_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_fallback_total Total pages where OCR fallback succeeded",
            "# TYPE parse_ocr_fallback_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_fallback":
                lines.append(
                    f'parse_ocr_fallback_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_failed_total Total pages where OCR was attempted but failed",
            "# TYPE parse_ocr_failed_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_failed":
                lines.append(
                    f'parse_ocr_failed_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ocr_rejected_total Total pages where OCR was attempted but not accepted",
            "# TYPE parse_ocr_rejected_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "ocr_rejected":
                lines.append(
                    f'parse_ocr_rejected_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_job_retry_scheduled_total Total jobs scheduled for retry",
            "# TYPE parse_job_retry_scheduled_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "job_retry_scheduled":
                lines.append(
                    f'parse_job_retry_scheduled_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_job_timeout_total Total jobs recovered after soft timeout",
            "# TYPE parse_job_timeout_total counter",
        ])
        for (tenant_id, quota_key, event_type), count in self.counters.items():
            if event_type == "job_timeout":
                lines.append(
                    f'parse_job_timeout_total{{tenant_id="{tenant_id}",quota_key="{quota_key}"}} {count}'
                )

        lines.extend([
            "# HELP parse_ringbuffer_size Current event ringbuffer size",
            "# TYPE parse_ringbuffer_size gauge",
            f"parse_ringbuffer_size {len(self.ringbuffer)}",
        ])

        return "\n".join(lines)


__all__ = ["EventAggregator", "JobEventLogger", "ParseStageTimer"]
