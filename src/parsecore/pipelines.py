from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence
from uuid import uuid4

from .audit_placeholders import is_non_content_audit_placeholder
from .config import ParserSettings
from .contracts import ChunkBuilder, ParserAdapter
from .models import Block, Chunk, ParseRequest


_ARTIFACT_SEMANTIC_ROLES = {
    "header_footer",
    "parse_artifact",
    "version_cell",
    "page_ref_cell",
}

_DOCX_SECTION_ANCHOR_ROLES = {
    "front_matter",
    "body_section",
    "appendix",
}

_DOCX_GROUPABLE_ROLES = {
    "toc_entry",
    "lep_entry",
    "revision_record",
    "distribution_list",
    "front_matter",
}

_STRUCTURED_UNIT_ROLES = {
    "clause",
    "definition",
    "list_item",
    "procedure",
    "procedure_step",
}

_MANUAL_NUMBERED_HEADING_PATTERN = re.compile(
    r"^(?:\d+(?:\.\d+)*|第[\d一二三四五六七八九十百千]+[章节条款]|chapter\s+[a-z0-9ivxlcdm]+|section\s+[a-z0-9ivxlcdm]+)\b",
    re.IGNORECASE,
)

_MANUAL_APPENDIX_HEADING_PATTERN = re.compile(
    r"^(?:appendix|annex|附录|附件)\b",
    re.IGNORECASE,
)

_MANUAL_FRONT_MATTER_PATTERN = re.compile(
    r"(?:table\s+of\s+contents|contents|目录|目次|list\s+of\s+effective\s+pages|有效页清单|record\s+of\s+revisions|revision(?:\s+record|\s+history)?|修订记录|版次表|distribution\s+list|分发清单|signature|approval|highlights)",
    re.IGNORECASE,
)

_MANUAL_ALL_CAPS_HEADING_PATTERN = re.compile(r"^[A-Z][A-Z0-9/&() .,-]{2,80}$")

_MANUAL_EFFECTIVE_PAGE_PATTERN = re.compile(
    r"(?:list\s+of\s+effective\s+pages|有效页清单)",
    re.IGNORECASE,
)

_MANUAL_REVISION_PATTERN = re.compile(
    r"(?:record\s+of\s+revisions|revision(?:\s+record|\s+history)?|修订记录|版次表)",
    re.IGNORECASE,
)

