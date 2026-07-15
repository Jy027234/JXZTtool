from parsecore.ir import build_coverage_payload
from parsecore.models import Chunk


def test_coverage_expands_cross_page_block_span() -> None:
    payload = build_coverage_payload(
        doc_id="doc-span",
        parse_run_id="run-span",
        profile="auto",
        state="done",
        blocks=(),
        chunks=(
            Chunk(
                chunk_id="chunk-span",
                doc_id="doc-span",
                block_ids=("block-span",),
                text="continued paragraph",
                embedding=(0.1, 0.2),
            ),
        ),
        pages=({"page_number": 3},),
        ir_blocks=(
            {
                "block_id": "block-span",
                "page_number": 3,
                "page_span": [3, 5],
                "text": "continued paragraph",
                "index_policy": "inline",
                "semantic_role": "paragraph",
                "provenance": {"provider_id": "pdf-text"},
            },
        ),
        tables=(),
        figures=(),
        knowledge_units=(
            {
                "unit_id": "unit-span",
                "doc_id": "doc-span",
                "source_block_ids": ["block-span"],
                "page_span": [3, 5],
                "text": "continued paragraph",
                "unit_type": "paragraph",
                "semantic_role": "paragraph",
                "should_index_for_rag": True,
                "chunk_ids": ["chunk-span"],
            },
        ),
        quality_signals=(),
        index_manifest=None,
    )

    pages = payload["coverage"]["pages"]
    assert [page["page_number"] for page in pages] == [3, 4, 5]
    assert all(page["block_count"] == 1 for page in pages)
    assert all(page["chunk_ids"] == ["chunk-span"] for page in pages)
    assert payload["coverage"]["summary"]["total_indexable_units"] == 1
    assert payload["coverage"]["summary"]["total_chunked_units"] == 1
    assert payload["coverage"]["summary"]["text_page_coverage_ratio"] == 1.0
