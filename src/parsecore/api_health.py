from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.util import find_spec
from time import monotonic
from typing import Any

from .ocr import is_ocr_provider_available


# P0-01: probing a local model or a remote provider on every /health call is too
# expensive and can turn a health check into an outage.  Probe results are cached
# per capability for a short window and the response always reports whether the
# capability can accept work, separately from process liveness.
_PROBE_CACHE_TTL_SECONDS = 30.0
_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached_probe(cache_key: str, probe: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    now = monotonic()
    cached = _PROBE_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < _PROBE_CACHE_TTL_SECONDS:
        result = dict(cached[1])
        result["cached"] = True
        result["cache_age_ms"] = round((now - cached[0]) * 1000, 2)
        return result
    result = probe()
    result["cached"] = False
    result["cache_age_ms"] = 0.0
    _PROBE_CACHE[cache_key] = (now, dict(result))
    return result


def clear_probe_cache() -> None:
    """Drop cached probe results (used by tests and by capability changes)."""

    _PROBE_CACHE.clear()


def is_ocr_service_available(runtime: Any) -> bool:
    return is_ocr_provider_available(runtime.settings.providers.ocr)


def _probe_module(
    module_name: str,
    *,
    version_name: str | None = None,
) -> dict[str, Any]:
    started = monotonic()
    if find_spec(module_name) is None:
        return {
            "available": False,
            "reason": "module_not_installed",
            "version": None,
            "init_elapsed_ms": round((monotonic() - started) * 1000, 2),
        }
    try:
        module = import_module(module_name)
    except Exception as exc:
        return {
            "available": False,
            "reason": f"import_failed:{type(exc).__name__}",
            "version": None,
            "init_elapsed_ms": round((monotonic() - started) * 1000, 2),
        }

    version = getattr(module, "__version__", None)
    if version_name and isinstance(version, str):
        version = version_name
    return {
        "available": True,
        "reason": "ok",
        "version": version if isinstance(version, str) else None,
        "init_elapsed_ms": round((monotonic() - started) * 1000, 2),
    }


def health_service_details(
    runtime: Any,
    *,
    ocr_probe: Callable[[Any], bool] | None = None,
) -> dict[str, dict[str, Any]]:
    parser_names = {parser.name for parser in runtime.parsers}
    probe = ocr_probe or is_ocr_service_available

    docx_registered = "docx-native" in parser_names
    pdf_registered = "pdf-text" in parser_names
    excel_registered = "excel-native" in parser_names
    ocr_registered = "image-ocr" in parser_names

    details: dict[str, dict[str, Any]] = {
        "pdfplumber": {
            "registered": pdf_registered,
            **(_probe_module("pdfplumber") if pdf_registered else {
                "available": False,
                "reason": "parser_not_registered",
                "version": None,
                "init_elapsed_ms": 0.0,
            }),
        },
        "python_docx": {
            "registered": docx_registered,
            **(_probe_module("docx") if docx_registered else {
                "available": False,
                "reason": "parser_not_registered",
                "version": None,
                "init_elapsed_ms": 0.0,
            }),
        },
        "openpyxl": {
            "registered": excel_registered,
            **(_probe_module("openpyxl") if excel_registered else {
                "available": False,
                "reason": "parser_not_registered",
                "version": None,
                "init_elapsed_ms": 0.0,
            }),
        },
        "xlrd": {
            "registered": excel_registered,
            **(_probe_module("xlrd") if excel_registered else {
                "available": False,
                "reason": "parser_not_registered",
                "version": None,
                "init_elapsed_ms": 0.0,
            }),
        },
    }

    ocr_available = False
    ocr_reason = "parser_not_registered"
    if ocr_registered:
        # A caller-injected probe (tests, host overrides) is authoritative and is
        # never cached; only the production probe is cached.
        if ocr_probe is None:
            ocr_available = bool(
                _cached_probe(
                    "ocr",
                    lambda: {"available": bool(probe(runtime)), "reason": "ok"},
                )["available"]
            )
        else:
            ocr_available = bool(probe(runtime))
        ocr_reason = "ok" if ocr_available else "provider_unavailable"
    details["paddleocr"] = {
        "registered": ocr_registered,
        "available": ocr_available,
        "reason": ocr_reason,
        "version": None,
        "init_elapsed_ms": 0.0,
    }
    return details


def health_services(
    runtime: Any,
    *,
    ocr_probe: Callable[[Any], bool] | None = None,
) -> dict[str, bool]:
    details = health_service_details(runtime, ocr_probe=ocr_probe)
    return {name: bool(detail.get("available")) for name, detail in details.items()}


def health_capabilities(runtime: Any) -> dict[str, Any]:
    """P0-01 capability block for ``/health``.

    ``status`` separates process liveness from deliverability:

    ``ok``
        Every required capability is available and no placeholder parser is
        active.
    ``degraded``
        The process can accept jobs, but optional capabilities are degraded
        (for example rerank fell back to a no-op).
    ``not_ready``
        A required capability is unavailable, or a placeholder parser is
        configured: the process is alive but must not produce formal results.
    """

    report = runtime.capability_report()
    if not report.get("accepting_jobs") or not report.get("formal_parse_ready"):
        status = "not_ready"
    elif report.get("degraded_capabilities"):
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "accepting_jobs": bool(report.get("accepting_jobs")),
        "formal_parse_ready": bool(report.get("formal_parse_ready")),
        "strictness": report.get("strictness"),
        "fingerprint": report.get("fingerprint"),
        "required_capabilities": report.get("required_capabilities") or [],
        "degraded_capabilities": report.get("degraded_capabilities") or [],
        "unavailable_capabilities": report.get("unavailable_capabilities") or [],
        "placeholder_parsers": report.get("placeholder_parsers") or [],
        "mock_capabilities": report.get("mock_capabilities") or [],
        "blocking_reasons": report.get("blocking_reasons") or [],
        "report": report,
    }


__all__ = [
    "clear_probe_cache",
    "health_capabilities",
    "health_service_details",
    "health_services",
    "is_ocr_service_available",
]
