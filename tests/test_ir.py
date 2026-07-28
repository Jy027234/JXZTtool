from parsecore.ir import build_coverage_payload, build_knowledge_unit_diff
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


def test_knowledge_unit_contract_is_stable_hierarchical_continuous_and_fully_accounted() -> None:
    ir_blocks = (
        {
            "block_id": "heading-1",
            "page_number": 1,
            "page_span": [1, 1],
            "text": "1 General",
            "semantic_role": "body_section",
            "is_section_heading": True,
            "heading_level": 1,
            "normalized_title": "1 General",
            "section_no": "1",
            "index_policy": "inline",
            "continuation": {},
            "provenance": {"provider_id": "fixture"},
        },
        {
            "block_id": "body-1",
            "page_number": 1,
            "page_span": [1, 1],
            "text": "General requirement.",
            "semantic_role": "paragraph",
            "is_section_heading": False,
            "heading_level": None,
            "normalized_title": "",
            "section_no": "",
            "index_policy": "inline",
            "continuation": {},
            "provenance": {"provider_id": "fixture"},
        },
        {
            "block_id": "heading-1-1",
            "page_number": 2,
            "page_span": [2, 2],
            "text": "1.1 Scope",
            "semantic_role": "body_section",
            "is_section_heading": True,
            "heading_level": 2,
            "normalized_title": "1.1 Scope",
            "section_no": "1.1",
            "index_policy": "inline",
            "continuation": {},
            "provenance": {"provider_id": "fixture"},
        },
        {
            "block_id": "procedure-a",
            "page_number": 3,
            "page_span": [3, 3],
            "text": "Step one",
            "semantic_role": "procedure",
            "is_section_heading": False,
            "heading_level": None,
            "normalized_title": "",
            "section_no": "",
            "index_policy": "inline",
            "continuation": {"group_id": "procedure-1"},
            "provenance": {"provider_id": "fixture"},
        },
        {
            "block_id": "procedure-b",
            "page_number": 4,
            "page_span": [4, 4],
            "text": "Step two",
            "semantic_role": "procedure",
            "is_section_heading": False,
            "heading_level": None,
            "normalized_title": "",
            "section_no": "",
            "index_policy": "inline",
            "continuation": {"group_id": "procedure-1", "is_continuation": True},
            "provenance": {"provider_id": "fixture"},
        },
    )
    chunks = tuple(
        Chunk(
            chunk_id=f"chunk-{index}",
            doc_id="doc-contract",
            block_ids=(str(block["block_id"]),),
            text=str(block["text"]),
            embedding=(0.1, 0.2),
        )
        for index, block in enumerate(ir_blocks, start=1)
    )
    units = tuple(
        {
            "unit_id": f"legacy-{index}",
            "doc_id": "doc-contract",
            "source_block_ids": [str(block["block_id"])],
            "source_table_ids": [],
            "page_span": list(block["page_span"]),
            "text": str(block["text"]),
            "unit_type": "title" if bool(block["is_section_heading"]) else "paragraph",
            "semantic_role": str(block["semantic_role"]),
            "should_index_for_rag": True,
            "chunk_ids": [f"chunk-{index}"],
        }
        for index, block in enumerate(ir_blocks, start=1)
    )

    def build() -> dict:
        return build_coverage_payload(
            doc_id="doc-contract",
            parse_run_id="run-contract",
            profile="auto",
            state="done",
            blocks=(),
            chunks=chunks,
            pages=tuple({"page_number": page} for page in range(1, 5)),
            ir_blocks=ir_blocks,
            tables=(),
            figures=(),
            knowledge_units=units,
            quality_signals=(),
            index_manifest=None,
        )

    first = build()
    second = build()
    first_units = first["coverage"]["units"]
    second_units = second["coverage"]["units"]

    assert [unit["stable_unit_id"] for unit in first_units] == [
        unit["stable_unit_id"] for unit in second_units
    ]
    assert all(len(unit["unit_fingerprint"]) == 64 for unit in first_units)
    assert all(len(unit["structure_fingerprint"]) == 64 for unit in first_units)
    assert first_units[1]["source_span"]["precision"] == "page"
    assert first_units[2]["parent_section_id"] == first_units[0]["section_id"]
    assert first_units[3]["section_id"] == first_units[2]["section_id"]
    assert first_units[3]["title_path"] == ["1 General", "1.1 Scope"]
    assert first_units[4]["continuity"]["continues_from_unit_id"] == first_units[3]["stable_unit_id"]
    assert first_units[3]["continuity"]["continues_to_unit_id"] == first_units[4]["stable_unit_id"]
    summary = first["coverage"]["summary"]
    assert summary["accounted_unit_count"] == len(first_units)
    assert summary["unaccounted_unit_count"] == 0
    assert summary["processing_status_counts"]["processed"] == len(first_units)