_MANUAL_DISTRIBUTION_PATTERN = re.compile(
    r"(?:distribution\s+list|分发清单|release\s+personnel\s+list|outsourced\s+vendor\s+list)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _ChunkAccumulator:
    role: str
    text_parts: list[str] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class PipelineStageSpec:
    name: str
    execution: str = "runtime"
    enabled_by_default: bool = True
    option_keys: tuple[str, ...] = ()
    requires_remote_services: bool = False
    enable_option_path: str | None = None
    allow_failure: bool = True


@dataclass(slots=True, frozen=True)
class PipelineCapabilities:
    pipeline_name: str
    format_name: str
    backend_name: str
    parser_name: str
    supports_parse: bool = True
    supports_rechunk: bool = True
    supports_reembed: bool = True
    stage_names: tuple[str, ...] = ()
    parser_backed_stage_names: tuple[str, ...] = ()
    chunker_name: str = "artifact-chunker"
    option_keys: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class DocumentArtifactItem:
    item_id: str
    block_ids: tuple[str, ...]
    kind: str
    semantic_role: str
    text: str
    page_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DocumentKnowledgeUnit:
    unit_id: str
    source_item_ids: tuple[str, ...]
    source_block_ids: tuple[str, ...]
    source_table_ids: tuple[str, ...]
    unit_type: str
    semantic_role: str
    text: str
    page_span: tuple[int, int]
    should_index_for_rag: bool
    skip_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ParsedDocumentArtifact:
    doc_id: str
    pipeline_name: str
    parser_name: str
    backend_name: str
    format_name: str
    source_path: str
    options_hash: str
    blocks: tuple[Block, ...]
    items: tuple[DocumentArtifactItem, ...]
    knowledge_units: tuple[DocumentKnowledgeUnit, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


class EnrichmentStage(Protocol):
    spec: PipelineStageSpec

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact: ...


class ArtifactChunker(Protocol):
    name: str

    def build(self, *, document: ParsedDocumentArtifact) -> Sequence[Chunk]: ...


@dataclass(slots=True, frozen=True)
class PipelineRegistration:
    pipeline_name: str
    format_name: str
    backend_name: str
    parser_name: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    options: Mapping[str, Any]
    parser: ParserAdapter
    stage_specs: tuple[PipelineStageSpec, ...]
    capabilities: PipelineCapabilities


@dataclass(slots=True)
class PipelineCache:
    _pipelines: dict[str, "DocumentPipeline"] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get_or_create(self, *, key: str, factory: callable) -> tuple["DocumentPipeline", bool]:
        pipeline = self._pipelines.get(key)
        if pipeline is not None:
            self.hits += 1
            return pipeline, True
        self.misses += 1
        pipeline = factory()
        self._pipelines[key] = pipeline
        return pipeline, False

    def describe(self) -> dict[str, int]:
        return {
            "size": len(self._pipelines),
            "hits": self.hits,
            "misses": self.misses,
        }


class PipelineProvenanceStage:
    spec = PipelineStageSpec(
        name="pipeline-provenance",
        execution="runtime",
        enabled_by_default=True,
    )

    def __init__(self, registration: PipelineRegistration) -> None:
        self._registration = registration

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        metadata = dict(document.metadata)
        metadata.update(
            {
                "pipeline_name": self._registration.pipeline_name,
                "parser_name": self._registration.parser_name,
                "backend_name": self._registration.backend_name,
                "format_name": self._registration.format_name,
                "request_options": _json_safe_mapping(request.options),
                "registered_options": _json_safe_mapping(self._registration.options),
                "active_stages": [spec.name for spec in self._registration.stage_specs],
                "parser_backed_stages": [
                    spec.name for spec in self._registration.stage_specs if spec.execution == "parser-backed"
                ],
            }
        )
        return replace(document, metadata=metadata)


class DocumentSummaryStage:
    spec = PipelineStageSpec(
        name="document-summary",
        execution="runtime",
        enabled_by_default=True,
    )

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        metadata = dict(document.metadata)
        role_counts: dict[str, int] = {}
        page_numbers: set[int] = set()
        for item in document.items:
            role_counts[item.semantic_role] = role_counts.get(item.semantic_role, 0) + 1
            if item.page_number is not None:
                page_numbers.add(item.page_number)
        manual_anatomy = _build_manual_anatomy(document.items)
        structure_quality = _build_structure_quality(document.items)
        metadata["summary"] = {
            "block_count": len(document.blocks),
            "item_count": len(document.items),
            "page_count": len(page_numbers),
            "item_kinds": role_counts,
        }
        metadata["manual_anatomy"] = manual_anatomy
        metadata["structure_quality"] = structure_quality
        return replace(document, metadata=metadata)


class TableStructureStage:
    spec = PipelineStageSpec(
        name="table-structure",
        execution="runtime",
        enabled_by_default=False,
        option_keys=(
            "enrichment.table_structure.enabled",
            "enrichment.table_structure.header_rows",
            "enrichment.table_structure.output_format",
        ),
        enable_option_path="enrichment.table_structure.enabled",
        allow_failure=True,
    )

    def __init__(self, registration: PipelineRegistration) -> None:
        self._registration = registration

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        effective_options = _effective_options(self._registration.options, request.options)
        header_rows = max(1, _as_int(_lookup_option_path(effective_options, "enrichment.table_structure.header_rows"), default=1))
        output_format = _as_str(
            _lookup_option_path(effective_options, "enrichment.table_structure.output_format"),
            default="markdown",
        ).lower()

        updated_items: list[DocumentArtifactItem] = []
        enriched_items = 0
        skipped_items = 0
        for item in document.items:
            if str(item.kind).strip().lower() != "table":
                updated_items.append(item)
                continue

            cells = _normalize_table_cells(item.metadata.get("cells"))
            if not cells:
                skipped_items += 1
                updated_items.append(item)
                continue

            rendered_text = _render_table_text(
                cells,
                header_rows=header_rows,
                output_format=output_format,
            )
            metadata = dict(item.metadata)
            enrichment = dict(metadata.get("enrichment") or {})
            enrichment["table_structure"] = {
                "header_rows": header_rows,
                "output_format": output_format,
                "row_count": len(cells),
                "col_count": max((len(row) for row in cells), default=0),
            }
            metadata["enrichment"] = enrichment
            metadata["rendered_text"] = rendered_text
            if output_format == "markdown":
                metadata["table_markdown"] = rendered_text
            updated_items.append(
                replace(
                    item,
                    text=rendered_text,
                    metadata=metadata,
                )
            )
            enriched_items += 1

        metadata = dict(document.metadata)
        metadata["table_structure"] = {
            "enabled": True,
            "enriched_items": enriched_items,
            "skipped_items": skipped_items,
            "output_format": output_format,
            "header_rows": header_rows,
        }
        return replace(document, items=tuple(updated_items), metadata=metadata)


class ArtifactBackedChunker:
    name = "artifact-chunker"

    def __init__(self, *, base_builder: ChunkBuilder) -> None:
        self._base_builder = base_builder

    def build(self, *, document: ParsedDocumentArtifact) -> Sequence[Chunk]:
        knowledge_units = _document_knowledge_units(document)
        if str(document.format_name).strip().lower() == "docx":
            structured_chunks = _build_docx_section_chunks(document=document, knowledge_units=knowledge_units)
            if structured_chunks:
                return structured_chunks
        unit_chunks = _build_unit_chunks(document=document, knowledge_units=knowledge_units)
        if unit_chunks:
            return unit_chunks
        base_chunks = tuple(
            self._base_builder.build(doc_id=document.doc_id, blocks=document.blocks)
        )
        if not base_chunks:
            return ()
        item_by_block_id = {
            item.block_ids[0]: item
            for item in document.items
            if len(item.block_ids) == 1
        }
        enriched: list[Chunk] = []
        for chunk in base_chunks:
            semantic_role = chunk.semantic_role
            text = chunk.text
            if len(chunk.block_ids) == 1:
                item = item_by_block_id.get(chunk.block_ids[0])
                if item is not None:
                    semantic_role = str(item.semantic_role or item.metadata.get("semantic_role") or semantic_role)
                    rendered_text = item.metadata.get("rendered_text")
                    text = str(rendered_text or item.text or text)
            enriched.append(replace(chunk, semantic_role=semantic_role, text=text))
        return tuple(enriched)


def _build_unit_chunks(
    *,
    document: ParsedDocumentArtifact,
    knowledge_units: Sequence[DocumentKnowledgeUnit],
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for unit in knowledge_units:
        if not unit.should_index_for_rag:
            continue
        text = str(unit.text or "").strip()
        if not text:
            continue
        chunks.append(
            Chunk(
                chunk_id=f"chk-{uuid4().hex[:12]}",
                doc_id=document.doc_id,
                block_ids=unit.source_block_ids,
                text=text,
                semantic_role=unit.semantic_role,
            )
        )
    return tuple(chunks)


def _build_docx_section_chunks(
    *,
    document: ParsedDocumentArtifact,
    knowledge_units: Sequence[DocumentKnowledgeUnit],
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    current_section: _ChunkAccumulator | None = None
    current_group: _ChunkAccumulator | None = None

    def flush_group() -> None:
        nonlocal current_group
        if current_group is None or not current_group.block_ids:
            current_group = None
            return
        text = "\n\n".join(part for part in current_group.text_parts if part.strip())
        if text.strip():
            chunks.append(
                Chunk(
                    chunk_id=f"chk-{uuid4().hex[:12]}",
                    doc_id=document.doc_id,
                    block_ids=tuple(current_group.block_ids),
                    text=text,
                    semantic_role=current_group.role,
                )
            )
        current_group = None

    def flush_section() -> None:
        nonlocal current_section
        if current_section is None or not current_section.block_ids:
            current_section = None
            return
        text = "\n\n".join(part for part in current_section.text_parts if part.strip())
        if text.strip():
            chunks.append(
                Chunk(
                    chunk_id=f"chk-{uuid4().hex[:12]}",
                    doc_id=document.doc_id,
                    block_ids=tuple(current_section.block_ids),
                    text=text,
                    semantic_role=current_section.role,
                )
            )
        current_section = None

    for unit in knowledge_units:
        metadata = unit.metadata or {}
        role = str(unit.semantic_role or metadata.get("semantic_role") or "paragraph").strip().lower()
        if not unit.should_index_for_rag:
            continue
        text = str(unit.text or "").strip()
        if not text:
            continue
        block_type = str(metadata.get("block_type") or metadata.get("kind") or "").strip().lower()
        is_section_heading = bool(metadata.get("is_section_heading"))

        if role == "title" and block_type == "title" and not is_section_heading:
            flush_group()
            flush_section()
            chunks.append(
                Chunk(
                    chunk_id=f"chk-{uuid4().hex[:12]}",
                    doc_id=document.doc_id,
                    block_ids=unit.source_block_ids,
                    text=text,
                    semantic_role=role,
                )
            )
            continue

        if is_section_heading and role in _DOCX_SECTION_ANCHOR_ROLES:
            flush_group()
            flush_section()
            current_section = _ChunkAccumulator(role=role, text_parts=[text], block_ids=list(unit.source_block_ids))
            continue

        if current_section is not None:
            if current_section.role == "front_matter" and role in _DOCX_GROUPABLE_ROLES - {"front_matter"}:
                current_section.role = role
            current_section.text_parts.append(text)
            current_section.block_ids.extend(unit.source_block_ids)
            continue

        if role in _DOCX_GROUPABLE_ROLES:
            if current_group is not None and current_group.role == role:
                current_group.text_parts.append(text)
                current_group.block_ids.extend(unit.source_block_ids)
            else:
                flush_group()
                current_group = _ChunkAccumulator(role=role, text_parts=[text], block_ids=list(unit.source_block_ids))
            continue

        flush_group()
        chunks.append(
            Chunk(
                chunk_id=f"chk-{uuid4().hex[:12]}",
                doc_id=document.doc_id,
                block_ids=unit.source_block_ids,
                text=text,
                semantic_role=role,
            )
        )

    flush_group()
    flush_section()
    return tuple(chunks)


def _entry_projection(
    item: DocumentArtifactItem,
    *,
    semantic_role: str | None = None,
) -> dict[str, Any]:
    metadata = item.metadata or {}
    resolved_role = semantic_role or item.semantic_role
    text_value = str(
        metadata.get("normalized_title")
        or metadata.get("table_title")
        or item.text
        or ""
    ).strip()
    if (
        resolved_role in {"front_matter", "body_section", "appendix", "revision_record", "distribution_list", "lep_entry"}
        and not metadata.get("normalized_title")
        and not metadata.get("table_title")
        and "\n" in text_value
    ):
        text_value = text_value.splitlines()[0].strip()
    entry = {
        "item_id": item.item_id,
        "semantic_role": resolved_role,
        "text": text_value,
    }
    logical_page_label = str(metadata.get("logical_page_label") or "").strip()
    if logical_page_label:
        entry["logical_page_label"] = logical_page_label
    if item.page_number is not None:
        entry["page_number"] = item.page_number
    table_type = str(metadata.get("table_type") or "").strip()
    if table_type:
        entry["table_type"] = table_type
    return entry


def _document_knowledge_units(document: ParsedDocumentArtifact) -> tuple[DocumentKnowledgeUnit, ...]:
    units = tuple(document.knowledge_units or ())
    if units:
        return units
    return _build_document_knowledge_units(document)


def _build_document_knowledge_units(document: ParsedDocumentArtifact) -> tuple[DocumentKnowledgeUnit, ...]:
    return tuple(
        _knowledge_unit_from_item(document.doc_id, item, index=index)
        for index, item in enumerate(document.items, start=1)
    )


def _knowledge_unit_from_item(doc_id: str, item: DocumentArtifactItem, *, index: int) -> DocumentKnowledgeUnit:
    metadata = dict(item.metadata or {})
    provenance = dict(item.provenance or {})
    role = str(item.semantic_role or metadata.get("semantic_role") or item.kind or "paragraph").strip().lower()
    unit_type = _knowledge_unit_type(item=item, semantic_role=role)
    text = _knowledge_unit_text(item=item, unit_type=unit_type)
    should_index = bool(text) and role not in _ARTIFACT_SEMANTIC_ROLES
    unit_metadata = dict(metadata)
    unit_metadata.update(
        {
            "item_id": item.item_id,
            "kind": item.kind,
            "block_type": str(provenance.get("block_type") or metadata.get("kind") or "").strip().lower(),
        }
    )
    if unit_type == "table":
        unit_metadata["table_summary"] = _knowledge_unit_table_summary(item)
    if unit_type == "figure_caption":
        unit_metadata["figure_summary"] = _knowledge_unit_figure_summary(item)
    return DocumentKnowledgeUnit(
        unit_id=f"{doc_id}:ku:{index:06d}",
        source_item_ids=(item.item_id,),
        source_block_ids=tuple(item.block_ids),
        source_table_ids=tuple(_knowledge_unit_table_ids(item=item, semantic_role=role, unit_type=unit_type)),
        unit_type=unit_type,
        semantic_role=role,
        text=text,
        page_span=_knowledge_unit_page_span(item),
        should_index_for_rag=should_index,
        skip_reason=None if should_index else _knowledge_unit_skip_reason(semantic_role=role, text=text),
        metadata=unit_metadata,
    )


def _knowledge_unit_text(*, item: DocumentArtifactItem, unit_type: str) -> str:
    if unit_type == "table":
        return _knowledge_unit_table_text(item)
    if unit_type == "figure_caption":
        return _knowledge_unit_figure_text(item)
    metadata = item.metadata or {}
    return str(metadata.get("rendered_text") or item.text or "").strip()


def _knowledge_unit_table_text(item: DocumentArtifactItem) -> str:
    metadata = item.metadata or {}
    caption = str(metadata.get("normalized_title") or metadata.get("table_title") or metadata.get("caption") or "").strip()
    rendered = str(metadata.get("rendered_text") or metadata.get("table_markdown") or "").strip()
    cells = _normalize_table_cells(metadata.get("cells"))
    if not rendered and cells:
        rendered = _render_table_text(
            cells,
            header_rows=max(1, _as_int(metadata.get("header_rows"), default=1)),
            output_format="markdown",
        )
    if not rendered:
        rendered = str(item.text or "").strip()
    parts: list[str] = []
    if caption and caption not in rendered:
        parts.append(caption)
    if rendered:
        parts.append(rendered)
    return "\n\n".join(parts).strip()


def _knowledge_unit_figure_text(item: DocumentArtifactItem) -> str:
    metadata = item.metadata or {}
    caption = str(item.text or "").strip()
    alt_text = str(metadata.get("alt_text") or "").strip()
    parts: list[str] = []
    if caption:
        parts.append(caption)
    if alt_text and alt_text not in parts:
        parts.append(alt_text)
    return "\n\n".join(parts).strip()


def _knowledge_unit_table_summary(item: DocumentArtifactItem) -> dict[str, Any]:
    metadata = item.metadata or {}
    cells = _normalize_table_cells(metadata.get("cells"))
    row_count = _as_int(metadata.get("rows", metadata.get("row_count")), default=len(cells))
    col_count = _as_int(
        metadata.get("cols", metadata.get("col_count")),
        default=max((len(row) for row in cells), default=0),
    )
    return {
        "caption": str(metadata.get("normalized_title") or metadata.get("table_title") or metadata.get("caption") or ""),
        "row_count": row_count,
        "col_count": col_count,
        "has_cells": bool(cells),
    }


def _knowledge_unit_figure_summary(item: DocumentArtifactItem) -> dict[str, Any]:
    metadata = item.metadata or {}
    return {
        "figure_kind": str(metadata.get("figure_kind") or metadata.get("figure_type") or "image"),
        "caption_confidence": metadata.get("caption_confidence"),
        "has_caption": bool(str(item.text or "").strip()),
        "has_alt_text": bool(str(metadata.get("alt_text") or "").strip()),
    }


def _knowledge_unit_type(*, item: DocumentArtifactItem, semantic_role: str) -> str:
    block_type = str((item.provenance or {}).get("block_type") or "").strip().lower()
    kind = str(item.kind or "").strip().lower()
    if block_type == "table" or kind == "table" or semantic_role == "table":
        return "table"
    if block_type == "image" or kind == "image" or semantic_role == "image":
        return "figure_caption"
    if block_type == "title" or semantic_role == "title":
        return "title"
    if semantic_role in _STRUCTURED_UNIT_ROLES:
        return semantic_role
    return "paragraph"


def _knowledge_unit_table_ids(
    *,
    item: DocumentArtifactItem,
    semantic_role: str,
    unit_type: str,
) -> tuple[str, ...]:
    metadata = item.metadata or {}
    table_ids = _string_list(metadata.get("source_table_ids"))
    table_id = str(metadata.get("table_id") or "").strip()
    if table_id:
        table_ids.append(table_id)
    if table_ids:
        return tuple(dict.fromkeys(table_ids))
    if unit_type == "table" or semantic_role == "table":
        return (item.item_id,)
    return ()


def _knowledge_unit_page_span(item: DocumentArtifactItem) -> tuple[int, int]:
    metadata = item.metadata or {}
    if metadata.get("page_span") is not None:
        return _page_span_tuple(metadata.get("page_span"))
    page_start = metadata.get("page_start", metadata.get("page"))
    page_end = metadata.get("page_end", page_start)
    if page_start is not None:
        start = _as_int(page_start, default=1)
        end = _as_int(page_end, default=start)
        return min(start, end), max(start, end)
    page = item.page_number or 1
    return int(page), int(page)


def _page_span_tuple(value: Any) -> tuple[int, int]:
    if isinstance(value, Mapping):
        start = _as_int(value.get("start", value.get("page_start")), default=1)
        end = _as_int(value.get("end", value.get("page_end")), default=start)
        return min(start, end), max(start, end)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _as_int(value[0], default=1)
        end = _as_int(value[1], default=start)
        return min(start, end), max(start, end)
    page = _as_int(value, default=1)
    return page, page


def _knowledge_unit_skip_reason(*, semantic_role: str, text: str) -> str:
    if not str(text or "").strip():
        return "empty_text"
    if semantic_role in _ARTIFACT_SEMANTIC_ROLES:
        return f"semantic_role:{semantic_role}"
    return "index_policy_skip"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _item_block_type(item: DocumentArtifactItem) -> str:
    metadata = item.metadata or {}
    return str(item.provenance.get("block_type") or metadata.get("kind") or "").strip().lower()


def _item_page_type(item: DocumentArtifactItem) -> str:
    return str((item.metadata or {}).get("page_type") or "").strip().lower()


def _has_evidence_anchor(item: DocumentArtifactItem) -> bool:
    metadata = item.metadata or {}
    if str(metadata.get("logical_page_label") or "").strip():
        return True
    return item.page_number is not None and int(item.page_number) > 0


def _looks_manual_heading_text(text: str) -> bool:
    normalized = " ".join(str(text or "").split())
    if not normalized or len(normalized) > 90:
        return False
    if _MANUAL_APPENDIX_HEADING_PATTERN.match(normalized):
        return True
    if _MANUAL_NUMBERED_HEADING_PATTERN.match(normalized):
        return True
    if _MANUAL_ALL_CAPS_HEADING_PATTERN.match(normalized):
        return True
    return False


def _infer_manual_role(item: DocumentArtifactItem) -> str | None:
    metadata = item.metadata or {}
    role = str(item.semantic_role or metadata.get("semantic_role") or "paragraph").strip().lower()
    if role in _ARTIFACT_SEMANTIC_ROLES:
        return None
    if bool(metadata.get("is_section_heading")) and role in _DOCX_SECTION_ANCHOR_ROLES:
        return role
    if role in {"toc_entry", "lep_entry", "revision_record", "distribution_list"}:
        return role

    text = str(
        metadata.get("normalized_title")
        or metadata.get("table_title")
        or item.text
        or ""
    ).strip()
    if not text:
        return None

    block_type = _item_block_type(item)
    page_type = _item_page_type(item)
    table_type = str(metadata.get("table_type") or "").strip().lower()
    if block_type == "table":
        if table_type == "effective_page_list":
            return "lep_entry"
        if table_type == "revision_record":
            return "revision_record"
        if table_type in {"distribution_list", "release_personnel_list", "outsourced_vendor_list"}:
            return "distribution_list"
        if table_type == "appendix_table":
            return "appendix"
        if _MANUAL_EFFECTIVE_PAGE_PATTERN.search(text):
            return "lep_entry"
        if _MANUAL_REVISION_PATTERN.search(text):
            return "revision_record"
        if _MANUAL_DISTRIBUTION_PATTERN.search(text):
            return "distribution_list"
        if _MANUAL_FRONT_MATTER_PATTERN.search(text):
            return "front_matter"
        return None

    if role == "title" and block_type == "title":
        return None

    normalized = " ".join(text.split())
    if _MANUAL_EFFECTIVE_PAGE_PATTERN.search(normalized):
        return "lep_entry"
    if _MANUAL_REVISION_PATTERN.search(normalized):
        return "revision_record"
    if _MANUAL_DISTRIBUTION_PATTERN.search(normalized):
        return "distribution_list"
    if _MANUAL_FRONT_MATTER_PATTERN.search(normalized):
        return "front_matter"
    if _MANUAL_APPENDIX_HEADING_PATTERN.match(normalized) or page_type == "appendix":
        if _looks_manual_heading_text(normalized):
            return "appendix"
    if page_type in {"toc", "signature"}:
        return "front_matter" if _looks_manual_heading_text(normalized) else None
    if _looks_manual_heading_text(normalized):
        return "body_section"
    return None


def _build_manual_anatomy(items: Sequence[DocumentArtifactItem]) -> dict[str, Any]:
    chapter_tree: list[dict[str, Any]] = []
    non_business_items: list[dict[str, Any]] = []
    body_sections: list[dict[str, Any]] = []
    tables_and_appendices: list[dict[str, Any]] = []
    suspected_noise: list[dict[str, Any]] = []

    for item in items:
        role = str(item.semantic_role or "paragraph").strip().lower()
        inferred_role = _infer_manual_role(item)
        projected = _entry_projection(item, semantic_role=inferred_role)
        if role in {"front_matter", "toc_entry", "lep_entry", "revision_record", "distribution_list"} or inferred_role in {
            "front_matter",
            "lep_entry",
            "revision_record",
            "distribution_list",
        }:
            non_business_items.append(projected)
        if inferred_role in {"body_section", "appendix"}:
            chapter_tree.append(projected)
        if inferred_role == "body_section":
            body_sections.append(projected)
        if inferred_role == "appendix" or _item_block_type(item) == "table":
            tables_and_appendices.append(projected)
        if role in _ARTIFACT_SEMANTIC_ROLES:
            suspected_noise.append(projected)

    return {
        "manual_parts": {
            "front_matter_items": len(non_business_items),
            "body_sections": len(body_sections),
            "tables_and_appendices": len(tables_and_appendices),
            "suspected_noise": len(suspected_noise),
        },
        "chapter_tree": chapter_tree,
        "non_business_items": non_business_items,
        "body_sections": body_sections,
        "tables_and_appendices": tables_and_appendices,
        "suspected_noise": suspected_noise,
    }


def _is_non_content_audit_item(item: DocumentArtifactItem) -> bool:
    metadata = item.metadata or {}
    return is_non_content_audit_placeholder(
        semantic_role=item.semantic_role,
        metadata=metadata,
    )


def _build_structure_quality(items: Sequence[DocumentArtifactItem]) -> dict[str, Any]:
    audit_artifact_items = [item for item in items if _is_non_content_audit_item(item)]
    quality_items = [item for item in items if not _is_non_content_audit_item(item)]
    total_items = max(len(quality_items), 1)
    heading_items: list[tuple[int, DocumentArtifactItem, str]] = []
    body_like_headings: list[tuple[int, DocumentArtifactItem, str]] = []
    for position, item in enumerate(quality_items):
        inferred_role = _infer_manual_role(item)
        if inferred_role in {"front_matter", "body_section", "appendix"}:
            heading_items.append((position, item, inferred_role))
        if inferred_role in {"body_section", "appendix"}:
            body_like_headings.append((position, item, inferred_role))
    noise_items = [
        item
        for item in quality_items
        if str(item.semantic_role or "").strip().lower() in _ARTIFACT_SEMANTIC_ROLES
    ]
    toc_candidates = [
        item
        for item in quality_items
        if str(item.semantic_role or "").strip().lower() in {"toc_entry", "lep_entry"}
        or str((item.metadata or {}).get("page_type") or "").strip().lower() == "toc"
    ]
    toc_recognized = [
        item
        for item in quality_items
        if str(item.semantic_role or "").strip().lower() in {"toc_entry", "lep_entry"}
    ]

    bound_sections = 0
    evidence_bound_items = 0
    relevant_for_evidence = 0
    for list_index, (start_position, item, _inferred_role) in enumerate(body_like_headings):
        if _has_evidence_anchor(item):
            evidence_bound_items += 1
        relevant_for_evidence += 1
        next_heading_position = (
            body_like_headings[list_index + 1][0]
            if list_index + 1 < len(body_like_headings)
            else len(quality_items)
        )
        for follower in quality_items[start_position + 1 : next_heading_position]:
            follower_role = str(follower.semantic_role or "paragraph").strip().lower()
            if follower_role in _ARTIFACT_SEMANTIC_ROLES:
                continue
            if _item_block_type(follower) == "table":
                relevant_for_evidence += 1
                if _has_evidence_anchor(follower):
                    evidence_bound_items += 1
            bound_sections += 1
            break

    directory_recognition_rate = len(toc_recognized) / max(len(toc_candidates), 1)
    chapter_coverage_rate = len(body_like_headings) / max(len(heading_items), 1)
    noise_ratio = len(noise_items) / total_items
    heading_body_binding_rate = bound_sections / max(len(body_like_headings), 1)
    evidence_binding_strength = evidence_bound_items / max(relevant_for_evidence, 1)
    structure_usability_score = round(
        (
            directory_recognition_rate
            + chapter_coverage_rate
            + (1.0 - noise_ratio)
            + heading_body_binding_rate
            + evidence_binding_strength
        )
        / 5.0,
        4,
    )
    return {
        "directory_recognition_rate": round(directory_recognition_rate, 4),
        "toc_recognition_rate": round(directory_recognition_rate, 4),
        "chapter_coverage_rate": round(chapter_coverage_rate, 4),
        "noise_ratio": round(noise_ratio, 4),
        "heading_body_binding_rate": round(heading_body_binding_rate, 4),
        "evidence_binding_strength": round(evidence_binding_strength, 4),
        "structure_usability_score": structure_usability_score,
        "counts": {
            "total_items": len(items),
            "quality_denominator_items": len(quality_items),
            "audit_artifact_items": len(audit_artifact_items),
            "heading_items": len(heading_items),
            "body_like_headings": len(body_like_headings),
            "noise_items": len(noise_items),
            "toc_items": len(toc_recognized),
        },
    }


class DocumentPipeline:
    def __init__(
        self,
        *,
        registration: PipelineRegistration,
        chunker: ArtifactChunker,
        runtime_stages: Sequence[EnrichmentStage],
    ) -> None:
        self.registration = registration
        self.parser = registration.parser
        self.chunker = chunker
        self.runtime_stages = tuple(runtime_stages)
        self._resolution_context: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.registration.pipeline_name

    @property
    def capabilities(self) -> PipelineCapabilities:
        return self.registration.capabilities

    def set_resolution_context(self, context: Mapping[str, Any]) -> None:
        self._resolution_context = dict(context)

    def validate(self, *, request: ParseRequest, purpose: str = "parse") -> None:
        normalized_purpose = str(purpose or "parse").strip().lower()
        if normalized_purpose == "parse" and not self.capabilities.supports_parse:
            raise RuntimeError(f"pipeline {self.name!r} does not support parse")
        if normalized_purpose == "rechunk" and not self.capabilities.supports_rechunk:
            raise RuntimeError(f"pipeline {self.name!r} does not support rechunk")
        if normalized_purpose == "reembed" and not self.capabilities.supports_reembed:
            raise RuntimeError(f"pipeline {self.name!r} does not support re-embed")

    def parse_blocks(self, *, request: ParseRequest) -> tuple[Block, ...]:
        self.validate(request=request, purpose="parse")
        return tuple(self.parser.parse(request))

    def build_document(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Block],
    ) -> ParsedDocumentArtifact:
        items = tuple(_build_document_items(blocks))
        document = ParsedDocumentArtifact(
            doc_id=request.doc_id,
            pipeline_name=self.registration.pipeline_name,
            parser_name=self.registration.parser_name,
            backend_name=self.registration.backend_name,
            format_name=self.registration.format_name,
            source_path=request.file_path,
            options_hash=_options_hash(_effective_options(self.registration.options, request.options)),
            blocks=tuple(blocks),
            items=items,
            metadata={
                "normalized_items": len(items),
                "pipeline_observability": {
                    "pipeline_name": self.registration.pipeline_name,
                    "options_hash": _options_hash(_effective_options(self.registration.options, request.options)),
                    "cache_key": str(self._resolution_context.get("cache_key") or ""),
                    "cache_hit": bool(self._resolution_context.get("cache_hit", False)),
                    "cache": {
                        "hits": int(self._resolution_context.get("cache_hits", 0) or 0),
                        "misses": int(self._resolution_context.get("cache_misses", 0) or 0),
                        "size": int(self._resolution_context.get("cache_size", 0) or 0),
                    },
                },
            },
        )
        active_runtime_stages: list[str] = []
        skipped_runtime_stages: list[str] = []
        failed_runtime_stages: list[dict[str, str]] = []
        for stage in self.runtime_stages:
            if not self._is_runtime_stage_enabled(stage=stage, request=request):
                skipped_runtime_stages.append(stage.spec.name)
                continue
            try:
                document = stage.run(request=request, document=document)
                active_runtime_stages.append(stage.spec.name)
            except Exception as exc:
                if not stage.spec.allow_failure:
                    raise
                failed_runtime_stages.append({"stage": stage.spec.name, "error": str(exc)})
        metadata = dict(document.metadata)
        metadata["active_runtime_stages"] = active_runtime_stages
        metadata["skipped_runtime_stages"] = skipped_runtime_stages
        metadata["failed_runtime_stages"] = failed_runtime_stages
        observability = dict(metadata.get("pipeline_observability") or {})
        observability["active_runtime_stages"] = list(active_runtime_stages)
        observability["skipped_runtime_stages"] = list(skipped_runtime_stages)
        observability["failed_runtime_stages"] = list(failed_runtime_stages)
        observability["parser_backed_stages"] = list(self.registration.capabilities.parser_backed_stage_names)
        metadata["pipeline_observability"] = observability
        document = replace(document, metadata=metadata)
        document = replace(document, knowledge_units=_build_document_knowledge_units(document))
        return document

    def build_chunks(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Block],
    ) -> tuple[Chunk, ...]:
        self.validate(
            request=request,
            purpose="rechunk" if str(request.options.get("mode", "")).strip().lower() == "rerun_chunks_only" else "parse",
        )
        document = self.build_document(request=request, blocks=blocks)
        return tuple(self.chunker.build(document=document))

    def _is_runtime_stage_enabled(self, *, stage: EnrichmentStage, request: ParseRequest) -> bool:
        spec = stage.spec
        if spec.execution != "runtime":
            return False
        effective_options = _effective_options(self.registration.options, request.options)
        if spec.enable_option_path:
            resolved = _lookup_option_path(effective_options, spec.enable_option_path)
            if resolved is not None:
                return _as_bool(resolved)
        return bool(spec.enabled_by_default)


class PipelineRegistry:
    def __init__(
        self,
        *,
        registrations: Sequence[PipelineRegistration],
        chunk_builder: ChunkBuilder,
        cache: PipelineCache | None = None,
    ) -> None:
        self.registrations = tuple(registrations)
        self._chunk_builder = chunk_builder
        self.cache = cache or PipelineCache()

    def warmup(self) -> None:
        for registration in self.registrations:
            self._get_or_create_pipeline(registration=registration, request_options={})

    def resolve(
        self,
        request: ParseRequest,
        *,
        purpose: str = "parse",
        parser_name: str | None = None,
    ) -> DocumentPipeline:
        suffix = Path(request.file_path).suffix.lower()
        preferred_parser = str(parser_name or "").strip().lower()
        for registration in self.registrations:
            if preferred_parser and registration.parser_name.strip().lower() != preferred_parser:
                continue
            if registration.parser.supports(media_type=request.media_type, suffix=suffix):
                pipeline = self._get_or_create_pipeline(
                    registration=registration,
                    request_options=request.options,
                )
                pipeline.validate(request=request, purpose=purpose)
                return pipeline
        if preferred_parser:
            raise LookupError(
                f"No pipeline registered for parser={preferred_parser!r}, "
                f"media_type={request.media_type!r}, suffix={suffix!r}"
            )
        raise LookupError(
            f"No pipeline registered for media_type={request.media_type!r}, suffix={suffix!r}"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "pipelines": [
                {
                    "name": registration.pipeline_name,
                    "format": registration.format_name,
                    "backend": registration.backend_name,
                    "parser": registration.parser_name,
                    "media_types": list(registration.media_types),
                    "extensions": list(registration.extensions),
                    "stages": [spec.name for spec in registration.stage_specs],
                    "runtime_stages": [spec.name for spec in registration.stage_specs if spec.execution == "runtime"],
                    "parser_backed_stages": [
                        spec.name for spec in registration.stage_specs if spec.execution == "parser-backed"
                    ],
                    "chunker": registration.capabilities.chunker_name,
                    "option_keys": list(registration.capabilities.option_keys),
                    "options": _json_safe_mapping(registration.options),
                }
                for registration in self.registrations
            ],
            "cache": self.cache.describe(),
        }

    def _get_or_create_pipeline(
        self,
        *,
        registration: PipelineRegistration,
        request_options: Mapping[str, Any],
    ) -> DocumentPipeline:
        effective_options = _effective_options(registration.options, request_options)
        options_hash = _options_hash(effective_options)
        cache_key = f"{registration.pipeline_name}:{options_hash}"
        pipeline, cache_hit = self.cache.get_or_create(
            key=cache_key,
            factory=lambda: DocumentPipeline(
                registration=registration,
                chunker=ArtifactBackedChunker(base_builder=self._chunk_builder),
                runtime_stages=(
                    PipelineProvenanceStage(registration),
                    TableStructureStage(registration),
                    DocumentSummaryStage(),
                ),
            ),
        )
        cache_snapshot = self.cache.describe()
        pipeline.set_resolution_context(
            {
                "pipeline_name": registration.pipeline_name,
                "options_hash": options_hash,
                "cache_key": cache_key,
                "cache_hit": cache_hit,
                "cache_hits": cache_snapshot.get("hits", 0),
                "cache_misses": cache_snapshot.get("misses", 0),
                "cache_size": cache_snapshot.get("size", 0),
            }
        )
        return pipeline


def build_pipeline_registry(
    *,
    parser_settings: Sequence[ParserSettings],
    parsers: Sequence[ParserAdapter],
    chunk_builder: ChunkBuilder,
) -> PipelineRegistry:
    registrations: list[PipelineRegistration] = []
    for settings, parser in zip(parser_settings, parsers, strict=True):
        format_name = _infer_format_name(settings)
        backend_name = _infer_backend_name(settings)
        stage_specs = _stage_specs_for_parser(settings)
        capabilities = PipelineCapabilities(
            pipeline_name=f"{settings.name}/default",
            format_name=format_name,
            backend_name=backend_name,
            parser_name=settings.name,
            stage_names=tuple(spec.name for spec in stage_specs),
            parser_backed_stage_names=tuple(
                spec.name for spec in stage_specs if spec.execution == "parser-backed"
            ),
            chunker_name=ArtifactBackedChunker.name,
            option_keys=_flatten_option_keys(settings.options),
        )
        registrations.append(
            PipelineRegistration(
                pipeline_name=capabilities.pipeline_name,
                format_name=format_name,
                backend_name=backend_name,
                parser_name=settings.name,
                media_types=settings.media_types,
                extensions=settings.extensions,
                options=settings.options,
                parser=parser,
                stage_specs=stage_specs,
                capabilities=capabilities,
            )
        )
    return PipelineRegistry(registrations=registrations, chunk_builder=chunk_builder)


def _build_document_items(blocks: Sequence[Block]) -> list[DocumentArtifactItem]:
    items: list[DocumentArtifactItem] = []
    for position, block in enumerate(blocks, start=1):
        metadata = dict(block.metadata)
        semantic_role = str(metadata.get("semantic_role") or block.type.value)
        metadata["semantic_role"] = semantic_role
        metadata.setdefault("item_kind", semantic_role)
        metadata.setdefault("structure_tags", _build_structure_tags(metadata=metadata, semantic_role=semantic_role))
        items.append(
            DocumentArtifactItem(
                item_id=f"itm-{position}",
                block_ids=(block.block_id,),
                kind=semantic_role,
                semantic_role=semantic_role,
                text=block.content,
                page_number=_safe_int(metadata.get("page")),
                metadata=metadata,
                provenance={
                    "block_type": block.type.value,
                    "parser": str(metadata.get("parser") or ""),
                    "position": position,
                    "semantic_role": semantic_role,
                },
            )
        )
    return items


def _build_structure_tags(*, metadata: Mapping[str, Any], semantic_role: str) -> list[str]:
    tags: list[str] = [f"role:{semantic_role}"]
    page_type = str(metadata.get("page_type") or "").strip().lower()
    if page_type:
        tags.append(f"page:{page_type}")
    parser_name = str(metadata.get("parser") or "").strip().lower()
    if parser_name:
        tags.append(f"parser:{parser_name}")
    kind = str(metadata.get("kind") or "").strip().lower()
    if kind:
        tags.append(f"kind:{kind}")
    return tags


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_format_name(settings: ParserSettings) -> str:
    normalized_name = settings.name.strip().lower()
    if normalized_name == "pdf-text":
        return "pdf"
    if normalized_name == "docx-native":
        return "docx"
    if normalized_name == "image-ocr":
        return "image"
    if normalized_name == "text-native":
        return "text"
    if settings.extensions:
        return settings.extensions[0].lstrip(".").lower() or normalized_name
    return normalized_name


def _infer_backend_name(settings: ParserSettings) -> str:
    normalized_name = settings.name.strip().lower()
    if normalized_name == "pdf-text":
        return "native-text"
    if normalized_name == "docx-native":
        return "native-xml"
    if normalized_name == "image-ocr":
        return "ocr"
    if normalized_name == "text-native":
        return "native-text"
    return normalized_name


def _stage_specs_for_parser(settings: ParserSettings) -> tuple[PipelineStageSpec, ...]:
    normalized_name = settings.name.strip().lower()
    specs: list[PipelineStageSpec] = [
        PipelineStageSpec(name="normalized-items", execution="runtime", enabled_by_default=True),
        PipelineProvenanceStage.spec,
        TableStructureStage.spec,
        DocumentSummaryStage.spec,
    ]
    if normalized_name == "pdf-text":
        specs.extend(
            [
                PipelineStageSpec(
                    name="layout-reading-order",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.layout_reading_order",
                        "enrichment.layout_reading_order.enabled",
                    ),
                ),
                PipelineStageSpec(
                    name="table-detection",
                    execution="parser-backed",
                    enabled_by_default=True,
                    option_keys=(
                        "post_process.dual_channel",
                        "post_process.merge_table_continuations",
                    ),
                ),
                PipelineStageSpec(
                    name="ocr-fallback",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.ocr_bad_pages",
                        "enable_ocr",
                    ),
                ),
                PipelineStageSpec(
                    name="boundary-refinement",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.llm_refine_min_length",
                        "post_process.llm_refine_min_markers",
                    ),
                    requires_remote_services=True,
                ),
            ]
        )
    if normalized_name == "image-ocr":
        specs.append(
            PipelineStageSpec(
                name="ocr-extract",
                execution="parser-backed",
                enabled_by_default=True,
            )
        )
    return tuple(specs)


