from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ParseJobState(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    STRUCTURING = "structuring"
    EMBEDDING = "embedding"
    DONE = "done"
    FAILED = "failed"


class BlockType(str, Enum):
    TITLE = "title"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"


class SemanticRole(str, Enum):
    TITLE = "title"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"
    FRONT_MATTER = "front_matter"
    BODY_SECTION = "body_section"
    APPENDIX = "appendix"
    TOC_ENTRY = "toc_entry"
    LEP_ENTRY = "lep_entry"
    REVISION_RECORD = "revision_record"
    DISTRIBUTION_LIST = "distribution_list"
    HIGHLIGHTS_ENTRY = "highlights_entry"
    NOTE = "note"
    WARNING = "warning"
    CAUTION = "caution"
    HEADER_FOOTER = "header_footer"
    PARSE_ARTIFACT = "parse_artifact"
    VERSION_CELL = "version_cell"
    PAGE_REF_CELL = "page_ref_cell"


@dataclass(slots=True, frozen=True)
class ParseRequest:
    doc_id: str
    file_path: str
    media_type: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    quota_key: str = "default"
    quota_units: int = 1


@dataclass(slots=True)
class ParseJob:
    job_id: str
    doc_id: str
    file_path: str
    state: ParseJobState
    media_type: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    quota_key: str = "default"
    quota_units: int = 1
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    failure_reason: str | None = None
    attempt_count: int = 0
    dead_lettered_at: str | None = None


@dataclass(slots=True, frozen=True)
class Block:
    block_id: str
    doc_id: str
    type: BlockType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    block_ids: tuple[str, ...]
    text: str
    language: str = "und"
    semantic_role: str = SemanticRole.PARAGRAPH.value
    embedding: tuple[float, ...] | None = None


@dataclass(slots=True)
class ParseOutcome:
    job: ParseJob
    blocks: tuple[Block, ...]
    chunks: tuple[Chunk, ...]


@dataclass(slots=True, frozen=True)
class ChunkSearchHit:
    chunk_id: str
    doc_id: str
    block_ids: tuple[str, ...]
    text: str
    semantic_role: str
    score: float


@dataclass(slots=True, frozen=True)
class StructureSearchHit:
    item_id: str
    doc_id: str
    block_ids: tuple[str, ...]
    text: str
    semantic_role: str
    structure_tags: tuple[str, ...] = ()
    page_number: int | None = None
    score: float = 0.0
