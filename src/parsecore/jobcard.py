from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Protocol

from .contracts import ProductAdapter
from .models import Block, ParseJob, ParseOutcome, ParseRequest


class MountableApp(Protocol):
    def mount(self, path: str, app: Any) -> None: ...


def mount_into_fastapi(app: MountableApp, *, config_path: str = "parsecore.toml", prefix: str = "/internal/parsecore") -> Any:
    from .asgi import create_app

    sub_app = create_app(config_path)
    app.mount(prefix, sub_app)
    return sub_app


def build_jobcard_document_patch(outcome: ParseOutcome) -> dict[str, Any]:
    content_blocks = _content_blocks(outcome.blocks)
    plain_segments = [block.content for block in content_blocks]
    pages = _group_blocks_by_page(content_blocks)
    return {
        "parseStatus": "PARSED",
        "parseError": None,
        "lastParseAt": outcome.job.updated_at,
        "parsedTextContent": {
            "plainText": "\n\n".join(plain_segments),
            "pages": pages,
            "totalPages": max((page["pageNumber"] for page in pages), default=0),
            "hasStructuredPages": bool(pages),
        },
        "parsecore": {
            "job": asdict(outcome.job),
            "blocks": [asdict(block) for block in outcome.blocks],
            "chunks": [asdict(chunk) for chunk in outcome.chunks],
        },
    }


def build_jobcard_failure_patch(job: ParseJob, *, error: str) -> dict[str, Any]:
    return {
        "parseStatus": "FAILED",
        "parseError": error,
        "lastParseAt": job.updated_at,
        "parsecore": {
            "job": asdict(job),
        },
    }


class JobcardProductAdapter(ProductAdapter):
    def __init__(self, sink: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.sink = sink
        self.events: list[dict[str, Any]] = []

    def before_parse(self, *, request: ParseRequest, job: ParseJob) -> None:
        patch = {
            "parseStatus": "PARSING",
            "parseError": None,
            "lastParseAt": job.updated_at,
            "parsecore": {"job": asdict(job)},
        }
        self._emit(request.doc_id, patch)

    def after_parse(self, *, outcome: ParseOutcome) -> None:
        self._emit(outcome.job.doc_id, build_jobcard_document_patch(outcome))

    def on_failure(self, *, request: ParseRequest, job: ParseJob, error: Exception) -> None:
        self._emit(request.doc_id, build_jobcard_failure_patch(job, error=str(error)))

    def _emit(self, doc_id: str, patch: dict[str, Any]) -> None:
        event = {"doc_id": doc_id, "patch": patch}
        self.events.append(event)
        if self.sink is not None:
            self.sink(doc_id, patch)


def _content_blocks(blocks: tuple[Block, ...]) -> tuple[Block, ...]:
    return tuple(
        block
        for block in blocks
        if block.type != block.type.TITLE and block.content.strip()
    )


def _group_blocks_by_page(blocks: tuple[Block, ...]) -> list[dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    for block in blocks:
        page_number = int(block.metadata.get("page", 1))
        entry = pages.setdefault(
            page_number,
            {"pageNumber": page_number, "text": [], "blockIds": [], "blockCount": 0},
        )
        entry["text"].append(block.content)
        entry["blockIds"].append(block.block_id)
        entry["blockCount"] += 1
    ordered = []
    for page_number in sorted(pages):
        page = pages[page_number]
        ordered.append(
            {
                "pageNumber": page["pageNumber"],
                "text": "\n\n".join(item for item in page["text"] if item.strip()),
                "blockIds": page["blockIds"],
                "blockCount": page["blockCount"],
            }
        )
    return ordered