def test_coverage_reports_heading_level_and_section_number_gaps() -> None:
    headings = (
        ("heading-1", "1 General", "1", 1),
        ("heading-1-1", "1.1 Scope", "1.1", 2),
        ("heading-1-3", "1.3 Responsibilities", "1.3", 2),
        ("heading-1-3-1-1", "1.3.1.1 Records", "1.3.1.1", 4),
    )
    ir_blocks = tuple(
        {
            "block_id": block_id,
            "page_number": index,
            "page_span": [index, index],
            "text": title,
            "semantic_role": "body_section",
            "is_section_heading": True,
            "heading_level": level,
            "normalized_title": title,
            "section_no": section_no,
            "index_policy": "inline",
            "continuation": {},
            "provenance": {"provider_id": "fixture"},
        }
        for index, (block_id, title, section_no, level) in enumerate(headings, start=1)
    )
    units = tuple(
        {
            "unit_id": f"unit-{index}",
            "doc_id": "doc-quality",
            "source_block_ids": [str(block["block_id"])],
            "source_table_ids": [],
            "page_span": list(block["page_span"]),
            "text": str(block["text"]),
            "unit_type": "title",
            "semantic_role": "body_section",
            "should_index_for_rag": False,
            "chunk_ids": [],
        }
        for index, block in enumerate(ir_blocks, start=1)
    )

    payload = build_coverage_payload(
        doc_id="doc-quality",
        parse_run_id="run-quality",
        profile="auto",
        state="done",
        blocks=(),
        chunks=(),
        pages=tuple({"page_number": page} for page in range(1, 5)),
        ir_blocks=ir_blocks,
        tables=(),
        figures=(),
        knowledge_units=units,
        quality_signals=(),
        index_manifest=None,
    )

    codes = {signal["code"] for signal in payload["quality_signals"]}
    assert "structure_section_number_jump" in codes
    assert "structure_heading_level_jump" in codes
    page_codes = {
        code
        for page in payload["coverage"]["pages"]
        for code in page["quality_signal_codes"]
    }
    assert {"structure_section_number_jump", "structure_heading_level_jump"}.issubset(page_codes)


def test_knowledge_unit_diff_classifies_all_change_kinds() -> None:
    def unit(
        unit_id: str,
        *,
        unit_fingerprint: str,
        content_fingerprint: str,
        structure_fingerprint: str,
        page: int,
    ) -> dict:
        return {
            "unit_id": unit_id,
            "stable_unit_id": f"stable-{unit_id}",
            "unit_fingerprint": unit_fingerprint,
            "content_fingerprint": content_fingerprint,
            "structure_fingerprint": structure_fingerprint,
            "page_span": [page, page],
            "section_no": unit_id,
            "title_path": [unit_id],
        }

    previous = (
        unit("same", unit_fingerprint="uf-same", content_fingerprint="cf-same", structure_fingerprint="sf-same", page=1),
        unit("move-old", unit_fingerprint="uf-move-old", content_fingerprint="cf-move", structure_fingerprint="sf-old", page=2),
        unit("change-old", unit_fingerprint="uf-change-old", content_fingerprint="cf-old", structure_fingerprint="sf-change", page=3),
        unit("removed", unit_fingerprint="uf-removed", content_fingerprint="cf-removed", structure_fingerprint="sf-removed", page=4),
    )
    current = (
        unit("same", unit_fingerprint="uf-same", content_fingerprint="cf-same", structure_fingerprint="sf-same", page=1),
        unit("move-new", unit_fingerprint="uf-move-new", content_fingerprint="cf-move", structure_fingerprint="sf-new", page=5),
        unit("change-new", unit_fingerprint="uf-change-new", content_fingerprint="cf-new", structure_fingerprint="sf-change", page=3),
        unit("added", unit_fingerprint="uf-added", content_fingerprint="cf-added", structure_fingerprint="sf-added", page=6),
    )

    diff = build_knowledge_unit_diff(
        previous_units=previous,
        current_units=current,
        previous_parse_run_id="run-old",
        current_parse_run_id="run-new",
    )

    assert diff["complete"] is True
    assert diff["counts"] == {
        "unchanged": 1,
        "added": 1,
        "changed": 1,
        "removed": 1,
        "relocated": 1,
        "unknown": 0,
    }


def test_coverage_reports_missing_list_parent_and_broken_table_continuation() -> None:
    ir_blocks = (
        {
            "block_id": "nested-list",
            "page_number": 1,
            "page_span": [1, 1],
            "type": "paragraph",
            "text": "  (a) Nested without parent",
            "semantic_role": "list_item",
            "list_level": 2,
            "list_marker": "(a)",
            "list_parent_block_id": "",
            "is_section_heading": False,
            "heading_level": None,
            "normalized_title": "",
            "section_no": "",
            "index_policy": "inline",
            "bbox": None,
            "reading_order": 1,
            "continuation": {},
            "provenance": {"provider_id": "fixture"},
        },
        {
            "block_id": "continued-table",
            "page_number": 2,
            "page_span": [2, 2],
            "type": "table",
            "text": "Part | Qty",
            "semantic_role": "table",
            "list_level": 0,
            "list_marker": "",
            "list_parent_block_id": "",
            "is_section_heading": False,
            "heading_level": None,
            "normalized_title": "",
            "section_no": "",
            "index_policy": "inline",
            "bbox": None,
            "reading_order": 2,
            "continuation": {"is_continuation": True},
            "provenance": {"provider_id": "fixture"},
        },
    )
    units = tuple(
        {
            "unit_id": f"unit-{index}",
            "doc_id": "doc-structure-gaps",
            "source_block_ids": [str(block["block_id"])],
            "source_table_ids": ["table-2"] if block["type"] == "table" else [],
            "page_span": list(block["page_span"]),
            "text": str(block["text"]),
            "unit_type": str(block["type"]),
            "semantic_role": str(block["semantic_role"]),
            "should_index_for_rag": False,
            "chunk_ids": [],
        }
        for index, block in enumerate(ir_blocks, start=1)
    )

    payload = build_coverage_payload(
        doc_id="doc-structure-gaps",
        parse_run_id="run-structure-gaps",
        profile="auto",
        state="done",
        blocks=(),
        chunks=(),
        pages=({"page_number": 1}, {"page_number": 2}),
        ir_blocks=ir_blocks,
        tables=(),
        figures=(),
        knowledge_units=units,
        quality_signals=(),
        index_manifest=None,
    )

    codes = {signal["code"] for signal in payload["quality_signals"]}
    assert "structure_list_parent_missing" in codes
    assert "structure_cross_page_table_break" in codes
