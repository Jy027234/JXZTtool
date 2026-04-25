"""Tests for table continuation merging in the PDF parser post-process stack."""
from __future__ import annotations

import unittest

from parsecore.parsers import _merge_table_continuations


class MergeTableContinuationsTests(unittest.TestCase):
    def test_noop_without_table_header(self) -> None:
        paragraphs = [
            "Normal paragraph one.",
            "Another paragraph with multiple lines\nand narrative content.",
            "Final paragraph.",
        ]
        self.assertEqual(_merge_table_continuations(paragraphs), paragraphs)

    def test_merges_address_fragments_into_previous_row(self) -> None:
        paragraphs = [
            "Page 614 73-22-87 May 31/08",
            "TABLE 602 TOOLS AND EQUIPMENT NECESSARY FOR REPAIR (continued)",
            "Index Name P/N or Type Manufacturer",
            "NOTE: You can use alternative tools and/or equipment to replace these items.",
            "K Extraction tool for contacts size 16 not connected M81969-30-06 Oceta 16 rue des Suisses 92380 Garches FRANCE",
            "R R R Daniels MFG Corp. 526 Thorpe Rd",
            "Orlando FL 32824-8133 USA",
            "L Insertion/extraction tool for contacts size 16 M81969-14-03 Oceta 16 rue des Suisses 92380 Garches FRANCE",
            "R R R Daniels MFG Corp. 526 Thorpe Rd",
            "Orlando FL 32824-8133 USA",
        ]

        merged = _merge_table_continuations(paragraphs)

        self.assertEqual(len(merged), 6)
        self.assertIn("Daniels MFG Corp.", merged[4])
        self.assertIn("Orlando FL 32824-8133 USA", merged[4])
        self.assertIn("Daniels MFG Corp.", merged[5])
        self.assertIn("Orlando FL 32824-8133 USA", merged[5])

    def test_recognises_row_start_with_leading_marker_columns(self) -> None:
        paragraphs = [
            "Page 613 73-22-87 May 31/08",
            "TABLE 602 TOOLS AND EQUIPMENT NECESSARY FOR REPAIR (continued)",
            "Index Name P/N or Type Manufacturer",
            "NOTE: You can use alternative tools and/or equipment to replace these items.",
            "R R G Pliers with protected jaws 410 Facom SA 6 rue Gustave Eiffel BP 99 91423 Morangis Cedex FRANCE",
            "Facom Tools 3535 West 47th Street Chicago IL 60632 USA",
            "H Extraction tool for contacts size 20 not connected M81969-30-05 Oceta 16 rue des Suisses 92380 Garches FRANCE",
        ]

        merged = _merge_table_continuations(paragraphs)

        self.assertEqual(len(merged), 6)
        self.assertIn("Facom Tools 3535 West 47th Street Chicago IL 60632 USA", merged[4])
        self.assertEqual(merged[5], paragraphs[6])

    def test_keeps_note_separate_from_first_row(self) -> None:
        paragraphs = [
            "TABLE 602 TOOLS AND EQUIPMENT NECESSARY FOR REPAIR (continued)",
            "Index Name P/N or Type Manufacturer",
            "NOTE: You can use alternative tools and/or equipment to replace these items.",
            "W Crimping pliers for contacts with turret M22520/1-01",
            "M22520/1-02 Oceta 16 rue des Suisses 92380 Garches FRANCE",
        ]

        merged = _merge_table_continuations(paragraphs)

        self.assertEqual(len(merged), 4)
        self.assertTrue(merged[2].startswith("NOTE:"))
        self.assertTrue(merged[3].startswith("W Crimping pliers"))
        self.assertIn("M22520/1-02", merged[3])


if __name__ == "__main__":
    unittest.main()
