from __future__ import annotations

from hashlib import sha1
import re
from typing import Any


PdfReader = None
PdfWriter = None

DEFAULT_TARGET_PAGES_PER_PART = 50
DEFAULT_OCR_HEAVY_PAGES_PER_PART = 10


def plan_pdf_parts(
    doc_id: str,
    total_pages: int,
    target_pages_per_part: int | None = None,
    ocr_heavy_pages_per_part: int | None = None,
    profile: str | None = None,
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Plan 1-based inclusive PDF page ranges for child parse parts."""
    pages = _positive_int(total_pages, "invalid_total_pages")
    pages_per_part = _pages_per_part(
        target_pages_per_part=target_pages_per_part,
        ocr_heavy_pages_per_part=ocr_heavy_pages_per_part,
        profile=profile,
        options=options,
    )

    parts: list[dict[str, Any]] = []
    page_start = 1
    part_index = 1
    source_doc_id = str(doc_id)
    while page_start <= pages:
        page_end = min(page_start + pages_per_part - 1, pages)
        part_id = child_doc_id(source_doc_id, part_index)
        parts.append(
            {
                "part_id": part_id,
                "part_doc_id": part_id,
                "part_index": part_index,
                "page_start": page_start,
                "page_end": page_end,
                "page_count": page_end - page_start + 1,
                "state": "pending",
                "source_doc_id": source_doc_id,
                "doc_id": source_doc_id,
                "profile": profile,
            }
        )
        page_start = page_end + 1
        part_index += 1
    return parts


def detect_pdf_page_count(file_path: str) -> int:
    reader_cls = _load_pdf_reader()
    try:
        reader = reader_cls(file_path)
        return len(reader.pages)
    except ImportError:
        raise
    except Exception as exc:
        raise ValueError("invalid_pdf") from exc


def create_pdf_part_file(
    source_path: str,
    target_path: str,
    page_start: int,
    page_end: int,
) -> None:
    start = _positive_int(page_start, "invalid_page_range")
    end = _positive_int(page_end, "invalid_page_range")
    if end < start:
        raise ValueError("invalid_page_range")

    reader_cls = _load_pdf_reader()
    writer_cls = _load_pdf_writer()
    try:
        with open(source_path, "rb") as source_file:
            reader = reader_cls(source_file)
            pages = reader.pages
            if end > len(pages):
                raise ValueError("invalid_page_range")
            writer = writer_cls()
            for page_number in range(start, end + 1):
                writer.add_page(pages[page_number - 1])
            with open(target_path, "wb") as target_file:
                writer.write(target_file)
    except ValueError:
        raise
    except ImportError:
        raise
    except Exception as exc:
        raise ValueError("invalid_pdf") from exc


def child_doc_id(parent_doc_id: str, part_index: int) -> str:
    index = _positive_int(part_index, "invalid_part_index")
    raw_parent = str(parent_doc_id)
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_parent).strip("-._")
    digest = sha1(raw_parent.encode("utf-8")).hexdigest()[:12]
    if not normalized:
        normalized = f"doc-{digest}"
    elif normalized != raw_parent or len(normalized) > 80:
        normalized = f"{normalized[:67].strip('-._')}-{digest}"
    return f"{normalized}-part-{index}"


def _pages_per_part(
    *,
    target_pages_per_part: int | None,
    ocr_heavy_pages_per_part: int | None,
    profile: str | None,
    options: dict[str, Any] | None,
) -> int:
    target = (
        DEFAULT_TARGET_PAGES_PER_PART
        if target_pages_per_part is None
        else target_pages_per_part
    )
    ocr_heavy_target = (
        DEFAULT_OCR_HEAVY_PAGES_PER_PART
        if ocr_heavy_pages_per_part is None
        else ocr_heavy_pages_per_part
    )
    if _is_ocr_heavy(profile=profile, options=options):
        return _positive_int(ocr_heavy_target, "invalid_pages_per_part")
    return _positive_int(target, "invalid_pages_per_part")


def _is_ocr_heavy(*, profile: str | None, options: dict[str, Any] | None) -> bool:
    values = {
        str(profile or "").lower(),
        str((options or {}).get("profile") or "").lower(),
        str((options or {}).get("ocr_mode") or "").lower(),
    }
    if values & {"ocr", "ocr-heavy", "ocr_heavy", "image-only", "image_only"}:
        return True
    return any(
        bool((options or {}).get(key))
        for key in ("ocr", "force_ocr", "ocr_heavy", "image_only_pdf")
    )


def _positive_int(value: Any, error: str) -> int:
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if integer < 1:
        raise ValueError(error)
    return integer


def _load_pdf_reader() -> Any:
    if PdfReader is not None:
        return PdfReader
    try:
        from pypdf import PdfReader as reader_cls  # type: ignore
    except ImportError:
        from PyPDF2 import PdfReader as reader_cls  # type: ignore
    return reader_cls


def _load_pdf_writer() -> Any:
    if PdfWriter is not None:
        return PdfWriter
    try:
        from pypdf import PdfWriter as writer_cls  # type: ignore
    except ImportError:
        from PyPDF2 import PdfWriter as writer_cls  # type: ignore
    return writer_cls


__all__ = [
    "DEFAULT_OCR_HEAVY_PAGES_PER_PART",
    "DEFAULT_TARGET_PAGES_PER_PART",
    "PdfReader",
    "PdfWriter",
    "child_doc_id",
    "create_pdf_part_file",
    "detect_pdf_page_count",
    "plan_pdf_parts",
]
