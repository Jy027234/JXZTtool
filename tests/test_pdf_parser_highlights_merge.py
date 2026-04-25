"""Tests for HIGHLIGHTS/change-log entry merging."""
from __future__ import annotations

import unittest

from parsecore.parsers import _merge_highlights_entries


class MergeHighlightsEntriesTests(unittest.TestCase):
    def test_noop_without_highlights_header(self) -> None:
        paragraphs = [
            "Ordinary paragraph.",
            "Changed to update general information.",
            "Pages 10 and 11",
        ]
        self.assertEqual(_merge_highlights_entries(paragraphs), paragraphs)

    def test_merges_page_ref_only_with_following_change(self) -> None:
        paragraphs = [
            "HIGHLIGHTS Page 2 CHAPTER/Section/Page Description of Change Check Pages 501 thru 504",
            "Pages 505 and 506",
            "Changed to update general information and Elements to be inspected table.",
        ]

        merged = _merge_highlights_entries(paragraphs)

        self.assertEqual(len(merged), 2)
        self.assertIn("Pages 505 and 506", merged[1])
        self.assertIn("Changed to update general information", merged[1])

    def test_merges_successive_change_blocks_when_previous_has_one_page_ref(self) -> None:
        paragraphs = [
            "HIGHLIGHTS Page 1 CHAPTER/Section/Page Description of Change",
            "Added Revision 6. Service Bulletin List Page 1/2",
            "Added SB 73-0248. List of Effective Pages Pages 1 thru 6",
            "Changed to indicate revised pages and format changes. Table of Contents Page 3 Page 4",
            "Changed to add Repair No. 1 and Repair No.2. Description and operation Pages 11/12 and 15",
            "Changed to update illustrations.",
        ]

        merged = _merge_highlights_entries(paragraphs)

        self.assertEqual(len(merged), 4)
        self.assertIn("List of Effective Pages", merged[1])
        self.assertIn("Changed to update illustrations.", merged[3])

    def test_does_not_merge_after_multi_page_ref_block(self) -> None:
        paragraphs = [
            "HIGHLIGHTS Page 1 CHAPTER/Section/Page Description of Change",
            "Added Revision 6. Service Bulletin List Page 1/2\nAdded SB 73-0248. List of Effective Pages Pages 1 thru 6",
            "Changed to indicate revised pages and format changes. Table of Contents Page 3 Page 4",
        ]

        merged = _merge_highlights_entries(paragraphs)

        self.assertEqual(len(merged), 3)
        self.assertEqual(merged, paragraphs)


if __name__ == "__main__":
    unittest.main()
