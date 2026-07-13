from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import threading
from starlette.applications import Starlette

from .api_health import health_service_details as _base_health_service_details
from .api_health import health_services as _base_health_services
from .api_health import is_ocr_service_available as _is_ocr_service_available
from .api_payloads import _project_pages
from .api_routes import ApiRoutes
from .api_support import (
    ApiKeyMiddleware,
    TraceIdMiddleware,
    _resolve_required_api_key,
    _resolve_staged_upload_api_key,
)
from .bootstrap import build_runtime
from .models import ParseJob, ParseJobState, ParseOutcome, ParseRequest
from .runtime import ParseRuntime


def _resolve_api_version() -> str:
    try:
        return package_version("parsecore-starter")
    except PackageNotFoundError:
        return "0.1.0"


API_VERSION = _resolve_api_version()
_PART_JOB_KIND = "pdf_part"


class BackgroundParseRunner:
    def __init__(
        self,
        runtime: ParseRuntime,
        *,
        max_workers: int = 2,
        max_inflight_jobs: int = 0,
    ) -> None:
        self.runtime = runtime
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="parsecore")
        self.inflight: dict[str, Future[ParseOutcome]] = {}
        self.queued_job_ids: list[str] = []
        self._lock = threading.Lock()
        configured_limit = int(max_inflight_jobs)
        if configured_limit > 0:
            self.max_inflight_jobs = configured_limit
        else:
            # Keep a modest queue above worker count to absorb short bursts.
            self.max_inflight_jobs = max(1, int(max_workers) * 4)

    def _assert_capacity(self) -> None:
        if len(self.inflight) >= self.max_inflight_jobs:
            raise RuntimeError("too_many_inflight_jobs")

    def submit(self, request: ParseRequest) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.start(request)
        self._submit_existing_job(job)
        return job

    def restart_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.restart_latest(doc_id=doc_id, tenant_id=tenant_id)
        self._submit_existing_job(job)
        return job

    def rechunk_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.rechunk_latest(doc_id=doc_id, tenant_id=tenant_id)
        self._submit_existing_job(job)
        return job

    def reembed_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.reembed_latest(doc_id=doc_id, tenant_id=tenant_id)
        self._submit_existing_job(job)
        return job

    def plan_pdf_parts(self, **kwargs) -> dict[str, object]:
        result = self.runtime.start_pdf_part_jobs(**kwargs)
        for job in tuple(result.get("part_jobs", ())):
            self._submit_existing_job(job, allow_queue=True)
        result["submitted_job_ids"] = list(self.inflight)
        result["queued_job_ids"] = list(self.queued_job_ids)
        return result

    def rerun_pdf_part(self, **kwargs) -> dict[str, object]:
        result = self.runtime.rerun_pdf_part(**kwargs)
        job = result.get("job")
        if isinstance(job, ParseJob):
            self._submit_existing_job(job, allow_queue=True)
        return result

    def rerun_pdf_parts(self, **kwargs) -> dict[str, object]:
        result = self.runtime.rerun_pdf_parts(**kwargs)
        for item in tuple(result.get("submitted", ())):
            if isinstance(item, dict) and isinstance(item.get("job"), ParseJob):
                self._submit_existing_job(item["job"], allow_queue=True)
        result["submitted_job_ids"] = list(self.inflight)
        result["queued_job_ids"] = list(self.queued_job_ids)
        return result

    def cancel_pdf_part(self, **kwargs) -> dict[str, object]:
        target = self.runtime.latest_pdf_part_job(
            doc_id=str(kwargs.get("doc_id") or ""),
            part_id=str(kwargs.get("part_id") or ""),
            tenant_id=kwargs.get("tenant_id"),
        )
        if isinstance(target, ParseJob):
            with self._lock:
                if target.job_id in self.inflight:
                    return {
                        "doc_id": kwargs.get("doc_id"),
                        "tenant_id": kwargs.get("tenant_id") or target.tenant_id,
                        "part_id": kwargs.get("part_id"),
                        "cancelled": False,
                        "job": target,
                        "state": target.state.value,
                        "message": "part is already running or completed",
                    }
                was_queued = target.job_id in self.queued_job_ids
                if was_queued:
                    self.queued_job_ids = [job_id for job_id in self.queued_job_ids if job_id != target.job_id]
                result = self.runtime.cancel_pdf_part(**kwargs)
                if was_queued and result.get("cancelled"):
                    result["message"] = "cancelled"
                return result
        result = self.runtime.cancel_pdf_part(**kwargs)
        job = result.get("job")
        if isinstance(job, ParseJob):
            with self._lock:
                if job.job_id in self.queued_job_ids:
                    self.queued_job_ids = [job_id for job_id in self.queued_job_ids if job_id != job.job_id]
                    result["cancelled"] = True
                    result["message"] = "cancelled"
        return result

    def cancel_job(self, *, job_id: str) -> dict[str, object]:
        with self._lock:
            self.queued_job_ids = [queued_id for queued_id in self.queued_job_ids if queued_id != job_id]
            future = self.inflight.get(job_id)
            if future is not None and future.cancel():
                self.inflight.pop(job_id, None)
        return self.runtime.cancel_job(job_id=job_id)

    def _submit_existing_job(self, job: ParseJob, *, allow_queue: bool = False) -> None:
        with self._lock:
            if job.state != ParseJobState.PENDING:
                return
            if len(self.inflight) >= self.max_inflight_jobs or not self._can_start_job(job):
                if allow_queue:
                    self.queued_job_ids.append(job.job_id)
                    return
                raise RuntimeError("too_many_inflight_jobs")
            future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
            self.inflight[job.job_id] = future
            future.add_done_callback(lambda _future, job_id=job.job_id: self._job_done(job_id))

    def _job_done(self, job_id: str) -> None:
        with self._lock:
            self.inflight.pop(job_id, None)
            while self.queued_job_ids and len(self.inflight) < self.max_inflight_jobs:
                next_index = self._next_startable_queued_index()
                if next_index is None:
                    break
                next_job_id = self.queued_job_ids.pop(next_index)
                future = self.executor.submit(self.runtime.execute, job_id=next_job_id)
                self.inflight[next_job_id] = future
                future.add_done_callback(lambda _future, queued_id=next_job_id: self._job_done(queued_id))

    def _next_startable_queued_index(self) -> int | None:
        for index, job_id in enumerate(self.queued_job_ids):
            job = self.runtime.get_job(job_id=job_id)
            if job is None:
                continue
            if job.state != ParseJobState.PENDING:
                continue
            if self._can_start_job(job):
                return index
        return None

    def _can_start_job(self, job: ParseJob) -> bool:
        if job.state != ParseJobState.PENDING:
            return False
        options = job.options or {}
        if str(options.get("job_kind") or "") != _PART_JOB_KIND:
            return True
        limit = _part_active_limit(job)
        if limit <= 0:
            return True
        source_doc_id = str(options.get("source_doc_id") or options.get("parent_doc_id") or "")
        parent_job_id = str(options.get("parent_job_id") or "")
        active = 0
        for active_job_id in self.inflight:
            active_job = self.runtime.get_job(job_id=active_job_id)
            active_options = (active_job.options if active_job is not None else {}) or {}
            if str(active_options.get("job_kind") or "") != _PART_JOB_KIND:
                continue
            if str(active_options.get("source_doc_id") or active_options.get("parent_doc_id") or "") != source_doc_id:
                continue
            if parent_job_id and str(active_options.get("parent_job_id") or "") != parent_job_id:
                continue
            active += 1
        return active < limit

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)


