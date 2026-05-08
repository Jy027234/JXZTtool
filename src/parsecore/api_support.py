from __future__ import annotations

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
    env_name = str(env_name or "").strip()
    if not env_name:
        return None
    api_key = str(os.environ.get(env_name) or "").strip()
    if not api_key:
        raise ValueError(
            f"{setting_name} is set to {env_name}, but the environment variable is empty"
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


def _exceeds_upload_limit(actual_bytes: int, limit_bytes: int) -> bool:
    return limit_bytes > 0 and actual_bytes > limit_bytes


def _file_too_large_detail(*, actual_bytes: int, limit_bytes: int) -> dict[str, int]:
    return {
        "actual_bytes": actual_bytes,
        "limit_bytes": limit_bytes,
    }


def _estimated_base64_decoded_size(value: str) -> int:
    compact = "".join(value.split())
    if not compact:
        return 0
    padding = len(compact) - len(compact.rstrip("="))
    return max(0, (len(compact) * 3 // 4) - padding)
