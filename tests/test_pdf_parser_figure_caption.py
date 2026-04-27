"""Tests for figure-caption adjacency merge in PDF parser post-process stack."""
from __future__ import annotations

import unittest

from parsecore.parsers import _merge_figure_caption_paragraphs


class MergeFigureCaptionParagraphsTests(unittest.TestCase):
    def test_merges_label_only_figure_paragraph_with_following_caption(self) -> None:
        paragraphs = [
            "Figure 3-1.",
            "Hydraulic pump assembly layout and connector orientation.",
            "Next procedural paragraph stays independent.",
        ]

        merged = _merge_figure_caption_paragraphs(paragraphs)

        self.assertEqual(len(merged), 2)
        self.assertEqual(
            merged[0],
            "Figure 3-1.\nHydraulic pump assembly layout and connector orientation.",
        )
        self.assertEqual(merged[1], "Next procedural paragraph stays independent.")

    def test_does_not_merge_when_label_already_contains_caption_text(self) -> None:
        paragraphs = [
            "Figure 4-2. Pump manifold exploded view",
            "Step 1. Disconnect the hose.",
        ]

        merged = _merge_figure_caption_paragraphs(paragraphs)

        self.assertEqual(merged, paragraphs)

    def test_does_not_merge_into_structural_or_heading_like_following_paragraph(self) -> None:
        paragraphs = [
            "Figure 2-1.",
            "1. Removal procedure",
            "(a) Release pressure.",
        ]

        merged = _merge_figure_caption_paragraphs(paragraphs)

        self.assertEqual(merged, paragraphs)

    def test_supports_fig_abbreviation_and_illustration_prefix(self) -> None:
        paragraphs = [
            "Fig. 5-3",
            "Main valve body with reference callouts.",
            "Illustration A1.",
            "Harness routing under side panel.",
        ]

        merged = _merge_figure_caption_paragraphs(paragraphs)

        self.assertEqual(len(merged), 2)
        self.assertIn("Main valve body", merged[0])
        self.assertIn("Harness routing", merged[1])


if __name__ == "__main__":
    unittest.main()
