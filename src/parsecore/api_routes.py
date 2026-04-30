from __future__ import annotations

import base64
from collections.abc import Callable
import mimetypes
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api_payloads import _batch_success_response, _parse_success_response, _to_payload
from .api_responses import batch_error_response as _batch_error_response
from .api_responses import error_response as _error_response
from .api_support import (
    _estimated_base64_decoded_size,
    _exceeds_upload_limit,
    _file_too_large_detail,
    _max_upload_bytes,
    _trace_id_for_request,
)
from .models import ParseRequest
from .runtime import ParseRuntime, QuotaExceededError


class ApiRoutes:
    def __init__(
        self,
        *,
        api_version: str,
        health_services: Callable[[ParseRuntime], dict[str, bool]],
    ) -> None:
        self.api_version = api_version
        self.health_services = health_services

    def routes(self) -> list[Route]:
        return [
            Route("/health", self.health, methods=["GET"]),
            Route("/parse", self.parse_upload, methods=["POST"]),
            Route("/parse/batch", self.parse_batch, methods=["POST"]),
            Route("/v1/parse", self.parse_upload, methods=["POST"]),
            Route("/v1/runtime", self.describe, methods=["GET"]),
            Route("/v1/parse/batch", self.parse_batch, methods=["POST"]),
            Route("/v1/parse/jobs", self.create_job, methods=["POST"]),
            Route("/v1/parse/jobs", self.list_jobs, methods=["GET"]),
            Route("/v1/parse/quotas/usage", self.quota_usage, methods=["GET"]),
            Route("/v1/parse/metrics", self.runtime_metrics, methods=["GET"]),
            Route("/v1/parse/indexes/metrics", self.index_metrics, methods=["GET"]),
            Route("/v1/parse/prometheus", self.prometheus_metrics, methods=["GET"]),
            Route("/v1/parse/events", self.get_events, methods=["GET"]),
            Route("/v1/parse/dashboard", self.tenant_dashboard, methods=["GET"]),
            Route("/v1/parse/jobs/{job_id}", self.get_job, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}", self.get_document, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/search", self.search_document, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/structure-search", self.search_document_structure, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/tasks/search", self.search_document_tasks, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/reparse", self.reparse_document, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/rechunk", self.rechunk_document, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/re-embed", self.reembed_document, methods=["POST"]),
        ]

    def _quota_error_response(self, request: Request, exc: QuotaExceededError) -> JSONResponse:
        return _error_response(
            request,
            code="quota_exceeded",
            message="Quota exceeded",
            status_code=429,
            detail={
                "tenant_id": exc.tenant_id,
                "quota_key": exc.quota_key,
                "used_units": exc.used_units,
                "requested_units": exc.requested_units,
                "limit_units": exc.limit_units,
                "window_hours": exc.window_hours,
            },
            extra={
                "tenant_id": exc.tenant_id,
                "quota_key": exc.quota_key,
                "used_units": exc.used_units,
                "requested_units": exc.requested_units,
                "limit_units": exc.limit_units,
                "window_hours": exc.window_hours,
            },
        )

    async def health(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        return JSONResponse(
            {
                "status": "ok",
                "version": self.api_version,
                "services": self.health_services(runtime_obj),
            }
        )

    async def describe(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        return JSONResponse(runtime_obj.describe())

    async def create_job(self, request: Request) -> JSONResponse:
        payload = await request.json()
        quota_units_raw = payload.get("quota_units", 1)
        try:
            quota_units = max(1, int(quota_units_raw))
        except (TypeError, ValueError):
            return _error_response(
                request,
                code="invalid_quota_units",
                message="Invalid quota_units",
                status_code=400,
            )
        parse_request = ParseRequest(
            doc_id=str(payload["doc_id"]),
            file_path=str(payload["file_path"]),
            media_type=payload.get("media_type"),
            options=dict(payload.get("options") or {}),
            tenant_id=str(payload.get("tenant_id") or "default"),
            quota_key=str(payload.get("quota_key") or "default"),
            quota_units=quota_units,
        )
        try:
            job = request.app.state.runner.submit(parse_request)
        except QuotaExceededError as exc:
            runtime_obj = request.app.state.runtime
            _record_quota_exceeded_event(request, runtime_obj, exc)
            return self._quota_error_response(request, exc)
        except RuntimeError as exc:
            if str(exc) == "too_many_inflight_jobs":
                runtime_obj = request.app.state.runtime
                _record_inflight_full_event(
                    request,
                    runtime_obj,
                    doc_id=parse_request.doc_id,
                    tenant_id=parse_request.tenant_id,
                    quota_key=parse_request.quota_key,
                )
                max_inflight = getattr(request.app.state.runner, "max_inflight_jobs", None)
                return _error_response(
                    request,
                    code="too_many_inflight_jobs",
                    message="Too many inflight jobs",
                    status_code=429,
                    detail={"max_inflight_jobs": max_inflight},
                    extra={"max_inflight_jobs": max_inflight},
                )
            raise
        return JSONResponse(_to_payload(job), status_code=202)

    async def parse_batch(self, request: Request) -> JSONResponse:
        payload = await request.json()
        file_base64 = str(payload.get("file_base64") or "").strip()
        file_name = str(payload.get("file_name") or "").strip()
        enable_ocr = _coerce_bool(payload.get("enable_ocr"), default=False)
        tenant_id = str(payload.get("tenant_id") or "default")
        quota_key = str(payload.get("quota_key") or "default")
        quota_units_raw = payload.get("quota_units", 1)
        if not file_base64:
            return _batch_error_response(
                request,
                code="missing_file_base64",
                message="Missing file_base64 encoding",
                status_code=400,
            )
        if not file_name:
            return _batch_error_response(
                request,
                code="missing_file_name",
                message="Missing file_name",
                status_code=400,
            )
        runtime_obj: ParseRuntime = request.app.state.runtime
        max_upload_bytes = _max_upload_bytes(runtime_obj)
        estimated_size = _estimated_base64_decoded_size(file_base64)
        if _exceeds_upload_limit(estimated_size, max_upload_bytes):
            return _batch_error_response(
                request,
                code="file_too_large",
                message="File exceeds configured upload limit",
                status_code=413,
                detail=_file_too_large_detail(
                    actual_bytes=estimated_size,
                    limit_bytes=max_upload_bytes,
                ),
            )
        try:
            quota_units = max(1, int(quota_units_raw))
        except (TypeError, ValueError):
            return _batch_error_response(
                request,
                code="invalid_quota_units",
                message="Invalid quota_units",
                status_code=400,
            )
        try:
            file_bytes = base64.b64decode(file_base64, validate=True)
        except (ValueError, TypeError):
            return _batch_error_response(
                request,
                code="invalid_base64_encoding",
                message="Invalid base64 encoding",
                status_code=400,
            )
        if not file_bytes:
            return _batch_error_response(
                request,
                code="empty_file",
                message="Empty file",
                status_code=400,
            )
        if _exceeds_upload_limit(len(file_bytes), max_upload_bytes):
            return _batch_error_response(
                request,
                code="file_too_large",
                message="File exceeds configured upload limit",
                status_code=413,
                detail=_file_too_large_detail(
                    actual_bytes=len(file_bytes),
                    limit_bytes=max_upload_bytes,
                ),
            )

        suffix = Path(file_name).suffix or ".bin"
        media_type = payload.get("media_type")
        options = dict(payload.get("options") or {})
        options["enable_ocr"] = enable_ocr
        tmp_path: str | None = None
        try:
            with NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(file_bytes)
                tmp_path = handle.name
            outcome = runtime_obj.submit(
                ParseRequest(
                    doc_id=str(payload.get("doc_id") or Path(file_name).stem or "batch-doc"),
                    file_path=tmp_path,
                    media_type=str(media_type) if media_type is not None else None,
                    options=options,
                    tenant_id=tenant_id,
                    quota_key=quota_key,
                    quota_units=quota_units,
                )
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, runtime_obj, exc)
            return _batch_error_response(
                request,
                code="quota_exceeded",
                message=(
                    f"quota exceeded for {exc.tenant_id}:{exc.quota_key} "
                    f"used={exc.used_units}, requested={exc.requested_units}, limit={exc.limit_units}"
                ),
                status_code=429,
                detail={
                    "tenant_id": exc.tenant_id,
                    "quota_key": exc.quota_key,
                    "used_units": exc.used_units,
                    "requested_units": exc.requested_units,
                    "limit_units": exc.limit_units,
                    "window_hours": exc.window_hours,
                },
            )
        except Exception as exc:
            return _batch_error_response(
                request,
                code="batch_parse_failed",
                message=str(exc),
                status_code=400 if isinstance(exc, RuntimeError) else 500,
            )
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

        return JSONResponse(_batch_success_response(outcome))

    async def parse_upload(self, request: Request) -> JSONResponse:
        try:
            form = await request.form()
        except Exception as exc:
            return _error_response(
                request,
                code="invalid_multipart",
                message="Invalid multipart form data",
                status_code=400,
                detail=str(exc),
            )

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _error_response(
                request,
                code="file_required",
                message="Multipart field 'file' is required",
                status_code=400,
            )

        file_name = str(getattr(upload, "filename", None) or "unknown")
        media_type = _resolve_media_type(file_name, getattr(upload, "content_type", None))
        enable_ocr = _coerce_bool(form.get("enable_ocr"), default=False)
        try:
            content = await upload.read()
            if not content:
                return _error_response(
                    request,
                    code="empty_file",
                    message="Empty file",
                    status_code=400,
                )
            runtime_obj: ParseRuntime = request.app.state.runtime
            max_upload_bytes = _max_upload_bytes(runtime_obj)
            if _exceeds_upload_limit(len(content), max_upload_bytes):
                return _error_response(
                    request,
                    code="file_too_large",
                    message="File exceeds configured upload limit",
                    status_code=413,
                    detail=_file_too_large_detail(
                        actual_bytes=len(content),
                        limit_bytes=max_upload_bytes,
                    ),
                )

            quota_units_raw = form.get("quota_units", 1)
            try:
                quota_units = max(1, int(quota_units_raw))
            except (TypeError, ValueError):
                return _error_response(
                    request,
                    code="invalid_quota_units",
                    message="Invalid quota_units",
                    status_code=400,
                )

            tenant_id = str(form.get("tenant_id") or "default")
            quota_key = str(form.get("quota_key") or "default")
            doc_id = str(form.get("doc_id") or Path(file_name).stem or "upload-doc")
            options = {"enable_ocr": enable_ocr}
            tmp_path: str | None = None
            try:
                with NamedTemporaryFile(delete=False, suffix=Path(file_name).suffix or ".bin") as handle:
                    handle.write(content)
                    tmp_path = handle.name
                outcome = runtime_obj.submit(
                    ParseRequest(
                        doc_id=doc_id,
                        file_path=tmp_path,
                        media_type=media_type,
                        options=options,
                        tenant_id=tenant_id,
                        quota_key=quota_key,
                        quota_units=quota_units,
                    )
                )
            except QuotaExceededError as exc:
                _record_quota_exceeded_event(request, runtime_obj, exc)
                return self._quota_error_response(request, exc)
            except Exception as exc:
                return _error_response(
                    request,
                    code="parse_failed",
                    message=str(exc),
                    status_code=400 if isinstance(exc, RuntimeError) else 500,
                )
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except FileNotFoundError:
                        pass
        finally:
            close_upload = getattr(upload, "close", None)
            if callable(close_upload):
                await close_upload()

        return JSONResponse(
            _parse_success_response(
                outcome,
                file_name=file_name,
                mime_type=media_type,
                enable_ocr=enable_ocr,
            )
        )

    async def list_jobs(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        doc_id = request.query_params.get("doc_id")
        tenant_id = request.query_params.get("tenant_id")
        quota_key = request.query_params.get("quota_key")
        return JSONResponse(
            {
                "items": [
                    _to_payload(job)
                    for job in runtime_obj.list_jobs(
                        doc_id=doc_id,
                        tenant_id=tenant_id,
                        quota_key=quota_key,
                    )
                ]
            }
        )

    async def quota_usage(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = request.query_params.get("tenant_id")
        since_hours_raw = request.query_params.get("since_hours")
        since_hours: float | None = None
        if since_hours_raw is not None:
            try:
                since_hours = float(since_hours_raw)
            except ValueError:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
            if since_hours <= 0:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
        return JSONResponse(
            _to_payload(
                runtime_obj.quota_usage(
                    tenant_id=tenant_id,
                    since_hours=since_hours,
                )
            )
        )

    async def runtime_metrics(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = request.query_params.get("tenant_id")
        sample_size_raw = request.query_params.get("sample_size", "200")
        since_hours_raw = request.query_params.get("since_hours")
        try:
            sample_size = max(1, int(sample_size_raw))
        except ValueError:
            return _error_response(request, code="invalid_sample_size", message="Invalid sample_size", status_code=400)
        since_hours: float | None = None
        if since_hours_raw is not None:
            try:
                since_hours = float(since_hours_raw)
            except ValueError:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
            if since_hours <= 0:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
        return JSONResponse(
            _to_payload(
                runtime_obj.runtime_metrics(
                    tenant_id=tenant_id,
                    sample_size=sample_size,
                    since_hours=since_hours,
                )
            )
        )

    async def tenant_dashboard(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = request.query_params.get("tenant_id")
        sample_size_raw = request.query_params.get("sample_size", "200")
        recent_limit_raw = request.query_params.get("recent_limit", "5")
        since_hours_raw = request.query_params.get("since_hours")
        try:
            sample_size = max(1, int(sample_size_raw))
        except ValueError:
            return _error_response(request, code="invalid_sample_size", message="Invalid sample_size", status_code=400)
        try:
            recent_limit = max(1, int(recent_limit_raw))
        except ValueError:
            return _error_response(request, code="invalid_recent_limit", message="Invalid recent_limit", status_code=400)
        since_hours: float | None = None
        if since_hours_raw is not None:
            try:
                since_hours = float(since_hours_raw)
            except ValueError:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
            if since_hours <= 0:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
        return JSONResponse(
            _to_payload(
                runtime_obj.tenant_dashboard(
                    tenant_id=tenant_id,
                    sample_size=sample_size,
                    recent_limit=recent_limit,
                    since_hours=since_hours,
                )
            )
        )

    async def prometheus_metrics(self, request: Request) -> Response:
        """Return Prometheus-format metrics."""
        runtime_obj: ParseRuntime = request.app.state.runtime
        prometheus_text = runtime_obj.event_aggregator.get_prometheus_metrics()
        return Response(content=prometheus_text, media_type="text/plain; charset=utf-8")

    async def get_events(self, request: Request) -> JSONResponse:
        """Query recent observability events."""
        runtime_obj: ParseRuntime = request.app.state.runtime
        limit_raw = request.query_params.get("limit", "100")
        event_type_filter = request.query_params.get("event_type")
        tenant_id_filter = request.query_params.get("tenant_id")
        try:
            limit = max(1, min(1000, int(limit_raw)))
        except (TypeError, ValueError):
            return _error_response(request, code="invalid_limit", message="Invalid limit", status_code=400)
        events = runtime_obj.event_aggregator.get_events(
            limit=limit,
            event_type_filter=event_type_filter,
            tenant_id_filter=tenant_id_filter,
        )
        counters = runtime_obj.event_aggregator.get_counters(
            event_type_filter=event_type_filter,
            tenant_id_filter=tenant_id_filter,
        )
        return JSONResponse({
            "events": events,
            "counters": counters,
        })

    async def get_job(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        job = runtime_obj.get_job(job_id=request.path_params["job_id"])
        if job is None:
            return _error_response(request, code="job_not_found", message="Parse job not found", status_code=404)
        return JSONResponse(_to_payload(job))

    async def get_document(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        return JSONResponse(_to_payload(snapshot))

    async def search_document(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        doc_id = request.path_params["doc_id"]
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=doc_id, tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        query = str(request.query_params.get("q") or "").strip()
        if not query:
            return _error_response(request, code="query_required", message="Query parameter q is required", status_code=400)
        limit_raw = request.query_params.get("limit", "10")
        try:
            limit = max(1, int(limit_raw))
        except ValueError:
            return _error_response(request, code="invalid_limit", message="Invalid limit", status_code=400)
        roles = request.query_params.getlist("role")
        index_layer = str(request.query_params.get("index_layer") or "primary").strip().lower()
        if index_layer not in {"primary", "high_precision"}:
            return _error_response(request, code="invalid_index_layer", message="Invalid index_layer", status_code=400)
        hits, retrieval_mode = runtime_obj.search_document_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=roles,
            tenant_id=tenant_id,
            index_layer=index_layer,
        )
        return JSONResponse(
            {
                "doc_id": doc_id,
                "query": query,
                "limit": limit,
                "roles": roles,
                "index_layer": index_layer,
                "retrieval_mode": retrieval_mode,
                "items": _to_payload(hits),
            }
        )

    async def search_document_structure(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        doc_id = request.path_params["doc_id"]
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=doc_id, tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        query = str(request.query_params.get("q") or "").strip()
        if not query:
            return _error_response(request, code="query_required", message="Query parameter q is required", status_code=400)
        limit_raw = request.query_params.get("limit", "10")
        try:
            limit = max(1, int(limit_raw))
        except ValueError:
            return _error_response(request, code="invalid_limit", message="Invalid limit", status_code=400)
        roles = request.query_params.getlist("role")
        tags = request.query_params.getlist("tag")
        hits, retrieval_mode = runtime_obj.search_document_structure_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            semantic_roles=roles,
            structure_tags=tags,
            tenant_id=tenant_id,
        )
        return JSONResponse(
            {
                "doc_id": doc_id,
                "query": query,
                "limit": limit,
                "roles": roles,
                "tags": tags,
                "retrieval_mode": retrieval_mode,
                "items": _to_payload(hits),
            }
        )

    async def search_document_tasks(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        doc_id = request.path_params["doc_id"]
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=doc_id, tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        query = str(request.query_params.get("q") or "").strip()
        if not query:
            return _error_response(request, code="query_required", message="Query parameter q is required", status_code=400)
        limit_raw = request.query_params.get("limit", "10")
        try:
            limit = max(1, int(limit_raw))
        except ValueError:
            return _error_response(request, code="invalid_limit", message="Invalid limit", status_code=400)
        hits, retrieval_mode = runtime_obj.search_document_tasks_with_mode(
            doc_id=doc_id,
            query=query,
            limit=limit,
            tenant_id=tenant_id,
        )
        return JSONResponse(
            {
                "doc_id": doc_id,
                "query": query,
                "limit": limit,
                "retrieval_mode": retrieval_mode,
                "items": _to_payload(hits),
            }
        )

    async def index_metrics(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = request.query_params.get("tenant_id")
        since_hours_raw = request.query_params.get("since_hours")
        trend_windows_raw = request.query_params.getlist("trend_window_hours")
        since_hours: float | None = None
        if since_hours_raw is not None:
            try:
                since_hours = float(since_hours_raw)
            except ValueError:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
            if since_hours <= 0:
                return _error_response(request, code="invalid_since_hours", message="Invalid since_hours", status_code=400)
        trend_windows_hours: list[float] | None = None
        if trend_windows_raw:
            trend_windows_hours = []
            for raw in trend_windows_raw:
                try:
                    hours = float(raw)
                except ValueError:
                    return _error_response(
                        request,
                        code="invalid_trend_window_hours",
                        message="Invalid trend_window_hours",
                        status_code=400,
                    )
                if hours <= 0:
                    return _error_response(
                        request,
                        code="invalid_trend_window_hours",
                        message="Invalid trend_window_hours",
                        status_code=400,
                    )
                trend_windows_hours.append(hours)
        return JSONResponse(
            _to_payload(
                runtime_obj.index_metrics(
                    tenant_id=tenant_id,
                    since_hours=since_hours,
                    trend_windows_hours=trend_windows_hours,
                )
            )
        )

    async def reparse_document(self, request: Request) -> JSONResponse:
        try:
            tenant_id = str(request.query_params.get("tenant_id") or "default")
            job = request.app.state.runner.restart_latest(
                doc_id=request.path_params["doc_id"],
                tenant_id=tenant_id,
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        except LookupError:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        except RuntimeError as exc:
            if str(exc) == "too_many_inflight_jobs":
                _record_inflight_full_event(
                    request,
                    request.app.state.runtime,
                    doc_id=request.path_params["doc_id"],
                    tenant_id=tenant_id,
                )
                max_inflight = getattr(request.app.state.runner, "max_inflight_jobs", None)
                return _error_response(
                    request,
                    code="too_many_inflight_jobs",
                    message="Too many inflight jobs",
                    status_code=429,
                    detail={"max_inflight_jobs": max_inflight},
                    extra={"max_inflight_jobs": max_inflight},
                )
            raise
        return JSONResponse(_to_payload(job), status_code=202)

    async def rechunk_document(self, request: Request) -> JSONResponse:
        try:
            tenant_id = str(request.query_params.get("tenant_id") or "default")
            job = request.app.state.runner.rechunk_latest(
                doc_id=request.path_params["doc_id"],
                tenant_id=tenant_id,
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        except LookupError:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        except RuntimeError as exc:
            if str(exc) == "too_many_inflight_jobs":
                _record_inflight_full_event(
                    request,
                    request.app.state.runtime,
                    doc_id=request.path_params["doc_id"],
                    tenant_id=tenant_id,
                )
                max_inflight = getattr(request.app.state.runner, "max_inflight_jobs", None)
                return _error_response(
                    request,
                    code="too_many_inflight_jobs",
                    message="Too many inflight jobs",
                    status_code=429,
                    detail={"max_inflight_jobs": max_inflight},
                    extra={"max_inflight_jobs": max_inflight},
                )
            raise
        return JSONResponse(_to_payload(job), status_code=202)

    async def reembed_document(self, request: Request) -> JSONResponse:
        try:
            tenant_id = str(request.query_params.get("tenant_id") or "default")
            job = request.app.state.runner.reembed_latest(
                doc_id=request.path_params["doc_id"],
                tenant_id=tenant_id,
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        except LookupError:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        except RuntimeError as exc:
            if str(exc) == "too_many_inflight_jobs":
                _record_inflight_full_event(
                    request,
                    request.app.state.runtime,
                    doc_id=request.path_params["doc_id"],
                    tenant_id=tenant_id,
                )
                max_inflight = getattr(request.app.state.runner, "max_inflight_jobs", None)
                return _error_response(
                    request,
                    code="too_many_inflight_jobs",
                    message="Too many inflight jobs",
                    status_code=429,
                    detail={"max_inflight_jobs": max_inflight},
                    extra={"max_inflight_jobs": max_inflight},
                )
            raise
        return JSONResponse(_to_payload(job), status_code=202)


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_media_type(file_name: str, provided: str | None) -> str | None:
    if provided and provided not in {"application/octet-stream", ""}:
        return provided
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed


def _record_quota_exceeded_event(request: Request, runtime_obj: ParseRuntime, exc: QuotaExceededError) -> None:
    runtime_obj.event_aggregator.record_event(
        "quota_exceeded",
        tenant_id=exc.tenant_id,
        quota_key=exc.quota_key,
        details={
            "used_units": exc.used_units,
            "requested_units": exc.requested_units,
            "limit_units": exc.limit_units,
            "trace_id": _trace_id_for_request(request),
        },
    )


def _record_inflight_full_event(
    request: Request,
    runtime_obj: ParseRuntime,
    *,
    doc_id: str,
    tenant_id: str,
    quota_key: str = "*",
) -> None:
    runtime_obj.event_aggregator.record_event(
        "too_many_inflight_jobs",
        tenant_id=tenant_id,
        quota_key=quota_key,
        doc_id=doc_id,
        details={"trace_id": _trace_id_for_request(request)},
    )
