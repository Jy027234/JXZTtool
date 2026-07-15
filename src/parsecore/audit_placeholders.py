"""Shared classification for non-content page-audit placeholders.

Empty or OCR-failed PDF pages intentionally remain in the parsed artifact set
so page coverage can be audited.  They are not parsed content, however, and
must not enter text/structure quality denominators.
"""

from __future__ import annotations

from typing import Any, Mapping


NON_CONTENT_AUDIT_MISSING_REASONS = frozenset(
    {
        "ocr_empty_text",
        "page_without_extractable_content",
    }
)


def is_non_content_audit_placeholder(
    *,
    semantic_role: object,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Return whether a record is page evidence rather than parsed content.

    The classification is deliberately strict: a matching ``missing_reason``
    alone must not remove ordinary user content from quality denominators.
    """

    values = metadata if isinstance(metadata, Mapping) else {}
    return (
        str(semantic_role or "").strip().lower() == "parse_artifact"
        and str(values.get("index_policy") or "").strip().lower() == "skip"
        and str(values.get("missing_reason") or "").strip().lower()
        in NON_CONTENT_AUDIT_MISSING_REASONS
    )
