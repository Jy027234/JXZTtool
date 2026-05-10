from __future__ import annotations

import base64
from collections.abc import Callable
import mimetypes
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Any
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .api_payloads import (
    _batch_success_response,
    _document_projection,
    _document_quality_projection,
    _document_records_projection,
    _document_view_rows,
    _parse_success_response,
    _to_payload,
)
from .api_responses import batch_error_response as _batch_error_response
from .api_responses import error_response as _error_response
from .api_support import (
    _api_key_unauthorized_response,
    _document_too_large_for_sync_detail,
    _estimated_base64_decoded_size,
    _extract_api_key,
    _exceeds_upload_limit,
    _file_too_large_detail,
    _max_upload_bytes,
    _max_staged_upload_bytes,
    _trace_id_for_request,
)
from .export_jobs import create_export_package, export_file_path, load_export_manifest
from .exports import EXPORT_DATASETS, EXPORT_FORMATS, export_structured_projection
from .models import ParseRequest
from .parts import PART_STATE_FILTERS, document_parts_projection
from .pdf_parts import detect_pdf_page_count
from .profiles import describe_parse_profiles, resolve_parse_profile
from .runtime import ParseRuntime, QuotaExceededError


_SYNC_ASYNC_RECOMMENDATION_MESSAGE = (
    "Document is too large for synchronous parsing; use the asynchronous upload/job flow"
)


