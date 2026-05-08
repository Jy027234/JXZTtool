from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .models import Block, BlockType, ParseOutcome
from .ocr_trace import build_ocr_decision_trace, ocr_decision_trace_payload
from .quality import (
    ParseQualitySummary,
    evaluate_parse_quality,
    evaluate_projected_parse_quality,
)


_ARTIFACT_SEMANTIC_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
}

# Increment when the shape of pages[] or top-level fields changes in a
# backwards-incompatible way.  Consumers can gate on this string.
PAYLOAD_SCHEMA_VERSION = "2026-04"


def _to_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_payload(item) for item in value]
    return value


def _quality_payload(qs: ParseQualitySummary) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "score": qs.score,
        "flags": sorted(qs.flags),
        "warnings": list(qs.warnings),
        "total_cid_tokens": qs.total_cid_tokens,
    }
    if qs.total_pdf_name_tokens:
        payload["total_pdf_name_tokens"] = qs.total_pdf_name_tokens
    if qs.recommended_action:
        payload["recommended_action"] = qs.recommended_action
    if qs.ocr_failed_pages:
        payload["ocr_failed_pages"] = qs.ocr_failed_pages
    if qs.suspect_signature_pages:
        payload["suspect_signature_pages"] = qs.suspect_signature_pages
    return payload


def _batch_success_response(outcome: ParseOutcome) -> dict[str, Any]:
    pages = _project_pages(outcome.blocks)
    raw_qs = evaluate_parse_quality(outcome.blocks)
    output_qs = evaluate_projected_parse_quality(pages)
    trace_payload = ocr_decision_trace_payload(build_ocr_decision_trace(outcome.blocks))
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "success": True,
        "total_pages": len(pages),
        "pages": pages,
        "parser_used": _infer_parser_used(outcome.blocks),
        "quality": _quality_payload(output_qs),
        "raw_quality": _quality_payload(raw_qs),
        "output_quality": _quality_payload(output_qs),
        "ocr_decision_trace": trace_payload,
        "error": None,
    }


def _parse_success_response(
    outcome: ParseOutcome,
    *,
    file_name: str,
    mime_type: str | None,
    enable_ocr: bool,
) -> dict[str, Any]:
    pages = _project_pages(outcome.blocks)
    parser_used = _infer_parser_used(outcome.blocks)
    raw_qs = evaluate_parse_quality(outcome.blocks)
    output_qs = evaluate_projected_parse_quality(pages)
    ocr_trace = build_ocr_decision_trace(outcome.blocks)
    metadata: dict[str, Any] = {
        "parser": parser_used,
        "schema_version": PAYLOAD_SCHEMA_VERSION,
    }
    if (mime_type or "").lower() == "application/pdf":
        metadata["ocr_enabled"] = enable_ocr
        # C1: expose aggregated stage timings for PDF.
        timings = _aggregate_stage_timings(outcome.blocks)
        if timings:
            metadata["stage_timings"] = timings
        # A3: expose the effective OCR strategy from the title block.
        ocr_strategy = _read_first_metadata(outcome.blocks, "ocr_strategy")
        if ocr_strategy:
            metadata["ocr_strategy"] = ocr_strategy
        trace_payload = ocr_decision_trace_payload(ocr_trace)
        if trace_payload.get("ocr_attempted_pages", 0) > 0:
            metadata["ocr_decision_trace"] = trace_payload
    # B3: expose fidelity_profile when it was set by the caller.
    fidelity_profile = _read_first_metadata(outcome.blocks, "fidelity_profile")
    if fidelity_profile:
        metadata["fidelity_profile"] = fidelity_profile
    return {
        "file_name": file_name,
        "mime_type": mime_type or "application/octet-stream",
        "total_pages": len(pages),
        "pages": pages,
        "metadata": metadata,
        "quality": _quality_payload(output_qs),
        "raw_quality": _quality_payload(raw_qs),
        "output_quality": _quality_payload(output_qs),
    }


def _read_first_metadata(blocks: tuple[Block, ...], key: str) -> Any:
    """Return the first non-None value for ``key`` in any block's metadata."""
    for block in blocks:
        value = block.metadata.get(key)
        if value is not None:
            return value
    return None


