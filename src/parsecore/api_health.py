from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.util import find_spec
from time import monotonic
from typing import Any

from .ocr import is_ocr_provider_available


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
