"""The shared table grid contract (``parsecore.tables``).

Every projection reconstructs rows through this module, so the rules it encodes
are tested directly: which key wins, how a merged value reaches a flattened row,
and how a cell that contains the delimiter characters is rendered.
"""

from __future__ import annotations

import unittest

from parsecore.tables import (
    grid_from_cells,
    grid_from_metadata,
    grid_from_rows,
    render_markdown,
    render_tsv,
    resolve_table_grid,
    rows_from_table_payload,
)


MERGED_CELLS = [
    {
        "row_index": 0,
        "col_index": 0,
        "text": "Part",
        "row_span": 1,
        "col_span": 2,
        "continuation": False,
        "origin": "0.0",
    },
    {
        "row_index": 0,
        "col_index": 1,
        "text": "",
        "row_span": 1,
        "col_span": 1,
        "continuation": True,
        "origin": "0.0",
    },
    {
        "row_index": 0,
        "col_index": 2,
        "text": "Qty",
        "row_span": 1,
        "col_span": 1,
        "continuation": False,
        "origin": "0.2",
    },
    {
        "row_index": 1,
        "col_index": 0,
        "text": "Bolt",
        "row_span": 2,
        "col_span": 1,
        "continuation": False,
        "origin": "1.0",
    },
    {
        "row_index": 1,
        "col_index": 1,
        "text": "M2",
        "row_span": 1,
        "col_span": 1,
        "continuation": False,
        "origin": "1.1",
    },
    {
        "row_index": 1,
        "col_index": 2,
        "text": "spec",
        "row_span": 1,
        "col_span": 1,
        "continuation": False,
        "origin": "1.2",
    },
    {
        "row_index": 2,
        "col_index": 0,
        "text": "",
        "row_span": 1,
        "col_span": 1,
        "continuation": True,
        "origin": "1.0",
    },
    {
        "row_index": 2,
        "col_index": 1,
        "text": "M10",
        "row_span": 1,
        "col_span": 1,
        "continuation": False,
        "origin": "2.1",
    },
    {
        "row_index": 2,
        "col_index": 2,
        "text": "3",
        "row_span": 1,
        "col_span": 1,
        "continuation": False,
        "origin": "2.2",
    },
]


