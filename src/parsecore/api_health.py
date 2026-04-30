from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from typing import Any

from .ocr import is_ocr_provider_available


def is_ocr_service_available(runtime: Any) -> bool:
    return is_ocr_provider_available(runtime.settings.providers.ocr)


def health_services(
    runtime: Any,
    *,
    ocr_probe: Callable[[Any], bool] | None = None,
) -> dict[str, bool]:
    parser_names = {parser.name for parser in runtime.parsers}
    probe = ocr_probe or is_ocr_service_available
    return {
        "pdfplumber": "pdf-text" in parser_names,
        "python_docx": "docx-native" in parser_names,
        "openpyxl": "excel-native" in parser_names and find_spec("openpyxl") is not None,
        "xlrd": "excel-native" in parser_names and find_spec("xlrd") is not None,
        "paddleocr": "image-ocr" in parser_names and probe(runtime),
    }
