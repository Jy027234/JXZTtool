from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from .config import ParseCoreSettings
from .contracts import ChunkBuilder, IndexAdapter, JobStore, ParserAdapter, ProductAdapter, TranslationAdapter
from .events import JobEventLogger
from .models import ParseJobState, ParseOutcome, ParseRequest


class ParseRuntime:
    def __init__(
        self,
        *,
        settings: ParseCoreSettings,
        parsers: Sequence[ParserAdapter],
        chunk_builder: ChunkBuilder,
        index: IndexAdapter,
        translator: TranslationAdapter,
        product_adapter: ProductAdapter,
        job_store: JobStore,
        event_logger: JobEventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.parsers = tuple(parsers)
        self.chunk_builder = chunk_builder
        self.index = index
        self.translator = translator
        self.product_adapter = product_adapter
        self.job_store = job_store
        self.event_logger = event_logger or JobEventLogger(settings.runtime.log_path)

    def describe(self) -> dict[str, object]:
        return {
            "project": self.settings.project_name,
            "mode": self.settings.mode,
            "database_url": self.settings.database_url,
            "object_store": self.settings.object_store,
            "index_mode": self.settings.index_mode,
            "runtime": {
                "execution_mode": self.settings.runtime.execution_mode,
                "max_workers": self.settings.runtime.max_workers,
                "poll_interval_ms": self.settings.runtime.poll_interval_ms,
            },
            "translation": {
                "enabled": self.settings.translation_enabled,
                "strategy": self.settings.translation_strategy,
            },
            "product_adapter": self.settings.product_adapter,
            "parsers": [parser.name for parser in self.parsers],
        }

    def submit(self, request: ParseRequest) -> ParseOutcome:
        job = self.start(request)
        return self.execute(job_id=job.job_id)

    def start(self, request: ParseRequest):
        job = self.job_store.create(request)
        self.product_adapter.before_parse(request=request, job=job)

        return job

    def execute(self, *, job_id: str) -> ParseOutcome:
        job = self.job_store.get_job(job_id=job_id)
        if job is None:
            raise LookupError(f"No parse job found for job_id={job_id!r}")
        if job.dead_lettered_at is not None:
            raise RuntimeError(
                f"job {job.job_id} is dead-lettered ({job.failure_reason!r}); refusing to execute"
            )
        attempt = self._increment_attempt(job_id=job.job_id)
        request = ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
        )
        started_at = time.monotonic()
        self.event_logger.log(
            "started",
            job_id=job.job_id,
            doc_id=job.doc_id,
            attempt=attempt,
            file_path=job.file_path,
        )
        try:
            if job.state == ParseJobState.PENDING:
                self.job_store.update_state(job_id=job.job_id, state=ParseJobState.PARSING)
                self.event_logger.log(
                    "state_changed",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    state=ParseJobState.PARSING.value,
                )
            parser = self._resolve_parser(request)
            blocks = tuple(parser.parse(request))

            self.job_store.save_blocks(doc_id=request.doc_id, blocks=blocks)
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.STRUCTURING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.STRUCTURING.value,
            )

            chunks = tuple(self.chunk_builder.build(doc_id=request.doc_id, blocks=blocks))
            self.job_store.save_chunks(doc_id=request.doc_id, chunks=chunks)
            self.job_store.update_state(job_id=job.job_id, state=ParseJobState.EMBEDDING)
            self.event_logger.log(
                "state_changed",
                job_id=job.job_id,
                doc_id=job.doc_id,
                state=ParseJobState.EMBEDDING.value,
            )

            self.index.upsert(doc_id=request.doc_id, chunks=chunks)
            final_job = self.job_store.update_state(job_id=job.job_id, state=ParseJobState.DONE)
            outcome = ParseOutcome(job=final_job, blocks=blocks, chunks=chunks)
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
        except Exception as exc:
            max_attempts = max(1, int(self.settings.runtime.max_attempts))
            if attempt >= max_attempts:
                dead_letter = getattr(self.job_store, "mark_dead_letter", None)
                if callable(dead_letter):
                    failed_job = dead_letter(job_id=job.job_id, reason=str(exc))
                else:
                    failed_job = self.job_store.update_state(
                        job_id=job.job_id,
                        state=ParseJobState.FAILED,
                        failure_reason=str(exc),
                    )
                self.event_logger.log(
                    "dead_letter",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            else:
                failed_job = self.job_store.update_state(
                    job_id=job.job_id,
                    state=ParseJobState.FAILED,
                    failure_reason=str(exc),
                )
                self.event_logger.log(
                    "failed",
                    job_id=job.job_id,
                    doc_id=job.doc_id,
                    attempt=attempt,
                    error=str(exc),
                )
            self.product_adapter.on_failure(request=request, job=failed_job, error=exc)
            raise

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

    def get_job(self, *, job_id: str):
        return self.job_store.get_job(job_id=job_id)

    def get_document(self, *, doc_id: str) -> dict[str, Any]:
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        return {
            "doc_id": doc_id,
            "job": latest_job,
            "blocks": tuple(self.job_store.get_blocks(doc_id=doc_id)),
            "chunks": tuple(self.job_store.get_chunks(doc_id=doc_id)),
        }

    def retry_latest(self, *, doc_id: str) -> ParseOutcome:
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.submit(self._request_from_job(latest_job))

    def restart_latest(self, *, doc_id: str):
        latest_job = self.job_store.get_latest_job(doc_id=doc_id)
        if latest_job is None:
            raise LookupError(f"No parse job found for doc_id={doc_id!r}")
        return self.start(self._request_from_job(latest_job))

    def list_jobs(self, *, doc_id: str | None = None):
        return tuple(self.job_store.list_jobs(doc_id=doc_id))

    def claim_next_job(self):
        return self.job_store.claim_next_job()

    def _request_from_job(self, job):
        return ParseRequest(
            doc_id=job.doc_id,
            file_path=job.file_path,
            media_type=job.media_type,
            options=dict(job.options),
        )

    def _resolve_parser(self, request: ParseRequest) -> ParserAdapter:
        suffix = Path(request.file_path).suffix.lower()
        for parser in self.parsers:
            if parser.supports(media_type=request.media_type, suffix=suffix):
                return parser
        raise LookupError(f"No parser registered for media_type={request.media_type!r}, suffix={suffix!r}")
