"""待办 1: map docling's structured document directly instead of via Markdown.

Docling already knows the heading level, the table grid with its merged cells, the
inline runs and the provenance of every item.  Exporting to Markdown and rebuilding
blocks from that text throws the parts Markdown cannot express away and then makes
the parser guess the grid back -- on the sample below the merged header would come
back as two identical `Part` columns with no way to tell a merge from a repeat.

The Markdown path stays for converters that only produce Markdown (TextParser,
PyMuPDF4LLM) and for docling results without a structured document.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from parsecore.models import BlockType, ParseRequest, SemanticRole
from parsecore.parsers import DoclingParser, _build_docling_provider_blocks


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _FakeFormatting:
    def __init__(self, *, bold=False, italic=False, underline=False, script="baseline"):
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.script = script


class _FakeLabel:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeProv:
    def __init__(self, page_no: int | None, bbox=None) -> None:
        self.page_no = page_no
        self.bbox = bbox


class _FakeCell:
    def __init__(self, text, row, column, *, row_span=1, col_span=1, column_header=False):
        self.text = text
        self.start_row_offset_idx = row
        self.start_col_offset_idx = column
        self.row_span = row_span
        self.col_span = col_span
        self.column_header = column_header
        self.row_header = False


class _FakeTableData:
    def __init__(self, cells, *, num_rows, num_cols, grid=None):
        self.table_cells = cells
        self.num_rows = num_rows
        self.num_cols = num_cols
        self.grid = grid or []


class _FakeItem:
    def __init__(self, label, *, text="", level=1, data=None, formatting=None, hyperlink=None, prov=None):
        self.label = _FakeLabel(label)
        self.text = text
        self.data = data
        self.formatting = formatting
        self.hyperlink = hyperlink
        self.prov = prov or []


class _FakeDocument:
    def __init__(self, items, *, name="structured") -> None:
        self.name = name
        self._items = list(items)

    def iterate_items(self):
        return iter(self._items)


class _FakeConversionResult:
    def __init__(self, document) -> None:
        self.document = document


MERGED_TABLE = _FakeTableData(
    [
        _FakeCell("Part", 0, 0, col_span=2, column_header=True),
        _FakeCell("Qty", 0, 2, column_header=True),
        _FakeCell("Bolt", 1, 0, row_span=2),
        _FakeCell("M2", 1, 1),
        _FakeCell("4", 1, 2),
        _FakeCell("M10", 2, 1),
        _FakeCell("2", 2, 2),
    ],
    num_rows=3,
    num_cols=3,
    grid=[["Part", "Part", "Qty"], ["Bolt", "M2", "4"], ["Bolt", "M10", "2"]],
)


def _request() -> ParseRequest:
    return ParseRequest(doc_id="doc-1", file_path="structured.docx", media_type=DOCX_MEDIA_TYPE)


def _structured_document() -> _FakeDocument:
    return _FakeDocument(
        [
            (
                _FakeItem(
                    "section_header",
                    text="Revision record",
                    level=1,
                    prov=[_FakeProv(3, (10.0, 20.0, 200.0, 40.0))],
                ),
                1,
            ),
            (
                _FakeItem(
                    "text",
                    text="Apply the specified torque.",
                    level=2,
                    prov=[_FakeProv(3)],
                ),
                2,
            ),
            (_FakeItem("table", level=2, data=MERGED_TABLE, prov=[_FakeProv(3)]), 2),
            (
                _FakeItem(
                    "text",
                    text="WARNING: do not reuse",
                    level=2,
                    formatting=_FakeFormatting(bold=True, underline=True),
                ),
                2,
            ),
            (_FakeItem("page_header", text="CMM 32-47-46", level=2), 2),
            (_FakeItem("formula", text="T = 25 \\text{ N.m}", level=2), 2),
        ]
    )


class DoclingStructureMappingTests(unittest.TestCase):
    def _blocks(self):
        blocks = _build_docling_provider_blocks(
            _FakeConversionResult(_structured_document()),
            request=_request(),
            provider_name="docling-local",
            provider_version="2.test",
        )
        self.assertIsNotNone(blocks)
        return list(blocks or [])

    def test_heading_levels_come_from_the_structure(self) -> None:
        blocks = self._blocks()
        heading = next(block for block in blocks if block.content == "Revision record")
        self.assertEqual(heading.type, BlockType.TITLE)
        self.assertTrue(heading.metadata["is_section_heading"])
        self.assertEqual(heading.metadata["heading_level"], 1)
        self.assertEqual(heading.metadata["semantic_role"], SemanticRole.BODY_SECTION.value)
        self.assertEqual(heading.metadata["docling_label"], "section_header")
        self.assertEqual(heading.metadata["page"], 3)
        self.assertEqual(heading.metadata["bbox"], (10.0, 20.0, 200.0, 40.0))

    def test_a_merged_table_keeps_its_spans_and_derives_its_rows(self) -> None:
        table = next(block for block in self._blocks() if block.type == BlockType.TABLE)

        cells = {(cell["row_index"], cell["col_index"]): cell for cell in table.metadata["table_cells"]}
        self.assertEqual(cells[(0, 0)]["col_span"], 2)
        self.assertTrue(cells[(0, 1)]["continuation"])
        self.assertEqual(cells[(0, 1)]["origin"], "0.0")
        self.assertEqual(cells[(1, 0)]["row_span"], 2)
        self.assertEqual(table.metadata["header_rows"], 1)
        self.assertEqual(
            sorted(merge["kind"] for merge in table.metadata["merged_cells"]),
            ["col_span", "row_span"],
        )
        # The flat rows repeat the merged value; the rendered text is derived from
        # the same grid, so the reader and the structured cells cannot disagree.
        self.assertEqual(
            table.metadata["cells"][0][:3],
            ["Part", "Part", "Qty"],
        )
        self.assertEqual(table.metadata["table_index"], 1)
        self.assertIn("| Part | Part | Qty |", table.content)
        self.assertEqual(table.metadata["header_values"], ["Part", "Part", "Qty"])

    def test_inline_runs_and_hyperlink_survive(self) -> None:
        warning = next(block for block in self._blocks() if block.content == "WARNING: do not reuse")
        self.assertEqual(warning.metadata["runs"], [{"text": "WARNING: do not reuse", "bold": True, "underline": True}])

    def test_page_headers_keep_their_role_and_formulas_stay_unverified(self) -> None:
        blocks = self._blocks()
        header = next(block for block in blocks if block.content == "CMM 32-47-46")
        self.assertEqual(header.metadata["semantic_role"], SemanticRole.HEADER_FOOTER.value)
        formula = next(block for block in blocks if "N.m" in block.content)
        self.assertTrue(formula.metadata["formula_unverified"])

    def test_missing_provenance_is_reported_as_a_logical_page(self) -> None:
        blocks = self._blocks()
        unlocated = [
            block
            for block in blocks
            if block.content == "WARNING: do not reuse"
        ]
        self.assertTrue(unlocated)
        self.assertEqual(unlocated[0].metadata["page"], 1)
        self.assertEqual(unlocated[0].metadata["page_kind"], "logical")

    def test_a_document_without_structure_returns_none(self) -> None:
        class _MarkdownOnly:
            def export_to_markdown(self) -> str:
                return "# Title"

        self.assertIsNone(
            _build_docling_provider_blocks(
                _MarkdownOnly(),
                request=_request(),
                provider_name="docling-local",
                provider_version=None,
            )
        )


class DoclingParserWiringTests(unittest.TestCase):
    def test_the_structured_path_is_used_when_available(self) -> None:
        parser = DoclingParser(
            media_types=[DOCX_MEDIA_TYPE],
            extensions=[".docx"],
            options={},
        )
        conversion = _FakeConversionResult(_structured_document())
        with patch.object(DoclingParser, "_get_converter", return_value=(object(), "2.test", False)), patch(
            "parsecore.parsers._docling_convert", return_value=conversion
        ), patch("parsecore.parsers._build_markdown_provider_blocks") as markdown_path:
            blocks = list(parser.parse(_request()))

        markdown_path.assert_not_called()
        self.assertTrue(any(block.type == BlockType.TABLE for block in blocks))
        self.assertTrue(all("converter_cache_hit" in block.metadata for block in blocks))

    def test_the_markdown_path_is_kept_for_markdown_only_results(self) -> None:
        class _MarkdownResult:
            def __init__(self) -> None:
                self.document = type(
                    "Doc", (), {"export_to_markdown": lambda self: "# Title\n\nBody text."}
                )()

        parser = DoclingParser(media_types=[DOCX_MEDIA_TYPE], extensions=[".docx"], options={})
        with patch.object(DoclingParser, "_get_converter", return_value=(object(), "2.test", False)), patch(
            "parsecore.parsers._docling_convert", return_value=_MarkdownResult()
        ):
            blocks = list(parser.parse(_request()))

        self.assertTrue(blocks)
        self.assertTrue(any("Title" in block.content for block in blocks))


def _docling_available() -> bool:
    import importlib.util

    return bool(importlib.util.find_spec("docling")) and bool(importlib.util.find_spec("docx"))


@unittest.skipUnless(_docling_available(), "docling and python-docx are required")
class RealDoclingStructureTests(unittest.TestCase):
    """The same expectations against a real conversion (docling runs locally)."""

    def _write_docx(self, target: Path) -> Path:
        from docx import Document

        document = Document()
        document.add_heading("Revision record", level=1)
        document.add_paragraph("Apply the specified torque to the fastener.")
        table = document.add_table(rows=3, cols=3)
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(0, 0).text = "Part"
        table.cell(0, 2).text = "Qty"
        table.cell(1, 0).merge(table.cell(2, 0))
        table.cell(1, 0).text = "Bolt"
        table.cell(1, 1).text = "M2"
        table.cell(1, 2).text = "4"
        table.cell(2, 1).text = "M10"
        table.cell(2, 2).text = "2"
        paragraph = document.add_paragraph()
        run = paragraph.add_run("WARNING: do not reuse")
        run.bold = True
        run.underline = True
        document.save(str(target))
        return target

    def test_a_real_conversion_keeps_the_merged_table(self) -> None:
        # Importing the real package into the test process has global effects:
        # on Windows the docling loader purges and re-imports ``docling*`` modules
        # to route the native extension through an ASCII junction, which would
        # destroy the module doubles other tests install.  The modules this test
        # loaded are therefore removed again.
        before = {
            name
            for name in sys.modules
            if name == "docling"
            or name.startswith("docling.")
            or name == "docling_parse"
            or name.startswith("docling_parse.")
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = self._write_docx(Path(directory) / "structured.docx")
                parser = DoclingParser(
                    media_types=[DOCX_MEDIA_TYPE], extensions=[".docx"], options={}
                )
                blocks = list(
                    parser.parse(
                        ParseRequest(doc_id="doc-real", file_path=str(source), media_type=DOCX_MEDIA_TYPE)
                    )
                )
        finally:
            for name in [
                item
                for item in sys.modules
                if item == "docling"
                or item.startswith("docling.")
                or item == "docling_parse"
                or item.startswith("docling_parse.")
            ]:
                if name not in before:
                    sys.modules.pop(name, None)

        tables = [block for block in blocks if block.type == BlockType.TABLE]
        self.assertEqual(len(tables), 1)
        cells = {
            (cell["row_index"], cell["col_index"]): cell
            for cell in tables[0].metadata["table_cells"]
        }
        self.assertEqual(cells[(0, 0)]["col_span"], 2)
        self.assertTrue(cells[(0, 1)]["continuation"])
        self.assertEqual(cells[(1, 0)]["row_span"], 2)
        self.assertEqual(tables[0].metadata["header_rows"], 1)
        # A Markdown round-trip would show "Part | Part | Qty" with no way to tell
        # the merge from a repeated value; the structured cells keep it.
        headings = [
            block
            for block in blocks
            if block.metadata.get("heading_level") and "Revision" in block.content
        ]
        self.assertTrue(headings)
        warnings = [block for block in blocks if block.content == "WARNING: do not reuse"]
        self.assertTrue(warnings)
        self.assertEqual(
            warnings[0].metadata.get("runs"),
            [{"text": "WARNING: do not reuse", "bold": True, "underline": True}],
        )
        self.assertTrue(all(block.metadata.get("page_kind") == "logical" for block in blocks[1:]))


if __name__ == "__main__":
    unittest.main()
