"""Recover reliable visual rows from an unruled illustrated parts list table."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from parsecore.parsers import _extract_pdfplumber_layout, _pdf_table_cells


_HEADER = [
    "FIG-ITEM",
    "PART NUMBER",
    "NOMENCLATURE\n1234567",
    "USAGE\nCODE",
    "UNITS\nPER\nASSY",
]


class _Row:
    def __init__(self, top: float, bottom: float) -> None:
        self.bbox = (0.0, top, 50.0, bottom)
        self.cells = [
            (float(index * 10), top, float((index + 1) * 10), bottom)
            for index in range(5)
        ]


class _Table:
    rows = [_Row(0.0, 10.0), _Row(10.0, 100.0)]

    def __init__(self, columns: list[list[tuple[float, str]]], *, header: list[str] | None = None) -> None:
        self.header = list(header or _HEADER)
        self.columns = columns

    def extract(self) -> list[list[str]]:
        return [
            list(self.header),
            ["\n".join(text for _center, text in column) for column in self.columns],
        ]


class _Crop:
    def __init__(self, lines: list[tuple[float, str]]) -> None:
        self.lines = lines

    def extract_text_lines(self, *, layout: bool, strip: bool) -> list[dict[str, object]]:
        assert layout is False
        assert strip is True
        return [
            {"top": center - 0.5, "bottom": center + 0.5, "text": text}
            for center, text in self.lines
        ]


class _Page:
    chars: list[dict[str, object]] = []

    def __init__(self, columns: list[list[tuple[float, str]]]) -> None:
        self.columns = columns

    def crop(self, bbox: tuple[float, float, float, float]) -> _Crop:
        column_index = int(bbox[0] // 10)
        return _Crop(self.columns[column_index])


class _InvalidCropPage(_Page):
    def crop(self, bbox: tuple[float, float, float, float]) -> _Crop:
        raise ValueError("invalid crop bounds")


class _UnexpectedCropFailurePage(_Page):
    def crop(self, bbox: tuple[float, float, float, float]) -> _Crop:
        raise RuntimeError("unexpected extraction failure")


class _LayoutPage(_UnexpectedCropFailurePage):
    width = 100.0
    height = 120.0
    rotation = 0
    images: list[dict[str, object]] = []

    def __init__(self, columns: list[list[tuple[float, str]]], table: _Table) -> None:
        super().__init__(columns)
        self.table = table
        self.chars = [
            {"text": "TABLE-CONTENT", "x0": 10.0, "x1": 40.0, "top": 20.0, "bottom": 30.0},
            {"text": "BODY-CONTENT", "x0": 70.0, "x1": 90.0, "top": 20.0, "bottom": 30.0},
        ]
        self.filter_decisions: list[bool] = []

    def find_tables(self) -> list[_Table]:
        return [self.table]

    def extract_words(self) -> list[dict[str, object]]:
        return []

    def extract_text(self) -> str:
        return "\n".join(str(char["text"]) for char in self.chars)

    def filter(self, predicate):
        kept = []
        self.filter_decisions = []
        for char in self.chars:
            keep = predicate(char)
            self.filter_decisions.append(keep)
            if keep:
                kept.append(char)
        return SimpleNamespace(
            width=self.width,
            height=self.height,
            extract_text=lambda: "\n".join(str(char["text"]) for char in kept),
        )


class _Document:
    def __init__(self, page: _LayoutPage) -> None:
        self.pages = [page]

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class PdfIplTableRowTests(unittest.TestCase):
    def test_aligned_item_and_part_lines_define_rows_and_keep_blank_columns(self) -> None:
        columns = [
            [(10, "2 170"), (30, "-170A"), (50, "180")],
            [(10, "PN-1"), (30, "PN-2"), (50, "PN-3")],
            [
                (10, ".COVER"),
                (15, "(ALT PN-1)"),
                (25, "ATTACHING PARTS"),
                (30, ".SCREW"),
                (40, "(ALT PN-2)"),
                (45, "***"),
                (50, ".SLEEVE"),
            ],
            [(30, "A")],
            [(10, "1"), (50, "AR")],
        ]
        table = _Table(columns)

        cells = _pdf_table_cells(_Page(columns), table, rotated_text=False)

        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[1], ["2 170", "PN-1", ".COVER\n(ALT PN-1)\nATTACHING PARTS", "", "1"])
        self.assertEqual(cells[2], ["-170A", "PN-2", ".SCREW\n(ALT PN-2)\n***", "A", ""])
        self.assertEqual(cells[3], ["180", "PN-3", ".SLEEVE", "", "AR"])

    def test_unmatched_primary_anchor_leaves_original_table_unchanged(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (40, "PN-2")],
            [(10, "description"), (30, "other description")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)

        self.assertEqual(
            _pdf_table_cells(_Page(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_misaligned_usage_or_quantity_value_leaves_original_unchanged(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [(22, "A")],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)

        self.assertEqual(
            _pdf_table_cells(_Page(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_reordered_crop_text_leaves_original_table_unchanged(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-2"), (30, "PN-1")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)
        table.extract = lambda: [
            list(_HEADER),
            ["1\n2", "PN-1\nPN-2", "description one\ndescription two", "", "1\n1"],
        ]

        self.assertEqual(
            _pdf_table_cells(_Page(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_only_the_explicit_ipl_header_signature_is_reconstructed(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        header = ["FIG-ITEMS", *_HEADER[1:]]
        table = _Table(columns, header=header)

        self.assertEqual(
            _pdf_table_cells(_Page(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_rotated_page_keeps_the_existing_table_result(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)

        self.assertEqual(
            _pdf_table_cells(_Page(columns), table, rotated_text=True),
            table.extract(),
        )

    def test_invalid_crop_bounds_fall_back_to_original_table(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)
        table.bbox = (0.0, 0.0, 50.0, 100.0)

        with self.assertLogs("parsecore.parsers", level="WARNING") as captured:
            cells = _pdf_table_cells(
                _InvalidCropPage(columns), table, rotated_text=False, page_number=3
            )

        self.assertEqual(cells, table.extract())
        self.assertTrue(
            any(
                "page=3" in record
                and "bbox=(0.0, 0.0, 50.0, 100.0)" in record
                and "ValueError: invalid crop bounds" in record
                for record in captured.output
            ),
            captured.output,
        )

    def test_unexpected_crop_failures_keep_the_original_table(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)

        self.assertEqual(
            _pdf_table_cells(_UnexpectedCropFailurePage(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_layout_keeps_successful_ipl_table_when_optional_row_recovery_fails(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)
        table.bbox = (0.0, 0.0, 50.0, 100.0)
        page = _LayoutPage(columns, table)
        fake_pdfplumber = SimpleNamespace(open=lambda _path: _Document(page))

        with patch.dict("sys.modules", {"pdfplumber": fake_pdfplumber}):
            with self.assertLogs("parsecore.parsers", level="WARNING") as captured:
                layouts = _extract_pdfplumber_layout(
                    "unused.pdf",
                    min_rows=2,
                    min_cols=5,
                    layout_reading_order_enabled=False,
                )

        self.assertEqual(len(layouts), 1)
        self.assertEqual(len(layouts[0].tables), 1)
        self.assertEqual(layouts[0].tables[0].bbox, table.bbox)
        self.assertEqual(layouts[0].tables[0].cells, table.extract())
        self.assertEqual(layouts[0].text_without_tables, "BODY-CONTENT")
        self.assertEqual(page.filter_decisions, [False, True])
        self.assertTrue(
            any(
                "page=1" in record and "bbox=(0.0, 0.0, 50.0, 100.0)" in record
                and "unexpected extraction failure" in record
                for record in captured.output
            ),
            captured.output,
        )
        self.assertNotIn("TABLE-CONTENT", "\n".join(captured.output))
        self.assertNotIn("BODY-CONTENT", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