def _flatten_option_keys(options: Mapping[str, Any], *, prefix: str = "") -> tuple[str, ...]:
    collected: list[str] = []
    for key, value in options.items():
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        collected.append(path)
        if isinstance(value, Mapping):
            collected.extend(_flatten_option_keys(value, prefix=path))
    return tuple(sorted(dict.fromkeys(collected)))


def _effective_options(
    registered_options: Mapping[str, Any],
    request_options: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _json_safe_mapping(registered_options)
    for key, value in _json_safe_mapping(request_options).items():
        if key == "mode":
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(merged[key], value)
            continue
        merged[key] = value
    return merged


def _merge_mappings(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(merged[key], value)
            continue
        merged[key] = value
    return merged


def _options_hash(options: Mapping[str, Any]) -> str:
    payload = json.dumps(_json_safe_mapping(options), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _json_safe_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe_value(value) for key, value in mapping.items()}


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _lookup_option_path(options: Mapping[str, Any], path: str) -> Any:
    current: Any = options
    for segment in str(path).split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _normalize_table_cells(raw_cells: Any) -> list[list[str]]:
    if not isinstance(raw_cells, Sequence) or isinstance(raw_cells, (str, bytes, bytearray)):
        return []
    rows: list[list[str]] = []
    for row in raw_cells:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
            continue
        normalized_row = ["" if cell is None else str(cell).strip() for cell in row]
        if any(cell for cell in normalized_row):
            rows.append(normalized_row)
    return rows


def _render_table_text(
    cells: Sequence[Sequence[str]],
    *,
    header_rows: int,
    output_format: str,
) -> str:
    normalized_rows = _pad_rows(cells)
    if not normalized_rows:
        return ""
    normalized_format = output_format.lower()
    if normalized_format == "tsv":
        return "\n".join("\t".join(row) for row in normalized_rows)

    header_count = min(max(1, header_rows), len(normalized_rows))
    header = normalized_rows[0]
    body = normalized_rows[header_count:]
    separator = ["---"] * len(header)
    lines = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join(separator)} |",
    ]
    if not body and header_count > 1:
        body = normalized_rows[1:]
    for row in body:
        lines.append(f"| {' | '.join(row)} |")
    return "\n".join(lines)


def _pad_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    width = max((len(row) for row in rows), default=0)
    if width <= 0:
        return []
    padded: list[list[str]] = []
    for row in rows:
        normalized_row = [str(cell).replace("\n", " ").strip() for cell in row]
        padded.append(normalized_row + [""] * (width - len(normalized_row)))
    return padded
