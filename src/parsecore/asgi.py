from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
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
from .models import ParseJob, ParseOutcome, ParseRequest
from .runtime import ParseRuntime


def _resolve_api_version() -> str:
    try:
        return package_version("parsecore-starter")
    except PackageNotFoundError:
        return "0.1.0"


API_VERSION = _resolve_api_version()


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
        future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
        self.inflight[job.job_id] = future
        future.add_done_callback(lambda _: self.inflight.pop(job.job_id, None))
        return job

    def restart_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.restart_latest(doc_id=doc_id, tenant_id=tenant_id)
        future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
        self.inflight[job.job_id] = future
        future.add_done_callback(lambda _: self.inflight.pop(job.job_id, None))
        return job

    def rechunk_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.rechunk_latest(doc_id=doc_id, tenant_id=tenant_id)
        future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
        self.inflight[job.job_id] = future
        future.add_done_callback(lambda _: self.inflight.pop(job.job_id, None))
        return job

    def reembed_latest(self, *, doc_id: str, tenant_id: str | None = None) -> ParseJob:
        self._assert_capacity()
        job = self.runtime.reembed_latest(doc_id=doc_id, tenant_id=tenant_id)
        future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
        self.inflight[job.job_id] = future
        future.add_done_callback(lambda _: self.inflight.pop(job.job_id, None))
        return job

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
