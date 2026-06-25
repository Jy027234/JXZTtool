from .bootstrap import build_runtime
from .models import Block, BlockType, Chunk, ChunkSearchHit, ParseJob, ParseJobState, ParseOutcome, ParseRequest, SemanticRole
from .parsers import DocxParser, PdfTextParser, PyMuPdf4LlmParser, TextParser
from .runtime import ParseRuntime
from .stores import PgVectorIndex, PostgresJobStore, SQLiteJobStore
from .worker import QueueWorker, build_worker, run_worker

__all__ = [
    "Block",
    "BlockType",
    "Chunk",
    "ChunkSearchHit",
    "DocxParser",
    "ParseJob",
    "ParseJobState",
    "ParseOutcome",
    "ParseRequest",
    "ParseRuntime",
    "PdfTextParser",
    "PgVectorIndex",
    "PostgresJobStore",
    "PyMuPdf4LlmParser",
    "QueueWorker",
    "SQLiteJobStore",
    "SemanticRole",
    "TextParser",
    "build_worker",
    "build_runtime",
    "create_app",
    "run_worker",
]


def __getattr__(name: str):
    if name == "create_app":
        from .asgi import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