def _aggregate_stage_timings(blocks: tuple[Block, ...]) -> dict[str, float] | None:
    """Sum per-page timing fields from block metadata into doc-level totals.

    Returns None when no timing data is present (e.g. pypdf-only path).
    """
    total_layout = 0.0
    total_ocr_render = 0.0
    total_ocr_provider = 0.0
    has_any = False
    for block in blocks:
        m = block.metadata
        v = m.get("layout_elapsed_s")
        if isinstance(v, (int, float)):
            total_layout += float(v)
            has_any = True
        v = m.get("ocr_render_elapsed_s")
        if isinstance(v, (int, float)):
            total_ocr_render += float(v)
        v = m.get("ocr_provider_elapsed_s")
        if isinstance(v, (int, float)):
            total_ocr_provider += float(v)
    if not has_any:
        return None
    result: dict[str, float] = {"layout_s": round(total_layout, 4)}
    if total_ocr_render > 0:
        result["ocr_render_s"] = round(total_ocr_render, 4)
    if total_ocr_provider > 0:
        result["ocr_provider_s"] = round(total_ocr_provider, 4)
    return result


def _project_pages(blocks: tuple[Block, ...]) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    page_signals: dict[int, dict[str, Any]] = {}
    # logical_page tracking for DOCX (always physical page==1, split by headings/breaks)
    logical_page_map: dict[int, set[int]] = {}  # logical_page_index -> {position indices}
    logical_page_texts: dict[int, list[str]] = {}
    has_logical_pages = False
    for block in blocks:
        page_number = int(block.metadata.get("page", 1))
        # Collect logical_page info for DOCX (physical page is always 1).
        lp = block.metadata.get("logical_page")
        if isinstance(lp, int):
            has_logical_pages = True
            if block.content.strip() and block.type != BlockType.TITLE:
                logical_page_texts.setdefault(lp, []).append(block.content)
        signal = page_signals.setdefault(
            page_number,
            {
                "roles": [],
                "all_text": [],
                "has_title": False,
                "page_types": [],
                "cid_token_counts": [],
                "ocr_attempted": False,
                "ocr_fallback": False,
                "ocr_rejected": False,
                "ocr_attempt_reasons": set(),
                "ocr_acceptance_reasons": set(),
                "ocr_rejection_reasons": set(),
                "ocr_error_reasons": set(),
                "native_text_token_count": 0,
                "final_text_token_count": 0,
            },
        )
        role = str(block.metadata.get("semantic_role") or "paragraph")
        signal["roles"].append(role)
        page_type = block.metadata.get("page_type")
        if isinstance(page_type, str) and page_type:
            signal["page_types"].append(page_type)
        if block.content.strip():
            signal["all_text"].append(block.content)
        if block.type == BlockType.TITLE:
            signal["has_title"] = True
        cid_count = block.metadata.get("cid_token_count")
        if isinstance(cid_count, int) and cid_count > 0:
            signal["cid_token_counts"].append(cid_count)
        if bool(block.metadata.get("ocr_attempted")):
            signal["ocr_attempted"] = True
        if bool(block.metadata.get("ocr_fallback_used")):
            signal["ocr_fallback"] = True
        if bool(block.metadata.get("ocr_rejected")):
            signal["ocr_rejected"] = True
        attempt_reason = block.metadata.get("ocr_attempt_reason")
        if isinstance(attempt_reason, str) and attempt_reason:
            signal["ocr_attempt_reasons"].add(attempt_reason)
        acceptance_reason = block.metadata.get("ocr_acceptance_reason")
        if isinstance(acceptance_reason, str) and acceptance_reason:
            signal["ocr_acceptance_reasons"].add(acceptance_reason)
        rejection_reason = block.metadata.get("ocr_rejection_reason")
        if isinstance(rejection_reason, str) and rejection_reason:
            signal["ocr_rejection_reasons"].add(rejection_reason)
        error_reason = block.metadata.get("ocr_error_reason")
        if isinstance(error_reason, str) and error_reason:
            signal["ocr_error_reasons"].add(error_reason)
        native_tokens = block.metadata.get("native_text_token_count")
        if isinstance(native_tokens, int) and native_tokens >= 0:
            signal["native_text_token_count"] = max(int(signal["native_text_token_count"]), native_tokens)
        final_tokens = block.metadata.get("final_text_token_count")
        if isinstance(final_tokens, int) and final_tokens >= 0:
            signal["final_text_token_count"] = max(int(signal["final_text_token_count"]), final_tokens)

        if block.type == BlockType.TITLE:
            continue

        entry = pages.setdefault(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "tables": [],
                "artifacts": [],
                "confidence_parts": [],
            },
        )
        if role in _ARTIFACT_SEMANTIC_ROLES:
            entry["artifacts"].append({"text": block.content, "semantic_role": role})
        elif block.type == BlockType.TABLE:
            if block.content.strip():
                entry["tables_markdown"].append(block.content)
                # B2: dual output – include raw cells alongside markdown.
                raw_cells = block.metadata.get("cells")
                table_entry: dict[str, Any] = {"markdown": block.content}
                if raw_cells:
                    table_entry["raw"] = raw_cells
                    table_entry["rows"] = block.metadata.get("rows", 0)
                    table_entry["cols"] = block.metadata.get("cols", 0)
                entry["tables"].append(table_entry)
        elif block.content.strip():
            entry["text_parts"].append(block.content)

        confidence = block.metadata.get("confidence")
        if isinstance(confidence, (int, float)):
            entry["confidence_parts"].append(float(confidence))

    total_pages = len(set(page_signals) | set(pages))
    ordered: list[dict[str, Any]] = []
    for page_number in sorted(set(page_signals) | set(pages)):
        entry = pages.get(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "tables": [],
                "artifacts": [],
                "confidence_parts": [],
            },
        )
        text = "\n\n".join(item for item in entry.pop("text_parts") if item.strip())
        confidences = entry.pop("confidence_parts")
        sig = page_signals.get(page_number, {})
        full_text = "\n\n".join(sig.get("all_text", []))

        # Parser-emitted page_types take priority; accumulate votes for
        # the remaining pages where the parser did not emit a type.
        explicit_types = [t for t in sig.get("page_types", []) if t and t != "body"]
        if explicit_types:
            page_type = explicit_types[0]
            page_type_confidence = "high"
        else:
            page_type, page_type_confidence = _infer_page_type_with_confidence(
                page_number=page_number,
                total_pages=total_pages,
                roles=sig.get("roles", []),
                full_text=full_text,
                has_title=bool(sig.get("has_title")),
                body_text=text,
            )

        page_entry: dict[str, Any] = {
            "page_number": page_number,
            "page_type": page_type,
            "page_type_confidence": page_type_confidence,
            "text": text,
            "tables_markdown": entry["tables_markdown"],
            "tables": entry["tables"],
            "artifacts": entry["artifacts"],
            "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 1.0,
        }
        cid_total = sum(sig.get("cid_token_counts", []))
        if cid_total > 0:
            page_entry["cid_token_count"] = cid_total
        if bool(sig.get("ocr_attempted")):
            page_entry["ocr_attempted"] = True
        if bool(sig.get("ocr_fallback")):
            page_entry["ocr_fallback"] = True
        if bool(sig.get("ocr_rejected")):
            page_entry["ocr_rejected"] = True
        attempt_reasons = sorted(sig.get("ocr_attempt_reasons", set()))
        if attempt_reasons:
            page_entry["ocr_attempt_reasons"] = attempt_reasons
        acceptance_reasons = sorted(sig.get("ocr_acceptance_reasons", set()))
        if acceptance_reasons:
            page_entry["ocr_acceptance_reasons"] = acceptance_reasons
        rejection_reasons = sorted(sig.get("ocr_rejection_reasons", set()))
        if rejection_reasons:
            page_entry["ocr_rejection_reasons"] = rejection_reasons
        error_reasons = sorted(sig.get("ocr_error_reasons", set()))
        if error_reasons:
            page_entry["ocr_error_reasons"] = error_reasons
        native_tokens = int(sig.get("native_text_token_count", 0) or 0)
        final_tokens = int(sig.get("final_text_token_count", 0) or 0)
        if native_tokens > 0:
            page_entry["native_text_token_count"] = native_tokens
        if final_tokens > 0:
            page_entry["final_text_token_count"] = final_tokens
        ordered.append(page_entry)

    # For DOCX documents, attach a logical_pages summary alongside the physical pages.
    if has_logical_pages and logical_page_texts:
        for page_entry in ordered:
            logical_pages_list = [
                {
                    "logical_page_number": lp_idx,
                    "text": "\n\n".join(texts),
                }
                for lp_idx, texts in sorted(logical_page_texts.items())
            ]
            page_entry["logical_pages"] = logical_pages_list
            break  # only attach to the first (and only physical) page entry

    return ordered


