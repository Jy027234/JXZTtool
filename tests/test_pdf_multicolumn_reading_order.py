"""Two-column reading order: place lines by region, never cut them in half.

The previous implementation cropped the page into fixed vertical bands, so any
line that crossed the gutter was split: on a real 飞行运行手册 page the header
"飞行运行手册" came back as "飞行" in the left band and "行运行手册" in the
right one, the "*-状态符号…" footnote lost its tail, and the list rows were
separated from their own right-hand values.  These tests pin the line-level
region order that replaced it.
"""

from __future__ import annotations

import unittest

from parsecore.parsers import _extract_text_by_columns


class _FakePage:
    """A pdfplumber-like page built from explicit word geometry.

    Words on one baseline are grouped into lines unless a horizontal gap wider
    than ``_X_TOLERANCE`` separates them, which is how pdfplumber keeps the two
    columns of a page apart.
    """

    _X_TOLERANCE = 15.0

    def __init__(self, words, *, width=595.0, height=842.0, line_api=True):
        self.width = width
        self.height = height
        self.words = [dict(word) for word in words]
        self._line_api = line_api

    def filter(self, predicate):
        kept = _FakePage([], width=self.width, height=self.height, line_api=self._line_api)
        kept.words = [word for word in self.words if predicate(word)]
        return kept

    def extract_text(self):
        return "\n".join(line["text"] for line in self._lines())

    def extract_text_lines(self):
        if not self._line_api:
            raise AttributeError("extract_text_lines")
        return self._lines()

    def _lines(self):
        rows: dict[int, list[dict]] = {}
        for word in self.words:
            rows.setdefault(round(float(word["top"]) / 3.0), []).append(word)
        lines = []
        for key in sorted(rows):
            group = sorted(rows[key], key=lambda item: float(item["x0"]))
            current = [group[0]]
            for word in group[1:]:
                if float(word["x0"]) - float(current[-1]["x1"]) > self._X_TOLERANCE:
                    lines.append(self._line(current))
                    current = [word]
                else:
                    current.append(word)
            lines.append(self._line(current))
        return lines

    @staticmethod
    def _line(group: list[dict]) -> dict:
        return {
            "text": " ".join(str(word["text"]) for word in group),
            "x0": float(group[0]["x0"]),
            "x1": float(group[-1]["x1"]),
            "top": float(group[0]["top"]),
            "bottom": float(group[0]["top"]) + 10.0,
        }


def _word(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 10.0}


#: Page width 600: left column 60-280, right column 320-540, gutter in between.
LEFT = (60.0, 280.0)
RIGHT = (320.0, 540.0)


