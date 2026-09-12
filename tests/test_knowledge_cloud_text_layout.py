import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from parsecore.models import ParseRequest
from parsecore.parsers import TextParser, _estimate_column_count


class KnowledgeCloudTextLayoutTest(unittest.TestCase):
    def test_markdown_keeps_body_headings_and_tables_without_collector_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.md"
            source.write_text("---\nstatus: 有效\nurl: https://example.invalid\n---\n# 第一章\n正文不能因采集标签变为已审核。\n\n## 数值\n\n| 条件 | 值 |\n| --- | --- |\n| 工作压力 | 3倍 |\n", encoding="utf-8")
            blocks = TextParser(media_types=["text/markdown"], extensions=[".md"]).parse(ParseRequest(doc_id="markdown", file_path=str(source)))
            content = "\n".join(block.content for block in blocks)
            self.assertNotIn("status:", content)
            self.assertNotIn("example.invalid", content)
            self.assertIn("第一章", content)
            self.assertTrue(any(block.metadata.get("heading_level") == 2 for block in blocks))
            self.assertTrue(any(block.metadata.get("cells") == [["条件", "值"], ["工作压力", "3倍"]] for block in blocks))
            self.assertEqual(blocks[0].metadata["page_mapping"], "text_document")

    def test_unclosed_frontmatter_is_rejected_instead_of_becoming_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.md"
            source.write_text("---\nstatus: 有效\n# 正文", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "markdown_frontmatter_unclosed"):
                TextParser(media_types=[], extensions=[".md"]).parse(ParseRequest(doc_id="bad", file_path=str(source)))

    def test_aligned_chinese_columns_are_read_as_columns_not_one_wide_line(self):
        page = SimpleNamespace(width=600, height=800)
        words = []
        for y in range(100, 700, 30):
            words += [{"text": "左栏法规条文", "x0": 50, "x1": 280, "top": y},
                      {"text": "右栏下一条文", "x0": 320, "x1": 550, "top": y}]
        self.assertEqual(_estimate_column_count(page, words=words), 2)
        words = [{"text": "连续单栏法规条文", "x0": 50, "x1": 550, "top": y} for y in range(100, 700, 30)]
        self.assertEqual(_estimate_column_count(page, words=words), 1)
        # English word spacing alone does not establish a column gutter.
        words = [{"text": "word", "x0": x, "x1": x + 44, "top": y} for y in range(100, 700, 30) for x in range(50, 550, 48)]
        self.assertEqual(_estimate_column_count(page, words=words), 1)


if __name__ == "__main__":
    unittest.main()
