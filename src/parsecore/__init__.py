from .bootstrap import build_runtime
from .jobcard import JobcardProductAdapter, build_jobcard_document_patch, build_jobcard_failure_patch, mount_into_fastapi
from .models import Block, BlockType, Chunk, ChunkSearchHit, ParseJob, ParseJobState, ParseOutcome, ParseRequest, SemanticRole
from .parsers import DocxParser, PdfTextParser, TextParser
from .runtime import ParseRuntime
from .stores import PgVectorIndex, PostgresJobStore, SQLiteJobStore
from .worker import QueueWorker, build_worker, run_worker

__all__ = [
    "Block",
    "BlockType",
    "Chunk",
    "ChunkSearchHit",
    "DocxParser",
    "JobcardProductAdapter",
    "ParseJob",
    "ParseJobState",
    "ParseOutcome",
    "ParseRequest",
    "ParseRuntime",
    "PdfTextParser",
    "PgVectorIndex",
    "PostgresJobStore",
    "QueueWorker",
    "SQLiteJobStore",
    "SemanticRole",
    "TextParser",
    "build_jobcard_document_patch",
    "build_jobcard_failure_patch",
    "build_worker",
    "build_runtime",
    "create_app",
    "mount_into_fastapi",
    "run_worker",
]


def __getattr__(name: str):
    if name == "create_app":
        from .asgi import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
