"""待办 3: a table that continues across pages says so.

A catalogue table is usually wider than the page: the same rows come back with
another block of columns, and every page carries its own grid.  Read as unrelated
tables, a consumer cannot tell a repeated row label (same item, more columns) from
a new item, and nothing links the parts.  These tests pin the classification and
the metadata that records it.
"""

from __future__ import annotations

import unittest

from parsecore.models import Block, BlockType
from parsecore.parsers import (
    _link_table_continuations,
    _table_continuation_kind,
)


def _table_block(block_id: str, page: int, cells: list[list[str]]) -> Block:
    return Block(
        block_id=block_id,
        doc_id="doc",
        type=BlockType.TABLE,
        content="",
        metadata={
            "page": page,
            "table_index": 1,
            "rows": len(cells),
            "cols": max((len(row) for row in cells), default=0),
            "cells": cells,
        },
    )


def _paragraph_block(block_id: str, page: int) -> Block:
    return Block(
        block_id=block_id,
        doc_id="doc",
        type=BlockType.PARAGRAPH,
        content="text",
        metadata={"page": page},
    )


COLUMN_PART_ONE = [
    ["REMOVAL", 'SCREW OF THE COVER\n6.A.(1)(a)', 'LOCKING RING "C135"\n6.A.(2)'],
    ["IDENT,PLATE\n(1-10) and (3-340)", "X", ""],
    ["LOCKING RING\n(2-10)", "", "X"],
]
COLUMN_PART_TWO = [
    ["REMOVAL", 'PLUG CONNECTOR "C136"\n6.A.(25)', 'ELEC. CONTACT\n6.A.(26)'],
    ["IDENT,PLATE\n(1-10) and (3-340)", "X", ""],
    ["LOCKING RING\n(2-10)", "", "X"],
]
COLUMN_PART_THREE = [
    ["REMOVAL", 'ADAPTER "C137"\n6.A.(49)', 'SPACERS + INSULATORS\n6.A.(50)'],
    ["IDENT,PLATE\n(1-10) and (3-340)", "", "X"],
    ["LOCKING RING\n(2-10)", "X", ""],
]

ROW_PART_ONE = [
    ["Index", "Name", "P/N or Type"],
    ["1", "Bolt", "320-366-701-0"],
    ["2", "Nut", "320-401-201-0"],
]
ROW_PART_TWO = [
    ["Index", "Name", "P/N or Type"],
    ["3", "Washer", "320-401-202-0"],
    ["4", "Sleeve", "320-401-203-0"],
]


class ContinuationKindTests(unittest.TestCase):
    def test_the_same_rows_with_other_columns_are_a_column_continuation(self) -> None:
        self.assertEqual(
            _table_continuation_kind(COLUMN_PART_ONE, COLUMN_PART_TWO),
            "column_continuation",
        )

    def test_a_repeated_header_with_new_rows_is_a_row_continuation(self) -> None:
        self.assertEqual(
            _table_continuation_kind(ROW_PART_ONE, ROW_PART_TWO), "row_continuation"
        )

    def test_an_identical_table_is_not_a_continuation(self) -> None:
        self.assertEqual(_table_continuation_kind(ROW_PART_ONE, ROW_PART_ONE), "")

    def test_unrelated_tables_are_not_linked(self) -> None:
        self.assertEqual(
            _table_continuation_kind(ROW_PART_ONE, [["a", "b"], ["c", "d"], ["e", "f"]]),
            "",
        )

    def test_a_single_row_table_is_never_a_continuation(self) -> None:
        self.assertEqual(_table_continuation_kind([["Index"], ["1"]], [["Index"], ["1"]]), "")


