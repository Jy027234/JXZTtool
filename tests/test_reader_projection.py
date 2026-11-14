from __future__ import annotations

from types import SimpleNamespace

from parsecore.api_payloads import _document_projection
from parsecore.models import Block, BlockType, ParseJobState


def test_pdf_text_mixed_positions_order_reader_blocks_without_changing_ir() -> None:
    doc_id = "doc-reader-mixed-position"
    job = SimpleNamespace(
        job_id="job-reader-mixed-position",
        doc_id=doc_id,
        state=ParseJobState.DONE,
        media_type="application/pdf",
        options={"profile": "default"},
    )
    blocks = (
        Block(
            block_id="blk-title",
            doc_id=doc_id,
            type=BlockType.TITLE,
            content="Maintenance manual",
            metadata={"page": 1, "provider_id": "pdf-text", "parser": "pdf-text", "semantic_role": "title"},
        ),
        Block(
            block_id="blk-top-image",
            doc_id=doc_id,
            type=BlockType.IMAGE,
            content="Top screenshot",
            metadata={"page": 48, "provider_id": "pdf-text", "parser": "pdf-text", "position": 1, "semantic_role": "image"},
        ),
        Block(
            block_id="blk-instructions",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Follow these instructions.",
            metadata={
                "page": 48,
                "provider_id": "pdf-text",
                "parser": "pdf-text",
                "position": 2,
                "page_position": 1,
                "semantic_role": "paragraph",
            },
        ),
        Block(
            block_id="blk-bottom-image",
            doc_id=doc_id,
            type=BlockType.IMAGE,
            content="Bottom screenshot",
            metadata={"page": 48, "provider_id": "pdf-text", "parser": "pdf-text", "position": 3, "semantic_role": "image"},
        ),
        # Explicit ordering keeps the page on its existing reading_order keys.
        Block(
            block_id="blk-explicit-image",
            doc_id=doc_id,
            type=BlockType.IMAGE,
            content="Explicit order image",
            metadata={
                "page": 49,
                "provider_id": "pdf-text",
                "parser": "pdf-text",
                "position": 4,
                "reading_order": 2,
                "semantic_role": "image",
            },
        ),
        Block(
            block_id="blk-explicit-text",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Explicit order text.",
            metadata={
                "page": 49,
                "provider_id": "pdf-text",
                "parser": "pdf-text",
                "position": 5,
                "page_position": 1,
                "reading_order": 1,
                "semantic_role": "paragraph",
            },
        ),
        # Other providers continue to ignore this PDF-specific fallback.
        Block(
            block_id="blk-other-provider-first",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="First source block.",
            metadata={"page": 50, "provider_id": "docx-local", "parser": "docx", "position": 20, "semantic_role": "paragraph"},
        ),
        Block(
            block_id="blk-other-provider-second",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Second source block.",
            metadata={"page": 50, "provider_id": "docx-local", "parser": "docx", "position": 10, "semantic_role": "paragraph"},
        ),
        # Unknown/legacy page positions keep the old Reader sort behavior.
        Block(
            block_id="blk-legacy-image",
            doc_id=doc_id,
            type=BlockType.IMAGE,
            content="Legacy screenshot",
            metadata={"page": 51, "provider_id": "pdf-text", "parser": "pdf-text", "semantic_role": "image"},
        ),
        Block(
            block_id="blk-legacy-text",
            doc_id=doc_id,
            type=BlockType.PARAGRAPH,
            content="Legacy paragraph.",
            metadata={
                "page": 51,
                "provider_id": "pdf-text",
                "parser": "pdf-text",
                "page_position": 1,
                "semantic_role": "paragraph",
            },
        ),
    )
    snapshot = {"job": job, "doc_id": doc_id, "blocks": blocks, "chunks": ()}

    ir = _document_projection(snapshot, projection="ir")
    reader = _document_projection(snapshot, projection="reader")

    ir_reading_order = {block["block_id"]: block["reading_order"] for block in ir["blocks"]}
    assert ir_reading_order["blk-top-image"] == 2
    assert ir_reading_order["blk-instructions"] == 1
    assert ir_reading_order["blk-bottom-image"] == 4

    reader_blocks_by_page = {
        page: [block for block in reader["blocks"] if block["page_number"] == page]
        for page in (48, 49, 50, 51)
    }
    assert [block["source_block_ids"][0] for block in reader_blocks_by_page[48]] == [
        "blk-top-image",
        "blk-instructions",
        "blk-bottom-image",
    ]
    assert [block["reading_order"] for block in reader_blocks_by_page[48]] == [1, 2, 3]
    assert [block["source_block_ids"][0] for block in reader_blocks_by_page[49]] == [
        "blk-explicit-text",
        "blk-explicit-image",
    ]
    assert [block["source_block_ids"][0] for block in reader_blocks_by_page[50]] == [
        "blk-other-provider-first",
        "blk-other-provider-second",
    ]
    assert [block["source_block_ids"][0] for block in reader_blocks_by_page[51]] == [
        "blk-legacy-text",
        "blk-legacy-image",
    ]
