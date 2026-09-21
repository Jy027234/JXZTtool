"""待办 3: a table continued across pages becomes one logical row set in records.

A catalogue table wider than the page repeats its item rows on each page part and
adds another block of part-number columns.  Read per page, the same item appears
once per part, so "续行不漏行、不重复" cannot hold and a consumer cannot tell which
columns belong together.  The parts are joined for the records projection; the
physical tables and their spans stay as they are.
"""

from __future__ import annotations

import unittest

from parsecore.api_payloads import (
    _document_projection,
    _logical_tables,
    _merge_continuation_records,
    _structured_records,
    _structured_tables,
)
from parsecore.models import Block, BlockType

#: A short catalogue table used by the logical-table cases.
CATALOG_ROWS = [["Index", "Name", "Material"], ["1", "Bolt (1-10)", "STEEL"]]


def _table_block(
    block_id: str,
    page: int,
    cells: list[list[str]],
    *,
    group: str | None = None,
    kind: str | None = None,
    continues_from: str | None = None,
    page_span: list[int] | None = None,
) -> Block:
    metadata: dict[str, object] = {
        "page": page,
        "rows": len(cells),
        "cols": max((len(row) for row in cells), default=0),
        "cells": cells,
        "header_rows": 1,
        "parser": "pdf-text",
    }
    if group:
        metadata["continuation_group_id"] = group
        metadata["table_group_id"] = group
        metadata["continuation_kind"] = kind or "column_continuation"
        metadata["page_span"] = page_span or [page, page]
    if continues_from:
        metadata["is_continuation"] = True
        metadata["continues_from"] = continues_from
    return Block(
        block_id=block_id,
        doc_id="doc-1",
        type=BlockType.TABLE,
        content="",
        metadata=metadata,
    )


PART_ONE = [
    ["REMOVAL", "SCREW OF THE COVER 6.A.(1)(a)", "LOCKING RING 6.A.(2)"],
    ["IDENT,PLATE (1-10)", "X", ""],
    ["LOCKING RING (2-10)", "", "X"],
]
PART_TWO = [
    ["REMOVAL", "PLUG CONNECTOR 6.A.(25)", "ADAPTER 6.A.(26)"],
    ["IDENT,PLATE (1-10)", "X", ""],
    ["LOCKING RING (2-10)", "", "X"],
]


def _records_for(blocks: tuple[Block, ...]) -> list[dict]:
    tables = _structured_tables(blocks, doc_id="doc-1")
    return _structured_records(blocks=blocks, tables=tables, quality_signals=[], doc_id="doc-1")


class LogicalTableTests(unittest.TestCase):
    """待办 3: the parts of one table are published as one table as well."""

    def test_a_row_continuation_becomes_one_grid_without_the_repeated_header(self) -> None:
        blocks = (
            _table_block("t-10", 10, CATALOG_ROWS, group="doc-1:tblgrp:p10", kind="row_continuation"),
            _table_block(
                "t-11",
                11,
                [CATALOG_ROWS[0], ["2", "Nut (2-20)", "STEEL"]],
                group="doc-1:tblgrp:p10",
                kind="row_continuation",
                continues_from="t-10",
            ),
        )
        tables = _structured_tables(blocks, doc_id="doc-1")

        logical = _logical_tables(tables)

        self.assertEqual(len(logical), 1)
        entry = logical[0]
        self.assertEqual(entry["table_ids"], ["doc-1:p10:t1", "doc-1:p11:t2"])
        self.assertEqual((entry["page_start"], entry["page_end"]), (10, 11))
        self.assertEqual(entry["header_values"], CATALOG_ROWS[0])
        self.assertEqual(entry["rows"], len(CATALOG_ROWS) + 1)
        texts = [cell["text"] for cell in entry["cells"]]
        self.assertIn("Nut (2-20)", texts)
        # The second part's repeated header is not counted as a data row.
        self.assertEqual([part["repeated_header_rows"] for part in entry["parts"]], [0, 1])
        self.assertEqual(entry["warnings"], [])
        # Every physical part still reports itself.
        self.assertEqual(
            [table["table_id"] for table in tables], ["doc-1:p10:t1", "doc-1:p11:t2"]
        )
        self.assertTrue(all(table["continuation_group_id"] == "doc-1:tblgrp:p10" for table in tables))

    def test_a_column_continuation_puts_the_parts_side_by_side(self) -> None:
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85", page_span=[85, 87]),
            _table_block(
                "t-87",
                87,
                PART_TWO,
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
                page_span=[85, 87],
            ),
        )

        logical = _logical_tables(_structured_tables(blocks, doc_id="doc-1"))

        self.assertEqual(len(logical), 1)
        entry = logical[0]
        self.assertEqual(entry["continuation_kind"], "column_continuation")
        self.assertEqual((entry["rows"], entry["cols"]), (3, 6))
        header = [cell["text"] for cell in entry["cells"] if cell["row_index"] == 0]
        # Both parts' headers are present, each over the columns it introduced.
        self.assertEqual(header[0], "REMOVAL")
        self.assertIn("PLUG CONNECTOR 6.A.(25)", header)
        row = [cell["text"] for cell in entry["cells"] if cell["row_index"] == 1]
        self.assertEqual(row.count("IDENT,PLATE (1-10)"), 2)
        self.assertEqual(entry["warnings"], [])

    def test_parts_whose_rows_do_not_line_up_are_reported_instead_of_joined(self) -> None:
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85"),
            _table_block(
                "t-87",
                87,
                [PART_TWO[0], ["SOMETHING ELSE (9-99)", "", ""], PART_TWO[2]],
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
            ),
        )

        logical = _logical_tables(_structured_tables(blocks, doc_id="doc-1"))

        self.assertEqual(len(logical), 1)
        self.assertEqual(logical[0]["cells"], [])
        self.assertEqual(logical[0]["rows"], 0)
        self.assertIn("row labels do not line up between parts", logical[0]["warnings"])

    def test_a_table_that_does_not_continue_has_no_logical_table(self) -> None:
        blocks = (_table_block("t-1", 1, PART_ONE),)

        self.assertEqual(_logical_tables(_structured_tables(blocks, doc_id="doc-1")), [])

    def test_the_structured_projection_publishes_both_views(self) -> None:
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85", page_span=[85, 87]),
            _table_block(
                "t-87",
                87,
                PART_TWO,
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
                page_span=[85, 87],
            ),
        )

        payload = _document_projection(
            {"job": None, "doc_id": "doc-1", "blocks": blocks, "chunks": ()},
            projection="structured",
        )

        self.assertEqual(len(payload["tables"]), 2)
        self.assertEqual(len(payload["logical_tables"]), 1)
        self.assertEqual(payload["logical_tables"][0]["table_ids"], [table["table_id"] for table in payload["tables"]])