class ContinuationLinkingTests(unittest.TestCase):
    def test_adjacent_pages_are_linked_in_both_directions(self) -> None:
        blocks = [
            _table_block("t-85", 85, COLUMN_PART_ONE),
            _table_block("t-87", 86, COLUMN_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        first, second = (block.metadata for block in blocks)
        group = "doc:tblgrp:p85"
        self.assertEqual(first["continuation_group_id"], group)
        self.assertEqual(second["continuation_group_id"], group)
        self.assertEqual(second["table_group_id"], group)
        self.assertTrue(second["is_continuation"])
        self.assertEqual(second["continues_from"], "t-85")
        self.assertEqual(second["continuation_kind"], "column_continuation")
        self.assertEqual(first["continues_to"], "t-87")
        self.assertEqual(first["page_span"], [85, 86])
        self.assertEqual(second["page_span"], [85, 86])

    def test_a_figure_page_between_the_parts_does_not_break_a_column_continuation(self) -> None:
        blocks = [
            _table_block("t-85", 85, COLUMN_PART_ONE),
            _paragraph_block("p-86", 86),
            _table_block("t-87", 87, COLUMN_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertTrue(blocks[2].metadata["is_continuation"])
        self.assertEqual(blocks[2].metadata["page_span"], [85, 87])

    def test_a_repeated_header_does_not_bridge_a_gap(self) -> None:
        blocks = [
            _table_block("t-10", 10, ROW_PART_ONE),
            _table_block("t-12", 12, ROW_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertNotIn("is_continuation", blocks[1].metadata)
        self.assertNotIn("continuation_group_id", blocks[1].metadata)

    def test_a_long_gap_is_not_a_continuation(self) -> None:
        blocks = [
            _table_block("t-10", 10, COLUMN_PART_ONE),
            _table_block("t-20", 20, COLUMN_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertNotIn("continuation_group_id", blocks[1].metadata)

    def test_the_group_keeps_the_page_it_started_on(self) -> None:
        blocks = [
            _table_block("t-85", 85, COLUMN_PART_ONE),
            _table_block("t-87", 87, COLUMN_PART_TWO),
            _table_block("t-89", 89, COLUMN_PART_THREE),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        for block in blocks[1:]:
            self.assertTrue(block.metadata["is_continuation"])
            self.assertEqual(block.metadata["page_span"], [85, 89])
        self.assertEqual(blocks[0].metadata["page_span"], [85, 89])
        # A middle part both continues and is continued.
        self.assertEqual(blocks[1].metadata["continues_from"], "t-85")
        self.assertEqual(blocks[1].metadata["continues_to"], "t-89")

    def test_a_page_with_different_rows_starts_a_new_table(self) -> None:
        # The catalogue splits one logical table both ways: pages that repeat the
        # same item rows continue the columns, a page with other items is new.
        other_rows = [
            ["REMOVAL", 'CLAMP\n6.A.(51)', 'SHEATH\n6.A.(52)'],
            ["CLAMP\n(2-140)", "X", ""],
            ["SHEATH\n(2-150)", "", "X"],
        ]
        blocks = [
            _table_block("t-87", 87, COLUMN_PART_TWO),
            _table_block("t-89", 89, other_rows),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertNotIn("continuation_group_id", blocks[0].metadata)
        self.assertNotIn("continuation_group_id", blocks[1].metadata)

    def test_two_tables_on_one_page_are_not_linked_to_each_other(self) -> None:
        blocks = [
            _table_block("t-a", 5, COLUMN_PART_ONE),
            _table_block("t-b", 5, COLUMN_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertNotIn("continuation_group_id", blocks[0].metadata)
        self.assertNotIn("continuation_group_id", blocks[1].metadata)

    def test_a_paragraph_run_sharing_a_page_does_not_break_the_chain(self) -> None:
        blocks = [
            _table_block("t-85", 85, COLUMN_PART_ONE),
            _paragraph_block("p-85", 85),
            _table_block("t-86", 86, COLUMN_PART_TWO),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertTrue(blocks[2].metadata["is_continuation"])
        self.assertEqual(blocks[2].metadata["continues_from"], "t-85")

    def test_tables_without_cells_are_ignored(self) -> None:
        blocks = [
            _table_block("t-85", 85, COLUMN_PART_ONE),
            Block(
                block_id="t-86",
                doc_id="doc",
                type=BlockType.TABLE,
                content="markdown",
                metadata={"page": 86},
            ),
        ]
        _link_table_continuations(blocks, doc_id="doc")

        self.assertNotIn("continuation_group_id", blocks[1].metadata)


if __name__ == "__main__":
    unittest.main()

