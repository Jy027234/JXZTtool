from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .bootstrap import build_runtime
from .models import ParseJob, ParseOutcome, ParseRequest
from .runtime import ParseRuntime


class BackgroundParseRunner:
    def __init__(self, runtime: ParseRuntime, *, max_workers: int = 2) -> None:
        self.runtime = runtime
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="parsecore")
        self.inflight: dict[str, Future[ParseOutcome]] = {}

    def submit(self, request: ParseRequest) -> ParseJob:
        job = self.runtime.start(request)
        future = self.executor.submit(self.runtime.execute, job_id=job.job_id)
        self.inflight[job.job_id] = future
        future.add_done_callback(lambda _: self.inflight.pop(job.job_id, None))
        return job

    def restart_latest(self, *, doc_id: str) -> ParseJob:
        job = self.runtime.restart_latest(doc_id=doc_id)
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

    def restart_latest(self, *, doc_id: str) -> ParseJob:
        return self.runtime.restart_latest(doc_id=doc_id)

    def shutdown(self) -> None:
        return None


def create_app(config_path: str | Path = "parsecore.toml") -> Starlette:
    runtime = build_runtime(config_path)
    if runtime.settings.runtime.execution_mode == "queue-worker":
        runner: BackgroundParseRunner | QueueSubmissionRunner = QueueSubmissionRunner(runtime)
    else:
        runner = BackgroundParseRunner(runtime, max_workers=runtime.settings.runtime.max_workers)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.runtime = runtime
        app.state.runner = runner
        yield
        runner.shutdown()

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def describe(request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        return JSONResponse(runtime_obj.describe())

    async def create_job(request: Request) -> JSONResponse:
        payload = await request.json()
        parse_request = ParseRequest(
            doc_id=str(payload["doc_id"]),
            file_path=str(payload["file_path"]),
            media_type=payload.get("media_type"),
            options=dict(payload.get("options") or {}),
        )
        job = request.app.state.runner.submit(parse_request)
        return JSONResponse(_to_payload(job), status_code=202)

    async def list_jobs(request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        doc_id = request.query_params.get("doc_id")
        return JSONResponse({"items": [_to_payload(job) for job in runtime_obj.list_jobs(doc_id=doc_id)]})

    async def get_job(request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        job = runtime_obj.get_job(job_id=request.path_params["job_id"])
        if job is None:
            return JSONResponse({"error": "job_not_found"}, status_code=404)
        return JSONResponse(_to_payload(job))

    async def get_document(request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"])
        if snapshot["job"] is None:
            return JSONResponse({"error": "document_not_found"}, status_code=404)
        return JSONResponse(_to_payload(snapshot))

    async def reparse_document(request: Request) -> JSONResponse:
        try:
            job = request.app.state.runner.restart_latest(doc_id=request.path_params["doc_id"])
        except LookupError:
            return JSONResponse({"error": "document_not_found"}, status_code=404)
        return JSONResponse(_to_payload(job), status_code=202)

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/runtime", describe, methods=["GET"]),
            Route("/v1/parse/jobs", create_job, methods=["POST"]),
            Route("/v1/parse/jobs", list_jobs, methods=["GET"]),
            Route("/v1/parse/jobs/{job_id}", get_job, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}", get_document, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/reparse", reparse_document, methods=["POST"]),
        ],
    )


def _to_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_payload(item) for item in value]
    return value