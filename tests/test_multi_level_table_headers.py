"""待办 3: a multi-level table header reaches the record field names.

The validation rules for tables ask that 多级表头 (a header spread over several
rows, with units or sub-parts underneath) survives.  `header_rows` was already
used to skip the header when building records, but only the first row's text
became the field name, so the second level was silently dropped and two columns
with the same top-level header became indistinguishable.
"""

from __future__ import annotations

import unittest

from parsecore.api_payloads import _structured_records, _structured_tables
from parsecore.models import Block, BlockType


def _table_block(cells: list[list[str]], *, header_rows: int, metadata: dict | None = None) -> Block:
    payload: dict[str, object] = {
        "page": 1,
        "rows": len(cells),
        "cols": max((len(row) for row in cells), default=0),
        "cells": cells,
        "header_rows": header_rows,
        "parser": "pdf-text",
    }
    payload.update(metadata or {})
    return Block(
        block_id="t-1",
        doc_id="doc-1",
        type=BlockType.TABLE,
        content="",
        metadata=payload,
    )


def _records(block: Block) -> list[dict]:
    tables = _structured_tables((block,), doc_id="doc-1")
    return _structured_records(blocks=(block,), tables=tables, quality_signals=[], doc_id="doc-1")


class MultiLevelHeaderRecordTests(unittest.TestCase):
    def test_a_second_header_level_becomes_part_of_the_field_name(self) -> None:
        block = _table_block(
            [
                ["Qty", "Mass"],
                ["数量", "重量"],
                ["2", "1.5 kg"],
            ],
            header_rows=2,
        )

        records = _records(block)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["fields"], {"Qty 数量": "2", "Mass 重量": "1.5 kg"})
        self.assertNotIn("数量", records[0]["fields"])

    def test_a_merged_top_level_header_keeps_the_two_sub_columns_apart(self) -> None:
        cells = [
            ["Part", "Part", "Qty"],
            ["T3-A", "T3-B", ""],
            ["Bolt", "Nut", "2"],
        ]
        block = _table_block(
            cells,
            header_rows=2,
            metadata={
                "table_cells": [
                    {"row_index": 0, "col_index": 0, "text": "Part", "row_span": 1, "col_span": 2},
                    {"row_index": 0, "col_index": 1, "text": "", "continuation": True, "origin": "0.0"},
                    {"row_index": 0, "col_index": 2, "text": "Qty"},
                    {"row_index": 1, "col_index": 0, "text": "T3-A"},
                    {"row_index": 1, "col_index": 1, "text": "T3-B"},
                    {"row_index": 2, "col_index": 0, "text": "Bolt"},
                    {"row_index": 2, "col_index": 1, "text": "Nut"},
                    {"row_index": 2, "col_index": 2, "text": "2"},
                ]
            },
        )

        records = _records(block)

        self.assertEqual(
            records[0]["fields"],
            {"Part T3-A": "Bolt", "Part T3-B": "Nut", "Qty": "2"},
        )

    def test_an_explicit_header_values_list_still_wins(self) -> None:
        block = _table_block(
            [["Qty", "Mass"], ["数量", "重量"], ["2", "1.5"]],
            header_rows=2,
            metadata={"header_values": ["数量(件)", "重量(kg)"]},
        )

        records = _records(block)

        self.assertEqual(records[0]["fields"], {"数量(件)": "2", "重量(kg)": "1.5"})

    def test_a_single_header_row_is_unchanged(self) -> None:
        block = _table_block([["Part", "Qty"], ["Bolt", "2"]], header_rows=1)
        records = _records(block)

        self.assertEqual(records[0]["fields"], {"Part": "Bolt", "Qty": "2"})

    def test_duplicate_joined_headers_stay_distinguishable(self) -> None:
        # Two columns whose top level repeats and whose second level is empty:
        # the stable-header rule still has to name them differently.
        block = _table_block(
            [["Part", "Part"], ["", ""], ["Bolt", "Nut"]],
            header_rows=2,
        )

        records = _records(block)

        self.assertEqual(sorted(records[0]["fields"]), ["Part", "Part_2"])
        self.assertEqual(records[0]["fields"]["Part"], "Bolt")
        self.assertEqual(records[0]["fields"]["Part_2"], "Nut")


if __name__ == "__main__":
    unittest.main()
