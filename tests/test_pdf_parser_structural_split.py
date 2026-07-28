from __future__ import annotations

import unittest

from parsecore.parsers import (
    PdfTextParser,
    _infer_pdf_structural_heading,
    _infer_semantic_role,
    _merge_cross_page_paragraph_blocks,
    _split_inline_structural_items,
    _split_structural_items,
)
from parsecore.models import Block, BlockType, SemanticRole


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


class SemanticRoleInferenceTests(unittest.TestCase):
    def test_preserves_safety_and_note_roles(self) -> None:
        self.assertEqual(_infer_semantic_role("NOTE: Retain this record."), SemanticRole.NOTE.value)
        self.assertEqual(_infer_semantic_role("警告：先断开电源。"), SemanticRole.WARNING.value)
        self.assertEqual(_infer_semantic_role("CAUTION: Wear gloves."), SemanticRole.CAUTION.value)

    def test_detects_generic_knowledge_structure_roles(self) -> None:
        cases = {
            "定义：责任经理是指经批准的岗位负责人。": SemanticRole.DEFINITION.value,
            '"Accountable manager" means the nominated executive.': SemanticRole.DEFINITION.value,
            "程序：工具校验管理": SemanticRole.PROCEDURE.value,
            "步骤 2 检查设备状态": SemanticRole.PROCEDURE_STEP.value,
            "第 三 步 检查签署记录": SemanticRole.PROCEDURE_STEP.value,
            "（一）检查适航资料": SemanticRole.LIST_ITEM.value,
            "- Retain the release record": SemanticRole.LIST_ITEM.value,
            "1.2 Record control requirements": SemanticRole.CLAUSE.value,
            "145.A.30 Personnel requirements": SemanticRole.CLAUSE.value,
            "第一章 总则": SemanticRole.BODY_SECTION.value,
            "第 145.28 条 维修管理手册": SemanticRole.CLAUSE.value,
            "CHAPTER 3 MAINTENANCE PROCEDURES": SemanticRole.BODY_SECTION.value,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_infer_semantic_role(text), expected)

    def test_extracts_generic_pdf_hierarchy_metadata_from_first_line(self) -> None:
        article = _infer_pdf_structural_heading(
            "第 145.28 条 维修管理手册\n维修单位应当建立并持续更新手册。"
        )
        nested = _infer_pdf_structural_heading(
            "4.2.1 工具设备采购选择评估要求\n按批准程序执行。"
        )

        self.assertIsNotNone(article)
        self.assertEqual(article.semantic_role, SemanticRole.CLAUSE.value)
        self.assertEqual(article.section_no, "第 145.28 条")
        self.assertEqual(article.heading_level, 2)
        self.assertIsNotNone(nested)
        self.assertEqual(nested.semantic_role, SemanticRole.CLAUSE.value)
        self.assertEqual(nested.section_no, "4.2.1")
        self.assertEqual(nested.heading_level, 3)


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

    def test_splits_chapters_articles_and_numbered_manual_sections(self) -> None:
        paragraph = "\n".join(
            [
                "document preamble",
                "filler one",
                "filler two",
                "第一章 总则",
                "第 145.1 条 目的和依据",
                "条款正文第一行",
                "条款正文第二行",
                "第 145.2 条 适用范围",
                "适用范围正文",
                "4.1 工具设备的请购",
                "请购程序正文",
                "4.2 工具设备的采购",
                "采购程序正文",
            ]
        )

        result = _split_structural_items([paragraph])

        self.assertTrue(any(item.startswith("第一章") for item in result))
        self.assertTrue(any(item.startswith("第 145.1 条") for item in result))
        self.assertTrue(any(item.startswith("第 145.2 条") for item in result))
        self.assertTrue(any(item.startswith("4.1 ") for item in result))
        self.assertTrue(any(item.startswith("4.2 ") for item in result))


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
        self.assertTrue(parser._merge_cross_page_enabled)

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
                    "merge_cross_page_paragraphs": False,
                }
            },
        )
        self.assertFalse(parser._split_structural_enabled)
        self.assertEqual(parser._structural_min_lines, 25)
        self.assertFalse(parser._split_inline_structural_enabled)
        self.assertEqual(parser._inline_structural_min_length, 250)
        self.assertFalse(parser._merge_cross_page_enabled)

    def test_full_fidelity_disables_cross_page_paragraph_merge(self) -> None:
        parser = PdfTextParser(
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={"fidelity_profile": "full_fidelity"},
        )
        self.assertFalse(parser._merge_cross_page_enabled)


class MergeCrossPageParagraphBlocksTests(unittest.TestCase):
    def test_merges_body_paragraph_continuation_across_pages(self) -> None:
        blocks = [
            Block(
                block_id="doc-title",
                doc_id="doc-cross-page",
                type=BlockType.TITLE,
                content="Manual",
                metadata={"page": 1, "semantic_role": SemanticRole.TITLE.value},
            ),
            Block(
                block_id="doc-p-1",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="8. 关键 DME。如果某个 DME 不可用时，将导致 DME/DME 不能提供满足导航服务，则该 DME 台",
                metadata={
                    "page": 13,
                    "page_type": "body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
            Block(
                block_id="doc-p-2",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="被称作关键 DME。这里假定飞机的 RNAV 系统满足最低标准。",
                metadata={
                    "page": 14,
                    "page_type": "body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
        ]

        merged = _merge_cross_page_paragraph_blocks(blocks)

        self.assertEqual(len(merged), 2)
        self.assertIn("DME 台被称作关键 DME", merged[1].content)
        self.assertEqual(merged[1].metadata["page"], 13)
        self.assertEqual(merged[1].metadata["page_end"], 14)
        self.assertEqual(merged[1].metadata["page_span"], [13, 14])
        self.assertTrue(merged[1].metadata["cross_page_continuation"])
        self.assertEqual(merged[1].metadata["merged_block_ids"], ["doc-p-1", "doc-p-2"])

    def test_keeps_new_numbered_item_separate_across_pages(self) -> None:
        blocks = [
            Block(
                block_id="doc-p-1",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="这里假定飞机的 RNAV 系统满足 AC-91FS-2008-09 中规定的最低标准",
                metadata={
                    "page": 14,
                    "page_type": "body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
            Block(
                block_id="doc-p-2",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="1. RNAV 航路。基于 RNAV 飞行方法划设的航路。",
                metadata={
                    "page": 15,
                    "page_type": "body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
        ]

        merged = _merge_cross_page_paragraph_blocks(blocks)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].content, blocks[0].content)
        self.assertEqual(merged[1].content, blocks[1].content)

    def test_keeps_headings_separate_across_pages(self) -> None:
        blocks = [
            Block(
                block_id="doc-p-1",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="前一页最后一个正文段落没有标点",
                metadata={
                    "page": 2,
                    "page_type": "body",
                    "semantic_role": SemanticRole.PARAGRAPH.value,
                },
            ),
            Block(
                block_id="doc-p-2",
                doc_id="doc-cross-page",
                type=BlockType.PARAGRAPH,
                content="第 3 章 维修能力说明",
                metadata={
                    "page": 3,
                    "page_type": "body",
                    "semantic_role": SemanticRole.BODY_SECTION.value,
                },
            ),
        ]

        merged = _merge_cross_page_paragraph_blocks(blocks)

        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
