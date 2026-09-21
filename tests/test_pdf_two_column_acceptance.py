"""Acceptance sample for the two-column reading order (待办 2).

The gate reports ``multi_col=0`` for the available real samples, so the column
path had no acceptance page of its own.  This module builds one: a page with a
spanning title, two full columns, a caption and a bottom footnote, all placed at
explicit coordinates, and checks the reading order and the paragraph merging on
it -- first on the geometry path, then through the runtime.
"""

from __future__ import annotations

import unittest

from parsecore.bootstrap import build_runtime
from parsecore.models import BlockType, ParseRequest
from parsecore.parsers import PdfTextParser, _extract_text_by_columns
from tests.support import TemporaryWorkspace
from tests.test_runtime import PDF_SAMPLE_CONFIG

TITLE = "Wheel Inspection Manual"
#: Long enough that the repeated-gutter detector sees a real page: it needs a
#: gutter across at least eight rows and six vertical bands, which a six-line
#: sketch does not provide.
LEFT_LINES = [
    "The inspection procedure requires a",
    "torque wrench and a calibrated gauge",
    "for every measurement taken during",
    "the check, and the values must be",
    "recorded on the work card before",
    "the component is released to service.",
    "Do not reuse a component whose",
    "surface shows heat damage or any",
    "sign of corrosion on the bearing",
    "seat, and replace the seals when",
    "the wheel is disassembled for the",
    "scheduled inspection interval.",
    "Record the measured torque and the",
    "ambient temperature on the card.",
]
RIGHT_LINES = [
    "Remove the retaining ring with the",
    "spreader tool and lift the wheel off",
    "the axle without damaging the seal",
    "seat, then clean every mating face",
    "and inspect the bearing for pitting",
    "before the wheel is reassembled.",
    "Renew the seals whenever the wheel",
    "is opened, and lubricate the bearing",
    "with the grease stated on the data",
    "sheet, never mixing two greases in",
    "the same assembly because the",
    "mixture hardens at low temperature.",
    "Fit the retaining ring with the",
    "spreader tool and check the seating.",
]
CAPTION = "Figure 1 Wheel assembly"
FOOTNOTE = "Note: values are for reference only."


def _two_column_page() -> list[tuple[float, float, str]]:
    lines: list[tuple[float, float, str]] = [(220.0, 740.0, TITLE)]
    for index, text in enumerate(LEFT_LINES[:12] + [CAPTION]):
        lines.append((60.0, 700.0 - index * 20.0, text))
    for index, text in enumerate(RIGHT_LINES[:12]):
        lines.append((330.0, 700.0 - index * 20.0, text))
    lines.append((60.0, 60.0, FOOTNOTE))
    return lines


class TwoColumnAcceptanceTests(unittest.TestCase):
    def _column_text(self, workspace: TemporaryWorkspace) -> str:
        """The geometry path on the real page, without the runtime in between."""

        import pdfplumber

        document_path = workspace.create_positioned_pdf("two-column.pdf", [_two_column_page()])
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"post_process": {"layout_reading_order": True}},
        )
        with pdfplumber.open(str(document_path)) as pdf:
            page = pdf.pages[0]
            return _extract_text_by_columns(page, [], column_count=2)

    def test_columns_do_not_interleave_and_wrapped_lines_merge(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            text = self._column_text(workspace)

        blocks = text.split("\n\n")
        # The title spans the gutter and stays whole, before the columns.
        self.assertIn(TITLE, blocks[0])
        # Wrapped lines of one paragraph are joined back into running text.
        self.assertIn(
            "The inspection procedure requires a torque wrench and a calibrated gauge"
            " for every measurement taken during the check, and the values must be"
            " recorded on the work card before the component is released to service.",
            text,
        )
        self.assertIn(
            "Remove the retaining ring with the spreader tool and lift the wheel off"
            " the axle without damaging the seal seat, then clean every mating face"
            " and inspect the bearing for pitting before the wheel is reassembled.",
            text,
        )
        # A caption and a footnote are region types of their own, not part of the
        # paragraph above them.
        self.assertIn(CAPTION, blocks)
        self.assertIn(FOOTNOTE, blocks)
        # No line moved across the column it belongs to.
        left_block = next(block for block in blocks if LEFT_LINES[0][:20] in block)
        for right_only in ("retaining ring", "spreader tool", "axle", "pitting"):
            self.assertNotIn(right_only, left_block)
        right_block = next(block for block in blocks if RIGHT_LINES[0][:20] in block)
        for left_only in ("torque wrench", "work card", "heat damage"):
            self.assertNotIn(left_only, right_block)
        # The left column is read before the right one.
        self.assertLess(
            next(index for index, block in enumerate(blocks) if LEFT_LINES[0][:20] in block),
            next(index for index, block in enumerate(blocks) if RIGHT_LINES[0][:20] in block),
        )

    def test_the_runtime_applies_column_reflow_to_the_sample(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_positioned_pdf(
                "two-column-runtime.pdf", [_two_column_page()]
            )
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-two-column",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"post_process": {"layout_reading_order": True, "ocr_bad_pages": False}},
                )
            )

        blocks = [block for block in outcome.blocks if block.type == BlockType.PARAGRAPH]
        strategies = {
            str((block.metadata or {}).get("layout_reading_order_strategy") or "")
            for block in blocks
        }
        self.assertIn("column-reflow", strategies)
        body = "\n".join(block.content for block in blocks)
        self.assertIn("torque wrench and a calibrated gauge", body)
        self.assertIn(CAPTION, body)
        self.assertIn("reference only", body)


if __name__ == "__main__":
    unittest.main()
