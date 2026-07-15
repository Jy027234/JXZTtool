from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import re
import time
from datetime import datetime, timedelta, timezone
from math import isfinite, sqrt
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import (
    ParseCoreSettings,
    local_provider_registry_payload,
    local_provider_route_plan_payload,
    quality_gate_payload,
)
from .contracts import ChunkBuilder, EmbeddingProvider, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, RerankProvider, TranslationAdapter
from .events import JobEventLogger, ParseStageTimer
from .models import Chunk, ChunkSearchHit, ParseJobState, ParseOutcome, ParseRequest, StructureSearchHit
from .ocr_trace import build_ocr_decision_trace
from .payload_schemas import payload_schema_registry
from .pdf_parts import child_doc_id, create_pdf_part_file, create_pdf_part_files, detect_pdf_page_count, plan_pdf_parts
from .pipelines import ParsedDocumentArtifact, PipelineRegistry
from .profiles import describe_parse_profiles
from .provider_failures import PROVIDER_FAILURE_CATEGORIES, classify_provider_failure
from .private_files import ensure_private_directory
from .quality import evaluate_parse_quality


_PDF_PARENT_JOB_KIND = "pdf_parent"
_PDF_PART_JOB_KIND = "pdf_part"
_RERUN_CHUNKS_ONLY_MODE = "rerun_chunks_only"
_RERUN_EMBEDDINGS_ONLY_MODE = "rerun_embeddings_only"
_SEARCH_VECTOR_WEIGHT = 0.7
_SEARCH_KEYWORD_WEIGHT = 0.3
_SEARCH_METRICS_HISTORY_LIMIT = 5000
_ACTIVE_PART_STATES = {
    ParseJobState.PARSING,
    ParseJobState.STRUCTURING,
    ParseJobState.EMBEDDING,
}
_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_SEMANTIC_ROLE_WEIGHTS: dict[str, float] = {
    "title": 1.35,
    "body_section": 1.3,
    "warning": 1.25,
    "caution": 1.15,
    "note": 1.1,
    "table": 1.05,
    "paragraph": 1.0,
    "appendix": 0.95,
    "highlights_entry": 0.95,
    "front_matter": 0.8,
    "revision_record": 0.75,
    "distribution_list": 0.75,
    "toc_entry": 0.7,
    "lep_entry": 0.55,
    "header_footer": 0.3,
    "parse_artifact": 0.2,
    "version_cell": 0.2,
    "page_ref_cell": 0.2,
}
_TASK_LIKE_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]|\([a-z]\)|step\s+\d+|task\s+\d+)",
    re.IGNORECASE,
)
_NON_INDEXABLE_RAG_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
}
_LOCAL_PROVIDER_ROUTING_OPTION = "local_provider_routing"
_LOCAL_PROVIDER_ROUTE_REQUEST_OPTION = "local_provider_route_request"
_PART_RERUN_PREVIOUS_OBSERVATION_OPTION = "rerun_previous_part_observation"
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


