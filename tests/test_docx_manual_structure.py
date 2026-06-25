from __future__ import annotations

import unittest

from parsecore.bootstrap import build_runtime
from parsecore.models import BlockType, ParseRequest, SemanticRole
from tests.support import TemporaryWorkspace, build_docx_paragraph, build_docx_table


SAMPLE_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


class DocxManualStructureTests(unittest.TestCase):
    def test_docx_parser_classifies_manual_roles_and_normalizes_titles(self) -> None:
        body_xml = "".join(
            [
                build_docx_paragraph("目录"),
                build_docx_paragraph("7.1维修许可证的申请和管理7-1"),
                build_docx_paragraph("有效页清单"),
                build_docx_table([["页码", "版次"], ["7-1", "R5TR1"]]),
                build_docx_paragraph("7.1维修许可证的申请和管理"),
                build_docx_paragraph("维修许可证应按规定申请和管理。"),
                build_docx_paragraph("2-3"),
            ]
        )
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            document_path = workspace.create_docx_with_body("manual.docx", body_xml)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-manual-roles",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

        self.assertEqual(outcome.blocks[1].type, BlockType.TITLE)
        self.assertEqual(outcome.blocks[1].metadata["semantic_role"], SemanticRole.FRONT_MATTER.value)
        self.assertEqual(outcome.blocks[2].metadata["semantic_role"], SemanticRole.TOC_ENTRY.value)
        self.assertEqual(outcome.blocks[2].content, "7.1 维修许可证的申请和管理")
        self.assertEqual(outcome.blocks[2].metadata["logical_page_label"], "7-1")
        self.assertEqual(outcome.blocks[4].type, BlockType.TABLE)
        self.assertEqual(outcome.blocks[4].metadata["semantic_role"], SemanticRole.LEP_ENTRY.value)
        self.assertEqual(outcome.blocks[4].metadata["table_type"], "effective_page_list")
        self.assertEqual(outcome.blocks[5].type, BlockType.TITLE)
        self.assertEqual(outcome.blocks[5].metadata["semantic_role"], SemanticRole.BODY_SECTION.value)
        self.assertEqual(outcome.blocks[6].metadata["semantic_role"], SemanticRole.PARAGRAPH.value)
        self.assertEqual(outcome.blocks[7].metadata["semantic_role"], SemanticRole.PAGE_REF_CELL.value)

    def test_docx_chunker_aggregates_sections_and_excludes_artifacts(self) -> None:
        body_xml = "".join(
            [
                build_docx_paragraph("目录"),
                build_docx_paragraph("7.1维修许可证的申请和管理7-1"),
                build_docx_paragraph("7.1维修许可证的申请和管理"),
                build_docx_paragraph("维修许可证应按规定申请和管理。"),
                build_docx_table([["项目", "要求"], ["申请", "提交资料"]]),
                build_docx_paragraph("2-3"),
            ]
        )
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            document_path = workspace.create_docx_with_body("manual-chunk.docx", body_xml)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-manual-chunk",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            snapshot = runtime.get_document(doc_id="doc-manual-chunk")

        chunk_roles = [chunk.semantic_role for chunk in outcome.chunks]
        self.assertEqual(chunk_roles[0], SemanticRole.TITLE.value)
        self.assertIn(SemanticRole.TOC_ENTRY.value, chunk_roles)
        self.assertIn(SemanticRole.BODY_SECTION.value, chunk_roles)
        body_chunk = next(chunk for chunk in outcome.chunks if chunk.semantic_role == SemanticRole.BODY_SECTION.value)
        self.assertIn("7.1 维修许可证的申请和管理", body_chunk.text)
        self.assertIn("维修许可证应按规定申请和管理。", body_chunk.text)
        self.assertIn("| 项目 | 要求 |", body_chunk.text)
        self.assertNotIn("2-3", body_chunk.text)
        rag_coverage = snapshot["index_manifest"]["rag_coverage"]
        units_by_role = {}
        for unit in rag_coverage["units"]:
            units_by_role.setdefault(unit["semantic_role"], []).append(unit)
        self.assertEqual(units_by_role[SemanticRole.PAGE_REF_CELL.value][0]["skip_reason"], "semantic_role:page_ref_cell")
        self.assertFalse(units_by_role[SemanticRole.PAGE_REF_CELL.value][0]["chunk_ids"])
        body_unit_chunk_ids = {
            chunk_id
            for unit in units_by_role[SemanticRole.BODY_SECTION.value] + units_by_role[SemanticRole.PARAGRAPH.value]
            for chunk_id in unit["chunk_ids"]
        }
        self.assertIn(body_chunk.chunk_id, body_unit_chunk_ids)

    def test_document_snapshot_includes_manual_anatomy_and_structure_quality(self) -> None:
        body_xml = "".join(
            [
                build_docx_paragraph("目录"),
                build_docx_paragraph("7.1维修许可证的申请和管理7-1"),
                build_docx_paragraph("修订记录"),
                build_docx_table([["版次", "日期"], ["R5TR1", "2022-09-30"]]),
                build_docx_paragraph("7.1维修许可证的申请和管理"),
                build_docx_paragraph("维修许可证应按规定申请和管理。"),
            ]
        )
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            document_path = workspace.create_docx_with_body("manual-snapshot.docx", body_xml)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-manual-snapshot",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            snapshot = runtime.get_document(doc_id="doc-manual-snapshot")

        manifest = snapshot["index_manifest"]
        self.assertIn("manual_anatomy", manifest)
        self.assertIn("structure_quality", manifest)
        self.assertEqual(manifest["manual_anatomy"]["chapter_tree"][0]["text"], "7.1 维修许可证的申请和管理")
        self.assertGreater(manifest["structure_quality"]["chapter_coverage_rate"], 0.0)
        self.assertGreater(manifest["structure_quality"]["heading_body_binding_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
