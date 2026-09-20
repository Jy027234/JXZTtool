from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.trace_id = request.headers.get("x-trace-id") or f"trace-{uuid4().hex}"
        response = await call_next(request)
        response.headers.setdefault("x-trace-id", request.state.trace_id)
        return response


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, api_key: str, public_paths: tuple[str, ...] = ("/health",)):
        super().__init__(app)
        self.api_key = api_key
        self.public_paths = frozenset(public_paths)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.public_paths:
            return await call_next(request)
        if _extract_api_key(request) != self.api_key:
            return _api_key_unauthorized_response(request)
        return await call_next(request)


class TenantScopeMiddleware(BaseHTTPMiddleware):
    """Resolve and enforce the tenant boundary before any handler runs.

    Doing this once, in middleware, is what keeps the two scope modes from
    drifting: a handler never reads a raw tenant parameter without the resolved
    identity being attached to the request first.  A request that cannot be
    attributed is refused here, so no handler can accidentally serve it.
    """

    def __init__(self, app, *, public_paths: tuple[str, ...] = ("/health",)):
        super().__init__(app)
        self.public_paths = frozenset(public_paths)

    async def dispatch(self, request: Request, call_next):
        from .tenant_scope import (
            TenantScopeError,
            resolve_request_tenant,
            tenant_error_response,
        )

        if request.url.path in self.public_paths:
            return await call_next(request)
        try:
            resolution = resolve_request_tenant(
                request,
                requested_tenant_id=await _peek_requested_tenant(request),
            )
        except TenantScopeError as exc:
            return tenant_error_response(request, exc)
        request.state.tenant_resolution = resolution
        response = await call_next(request)
        # Record the attribution so an operator can audit which path decided the
        # tenant for this response.
        response.headers.setdefault("x-parsecore-tenant", resolution.tenant_id)
        response.headers.setdefault("x-parsecore-tenant-source", resolution.source)
        return response


async def _peek_requested_tenant(request: Request) -> str | None:
    """Best-effort read of a tenant the *caller* asked for.

    Query parameters cover the read endpoints.  For JSON bodies the raw bytes are
    cached by Starlette, so peeking does not consume the stream the handler will
    read.  Multipart and form bodies are left to the handler, which calls
    :func:`~parsecore.tenant_scope.request_tenant_id` with the value it parsed;
    that call re-checks the value against this middleware's attribution.
    """

    requested = str(request.query_params.get("tenant_id") or "").strip()
    if requested:
        return requested
    content_type = str(request.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        return None
    try:
        raw = await request.body()
        payload = json.loads(raw or b"{}")
    except Exception:
        return None
    if isinstance(payload, dict):
        candidate = str(payload.get("tenant_id") or "").strip()
        return candidate or None
    return None


def _trace_id_for_request(request: Request) -> str:
    trace_id = getattr(request.state, "trace_id", None)
    if isinstance(trace_id, str) and trace_id:
        return trace_id
    fallback = request.headers.get("x-trace-id") or f"trace-{uuid4().hex}"
    request.state.trace_id = fallback
    return fallback


def _extract_api_key(request: Request) -> str | None:
    header_value = str(request.headers.get("x-api-key") or "").strip()
    if header_value:
        return header_value
    authorization = str(request.headers.get("authorization") or "").strip()
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _api_key_unauthorized_response(
    request: Request,
    *,
    code: str = "unauthorized",
    message: str = "Missing or invalid API key",
) -> JSONResponse:
    response = JSONResponse(
        {
            "error": code,
            "code": code,
            "message": message,
            "trace_id": _trace_id_for_request(request),
        },
        status_code=401,
    )
    response.headers.setdefault("WWW-Authenticate", "Bearer")
    response.headers.setdefault("x-trace-id", _trace_id_for_request(request))
    return response


def _resolve_api_key_from_env(*, env_name: str, setting_name: str) -> str | None:
    normalized_env_name = str(env_name or "").strip()
    if not normalized_env_name:
        return None
    api_key = str(os.environ.get(normalized_env_name) or "").strip()
    if not api_key:
        raise ValueError(
            f"{setting_name} is set to {normalized_env_name}, but the environment variable is empty"
        )
    return api_key


def _resolve_required_api_key(runtime: Any) -> str | None:
    return _resolve_api_key_from_env(
        env_name=str(getattr(runtime.settings.runtime, "api_key_env", "") or ""),
        setting_name="runtime.api_key_env",
    )


def _resolve_staged_upload_api_key(runtime: Any) -> str | None:
    return _resolve_api_key_from_env(
        env_name=str(getattr(runtime.settings.runtime, "staged_upload_api_key_env", "") or ""),
        setting_name="runtime.staged_upload_api_key_env",
    )


def _max_upload_bytes(runtime: Any) -> int:
    try:
        return max(0, int(runtime.settings.runtime.max_upload_bytes))
    except (TypeError, ValueError):
        return 0


def _max_staged_upload_bytes(runtime: Any) -> int:
    try:
        return max(0, int(getattr(runtime.settings.runtime, "staged_upload_max_bytes", 0)))
    except (TypeError, ValueError):
        return 0


def _exceeds_upload_limit(actual_bytes: int, limit_bytes: int) -> bool:
    return limit_bytes > 0 and actual_bytes > limit_bytes


def _file_too_large_detail(*, actual_bytes: int, limit_bytes: int) -> dict[str, int]:
    return {
        "actual_bytes": actual_bytes,
        "limit_bytes": limit_bytes,
    }


def _document_too_large_for_sync_detail(
    *,
    actual_bytes: int,
    limit_bytes: int,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_profile = dict(profile or {})
    return {
        "actual_bytes": actual_bytes,
        "limit_bytes": limit_bytes,
        "recommended_endpoint": "/v1/parse/uploads",
        "recommended_job_endpoint": "/v1/parse/jobs",
        "profile": "auto",
        "resolved_profile": resolved_profile.get("profile", "default"),
        "profile_source": resolved_profile.get("source", "auto"),
        "profile_reasons": list(resolved_profile.get("reasons") or []),
        "profile_limits": dict(resolved_profile.get("limits") or {}),
        "profile_known": bool(resolved_profile.get("profile_known", True)),
        "profile_warning": resolved_profile.get("profile_warning"),
        "can_force_sync": True,
        "force_sync_param_names": ["force_sync", "allow_sync_large_document"],
    }


def _estimated_base64_decoded_size(value: str) -> int:
    compact = "".join(value.split())
    if not compact:
        return 0
    padding = len(compact) - len(compact.rstrip("="))
    return max(0, (len(compact) * 3 // 4) - padding)
