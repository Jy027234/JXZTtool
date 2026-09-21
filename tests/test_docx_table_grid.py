"""Word table structure fidelity: native grid -> artifact -> Reader/IR/records.

Covers the merged-header Word path end to end.  The point of these tests is not
that output is non-empty: it is that the merge, the inline formatting and the
cell coordinates read from ``document.xml`` are the same ones the Reader, the
knowledge units, the records and the IR projection report, and that none of them
re-derives the grid from flattened text.
"""

from __future__ import annotations

import time
import unittest

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.models import ParseJobState, ParseRequest
from parsecore.parsers import DocxParser
from parsecore.tables import grid_from_metadata
from tests.support import (
    TemporaryWorkspace,
    build_docx_hyperlink,
    build_docx_paragraph,
    build_docx_table_xml,
)


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SAMPLE_CONFIG = """
[project]
name = "test-docx-grid"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[product]
adapter = "embedded"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


#: Header "Part" spans two columns; "Bolt" spans two rows; the last column adds an
#: inline hyperlink, a superscript and bold text.
MERGED_TABLE_BODY = build_docx_paragraph("Revision record") + build_docx_table_xml(
    [
        [{"text": "Part", "col_span": 2}, "Qty"],
        [
            {
                "text": "Bolt",
                "vmerge": "restart",
                "runs": ['<w:r><w:rPr><w:b/></w:rPr><w:t>Bolt</w:t></w:r>'],
            },
            {
                "runs": [
                    "<w:r><w:t>M</w:t></w:r>",
                    '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>2</w:t></w:r>',
                ]
            },
            {"runs": [build_docx_hyperlink("rId1", "spec")]},
        ],
        [{"vmerge": "continue"}, "M10", "3"],
    ]
)

HYPERLINKS = {"rId1": "https://example.com/spec"}


def _parse_docx_blocks(workspace: TemporaryWorkspace, name: str = "grid.docx"):
    document_path = workspace.create_docx_with_body(name, MERGED_TABLE_BODY, hyperlinks=HYPERLINKS)
    parser = DocxParser(media_types=[DOCX_MEDIA_TYPE], extensions=[".docx"])
    return parser.parse(ParseRequest(doc_id=f"doc-{name}", file_path=str(document_path)))


class WordTableGridExtractionTests(unittest.TestCase):
    """The parser is the only place the Word grid is read."""

    def test_merged_cells_keep_their_grid_position_and_span(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            blocks = _parse_docx_blocks(workspace)
        table = next(block for block in blocks if block.type.value == "table")
        metadata = table.metadata
        grid = grid_from_metadata(metadata)

        self.assertEqual((grid.row_count, grid.column_count), (3, 3))
        header = grid.cell_at(0, 0)
        self.assertIsNotNone(header)
        self.assertEqual(header.text, "Part")
        self.assertEqual(header.col_span, 2)
        # The second column of the header is the merge continuation, not a value.
        self.assertTrue(grid.cell_at(0, 1).continuation)
        self.assertEqual(grid.cell_at(0, 1).origin, "0.0")
        self.assertEqual(grid.cell_at(0, 2).text, "Qty")
        # vMerge: "Bolt" starts a two-row merge continued by the third row.
        self.assertEqual(grid.cell_at(1, 0).row_span, 2)
        self.assertTrue(grid.cell_at(2, 0).continuation)
        self.assertEqual(grid.cell_at(2, 0).origin, "1.0")
        self.assertEqual(grid.cell_at(2, 1).text, "M10")

    def test_merged_cells_and_headers_are_published_in_metadata(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            blocks = _parse_docx_blocks(workspace)
        metadata = next(block for block in blocks if block.type.value == "table").metadata

        kinds = sorted(
            (merge["kind"], merge["origin"]) for merge in metadata["merged_cells"]
        )
        self.assertEqual(kinds, [("col_span", "0.0"), ("row_span", "1.0")])
        # Both columns the merged header covers report that header.
        self.assertEqual(metadata["header_values"], ["Part", "Part", "Qty"])
        # The flattened rows are derived; they carry the merged value, not a hole.
        self.assertEqual(
            metadata["cells"],
            [["Part", "Part", "Qty"], ["Bolt", "M2", "spec"], ["Bolt", "M10", "3"]],
        )
        self.assertEqual(metadata["source_row_col_counts"], [3, 3, 3])

    def test_inline_formatting_survives_on_the_cell(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            blocks = _parse_docx_blocks(workspace)
        metadata = next(block for block in blocks if block.type.value == "table").metadata
        cells = {
            (cell["row_index"], cell["col_index"]): cell for cell in metadata["table_cells"]
        }

        self.assertEqual(cells[(1, 0)]["runs"], [{"text": "Bolt", "bold": True}])
        superscript = cells[(1, 1)]["runs"]
        self.assertEqual(superscript, [{"text": "2", "vertical_align": "superscript"}])
        self.assertEqual(cells[(1, 1)]["text"], "M2")
        # A link target lives in the relationship part, not in document.xml.
        self.assertEqual(
            cells[(1, 2)]["runs"],
            [{"text": "spec", "link": "https://example.com/spec"}],
        )

    def test_markdown_flattens_the_header_but_keeps_one_row(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            blocks = _parse_docx_blocks(workspace)
        table = next(block for block in blocks if block.type.value == "table")
        lines = table.content.splitlines()

        self.assertEqual(len(lines), 2 + 2)
        self.assertEqual(lines[0], "| Part | Part | Qty |")
        self.assertEqual(lines[1], "| --- | --- | --- |")


class WordTableProjectionConsistencyTests(unittest.TestCase):
    """Reader, IR, records and knowledge units read the same grid."""

    def _run_document(self, client: TestClient, document_path, doc_id: str) -> None:
        created = client.post(
            "/v1/parse/jobs",
            json={
                "doc_id": doc_id,
                "file_path": str(document_path),
                "media_type": DOCX_MEDIA_TYPE,
                "options": {"profile": "table-heavy"},
            },
        )
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["job_id"]
        for _ in range(40):
            current = client.get(f"/v1/parse/jobs/{job_id}")
            self.assertEqual(current.status_code, 200)
            if current.json()["state"] == ParseJobState.DONE.value:
                return
            time.sleep(0.05)
        self.fail("parse job did not finish")

    def test_structured_projection_exposes_the_merge(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx_with_body(
                "grid.docx", MERGED_TABLE_BODY, hyperlinks=HYPERLINKS
            )
            with TestClient(create_app(workspace.config_path)) as client:
                self._run_document(client, document_path, "doc-grid")
                payload = client.get(
                    "/v1/parse/documents/doc-grid", params={"projection": "structured"}
                ).json()

        self.assertEqual(len(payload["tables"]), 1)
        table = payload["tables"][0]
        cells = {
            (cell["row_index"], cell["col_index"]): cell for cell in table["cells"]
        }
        self.assertEqual(table["header_rows"], 1)
        self.assertTrue(cells[(0, 0)]["merged"])
        self.assertEqual(cells[(0, 0)]["col_span"], 2)
        self.assertEqual(cells[(0, 1)]["continuation"], True)
        self.assertEqual(cells[(0, 1)]["origin"], "0.0")
        self.assertEqual(cells[(1, 0)]["row_span"], 2)
        self.assertEqual(
            sorted(merge["kind"] for merge in table["merged_cells"]),
            ["col_span", "row_span"],
        )

    def test_records_use_the_merged_header_for_every_column_it_covers(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx_with_body(
                "grid.docx", MERGED_TABLE_BODY, hyperlinks=HYPERLINKS
            )
            with TestClient(create_app(workspace.config_path)) as client:
                self._run_document(client, document_path, "doc-records")
                payload = client.get(
                    "/v1/parse/documents/doc-records/records",
                    params={"limit": "10"},
                ).json()

        headers = [record["fields"] for record in payload["items"]]
        self.assertEqual(len(headers), 2)
        # The merged header governs both columns it spans, so neither is reported
        # as an unnamed column: the second one is derived from the same header
        # rather than from a positional fallback.
        self.assertEqual(headers[0], {"Part": "Bolt", "Part_2": "M2", "Qty": "spec"})
        self.assertEqual(headers[1], {"Part": "Bolt", "Part_2": "M10", "Qty": "3"})

    def test_merged_table_payloads_match_the_frozen_schemas(self) -> None:
        from jsonschema import Draft202012Validator

        from parsecore.payload_schemas import payload_schema

        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx_with_body(
                "grid.docx", MERGED_TABLE_BODY, hyperlinks=HYPERLINKS
            )
            with TestClient(create_app(workspace.config_path)) as client:
                self._run_document(client, document_path, "doc-schema")
                ir = client.get(
                    "/v1/parse/documents/doc-schema", params={"projection": "ir"}
                ).json()
                reader = client.get("/v1/parse/documents/doc-schema/reader").json()

        Draft202012Validator(payload_schema("document-ir")).validate(ir)
        Draft202012Validator(payload_schema("document-reader")).validate(reader)

    def test_reader_ir_and_knowledge_units_share_one_table_text(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx_with_body(
                "grid.docx", MERGED_TABLE_BODY, hyperlinks=HYPERLINKS
            )
            with TestClient(create_app(workspace.config_path)) as client:
                self._run_document(client, document_path, "doc-views")
                reader = client.get("/v1/parse/documents/doc-views/reader").json()
                ir = client.get(
                    "/v1/parse/documents/doc-views", params={"projection": "ir"}
                ).json()

        rendered = "| Part | Part | Qty |\n| --- | --- | --- |\n| Bolt | M2 | spec |\n| Bolt | M10 | 3 |"
        ir_table = ir["tables"][0]
        ir_unit_texts = [
            unit["text"] for unit in ir["knowledge_units"] if unit.get("source_table_ids")
        ]
        self.assertTrue(ir_unit_texts)
        self.assertIn(rendered, "\n".join(ir_unit_texts))
        # The IR cell keeps the span instead of a second copy of the value.
        ir_cells = {
            (cell["row_index"], cell["col_index"]): cell for cell in ir_table["cells"]
        }
        self.assertEqual(ir_cells[(0, 0)]["col_span"], 2)
        self.assertEqual(ir_cells[(1, 0)]["row_span"], 2)

        table_blocks = [block for block in reader["blocks"] if block["type"] == "table"]
        self.assertEqual(len(table_blocks), 1)
        reader_table = table_blocks[0]["table"]
        # Reader shows the same table record as the structured projection, so the
        # merge and the header association travel with it.
        self.assertEqual(
            sorted(merge["kind"] for merge in reader_table["merged_cells"]),
            ["col_span", "row_span"],
        )
        self.assertEqual(reader_table["header_values"], ["Part", "Part", "Qty"])
        reader_unit_texts = [
            str(unit.get("text") or "")
            for unit in table_blocks[0]["knowledge_units"]
        ]
        self.assertTrue(reader_unit_texts)
        self.assertIn(rendered, "\n".join(reader_unit_texts))


if __name__ == "__main__":
    unittest.main()
