"""Recover reliable visual rows from an unruled illustrated parts list table."""

from __future__ import annotations

import unittest

from parsecore.parsers import _pdf_table_cells


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

        self.assertEqual(
            _pdf_table_cells(_InvalidCropPage(columns), table, rotated_text=False),
            table.extract(),
        )

    def test_unexpected_crop_failures_are_not_swallowed(self) -> None:
        columns = [
            [(10, "1"), (30, "2")],
            [(10, "PN-1"), (30, "PN-2")],
            [(10, "description one"), (30, "description two")],
            [],
            [(10, "1"), (30, "1")],
        ]
        table = _Table(columns)

        with self.assertRaisesRegex(RuntimeError, "unexpected extraction failure"):
            _pdf_table_cells(_UnexpectedCropFailurePage(columns), table, rotated_text=False)


if __name__ == "__main__":
    unittest.main()
