from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .api_support import _trace_id_for_request


def batch_error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    parser_used: str = "none",
    detail: Any = None,
) -> JSONResponse:
    payload = {
        "success": False,
        "total_pages": 0,
        "pages": [],
        "parser_used": parser_used,
        "error": message,
        "code": code,
        "message": message,
        "trace_id": _trace_id_for_request(request),
    }
    if detail is not None:
        payload["detail"] = detail
    return JSONResponse(payload, status_code=status_code)


def error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    detail: Any = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": code,
        "code": code,
        "message": message,
        "trace_id": _trace_id_for_request(request),
    }
    if detail is not None:
        payload["detail"] = detail
    if extra:
        payload.update(extra)
    return JSONResponse(payload, status_code=status_code)
