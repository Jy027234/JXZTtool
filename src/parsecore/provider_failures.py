"""Stable failure categories shared by provider runtime and evaluation tools."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError


PROVIDER_FAILURE_CATEGORIES = (
    "invalid_input",
    "permission_denied",
    "rate_limited",
    "timeout",
    "provider_unavailable",
    "invalid_response",
    "configuration_error",
    "unsupported",
    "provider_failed",
)


def classify_provider_failure(error: BaseException | str) -> str:
    """Map provider failures to stable, low-cardinality observability buckets.

    This classification is diagnostic metadata only. It must not affect routing,
    retry, fallback, or result ordering.
    """

    message = str(error or "").strip().lower()

    if isinstance(error, TimeoutError) or any(
        token in message for token in ("timed out", "timeout", "time-out")
    ):
        return "timeout"
    if isinstance(error, PermissionError) or any(
        token in message
        for token in (
            "http error 401",
            "http error 403",
            "status code 401",
            "status code 403",
            "unauthorized",
            "forbidden",
            "permission denied",
            "access is denied",
            "invalid api key",
        )
    ):
        return "permission_denied"
    if (isinstance(error, HTTPError) and error.code == 429) or any(
        token in message
        for token in ("http error 429", "status code 429", "rate limit", "rate-limit", "too many requests")
    ):
        return "rate_limited"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "invalid_input"
    if any(
        token in message
        for token in (
            "http error 400",
            "http error 404",
            "http error 409",
            "http error 413",
            "http error 422",
            "status code 400",
            "status code 404",
            "status code 409",
            "status code 413",
            "status code 422",
            "bad request",
            "does not exist",
            "invalid input",
            "invalid request",
            "invalid file",
            "page_range",
        )
    ):
        return "invalid_input"
    if any(token in message for token in ("unsupported", "not supported", "media type", "extension")):
        return "unsupported"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)) or any(
        token in message
        for token in (
            "invalid response",
            "empty response",
            "malformed response",
            "missing vector",
            "missing embedding",
            "embedding_provider_mismatch",
            "empty_or_invalid_response",
        )
    ):
        return "invalid_response"
    if any(
        token in message
        for token in (
            "missing configuration",
            "configuration error",
            "not configured",
            "api_key_env",
            "environment variable",
            "provider disabled",
        )
    ):
        return "configuration_error"
    if isinstance(error, (ImportError, ConnectionError)) or (
        isinstance(error, URLError) and not isinstance(error, HTTPError)
    ) or (
        isinstance(error, HTTPError) and 500 <= error.code <= 599
    ) or any(
        token in message
        for token in (
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
            "status code 500",
            "status code 502",
            "status code 503",
            "status code 504",
            "no module named",
            "is required",
            "not installed",
            "missing dependency",
            "connection refused",
            "connection reset",
            "name or service not known",
            "temporary failure in name resolution",
            "gateway unavailable",
            "provider unavailable",
            "embedding unavailable",
            "service unavailable",
            "network is unreachable",
        )
    ):
        return "provider_unavailable"
    return "provider_failed"
