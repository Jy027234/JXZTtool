from __future__ import annotations

from pathlib import Path
from typing import Any


MIB = 1024 * 1024

KNOWN_PROFILES = {
    "default",
    "large-pdf",
    "table-heavy",
    "ocr-heavy",
    "excel-ledger",
    "scan-pdf",
}
SUPPORTED_PROFILES = (
    "default",
    "large-pdf",
    "table-heavy",
    "ocr-heavy",
    "excel-ledger",
    "scan-pdf",
)
RECOMMENDED_ASYNC_PROFILES = ("large-pdf", "scan-pdf")

EXCEL_MEDIA_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
}
EXCEL_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".xlsb", ".csv", ".tsv"}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}

DEFAULT_LIMITS = {
    "max_file_size_bytes": 50 * MIB,
    "max_page_count": 500,
    "max_table_density": 0.5,
}


def describe_parse_profiles() -> dict[str, Any]:
    """Describe supported parse profiles and the default auto-selection rules."""

    return {
        "supported_profiles": list(SUPPORTED_PROFILES),
        "default_profile": "default",
        "auto_profile": "auto",
        "default_auto_rule_thresholds": dict(DEFAULT_LIMITS),
        "recommended_async_profiles": list(RECOMMENDED_ASYNC_PROFILES),
        "profiles": [
            {
                "name": profile,
                "recommended_async": profile in RECOMMENDED_ASYNC_PROFILES,
                "limits": _limits_for(profile),
            }
            for profile in SUPPORTED_PROFILES
        ],
        "auto_rules": [
            {
                "profile": "large-pdf",
                "conditions": [
                    f"pdf.page_count>={DEFAULT_LIMITS['max_page_count']}",
                    f"pdf.file_size_bytes>={DEFAULT_LIMITS['max_file_size_bytes']}",
                ],
                "recommended_async": True,
            },
            {
                "profile": "excel-ledger",
                "conditions": ["excel_input"],
                "recommended_async": False,
            },
            {
                "profile": "table-heavy",
                "conditions": [f"table_density>={DEFAULT_LIMITS['max_table_density']}"],
                "recommended_async": False,
            },
            {
                "profile": "ocr-heavy",
                "conditions": ["image_input"],
                "recommended_async": False,
            },
            {
                "profile": "scan-pdf",
                "conditions": ["pdf.file_name contains scan"],
                "recommended_async": True,
            },
            {
                "profile": "default",
                "conditions": ["fallback_default"],
                "recommended_async": False,
            },
        ],
    }


def resolve_parse_profile(
    media_type: str | None,
    file_name: str | None,
    file_size_bytes: int | None,
    page_count: int | None,
    table_count: int | None,
    requested_profile: str | None,
) -> dict[str, Any]:
    """Resolve the parse profile for a document using conservative heuristics."""

    normalized_requested = _clean(requested_profile)
    if normalized_requested and normalized_requested != "auto":
        known = normalized_requested in KNOWN_PROFILES
        return {
            "profile": normalized_requested,
            "source": "requested",
            "reasons": [f"requested_profile={normalized_requested}"],
            "recommended_async": normalized_requested in RECOMMENDED_ASYNC_PROFILES,
            "limits": _limits_for(normalized_requested),
            "profile_known": known,
            "profile_warning": None if known else "unknown_profile",
        }

    normalized_media_type = _clean(media_type)
    suffix = Path(file_name or "").suffix.lower()
    reasons: list[str] = []

    is_pdf = normalized_media_type == "application/pdf" or suffix == ".pdf"
    is_excel = normalized_media_type in EXCEL_MEDIA_TYPES or suffix in EXCEL_EXTENSIONS
    is_image = (normalized_media_type or "").startswith("image/") or suffix in IMAGE_EXTENSIONS

    if is_pdf and page_count is not None and page_count >= DEFAULT_LIMITS["max_page_count"]:
        reasons.append(f"page_count>={DEFAULT_LIMITS['max_page_count']}")
        return _resolved("large-pdf", reasons, recommended_async=True)

    if is_pdf and file_size_bytes is not None and file_size_bytes >= DEFAULT_LIMITS["max_file_size_bytes"]:
        reasons.append(f"file_size_bytes>={DEFAULT_LIMITS['max_file_size_bytes']}")
        return _resolved("large-pdf", reasons, recommended_async=True)

    if is_excel:
        reasons.append("excel_input")
        return _resolved("excel-ledger", reasons)

    table_density = _table_density(table_count, page_count)
    if table_density is not None and table_density >= DEFAULT_LIMITS["max_table_density"]:
        reasons.append(f"table_density>={DEFAULT_LIMITS['max_table_density']}")
        return _resolved("table-heavy", reasons)

    if is_image:
        reasons.append("image_input")
        return _resolved("ocr-heavy", reasons)

    if is_pdf and file_name and "scan" in file_name.lower():
        reasons.append("scan_name_hint")
        return _resolved("scan-pdf", reasons, recommended_async=True)

    reasons.append("fallback_default")
    return _resolved("default", reasons)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _table_density(table_count: int | None, page_count: int | None) -> float | None:
    if table_count is None or page_count is None or page_count <= 0:
        return None
    return table_count / page_count


def _resolved(profile: str, reasons: list[str], recommended_async: bool = False) -> dict[str, Any]:
    return {
        "profile": profile,
        "source": "auto",
        "reasons": reasons,
        "recommended_async": recommended_async,
        "limits": _limits_for(profile),
        "profile_known": profile in KNOWN_PROFILES,
        "profile_warning": None if profile in KNOWN_PROFILES else "unknown_profile",
    }


def _limits_for(profile: str) -> dict[str, Any]:
    limits = dict(DEFAULT_LIMITS)
    limits["profile"] = profile if profile in KNOWN_PROFILES else "custom"
    return limits
