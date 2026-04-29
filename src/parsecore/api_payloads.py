from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .models import Block, BlockType, ParseOutcome


_ARTIFACT_SEMANTIC_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
}


def _to_payload(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_payload(item) for item in value]
    return value


def _batch_success_response(outcome: ParseOutcome) -> dict[str, Any]:
    pages = _project_pages(outcome.blocks)
    return {
        "success": True,
        "total_pages": len(pages),
        "pages": pages,
        "parser_used": _infer_parser_used(outcome.blocks),
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
    metadata: dict[str, Any] = {
        "parser": parser_used,
    }
    if (mime_type or "").lower() == "application/pdf":
        metadata["ocr_enabled"] = enable_ocr
    return {
        "file_name": file_name,
        "mime_type": mime_type or "application/octet-stream",
        "total_pages": len(pages),
        "pages": pages,
        "metadata": metadata,
    }


def _project_pages(blocks: tuple[Block, ...]) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    page_signals: dict[int, dict[str, Any]] = {}
    for block in blocks:
        page_number = int(block.metadata.get("page", 1))
        signal = page_signals.setdefault(
            page_number,
            {
                "roles": [],
                "all_text": [],
                "has_title": False,
                "page_types": [],
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

        if block.type == BlockType.TITLE:
            continue

        entry = pages.setdefault(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "artifacts": [],
                "confidence_parts": [],
            },
        )
        if role in _ARTIFACT_SEMANTIC_ROLES:
            entry["artifacts"].append({"text": block.content, "semantic_role": role})
        elif block.type == BlockType.TABLE:
            if block.content.strip():
                entry["tables_markdown"].append(block.content)
        elif block.content.strip():
            entry["text_parts"].append(block.content)

        confidence = block.metadata.get("confidence")
        if isinstance(confidence, (int, float)):
            entry["confidence_parts"].append(float(confidence))

    ordered: list[dict[str, Any]] = []
    for page_number in sorted(set(page_signals) | set(pages)):
        entry = pages.get(
            page_number,
            {
                "page_number": page_number,
                "page_type": "body",
                "text_parts": [],
                "tables_markdown": [],
                "artifacts": [],
                "confidence_parts": [],
            },
        )
        text = "\n\n".join(item for item in entry.pop("text_parts") if item.strip())
        confidences = entry.pop("confidence_parts")
        page_types = [
            item
            for item in page_signals.get(page_number, {}).get("page_types", [])
            if item and item != "body"
        ]
        page_type = page_types[0] if page_types else _infer_page_type(
            page_number=page_number,
            roles=page_signals.get(page_number, {}).get("roles", []),
            full_text="\n\n".join(page_signals.get(page_number, {}).get("all_text", [])),
            has_title=bool(page_signals.get(page_number, {}).get("has_title")),
            body_text=text,
        )
        ordered.append(
            {
                "page_number": page_number,
                "page_type": page_type,
                "text": text,
                "tables_markdown": entry["tables_markdown"],
                "artifacts": entry["artifacts"],
                "confidence": round(sum(confidences) / len(confidences), 4) if confidences else 1.0,
            }
        )
    return ordered


def _infer_page_type(
    *,
    page_number: int,
    roles: list[str],
    full_text: str,
    has_title: bool,
    body_text: str,
) -> str:
    role_set = set(roles)
    normalized_text = full_text.lower()
    if "toc_entry" in role_set or "lep_entry" in role_set:
        return "toc"
    if any(role in role_set for role in ("front_matter", "revision_record", "distribution_list")):
        return "front_matter"
    if any(token in normalized_text for token in ("signature", "signed by", "approved by", "签字", "签名", "审批")):
        return "signature"
    if any(token in normalized_text for token in ("appendix", "annex", "附录")):
        return "appendix"
    stripped_body = body_text.strip()
    if page_number == 1 and has_title and not stripped_body:
        return "cover"
    return "body"


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