class ContinuationRecordTests(unittest.TestCase):
    def test_a_column_continuation_yields_one_record_per_logical_row(self) -> None:
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85", page_span=[85, 87]),
            _table_block(
                "t-87",
                87,
                PART_TWO,
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
                page_span=[85, 87],
            ),
        )

        records = _records_for(blocks)

        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first["page_start"], 85)
        self.assertEqual(first["page_end"], 87)
        self.assertEqual(first["continuation"]["part_count"], 2)
        self.assertEqual(
            [part["page_number"] for part in first["continuation"]["parts"]], [85, 87]
        )
        # The first part keeps the plain field names; a name the later part repeats
        # is suffixed with its page so two parts cannot silently overwrite each
        # other, while a new column keeps its own name.
        self.assertEqual(first["fields"]["REMOVAL"], "IDENT,PLATE (1-10)")
        self.assertEqual(first["fields"]["REMOVAL__p87"], "IDENT,PLATE (1-10)")
        self.assertEqual(first["fields"]["SCREW OF THE COVER 6.A.(1)(a)"], "X")
        self.assertEqual(first["fields"]["PLUG CONNECTOR 6.A.(25)"], "X")
        self.assertEqual(first["record_id"], "doc-1:tblgrp:p85:r1")
        # The row label is not duplicated: the second record is the other item.
        self.assertEqual(records[1]["fields"]["REMOVAL"], "LOCKING RING (2-10)")

    def test_records_without_a_continuation_group_are_untouched(self) -> None:
        blocks = (_table_block("t-1", 1, PART_ONE),)
        records = _records_for(blocks)

        self.assertEqual(len(records), 2)
        self.assertNotIn("continuation", records[0])

    def test_parts_whose_rows_do_not_line_up_are_left_separate(self) -> None:
        other_rows = [
            ["REMOVAL", "CLAMP 6.A.(51)", "SHEATH 6.A.(52)"],
            ["CLAMP (2-140)", "X", ""],
            ["SHEATH (2-150)", "", "X"],
        ]
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85"),
            _table_block(
                "t-89",
                89,
                other_rows,
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
            ),
        )

        records = _records_for(blocks)

        # Four records: the labels disagree, so joining would attach the wrong
        # columns to a row.
        self.assertEqual(len(records), 4)
        self.assertTrue(all("continuation" not in record for record in records))

    def test_a_row_continuation_is_not_merged(self) -> None:
        rows_one = [["Index", "Name"], ["1", "Bolt"], ["2", "Nut"]]
        rows_two = [["Index", "Name"], ["3", "Washer"], ["4", "Sleeve"]]
        blocks = (
            _table_block("t-10", 10, rows_one, group="doc-1:tblgrp:p10", kind="row_continuation"),
            _table_block(
                "t-11",
                11,
                rows_two,
                group="doc-1:tblgrp:p10",
                kind="row_continuation",
                continues_from="t-10",
            ),
        )

        records = _records_for(blocks)

        # Rows already differ; nothing repeats, so nothing is joined.
        self.assertEqual(len(records), 4)
        self.assertTrue(all("continuation" not in record for record in records))

    def test_missing_parts_keep_the_records_they_have(self) -> None:
        blocks = (
            _table_block("t-85", 85, PART_ONE, group="doc-1:tblgrp:p85"),
            _table_block(
                "t-87",
                87,
                PART_TWO,
                group="doc-1:tblgrp:p85",
                continues_from="t-85",
            ),
        )
        tables = _structured_tables(blocks, doc_id="doc-1")
        only_first = [table for table in tables if table["block_id"] == "t-85"]

        records = _structured_records(
            blocks=blocks, tables=only_first, quality_signals=[], doc_id="doc-1"
        )

        self.assertEqual(len(records), 2)
        self.assertTrue(all("continuation" not in record for record in records))

    def test_the_helper_is_a_no_op_without_groups(self) -> None:
        records = [{"record_id": "r1", "table_id": "t1", "fields": {"a": "b"}}]
        self.assertEqual(_merge_continuation_records(records, []), records)
        self.assertEqual(
            _merge_continuation_records(records, [{"table_id": "t1", "continuation_kind": ""}]),
            records,
        )


if __name__ == "__main__":
    unittest.main()