class TwoColumnReadingOrderTests(unittest.TestCase):
    def test_a_spanning_title_is_not_cut_at_the_gutter(self) -> None:
        page = _FakePage(
            [
                _word("飞行运行手册", 200, 400, 20),
                _word("左栏第一行", *LEFT, 100),
                _word("左栏第二行", *LEFT, 130),
                _word("右栏第一行", *RIGHT, 100),
                _word("右栏第二行", *RIGHT, 130),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertIn("飞行运行手册", text)
        self.assertNotIn("运行手册\n", text.split("左栏")[0].replace("飞行运行手册", ""))
        self.assertEqual(
            text.split("\n\n"),
            ["飞行运行手册", "左栏第一行\n左栏第二行", "右栏第一行\n右栏第二行"],
        )

    def test_a_full_width_row_keeps_its_own_line(self) -> None:
        # A repeating table header and its rows cross the gutter as one line:
        # they belong to the page flow, not to either column.
        page = _FakePage(
            [
                _word("飞行运行手册", 200, 400, 20),
                _word("编号 章节 * 日期 编号 章节 * 日期", 88, 544, 160),
                _word("0.1-1 第 0 章 N 0.2-1 第 0 章 R", 84, 563, 200),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertEqual(
            text.split("\n\n"),
            [
                "飞行运行手册",
                "编号 章节 * 日期 编号 章节 * 日期",
                "0.1-1 第 0 章 N 0.2-1 第 0 章 R",
            ],
        )

    def test_the_same_values_as_column_lines_are_read_column_by_column(self) -> None:
        # Same content, but each column's value is its own line: now the regions
        # are two columns and each one is read to its end.
        page = _FakePage(
            [
                _word("编号", 88, 300, 160),
                _word("编号", 320, 544, 160),
                _word("0.1-1", 84, 300, 200),
                _word("0.2-1", 320, 563, 200),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertEqual(text.split("\n\n"), ["编号\n0.1-1", "编号\n0.2-1"])

    def test_a_mid_page_heading_starts_a_new_column_region(self) -> None:
        page = _FakePage(
            [
                _word("左栏上", *LEFT, 100),
                _word("右栏上", *RIGHT, 100),
                _word("第 2 章 系统说明", 150, 450, 300),
                _word("左栏下", *LEFT, 400),
                _word("右栏下", *RIGHT, 400),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertEqual(
            text.split("\n\n"),
            ["左栏上", "右栏上", "第 2 章 系统说明", "左栏下", "右栏下"],
        )

    def test_lines_never_move_across_a_column(self) -> None:
        page = _FakePage(
            [
                _word("左一", *LEFT, 100),
                _word("右一", *RIGHT, 100),
                _word("左二", *LEFT, 130),
                _word("右二", *RIGHT, 130),
                _word("左三", *LEFT, 160),
                _word("右三", *RIGHT, 160),
            ]
        )

        blocks = _extract_text_by_columns(page, [], column_count=2).split("\n\n")

        self.assertEqual(blocks, ["左一\n左二\n左三", "右一\n右二\n右三"])

    def test_an_excluded_table_region_is_left_out(self) -> None:
        page = _FakePage(
            [
                _word("正文行", *LEFT, 100),
                _word("表格单元", 200, 400, 200),
                _word("后续正文", *LEFT, 300),
            ]
        )

        text = _extract_text_by_columns(page, [(150.0, 190.0, 450.0, 260.0)], column_count=2)

        self.assertIn("正文行", text)
        self.assertIn("后续正文", text)
        self.assertNotIn("表格单元", text)

    def test_a_single_column_page_keeps_the_existing_path(self) -> None:
        page = _FakePage([_word("单栏正文", 60, 540, 100)])

        self.assertEqual(
            _extract_text_by_columns(page, [], column_count=1), "单栏正文"
        )

    def test_a_page_without_line_geometry_falls_back_to_plain_text(self) -> None:
        page = _FakePage(
            [_word("左栏", *LEFT, 100), _word("右栏", *RIGHT, 100)], line_api=False
        )

        self.assertEqual(_extract_text_by_columns(page, [], column_count=2), "左栏\n右栏")


class ParagraphMergingInsideARegionTests(unittest.TestCase):
    """Wrapped lines of one paragraph are joined; separate facts are not."""

    def test_wrapped_chinese_lines_are_joined_without_a_space(self) -> None:
        page = _FakePage(
            [
                _word("本手册规定了主机轮的检查要求与适用范围，", *LEFT, 100),
                _word("并给出对应的验收判据与记录格式。", *LEFT, 115),
                _word("维修单位应按本手册执行并保留记录，", *RIGHT, 100),
                _word("不得自行降低检查标准或省略步骤。", *RIGHT, 115),
            ]
        )

        blocks = _extract_text_by_columns(page, [], column_count=2).split("\n\n")

        self.assertEqual(
            blocks,
            [
                "本手册规定了主机轮的检查要求与适用范围，并给出对应的验收判据与记录格式。",
                "维修单位应按本手册执行并保留记录，不得自行降低检查标准或省略步骤。",
            ],
        )

    def test_a_finished_sentence_and_a_new_item_start_their_own_line(self) -> None:
        # Sentence-final punctuation ends a paragraph, and a numbered item opens a
        # new one, so neither is welded onto the previous line.
        page = _FakePage(
            [
                _word("第一段已经完整结束了，不会再接下一行内容。", *LEFT, 100),
                _word("第二段从这里重新开始并继续展开说明。", *LEFT, 115),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertEqual(
            text.split("\n"),
            [
                "第一段已经完整结束了，不会再接下一行内容。",
                "第二段从这里重新开始并继续展开说明。",
            ],
        )

    def test_latin_lines_are_joined_with_a_space_and_a_split_word_is_repaired(self) -> None:
        page = _FakePage(
            [
                _word("The inspection procedure requires a torque wrench", *LEFT, 100),
                _word("and a calibrated gauge for every measurement.", *LEFT, 115),
                _word("Do not reuse a component whose surface is", *LEFT, 130),
                _word("dam-", *LEFT, 145),
                _word("aged by heat or corrosion.", *LEFT, 160),
            ]
        )

        text = _extract_text_by_columns(page, [], column_count=2)

        self.assertIn(
            "The inspection procedure requires a torque wrench and a calibrated gauge for every measurement.",
            text,
        )
        self.assertIn("damaged by heat or corrosion.", text)

    def test_a_caption_between_paragraphs_keeps_its_own_block(self) -> None:
        # A caption is a region type of its own: it must not be swallowed by the
        # paragraph above it, and the paragraph below it is a separate block.
        page = _FakePage(
            [
                _word("拆卸前应先释放系统压力并断开电源，", *LEFT, 100),
                _word("确认压力表读数为零后方可继续作业。", *LEFT, 115),
                _word("图 1 液压系统泄压示意", *LEFT, 130),
                _word("安装时按相反顺序执行并检查密封件，", *LEFT, 145),
                _word("力矩值见表 2 的规定。", *LEFT, 160),
            ]
        )

        blocks = _extract_text_by_columns(page, [], column_count=2).split("\n\n")

        self.assertEqual(
            blocks,
            [
                "拆卸前应先释放系统压力并断开电源，确认压力表读数为零后方可继续作业。",
                "图 1 液压系统泄压示意",
                "安装时按相反顺序执行并检查密封件，力矩值见表 2 的规定。",
            ],
        )

    def test_a_footnote_at_the_page_bottom_is_its_own_block(self) -> None:
        page = _FakePage(
            [
                _word("正文段落说明检查项目与判定依据，", *LEFT, 100),
                _word("并按顺序记录每一项结果。", *LEFT, 115),
                _word("注：表中数值为参考值，实际以工卡为准。", 60, 540, 800),
            ]
        )

        blocks = _extract_text_by_columns(page, [], column_count=2).split("\n\n")

        self.assertEqual(blocks[-1], "注：表中数值为参考值，实际以工卡为准。")
        self.assertEqual(blocks[0], "正文段落说明检查项目与判定依据，并按顺序记录每一项结果。")

    def test_a_numbered_body_line_in_the_middle_is_not_a_footnote(self) -> None:
        # The same marker in the middle of the page is body text: only the bottom
        # band plus a marker makes a footnote.
        page = _FakePage(
            [
                _word("1. 检查项目如下说明并逐条执行确认。", *LEFT, 300),
                _word("2. 完成后再进行复核并签字确认。", *LEFT, 315),
            ]
        )

        blocks = _extract_text_by_columns(page, [], column_count=2).split("\n\n")

        self.assertEqual(len(blocks), 1)
        self.assertIn("1. 检查项目如下说明并逐条执行确认。", blocks[0])
        self.assertIn("2. 完成后再进行复核并签字确认。", blocks[0])


if __name__ == "__main__":
    unittest.main()
