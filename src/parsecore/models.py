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


@dataclass(slots=True, frozen=True)
class ParseRequest:
    doc_id: str
    file_path: str
    media_type: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParseJob:
    job_id: str
    doc_id: str
    file_path: str
    state: ParseJobState
    media_type: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
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
    embedding: tuple[float, ...] | None = None


@dataclass(slots=True)
class ParseOutcome:
    job: ParseJob
    blocks: tuple[Block, ...]
    chunks: tuple[Chunk, ...]
