from __future__ import annotations

import unittest

from parsecore.parsers import (
    PdfTextParser,
    _split_inline_structural_items,
    _split_structural_items,
)


_MAINTENANCE_PARAGRAPH = "\n".join(
    [
        "(a) Strip the end of the shielded cables.",
        "Use automatic stripping pliers (index U).",
        "(b) Prepare the grounding strand of the shielded cables.",
        "Obey these instructions:",
        "1 Untwist the shielding braid of the two shielded cables.",
        "Twist together the strands over a length of 0.39 in.",
        "NOTE: The shielding braid of one of the two cables is shorter.",
        "(c) Apply flux and do the weld.",
        "Use flux (index D - refer to Table 601).",
        "WARNING: PUT ON PROTECTIVE GLOVES.",
        "HEAT IS DANGEROUS FOR YOUR HANDS.",
        "(d) Remove the polyamide sheath.",
    ]
)

_INLINE_STRUCTURAL_PARAGRAPH = (
    '(5) Identification for P/N 320-366-701-0. '
    '(a) Two identification sleeves are identified "T3-A" and "T3-B". '
    '(b) Four identification marks are identified "TCC-B", "T5" and "T49,5".'
)

_INLINE_NUMBERED_PARAGRAPH = (
    'A. Removal of Harness from Storage. After storage, the harness can be installed '
    'without initial tests. '
    '(1) Do the procedure on a smooth and clean surface. '
    '(2) Remove the container from the cardboard box. '
    '(3) Cut the bag as close to the seam as possible.'
)


class SplitStructuralItemsTests(unittest.TestCase):
    def test_passes_short_paragraphs_through(self) -> None:
        short = "single short paragraph with\ntwo lines"
        self.assertEqual(_split_structural_items([short]), [short])

    def test_does_not_split_when_markers_absent(self) -> None:
        lines = "\n".join(f"line number {i} with some content here." for i in range(15))
        self.assertEqual(_split_structural_items([lines]), [lines])

    def test_splits_maintenance_items(self) -> None:
        result = _split_structural_items([_MAINTENANCE_PARAGRAPH])
        self.assertGreaterEqual(len(result), 4)
        self.assertTrue(result[0].startswith("(a)"))
        self.assertTrue(any(seg.startswith("(b)") for seg in result))
        self.assertTrue(any(seg.startswith("NOTE:") for seg in result))
        self.assertTrue(any(seg.startswith("WARNING:") for seg in result))

    def test_requires_minimum_marker_count(self) -> None:
        long_line_single_marker = "\n".join(
            ["intro line " + str(i) for i in range(12)] + ["(a) first and only item"]
        )
        self.assertEqual(
            _split_structural_items([long_line_single_marker]),
            [long_line_single_marker],
        )

    def test_keeps_preamble_before_first_marker(self) -> None:
        preamble_plus_items = "\n".join(
            ["Preamble line 1", "Preamble line 2"]
            + ["Filler " + str(i) for i in range(9)]
            + ["(a) item alpha", "detail", "(b) item beta", "detail"]
        )
        result = _split_structural_items([preamble_plus_items])
        self.assertGreaterEqual(len(result), 3)
        self.assertIn("Preamble line 1", result[0])


class SplitInlineStructuralItemsTests(unittest.TestCase):
    def test_splits_inline_letter_markers(self) -> None:
        result = _split_inline_structural_items([_INLINE_STRUCTURAL_PARAGRAPH])
        self.assertEqual(len(result), 3)
        self.assertTrue(result[0].startswith("(5)"))
        self.assertTrue(result[1].startswith("(a)"))
        self.assertTrue(result[2].startswith("(b)"))

    def test_splits_inline_numbered_steps_after_preamble(self) -> None:
        result = _split_inline_structural_items([_INLINE_NUMBERED_PARAGRAPH])
        self.assertEqual(len(result), 4)
        self.assertTrue(result[0].startswith("A. Removal"))
        self.assertTrue(result[1].startswith("(1)"))
        self.assertTrue(result[3].startswith("(3)"))

    def test_ignores_short_paragraphs(self) -> None:
        short = "(1) alpha (2) beta"
        self.assertEqual(_split_inline_structural_items([short]), [short])


class PdfTextParserStructuralOptionTests(unittest.TestCase):
    def test_defaults_enable_structural_split(self) -> None:
        parser = PdfTextParser(media_types=["application/pdf"], extensions=[".pdf"])
        self.assertTrue(parser._split_structural_enabled)
        self.assertEqual(parser._structural_min_lines, 10)
        self.assertTrue(parser._split_inline_structural_enabled)
        self.assertEqual(parser._inline_structural_min_length, 120)

    def test_option_can_disable_structural_split(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={
                "post_process": {
                    "split_structural_items": False,
                    "structural_min_lines_trigger": 25,
                    "split_inline_structural_items": False,
                    "inline_structural_min_length_trigger": 250,
                }
            },
        )
        self.assertFalse(parser._split_structural_enabled)
        self.assertEqual(parser._structural_min_lines, 25)
        self.assertFalse(parser._split_inline_structural_enabled)
        self.assertEqual(parser._inline_structural_min_length, 250)


if __name__ == "__main__":
    unittest.main()