class TableGridTests(unittest.TestCase):
    def test_structured_cells_win_over_flat_rows(self) -> None:
        grid = grid_from_metadata(
            {"table_cells": MERGED_CELLS, "cells": [["stale", "values"]]}
        )
        self.assertEqual(grid.row_count, 3)
        self.assertEqual(grid.column_count, 3)
        self.assertEqual(grid.cell_at(0, 0).text, "Part")

    def test_flat_rows_are_the_fallback_and_never_invent_spans(self) -> None:
        grid = grid_from_metadata({"cells": [["Part", "Qty"], ["Bolt", "2"]]})
        self.assertEqual(grid.rows(), [["Part", "Qty"], ["Bolt", "2"]])
        self.assertEqual(grid.merged_cells(), [])
        self.assertEqual(grid.cell_at(0, 0).col_span, 1)

    def test_a_flattened_row_carries_the_value_a_merge_governs(self) -> None:
        grid = grid_from_cells(MERGED_CELLS)
        self.assertEqual(
            grid.rows(),
            [["Part", "Part", "Qty"], ["Bolt", "M2", "spec"], ["Bolt", "M10", "3"]],
        )
        # Span-aware output keeps the continuation empty instead.
        self.assertEqual(
            grid.rows(fill_merged=False),
            [["Part", "", "Qty"], ["Bolt", "M2", "spec"], ["", "M10", "3"]],
        )

    def test_merges_are_derived_from_spans_only(self) -> None:
        grid = grid_from_cells(MERGED_CELLS)
        self.assertEqual(
            sorted(merge["kind"] for merge in grid.merged_cells()),
            ["col_span", "row_span"],
        )

    def test_header_values_cover_every_column_a_merge_spans(self) -> None:
        grid = grid_from_cells(MERGED_CELLS)
        self.assertEqual(grid.header_values(), ["Part", "Part", "Qty"])

    def test_header_values_do_not_repeat_a_vertical_merge(self) -> None:
        vertical = [
            {"row_index": 0, "col_index": 0, "text": "Part", "row_span": 2},
            {"row_index": 0, "col_index": 1, "text": "Qty"},
            {"row_index": 1, "col_index": 0, "text": "", "continuation": True, "origin": "0.0"},
            {"row_index": 1, "col_index": 1, "text": "Bolt"},
        ]
        grid = grid_from_cells(vertical)
        self.assertEqual(grid.rows(), [["Part", "Qty"], ["Part", "Bolt"]])
        # Over two header levels the merged value is one header, not "Part Part";
        # a genuinely different second level is joined.
        self.assertEqual(grid.header_values(header_rows=2), ["Part", "Qty Bolt"])
        self.assertEqual(grid.header_values(header_rows=1), ["Part", "Qty"])

    def test_rows_pad_to_the_grid_not_to_the_longest_row(self) -> None:
        grid = grid_from_metadata({"cells": [["Part"], ["Bolt", "2"]]})
        self.assertEqual(grid.rows(), [["Part", ""], ["Bolt", "2"]])

    def test_projected_table_rows_read_the_projected_cells(self) -> None:
        table = {"cells": MERGED_CELLS, "rows": 3, "cols": 3}
        self.assertEqual(resolve_table_grid(table).column_count, 3)
        self.assertEqual(
            rows_from_table_payload(table),
            [["Part", "Part", "Qty"], ["Bolt", "M2", "spec"], ["Bolt", "M10", "3"]],
        )
        self.assertEqual(rows_from_table_payload(None), [])


class TableRenderingTests(unittest.TestCase):
    def test_markdown_separates_the_header_from_the_body(self) -> None:
        self.assertEqual(
            render_markdown([["Part", "Qty"], ["Bolt", "2"]]),
            "| Part | Qty |\n| --- | --- |\n| Bolt | 2 |",
        )

    def test_markdown_escapes_cell_delimiters(self) -> None:
        rendered = render_markdown([["a|b", "c\nd"]])
        self.assertIn(r"a\|b", rendered)
        self.assertIn("c<br>d", rendered)
        # The row stays one line: an embedded newline must not split the table.
        self.assertEqual(len(rendered.splitlines()), 2)

    def test_tsv_keeps_newlines_inside_the_row(self) -> None:
        self.assertEqual(render_tsv([["a|b", "c\nd"]]), "a|b\tc d")

    def test_empty_rows_do_not_shift_the_grid(self) -> None:
        self.assertEqual(
            render_markdown([["Part", "Qty"], ["", ""], ["Bolt", "2"]]),
            "| Part | Qty |\n| --- | --- |\n| Bolt | 2 |",
        )

    def test_rendering_nothing_is_empty(self) -> None:
        self.assertEqual(render_markdown([]), "")
        self.assertEqual(render_tsv([]), "")
        self.assertEqual(render_markdown([["", ""]]), "")


class TableGridInputTests(unittest.TestCase):
    def test_malformed_cells_are_skipped_not_guessed(self) -> None:
        grid = grid_from_cells(["not-a-record", None, {"row_index": 0, "col_index": 0, "text": "A"}])
        self.assertEqual(grid.rows(), [["A"]])

    def test_origin_is_recovered_from_the_payload_string(self) -> None:
        grid = grid_from_cells(
            [{"row_index": 1, "col_index": 2, "continuation": True, "origin": "0.1"}]
        )
        cell = grid.cells[0]
        self.assertEqual((cell.origin_row, cell.origin_col), (0, 1))

    def test_rows_of_strings_are_accepted(self) -> None:
        self.assertEqual(grid_from_rows(["A", "B"]).rows(), [["A"], ["B"]])


if __name__ == "__main__":
    unittest.main()