class ApiRoutes:
    def __init__(
        self,
        *,
        api_version: str,
        health_services: Callable[[ParseRuntime], dict[str, bool]],
        health_service_details: Callable[[ParseRuntime], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.api_version = api_version
        self.health_services = health_services
        self.health_service_details = health_service_details

    def routes(self) -> list[Route]:
        return [
            Route("/health", self.health, methods=["GET"]),
            Route("/parse", self.parse_upload, methods=["POST"]),
            Route("/parse/uploads", self.stage_upload, methods=["POST"]),
            Route("/parse/batch", self.parse_batch, methods=["POST"]),
            Route("/v1/parse", self.parse_upload, methods=["POST"]),
            Route("/v1/runtime", self.describe, methods=["GET"]),
            Route("/v1/parse/profiles", self.parse_profiles, methods=["GET"]),
            Route("/v1/parse/uploads", self.stage_upload, methods=["POST"]),
            Route("/v1/parse/batch", self.parse_batch, methods=["POST"]),
            Route("/v1/parse/jobs", self.create_job, methods=["POST"]),
            Route("/v1/parse/jobs", self.list_jobs, methods=["GET"]),
            Route("/v1/parse/export-jobs/{export_id}", self.get_export_job, methods=["GET"]),
            Route("/v1/parse/export-jobs/{export_id}/download", self.download_export_file, methods=["GET"]),
            Route("/v1/parse/quotas/usage", self.quota_usage, methods=["GET"]),
            Route("/v1/parse/metrics", self.runtime_metrics, methods=["GET"]),
            Route("/v1/parse/indexes/metrics", self.index_metrics, methods=["GET"]),
            Route("/v1/parse/prometheus", self.prometheus_metrics, methods=["GET"]),
            Route("/v1/parse/events", self.get_events, methods=["GET"]),
            Route("/v1/parse/dashboard", self.tenant_dashboard, methods=["GET"]),
            Route("/v1/parse/jobs/{job_id}", self.get_job, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/quality", self.get_document_quality, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/records", self.get_document_records, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/exports", self.export_document, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/export-jobs", self.create_export_job, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/parts", self.get_document_parts, methods=["GET"]),
            Route("/v1/parse/documents/{doc_id}/parts/plan", self.plan_document_parts, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/parts/rerun", self.rerun_document_parts, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/parts/{part_id}/cancel", self.cancel_document_part, methods=["POST"]),
            Route("/v1/parse/documents/{doc_id}/parts/{part_id}/rerun", self.rerun_document_part, methods=["POST"]),
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
        services = self.health_services(runtime_obj)
        payload: dict[str, Any] = {
            "status": "ok",
            "version": self.api_version,
            "services": services,
        }
        if self.health_service_details is not None:
            payload["service_details"] = self.health_service_details(runtime_obj)
        return JSONResponse(
            payload
        )

    async def describe(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        return JSONResponse(runtime_obj.describe())

    async def parse_profiles(self, request: Request) -> JSONResponse:
        return JSONResponse(describe_parse_profiles())

    async def create_job(self, request: Request) -> JSONResponse:
        payload = await request.json()
        doc_id = str(payload.get("doc_id") or "").strip()
        if not doc_id:
            return _error_response(
                request,
                code="missing_doc_id",
                message="Missing doc_id",
                status_code=400,
            )
        runtime_obj: ParseRuntime = request.app.state.runtime
        try:
            file_path = _resolve_api_file_path(runtime_obj, payload.get("file_path"))
        except ValueError as exc:
            code = str(exc) or "invalid_file_path"
            if code == "missing_file_path":
                message = "Missing file_path"
            else:
                code = "invalid_file_path"
                message = "Invalid file_path"
            return _error_response(
                request,
                code=code,
                message=message,
                status_code=400,
            )
        except PermissionError:
            return _error_response(
                request,
                code="file_path_not_allowed",
                message="file_path must be inside the configured local object_store",
                status_code=403,
                detail={
                    "allow_external_file_paths": False,
                    "object_store": runtime_obj.settings.object_store,
                },
            )
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
        raw_options = dict(payload.get("options") or {})
        file_name = str(payload.get("file_name") or raw_options.get("file_name") or Path(file_path).name)
        media_type = _resolve_media_type(file_name, payload.get("media_type"))
        options, _profile = _profile_options(
            raw_options,
            media_type=media_type,
            file_name=file_name,
            file_size_bytes=_safe_file_size(file_path),
            page_count=_detect_pdf_page_count_for_profile(file_path, media_type=media_type),
            requested_profile=payload.get("profile"),
        )
        options.setdefault("file_name", file_name)
        parse_request = ParseRequest(
            doc_id=doc_id,
            file_path=file_path,
            media_type=media_type,
            options=options,
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
        raw_options = dict(payload.get("options") or {})
        media_type = _resolve_media_type(file_name, payload.get("media_type"))
        force_sync = _force_sync_requested(payload, raw_options)
        estimated_size = _estimated_base64_decoded_size(file_base64)
        estimated_profile = _resolve_request_profile(
            raw_options,
            media_type=media_type,
            file_name=file_name,
            file_size_bytes=estimated_size,
            requested_profile=payload.get("profile"),
        )
        if _exceeds_upload_limit(estimated_size, max_upload_bytes):
            return _batch_error_response(
                request,
                code="document_too_large_for_sync",
                message=_SYNC_ASYNC_RECOMMENDATION_MESSAGE,
                status_code=413,
                detail=_document_too_large_for_sync_detail(
                    actual_bytes=estimated_size,
                    limit_bytes=max_upload_bytes,
                    profile=estimated_profile,
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
                code="document_too_large_for_sync",
                message=_SYNC_ASYNC_RECOMMENDATION_MESSAGE,
                status_code=413,
                detail=_document_too_large_for_sync_detail(
                    actual_bytes=len(file_bytes),
                    limit_bytes=max_upload_bytes,
                    profile=_resolve_request_profile(
                        raw_options,
                        media_type=media_type,
                        file_name=file_name,
                        file_size_bytes=len(file_bytes),
                        requested_profile=payload.get("profile"),
                    ),
                ),
            )

        options, profile = _profile_options(
            raw_options,
            media_type=media_type,
            file_name=file_name,
            file_size_bytes=len(file_bytes),
            requested_profile=payload.get("profile"),
        )
        if bool(profile.get("recommended_async")) and not force_sync:
            return _batch_error_response(
                request,
                code="document_too_large_for_sync",
                message=_SYNC_ASYNC_RECOMMENDATION_MESSAGE,
                status_code=413,
                detail=_document_too_large_for_sync_detail(
                    actual_bytes=len(file_bytes),
                    limit_bytes=max_upload_bytes,
                    profile=profile,
                ),
            )
        options["enable_ocr"] = enable_ocr
        options["file_name"] = file_name
        if force_sync:
            options["force_sync"] = True
        submission_path: str | None = None
        delete_after_submit = False
        try:
            submission_path, delete_after_submit = _persist_upload_file(
                runtime_obj,
                content=file_bytes,
                file_name=file_name,
                doc_id=str(payload.get("doc_id") or Path(file_name).stem or "batch-doc"),
            )
            outcome = runtime_obj.submit(
                ParseRequest(
                    doc_id=str(payload.get("doc_id") or Path(file_name).stem or "batch-doc"),
                    file_path=submission_path,
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
            if delete_after_submit and submission_path is not None:
                try:
                    os.unlink(submission_path)
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
        requested_profile = form.get("profile") or form.get("parse_profile")
        force_sync = _force_sync_requested(form, {})
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
                    code="document_too_large_for_sync",
                    message=_SYNC_ASYNC_RECOMMENDATION_MESSAGE,
                    status_code=413,
                    detail=_document_too_large_for_sync_detail(
                        actual_bytes=len(content),
                        limit_bytes=max_upload_bytes,
                        profile=_resolve_request_profile(
                            {},
                            media_type=media_type,
                            file_name=file_name,
                            file_size_bytes=len(content),
                            requested_profile=requested_profile,
                        ),
                    ),
                )
            profile_options, profile = _profile_options(
                {},
                media_type=media_type,
                file_name=file_name,
                file_size_bytes=len(content),
                requested_profile=requested_profile,
            )
            if bool(profile.get("recommended_async")) and not force_sync:
                return _error_response(
                    request,
                    code="document_too_large_for_sync",
                    message=_SYNC_ASYNC_RECOMMENDATION_MESSAGE,
                    status_code=413,
                    detail=_document_too_large_for_sync_detail(
                        actual_bytes=len(content),
                        limit_bytes=max_upload_bytes,
                        profile=profile,
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
            options = dict(profile_options)
            options["enable_ocr"] = enable_ocr
            options["file_name"] = file_name
            if force_sync:
                options["force_sync"] = True
            submission_path: str | None = None
            delete_after_submit = False
            try:
                submission_path, delete_after_submit = _persist_upload_file(
                    runtime_obj,
                    content=content,
                    file_name=file_name,
                    doc_id=doc_id,
                )
                outcome = runtime_obj.submit(
                    ParseRequest(
                        doc_id=doc_id,
                        file_path=submission_path,
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
                if delete_after_submit and submission_path is not None:
                    try:
                        os.unlink(submission_path)
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

    async def stage_upload(self, request: Request) -> JSONResponse:
        bridge_api_key = getattr(request.app.state, "upload_bridge_api_key", None)
        if bridge_api_key is not None and _extract_api_key(request) != bridge_api_key:
            return _api_key_unauthorized_response(
                request,
                code="upload_bridge_unauthorized",
                message="Missing or invalid upload bridge API key",
            )

        runtime_obj: ParseRuntime = request.app.state.runtime
        _cleanup_expired_staged_uploads(runtime_obj)

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
        create_job = _coerce_bool(form.get("create_job"), default=False)
        requested_profile = form.get("profile") or form.get("parse_profile")
        try:
            content = await upload.read()
            if not content:
                return _error_response(
                    request,
                    code="empty_file",
                    message="Empty file",
                    status_code=400,
                )
            max_upload_bytes = _max_staged_upload_bytes(runtime_obj)
            if _exceeds_upload_limit(len(content), max_upload_bytes):
                return _error_response(
                    request,
                    code="file_too_large",
                    message="File exceeds configured staged upload limit",
                    status_code=413,
                    detail=_file_too_large_detail(
                        actual_bytes=len(content),
                        limit_bytes=max_upload_bytes,
                    ),
                )
            profile_options, profile = _profile_options(
                {"enable_ocr": enable_ocr, "file_name": file_name},
                media_type=media_type,
                file_name=file_name,
                file_size_bytes=len(content),
                requested_profile=requested_profile,
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
            try:
                submission_path = _persist_staged_upload_file(
                    runtime_obj,
                    content=content,
                    file_name=file_name,
                    doc_id=doc_id,
                )
            except RuntimeError as exc:
                if str(exc) == "staged_upload_requires_local_object_store":
                    return _error_response(
                        request,
                        code="staged_upload_requires_local_object_store",
                        message="Staged uploads require a local:// object_store",
                        status_code=500,
                        detail={"object_store": runtime_obj.settings.object_store},
                    )
                raise
            profile_options, profile = _profile_options(
                {"enable_ocr": enable_ocr, "file_name": file_name},
                media_type=media_type,
                file_name=file_name,
                file_size_bytes=len(content),
                page_count=_detect_pdf_page_count_for_profile(submission_path, media_type=media_type),
                requested_profile=requested_profile,
            )
            job_payload = None
            if create_job:
                parse_request = ParseRequest(
                    doc_id=doc_id,
                    file_path=submission_path,
                    media_type=media_type,
                    options=profile_options,
                    tenant_id=tenant_id,
                    quota_key=quota_key,
                    quota_units=quota_units,
                )
                try:
                    job = request.app.state.runner.submit(parse_request)
                except QuotaExceededError as exc:
                    _record_quota_exceeded_event(request, runtime_obj, exc)
                    return self._quota_error_response(request, exc)
                except RuntimeError as exc:
                    if str(exc) == "too_many_inflight_jobs":
                        _record_inflight_full_event(
                            request,
                            runtime_obj,
                            doc_id=doc_id,
                            tenant_id=tenant_id,
                            quota_key=quota_key,
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
                job_payload = _to_payload(job)
        finally:
            close_upload = getattr(upload, "close", None)
            if callable(close_upload):
                await close_upload()

        payload: dict[str, Any] = {
            "doc_id": doc_id,
            "file_name": file_name,
            "media_type": media_type,
            "tenant_id": tenant_id,
            "quota_key": quota_key,
            "quota_units": quota_units,
            "enable_ocr": enable_ocr,
            "profile": profile.get("profile", "default"),
            "profile_source": profile.get("source", "auto"),
            "profile_reasons": list(profile.get("reasons") or []),
            "profile_recommended_async": bool(profile.get("recommended_async")),
            "parsecore_server_file_path": submission_path,
            "create_job": create_job,
        }
        if job_payload is not None:
            payload.update(job_payload)
            payload["job"] = job_payload
            return JSONResponse(payload, status_code=202)
        payload["state"] = "staged"
        return JSONResponse(payload, status_code=201)

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
        projection = str(request.query_params.get("projection") or "full").strip().lower()
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        try:
            return JSONResponse(_document_projection(snapshot, projection=projection))
        except ValueError as exc:
            if str(exc) == "invalid_projection":
                return _error_response(
                    request,
                    code="invalid_projection",
                    message="Invalid projection",
                    status_code=400,
                    detail={"allowed": ["compat", "structured", "full"]},
                )
            raise

    async def get_document_quality(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        return JSONResponse(_document_quality_projection(snapshot))

    async def get_document_records(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        try:
            limit = max(1, min(1000, int(request.query_params.get("limit", "100"))))
            offset = max(0, int(request.query_params.get("offset", "0")))
            page_start = _optional_int(request.query_params.get("page_start"))
            page_end = _optional_int(request.query_params.get("page_end"))
            return JSONResponse(
                _document_records_projection(
                    snapshot,
                    limit=limit,
                    offset=offset,
                    query=request.query_params.get("query"),
                    table_id=request.query_params.get("table_id"),
                    quality_signal=request.query_params.get("quality_signal"),
                    field_filters=_record_field_filters(request),
                    page_start=page_start,
                    page_end=page_end,
                )
            )
        except ValueError as exc:
            code = str(exc) or "invalid_records_query"
            if code == "invalid_page_range":
                return _error_response(
                    request,
                    code=code,
                    message="Invalid records page range",
                    status_code=400,
                )
            return _error_response(
                request,
                code="invalid_records_query",
                message="Invalid records query",
                status_code=400,
            )

    async def export_document(self, request: Request) -> Response:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        dataset = str(request.query_params.get("dataset") or "tables")
        export_format = str(request.query_params.get("format") or "jsonl")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        normalized_dataset = dataset.strip().lower()
        if normalized_dataset in {"pages", "lines"}:
            payload = _minimal_export_payload(snapshot, tenant_id=tenant_id)
            payload[normalized_dataset] = _document_view_rows(
                snapshot,
                view_types=(normalized_dataset,),
            ).get(normalized_dataset, [])
        elif normalized_dataset == "records":
            payload = _minimal_export_payload(snapshot, tenant_id=tenant_id)
            payload["records"] = _document_records_projection(
                snapshot,
                limit=None,
                offset=0,
                query=request.query_params.get("query"),
                table_id=request.query_params.get("table_id"),
                quality_signal=request.query_params.get("quality_signal"),
                field_filters=_record_field_filters(request),
                page_start=_optional_int(request.query_params.get("page_start")),
                page_end=_optional_int(request.query_params.get("page_end")),
            )["items"]
        else:
            payload = _document_projection(snapshot, projection="structured")
        try:
            exported = export_structured_projection(
                payload,
                dataset=dataset,
                format=export_format,
                as_bytes=True,
            )
        except ValueError as exc:
            code = str(exc) or "invalid_export"
            if code == "invalid_export_dataset":
                return _error_response(
                    request,
                    code=code,
                    message="Invalid export dataset",
                    status_code=400,
                    detail={"allowed": sorted(EXPORT_DATASETS)},
                )
            if code == "invalid_export_format":
                return _error_response(
                    request,
                    code=code,
                    message="Invalid export format",
                    status_code=400,
                    detail={"allowed": sorted(EXPORT_FORMATS)},
                )
            return _error_response(
                request,
                code=code,
                message="Invalid export payload",
                status_code=500,
            )

        headers = {
            "content-disposition": f"attachment; filename=\"{exported['filename']}\"",
        }
        return Response(
            content=exported["content"],
            media_type=str(exported["content_type"]),
            headers=headers,
        )

    async def get_document_parts(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        tenant_id = str(request.query_params.get("tenant_id") or "default")
        state_filter = request.query_params.getlist("state")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        payload = _document_projection(snapshot, projection="structured")
        try:
            return JSONResponse(document_parts_projection(payload, state_filter=state_filter))
        except ValueError as exc:
            code = str(exc) or "invalid_parts_payload"
            if code == "invalid_part_state":
                return _error_response(
                    request,
                    code=code,
                    message="Invalid part state filter",
                    status_code=400,
                    detail={"allowed": sorted(PART_STATE_FILTERS)},
                )
            return _error_response(
                request,
                code=code,
                message="Invalid parts payload",
                status_code=500,
            )

    async def plan_document_parts(self, request: Request) -> JSONResponse:
        payload = await _optional_json_payload(request)
        tenant_id = str(request.query_params.get("tenant_id") or payload.get("tenant_id") or "default")
        try:
            result = request.app.state.runner.plan_pdf_parts(
                doc_id=request.path_params["doc_id"],
                tenant_id=tenant_id,
                target_pages_per_part=_optional_int(payload.get("target_pages_per_part")),
                ocr_heavy_pages_per_part=_optional_int(payload.get("ocr_heavy_pages_per_part")),
                max_active_parts_per_doc=(
                    _optional_int(payload.get("max_active_parts_per_doc"))
                    if payload.get("max_active_parts_per_doc") is not None
                    else int(getattr(request.app.state.runtime.settings.runtime, "max_active_parts_per_doc", 0) or 0)
                ),
                profile=payload.get("profile"),
            )
        except LookupError:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        except ValueError as exc:
            code = str(exc) or "invalid_part_plan"
            status = 400
            message = "Invalid PDF part plan"
            if code == "document_not_pdf":
                message = "Document is not a PDF"
            if code == "invalid_pdf":
                message = "Invalid PDF"
            return _error_response(request, code=code, message=message, status_code=status)
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        return JSONResponse(_to_payload(result), status_code=202)

    async def rerun_document_part(self, request: Request) -> JSONResponse:
        payload = await _optional_json_payload(request)
        tenant_id = str(request.query_params.get("tenant_id") or payload.get("tenant_id") or "default")
        try:
            result = request.app.state.runner.rerun_pdf_part(
                doc_id=request.path_params["doc_id"],
                part_id=request.path_params["part_id"],
                tenant_id=tenant_id,
                profile=payload.get("profile"),
            )
        except LookupError:
            return _error_response(request, code="part_not_found", message="Part not found", status_code=404)
        except ValueError as exc:
            return _error_response(
                request,
                code=str(exc) or "invalid_part_rerun",
                message="Invalid part rerun",
                status_code=400,
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        return JSONResponse(_to_payload(result), status_code=202)

    async def rerun_document_parts(self, request: Request) -> JSONResponse:
        payload = await _optional_json_payload(request)
        tenant_id = str(request.query_params.get("tenant_id") or payload.get("tenant_id") or "default")
        failed_only = _coerce_bool(payload.get("failed_only"), default=True)
        try:
            result = request.app.state.runner.rerun_pdf_parts(
                doc_id=request.path_params["doc_id"],
                tenant_id=tenant_id,
                part_ids=_includes_payload(payload.get("part_ids"), field_name="part_ids"),
                failed_only=failed_only,
                state_filter=_includes_payload(payload.get("state"), field_name="state"),
                profile=payload.get("profile"),
            )
        except LookupError:
            return _error_response(request, code="part_not_found", message="Part not found", status_code=404)
        except ValueError as exc:
            return _error_response(
                request,
                code=str(exc) or "invalid_part_rerun",
                message="Invalid part rerun",
                status_code=400,
            )
        except QuotaExceededError as exc:
            _record_quota_exceeded_event(request, request.app.state.runtime, exc)
            return self._quota_error_response(request, exc)
        if not result.get("submitted"):
            return JSONResponse(_to_payload(result), status_code=409)
        return JSONResponse(_to_payload(result), status_code=202)

    async def cancel_document_part(self, request: Request) -> JSONResponse:
        payload = await _optional_json_payload(request)
        tenant_id = str(request.query_params.get("tenant_id") or payload.get("tenant_id") or "default")
        try:
            result = request.app.state.runner.cancel_pdf_part(
                doc_id=request.path_params["doc_id"],
                part_id=request.path_params["part_id"],
                tenant_id=tenant_id,
            )
        except LookupError:
            return _error_response(request, code="part_not_found", message="Part not found", status_code=404)
        status_code = 202 if result.get("cancelled") else 409
        return JSONResponse(_to_payload(result), status_code=status_code)

    async def create_export_job(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        payload = await _optional_json_payload(request)
        tenant_id = str(request.query_params.get("tenant_id") or payload.get("tenant_id") or "default")
        snapshot = runtime_obj.get_document(doc_id=request.path_params["doc_id"], tenant_id=tenant_id)
        if snapshot["job"] is None:
            return _error_response(request, code="document_not_found", message="Document not found", status_code=404)
        try:
            includes = _includes_payload(payload.get("include") or payload.get("includes"), field_name="include")
            formats = _formats_payload(payload.get("formats"))
            normalized_includes = {str(item).strip().lower() for item in includes or ()}
            view_only_export = bool(normalized_includes) and not (normalized_includes - {"pages", "lines"})
            if view_only_export:
                export_payload = _minimal_export_payload(snapshot, tenant_id=tenant_id)
            else:
                export_payload = _document_projection(snapshot, projection="structured")
                export_payload["tenant_id"] = tenant_id
            if normalized_includes & {"pages", "lines"}:
                view_names = tuple(name for name in ("pages", "lines") if name in normalized_includes)
                views = _document_view_rows(snapshot, view_types=view_names)
                for dataset_name in ("pages", "lines"):
                    if dataset_name in normalized_includes:
                        export_payload[dataset_name] = views.get(dataset_name, [])
            if includes and "records" in normalized_includes:
                export_payload["records"] = _document_records_projection(snapshot, limit=None, offset=0)["items"]
            manifest = create_export_package(
                export_payload,
                _export_root(runtime_obj),
                formats=formats,
                includes=includes,
                filters=dict(payload.get("filters") or {}),
            )
        except ValueError as exc:
            return _error_response(
                request,
                code=str(exc) or "invalid_export_job",
                message="Invalid export job",
                status_code=400,
            )
        manifest["download_endpoint"] = f"/v1/parse/export-jobs/{manifest['export_id']}/download"
        return JSONResponse(manifest, status_code=202)

    async def get_export_job(self, request: Request) -> JSONResponse:
        runtime_obj: ParseRuntime = request.app.state.runtime
        try:
            manifest = load_export_manifest(_export_root(runtime_obj), request.path_params["export_id"])
        except (FileNotFoundError, ValueError):
            return _error_response(request, code="export_not_found", message="Export job not found", status_code=404)
        manifest["download_endpoint"] = f"/v1/parse/export-jobs/{manifest['export_id']}/download"
        return JSONResponse(manifest)

    async def download_export_file(self, request: Request) -> Response:
        runtime_obj: ParseRuntime = request.app.state.runtime
        export_id = request.path_params["export_id"]
        filename = str(request.query_params.get("file") or "manifest.json")
        try:
            manifest = load_export_manifest(_export_root(runtime_obj), export_id)
            path = export_file_path(_export_root(runtime_obj), export_id, filename)
        except (FileNotFoundError, ValueError):
            return _error_response(request, code="export_not_found", message="Export file not found", status_code=404)
        if not path.exists() or not path.is_file():
            return _error_response(request, code="export_not_found", message="Export file not found", status_code=404)
        content_type = "application/json; charset=utf-8" if filename == "manifest.json" else "application/octet-stream"
        for entry in tuple(manifest.get("files") or ()):
            if isinstance(entry, dict) and entry.get("path") == filename:
                content_type = str(entry.get("content_type") or content_type)
                break
        return Response(
            content=path.read_bytes(),
            media_type=content_type,
            headers={"content-disposition": f"attachment; filename=\"{Path(filename).name}\""},
        )

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


def _profile_options(
    options: dict[str, Any],
    *,
    media_type: str | None,
    file_name: str | None,
    file_size_bytes: int | None,
    requested_profile: Any = None,
    page_count: int | None = None,
    table_count: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = dict(options or {})
    requested = _requested_profile(normalized, requested_profile)
    resolved = resolve_parse_profile(
        media_type=media_type,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        page_count=page_count,
        table_count=table_count,
        requested_profile=requested,
    )
    normalized["profile"] = str(resolved.get("profile") or "default")
    normalized["requested_profile"] = requested or "auto"
    normalized["profile_source"] = str(resolved.get("source") or "auto")
    normalized["profile_reasons"] = list(resolved.get("reasons") or [])
    normalized["profile_recommended_async"] = bool(resolved.get("recommended_async"))
    normalized["profile_limits"] = dict(resolved.get("limits") or {})
    normalized["profile_known"] = bool(resolved.get("profile_known", True))
    if resolved.get("profile_warning"):
        normalized["profile_warning"] = str(resolved["profile_warning"])
    return normalized, resolved


async def _optional_json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _minimal_export_payload(snapshot: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    job = snapshot.get("job")
    options = getattr(job, "options", {}) if job is not None else {}
    profile = options.get("profile") if isinstance(options, dict) else None
    return {
        "doc_id": str(snapshot.get("doc_id") or getattr(job, "doc_id", "") or ""),
        "parse_run_id": str(getattr(job, "job_id", "") or ""),
        "tenant_id": tenant_id,
        "profile": str(profile or "") if profile is not None else None,
    }


def _record_field_filters(request: Request) -> dict[str, str]:
    filters: dict[str, str] = {}
    field_name = str(request.query_params.get("field") or request.query_params.get("field_name") or "").strip()
    if field_name:
        filters[field_name] = str(request.query_params.get("value") or "")
    for key, value in request.query_params.multi_items():
        key_text = str(key)
        if not key_text.startswith("field."):
            continue
        name = key_text[len("field."):].strip()
        if name:
            filters[name] = str(value or "")
    return filters


def _formats_payload(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    return None


def _includes_payload(value: Any, *, field_name: str = "include") -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, (dict, list, tuple, set)):
                raise ValueError(f"invalid_{field_name}")
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    raise ValueError(f"invalid_{field_name}")


def _export_root(runtime_obj: ParseRuntime) -> Path:
    root = _resolve_local_object_store_root(runtime_obj)
    if root is None:
        raise ValueError("export_requires_local_object_store")
    export_root = root / "_exports"
    export_root.mkdir(parents=True, exist_ok=True)
    return export_root


def _resolve_request_profile(
    options: dict[str, Any],
    *,
    media_type: str | None,
    file_name: str | None,
    file_size_bytes: int | None,
    requested_profile: Any = None,
    page_count: int | None = None,
    table_count: int | None = None,
) -> dict[str, Any]:
    requested = _requested_profile(options, requested_profile)
    return resolve_parse_profile(
        media_type=media_type,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        page_count=page_count,
        table_count=table_count,
        requested_profile=requested,
    )


def _requested_profile(options: dict[str, Any], requested_profile: Any = None) -> str | None:
    for candidate in (
        requested_profile,
        options.get("profile") if isinstance(options, dict) else None,
        options.get("parse_profile") if isinstance(options, dict) else None,
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return None


def _force_sync_requested(payload: Any, options: dict[str, Any]) -> bool:
    for key in ("force_sync", "allow_sync_large_document"):
        if hasattr(payload, "get") and _coerce_bool(payload.get(key), default=False):
            return True
        if isinstance(options, dict) and _coerce_bool(options.get(key), default=False):
            return True
    return False


def _safe_file_size(file_path: str) -> int | None:
    try:
        return Path(file_path).stat().st_size
    except OSError:
        return None


def _detect_pdf_page_count_for_profile(file_path: str, *, media_type: str | None) -> int | None:
    suffix = Path(str(file_path)).suffix.lower()
    if str(media_type or "").lower() != "application/pdf" and suffix != ".pdf":
        return None
    try:
        return detect_pdf_page_count(file_path)
    except Exception:
        return None


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


def _resolve_api_file_path(runtime_obj: ParseRuntime, value: Any) -> str:
    file_path = str(value or "").strip()
    if not file_path:
        raise ValueError("missing_file_path")

    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("invalid_file_path") from exc
    if not resolved_candidate.is_file():
        raise ValueError("invalid_file_path")
    if runtime_obj.settings.runtime.allow_external_file_paths:
        return str(resolved_candidate)

    object_store_root = _resolve_local_object_store_root(runtime_obj)
    if object_store_root is None or not _path_is_relative_to(
        resolved_candidate,
        object_store_root,
    ):
        raise PermissionError("file_path_not_allowed")
    return str(resolved_candidate)


def _resolve_local_object_store_root(runtime_obj: ParseRuntime) -> Path | None:
    object_store = str(runtime_obj.settings.object_store or "")
    if not object_store.startswith("local://"):
        return None
    root = Path(object_store[len("local://"):])
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve(strict=False)


def _path_is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _persist_upload_file(
    runtime_obj: ParseRuntime,
    *,
    content: bytes,
    file_name: str,
    doc_id: str,
) -> tuple[str, bool]:
    suffix = Path(file_name).suffix or ".bin"
    if runtime_obj.settings.runtime.execution_mode != "queue-worker":
        with NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(content)
            return handle.name, True

    object_store = str(runtime_obj.settings.object_store or "")
    if object_store.startswith("local://"):
        root = Path(object_store[len("local://"):])
        if not root.is_absolute():
            root = Path.cwd() / root
        upload_dir = root / "_api_uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_doc_id = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in doc_id).strip("._") or "upload-doc"
        file_path = upload_dir / f"{safe_doc_id}-{uuid4().hex[:12]}{suffix}"
        file_path.write_bytes(content)
        return str(file_path), False

    with NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(content)
        return handle.name, True


def _persist_staged_upload_file(
    runtime_obj: ParseRuntime,
    *,
    content: bytes,
    file_name: str,
    doc_id: str,
) -> str:
    suffix = Path(file_name).suffix or ".bin"
    upload_dir = _resolve_staged_upload_dir(runtime_obj)
    if upload_dir is not None:
        safe_doc_id = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in doc_id
        ).strip("._") or "upload-doc"
        file_path = upload_dir / f"{safe_doc_id}-{uuid4().hex[:12]}{suffix}"
        file_path.write_bytes(content)
        return str(file_path)

    raise RuntimeError("staged_upload_requires_local_object_store")


def _resolve_staged_upload_dir(runtime_obj: ParseRuntime) -> Path | None:
    object_store = runtime_obj.settings.object_store
    if not object_store.startswith("local://"):
        return None
    root = Path(object_store[len("local://"):])
    if not root.is_absolute():
        root = Path.cwd() / root
    upload_dir = root / "_api_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _cleanup_expired_staged_uploads(runtime_obj: ParseRuntime) -> None:
    retention_seconds = max(0, int(runtime_obj.settings.runtime.staged_upload_retention_seconds))
    if retention_seconds <= 0:
        return
    upload_dir = _resolve_staged_upload_dir(runtime_obj)
    if upload_dir is None or not upload_dir.exists():
        return
    expiry_cutoff = time.time() - retention_seconds
    for candidate in upload_dir.iterdir():
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_mtime > expiry_cutoff:
                continue
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue


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