class QuotaExceededError(RuntimeError):
    def __init__(
        self,
        *,
        tenant_id: str,
        quota_key: str,
        limit_units: int,
        used_units: int,
        requested_units: int,
        window_hours: float | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.quota_key = quota_key
        self.limit_units = int(limit_units)
        self.used_units = int(used_units)
        self.requested_units = int(requested_units)
        self.window_hours = float(window_hours) if window_hours is not None else None
        super().__init__(
            f"quota exceeded for {tenant_id}:{quota_key} "
            f"used={used_units}, requested={requested_units}, limit={limit_units}"
        )


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


class ParseRuntime:
    def __init__(
        self,
        *,
        settings: ParseCoreSettings,
        parsers: Sequence[ParserAdapter],
        chunk_builder: ChunkBuilder,
        embedding_provider: EmbeddingProvider,
        index: IndexAdapter,
        translator: TranslationAdapter,
        product_adapter: ProductAdapter,
        job_store: JobStore,
        event_logger: JobEventLogger | None = None,
        pipeline_registry: PipelineRegistry | None = None,
        rerank_provider: RerankProvider | None = None,
    ) -> None:
        self.settings = settings
        self.parsers = tuple(parsers)
        self.chunk_builder = chunk_builder
        self.embedding_provider = embedding_provider
        self.rerank_provider = rerank_provider
        self.index = index
        self.translator = translator
        self.product_adapter = product_adapter
        self.job_store = job_store
        self.event_logger = event_logger or JobEventLogger(settings.runtime.log_path)
        self.event_aggregator = EventAggregator(max_ringbuffer_size=1000)
        self.pipeline_registry = pipeline_registry
        self._search_layer_query_hits: dict[tuple[str, str], list[int]] = {}
        # Part jobs are created and executed by this runtime instance. Keep a
        # parent-scoped latest-child map so every completed part does not scan
        # the entire tenant job table just to refresh the parent manifest. Queue
        # workers keep this disabled because another worker may update a child
        # job between refreshes; those workers must read the job store instead.
        self._pdf_part_jobs_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _pdf_part_cache_enabled(self) -> bool:
        return str(self.settings.runtime.execution_mode or "inline").strip().lower() == "inline"

    def describe(self) -> dict[str, object]:
        description: dict[str, object] = {
            "project": self.settings.project_name,
            "mode": self.settings.mode,
            "database_url": self.settings.database_url,
            "object_store": self.settings.object_store,
            "index_mode": self.settings.index_mode,
            "index_embedding_dimension": self.settings.index_embedding_dimension,
            "runtime": {
                "execution_mode": self.settings.runtime.execution_mode,
                "max_workers": self.settings.runtime.max_workers,
                "poll_interval_ms": self.settings.runtime.poll_interval_ms,
                "max_upload_bytes": self.settings.runtime.max_upload_bytes,
                "max_inflight_jobs": self.settings.runtime.max_inflight_jobs,
                "max_active_parts_per_doc": self.settings.runtime.max_active_parts_per_doc,
                "max_attempts": self.settings.runtime.max_attempts,
                "retry_backoff_seconds": self.settings.runtime.retry_backoff_seconds,
                "retry_backoff_max_seconds": self.settings.runtime.retry_backoff_max_seconds,
                "job_timeout_seconds": self.settings.runtime.job_timeout_seconds,
                "part_timeout_seconds": self.settings.runtime.part_timeout_seconds,
                "allow_external_file_paths": self.settings.runtime.allow_external_file_paths,
                "staged_upload_max_bytes": self.settings.runtime.staged_upload_max_bytes,
                "api_auth_enabled": bool(str(self.settings.runtime.api_key_env).strip()),
            },
            "translation": {
                "enabled": self.settings.translation_enabled,
                "strategy": self.settings.translation_strategy,
            },
            "embedding": {
                "enabled": self.settings.providers.embedding.enabled,
                "provider": self.settings.providers.embedding.provider,
                "model": self.settings.providers.embedding.model,
            },
            "rerank": {
                "enabled": self.settings.providers.rerank.enabled,
                "provider": self.settings.providers.rerank.provider,
                "model": self.settings.providers.rerank.model,
                "candidate_limit": self.settings.providers.rerank.candidate_limit,
            },
            "product_adapter": self.settings.product_adapter,
            "parsers": [parser.name for parser in self.parsers],
            "provider_registry": self.provider_registry(),
            "payload_schemas": payload_schema_registry(),
            "quality_gate": self.quality_gate_config(),
            "profiles": describe_parse_profiles(),
            "index_layers": ["primary", "structure", "high_precision"],
            "embedding_tiers": ["small", "large"],
        }
        if self.pipeline_registry is not None:
            registry_description = self.pipeline_registry.describe()
            description["pipelines"] = registry_description["pipelines"]
            description["pipeline_cache"] = registry_description["cache"]
        return description

    def provider_registry(self) -> dict[str, Any]:
        return local_provider_registry_payload(self.settings.providers)

    def provider_route_plan(
        self,
        *,
        media_type: str | None = None,
        extension: str | None = None,
        file_name: str | None = None,
        profile: str | None = None,
        required_capabilities: Sequence[str] = (),
        include_disabled: bool = True,
    ) -> dict[str, Any]:
        return local_provider_route_plan_payload(
            self.settings.providers,
            media_type=media_type,
            extension=extension,
            file_name=file_name,
            profile=profile,
            required_capabilities=required_capabilities,
            include_disabled=include_disabled,
        )

    def quality_gate_config(self) -> dict[str, Any]:
        return quality_gate_payload(self.settings.quality_gate)

    def submit(self, request: ParseRequest) -> ParseOutcome:
        job = self.start(request)
        return self.execute(job_id=job.job_id)

    def start(self, request: ParseRequest):
        self._check_quota_limit(request)
        job = self.job_store.create(request)
        self.product_adapter.before_parse(request=request, job=job)

        return job

    def execute(self, *, job_id: str, claim_token: str | None = None) -> ParseOutcome:
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            raise LookupError(f"No parse job found for job_id={job_id!r}")
        if job.dead_lettered_at is not None:
            raise RuntimeError(
                f"job {job.job_id} is dead-lettered ({job.failure_reason!r}); refusing to execute"
            )
        if job.state == ParseJobState.FAILED and str(job.failure_reason or "").strip().lower() == "cancelled":
            raise RuntimeError(f"job {job.job_id} was cancelled; refusing to execute")
        if _job_kind(job) == _PDF_PARENT_JOB_KIND:
            raise RuntimeError("pdf_parent_not_executable")
        if job.state == ParseJobState.PENDING:
            claimed = self.claim_job(job_id=job.job_id)
            if claimed is None:
                raise RuntimeError(f"job {job.job_id} could not be claimed; refusing to execute")
            job = claimed
            claim_token = job.claim_token
        elif claim_token is None and job.claim_token:
            claim_token = job.claim_token
        attempt = int(job.attempt_count or 0)
        if attempt <= 0:
            attempt = self._increment_attempt(job_id=job.job_id)
        request = ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
            tenant_id=job.tenant_id,
            quota_key=job.quota_key,
            quota_units=job.quota_units,
        )
        request, job = self._prepare_request_for_execution(request=request, job=job)
        started_at = time.monotonic()
        stage_timer = ParseStageTimer(
            self.event_logger,
            job_id=job.job_id,
            doc_id=job.doc_id,
            part_id=str(job.options.get("part_id") or "") or None,
            tenant_id=job.tenant_id,
        )
        self.event_logger.log(
            "started",
            job_id=job.job_id,
            doc_id=job.doc_id,
            attempt=attempt,
            file_path=job.file_path,
        )
        try:
            if job.state == ParseJobState.PENDING:
                self._update_job_state(
                    job_id=job.job_id,
                    state=ParseJobState.PARSING,
                    claim_token=claim_token,
                )
                self.event_logger.log(
                    "state_changed",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    state=ParseJobState.PARSING.value,
                )
            with stage_timer.stage("parse"):
                if _is_pdf_part_request(request):
                    self._ensure_pdf_part_file(request)
                blocks = self._load_blocks_for_request(request)
                if _is_pdf_part_request(request):
                    blocks = self._normalize_pdf_part_blocks(request=request, blocks=blocks)
                self._record_ocr_observability(request=request, blocks=blocks)
            if not self._is_rerun_chunks_only(request):
                with stage_timer.stage("persist_blocks"):
                    self._ensure_active_claim(job_id=job.job_id, claim_token=claim_token)
                    self.job_store.save_blocks(
                        doc_id=request.doc_id,
                        blocks=blocks,
                        tenant_id=request.tenant_id,
                    )
            self._update_job_state(
                job_id=job.job_id,
                state=ParseJobState.STRUCTURING,
                claim_token=claim_token,
            )
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.STRUCTURING.value,
            )

            with stage_timer.stage("normalize_ir"):
                document = self._load_document_for_request(request, blocks=blocks)
            with stage_timer.stage("chunk"):
                chunks = tuple(self._load_chunks_for_request(request, blocks=blocks, document=document))
            self._update_job_state(
                job_id=job.job_id,
                state=ParseJobState.EMBEDDING,
                claim_token=claim_token,
            )
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.EMBEDDING.value,
            )
            with stage_timer.stage("embed"):
                chunks = tuple(
                    self._embed_chunks(
                        doc_id=request.doc_id,
                        chunks=chunks,
                        tenant_id=request.tenant_id,
                        quota_key=request.quota_key,
                    )
                )
                if _is_pdf_part_request(request):
                    chunks = self._normalize_pdf_part_chunks(request=request, chunks=chunks)

            with stage_timer.stage("persist_index"):
                index_manifest = self._build_index_manifest(
                    request=request,
                    job_id=job.job_id,
                    document=document,
                    chunks=chunks,
                )
                self._ensure_active_claim(job_id=job.job_id, claim_token=claim_token)
                self.job_store.save_chunks(
                    doc_id=request.doc_id,
                    chunks=chunks,
                    tenant_id=request.tenant_id,
                )
                self._ensure_active_claim(job_id=job.job_id, claim_token=claim_token)
                self.index.upsert(
                    doc_id=request.doc_id,
                    chunks=chunks,
                    tenant_id=request.tenant_id,
                    document=document,
                    index_manifest=index_manifest,
                )
                final_job = self._update_job_state(
                    job_id=job.job_id,
                    state=ParseJobState.DONE,
                    claim_token=claim_token,
                    clear_claim=True,
                )
                self._remember_pdf_part_job(final_job)
                self._persist_document_views(job=final_job, blocks=blocks)
                completed_parent_job = None
                if _is_pdf_part_request(request):
                    refreshed_parent = self.refresh_partitioned_parent(
                        doc_id=str(request.options.get("source_doc_id") or request.options.get("parent_doc_id") or ""),
                        tenant_id=request.tenant_id,
                        parent_job_id=str(request.options.get("parent_job_id") or ""),
                        changed_part_ids=[str(request.options.get("part_id") or request.doc_id)],
                    )
                    if getattr(refreshed_parent, "state", None) == ParseJobState.DONE:
                        completed_parent_job = refreshed_parent
            with stage_timer.stage("quality_observability"):
                self._record_document_quality_observability(
                    job=final_job,
                    blocks=blocks,
                    index_manifest=index_manifest,
                )
                if completed_parent_job is not None:
                    self._record_document_quality_observability(job=completed_parent_job)
            stage_timings = stage_timer.elapsed
            # Saving blocks happens before normalization to preserve the
            # existing failure semantics.  The aggregate is the end-to-end
            # persistence/index phase used by performance tooling.
            stage_timings["persist_index"] = round(
                stage_timings.get("persist_blocks", 0.0)
                + stage_timings.get("persist_index", 0.0),
                4,
            )
            outcome = ParseOutcome(
                job=final_job,
                blocks=blocks,
                chunks=chunks,
                stage_timings=stage_timings,
            )
            self.event_logger.log(
                "completed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                attempt=attempt,
                duration_s=round(time.monotonic() - started_at, 3),
                blocks=len(blocks),
                chunks=len(chunks),
            )
            self.product_adapter.after_parse(outcome=outcome)
            return outcome
        except RuntimeError as exc:
            if str(exc) == "stale_claim":
                raise
            max_attempts = max(1, int(self.settings.runtime.max_attempts))
            if attempt >= max_attempts:
                dead_letter = getattr(self.job_store, "mark_dead_letter", None)
                if callable(dead_letter):
                    failed_job = dead_letter(
                        job_id=job.job_id,
                        reason=str(exc),
                        expected_claim_token=claim_token,
                    )
                else:
                    failed_job = self._update_job_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                        claim_token=claim_token,
                        clear_claim=True,
                    )
                self.event_logger.log(
                    "dead_letter",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                if self._should_retry_failed_job():
                    failed_job = self._schedule_retry(job=job, attempt=attempt, error=exc, claim_token=claim_token)
                else:
                    failed_job = self._update_job_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                        claim_token=claim_token,
                        clear_claim=True,
                    )
                    self.event_logger.log(
                        "failed",
                        job_id=job.job_id,
                        doc_id=job.doc_id,
                        attempt=attempt,
                        error=str(exc),
                    )
            if _job_kind(job) == _PDF_PART_JOB_KIND:
                self._remember_pdf_part_job(failed_job)
                self.refresh_partitioned_parent(
                    doc_id=str(job.options.get("source_doc_id") or job.options.get("parent_doc_id") or ""),
                    tenant_id=job.tenant_id,
                    parent_job_id=str(job.options.get("parent_job_id") or ""),
                )
            self.product_adapter.on_failure(request=request, job=failed_job, error=exc)
            raise
        except Exception as exc:
            max_attempts = max(1, int(self.settings.runtime.max_attempts))
            if attempt >= max_attempts:
                dead_letter = getattr(self.job_store, "mark_dead_letter", None)
                if callable(dead_letter):
                    failed_job = dead_letter(
                        job_id=job.job_id,
                        reason=str(exc),
                        expected_claim_token=claim_token,
                    )
                else:
                    failed_job = self._update_job_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                        claim_token=claim_token,
                        clear_claim=True,
                    )
                self.event_logger.log(
                    "dead_letter",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                if self._should_retry_failed_job():
                    failed_job = self._schedule_retry(job=job, attempt=attempt, error=exc, claim_token=claim_token)
                else:
                    failed_job = self._update_job_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                        claim_token=claim_token,
                        clear_claim=True,
                    )
                    self.event_logger.log(
                        "failed",
                        job_id=job.job_id,
                        doc_id=job.doc_id,
                        attempt=attempt,
                        error=str(exc),
                    )
            if _job_kind(job) == _PDF_PART_JOB_KIND:
                self._remember_pdf_part_job(failed_job)
                self.refresh_partitioned_parent(
                    doc_id=str(job.options.get("source_doc_id") or job.options.get("parent_doc_id") or ""),
                    tenant_id=job.tenant_id,
                    parent_job_id=str(job.options.get("parent_job_id") or ""),
                )
            self.product_adapter.on_failure(request=request, job=failed_job, error=exc)
            raise

    def _should_retry_failed_job(self) -> bool:
        return str(self.settings.runtime.execution_mode).strip().lower() == "queue-worker"

    def _schedule_retry(
        self,
        *,
        job,
        attempt: int,
        error: Exception,
        claim_token: str | None = None,
    ):
        delay = _retry_delay_seconds(
            attempt,
            base_seconds=self.settings.runtime.retry_backoff_seconds,
            max_seconds=self.settings.runtime.retry_backoff_max_seconds,
        )
        next_attempt_at = _iso_after_seconds(delay)
        options = dict(job.options or {})
        options["next_attempt_at"] = next_attempt_at
        options["retry_delay_s"] = delay
        options["last_error"] = str(error)
        retry_job = self._update_job_state(
            job_id=job.job_id,
            state=ParseJobState.PENDING,
            failure_reason=f"retry_scheduled:{type(error).__name__}:{error}",
            claim_token=claim_token,
            clear_claim=True,
            next_attempt_at=next_attempt_at,
        )
        update_options = getattr(self.job_store, "update_options", None)
        if callable(update_options):
            update_options(job_id=job.job_id, options=options)
        self.event_logger.log(
            "retry",
            job_id=job.job_id,
            doc_id=job.doc_id,
            attempt=attempt,
            error=str(error),
            retry_delay_s=delay,
        )
        self.event_aggregator.record_event(
            "job_retry_scheduled",
            tenant_id=job.tenant_id or "default",
            quota_key=job.quota_key or "default",
            doc_id=job.doc_id,
            details={
                "job_id": job.job_id,
                "attempt": attempt,
                "retry_delay_s": delay,
                "next_attempt_at": next_attempt_at,
                "error": str(error),
            },
        )
        return retry_job

    def _increment_attempt(self, *, job_id: str) -> int:
        increment = getattr(self.job_store, "increment_attempt", None)
        if callable(increment):
            return int(increment(job_id=job_id))
        # In-memory store path: read-modify-write the in-place job.
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            return 1
        job.attempt_count = int(job.attempt_count or 0) + 1
        return job.attempt_count

    def claim_job(self, *, job_id: str):
        claim_job = getattr(self.job_store, "claim_job", None)
        if not callable(claim_job):
            return None
        pending = self.job_store.get_job(job_id=job_id)
        if pending is None:
            return None
        if _job_kind(pending) == _PDF_PARENT_JOB_KIND:
            return None
        return claim_job(
            job_id=job_id,
            lease_expires_at=_lease_expires_at_for_job(pending, self.settings.runtime),
        )

    def _update_job_state(
        self,
        *,
        job_id: str,
        state: ParseJobState,
        failure_reason: str | None = None,
        claim_token: str | None = None,
        clear_claim: bool = False,
        next_attempt_at: str | None = None,
    ):
        return self.job_store.update_state(
            job_id=job_id,
            state=state,
            failure_reason=failure_reason,
            expected_claim_token=claim_token,
            clear_claim=clear_claim,
            next_attempt_at=next_attempt_at,
        )

    def _ensure_active_claim(self, *, job_id: str, claim_token: str | None) -> None:
        if claim_token is None:
            return
        current = self.job_store.get_job(job_id=job_id)
        if current is None or current.claim_token != claim_token or current.state not in _ACTIVE_PART_STATES:
            raise RuntimeError("stale_claim")

    def _persist_document_views(
        self,
        *,
        job: Any,
        blocks: Sequence[Any],
        replace_prefix: str | None = None,
    ) -> bool:
        save_document_views = getattr(self.job_store, "save_document_views", None)
        replace_document_views = getattr(self.job_store, "replace_document_views_by_prefix", None)
        if replace_prefix and not callable(replace_document_views):
            return False

        if not replace_prefix and not callable(save_document_views):
            return False
        try:
            from .api_payloads import _document_view_rows

            snapshot = {
                "doc_id": getattr(job, "doc_id", ""),
                "job": job,
                "blocks": tuple(blocks),
                "chunks": (),
                "partition_parts": [],
            }
            views = _document_view_rows(snapshot)
            if replace_prefix:
                replace_document_views(
                    doc_id=str(getattr(job, "doc_id", "")),
                    tenant_id=getattr(job, "tenant_id", None),
                    item_id_prefix=replace_prefix,
                    pages=views.get("pages", []),
                    lines=views.get("lines", []),
                    records=views.get("records", []),
                )
            else:
                save_document_views(
                    doc_id=str(getattr(job, "doc_id", "")),
                    tenant_id=getattr(job, "tenant_id", None),
                    pages=views.get("pages", []),
                    lines=views.get("lines", []),
                    records=views.get("records", []),
                )
            return True
        except Exception as exc:  # pragma: no cover - defensive observability path
            self.event_logger.log(
                "document_views_persist_failed",
                job_id=getattr(job, "job_id", ""),
                doc_id=getattr(job, "doc_id", ""),
                error=str(exc),
            )
            self.event_aggregator.record_event(
                "document_views_persist_failed",
                tenant_id=str(getattr(job, "tenant_id", "default") or "default"),
                quota_key=str(getattr(job, "quota_key", "default") or "default"),
                doc_id=str(getattr(job, "doc_id", "") or ""),
                details={"error": str(exc)},
            )
            return False

    def _record_document_quality_observability(
        self,
        *,
        job: Any,
        blocks: Sequence[Any] | None = None,
        index_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            doc_id = str(getattr(job, "doc_id", "") or "")
            tenant_id = str(getattr(job, "tenant_id", "default") or "default")
            observation_blocks = tuple(blocks or ())
            observation_manifest = index_manifest
            if blocks is None or index_manifest is None:
                snapshot = self.get_document(doc_id=doc_id, tenant_id=tenant_id)
                observation_blocks = tuple(snapshot.get("blocks") or ())
                raw_manifest = snapshot.get("index_manifest")
                observation_manifest = raw_manifest if isinstance(raw_manifest, Mapping) else {}
            details = _runtime_document_quality_observation(
                settings=self.settings,
                blocks=observation_blocks,
                index_manifest=observation_manifest or {},
            )
            self.event_aggregator.record_event(
                "document_quality",
                tenant_id=tenant_id,
                quota_key=str(getattr(job, "quota_key", "default") or "default"),
                doc_id=doc_id,
                details={
                    "job_id": str(getattr(job, "job_id", "") or ""),
                    **details,
                },
            )
        except Exception as exc:  # pragma: no cover - quality telemetry must not fail parsing
            self.event_logger.log(
                "document_quality_observability_failed",
                job_id=str(getattr(job, "job_id", "") or ""),
                doc_id=str(getattr(job, "doc_id", "") or ""),
                error_type=type(exc).__name__,
            )

    def get_job(self, *, job_id: str):
        return self.job_store.get_job(job_id=job_id)

    def cancel_job(self, *, job_id: str) -> dict[str, Any]:
        """Cancel a host-owned parse job and revoke its claim.

        Python worker threads cannot be forcefully interrupted safely.  Clearing
        the claim makes the existing execution checks stop before the next
        persistence boundary, while pending jobs are prevented from starting.
        """
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            raise LookupError(f"No parse job found for job_id={job_id!r}")
        if job.state in {ParseJobState.DONE, ParseJobState.FAILED}:
            already_cancelled = str(job.failure_reason or "").strip().lower() == "cancelled"
            return {
                "job": job,
                "cancelled": already_cancelled,
                "state": "cancelled" if already_cancelled else str(job.state.value),
                "message": "cancelled" if already_cancelled else "job is already terminal",
            }
        cancelled = self._update_job_state(
            job_id=job.job_id,
            state=ParseJobState.FAILED,
            failure_reason="cancelled",
            clear_claim=True,
        )
        self.event_logger.log(
            "cancelled",
            job_id=job.job_id,
            doc_id=job.doc_id,
            state="cancelled",
        )
        return {
            "job": cancelled,
            "cancelled": True,
            "state": "cancelled",
            "message": "cancelled",
        }

    def get_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        document_view_types: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        normalized_tenant = str(tenant_id or getattr(latest_job, "tenant_id", "default") or "default")
        if latest_job is None:
            blocks: tuple[Any, ...] = ()
            chunks: tuple[Any, ...] = ()
            partition_parts: list[dict[str, Any]] = []
        else:
            partition_parts = self.partition_parts_for_document(
                doc_id=doc_id,
                tenant_id=latest_job.tenant_id,
                parent_job_id=(
                    latest_job.job_id
                    if _job_kind(latest_job) == _PDF_PARENT_JOB_KIND
                    else None
                ),
            )
            blocks = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
            chunks = tuple(self.job_store.get_chunks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
            if _job_kind(latest_job) == _PDF_PARENT_JOB_KIND and partition_parts and not blocks:
                blocks, chunks = self._merged_partition_artifacts(
                    parent_doc_id=doc_id,
                    tenant_id=latest_job.tenant_id,
                    parent_job_id=latest_job.job_id,
                )
        document_views = self._document_views_for_types(
            doc_id=doc_id,
            tenant_id=getattr(latest_job, "tenant_id", tenant_id),
            view_types=document_view_types,
        ) if latest_job is not None else {}
        describe_document = getattr(self.index, "describe_document", None)
        index_manifest = None
        if callable(describe_document):
            index_manifest = describe_document(doc_id=doc_id, tenant_id=normalized_tenant)
        if index_manifest is None and latest_job is not None:
            index_manifest = self._derive_index_manifest_from_snapshot(
                job=latest_job,
                blocks=blocks,
                chunks=chunks,
            )
        return {
            "doc_id": doc_id,
            "job": latest_job,
            "blocks": blocks,
            "chunks": chunks,
            "document_views": document_views,
            "index_manifest": index_manifest,
            "partition_parts": partition_parts,
            "provider_registry": self.provider_registry(),
            "quality_gate": self.quality_gate_config(),
        }

    def get_document_records_projection(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        limit: int | None = 100,
        offset: int = 0,
        query: str | None = None,
        table_id: str | None = None,
        quality_signal: str | None = None,
        field_filters: Mapping[str, Any] | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> dict[str, Any] | None:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            return None
        snapshot = self._minimal_document_snapshot(doc_id=doc_id, job=latest_job)
        query_document_records = getattr(self.job_store, "query_document_records", None)
        if callable(query_document_records):
            result = query_document_records(
                doc_id=doc_id,
                tenant_id=latest_job.tenant_id,
                limit=limit,
                offset=offset,
                query=query,
                table_id=table_id,
                quality_signal=quality_signal,
                field_filters=field_filters,
                page_start=page_start,
                page_end=page_end,
            )
            if bool(result.get("persisted")):
                from .api_payloads import _document_records_response

                return _document_records_response(
                    snapshot,
                    total=int(result.get("total") or 0),
                    limit=result.get("limit"),
                    offset=int(result.get("offset") or 0),
                    items=tuple(item for item in result.get("items", ()) if isinstance(item, dict)),
                )

        fallback_snapshot = self.get_document(doc_id=doc_id, tenant_id=tenant_id)
        from .api_payloads import _document_records_projection

        return _document_records_projection(
            fallback_snapshot,
            limit=limit,
            offset=offset,
            query=query,
            table_id=table_id,
            quality_signal=quality_signal,
            field_filters=field_filters,
            page_start=page_start,
            page_end=page_end,
        )

    def _document_views_for_types(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        view_types: Sequence[str] | None,
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        if not view_types:
            return {}
        document_views: dict[str, Sequence[Mapping[str, Any]]] = {}
        for view_type in _normalized_document_view_types(view_types):
            getter = getattr(self.job_store, f"get_document_{view_type}", None)
            if callable(getter):
                document_views[view_type] = getter(doc_id=doc_id, tenant_id=tenant_id)
        return document_views

    def _minimal_document_snapshot(self, *, doc_id: str, job: Any) -> dict[str, Any]:
        return {
            "doc_id": doc_id,
            "job": job,
            "blocks": (),
            "chunks": (),
            "document_views": {},
            "index_manifest": None,
            "partition_parts": [],
            "provider_registry": self.provider_registry(),
            "quality_gate": self.quality_gate_config(),
        }

    def start_pdf_part_jobs(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        target_pages_per_part: int | None = None,
        ocr_heavy_pages_per_part: int | None = None,
        max_active_parts_per_doc: int | None = None,
        profile: str | None = None,
        materialize_part_files: bool = True,
    ) -> dict[str, Any]:
        source_job = self._resolve_latest_non_partition_job(doc_id=doc_id, tenant_id=tenant_id)
        if source_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        if not _is_pdf_job(source_job):
            raise ValueError("document_not_pdf")

        total_pages = detect_pdf_page_count(source_job.file_path)
        parent_options = dict(source_job.options)
        effective_profile = str(profile or parent_options.get("profile") or "large-pdf")
        # P5-T01: extract file size for part strategy decision
        _file_size_bytes: int | None = None
        try:
            _file_size_bytes = Path(source_job.file_path).stat().st_size
        except Exception:
            pass
        part_specs = plan_pdf_parts(
            doc_id,
            total_pages,
            target_pages_per_part=target_pages_per_part,
            ocr_heavy_pages_per_part=ocr_heavy_pages_per_part,
            profile=effective_profile,
            options=parent_options,
            file_size_bytes=_file_size_bytes,
            ocr_page_ratio=parent_options.get("ocr_page_ratio"),
            historical_failure_rate=parent_options.get("historical_failure_rate"),
        )
        parent_options.update(
            {
                "job_kind": _PDF_PARENT_JOB_KIND,
                "partitioned": True,
                "source_doc_id": doc_id,
                "source_job_id": source_job.job_id,
                "total_pages": total_pages,
                "target_pages_per_part": target_pages_per_part,
                "ocr_heavy_pages_per_part": ocr_heavy_pages_per_part,
                "max_active_parts_per_doc": max_active_parts_per_doc,
                "profile": effective_profile,
                "part_specs": part_specs,
            }
        )
        parent_job = self.start(
            ParseRequest(
                doc_id=doc_id,
                file_path=source_job.file_path,
                media_type=source_job.media_type,
                options=parent_options,
                tenant_id=source_job.tenant_id,
                quota_key=source_job.quota_key,
                quota_units=source_job.quota_units,
            )
        )
        parent_job = self._update_job_state(
            job_id=parent_job.job_id,
            state=ParseJobState.PARTIAL,
            clear_claim=True,
        )

        part_file_specs = []
        for spec in part_specs:
            part_id = str(spec.get("part_id") or child_doc_id(parent_job.doc_id, spec.get("part_index") or 1))
            part_file = self._pdf_part_file_path(
                source_path=source_job.file_path,
                parent_doc_id=parent_job.doc_id,
                parent_job_id=parent_job.job_id,
                part_id=part_id,
            )
            part_file_specs.append(
                {
                    "target_path": str(part_file),
                    "page_start": spec.get("page_start"),
                    "page_end": spec.get("page_end"),
                }
            )
        if materialize_part_files:
            for part_file_spec in part_file_specs:
                target = Path(str(part_file_spec["target_path"]))
                ensure_private_directory(
                    target.parent,
                    allowed_root=self._pdf_parts_root(source_path=source_job.file_path),
                )
            create_pdf_part_files(source_job.file_path, part_file_specs)

        part_jobs = []
        for spec in part_specs:
            part_jobs.append(
                self._start_pdf_part_job(
                    source_job=source_job,
                    parent_job=parent_job,
                    spec=spec,
                    profile=effective_profile,
                    max_active_parts_per_doc=max_active_parts_per_doc,
                    create_part_file=materialize_part_files,
                )
            )
        parent_job = self.refresh_partitioned_parent(
            doc_id=doc_id,
            tenant_id=source_job.tenant_id,
            parent_job_id=parent_job.job_id,
        )
        return {
            "doc_id": doc_id,
            "tenant_id": source_job.tenant_id,
            "parent_job": parent_job,
            "total_pages": total_pages,
            "parts": self.partition_parts_for_document(
                doc_id=doc_id,
                tenant_id=source_job.tenant_id,
                parent_job_id=parent_job.job_id,
            ),
            "part_jobs": tuple(part_jobs),
        }

    def rerun_pdf_part(
        self,
        *,
        doc_id: str,
        part_id: str,
        tenant_id: str | None = None,
        profile: str | None = None,
        provider_route_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        parent_job = self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        if parent_job is None:
            raise LookupError(f"No partitioned parse job found for doc_id={doc_id!r}")
        specs = _part_specs_from_job(parent_job)
        matching = [spec for spec in specs if str(spec.get("part_id")) == str(part_id)]
        if not matching:
            raise LookupError(f"No part found for part_id={part_id!r}")
        source_job_id = str(parent_job.options.get("source_job_id") or "")
        source_job = self.job_store.get_job(job_id=source_job_id) if source_job_id else None
        if source_job is None:
            source_job = parent_job
        route_request = _normalize_local_provider_route_request(provider_route_plan)
        previous_part_observation = self._current_part_observation(
            doc_id=doc_id,
            part_id=part_id,
            tenant_id=parent_job.tenant_id,
        )
        part_job = self._start_pdf_part_job(
            source_job=source_job,
            parent_job=parent_job,
            spec=matching[0],
            profile=str(profile or parent_job.options.get("profile") or source_job.options.get("profile") or "large-pdf"),
            max_active_parts_per_doc=_safe_optional_int(parent_job.options.get("max_active_parts_per_doc")),
            rerun=True,
            provider_route_request=route_request,
            previous_part_observation=previous_part_observation,
        )
        self.refresh_partitioned_parent(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
        )
        return {
            "doc_id": doc_id,
            "tenant_id": parent_job.tenant_id,
            "part_id": part_id,
            "job": part_job,
            "previous_part_observation": previous_part_observation,
        }

    def rerun_pdf_parts(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        part_ids: Sequence[str] | None = None,
        failed_only: bool = False,
        state_filter: Sequence[str] | None = None,
        profile: str | None = None,
        provider_route_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        parent_job = self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        if parent_job is None:
            raise LookupError(f"No partitioned parse job found for doc_id={doc_id!r}")
        route_request = _normalize_local_provider_route_request(provider_route_plan)
        requested_part_ids = {str(part_id) for part_id in tuple(part_ids or ()) if str(part_id).strip()}
        requested_states = {
            str(state).strip().lower()
            for state in tuple(state_filter or ())
            if str(state).strip()
        }
        submitted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for part in self.partition_parts_for_document(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
        ):
            part_id = str(part.get("part_id") or "")
            state = str(part.get("state") or "pending")
            if requested_part_ids and part_id not in requested_part_ids:
                skipped.append({"part_id": part_id, "state": state, "reason": "not_requested"})
                continue
            if failed_only and state not in {"failed", "cancelled"}:
                skipped.append({"part_id": part_id, "state": state, "reason": "not_failed"})
                continue
            if requested_states and state not in requested_states:
                skipped.append({"part_id": part_id, "state": state, "reason": "state_not_selected"})
                continue
            rerun = self.rerun_pdf_part(
                doc_id=doc_id,
                part_id=part_id,
                tenant_id=parent_job.tenant_id,
                profile=profile,
                provider_route_plan=route_request,
            )
            job = rerun["job"]
            submitted.append(
                {
                    "part_id": part_id,
                    "job": job,
                    "previous_part_observation": rerun.get("previous_part_observation"),
                }
            )
        return {
            "doc_id": doc_id,
            "tenant_id": parent_job.tenant_id,
            "submitted": submitted,
            "skipped": skipped,
        }

    def cancel_pdf_part(
        self,
        *,
        doc_id: str,
        part_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        parent_job = self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        if parent_job is None:
            raise LookupError(f"No partitioned parse job found for doc_id={doc_id!r}")
        child_job = None
        for job in self._latest_pdf_part_jobs(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
            refresh=True,
        ):
            if str((job.options or {}).get("part_id") or "") == str(part_id):
                child_job = job
                break
        if child_job is None:
            raise LookupError(f"No part found for part_id={part_id!r}")
        cancellable_states = {ParseJobState.PENDING}
        cancelled = False
        job = child_job
        if child_job.state in cancellable_states:
            job = self._update_job_state(
                job_id=child_job.job_id,
                state=ParseJobState.FAILED,
                failure_reason="cancelled",
                clear_claim=True,
            )
            self._remember_pdf_part_job(job)
            cancelled = True
            self.refresh_partitioned_parent(
                doc_id=doc_id,
                tenant_id=parent_job.tenant_id,
                parent_job_id=parent_job.job_id,
            )
        return {
            "doc_id": doc_id,
            "tenant_id": parent_job.tenant_id,
            "part_id": part_id,
            "cancelled": cancelled,
            "job": job,
            "state": "cancelled" if cancelled else str(job.state.value),
            "message": "cancelled" if cancelled else "part is already running or completed",
        }

    def latest_pdf_part_job(
        self,
        *,
        doc_id: str,
        part_id: str,
        tenant_id: str | None = None,
    ):
        parent_job = self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        if parent_job is None:
            return None
        for job in self._latest_pdf_part_jobs(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
            refresh=True,
        ):
            if str((job.options or {}).get("part_id") or "") == str(part_id):
                return job
        return None

    def partition_parts_for_document(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        parent_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parent_job = (
            self.job_store.get_job(job_id=parent_job_id)
            if parent_job_id
            else self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        )
        if parent_job is None:
            return []
        specs = _part_specs_from_job(parent_job)
        child_jobs = self._latest_pdf_part_jobs(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
            refresh=True,
        )
        child_by_part_id = {
            str(job.options.get("part_id") or ""): job
            for job in child_jobs
            if str(job.options.get("part_id") or "")
        }
        parts: list[dict[str, Any]] = []
        for index, spec in enumerate(specs, start=1):
            part_id = str(spec.get("part_id") or child_doc_id(doc_id, index))
            child = child_by_part_id.get(part_id)
            part_doc_id = str(spec.get("part_doc_id") or part_id)
            page_start = _safe_int_runtime(spec.get("page_start"), default=1)
            page_end = _safe_int_runtime(spec.get("page_end"), default=page_start)
            table_count = 0
            provider_ids: list[str] = []
            local_provider_routing: dict[str, Any] | None = None
            provider_route_plan: dict[str, Any] | None = None
            previous_part_observation: dict[str, Any] | None = None
            if child is not None:
                child_options = getattr(child, "options", {}) or {}
                if isinstance(child_options, Mapping):
                    raw_route = child_options.get(_LOCAL_PROVIDER_ROUTING_OPTION)
                    if isinstance(raw_route, Mapping):
                        local_provider_routing = dict(raw_route)
                    route_request = _local_provider_route_request(child_options)
                    if route_request is not None:
                        provider_route_plan = dict(route_request)
                    raw_previous_observation = child_options.get(_PART_RERUN_PREVIOUS_OBSERVATION_OPTION)
                    if isinstance(raw_previous_observation, Mapping):
                        previous_part_observation = _runtime_json_safe_mapping(raw_previous_observation)
            if child is not None and child.state == ParseJobState.DONE:
                child_blocks = tuple(self.job_store.get_blocks(doc_id=child.doc_id, tenant_id=child.tenant_id))
                table_count = sum(
                    1
                    for block in child_blocks
                    if str(getattr(getattr(block, "type", None), "value", getattr(block, "type", ""))) == "table"
                )
                provider_ids = _runtime_provider_ids(child_blocks)
            if not provider_ids and local_provider_routing:
                selected_provider_id = str(local_provider_routing.get("selected_provider_id") or "").strip()
                if selected_provider_id:
                    provider_ids = [selected_provider_id]
            state = str((child.state.value if child is not None else spec.get("state")) or "pending")
            failure_reason = getattr(child, "failure_reason", None) if child is not None else None
            if state == ParseJobState.FAILED.value and str(failure_reason or "").strip().lower() == "cancelled":
                state = "cancelled"
            part_payload = {
                "parse_unit_id": part_id,
                "part_id": part_id,
                "source_doc_id": doc_id,
                "part_doc_id": child.doc_id if child is not None else part_doc_id,
                "part_index": _safe_int_runtime(spec.get("part_index"), default=index),
                "source_type": str(parent_job.media_type or ""),
                "page_start": page_start,
                "page_end": page_end,
                "page_count": _safe_int_runtime(spec.get("page_count"), default=max(1, page_end - page_start + 1)),
                "state": state,
                "raw_state": str((child.state.value if child is not None else spec.get("state")) or "pending"),
                "job_id": child.job_id if child is not None else None,
                "attempts": int(getattr(child, "attempt_count", 0) or 0) if child is not None else 0,
                "table_count": table_count,
                "quality_signal_count": 0,
                "rerun_supported": True,
                "last_error": failure_reason,
                "provider_ids": provider_ids,
            }
            if provider_route_plan:
                part_payload["provider_route_plan"] = provider_route_plan
            if local_provider_routing:
                part_payload["local_provider_routing"] = local_provider_routing
            if previous_part_observation:
                part_payload["previous_part_observation"] = previous_part_observation
            parts.append(part_payload)
        return parts

    def refresh_partitioned_parent(
        self,
        *,
        doc_id: str,
        tenant_id: str | None = None,
        parent_job_id: str | None = None,
        changed_part_ids: Sequence[str] | None = None,
    ):
        if not doc_id:
            return None
        parent_job = (
            self.job_store.get_job(job_id=parent_job_id)
            if parent_job_id
            else self._latest_partition_parent_job(doc_id=doc_id, tenant_id=tenant_id)
        )
        if parent_job is None:
            return None
        child_jobs = self._latest_pdf_part_jobs(
            doc_id=doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
        )
        new_state = self._partition_parent_state(child_jobs)
        changed_ids = tuple(
            str(part_id).strip()
            for part_id in tuple(changed_part_ids or ())
            if str(part_id).strip()
        )
        views_dirty = False
        views_updated = False
        blocks_for_views: tuple[Any, ...] = ()
        if changed_ids:
            views_dirty, views_updated = self._refresh_partitioned_parent_incremental(
                parent_doc_id=doc_id,
                parent_job=parent_job,
                child_jobs=child_jobs,
                changed_part_ids=changed_ids,
            )
        else:
            blocks, chunks = self._merged_partition_artifacts(
                parent_doc_id=doc_id,
                tenant_id=parent_job.tenant_id,
                parent_job_id=parent_job.job_id,
            )
            if blocks or chunks:
                existing_blocks = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=parent_job.tenant_id))
                if new_state == ParseJobState.DONE or len(blocks) >= len(existing_blocks):
                    self.job_store.save_blocks(doc_id=doc_id, blocks=blocks, tenant_id=parent_job.tenant_id)
                    self.job_store.save_chunks(doc_id=doc_id, chunks=chunks, tenant_id=parent_job.tenant_id)
                    blocks_for_views = tuple(blocks)
                    views_dirty = True
                    index_manifest = self._derive_index_manifest_from_snapshot(
                        job=parent_job,
                        blocks=blocks,
                        chunks=chunks,
                    )
                    self.index.upsert(
                        doc_id=doc_id,
                        chunks=chunks,
                        tenant_id=parent_job.tenant_id,
                        document=None,
                        index_manifest=index_manifest,
                    )
        if parent_job.state != new_state:
            parent_job = self._update_job_state(
                job_id=parent_job.job_id,
                state=new_state,
                clear_claim=True,
            )
        if views_dirty and not views_updated:
            if not blocks_for_views:
                blocks_for_views = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=parent_job.tenant_id))
            self._persist_document_views(job=parent_job, blocks=blocks_for_views)
        return parent_job

    def _refresh_partitioned_parent_incremental(
        self,
        *,
        parent_doc_id: str,
        parent_job: Any,
        child_jobs: Sequence[Any],
        changed_part_ids: Sequence[str],
    ) -> tuple[bool, bool]:
        child_by_part_id = {
            str((getattr(job, "options", {}) or {}).get("part_id") or job.doc_id): job
            for job in child_jobs
        }
        replaced: list[tuple[str, tuple[Any, ...], tuple[Chunk, ...]]] = []
        replace_blocks = getattr(self.job_store, "replace_blocks_by_prefix", None)
        replace_chunks = getattr(self.job_store, "replace_chunks_by_prefix", None)
        if not callable(replace_blocks) or not callable(replace_chunks):
            blocks, chunks = self._merged_partition_artifacts(
                parent_doc_id=parent_doc_id,
                tenant_id=parent_job.tenant_id,
                parent_job_id=parent_job.job_id,
            )
            if blocks or chunks:
                self.job_store.save_blocks(doc_id=parent_doc_id, blocks=blocks, tenant_id=parent_job.tenant_id)
                self.job_store.save_chunks(doc_id=parent_doc_id, chunks=chunks, tenant_id=parent_job.tenant_id)
                index_manifest = self._derive_index_manifest_from_snapshot(
                    job=parent_job,
                    blocks=blocks,
                    chunks=chunks,
                )
                self.index.upsert(
                    doc_id=parent_doc_id,
                    chunks=chunks,
                    tenant_id=parent_job.tenant_id,
                    document=None,
                    index_manifest=index_manifest,
                )
                return True, False
            return False, False

        views_updated_all = True
        for part_id in changed_part_ids:
            child = child_by_part_id.get(str(part_id))
            if child is None or child.state != ParseJobState.DONE:
                continue
            prefix = _merged_part_prefix(parent_doc_id=parent_doc_id, part_id=str(part_id))
            blocks, chunks = self._merged_partition_artifacts(
                parent_doc_id=parent_doc_id,
                tenant_id=parent_job.tenant_id,
                parent_job_id=parent_job.job_id,
                part_ids=[str(part_id)],
            )
            replace_blocks(
                doc_id=parent_doc_id,
                blocks=blocks,
                block_id_prefix=prefix,
                tenant_id=parent_job.tenant_id,
            )
            replace_chunks(
                doc_id=parent_doc_id,
                chunks=chunks,
                chunk_id_prefix=prefix,
                tenant_id=parent_job.tenant_id,
            )
            views_updated_all = (
                self._persist_document_views(job=parent_job, blocks=blocks, replace_prefix=prefix)
                and views_updated_all
            )
            replaced.append((prefix, blocks, chunks))

        if not replaced:
            return False, False

        index_manifest = self._incremental_partition_index_manifest(
            parent_job=parent_job,
            child_jobs=child_jobs,
            changed_artifacts=replaced,
        )
        if index_manifest is None:
            all_blocks = tuple(self.job_store.get_blocks(doc_id=parent_doc_id, tenant_id=parent_job.tenant_id))
            all_chunks = tuple(self.job_store.get_chunks(doc_id=parent_doc_id, tenant_id=parent_job.tenant_id))
            index_manifest = self._derive_index_manifest_from_snapshot(
                job=parent_job,
                blocks=all_blocks,
                chunks=all_chunks,
            )
        replace_index = getattr(self.index, "replace_chunks_by_prefix", None)
        if callable(replace_index):
            for prefix, _blocks, chunks in replaced:
                replace_index(
                    doc_id=parent_doc_id,
                    chunks=chunks,
                    chunk_id_prefix=prefix,
                    tenant_id=parent_job.tenant_id,
                    document=None,
                    index_manifest=index_manifest,
                )
        else:
            self.index.upsert(
                doc_id=parent_doc_id,
                chunks=all_chunks,
                tenant_id=parent_job.tenant_id,
                document=None,
                index_manifest=index_manifest,
            )
        return True, views_updated_all

    def _incremental_partition_index_manifest(
        self,
        *,
        parent_job: Any,
        child_jobs: Sequence[Any],
        changed_artifacts: Sequence[tuple[str, Sequence[Any], Sequence[Chunk]]],
    ) -> dict[str, Any] | None:
        """Update the parent index manifest without rereading all merged artifacts.

        Prefix replacement already writes only the changed part's blocks/chunks.
        Rebuilding a manifest from the full parent snapshot after every part made
        large PDF execution quadratic in the number of completed parts.  The
        manifest contains per-part counts and chunk IDs, so those values can be
        updated from the changed part while retaining the prior manifest.
        """

        describe_document = getattr(self.index, "describe_document", None)
        if not callable(describe_document):
            return None
        try:
            existing = describe_document(doc_id=parent_job.doc_id, tenant_id=parent_job.tenant_id)
        except Exception:
            return None
        if isinstance(existing, Mapping):
            manifest = deepcopy(dict(existing))
        else:
            manifest = self._derive_index_manifest_from_snapshot(
                job=parent_job,
                blocks=(),
                chunks=(),
            )

        base_part_index = self._partition_index_manifest(
            parent_job=parent_job,
            blocks=(),
            chunks=(),
        )
        existing_part_index = manifest.get("part_index") if isinstance(manifest, Mapping) else None
        parts_by_id = {
            str(part.get("part_id") or ""): dict(part)
            for part in (existing_part_index.get("parts", []) if isinstance(existing_part_index, Mapping) else ())
            if isinstance(part, Mapping) and str(part.get("part_id") or "")
        }
        for part in base_part_index.get("parts", []):
            part_id = str(part.get("part_id") or "")
            if part_id:
                if part_id not in parts_by_id:
                    parts_by_id[part_id] = dict(part)
                else:
                    # A rerun creates a replacement child job. Refresh the
                    # identity/state fields while retaining prior counts until
                    # the changed part artifacts are written below.
                    for key in (
                        "part_doc_id",
                        "job_id",
                        "state",
                        "page_range",
                        "page_start",
                        "page_end",
                        "chunk_id_prefix",
                        "index_version",
                    ):
                        if key in part:
                            parts_by_id[part_id][key] = part[key]
        for prefix, blocks, chunks in changed_artifacts:
            prefix_text = str(prefix)
            prefix_marker = f"{parent_job.doc_id}:merged:"
            part_id = (
                prefix_text[len(prefix_marker) : -1]
                if prefix_text.startswith(prefix_marker) and prefix_text.endswith(":")
                else ""
            )
            # The prefix is parent:merged:<part_id>:; prefer block metadata
            # when a custom adapter uses a different delimiter.
            if blocks:
                metadata_part_id = str((getattr(blocks[0], "metadata", {}) or {}).get("part_id") or "")
                if metadata_part_id:
                    part_id = metadata_part_id
            part = parts_by_id.get(part_id)
            if part is None:
                continue
            part["chunk_ids"] = [str(chunk.chunk_id) for chunk in chunks if str(chunk.chunk_id)]
            part["chunk_count"] = len(chunks)
            part["block_count"] = len(blocks)
            parts_by_id[part_id] = part

        parts = [
            parts_by_id[str(part.get("part_id") or "")]
            for part in base_part_index.get("parts", [])
            if str(part.get("part_id") or "") in parts_by_id
        ]
        manifest["doc_id"] = parent_job.doc_id
        manifest["tenant_id"] = parent_job.tenant_id
        manifest["index_version"] = parent_job.job_id
        manifest["part_index"] = {
            "strategy": "pdf_part",
            "parent_job_id": parent_job.job_id,
            "source_doc_id": parent_job.doc_id,
            "part_count": len(parts),
            "indexed_part_count": len([part for part in parts if int(part.get("chunk_count") or 0) > 0]),
            "parts": parts,
        }

        layers = [dict(layer) for layer in (manifest.get("layers") or ()) if isinstance(layer, Mapping)]
        changed_blocks = tuple(block for _prefix, blocks, _chunks in changed_artifacts for block in blocks)
        changed_chunks = tuple(chunk for _prefix, _blocks, chunks in changed_artifacts for chunk in chunks)
        for layer in layers:
            name = str(layer.get("name") or "")
            if name == "primary":
                layer["item_count"] = sum(int(part.get("chunk_count") or 0) for part in parts)
                layer["semantic_roles"] = sorted(
                    {
                        *[str(role) for role in tuple(layer.get("semantic_roles") or ()) if str(role)],
                        *[str(chunk.semantic_role or "paragraph") for chunk in changed_chunks],
                    }
                )
            elif name == "structure":
                layer["item_count"] = sum(int(part.get("block_count") or 0) for part in parts)
                layer["semantic_roles"] = sorted(
                    {
                        *[str(role) for role in tuple(layer.get("semantic_roles") or ()) if str(role)],
                        *[
                            str((getattr(block, "metadata", {}) or {}).get("semantic_role") or "paragraph")
                            for block in changed_blocks
                        ],
                    }
                )
            elif name == "high_precision":
                high_precision_ids = [str(chunk_id) for chunk_id in tuple(layer.get("chunk_ids") or ())]
                for prefix, _blocks, chunks in changed_artifacts:
                    high_precision_ids = [
                        chunk_id for chunk_id in high_precision_ids if not chunk_id.startswith(str(prefix))
                    ]
                    high_precision_ids.extend(
                        str(chunk.chunk_id) for chunk in _select_high_precision_chunks(chunks)
                    )
                layer["chunk_ids"] = high_precision_ids
                layer["item_count"] = len(high_precision_ids)
        manifest["layers"] = layers
        return manifest

    def _start_pdf_part_job(
        self,
        *,
        source_job: Any,
        parent_job: Any,
        spec: dict[str, Any],
        profile: str,
        max_active_parts_per_doc: int | None = None,
        rerun: bool = False,
        create_part_file: bool = True,
        provider_route_request: Mapping[str, Any] | None = None,
        previous_part_observation: Mapping[str, Any] | None = None,
    ):
        part_id = str(spec.get("part_id") or child_doc_id(parent_job.doc_id, spec.get("part_index") or 1))
        part_doc_id = str(spec.get("part_doc_id") or part_id)
        page_start = _safe_int_runtime(spec.get("page_start"), default=1)
        page_end = _safe_int_runtime(spec.get("page_end"), default=page_start)
        part_file = self._pdf_part_file_path(
            source_path=source_job.file_path,
            parent_doc_id=parent_job.doc_id,
            parent_job_id=parent_job.job_id,
            part_id=part_id,
        )
        if create_part_file and not part_file.exists():
            ensure_private_directory(
                part_file.parent,
                allowed_root=self._pdf_parts_root(source_path=source_job.file_path),
            )
            create_pdf_part_file(source_job.file_path, str(part_file), page_start, page_end)
        options = dict(source_job.options)
        options.pop(_LOCAL_PROVIDER_ROUTING_OPTION, None)
        options.pop(_LOCAL_PROVIDER_ROUTE_REQUEST_OPTION, None)
        options.update(
            {
                "job_kind": _PDF_PART_JOB_KIND,
                "partitioned": True,
                "source_doc_id": parent_job.doc_id,
                "parent_doc_id": parent_job.doc_id,
                "parent_job_id": parent_job.job_id,
                "source_job_id": source_job.job_id,
                "part_id": part_id,
                "part_doc_id": part_doc_id,
                "part_index": _safe_int_runtime(spec.get("part_index"), default=1),
                "page_start": page_start,
                "page_end": page_end,
                "page_count": max(1, page_end - page_start + 1),
                "page_offset": page_start - 1,
                "max_active_parts_per_doc": max_active_parts_per_doc,
                "profile": profile,
                "rerun": bool(rerun),
            }
        )
        if provider_route_request:
            options[_LOCAL_PROVIDER_ROUTE_REQUEST_OPTION] = dict(provider_route_request)
        if previous_part_observation:
            options[_PART_RERUN_PREVIOUS_OBSERVATION_OPTION] = _runtime_json_safe_mapping(previous_part_observation)
        part_job = self.start(
            ParseRequest(
                doc_id=part_doc_id,
                file_path=str(part_file),
                media_type=source_job.media_type,
                options=options,
                tenant_id=source_job.tenant_id,
                quota_key=source_job.quota_key,
                quota_units=source_job.quota_units,
            )
        )
        self._remember_pdf_part_job(part_job)
        return part_job

    def _ensure_pdf_part_file(self, request: ParseRequest) -> None:
        """Materialize a deferred PDF part immediately before execution."""
        target = Path(request.file_path)
        if target.exists():
            return
        options = request.options if isinstance(request.options, Mapping) else {}
        source_job_id = str(options.get("source_job_id") or "").strip()
        source_job = self.job_store.get_job(job_id=source_job_id) if source_job_id else None
        source_path = str(getattr(source_job, "file_path", "") or "").strip()
        page_start = _safe_int_runtime(options.get("page_start"), default=0)
        page_end = _safe_int_runtime(options.get("page_end"), default=0)
        if not source_path or page_start <= 0 or page_end < page_start:
            return
        ensure_private_directory(
            target.parent,
            allowed_root=self._pdf_parts_root(source_path=source_path),
        )
        create_pdf_part_file(source_path, str(target), page_start, page_end)

    def _pdf_parts_root(self, *, source_path: str) -> Path:
        return Path(source_path).parent / "_parsecore_parts"

    def _pdf_part_file_path(
        self,
        *,
        source_path: str,
        parent_doc_id: str,
        parent_job_id: str,
        part_id: str,
    ) -> Path:
        return self._pdf_parts_root(source_path=source_path) / _safe_path_segment(parent_doc_id) / parent_job_id / f"{_safe_path_segment(part_id)}.pdf"

    def _latest_partition_parent_job(self, *, doc_id: str, tenant_id: str | None = None):
        for job in self.list_jobs(doc_id=doc_id, tenant_id=tenant_id):
            if _job_kind(job) == _PDF_PARENT_JOB_KIND:
                return job
        return None

    def _resolve_latest_non_partition_job(self, *, doc_id: str, tenant_id: str | None = None):
        for job in self.list_jobs(doc_id=doc_id, tenant_id=tenant_id):
            if _job_kind(job) != _PDF_PARENT_JOB_KIND:
                return job
        return None

    def _latest_pdf_part_jobs(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        parent_job_id: str | None,
        refresh: bool = False,
    ) -> tuple[Any, ...]:
        normalized_tenant = str(tenant_id or "default")
        cache_key = (str(doc_id), normalized_tenant, str(parent_job_id or ""))
        if self._pdf_part_cache_enabled() and parent_job_id and not refresh and cache_key in self._pdf_part_jobs_cache:
            cached = self._pdf_part_jobs_cache[cache_key]
            return tuple(
                sorted(
                    cached.values(),
                    key=lambda item: _safe_int_runtime(
                        (getattr(item, "options", {}) or {}).get("part_index"),
                        default=0,
                    ),
                )
            )
        latest_by_part: dict[str, Any] = {}
        for job in self.list_jobs(tenant_id=tenant_id):
            options = getattr(job, "options", {}) or {}
            if str(options.get("job_kind") or "") != _PDF_PART_JOB_KIND:
                continue
            if str(options.get("source_doc_id") or options.get("parent_doc_id") or "") != doc_id:
                continue
            if parent_job_id and str(options.get("parent_job_id") or "") != parent_job_id:
                continue
            part_id = str(options.get("part_id") or job.doc_id)
            if part_id and part_id not in latest_by_part:
                latest_by_part[part_id] = job
        if self._pdf_part_cache_enabled() and parent_job_id:
            self._pdf_part_jobs_cache[cache_key] = latest_by_part
        return tuple(
            sorted(
                latest_by_part.values(),
                key=lambda item: _safe_int_runtime((getattr(item, "options", {}) or {}).get("part_index"), default=0),
            )
        )

    def _remember_pdf_part_job(self, job: Any) -> None:
        if not self._pdf_part_cache_enabled():
            return
        options = getattr(job, "options", {}) or {}
        if str(options.get("job_kind") or "") != _PDF_PART_JOB_KIND:
            return
        source_doc_id = str(options.get("source_doc_id") or options.get("parent_doc_id") or "")
        parent_job_id = str(options.get("parent_job_id") or "")
        part_id = str(options.get("part_id") or getattr(job, "doc_id", "") or "")
        if not source_doc_id or not parent_job_id or not part_id:
            return
        tenant_id = str(getattr(job, "tenant_id", None) or "default")
        cache_key = (source_doc_id, tenant_id, parent_job_id)
        self._pdf_part_jobs_cache.setdefault(cache_key, {})[part_id] = job

    def _current_part_observation(
        self,
        *,
        doc_id: str,
        part_id: str,
        tenant_id: str | None,
    ) -> dict[str, Any] | None:
        try:
            from .api_payloads import _document_projection
            from .parts import document_parts_projection
        except Exception:
            return None
        snapshot = self.get_document(doc_id=doc_id, tenant_id=tenant_id)
        if snapshot.get("job") is None:
            return None
        try:
            structured = _document_projection(snapshot, projection="structured")
            parts_payload = document_parts_projection(structured)
        except Exception:
            return None
        for part in parts_payload.get("parts", []):
            if not isinstance(part, Mapping):
                continue
            if str(part.get("part_id") or "") != str(part_id):
                continue
            return _runtime_part_observation_payload(part)
        return None

    def _merged_partition_artifacts(
        self,
        *,
        parent_doc_id: str,
        tenant_id: str,
        parent_job_id: str,
        part_ids: Sequence[str] | None = None,
    ) -> tuple[tuple[Any, ...], tuple[Chunk, ...]]:
        part_filter = {
            str(part_id).strip()
            for part_id in tuple(part_ids or ())
            if str(part_id).strip()
        }
        child_jobs = self._latest_pdf_part_jobs(
            doc_id=parent_doc_id,
            tenant_id=tenant_id,
            parent_job_id=parent_job_id,
        )
        merged_blocks: list[Any] = []
        merged_chunks: list[Chunk] = []
        block_id_map: dict[str, str] = {}
        for job in child_jobs:
            if job.state != ParseJobState.DONE:
                continue
            part_id = str((job.options or {}).get("part_id") or job.doc_id)
            if part_filter and part_id not in part_filter:
                continue
            for block in self.job_store.get_blocks(doc_id=job.doc_id, tenant_id=job.tenant_id):
                merged_id = f"{parent_doc_id}:merged:{getattr(block, 'block_id', len(merged_blocks) + 1)}"
                block_id_map[str(getattr(block, "block_id", ""))] = merged_id
                metadata = dict(getattr(block, "metadata", {}) or {})
                metadata.setdefault("part_doc_id", job.doc_id)
                metadata.setdefault("part_id", part_id)
                merged_blocks.append(
                    replace(
                        block,
                        block_id=merged_id,
                        doc_id=parent_doc_id,
                        metadata=metadata,
                    )
                )
            for chunk in self.job_store.get_chunks(doc_id=job.doc_id, tenant_id=job.tenant_id):
                merged_chunk_id = f"{parent_doc_id}:merged:{chunk.chunk_id}"
                merged_block_ids = tuple(block_id_map.get(str(block_id), str(block_id)) for block_id in chunk.block_ids)
                merged_chunks.append(
                    replace(
                        chunk,
                        chunk_id=merged_chunk_id,
                        doc_id=parent_doc_id,
                        block_ids=merged_block_ids,
                    )
                )
        return tuple(merged_blocks), tuple(merged_chunks)

    def _normalize_pdf_part_blocks(self, *, request: ParseRequest, blocks: Sequence[Any]) -> tuple[Any, ...]:
        options = request.options or {}
        part_id = str(options.get("part_id") or request.doc_id)
        page_offset = _safe_int_runtime(options.get("page_offset"), default=0)
        normalized = []
        for index, block in enumerate(blocks, start=1):
            old_id = str(getattr(block, "block_id", f"blk-{index}"))
            metadata = dict(getattr(block, "metadata", {}) or {})
            if metadata.get("page") is not None:
                metadata["page"] = _safe_int_runtime(metadata.get("page"), default=1) + page_offset
            metadata.update(
                {
                    "part_id": part_id,
                    "part_doc_id": request.doc_id,
                    "source_doc_id": str(options.get("source_doc_id") or ""),
                    "parent_job_id": str(options.get("parent_job_id") or ""),
                    "page_start": _safe_int_runtime(options.get("page_start"), default=1),
                    "page_end": _safe_int_runtime(options.get("page_end"), default=1),
                }
            )
            normalized.append(
                replace(
                    block,
                    block_id=f"{part_id}:{old_id}",
                    doc_id=request.doc_id,
                    metadata=metadata,
                )
            )
        return tuple(normalized)

    def _normalize_pdf_part_chunks(self, *, request: ParseRequest, chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
        part_id = str((request.options or {}).get("part_id") or request.doc_id)
        normalized = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_id = str(chunk.chunk_id or f"chk-{index}")
            normalized.append(
                replace(
                    chunk,
                    chunk_id=f"{part_id}:{chunk_id}",
                    doc_id=request.doc_id,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _partition_parent_state(child_jobs: Sequence[Any]) -> ParseJobState:
        if not child_jobs:
            return ParseJobState.PARTIAL
        states = [job.state for job in child_jobs]
        if all(state == ParseJobState.DONE for state in states):
            return ParseJobState.DONE
        if all(state == ParseJobState.FAILED for state in states):
            return ParseJobState.FAILED
        return ParseJobState.PARTIAL

    def _derive_index_manifest_from_snapshot(
        self,
        *,
        job: Any,
        blocks: Sequence[Any],
        chunks: Sequence[Chunk],
    ) -> dict[str, Any]:
        semantic_roles = sorted(
            {
                str((getattr(block, "metadata", {}) or {}).get("semantic_role") or "paragraph")
                for block in blocks
            }
        )
        chunk_roles = sorted({str(chunk.semantic_role or "paragraph") for chunk in chunks})
        options_hash = ""
        pipeline_name = ""
        if self.pipeline_registry is not None:
            try:
                pipeline = self.pipeline_registry.resolve(self._request_from_job(job), purpose="parse")
                pipeline_name = str(getattr(pipeline, "name", "") or "")
            except Exception:
                pipeline_name = ""
        embedding_tiers = _resolve_embedding_tiers(getattr(job, "options", {}) or {})
        high_precision_chunks = _select_high_precision_chunks(chunks)
        layers: list[dict[str, Any]] = [
            {
                "name": "primary",
                "kind": "chunk",
                "version": job.job_id,
                "item_count": len(chunks),
                "semantic_roles": chunk_roles,
                "embedding_tier": embedding_tiers[0],
            },
            {
                "name": "structure",
                "kind": "typed-item",
                "version": job.job_id,
                "item_count": len(blocks),
                "semantic_roles": semantic_roles,
            },
        ]
        if "large" in embedding_tiers:
            layers.append(
                {
                    "name": "high_precision",
                    "kind": "chunk",
                    "version": job.job_id,
                    "item_count": len(high_precision_chunks),
                    "semantic_roles": sorted({str(chunk.semantic_role or "paragraph") for chunk in high_precision_chunks}),
                    "embedding_tier": "large",
                    "chunk_ids": [chunk.chunk_id for chunk in high_precision_chunks],
                }
            )
        manifest = {
            "doc_id": job.doc_id,
            "tenant_id": job.tenant_id,
            "pipeline_name": pipeline_name,
            "options_hash": options_hash,
            "index_version": job.job_id,
            "embedding_tiers": embedding_tiers,
            "layers": layers,
        }
        if _job_kind(job) == _PDF_PARENT_JOB_KIND:
            part_index = self._partition_index_manifest(parent_job=job, blocks=blocks, chunks=chunks)
            if part_index:
                manifest["part_index"] = part_index
        return manifest

    def _partition_index_manifest(
        self,
        *,
        parent_job: Any,
        blocks: Sequence[Any],
        chunks: Sequence[Chunk],
    ) -> dict[str, Any]:
        child_jobs = self._latest_pdf_part_jobs(
            doc_id=parent_job.doc_id,
            tenant_id=parent_job.tenant_id,
            parent_job_id=parent_job.job_id,
        )
        if not child_jobs:
            return {}
        blocks_by_part: dict[str, int] = {}
        for block in blocks:
            metadata = getattr(block, "metadata", {}) or {}
            part_id = str(metadata.get("part_id") or "").strip()
            if not part_id:
                block_id = str(getattr(block, "block_id", "") or "")
                part_id = _part_id_from_merged_id(parent_doc_id=parent_job.doc_id, item_id=block_id)
            if part_id:
                blocks_by_part[part_id] = blocks_by_part.get(part_id, 0) + 1

        chunk_ids_by_part: dict[str, list[str]] = {}
        for chunk in chunks:
            chunk_id = str(chunk.chunk_id or "")
            part_id = _part_id_from_merged_id(parent_doc_id=parent_job.doc_id, item_id=chunk_id)
            if not part_id:
                for block_id in tuple(chunk.block_ids or ()):
                    part_id = _part_id_from_merged_id(parent_doc_id=parent_job.doc_id, item_id=str(block_id))
                    if part_id:
                        break
            if part_id:
                chunk_ids_by_part.setdefault(part_id, []).append(chunk_id)

        parts: list[dict[str, Any]] = []
        for child in child_jobs:
            options = getattr(child, "options", {}) or {}
            part_id = str(options.get("part_id") or child.doc_id).strip()
            if not part_id:
                continue
            state = _job_state(child)
            page_start = _safe_int_runtime(options.get("page_start"), default=1)
            page_end = _safe_int_runtime(options.get("page_end"), default=page_start)
            chunk_ids = chunk_ids_by_part.get(part_id, [])
            parts.append(
                {
                    "part_id": part_id,
                    "part_doc_id": child.doc_id,
                    "job_id": child.job_id,
                    "state": state.value if state is not None else str(getattr(child, "state", "") or ""),
                    "page_range": {
                        "start": page_start,
                        "end": page_end,
                    },
                    "page_start": page_start,
                    "page_end": page_end,
                    "chunk_id_prefix": _merged_part_prefix(parent_doc_id=parent_job.doc_id, part_id=part_id),
                    "chunk_ids": chunk_ids,
                    "chunk_count": len(chunk_ids),
                    "block_count": blocks_by_part.get(part_id, 0),
                    "index_version": child.job_id,
                }
            )
        return {
            "strategy": "pdf_part",
            "parent_job_id": parent_job.job_id,
            "source_doc_id": parent_job.doc_id,
            "part_count": len(parts),
            "indexed_part_count": len([part for part in parts if int(part.get("chunk_count") or 0) > 0]),
            "parts": parts,
        }

    def search_document(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[ChunkSearchHit, ...]:
        hits, _mode = self.search_document_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=semantic_roles,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        tenant_id: str | None = None,
        index_layer: str = "primary",
    ) -> tuple[tuple[ChunkSearchHit, ...], str]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            return (), "keyword-fallback"
        requested_layer = _normalize_chunk_index_layer(index_layer)
        chunks = self._load_chunks_for_index_layer(
            doc_id=doc_id,
            tenant_id=latest_job.tenant_id,
            layer=requested_layer,
        )
        if not chunks:
            return (), "keyword-fallback"

        query_embedding = self._try_embed_search_query(
            query=query,
            doc_id=doc_id,
            tenant_id=latest_job.tenant_id,
            quota_key=latest_job.quota_key,
        )
        retrieval_mode = _resolve_retrieval_mode(
            query_embedding=query_embedding,
            chunks=chunks,
        )
        allowed_roles = {
            str(role).strip().lower()
            for role in (semantic_roles or ())
            if str(role).strip()
        }
        scored: list[ChunkSearchHit] = []
        for chunk in chunks:
            role = str(chunk.semantic_role or "paragraph").strip().lower()
            if allowed_roles and role not in allowed_roles:
                continue
            score = _score_chunk(
                query=query,
                query_embedding=query_embedding,
                chunk=chunk,
            )
            if score <= 0:
                continue
            scored.append(
                ChunkSearchHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    block_ids=chunk.block_ids,
                    text=chunk.text,
                    semantic_role=role,
                    score=round(score, 4),
                    retrieval_score=round(score, 4),
                )
            )

        ranked = sorted(scored, key=lambda item: (-item.score, item.chunk_id))
        result_limit = max(1, int(limit))
        candidates = tuple(ranked[: self._rerank_candidate_limit(result_limit)])
        result, rerank_applied = self._rerank_candidates(
            doc_id=doc_id,
            tenant_id=latest_job.tenant_id,
            quota_key=latest_job.quota_key,
            query=query,
            candidates=candidates,
            limit=result_limit,
        )
        if rerank_applied:
            retrieval_mode = f"{retrieval_mode}+rerank"
        self._record_search_effectiveness(
            tenant_id=latest_job.tenant_id,
            layer=requested_layer,
            hit_count=len(result),
        )
        return result, retrieval_mode

    def _rerank_candidate_limit(self, result_limit: int) -> int:
        settings = self.settings.providers.rerank
        if not settings.enabled:
            return result_limit
        return max(result_limit, int(settings.candidate_limit))

    def _rerank_candidates(
        self,
        *,
        doc_id: str,
        tenant_id: str,
        quota_key: str,
        query: str,
        candidates: Sequence[ChunkSearchHit],
        limit: int,
    ) -> tuple[tuple[ChunkSearchHit, ...], bool]:
        settings = self.settings.providers.rerank
        fallback = tuple(candidates[:limit])
        if not settings.enabled or self.rerank_provider is None or not candidates:
            return fallback, False

        try:
            raw_scores = self.rerank_provider.rerank(
                query=query,
                documents=[candidate.text for candidate in candidates],
            )
        except Exception as exc:
            failure_category = classify_provider_failure(exc)
            self.event_logger.log(
                "rerank_skipped",
                doc_id=doc_id,
                provider=settings.provider,
                model=settings.model,
                error_type=type(exc).__name__,
                failure_category=failure_category,
            )
            self.event_aggregator.record_event(
                "provider_failure",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=doc_id,
                details={
                    "provider_type": "rerank",
                    "provider_id": settings.provider,
                    "model": settings.model,
                    "failure_category": failure_category,
                    "operation": "rerank",
                },
            )
            return fallback, False

        scores_by_index: dict[int, float] = {}
        for item in raw_scores:
            raw_index = getattr(item, "index", None)
            raw_score = getattr(item, "score", None)
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            index = raw_index
            if index < 0 or index >= len(candidates) or index in scores_by_index:
                continue
            if isfinite(score):
                scores_by_index[index] = score
        if not scores_by_index:
            self.event_logger.log(
                "rerank_skipped",
                doc_id=doc_id,
                provider=settings.provider,
                model=settings.model,
                error_type="empty_or_invalid_response",
                failure_category="invalid_response",
            )
            self.event_aggregator.record_event(
                "provider_failure",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=doc_id,
                details={
                    "provider_type": "rerank",
                    "provider_id": settings.provider,
                    "model": settings.model,
                    "failure_category": "invalid_response",
                    "operation": "rerank",
                },
            )
            return fallback, False

        ranked_pairs = sorted(
            enumerate(candidates),
            key=lambda pair: (
                pair[0] not in scores_by_index,
                -scores_by_index[pair[0]] if pair[0] in scores_by_index else 0.0,
                -(pair[1].retrieval_score if pair[1].retrieval_score is not None else pair[1].score),
                pair[1].chunk_id,
            ),
        )
        reranked: list[ChunkSearchHit] = []
        for index, candidate in ranked_pairs:
            rerank_score = scores_by_index.get(index)
            if rerank_score is None:
                reranked.append(candidate)
                continue
            reranked.append(
                replace(
                    candidate,
                    score=round(rerank_score, 4),
                    retrieval_score=(
                        candidate.retrieval_score
                        if candidate.retrieval_score is not None
                        else candidate.score
                    ),
                    rerank_score=round(rerank_score, 4),
                )
            )
        self.event_logger.log(
            "rerank_applied",
            doc_id=doc_id,
            provider=settings.provider,
            model=settings.model,
            candidate_count=len(candidates),
            scored_candidate_count=len(scores_by_index),
        )
        return tuple(reranked[:limit]), True

    def _record_search_effectiveness(self, *, tenant_id: str | None, layer: str, hit_count: int) -> None:
        normalized_tenant = str(tenant_id or "default").strip() or "default"
        normalized_layer = _normalize_chunk_index_layer(layer)
        key = (normalized_tenant, normalized_layer)
        history = self._search_layer_query_hits.setdefault(key, [])
        history.append(max(0, int(hit_count)))
        overflow = len(history) - _SEARCH_METRICS_HISTORY_LIMIT
        if overflow > 0:
            del history[:overflow]
        record_layer_search_hit = getattr(self.job_store, "record_layer_search_hit", None)
        if callable(record_layer_search_hit):
            record_layer_search_hit(
                tenant_id=normalized_tenant,
                layer=normalized_layer,
                hit_count=max(0, int(hit_count)),
            )

    def _load_chunks_for_index_layer(
        self,
        *,
        doc_id: str,
        tenant_id: str | None,
        layer: str,
    ) -> tuple[Chunk, ...]:
        chunks = tuple(self.job_store.get_chunks(doc_id=doc_id, tenant_id=tenant_id))
        normalized_layer = _normalize_chunk_index_layer(layer)
        if normalized_layer == "primary":
            return chunks

        get_layer_chunks = getattr(self.index, "get_layer_chunks", None)
        if callable(get_layer_chunks):
            indexed_chunks = get_layer_chunks(doc_id=doc_id, layer=normalized_layer, tenant_id=tenant_id)
            if indexed_chunks is not None:
                return tuple(indexed_chunks)

        return _select_high_precision_chunks(chunks)

    def search_document_structure(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        structure_tags: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[StructureSearchHit, ...]:
        hits, _mode = self.search_document_structure_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=semantic_roles,
            structure_tags=structure_tags,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_structure_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        semantic_roles: Sequence[str] | None = None,
        structure_tags: Sequence[str] | None = None,
        tenant_id: str | None = None,
    ) -> tuple[tuple[StructureSearchHit, ...], str]:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            return (), "structure-keyword"
        blocks = tuple(self.job_store.get_blocks(doc_id=doc_id, tenant_id=latest_job.tenant_id))
        if not blocks:
            return (), "structure-keyword"
        entries = self._structure_entries_from_blocks(doc_id=doc_id, blocks=blocks)
        allowed_roles = {
            str(role).strip().lower() for role in (semantic_roles or ()) if str(role).strip()
        }
        allowed_tags = {
            str(tag).strip().lower() for tag in (structure_tags or ()) if str(tag).strip()
        }
        scored: list[StructureSearchHit] = []
        for entry in entries:
            role = str(entry["semantic_role"] or "paragraph").strip().lower()
            tags = tuple(str(tag).strip().lower() for tag in entry["structure_tags"])
            if allowed_roles and role not in allowed_roles:
                continue
            if allowed_tags and not any(tag in allowed_tags for tag in tags):
                continue
            score = _score_structure_entry(query=query, text=str(entry["text"]), semantic_role=role, tags=tags)
            if score <= 0.0:
                continue
            scored.append(
                StructureSearchHit(
                    item_id=str(entry["item_id"]),
                    doc_id=doc_id,
                    block_ids=(str(entry["block_id"]),),
                    text=str(entry["text"]),
                    semantic_role=role,
                    structure_tags=tuple(tags),
                    page_number=entry["page_number"],
                    score=round(score, 4),
                )
            )
        ranked = sorted(scored, key=lambda item: (-item.score, item.item_id))
        return tuple(ranked[: max(1, int(limit))]), "structure-keyword"

    def search_document_tasks(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> tuple[StructureSearchHit, ...]:
        hits, _mode = self.search_document_tasks_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
        )
        return hits

    def search_document_tasks_with_mode(
        self,
        *,
        doc_id: str,
        query: str,
        limit: int = 10,
        tenant_id: str | None = None,
    ) -> tuple[tuple[StructureSearchHit, ...], str]:
        hits, mode = self.search_document_structure_with_mode(
            doc_id=doc_id,
            query=query,
            limit=max(limit * 3, 10),
            semantic_roles=("paragraph", "body_section", "warning", "caution", "note"),
            tenant_id=tenant_id,
        )
        task_hits = tuple(
            hit for hit in hits if _is_task_like_entry(hit.text, hit.semantic_role, hit.structure_tags)
        )
        return task_hits[: max(1, int(limit))], mode

    def _try_embed_search_query(
        self,
        *,
        query: str,
        doc_id: str,
        tenant_id: str,
        quota_key: str,
    ) -> tuple[float, ...] | None:
        if not query.strip():
            return None
        probe = Chunk(
            chunk_id="query-probe",
            doc_id="search-query",
            block_ids=(),
            text=query,
        )
        try:
            embedded = tuple(self.embedding_provider.embed(doc_id="search-query", chunks=[probe]))
        except Exception as exc:
            settings = self.settings.providers.embedding
            failure_category = classify_provider_failure(exc)
            self.event_logger.log(
                "search_embedding_skipped",
                doc_id=doc_id,
                provider=settings.provider,
                model=settings.model,
                error_type=type(exc).__name__,
                failure_category=failure_category,
            )
            self.event_aggregator.record_event(
                "provider_failure",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=doc_id,
                details={
                    "provider_type": "embedding",
                    "provider_id": settings.provider,
                    "model": settings.model,
                    "failure_category": failure_category,
                    "operation": "embed_query",
                },
            )
            return None
        if not embedded or embedded[0].embedding is None:
            settings = self.settings.providers.embedding
            self.event_logger.log(
                "search_embedding_skipped",
                doc_id=doc_id,
                provider=settings.provider,
                model=settings.model,
                error_type="empty_or_invalid_response",
                failure_category="invalid_response",
            )
            self.event_aggregator.record_event(
                "provider_failure",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=doc_id,
                details={
                    "provider_type": "embedding",
                    "provider_id": settings.provider,
                    "model": settings.model,
                    "failure_category": "invalid_response",
                    "operation": "embed_query",
                },
            )
            return None
        return tuple(float(item) for item in embedded[0].embedding)

    def retry_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseOutcome:
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.submit(self._request_from_job(latest_job))

    def restart_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.start(self._request_from_job(latest_job))

    def rechunk_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_CHUNKS_ONLY_MODE
        return self.start(request)

    def reembed_latest(self, *, doc_id: str, tenant_id: str | None = None):
        latest_job = self._resolve_latest_job(doc_id=doc_id, tenant_id=tenant_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        request = self._request_from_job(latest_job)
        request.options["mode"] = _RERUN_EMBEDDINGS_ONLY_MODE
        return self.start(request)

    def list_jobs(
        self,
        *,
        doc_id: str | None = None,
        tenant_id: str | None = None,
        quota_key: str | None = None,
        since_hours: float | None = None,
    ):
        jobs = tuple(self.job_store.list_jobs(doc_id=doc_id))
        normalized_tenant = (tenant_id or "").strip()
        normalized_quota = (quota_key or "").strip()
        if normalized_tenant:
            jobs = tuple(job for job in jobs if (job.tenant_id or "default") == normalized_tenant)
        if normalized_quota:
            jobs = tuple(job for job in jobs if (job.quota_key or "default") == normalized_quota)
        return _filter_jobs_by_since_hours(jobs, since_hours=since_hours)

    def quota_usage(self, *, tenant_id: str | None = None, since_hours: float | None = None) -> dict[str, Any]:
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        by_bucket: dict[tuple[str, str], dict[str, Any]] = {}
        for job in jobs:
            t_id = job.tenant_id or "default"
            q_key = job.quota_key or "default"
            units = max(1, int(job.quota_units or 1))
            key = (t_id, q_key)
            bucket = by_bucket.get(key)
            if bucket is None:
                bucket = {
                    "tenant_id": t_id,
                    "quota_key": q_key,
                    "job_count": 0,
                    "total_quota_units": 0,
                    "done_jobs": 0,
                    "failed_jobs": 0,
                }
                by_bucket[key] = bucket
            bucket["job_count"] += 1
            bucket["total_quota_units"] += units
            if job.state == ParseJobState.DONE:
                bucket["done_jobs"] += 1
            if job.state == ParseJobState.FAILED:
                bucket["failed_jobs"] += 1

        items = sorted(
            by_bucket.values(),
            key=lambda item: (str(item["tenant_id"]), str(item["quota_key"])),
        )
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "total_jobs": len(jobs),
            "total_quota_units": sum(int(item["total_quota_units"]) for item in items),
            "items": items,
        }

    def index_metrics(
        self,
        *,
        tenant_id: str | None = None,
        since_hours: float | None = None,
        trend_windows_hours: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        normalized_tenant_filter = (tenant_id or "").strip() or None
        manifests: list[dict[str, Any]] = []
        seen_docs: set[tuple[str, str]] = set()
        for job in jobs:
            key = (job.tenant_id or "default", job.doc_id)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            snapshot = self.get_document(doc_id=job.doc_id, tenant_id=job.tenant_id)
            manifest = snapshot.get("index_manifest")
            if isinstance(manifest, dict):
                manifests.append(manifest)
        layer_counts: dict[str, int] = {}
        layer_items: dict[str, int] = {}
        semantic_roles: dict[str, int] = {}
        versions: dict[str, int] = {}
        for manifest in manifests:
            versions[str(manifest.get("index_version") or "")] = versions.get(str(manifest.get("index_version") or ""), 0) + 1
            for layer in manifest.get("layers", []):
                name = str(layer.get("name") or "unknown")
                layer_counts[name] = layer_counts.get(name, 0) + 1
                layer_items[name] = layer_items.get(name, 0) + int(layer.get("item_count") or 0)
                for role in layer.get("semantic_roles", []):
                    role_name = str(role or "paragraph")
                    semantic_roles[role_name] = semantic_roles.get(role_name, 0) + 1

        document_total = len(manifests)
        high_precision_docs = int(layer_counts.get("high_precision", 0))
        high_precision_items = int(layer_items.get("high_precision", 0))
        primary_items = int(layer_items.get("primary", 0))
        document_coverage = 0.0
        if document_total > 0:
            document_coverage = round(high_precision_docs / document_total, 4)
        item_ratio_vs_primary = 0.0
        if primary_items > 0:
            item_ratio_vs_primary = round(high_precision_items / primary_items, 4)

        search_effectiveness = self._build_search_effectiveness_snapshot(
            tenant_id=normalized_tenant_filter,
            since_hours=since_hours,
        )
        normalized_trend_windows = _normalize_trend_windows_hours(trend_windows_hours)
        search_effectiveness_trends: dict[str, dict[str, dict[str, Any]]] = {}
        for window_hours in normalized_trend_windows:
            window_label = _format_trend_window_label(window_hours)
            search_effectiveness_trends[window_label] = self._build_search_effectiveness_snapshot(
                tenant_id=normalized_tenant_filter,
                since_hours=window_hours,
            )

        high_precision_search = search_effectiveness.get("high_precision", {})
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "trend_windows_hours": list(normalized_trend_windows),
            "documents": document_total,
            "layer_counts": layer_counts,
            "layer_items": layer_items,
            "semantic_role_coverage": semantic_roles,
            "index_versions": {key: value for key, value in versions.items() if key},
            "search_effectiveness": search_effectiveness,
            "search_effectiveness_trends": search_effectiveness_trends,
            "high_precision": {
                "documents": high_precision_docs,
                "document_coverage": document_coverage,
                "items": high_precision_items,
                "item_ratio_vs_primary": item_ratio_vs_primary,
                "query_count": int(high_precision_search.get("queries") or 0),
                "query_hit_rate": float(high_precision_search.get("hit_rate") or 0.0),
                "query_avg_hits": float(high_precision_search.get("avg_hits") or 0.0),
            },
        }

    def _build_search_effectiveness_snapshot(
        self,
        *,
        tenant_id: str | None,
        since_hours: float | None,
    ) -> dict[str, dict[str, Any]]:
        aggregate_layer_search_metrics = getattr(self.job_store, "aggregate_layer_search_metrics", None)
        raw_metrics: Mapping[str, Mapping[str, int]] | None = None
        if callable(aggregate_layer_search_metrics):
            raw_metrics = aggregate_layer_search_metrics(tenant_id=tenant_id, since_hours=since_hours)
        if raw_metrics is None:
            raw_metrics = self._aggregate_search_effectiveness_from_memory(tenant_id=tenant_id)

        snapshot: dict[str, dict[str, Any]] = {}
        for layer, bucket in raw_metrics.items():
            queries = max(0, int(bucket.get("queries") or 0))
            if queries <= 0:
                continue
            hit_queries = max(0, int(bucket.get("hit_queries") or 0))
            total_hits = max(0, int(bucket.get("total_hits") or 0))
            max_hits = max(0, int(bucket.get("max_hits") or 0))
            snapshot[str(layer)] = {
                "queries": queries,
                "hit_queries": hit_queries,
                "zero_hit_queries": max(0, queries - hit_queries),
                "hit_rate": round(hit_queries / queries, 4),
                "avg_hits": round(total_hits / queries, 4),
                "max_hits": max_hits,
            }
        return snapshot

    def _aggregate_search_effectiveness_from_memory(
        self,
        *,
        tenant_id: str | None,
    ) -> dict[str, dict[str, int]]:
        tenant_filter = (tenant_id or "").strip()
        metrics: dict[str, dict[str, int]] = {}
        for (search_tenant_id, layer), history in self._search_layer_query_hits.items():
            if tenant_filter and search_tenant_id != tenant_filter:
                continue
            if not history:
                continue
            bucket = metrics.setdefault(layer, {"queries": 0, "hit_queries": 0, "total_hits": 0, "max_hits": 0})
            bucket["queries"] += len(history)
            bucket["hit_queries"] += sum(1 for count in history if count > 0)
            bucket["total_hits"] += sum(history)
            bucket["max_hits"] = max(bucket["max_hits"], max(history))
        return metrics

    def batch_reindex(
        self,
        *,
        tenant_id: str | None = None,
        doc_ids: Sequence[str] | None = None,
        since_hours: float | None = None,
        include_embeddings: bool = False,
    ) -> dict[str, Any]:
        candidates = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)
        requested_docs = {str(doc_id).strip() for doc_id in (doc_ids or ()) if str(doc_id).strip()}
        latest_by_doc: dict[tuple[str, str], Any] = {}
        for job in candidates:
            key = (job.tenant_id or "default", job.doc_id)
            if key not in latest_by_doc:
                latest_by_doc[key] = job
        processed: list[dict[str, Any]] = []
        for job in latest_by_doc.values():
            if requested_docs and job.doc_id not in requested_docs:
                continue
            request = self._request_from_job(job)
            request.options["mode"] = _RERUN_CHUNKS_ONLY_MODE
            outcome = self.submit(request)
            processed.append(
                {
                    "doc_id": job.doc_id,
                    "tenant_id": job.tenant_id,
                    "job_id": outcome.job.job_id,
                    "mode": _RERUN_CHUNKS_ONLY_MODE,
                    "chunks": len(outcome.chunks),
                }
            )
            if include_embeddings:
                embed_request = self._request_from_job(outcome.job)
                embed_request.options["mode"] = _RERUN_EMBEDDINGS_ONLY_MODE
                embedded = self.submit(embed_request)
                processed.append(
                    {
                        "doc_id": job.doc_id,
                        "tenant_id": job.tenant_id,
                        "job_id": embedded.job.job_id,
                        "mode": _RERUN_EMBEDDINGS_ONLY_MODE,
                        "chunks": len(embedded.chunks),
                    }
                )
        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "include_embeddings": bool(include_embeddings),
            "processed": processed,
            "documents": len({(item["tenant_id"], item["doc_id"]) for item in processed}),
        }

    def _check_quota_limit(self, request: ParseRequest) -> None:
        settings = self.settings.runtime
        if not bool(settings.quota_enforce):
            return
        tenant_id = (request.tenant_id or "default").strip() or "default"
        quota_key = (request.quota_key or "default").strip() or "default"
        limit_units = _resolve_quota_limit(
            quota_limits=settings.quota_limits,
            tenant_id=tenant_id,
            quota_key=quota_key,
            default_limit_units=int(settings.quota_default_limit_units),
        )
        if limit_units <= 0:
            return
        requested_units = max(1, int(request.quota_units or 1))
        since_hours: float | None = None
        if float(settings.quota_window_hours) > 0:
            since_hours = float(settings.quota_window_hours)
        usage = self.quota_usage(tenant_id=tenant_id, since_hours=since_hours)
        used_units = 0
        for item in usage.get("items", []):
            if str(item.get("quota_key") or "default") == quota_key:
                used_units = int(item.get("total_quota_units") or 0)
                break
        if used_units + requested_units > limit_units:
            self.event_aggregator.record_event(
                "quota_exceeded",
                tenant_id=tenant_id,
                quota_key=quota_key,
                doc_id=request.doc_id,
                details={
                    "used_units": used_units,
                    "requested_units": requested_units,
                    "limit_units": limit_units,
                },
            )
            raise QuotaExceededError(
                tenant_id=tenant_id,
                quota_key=quota_key,
                limit_units=limit_units,
                used_units=used_units,
                requested_units=requested_units,
                window_hours=since_hours,
            )

    def runtime_metrics(
        self,
        *,
        tenant_id: str | None = None,
        sample_size: int = 200,
        since_hours: float | None = None,
    ) -> dict[str, Any]:
        bounded_size = max(1, min(5000, int(sample_size)))
        jobs = self.list_jobs(tenant_id=tenant_id, since_hours=since_hours)[:bounded_size]
        if not jobs:
            return {
                "tenant_id": (tenant_id or "").strip() or None,
                "since_hours": float(since_hours) if since_hours is not None else None,
                "sample_size": bounded_size,
                "total_jobs": 0,
                "done_jobs": 0,
                "failed_jobs": 0,
                "active_jobs": 0,
                "failure_rate": 0.0,
                "retry_pending_jobs": 0,
                "part_jobs": _part_job_metrics(()),
                "durations_s": {
                    "count": 0,
                    "mean": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "p50": 0.0,
                    "p90": 0.0,
                    "p99": 0.0,
                },
            }

        done_jobs = [job for job in jobs if job.state == ParseJobState.DONE]
        failed_jobs = [job for job in jobs if job.state == ParseJobState.FAILED]
        active_jobs = [
            job
            for job in jobs
            if job.state in (ParseJobState.PENDING, ParseJobState.PARSING, ParseJobState.STRUCTURING, ParseJobState.EMBEDDING)
        ]
        durations = [duration for duration in (_job_duration_seconds(job) for job in done_jobs) if duration is not None]
        duration_stats = _duration_summary(durations)
        total_terminal = len(done_jobs) + len(failed_jobs)
        failure_rate = round((len(failed_jobs) / total_terminal), 4) if total_terminal > 0 else 0.0
        retry_pending_jobs = len([job for job in jobs if job.state == ParseJobState.PENDING and _next_attempt_at(job)])

        return {
            "tenant_id": (tenant_id or "").strip() or None,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "sample_size": bounded_size,
            "total_jobs": len(jobs),
            "done_jobs": len(done_jobs),
            "failed_jobs": len(failed_jobs),
            "active_jobs": len(active_jobs),
            "failure_rate": failure_rate,
            "retry_pending_jobs": retry_pending_jobs,
            "part_jobs": _part_job_metrics(jobs),
            "durations_s": duration_stats,
        }

    def tenant_dashboard(
        self,
        *,
        tenant_id: str | None = None,
        sample_size: int = 200,
        recent_limit: int = 5,
        since_hours: float | None = None,
    ) -> dict[str, Any]:
        bounded_recent = max(1, min(100, int(recent_limit)))
        normalized_tenant = (tenant_id or "").strip() or None
        usage = self.quota_usage(tenant_id=normalized_tenant, since_hours=since_hours)
        metrics = self.runtime_metrics(
            tenant_id=normalized_tenant,
            sample_size=sample_size,
            since_hours=since_hours,
        )
        recent_jobs = [
            {
                "job_id": job.job_id,
                "doc_id": job.doc_id,
                "state": job.state.value,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "quota_key": job.quota_key,
                "quota_units": int(job.quota_units or 1),
            }
            for job in self.list_jobs(tenant_id=normalized_tenant, since_hours=since_hours)[:bounded_recent]
        ]
        # Add observability data
        recent_events = self.event_aggregator.get_events(
            limit=10,
            tenant_id_filter=normalized_tenant,
        )
        event_counters = self.event_aggregator.get_counters(
            tenant_id_filter=normalized_tenant,
        )
        return {
            "tenant_id": normalized_tenant,
            "since_hours": float(since_hours) if since_hours is not None else None,
            "usage": usage,
            "metrics": metrics,
            "recent_jobs": recent_jobs,
            "observability": {
                "recent_events": recent_events,
                "event_counters": event_counters,
            },
        }

    def claim_next_job(self):
        claim_job = getattr(self.job_store, "claim_job", None)
        if not callable(claim_job):
            return self.job_store.claim_next_job()

        self.recover_timed_out_jobs()
        jobs = tuple(self.list_jobs())
        active_counts = _active_part_counts(jobs)
        now = datetime.now(timezone.utc)
        pending_jobs = sorted(
            (
                job
                for job in jobs
                if job.state == ParseJobState.PENDING and _job_ready_for_attempt(job, now=now)
                and _job_kind(job) != _PDF_PARENT_JOB_KIND
            ),
            key=lambda item: item.created_at,
        )
        for candidate in pending_jobs:
            if not _part_claim_allowed(candidate, active_counts):
                continue
            claimed = claim_job(
                job_id=candidate.job_id,
                lease_expires_at=_lease_expires_at_for_job(candidate, self.settings.runtime),
            )
            if claimed is None:
                continue
            if _part_claim_over_limit(claimed, tuple(self.list_jobs())):
                self._update_job_state(
                    job_id=claimed.job_id,
                    state=ParseJobState.PENDING,
                    claim_token=claimed.claim_token,
                    clear_claim=True,
                )
                continue
            return claimed
        return None

    def recover_timed_out_jobs(self) -> dict[str, Any]:
        timed_out: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for job in self.list_jobs():
            if _job_kind(job) == _PDF_PARENT_JOB_KIND or job.state not in _ACTIVE_PART_STATES:
                continue
            timeout_seconds = _timeout_seconds_for_job(job, self.settings.runtime)
            if timeout_seconds <= 0 or not _job_exceeded_timeout(job, now=now, timeout_seconds=timeout_seconds):
                continue
            attempt = int(job.attempt_count or 0)
            if attempt <= 0:
                attempt = self._increment_attempt(job_id=job.job_id)
            error = TimeoutError(f"job_timeout:{timeout_seconds}s")
            if attempt >= max(1, int(self.settings.runtime.max_attempts)):
                dead_letter = getattr(self.job_store, "mark_dead_letter", None)
                if callable(dead_letter):
                    updated = dead_letter(
                        job_id=job.job_id,
                        reason=str(error),
                        expected_claim_token=job.claim_token,
                    )
                else:
                    updated = self._update_job_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(error),
                        claim_token=job.claim_token,
                        clear_claim=True,
                    )
            else:
                updated = self._schedule_retry(job=job, attempt=attempt, error=error, claim_token=job.claim_token)
            self.event_aggregator.record_event(
                "job_timeout",
                tenant_id=job.tenant_id or "default",
                quota_key=job.quota_key or "default",
                doc_id=job.doc_id,
                details={
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "timeout_seconds": timeout_seconds,
                    "state": getattr(updated.state, "value", str(updated.state)),
                },
            )
            if _job_kind(job) == _PDF_PART_JOB_KIND:
                self.refresh_partitioned_parent(
                    doc_id=str(job.options.get("source_doc_id") or job.options.get("parent_doc_id") or ""),
                    tenant_id=job.tenant_id,
                    parent_job_id=str(job.options.get("parent_job_id") or ""),
                )
            timed_out.append(
                {
                    "job_id": job.job_id,
                    "doc_id": job.doc_id,
                    "attempt": attempt,
                    "timeout_seconds": timeout_seconds,
                    "state": getattr(updated.state, "value", str(updated.state)),
                }
            )
        return {"timed_out": timed_out}

    def _resolve_latest_job(self, *, doc_id: str, tenant_id: str | None = None):
        jobs = self.list_jobs(doc_id=doc_id, tenant_id=tenant_id)
        if not jobs:
            return None
        return jobs[0]

    def _request_from_job(self, job):
        return ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
            tenant_id=job.tenant_id,
            quota_key=job.quota_key,
            quota_units=job.quota_units,
        )

    def _load_blocks_for_request(self, request: ParseRequest):
        if self._is_rerun_chunks_only(request) or self._is_rerun_embeddings_only(request):
            purpose = "reembed" if self._is_rerun_embeddings_only(request) else "rechunk"
            self._resolve_pipeline(request, purpose=purpose)
            blocks = tuple(
                self.job_store.get_blocks(
                    doc_id=request.doc_id,
                    tenant_id=request.tenant_id,
                )
            )
            if not blocks:
                raise LookupError(
                    f"No existing blocks found for doc_id={request.doc_id!r}; cannot rerun derived outputs"
                )
            self.event_logger.log(
                "reused_blocks",
                doc_id=request.doc_id,
                blocks=len(blocks),
            )
            return blocks

        pipeline = self._resolve_pipeline(request, purpose="parse")
        return tuple(pipeline.parse_blocks(request=request))

    def _load_document_for_request(
        self,
        request: ParseRequest,
        *,
        blocks: Sequence[Any],
    ) -> ParsedDocumentArtifact | None:
        if self.pipeline_registry is None:
            return None
        purpose = "reembed" if self._is_rerun_embeddings_only(request) else (
            "rechunk" if self._is_rerun_chunks_only(request) else "parse"
        )
        pipeline = self._resolve_pipeline(request, purpose=purpose)
        return pipeline.build_document(request=request, blocks=blocks)

    def _load_chunks_for_request(
        self,
        request: ParseRequest,
        *,
        blocks: Sequence[Any],
        document: ParsedDocumentArtifact | None = None,
    ):
        if self._is_rerun_embeddings_only(request):
            chunks = tuple(
                self.job_store.get_chunks(
                    doc_id=request.doc_id,
                    tenant_id=request.tenant_id,
                )
            )
            if not chunks:
                raise LookupError(
                    f"No existing chunks found for doc_id={request.doc_id!r}; cannot rerun embeddings only"
                )
            self.event_logger.log(
                "reused_chunks",
                doc_id=request.doc_id,
                chunks=len(chunks),
            )
            return chunks
        if document is not None:
            pipeline = self._resolve_pipeline(
                request,
                purpose="rechunk" if self._is_rerun_chunks_only(request) else "parse",
            )
            return tuple(pipeline.chunker.build(document=document))
        purpose = "rechunk" if self._is_rerun_chunks_only(request) else "parse"
        pipeline = self._resolve_pipeline(request, purpose=purpose)
        return pipeline.build_chunks(request=request, blocks=blocks)

    def _build_index_manifest(
        self,
        *,
        request: ParseRequest,
        job_id: str,
        document: ParsedDocumentArtifact | None,
        chunks: Sequence[Chunk],
    ) -> dict[str, Any]:
        pipeline_name = str(getattr(document, "pipeline_name", "") or "")
        options_hash = str(getattr(document, "options_hash", "") or "")
        structure_roles = sorted(
            {
                str(getattr(item, "semantic_role", "") or "paragraph")
                for item in tuple(getattr(document, "items", ()) or ())
            }
        )
        chunk_roles = sorted({str(chunk.semantic_role or "paragraph") for chunk in chunks})
        index_version = options_hash or job_id
        embedding_tiers = _resolve_embedding_tiers(request.options)
        high_precision_chunks = _select_high_precision_chunks(chunks)
        layers: list[dict[str, Any]] = [
            {
                "name": "primary",
                "kind": "chunk",
                "version": index_version,
                "item_count": len(chunks),
                "semantic_roles": chunk_roles,
                "embedding_tier": embedding_tiers[0],
            },
            {
                "name": "structure",
                "kind": "typed-item",
                "version": index_version,
                "item_count": len(tuple(getattr(document, "items", ()) or ())),
                "semantic_roles": structure_roles,
            },
        ]
        if "large" in embedding_tiers:
            layers.append(
                {
                    "name": "high_precision",
                    "kind": "chunk",
                    "version": index_version,
                    "item_count": len(high_precision_chunks),
                    "semantic_roles": sorted({str(chunk.semantic_role or "paragraph") for chunk in high_precision_chunks}),
                    "embedding_tier": "large",
                    "chunk_ids": [chunk.chunk_id for chunk in high_precision_chunks],
                }
            )
        manifest = {
            "doc_id": request.doc_id,
            "tenant_id": request.tenant_id,
            "pipeline_name": pipeline_name,
            "options_hash": options_hash,
            "index_version": index_version,
            "embedding_tiers": embedding_tiers,
            "layers": layers,
            "manual_anatomy": dict((getattr(document, "metadata", {}) or {}).get("manual_anatomy") or {}),
            "structure_quality": dict((getattr(document, "metadata", {}) or {}).get("structure_quality") or {}),
            "rag_coverage": _runtime_rag_coverage_manifest(
                doc_id=request.doc_id,
                document=document,
                chunks=chunks,
            ),
        }
        if _is_pdf_part_request(request):
            manifest["part"] = _pdf_part_index_manifest(
                request=request,
                chunks=chunks,
                index_version=index_version,
            )
        return manifest

    @staticmethod
    def _structure_entries_from_blocks(*, doc_id: str, blocks: Sequence[Any]) -> tuple[dict[str, Any], ...]:
        entries: list[dict[str, Any]] = []
        for index, block in enumerate(blocks, start=1):
            metadata = getattr(block, "metadata", {}) or {}
            semantic_role = str(metadata.get("semantic_role") or "paragraph").strip().lower()
            tags = _structure_tags_from_block_metadata(metadata=metadata, semantic_role=semantic_role)
            page_raw = metadata.get("page")
            try:
                page_number = int(page_raw) if page_raw is not None else None
            except (TypeError, ValueError):
                page_number = None
            entries.append(
                {
                    "item_id": f"itm-{index}",
                    "doc_id": doc_id,
                    "block_id": getattr(block, "block_id", f"blk-{index}"),
                    "text": getattr(block, "content", ""),
                    "semantic_role": semantic_role,
                    "structure_tags": tags,
                    "page_number": page_number,
                }
            )
        return tuple(entries)

    @staticmethod
    def _is_rerun_chunks_only(request: ParseRequest) -> bool:
        mode = str(request.options.get("mode", "")).strip().lower()
        return mode == _RERUN_CHUNKS_ONLY_MODE

    @staticmethod
    def _is_rerun_embeddings_only(request: ParseRequest) -> bool:
        mode = str(request.options.get("mode", "")).strip().lower()
        return mode == _RERUN_EMBEDDINGS_ONLY_MODE

    def _resolve_parser(self, request: ParseRequest) -> ParserAdapter:
        suffix = Path(request.file_path).suffix.lower()
        preferred_parser = _preferred_local_provider_parser(request.options)
        if preferred_parser:
            for parser in self.parsers:
                if parser.name.strip().lower() != preferred_parser:
                    continue
                if parser.supports(media_type=request.media_type, suffix=suffix):
                    return parser
            raise LookupError(
                f"No parser registered for provider={preferred_parser!r}, "
                f"media_type={request.media_type!r}, suffix={suffix!r}"
            )
        for parser in self.parsers:
            if parser.supports(media_type=request.media_type, suffix=suffix):
                return parser
        raise LookupError(f"No parser registered for media_type={request.media_type!r}, suffix={suffix!r}")

    def _resolve_pipeline(self, request: ParseRequest, *, purpose: str = "parse"):
        preferred_parser = _preferred_local_provider_parser(request.options)
        if self.pipeline_registry is not None:
            return self.pipeline_registry.resolve(
                request,
                purpose=purpose,
                parser_name=preferred_parser,
            )
        parser = self._resolve_parser(request)
        if purpose not in {"parse", "rechunk", "reembed"}:
            raise LookupError(f"Unsupported pipeline purpose={purpose!r}")
        raise LookupError(
            f"No pipeline registry available for parser={parser.name!r}; rebuild runtime with pipeline registry support"
        )

    def _prepare_request_for_execution(self, *, request: ParseRequest, job: Any) -> tuple[ParseRequest, Any]:
        routed_request = self._request_with_local_provider_route(request)
        if routed_request.options == request.options:
            return request, job
        update_options = getattr(self.job_store, "update_options", None)
        if callable(update_options):
            try:
                job = update_options(job_id=job.job_id, options=routed_request.options)
            except Exception as exc:  # pragma: no cover - defensive observability path
                self.event_logger.log(
                    "local_provider_route_option_update_failed",
                    job_id=getattr(job, "job_id", ""),
                    doc_id=request.doc_id,
                    error=str(exc),
                )
        return routed_request, job

    def _request_with_local_provider_route(self, request: ParseRequest) -> ParseRequest:
        if self._is_rerun_chunks_only(request) or self._is_rerun_embeddings_only(request):
            return request
        routing_settings = self.settings.providers.local_parser_routing
        if not routing_settings.enabled:
            return request
        route_request = _local_provider_route_request(request.options)
        existing_route = request.options.get(_LOCAL_PROVIDER_ROUTING_OPTION)
        if (
            route_request is None
            and isinstance(existing_route, Mapping)
            and str(existing_route.get("selected_provider_id") or "").strip()
        ):
            return request

        plan = self.provider_route_plan(
            media_type=request.media_type,
            file_name=request.file_path,
            profile=str(request.options.get("profile") or ""),
            required_capabilities=_route_request_required_capabilities(route_request),
            include_disabled=routing_settings.include_disabled,
        )
        selected_provider_id, selected_role = self._select_registered_local_provider(
            plan=plan,
            request=request,
        )
        eligible_ids = [
            str(item)
            for item in (plan.get("selection") or {}).get("eligible_provider_ids", [])
            if str(item).strip()
        ]
        primary_provider_id = str((plan.get("selection") or {}).get("primary_provider_id") or "").strip()
        if selected_provider_id:
            route_status = "selected" if selected_role == "primary" else "fallback_selected"
        elif eligible_ids:
            route_status = (
                "no_registered_provider_fallback_default"
                if routing_settings.fallback_to_default
                else "no_registered_provider"
            )
        else:
            route_status = "no_eligible_provider"
        decision = {
            "schema_version": "2026-06-local-provider-routing-decision",
            "enabled": True,
            "routing_policy": plan.get("routing_policy"),
            "route_status": route_status,
            "selected_provider_id": selected_provider_id,
            "selected_route_role": selected_role,
            "primary_provider_id": primary_provider_id or None,
            "fallback_provider_ids": list((plan.get("selection") or {}).get("fallback_provider_ids", [])),
            "eligible_provider_ids": eligible_ids,
            "excluded_provider_ids": list((plan.get("selection") or {}).get("excluded_provider_ids", [])),
            "fallback_to_default": routing_settings.fallback_to_default,
            "requested": plan.get("requested") or {},
        }
        if not selected_provider_id and not routing_settings.fallback_to_default and eligible_ids:
            decision["selected_provider_id"] = primary_provider_id or eligible_ids[0]
            decision["selected_route_role"] = "primary"
        options = dict(request.options)
        options[_LOCAL_PROVIDER_ROUTING_OPTION] = decision
        self.event_logger.log(
            "local_provider_route",
            doc_id=request.doc_id,
            route_status=route_status,
            selected_provider_id=decision.get("selected_provider_id"),
            primary_provider_id=primary_provider_id or None,
            eligible_provider_ids=eligible_ids,
        )
        return replace(request, options=options)

    def _select_registered_local_provider(
        self,
        *,
        plan: Mapping[str, Any],
        request: ParseRequest,
    ) -> tuple[str | None, str | None]:
        suffix = Path(request.file_path).suffix.lower()
        parser_by_name = {parser.name.strip().lower(): parser for parser in self.parsers}
        candidate_roles = {
            str(candidate.get("id") or "").strip().lower(): str(candidate.get("route_role") or "")
            for candidate in plan.get("candidates", [])
            if isinstance(candidate, Mapping)
        }
        for provider_id in (plan.get("selection") or {}).get("eligible_provider_ids", []) or []:
            normalized = str(provider_id or "").strip().lower()
            if not normalized:
                continue
            parser = parser_by_name.get(normalized)
            if parser is None:
                continue
            if parser.supports(media_type=request.media_type, suffix=suffix):
                return normalized, candidate_roles.get(normalized) or "candidate"
        return None, None

    def _record_ocr_observability(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Any],
    ) -> None:
        trace = build_ocr_decision_trace(blocks)

        attempted_blocks = sum(1 for block in blocks if bool((getattr(block, "metadata", {}) or {}).get("ocr_attempted")))
        fallback_blocks = sum(1 for block in blocks if bool((getattr(block, "metadata", {}) or {}).get("ocr_fallback_used")))
        failed_blocks = sum(1 for block in blocks if bool((getattr(block, "metadata", {}) or {}).get("ocr_error_reason")))

        if trace.attempted_pages:
            self.event_aggregator.record_event(
                "ocr_attempted",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": trace.attempted_pages,
                    "block_count": attempted_blocks,
                    "attempt_reasons": list(trace.attempt_reasons),
                    "acceptance_reasons": list(trace.acceptance_reasons),
                    "rejection_reasons": list(trace.rejection_reasons),
                    "native_text_token_count": trace.native_text_token_count,
                    "final_text_token_count": trace.final_text_token_count,
                },
                count=trace.attempted_pages,
            )

        if trace.fallback_pages:
            self.event_aggregator.record_event(
                "ocr_fallback",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": trace.fallback_pages,
                    "block_count": fallback_blocks,
                    "acceptance_reasons": list(trace.acceptance_reasons),
                },
                count=trace.fallback_pages,
            )

        if trace.failed_pages:
            self.event_aggregator.record_event(
                "ocr_failed",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": trace.failed_pages,
                    "block_count": failed_blocks,
                    "error_reasons": list(trace.error_reasons),
                },
                count=trace.failed_pages,
            )

        if trace.rejected_pages:
            self.event_aggregator.record_event(
                "ocr_rejected",
                tenant_id=request.tenant_id,
                quota_key=request.quota_key,
                doc_id=request.doc_id,
                details={
                    "provider": self.settings.providers.ocr.provider,
                    "page_count": trace.rejected_pages,
                    "rejection_reasons": list(trace.rejection_reasons),
                },
                count=trace.rejected_pages,
            )

    def _embed_chunks(
        self,
        *,
        doc_id: str,
        chunks: Sequence[Any],
        tenant_id: str = "*",
        quota_key: str = "*",
    ) -> Sequence[Any]:
        if not chunks:
            return tuple(chunks)
        provider_settings = self.settings.providers.embedding
        batch_size = max(1, int(provider_settings.batch_size or len(chunks)))
        max_retries = max(0, int(provider_settings.max_retries))
        base_backoff_seconds = 0.05
        embedded: list[Any] = []
        failed_batches = 0
        for batch_index, start in enumerate(range(0, len(chunks), batch_size), start=1):
            batch = tuple(chunks[start : start + batch_size])
            result = self._embed_batch_with_retry(
                doc_id=doc_id,
                batch=batch,
                batch_index=batch_index,
                max_retries=max_retries,
                base_backoff_seconds=base_backoff_seconds,
                tenant_id=tenant_id,
                quota_key=quota_key,
            )
            if result is None:
                failed_batches += 1
                embedded.extend(batch)
            else:
                embedded.extend(result)

        if failed_batches > 0:
            self.event_logger.log(
                "embedding_skipped",
                doc_id=doc_id,
                chunks=len(chunks),
                failed_batches=failed_batches,
                total_batches=((len(chunks) + batch_size - 1) // batch_size),
            )
        return tuple(embedded)

    def _embed_batch_with_retry(
        self,
        *,
        doc_id: str,
        batch: Sequence[Any],
        batch_index: int,
        max_retries: int,
        base_backoff_seconds: float,
        tenant_id: str,
        quota_key: str,
    ) -> tuple[Any, ...] | None:
        attempts = max(1, max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                embedded_batch = tuple(self.embedding_provider.embed(doc_id=doc_id, chunks=batch))
                if len(embedded_batch) != len(batch):
                    raise RuntimeError(
                        f"embedding_provider_mismatch: got {len(embedded_batch)} for {len(batch)}"
                    )
                return embedded_batch
            except Exception as exc:
                provider_settings = self.settings.providers.embedding
                failure_category = classify_provider_failure(exc)
                if attempt < attempts:
                    delay = min(base_backoff_seconds * (2 ** (attempt - 1)), 0.5)
                    self.event_logger.log(
                        "embedding_retry",
                        doc_id=doc_id,
                        batch_index=batch_index,
                        attempt=attempt,
                        max_attempts=attempts,
                        delay_s=round(delay, 3),
                        error=str(exc),
                        provider=provider_settings.provider,
                        model=provider_settings.model,
                        failure_category=failure_category,
                    )
                    self.event_aggregator.record_event(
                        "embedding_retry",
                        tenant_id=tenant_id,
                        quota_key=quota_key,
                        doc_id=doc_id,
                        details={
                            "batch_index": batch_index,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "delay_s": round(delay, 3),
                            "provider_type": "embedding",
                            "provider_id": provider_settings.provider,
                            "model": provider_settings.model,
                            "failure_category": failure_category,
                        },
                    )
                    time.sleep(delay)
                    continue
                self.event_logger.log(
                    "embedding_batch_skipped",
                    doc_id=doc_id,
                    batch_index=batch_index,
                    attempts=attempts,
                    chunks=len(batch),
                    error=str(exc),
                    provider=provider_settings.provider,
                    model=provider_settings.model,
                    failure_category=failure_category,
                )
                self.event_aggregator.record_event(
                    "embedding_skipped",
                    tenant_id=tenant_id,
                    quota_key=quota_key,
                    doc_id=doc_id,
                    details={
                        "batch_index": batch_index,
                        "attempts": attempts,
                        "chunks": len(batch),
                        "provider_type": "embedding",
                        "provider_id": provider_settings.provider,
                        "model": provider_settings.model,
                        "failure_category": failure_category,
                    },
                )
                self.event_aggregator.record_event(
                    "provider_failure",
                    tenant_id=tenant_id,
                    quota_key=quota_key,
                    doc_id=doc_id,
                    details={
                        "provider_type": "embedding",
                        "provider_id": provider_settings.provider,
                        "model": provider_settings.model,
                        "failure_category": failure_category,
                        "operation": "embed_batch",
                        "batch_index": batch_index,
                        "attempts": attempts,
                        "chunks": len(batch),
                    },
                )
                return None

def _job_duration_seconds(job: Any) -> float | None:
    created_at = getattr(job, "created_at", None)
    updated_at = getattr(job, "updated_at", None)
    if not created_at or not updated_at:
        return None
    try:
        started = datetime.fromisoformat(str(created_at))
        finished = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return None
    delta = (finished - started).total_seconds()
    if delta < 0:
        return None
    return float(delta)


def _duration_summary(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {
            "count": 0,
            "mean": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
        }
    ordered = sorted(float(item) for item in values)
    count = len(ordered)
    mean = sum(ordered) / count
    return {
        "count": count,
        "mean": round(mean, 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p90": round(_percentile(ordered, 0.90), 3),
        "p99": round(_percentile(ordered, 0.99), 3),
    }


def _percentile(sorted_values: Sequence[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    target = max(0.0, min(1.0, float(ratio)))
    index = int(round(target * (len(sorted_values) - 1)))
    return float(sorted_values[index])


def _part_job_metrics(jobs: Sequence[Any]) -> dict[str, Any]:
    part_jobs = [job for job in jobs if _job_kind(job) == _PDF_PART_JOB_KIND]
    active_jobs = [job for job in part_jobs if _job_state(job) in _ACTIVE_PART_STATES]
    queued_jobs = [job for job in part_jobs if _job_state(job) == ParseJobState.PENDING]
    cancelled_jobs = [
        job
        for job in part_jobs
        if _job_state(job) == ParseJobState.FAILED
        and str(getattr(job, "failure_reason", "") or "").strip().lower() == "cancelled"
    ]
    failed_jobs = [job for job in part_jobs if _job_state(job) == ParseJobState.FAILED and job not in cancelled_jobs]
    durations = [duration for duration in (_job_duration_seconds(job) for job in part_jobs if _job_state(job) == ParseJobState.DONE) if duration is not None]
    return {
        "parts_total": len(part_jobs),
        "parts_done": len([job for job in part_jobs if _job_state(job) == ParseJobState.DONE]),
        "parts_failed": len(failed_jobs),
        "parts_active": len(active_jobs),
        "parts_queued": len(queued_jobs),
        "parts_cancelled": len(cancelled_jobs),
        "parts_retry_pending": len([job for job in queued_jobs if _next_attempt_at(job)]),
        "part_elapsed_s": _duration_summary(durations),
    }


def _job_ready_for_attempt(job: Any, *, now: datetime) -> bool:
    next_attempt_at = _next_attempt_at(job)
    if next_attempt_at is None:
        return True
    return next_attempt_at <= now


def _next_attempt_at(job: Any) -> datetime | None:
    column_value = _parse_iso_datetime(getattr(job, "next_attempt_at", None))
    if column_value is not None:
        return column_value
    options = getattr(job, "options", {}) or {}
    if not isinstance(options, dict):
        return None
    return _parse_iso_datetime(options.get("next_attempt_at"))


def _retry_delay_seconds(attempt: int, *, base_seconds: float, max_seconds: float) -> float:
    base = max(0.0, float(base_seconds))
    cap = max(base, float(max_seconds))
    if base <= 0:
        return 0.0
    delay = base * (2 ** max(0, int(attempt) - 1))
    return round(min(delay, cap), 3)


def _iso_after_seconds(delay_seconds: float) -> str:
    target = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(delay_seconds)))
    return target.isoformat()


def _lease_expires_at_for_job(job: Any, runtime_settings: Any) -> str | None:
    timeout_seconds = _timeout_seconds_for_job(job, runtime_settings)
    if timeout_seconds <= 0:
        return None
    return _iso_after_seconds(timeout_seconds)


def _timeout_seconds_for_job(job: Any, runtime_settings: Any) -> int:
    if _job_kind(job) == _PDF_PART_JOB_KIND:
        part_timeout = int(getattr(runtime_settings, "part_timeout_seconds", 0) or 0)
        if part_timeout > 0:
            return part_timeout
    return int(getattr(runtime_settings, "job_timeout_seconds", 0) or 0)


def _job_exceeded_timeout(job: Any, *, now: datetime, timeout_seconds: int) -> bool:
    lease_expires_at = _parse_iso_datetime(getattr(job, "lease_expires_at", None))
    if lease_expires_at is not None:
        return now >= lease_expires_at
    updated_at = _parse_iso_datetime(getattr(job, "updated_at", None))
    if updated_at is None:
        return False
    return (now - updated_at).total_seconds() >= max(1, int(timeout_seconds))


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _active_part_counts(jobs: Sequence[Any]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for job in jobs:
        if _job_kind(job) != _PDF_PART_JOB_KIND or _job_state(job) not in _ACTIVE_PART_STATES:
            continue
        key = _part_limit_key(job)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _part_claim_allowed(job: Any, active_counts: Mapping[tuple[str, str, str], int]) -> bool:
    if _job_kind(job) != _PDF_PART_JOB_KIND:
        return True
    limit = _part_active_limit(job)
    if limit <= 0:
        return True
    return int(active_counts.get(_part_limit_key(job), 0)) < limit


def _part_claim_over_limit(job: Any, jobs: Sequence[Any]) -> bool:
    if _job_kind(job) != _PDF_PART_JOB_KIND:
        return False
    limit = _part_active_limit(job)
    if limit <= 0:
        return False
    key = _part_limit_key(job)
    active_jobs = sorted(
        (
            item
            for item in jobs
            if _job_kind(item) == _PDF_PART_JOB_KIND
            and _job_state(item) in _ACTIVE_PART_STATES
            and _part_limit_key(item) == key
        ),
        key=lambda item: item.created_at,
    )
    allowed_ids = {item.job_id for item in active_jobs[:limit]}
    return getattr(job, "job_id", None) not in allowed_ids


def _part_active_limit(job: Any) -> int:
    options = getattr(job, "options", {}) or {}
    if not isinstance(options, dict):
        return 0
    try:
        return max(0, int(options.get("max_active_parts_per_doc") or 0))
    except (TypeError, ValueError):
        return 0


def _part_limit_key(job: Any) -> tuple[str, str, str]:
    options = getattr(job, "options", {}) or {}
    if not isinstance(options, dict):
        options = {}
    tenant_id = str(getattr(job, "tenant_id", None) or "default")
    source_doc_id = str(options.get("source_doc_id") or getattr(job, "doc_id", "") or "")
    parent_job_id = str(options.get("parent_job_id") or "")
    return (tenant_id, source_doc_id, parent_job_id)


def _job_state(job: Any) -> ParseJobState | None:
    state = getattr(job, "state", None)
    if isinstance(state, ParseJobState):
        return state
    value = getattr(state, "value", state)
    try:
        return ParseJobState(str(value))
    except (TypeError, ValueError):
        return None


def _job_kind(job: Any) -> str:
    options = getattr(job, "options", {}) or {}
    if not isinstance(options, dict):
        return ""
    return str(options.get("job_kind") or "").strip()


def _is_pdf_job(job: Any) -> bool:
    media_type = str(getattr(job, "media_type", "") or "").lower()
    suffix = Path(str(getattr(job, "file_path", "") or "")).suffix.lower()
    return media_type == "application/pdf" or suffix == ".pdf"


def _is_pdf_part_request(request: ParseRequest) -> bool:
    return str((request.options or {}).get("job_kind") or "") == _PDF_PART_JOB_KIND


def _preferred_local_provider_parser(options: Mapping[str, Any]) -> str | None:
    route = options.get(_LOCAL_PROVIDER_ROUTING_OPTION)
    if not isinstance(route, Mapping):
        return None
    provider_id = str(route.get("selected_provider_id") or "").strip().lower()
    return provider_id or None


def _local_provider_route_request(options: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(options, Mapping):
        return None
    raw = options.get(_LOCAL_PROVIDER_ROUTE_REQUEST_OPTION)
    if not isinstance(raw, Mapping):
        return None
    required_capabilities = _route_request_required_capabilities(raw)
    if not required_capabilities:
        return None
    return {"required_capabilities": required_capabilities}


def _normalize_local_provider_route_request(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid_provider_route_plan")
    required_capabilities = _route_request_required_capabilities(value)
    if not required_capabilities:
        return None
    return {"required_capabilities": required_capabilities}


def _route_request_required_capabilities(value: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    required_capabilities = _runtime_string_list(value.get("required_capabilities"))
    required_capabilities.extend(_runtime_string_list(value.get("capabilities")))
    capability = str(value.get("capability") or "").strip()
    if capability:
        required_capabilities.append(capability)
    return list(dict.fromkeys(required_capabilities))


def _part_specs_from_job(job: Any) -> list[dict[str, Any]]:
    options = getattr(job, "options", {}) or {}
    raw_specs = options.get("part_specs") if isinstance(options, dict) else None
    if not isinstance(raw_specs, list):
        return []
    specs: list[dict[str, Any]] = []
    for spec in raw_specs:
        if isinstance(spec, dict):
            specs.append(dict(spec))
    return specs


def _runtime_rag_coverage_manifest(
    *,
    doc_id: str,
    document: ParsedDocumentArtifact | None,
    chunks: Sequence[Chunk],
) -> dict[str, Any]:
    items = tuple(getattr(document, "items", ()) or ()) if document is not None else ()
    artifact_units = tuple(getattr(document, "knowledge_units", ()) or ()) if document is not None else ()
    embedded_chunk_ids = {str(chunk.chunk_id) for chunk in chunks if chunk.embedding is not None}
    all_chunk_ids = sorted({str(chunk.chunk_id) for chunk in chunks})
    chunks_by_block_id: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        for block_id in tuple(chunk.block_ids or ()):
            normalized = str(block_id or "").strip()
            if normalized:
                chunks_by_block_id.setdefault(normalized, []).append(chunk)

    units: list[dict[str, Any]] = []
    if artifact_units:
        for unit in artifact_units:
            block_ids = tuple(
                str(block_id)
                for block_id in tuple(getattr(unit, "source_block_ids", ()) or ())
                if str(block_id)
            )
            chunk_ids = sorted(
                {
                    str(chunk.chunk_id)
                    for block_id in block_ids
                    for chunk in chunks_by_block_id.get(block_id, ())
                }
            )
            units.append(
                {
                    "unit_id": str(getattr(unit, "unit_id", "") or ""),
                    "source_item_id": next(iter(tuple(getattr(unit, "source_item_ids", ()) or ())), ""),
                    "unit_type": str(getattr(unit, "unit_type", "") or "paragraph"),
                    "semantic_role": str(getattr(unit, "semantic_role", "") or "paragraph"),
                    "page_span": list(_runtime_page_span_tuple(getattr(unit, "page_span", None))),
                    "source_block_ids": list(block_ids),
                    "source_table_ids": _runtime_string_list(getattr(unit, "source_table_ids", ())),
                    "should_index_for_rag": bool(getattr(unit, "should_index_for_rag", False)),
                    "skip_reason": getattr(unit, "skip_reason", None),
                    "chunk_ids": chunk_ids,
                    "embedded": bool(chunk_ids) and all(chunk_id in embedded_chunk_ids for chunk_id in chunk_ids),
                }
            )
    else:
        for index, item in enumerate(items, start=1):
            metadata = getattr(item, "metadata", {}) or {}
            role = str(
                getattr(item, "semantic_role", "")
                or metadata.get("semantic_role")
                or getattr(item, "kind", "")
                or "paragraph"
            ).strip().lower()
            text = _runtime_unit_text(item)
            block_ids = tuple(str(block_id) for block_id in tuple(getattr(item, "block_ids", ()) or ()) if str(block_id))
            chunk_ids = sorted(
                {
                    str(chunk.chunk_id)
                    for block_id in block_ids
                    for chunk in chunks_by_block_id.get(block_id, ())
                }
            )
            should_index = bool(text.strip()) and role not in _NON_INDEXABLE_RAG_ROLES
            units.append(
                {
                    "unit_id": f"{doc_id}:ku:{index:06d}",
                    "source_item_id": str(getattr(item, "item_id", "") or f"itm-{index}"),
                    "unit_type": _runtime_unit_type(item=item, semantic_role=role),
                    "semantic_role": role,
                    "page_span": list(_runtime_item_page_span(item)),
                    "source_block_ids": list(block_ids),
                    "source_table_ids": _runtime_source_table_ids(item=item, semantic_role=role),
                    "should_index_for_rag": should_index,
                    "skip_reason": None if should_index else _runtime_unit_skip_reason(semantic_role=role, text=text),
                    "chunk_ids": chunk_ids,
                    "embedded": bool(chunk_ids) and all(chunk_id in embedded_chunk_ids for chunk_id in chunk_ids),
                }
            )

    indexable_units = [unit for unit in units if bool(unit.get("should_index_for_rag"))]
    skipped_units = [unit for unit in units if not bool(unit.get("should_index_for_rag"))]
    chunked_units = [unit for unit in indexable_units if unit.get("chunk_ids")]
    embedded_units = [unit for unit in indexable_units if bool(unit.get("embedded"))]
    unembedded_units = [
        unit
        for unit in indexable_units
        if unit.get("chunk_ids") and not bool(unit.get("embedded"))
    ]
    text_pages = {
        _runtime_page_span_tuple(unit.get("page_span"))[0]
        for unit in units
        if unit.get("skip_reason") != "empty_text"
    }
    pages_with_indexable_units = {
        _runtime_page_span_tuple(unit.get("page_span"))[0]
        for unit in indexable_units
    }
    table_pages = {
        _runtime_page_span_tuple(unit.get("page_span"))[0]
        for unit in units
        if str(unit.get("unit_type") or "") == "table"
    }
    table_pages_with_units = {
        _runtime_page_span_tuple(unit.get("page_span"))[0]
        for unit in indexable_units
        if str(unit.get("unit_type") or "") == "table"
    }
    return {
        "schema_version": "2026-06-rag-index-manifest",
        "source": "runtime_index_manifest",
        "strategy": "document_knowledge_units" if artifact_units else "document_artifact_units",
        "unit_count": len(units),
        "indexable_unit_count": len(indexable_units),
        "skipped_unit_count": len(skipped_units),
        "chunked_unit_count": len(chunked_units),
        "unchunked_unit_count": len(indexable_units) - len(chunked_units),
        "chunk_count": len(all_chunk_ids),
        "embedded_chunk_count": len(embedded_chunk_ids),
        "embedded_unit_count": len(embedded_units),
        "unembedded_unit_count": len(unembedded_units),
        "coverage_score": _runtime_ratio(len(chunked_units), len(indexable_units)),
        "unit_chunk_coverage_ratio": _runtime_ratio(len(chunked_units), len(indexable_units)),
        "text_page_coverage_ratio": _runtime_ratio(len(pages_with_indexable_units), len(text_pages)),
        "table_unit_coverage_ratio": _runtime_ratio(len(table_pages_with_units), len(table_pages)),
        "chunk_ids": all_chunk_ids,
        "units": units,
    }


def _runtime_unit_text(item: Any) -> str:
    metadata = getattr(item, "metadata", {}) or {}
    return str(metadata.get("rendered_text") or getattr(item, "text", "") or "").strip()


def _runtime_unit_type(*, item: Any, semantic_role: str) -> str:
    block_type = str((getattr(item, "provenance", {}) or {}).get("block_type") or "").strip().lower()
    kind = str(getattr(item, "kind", "") or "").strip().lower()
    if block_type == "table" or kind == "table" or semantic_role == "table":
        return "table"
    if block_type == "image" or kind == "image" or semantic_role == "image":
        return "figure_caption"
    if block_type == "title" or semantic_role == "title":
        return "title"
    return "paragraph"


def _runtime_source_table_ids(*, item: Any, semantic_role: str) -> list[str]:
    metadata = getattr(item, "metadata", {}) or {}
    table_ids = _runtime_string_list(metadata.get("source_table_ids"))
    table_id = str(metadata.get("table_id") or "").strip()
    if table_id:
        table_ids.append(table_id)
    if table_ids:
        return sorted(dict.fromkeys(table_ids))
    if _runtime_unit_type(item=item, semantic_role=semantic_role) == "table":
        return [str(getattr(item, "item_id", "") or "table")]
    return []


def _runtime_unit_skip_reason(*, semantic_role: str, text: str) -> str:
    if not str(text or "").strip():
        return "empty_text"
    if semantic_role in _NON_INDEXABLE_RAG_ROLES:
        return f"semantic_role:{semantic_role}"
    return "index_policy_skip"


def _runtime_item_page_span(item: Any) -> tuple[int, int]:
    metadata = getattr(item, "metadata", {}) or {}
    if metadata.get("page_span") is not None:
        return _runtime_page_span_tuple(metadata.get("page_span"))
    page_start = metadata.get("page_start", metadata.get("page"))
    page_end = metadata.get("page_end", page_start)
    if page_start is not None:
        start = _safe_int_runtime(page_start, default=1)
        end = _safe_int_runtime(page_end, default=start)
        return min(start, end), max(start, end)
    page_number = getattr(item, "page_number", None)
    page = _safe_int_runtime(page_number, default=1)
    return page, page


def _runtime_page_span_tuple(value: Any) -> tuple[int, int]:
    if isinstance(value, Mapping):
        start = _safe_int_runtime(value.get("start", value.get("page_start")), default=1)
        end = _safe_int_runtime(value.get("end", value.get("page_end")), default=start)
        return min(start, end), max(start, end)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _safe_int_runtime(value[0], default=1)
        end = _safe_int_runtime(value[1], default=start)
        return min(start, end), max(start, end)
    page = _safe_int_runtime(value, default=1)
    return page, page


def _runtime_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _runtime_provider_ids(blocks: Sequence[Any]) -> list[str]:
    provider_ids: list[str] = []
    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            continue
        for key in ("provider_id", "parser", "layout_source", "ocr_engine"):
            value = str(metadata.get(key) or "").strip()
            if value and value not in provider_ids:
                provider_ids.append(value)
                break
    return provider_ids


def _runtime_document_quality_observation(
    *,
    settings: ParseCoreSettings,
    blocks: Sequence[Any],
    index_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build quality telemetry without rebuilding the diagnostic projections."""
    rag_manifest = _as_mapping_runtime(index_manifest.get("rag_coverage"))
    text_ratio = _runtime_float(rag_manifest.get("text_page_coverage_ratio"), default=1.0)
    table_ratio = _runtime_float(rag_manifest.get("table_unit_coverage_ratio"), default=1.0)
    unit_chunk_ratio = _runtime_float(rag_manifest.get("unit_chunk_coverage_ratio"), default=1.0)
    chunk_count = max(0, _safe_int_runtime(rag_manifest.get("chunk_count"), default=0))
    embedded_chunk_count = max(
        0,
        _safe_int_runtime(rag_manifest.get("embedded_chunk_count"), default=0),
    )
    embedded_chunk_ratio = embedded_chunk_count / chunk_count if chunk_count else 1.0

    units = [
        item
        for item in (rag_manifest.get("units") or [])
        if isinstance(item, Mapping)
    ]
    unchunked_units = max(
        0,
        _safe_int_runtime(rag_manifest.get("unchunked_unit_count"), default=0),
    )
    unembedded_units = max(
        0,
        _safe_int_runtime(rag_manifest.get("unembedded_unit_count"), default=0),
    )
    figure_caption_missing_units = [
        unit
        for unit in units
        if str(unit.get("unit_type") or "") == "figure_caption"
        and not bool(unit.get("should_index_for_rag"))
        and str(unit.get("skip_reason") or "") == "empty_text"
    ]

    page_confidences: dict[int, list[float]] = {}
    signal_keys: set[tuple[int, str]] = set()
    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            continue
        page_number = _safe_int_runtime(metadata.get("page"), default=1)
        confidence = metadata.get(
            "reading_order_confidence",
            metadata.get("layout_reading_order_confidence"),
        )
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            page_confidences.setdefault(page_number, []).append(
                max(0.0, min(1.0, float(confidence)))
            )
        for code in _runtime_string_list(metadata.get("quality_signal_codes")):
            signal_keys.add((page_number, code))
        if bool(metadata.get("ocr_attempted")):
            signal_keys.add((page_number, "ocr_attempted"))
        if metadata.get("ocr_error_reason"):
            signal_keys.add((page_number, "ocr_failed"))
        if (
            not str(getattr(block, "content", "") or "").strip()
            and str(metadata.get("semantic_role") or "") == "parse_artifact"
        ):
            signal_keys.add((page_number, "empty_page"))

    page_reading_order = [
        sum(values) / len(values) for values in page_confidences.values() if values
    ]
    reading_order_confidence = (
        round(sum(page_reading_order) / len(page_reading_order), 4)
        if page_reading_order
        else None
    )

    gate_settings = settings.quality_gate
    flags: list[str] = []
    if text_ratio < gate_settings.min_text_page_coverage:
        flags.append("text_page_coverage_below_threshold")
    if table_ratio < gate_settings.min_table_unit_coverage:
        flags.append("table_unit_coverage_below_threshold")
    if unit_chunk_ratio < gate_settings.min_unit_chunk_coverage:
        flags.append("unit_chunk_coverage_below_threshold")
    if (
        reading_order_confidence is not None
        and reading_order_confidence < gate_settings.min_reading_order_confidence
    ):
        flags.append("reading_order_confidence_below_threshold")
    if text_ratio < 1.0:
        flags.append("rag_empty_text_page")
    if unchunked_units > 0:
        flags.append("rag_units_without_chunks")
    if embedded_chunk_count < chunk_count or unembedded_units > 0:
        flags.append("rag_chunks_not_embedded")
    if table_ratio < 1.0:
        flags.append("rag_table_without_unit")
    if figure_caption_missing_units:
        flags.append("rag_figure_caption_missing")
    flags = list(dict.fromkeys(flags))

    gate = "accept"
    recommended_action: str | None = None
    if not gate_settings.enabled:
        gate = "disabled"
        flags = []
    elif "rag_empty_text_page" in flags:
        gate = "manual_review" if gate_settings.allow_manual_review else "accept_with_warning"
        recommended_action = "review_parse_ir"
    elif "rag_table_without_unit" in flags:
        gate = "local_rerun" if gate_settings.allow_local_rerun else "accept_with_warning"
        recommended_action = "local_provider_rerun"
    elif "reading_order_confidence_below_threshold" in flags:
        gate = "local_rerun" if gate_settings.allow_local_rerun else "accept_with_warning"
        recommended_action = "local_provider_rerun"
    elif any(
        flag in flags
        for flag in (
            "text_page_coverage_below_threshold",
            "table_unit_coverage_below_threshold",
        )
    ):
        gate = "local_rerun" if gate_settings.allow_local_rerun else "accept_with_warning"
        recommended_action = "local_provider_rerun"
    elif "rag_figure_caption_missing" in flags:
        gate = "accept_with_warning"
        recommended_action = "review_parse_ir"
    elif (
        "unit_chunk_coverage_below_threshold" in flags
        or "rag_units_without_chunks" in flags
    ):
        gate = "accept_with_warning"
        recommended_action = "rechunk_document"
    elif "rag_chunks_not_embedded" in flags:
        gate = "accept_with_warning"
        recommended_action = "reembed_document"
    elif flags:
        gate = "accept_with_warning"

    for unit in units:
        page_start, _ = _runtime_page_span_tuple(unit.get("page_span"))
        if bool(unit.get("should_index_for_rag")) and not unit.get("chunk_ids"):
            signal_keys.add((page_start, "rag_units_without_chunks"))
        if unit.get("chunk_ids") and not bool(unit.get("embedded")):
            signal_keys.add((page_start, "rag_chunks_not_embedded"))
    for unit in figure_caption_missing_units:
        page_start, _ = _runtime_page_span_tuple(unit.get("page_span"))
        signal_keys.add((page_start, "rag_figure_caption_missing"))

    quality = evaluate_parse_quality(tuple(blocks))
    provider_warning_count = len(_runtime_provider_ids(blocks)) if flags else 0
    return {
        "quality_gate": gate,
        "quality_flags": flags,
        "needs_attention": gate not in {"accept", "disabled"} or provider_warning_count > 0,
        "recommended_action": recommended_action,
        "quality_score": float(quality.score),
        "quality_signal_count": len(signal_keys),
        "provider_warning_count": provider_warning_count,
        "text_page_coverage_ratio": round(text_ratio, 6),
        "table_unit_coverage_ratio": round(table_ratio, 6),
        "unit_chunk_coverage_ratio": round(unit_chunk_ratio, 6),
        "embedded_chunk_ratio": round(embedded_chunk_ratio, 6),
    }


def _as_mapping_runtime(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _runtime_float(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if isfinite(parsed) else float(default)


def _runtime_part_observation_payload(part: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "2026-06-part-observation",
        "part_id": str(part.get("part_id") or part.get("parse_unit_id") or ""),
        "job_id": str(part.get("job_id") or "") or None,
        "state": str(part.get("state") or ""),
        "raw_state": str(part.get("raw_state") or ""),
        "quality_signal_count": _safe_int_runtime(part.get("quality_signal_count"), default=0),
        "quality_signal_codes": _runtime_string_list(part.get("quality_signal_codes")),
        "quality_signal_page_numbers": [
            _safe_int_runtime(page_number, default=0)
            for page_number in (part.get("quality_signal_page_numbers") or [])
            if _safe_int_runtime(page_number, default=0) > 0
        ],
        "provider_ids": _runtime_string_list(part.get("provider_ids")),
    }
    for optional_key in (
        "coverage_summary",
        "coverage_gap_pages",
        "coverage_gap_count",
        "rag_coverage_quality",
        "provider_route_plan",
        "local_provider_routing",
        "selected_provider_id",
        "route_status",
    ):
        if optional_key in part and part.get(optional_key) is not None:
            payload[optional_key] = _runtime_json_safe_value(part.get(optional_key))
    return payload


def _runtime_json_safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _runtime_json_safe_value(item)
        for key, item in value.items()
    }


def _runtime_json_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _runtime_json_safe_mapping(value)
    if isinstance(value, tuple):
        return [_runtime_json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [_runtime_json_safe_value(item) for item in value]
    return value


def _runtime_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(float(numerator) / float(denominator), 4)


def _safe_int_runtime(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalized_document_view_types(view_types: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for view_type in view_types:
        value = str(view_type or "").strip().lower()
        if value in {"pages", "lines", "records"} and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _safe_path_segment(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return normalized or "document"


def _filter_jobs_by_since_hours(jobs: Sequence[Any], *, since_hours: float | None) -> tuple[Any, ...]:
    if since_hours is None:
        return tuple(jobs)
    hours = float(since_hours)
    if hours <= 0:
        return ()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered: list[Any] = []
    for job in jobs:
        updated = _parse_datetime(getattr(job, "updated_at", None))
        if updated is None:
            continue
        if updated >= cutoff:
            filtered.append(job)
    return tuple(filtered)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_quota_limit(
    *,
    quota_limits: dict[str, Any] | Any,
    tenant_id: str,
    quota_key: str,
    default_limit_units: int,
) -> int:
    mapping = dict(quota_limits or {})
    candidates = (
        f"{tenant_id}:{quota_key}",
        f"{tenant_id}:*",
        f"*:{quota_key}",
        "*:*",
    )
    for key in candidates:
        if key in mapping:
            try:
                return int(mapping[key])
            except (TypeError, ValueError):
                continue
    return int(default_limit_units)


def _score_chunk(
    *,
    query: str,
    query_embedding: tuple[float, ...] | None,
    chunk: Chunk,
) -> float:
    keyword_score = _keyword_relevance_score(query=query, chunk=chunk)
    vector_score = _vector_relevance_score(query_embedding=query_embedding, chunk=chunk)

    if vector_score is not None:
        combined = (_SEARCH_VECTOR_WEIGHT * vector_score) + (
            _SEARCH_KEYWORD_WEIGHT * keyword_score
        )
    else:
        combined = keyword_score

    if combined <= 0:
        return 0.0

    role = str(chunk.semantic_role or "paragraph").strip().lower()
    role_weight = _SEMANTIC_ROLE_WEIGHTS.get(role, 1.0)
    return combined * role_weight


def _keyword_relevance_score(*, query: str, chunk: Chunk) -> float:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0.0
    text = str(chunk.text or "")
    text_lower = text.lower()
    text_tokens = _tokenize(text)
    if not text_tokens:
        return 0.0

    overlap = 0.0
    for token in query_tokens:
        count = text_tokens.count(token)
        if count > 0:
            overlap += 1.0 + min(count - 1, 2) * 0.25
    if overlap <= 0:
        return 0.0

    token_coverage = overlap / max(len(query_tokens), 1)
    phrase_boost = 0.25 if query.strip().lower() in text_lower else 0.0
    density_boost = min(overlap / max(len(text_tokens), 1), 0.25)
    return min(token_coverage + phrase_boost + density_boost, 1.0)


def _vector_relevance_score(
    *,
    query_embedding: tuple[float, ...] | None,
    chunk: Chunk,
) -> float | None:
    if query_embedding is None or chunk.embedding is None:
        return None
    chunk_embedding = tuple(float(item) for item in chunk.embedding)
    if len(query_embedding) != len(chunk_embedding):
        return None
    similarity = _cosine_similarity(query_embedding, chunk_embedding)
    return max(0.0, float(similarity))


def _resolve_retrieval_mode(
    *,
    query_embedding: tuple[float, ...] | None,
    chunks: Sequence[Chunk],
) -> str:
    if query_embedding is None:
        return "keyword-fallback"
    for chunk in chunks:
        if chunk.embedding is None:
            continue
        if len(chunk.embedding) == len(query_embedding):
            return "hybrid"
    return "keyword-fallback"



def _structure_tags_from_block_metadata(*, metadata: dict[str, Any], semantic_role: str) -> tuple[str, ...]:
    tags = [f"role:{semantic_role}"]
    page_type = str(metadata.get("page_type") or "").strip().lower()
    if page_type:
        tags.append(f"page:{page_type}")
    parser_name = str(metadata.get("parser") or "").strip().lower()
    if parser_name:
        tags.append(f"parser:{parser_name}")
    kind = str(metadata.get("kind") or "").strip().lower()
    if kind:
        tags.append(f"kind:{kind}")
    return tuple(dict.fromkeys(tags))


def _score_structure_entry(
    *,
    query: str,
    text: str,
    semantic_role: str,
    tags: Sequence[str],
) -> float:
    keyword_score = _keyword_match_score(query=query, text=text)
    if keyword_score <= 0.0:
        return 0.0
    role_weight = _SEMANTIC_ROLE_WEIGHTS.get(semantic_role, 1.0)
    tag_bonus = 0.02 * len(tuple(tags))
    return keyword_score * role_weight + tag_bonus


def _keyword_match_score(*, query: str, text: str) -> float:
    query_tokens = _tokenize(query)
    text_tokens = _tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = sum(1 for token in query_tokens if token in text_tokens)
    if overlap <= 0:
        return 0.0
    return overlap / max(len(query_tokens), 1)


def _tokenize(text: str) -> tuple[str, ...]:
    return tuple(
        token.lower()
        for token in _TOKEN_PATTERN.findall(str(text or ""))
        if token.strip()
    )


def _is_task_like_entry(text: str, semantic_role: str, tags: Sequence[str]) -> bool:
    normalized_role = str(semantic_role or "paragraph").strip().lower()
    if normalized_role in {"warning", "caution", "note"}:
        return True
    if any(str(tag).strip().lower().startswith("page:toc") for tag in tags):
        return False
    return _TASK_LIKE_PATTERN.search(str(text or "").strip()) is not None


def _resolve_embedding_tiers(options: dict[str, Any]) -> list[str]:
    index_options = options.get("index") if isinstance(options, dict) else None
    raw_tiers = None
    if isinstance(index_options, dict):
        raw_tiers = index_options.get("embedding_tiers")
    if raw_tiers is None:
        return ["small"]
    if isinstance(raw_tiers, (list, tuple)):
        tiers = [str(item).strip().lower() for item in raw_tiers if str(item).strip()]
    else:
        tiers = [str(raw_tiers).strip().lower()]
    normalized = [tier for tier in tiers if tier in {"small", "large"}]
    return normalized or ["small"]


def _normalize_chunk_index_layer(value: str | None) -> str:
    normalized = str(value or "primary").strip().lower()
    if normalized in {"primary", "high_precision"}:
        return normalized
    return "primary"


def _merged_part_prefix(*, parent_doc_id: str, part_id: str) -> str:
    return f"{parent_doc_id}:merged:{part_id}:"


def _part_id_from_merged_id(*, parent_doc_id: str, item_id: str) -> str:
    prefix = f"{parent_doc_id}:merged:"
    value = str(item_id or "")
    if not value.startswith(prefix):
        return ""
    remainder = value[len(prefix):]
    if ":" not in remainder:
        return ""
    return remainder.split(":", 1)[0]


def _pdf_part_index_manifest(
    *,
    request: ParseRequest,
    chunks: Sequence[Chunk],
    index_version: str,
) -> dict[str, Any]:
    options = request.options or {}
    part_id = str(options.get("part_id") or request.doc_id)
    page_start = _safe_int_runtime(options.get("page_start"), default=1)
    page_end = _safe_int_runtime(options.get("page_end"), default=page_start)
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    return {
        "part_id": part_id,
        "part_doc_id": request.doc_id,
        "source_doc_id": str(options.get("source_doc_id") or options.get("parent_doc_id") or ""),
        "parent_doc_id": str(options.get("parent_doc_id") or options.get("source_doc_id") or ""),
        "parent_job_id": str(options.get("parent_job_id") or ""),
        "source_job_id": str(options.get("source_job_id") or ""),
        "page_range": {
            "start": page_start,
            "end": page_end,
        },
        "page_start": page_start,
        "page_end": page_end,
        "chunk_id_prefix": f"{part_id}:",
        "chunk_ids": chunk_ids,
        "chunk_count": len(chunk_ids),
        "index_version": index_version,
    }


def _normalize_trend_windows_hours(values: Sequence[float] | None) -> tuple[float, ...]:
    if not values:
        return (1.0, 6.0, 24.0)
    normalized: list[float] = []
    for raw in values:
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            continue
        if hours <= 0:
            continue
        rounded = round(hours, 3)
        if rounded in normalized:
            continue
        normalized.append(rounded)
    if not normalized:
        return (1.0, 6.0, 24.0)
    return tuple(sorted(normalized))


def _format_trend_window_label(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)}h"
    return f"{hours:g}h"


def _select_high_precision_chunks(chunks: Sequence[Chunk]) -> tuple[Chunk, ...]:
    selected: list[Chunk] = []
    for chunk in chunks:
        role = str(chunk.semantic_role or "paragraph").strip().lower()
        text = str(chunk.text or "").strip()
        if role in {"title", "body_section", "warning", "caution", "note", "table"}:
            selected.append(chunk)
            continue
        if len(text) >= 120:
            selected.append(chunk)
    return tuple(selected)


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = sqrt(sum(a * a for a in vec_a))
    norm_b = sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