# Strong token sets for page-type classification.
# Only fire signature if the page contains a dedicated signature block header
# (not a single mention of "签字" mid-paragraph).
_SIGNATURE_STRONG_TOKENS = frozenset(
    [
        "signature page",
        "signed by:",
        "approved by:",
        "审批人：",
        "审批人:",
        "签字栏",
        "signature block",
        "authorized signature",
    ]
)
# Minimum fraction of blocks whose role must be non-body for the page to be
# classified as a special type.  This prevents a single stray role from
# overriding the whole page.
_PAGE_TYPE_ROLE_THRESHOLD = 0.4


def _infer_page_type_with_confidence(
    *,
    page_number: int,
    total_pages: int,
    roles: list[str],
    full_text: str,
    has_title: bool,
    body_text: str,
) -> tuple[str, str]:
    """Return (page_type, confidence) where confidence is 'high'/'medium'/'low'."""
    role_set = set(roles)
    n_blocks = max(len(roles), 1)
    normalized_text = full_text.lower()

    # --- TOC / LEP  (role-based, high confidence) ---
    toc_count = sum(1 for r in roles if r in ("toc_entry", "lep_entry"))
    if toc_count / n_blocks >= _PAGE_TYPE_ROLE_THRESHOLD:
        confidence = "high" if toc_count / n_blocks >= 0.7 else "medium"
        return "toc", confidence

    # --- Front matter (role-based, high confidence) ---
    fm_count = sum(1 for r in roles if r in ("front_matter", "revision_record", "distribution_list"))
    if fm_count / n_blocks >= _PAGE_TYPE_ROLE_THRESHOLD:
        confidence = "high" if fm_count / n_blocks >= 0.7 else "medium"
        return "front_matter", confidence

    # --- Signature: require a strong dedicated header, not casual keyword mention ---
    if any(token in normalized_text for token in _SIGNATURE_STRONG_TOKENS):
        return "signature", "high"

    # --- Appendix ---
    if any(token in normalized_text for token in ("appendix", "annex", "附录")):
        return "appendix", "medium"

    # --- Cover page (first page, title only, no body text) ---
    if page_number == 1 and has_title and not body_text.strip():
        return "cover", "high"

    return "body", "high"


# Kept for backwards compatibility with any direct callers in tests.
def _infer_page_type(
    *,
    page_number: int,
    roles: list[str],
    full_text: str,
    has_title: bool,
    body_text: str,
) -> str:
    page_type, _ = _infer_page_type_with_confidence(
        page_number=page_number,
        total_pages=1,
        roles=roles,
        full_text=full_text,
        has_title=has_title,
        body_text=body_text,
    )
    return page_type


def _infer_parser_used(blocks: tuple[Block, ...]) -> str:
    parser_aliases = {
        "docx-native": "python-docx",
        "pdf-text": "pdf-text",
        "text-native": "text-native",
    }
    for block in blocks:
        layout_source = block.metadata.get("layout_source")
        if isinstance(layout_source, str) and layout_source:
            return layout_source
        parser_name = block.metadata.get("parser")
        if isinstance(parser_name, str) and parser_name:
            return parser_aliases.get(parser_name, parser_name)
    return "unknown"
