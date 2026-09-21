"""Tests for PDF table cell reading in the page's own text direction.

A catalogue table rotates its column headers 90° so they fit in a narrow column.
The page itself is upright, so nothing marks the cell as unusual, and a plain
left-to-right read returns those headers reversed: on a real sample,
``SCREW OF THE COVER 6.A.(1)(a)`` came back as ``REVOC EHT FO WERCS``.
"""

from __future__ import annotations

import unittest

from parsecore.parsers import (
    _pdf_char_reading_frame,
    _text_along_char_direction,
)


def _upright_char(text: str, x0: float, top: float, *, size: float = 8.0) -> dict:
    """A character drawn normally (advance +x, next line +top)."""

    return {
        "text": text,
        "x0": x0,
        "x1": x0 + size * 0.6,
        "top": top,
        "bottom": top + size,
        "size": size,
        "matrix": (size, 0.0, 0.0, size, x0, top),
    }


def _rotated_char(text: str, x0: float, top: float, *, size: float = 7.0) -> dict:
    """A character rotated 90°: it advances upward and the next line is +x.

    The matrix is the one the real sample carries:
    ``(0, size, -size, 0, e, f)``.
    """

    return {
        "text": text,
        "x0": x0,
        "x1": x0 + size,
        "top": top,
        "bottom": top + size * 0.6,
        "size": size,
        "matrix": (0.0, size, -size, 0.0, x0, top),
    }


def _rotated_line(text: str, *, x: float, start_top: float, step: float = 6.0) -> list[dict]:
    return [
        _rotated_char(char, x, start_top - index * step)
        for index, char in enumerate(text)
    ]


class RotatedCellTextTests(unittest.TestCase):
    def test_upright_text_is_left_to_the_caller(self) -> None:
        chars = [_upright_char("A", 10, 10), _upright_char("B", 16, 10)]
        self.assertIsNone(_pdf_char_reading_frame(chars))
        self.assertIsNone(_text_along_char_direction(chars))

    def test_rotated_text_is_read_along_its_own_direction(self) -> None:
        # "SCREW" written upward in a narrow column, then "6.A" one line over.
        chars = _rotated_line("SCREW", x=208.5, start_top=218.6)
        chars += _rotated_line("6.A", x=216.8, start_top=218.6)

        text = _text_along_char_direction(chars)

        self.assertEqual(text, "SCREW\n6.A")

    def test_an_explicit_space_is_kept(self) -> None:
        chars = _rotated_line("OF", x=210.0, start_top=200.0)
        chars += [_rotated_char(" ", 210.0, 188.0)]
        chars += _rotated_line("THE", x=210.0, start_top=182.0)

        self.assertEqual(_text_along_char_direction(chars), "OF THE")

    def test_a_wide_gap_becomes_a_separator(self) -> None:
        # Same line, a visible gap: the words must not be glued together.
        chars = _rotated_line("AB", x=100.0, start_top=200.0)
        chars += _rotated_line("CD", x=100.0, start_top=200.0 - 10 * 6.0)

        text = _text_along_char_direction(chars)

        self.assertEqual(text, "AB CD")

    def test_a_cell_dominated_by_upright_text_keeps_its_own_order(self) -> None:
        chars = [_upright_char(char, 10 + index * 6, 10) for index, char in enumerate("IDENT")]
        chars.append(_rotated_char("X", 10, 40))

        self.assertIsNone(_text_along_char_direction(chars))

    def test_chars_without_a_matrix_are_ignored(self) -> None:
        self.assertIsNone(_pdf_char_reading_frame([{"text": "A", "x0": 1, "x1": 2, "top": 3, "bottom": 4}]))
        self.assertIsNone(_text_along_char_direction([]))


if __name__ == "__main__":
    unittest.main()
