"""Tests for ``_split_toc_entries`` TOC entry splitter."""
from __future__ import annotations

import unittest

from parsecore.parsers import _split_toc_entries


_TOC_PARAGRAPH = (
    "TABLE OF CONTENTS\n"
    "Subject Page\n"
    "TITLE .................................................................. 1\n"
    " HISTORY OF REVISIONS ................................................... 1\n"
    " RECORD OF REVISIONS .................................................... 1\n"
    " SERVICE BULLETIN LIST .................................................. 1\n"
    " 1. Description and Operation . . . . . . . . . . . . 5\n"
    " 2. Testing and Fault Isolation . . . . . . . . . . . 101\n"
)


class SplitTocEntriesTests(unittest.TestCase):
    def test_passes_empty_input(self) -> None:
        self.assertEqual(_split_toc_entries([]), [])

    def test_does_not_split_when_few_entries(self) -> None:
        text = "Some intro line.\nTITLE .................... 1\nAnother line."
        self.assertEqual(_split_toc_entries([text]), [text])

    def test_splits_toc_paragraph_into_entries(self) -> None:
        segments = _split_toc_entries([_TOC_PARAGRAPH])
        self.assertGreaterEqual(len(segments), 6)
        # Each segment should end with a terminator (dots + page/status)
        for seg in segments:
            self.assertRegex(seg, r"(?:\.\s*){2,}\s*(?:\d+|Not applicable|N/A|TBD)\b")

    def test_handles_spaced_dot_leaders(self) -> None:
        text = (
            " A . . . . . . 1\n"
            " B . . . . . . 2\n"
            " C . . . . . . 3\n"
        )
        segments = _split_toc_entries([text])
        self.assertEqual(len(segments), 3)

    def test_respects_min_entries_trigger(self) -> None:
        text = "TITLE .................. 1\nHISTORY .................. 1"
        # Only 2 TOC lines, default trigger=3 — keep as single block
        self.assertEqual(_split_toc_entries([text]), [text])
        # With lower threshold, splits
        segments = _split_toc_entries([text], min_entries_trigger=2)
        self.assertEqual(len(segments), 2)

    def test_non_toc_paragraphs_unchanged(self) -> None:
        text = "This is a normal paragraph.\nIt has multiple lines.\nBut no dots."
        self.assertEqual(_split_toc_entries([text]), [text])

    def test_trailing_non_toc_lines_attach_to_last_segment(self) -> None:
        text = (
            "A .................. 1\n"
            "B .................. 2\n"
            "C .................. 3\n"
            "Note: continued on next page"
        )
        segments = _split_toc_entries([text])
        self.assertEqual(len(segments), 3)
        self.assertIn("Note: continued", segments[-1])

    def test_splits_entries_sharing_one_line(self) -> None:
        # Some PDFs pack multiple entries into one physical line separated by spaces.
        text = "1. General .................. 401   2. Procedure .................. 402   3. Notes .................. 403"
        segments = _split_toc_entries([text])
        self.assertEqual(len(segments), 3)
        self.assertIn("General", segments[0])
        self.assertIn("Procedure", segments[1])
        self.assertIn("Notes", segments[2])

    def test_recognises_not_applicable_terminator(self) -> None:
        text = (
            "DISASSEMBLY ................................ Not applicable\n"
            "CLEANING ................................... Not applicable\n"
            "INSPECTION ................................. 501\n"
        )
        segments = _split_toc_entries([text])
        self.assertEqual(len(segments), 3)
        self.assertIn("DISASSEMBLY", segments[0])
        self.assertIn("CLEANING", segments[1])
        self.assertIn("INSPECTION", segments[2])


if __name__ == "__main__":
    unittest.main()