class QueueSubmissionRunner:
    def __init__(self, runtime: ParseRuntime) -> None:
        self.runtime = runtime

    def submit(self, request: ParseRequest) -> ParseJob:
        return self.runtime.start(request)

    def restart_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        return self.runtime.restart_latest(doc_id=doc_id, tenant_id=tenant_id)

    def rechunk_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        return self.runtime.rechunk_latest(doc_id=doc_id, tenant_id=tenant_id)

    def reembed_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        return self.runtime.reembed_latest(doc_id=doc_id, tenant_id=tenant_id)

    def plan_pdf_parts(self, **kwargs) -> dict[str, object]:
        return self.runtime.start_pdf_part_jobs(**kwargs)

    def rerun_pdf_part(self, **kwargs) -> dict[str, object]:
        return self.runtime.rerun_pdf_part(**kwargs)

    def rerun_pdf_parts(self, **kwargs) -> dict[str, object]:
        return self.runtime.rerun_pdf_parts(**kwargs)

    def cancel_pdf_part(self, **kwargs) -> dict[str, object]:
        return self.runtime.cancel_pdf_part(**kwargs)

    def cancel_job(self, *, job_id: str) -> dict[str, object]:
        return self.runtime.cancel_job(job_id=job_id)

    def shutdown(self) -> None:
        return None


def create_app(config_path: str | Path = "parsecore.toml") -> Starlette:
    runtime = build_runtime(config_path)
    required_api_key = _resolve_required_api_key(runtime)
    staged_upload_api_key = _resolve_staged_upload_api_key(runtime)

    if runtime.settings.runtime.execution_mode == "queue-worker":
        runner: BackgroundParseRunner | QueueSubmissionRunner = QueueSubmissionRunner(runtime)
    else:
        runner = BackgroundParseRunner(
            runtime,
            max_workers=runtime.settings.runtime.max_workers,
            max_inflight_jobs=runtime.settings.runtime.max_inflight_jobs,
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.runtime = runtime
        app.state.runner = runner
        yield
        runner.shutdown()
        close_job_store = getattr(runtime.job_store, "close", None)
        if callable(close_job_store):
            close_job_store()
        close_index = getattr(runtime.index, "close", None)
        if callable(close_index):
            close_index()

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=ApiRoutes(
            api_version=API_VERSION,
            health_services=_health_services,
            health_service_details=_health_service_details,
        ).routes(),
    )

    app.add_middleware(TraceIdMiddleware)
    if required_api_key is not None:
        app.add_middleware(ApiKeyMiddleware, api_key=required_api_key)
    app.state.upload_bridge_api_key = staged_upload_api_key
    return app


def _health_services(runtime: ParseRuntime) -> dict[str, bool]:
    return _base_health_services(runtime, ocr_probe=_is_ocr_service_available)


def _health_service_details(runtime: ParseRuntime) -> dict[str, dict[str, object]]:
    return _base_health_service_details(runtime, ocr_probe=_is_ocr_service_available)


def _part_active_limit(job: ParseJob) -> int:
    try:
        return max(0, int((job.options or {}).get("max_active_parts_per_doc") or 0))
    except (TypeError, ValueError):
        return 0
