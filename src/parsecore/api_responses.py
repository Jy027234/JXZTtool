from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .api_support import _trace_id_for_request


# ---------------------------------------------------------------------------
# P7-T05: Error category taxonomy
# ---------------------------------------------------------------------------

ERROR_CATEGORIES = {
    "invalid_input": {
        "http_status": 400,
        "description": "Request payload is malformed or missing required fields",
    },
    "unsupported_media_type": {
        "http_status": 415,
        "description": "Document media type or extension is not supported by any configured provider",
    },
    "parser_failed": {
        "http_status": 500,
        "description": "Parser raised an unrecoverable error during document processing",
    },
    "provider_unavailable": {
        "http_status": 503,
        "description": "Required external provider (OCR / LLM / embedding) is unreachable",
    },
    "quota_exceeded": {
        "http_status": 429,
        "description": "Tenant or quota-key request limit has been exceeded",
    },
    "timeout": {
        "http_status": 504,
        "description": "Processing exceeded the configured job or part timeout",
    },
    "storage_failed": {
        "http_status": 500,
        "description": "Object store or database write failed",
    },
    "not_found": {
        "http_status": 404,
        "description": "Requested document, job, or resource does not exist",
    },
    "conflict": {
        "http_status": 409,
        "description": "Resource state conflicts with the requested operation",
    },
}


# ---------------------------------------------------------------------------
# P7-T02: Parse stage identifiers for timing instrumentation
# ---------------------------------------------------------------------------

PARSE_STAGES = (
    "upload",
    "parse",
    "normalize",
    "chunk",
    "embed",
    "export",
    "rerun",
)


def error_category_for_code(code: str) -> str:
    """Map an error *code* string to its canonical category.

    Returns ``"unknown"`` when no mapping exists.
    """
    _CODE_TO_CATEGORY = {
        "file_too_large": "invalid_input",
        "file_required": "invalid_input",
        "invalid_json": "invalid_input",
        "invalid_page_range": "invalid_input",
        "missing_required_field": "invalid_input",
        "unsupported_media_type": "unsupported_media_type",
        "unsupported_extension": "unsupported_media_type",
        "parse_failed": "parser_failed",
        "parser_error": "parser_failed",
        "batch_parse_failed": "parser_failed",
        "ocr_provider_unreachable": "provider_unavailable",
        "llm_provider_unreachable": "provider_unavailable",
        "embedding_provider_unreachable": "provider_unavailable",
        "quota_exceeded": "quota_exceeded",
        "job_timeout": "timeout",
        "part_timeout": "timeout",
        "object_store_error": "storage_failed",
        "database_error": "storage_failed",
        "document_not_found": "not_found",
        "job_not_found": "not_found",
        "part_not_found": "not_found",
        "state_conflict": "conflict",
        "rerun_conflict": "conflict",
    }
    return _CODE_TO_CATEGORY.get(code, "unknown")

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
