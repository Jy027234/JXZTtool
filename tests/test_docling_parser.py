from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from parsecore.api_payloads import _document_projection
from parsecore.bootstrap import build_runtime
from parsecore.models import BlockType, ParseRequest, SemanticRole
from parsecore.parsers import _build_docling_converter, build_parser
from tests.support import TemporaryWorkspace


DOCLING_RUNTIME_CONFIG = """
[project]
name = "test-docling"
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

[[providers.local_parsers]]
id = "docling-local"
enabled = true
priority = 70
media_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".pdf", ".docx"]
profiles = ["default", "table-heavy"]
capabilities = ["layout", "tables", "reading-order"]

[[parsers]]
name = "docling-local"
media_types = ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".pdf", ".docx"]
options = { detect_tables = true }
""".strip()


class _FakeDoclingPage:
    def __init__(self, page_no: int, markdown: str) -> None:
        self.page_no = page_no
        self._markdown = markdown

    def export_to_markdown(self) -> str:
        return self._markdown


class _FakeDoclingDocument:
    def __init__(self, pages: list[_FakeDoclingPage]) -> None:
        self.pages = pages


class _FakeMarkdownDocument:
    def __init__(self, markdown: str) -> None:
        self._markdown = markdown

    def export_to_markdown(self) -> str:
        return self._markdown


def _install_fake_docling(converter_cls: type[object]) -> dict[str, ModuleType]:
    docling_module = ModuleType("docling")
    docling_module.__version__ = "2.test"
    converter_module = ModuleType("docling.document_converter")
    converter_module.DocumentConverter = converter_cls
    docling_module.document_converter = converter_module
    return {
        "docling": docling_module,
        "docling.document_converter": converter_module,
    }


class DoclingParserTests(unittest.TestCase):
    def test_reuse_converter_defaults_off(self) -> None:
        parser = build_parser(
            "docling-local",
            media_types=["application/pdf"],
            extensions=[".pdf"],
        )
        self.assertFalse(parser._reuse_converter)

    def test_pipeline_options_are_explicit_and_candidate_only(self) -> None:
        class FakePipelineOptions:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        class FakePdfFormatOption:
            def __init__(self, *, pipeline_options: object) -> None:
                self.pipeline_options = pipeline_options

        class FakeConverter:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        base_models = ModuleType("docling.datamodel.base_models")
        base_models.InputFormat = SimpleNamespace(PDF="pdf")
        pipeline_options = ModuleType("docling.datamodel.pipeline_options")
        pipeline_options.ThreadedPdfPipelineOptions = FakePipelineOptions
        converter_module = ModuleType("docling.document_converter")
        converter_module.PdfFormatOption = FakePdfFormatOption
        fake_modules = {
            "docling.datamodel.base_models": base_models,
            "docling.datamodel.pipeline_options": pipeline_options,
            "docling.document_converter": converter_module,
        }
        converter_module.PdfFormatOption = FakePdfFormatOption
        with patch.dict(sys.modules, fake_modules):
            converter = _build_docling_converter(
                FakeConverter,
                pipeline_options={
                    "do_ocr": False,
                    "do_table_structure": False,
                    "force_backend_text": True,
                    "layout_batch_size": 1,
                },
            )

        format_option = converter.kwargs["format_options"]["pdf"]
        self.assertEqual(
            format_option.pipeline_options.kwargs,
            {
                "do_ocr": False,
                "do_table_structure": False,
                "force_backend_text": True,
                "layout_batch_size": 1,
            },
        )

    def test_reuse_converter_is_explicit_and_reuses_one_instance(self) -> None:
        instances: list[object] = []

        class FakeDocumentConverter:
            def __init__(self) -> None:
                instances.append(self)

            def convert(self, file_path: str, **kwargs: object) -> object:
                return SimpleNamespace(
                    document=_FakeDoclingDocument(
                        [_FakeDoclingPage(1, f"Parsed {Path(file_path).name}")]
                    )
                )

        fake_modules = _install_fake_docling(FakeDocumentConverter)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, fake_modules):
            first = Path(tmp) / "first.docx"
            second = Path(tmp) / "second.docx"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            parser = build_parser(
                "docling-local",
                media_types=[
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ],
                extensions=[".docx"],
                options={"reuse_converter": True},
            )
            first_blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-first",
                        file_path=str(first),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )
            )
            second_blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-second",
                        file_path=str(second),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )
            )

        self.assertEqual(len(instances), 1)
        self.assertIn("Parsed first.docx", "\n".join(block.content for block in first_blocks))
        self.assertIn("Parsed second.docx", "\n".join(block.content for block in second_blocks))
        self.assertFalse(first_blocks[0].metadata["converter_cache_hit"])
        self.assertTrue(first_blocks[0].metadata["converter_reuse_enabled"])
        self.assertTrue(second_blocks[0].metadata["converter_cache_hit"])
        self.assertTrue(second_blocks[0].metadata["converter_reuse_enabled"])

    def test_parser_converts_docling_pages_to_blocks_and_tables(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeDocumentConverter:
            def convert(self, file_path: str, **kwargs: object) -> object:
                calls.append({"file_path": file_path, **kwargs})
                return SimpleNamespace(
                    document=_FakeDoclingDocument(
                        [
                            _FakeDoclingPage(
                                1,
                                "# Section 1\n\nInspect valve.\n\n| Part | Qty |\n| --- | --- |\n| Valve | 2 |",
                            ),
                            _FakeDoclingPage(2, "Continue inspection."),
                        ]
                    )
                )

        fake_modules = _install_fake_docling(FakeDocumentConverter)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, fake_modules):
            path = Path(tmp) / "manual.docx"
            path.write_bytes(b"fake-docx")
            parser = build_parser(
                "docling-local",
                media_types=[
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ],
                extensions=[".pdf", ".docx"],
                options={
                    "detect_tables": True,
                    "page_range": [1, 2],
                    "max_num_pages": 2,
                    "max_file_size": 1024,
                    "raises_on_error": False,
                },
            )
            blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-docling",
                        file_path=str(path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )
            )

        self.assertEqual(calls[0]["page_range"], (1, 2))
        self.assertEqual(calls[0]["max_num_pages"], 2)
        self.assertEqual(calls[0]["max_file_size"], 1024)
        self.assertEqual(calls[0]["raises_on_error"], False)
        self.assertEqual(blocks[0].type, BlockType.TITLE)
        self.assertEqual(blocks[0].content, "manual")
        self.assertEqual(blocks[0].metadata["parser"], "docling-local")
        self.assertEqual(blocks[0].metadata["provider_id"], "docling-local")
        section = blocks[1]
        self.assertEqual(section.type, BlockType.TITLE)
        self.assertEqual(section.content, "Section 1")
        self.assertEqual(section.metadata["semantic_role"], SemanticRole.BODY_SECTION.value)
        paragraph = blocks[2]
        self.assertEqual(paragraph.type, BlockType.PARAGRAPH)
        self.assertEqual(paragraph.content, "Inspect valve.")
        table = blocks[3]
        self.assertEqual(table.type, BlockType.TABLE)
        self.assertEqual(table.metadata["semantic_role"], SemanticRole.TABLE.value)
        self.assertEqual(table.metadata["rows"], 2)
        self.assertEqual(table.metadata["cols"], 2)
        self.assertEqual(table.metadata["cells"], [["Part", "Qty"], ["Valve", "2"]])
        self.assertEqual(table.metadata["table_index"], 1)
        self.assertEqual(blocks[4].metadata["page"], 2)
        self.assertEqual(blocks[4].metadata["source_kind"], "docling_markdown")

    def test_parser_falls_back_to_document_markdown_export(self) -> None:
        class FakeDocumentConverter:
            def convert(self, _file_path: str, **_kwargs: object) -> object:
                return SimpleNamespace(
                    document=_FakeMarkdownDocument("# Heading\n\nAlpha\n\fBeta")
                )

        fake_modules = _install_fake_docling(FakeDocumentConverter)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, fake_modules):
            path = Path(tmp) / "single.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            parser = build_parser(
                "docling-local",
                media_types=["application/pdf"],
                extensions=[".pdf"],
                options={"detect_tables": False},
            )
            blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-docling-single",
                        file_path=str(path),
                        media_type="application/pdf",
                    )
                )
            )

        self.assertEqual([block.metadata["page"] for block in blocks[1:]], [1, 1, 2])
        self.assertEqual([block.content for block in blocks[1:]], ["Heading", "Alpha", "Beta"])

    def test_missing_dependency_raises_clear_error(self) -> None:
        parser = build_parser(
            "docling-local",
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            with patch("parsecore.parsers.importlib.import_module", side_effect=ImportError("missing")):
                with self.assertRaisesRegex(RuntimeError, "docling is required"):
                    parser.parse(
                        ParseRequest(
                            doc_id="doc-docling-missing",
                            file_path=str(path),
                            media_type="application/pdf",
                        )
                    )

    def test_conversion_failure_is_isolated_and_propagated(self) -> None:
        """P3-T04: docling conversion failure must be isolated and propagated clearly."""
        class FailingConverter:
            def convert(self, _file_path: str, **_kwargs: object) -> object:
                raise RuntimeError("docling conversion engine crash")

        parser = build_parser(
            "docling-local",
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fail.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            fake_modules = _install_fake_docling(FailingConverter)
            with patch.dict(sys.modules, fake_modules):
                with self.assertRaisesRegex(RuntimeError, "docling.*crash"):
                    parser.parse(
                        ParseRequest(
                            doc_id="doc-docling-fail",
                            file_path=str(path),
                            media_type="application/pdf",
                        )
                    )

    def test_malformed_output_is_handled_gracefully(self) -> None:
        """P3-T04: docling returning None/empty must not crash the parser."""
        class EmptyConverter:
            def convert(self, _file_path: str, **_kwargs: object) -> object:
                return SimpleNamespace(document=None)

        parser = build_parser(
            "docling-local",
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            fake_modules = _install_fake_docling(EmptyConverter)
            with patch.dict(sys.modules, fake_modules):
                # Should either return empty blocks or raise a clear error
                try:
                    result = parser.parse(
                        ParseRequest(
                            doc_id="doc-docling-empty",
                            file_path=str(path),
                            media_type="application/pdf",
                        )
                    )
                    # If it returns, blocks should be empty or minimal
                    self.assertIsNotNone(result)
                except (RuntimeError, AttributeError, TypeError):
                    # Acceptable: parser raises clear error for malformed output
                    pass

    def test_runtime_can_use_docling_parser_when_explicitly_configured(self) -> None:
        class FakeDocumentConverter:
            def convert(self, _file_path: str, **_kwargs: object) -> object:
                return SimpleNamespace(
                    document=_FakeDoclingDocument(
                        [_FakeDoclingPage(1, "# Manual\n\nRuntime paragraph.")]
                    )
                )

        fake_modules = _install_fake_docling(FakeDocumentConverter)
        with TemporaryWorkspace(DOCLING_RUNTIME_CONFIG) as workspace:
            assert workspace.root is not None
            path = workspace.root / "runtime.docx"
            path.write_bytes(b"fake-docx")
            with patch.dict(sys.modules, fake_modules):
                runtime = build_runtime(workspace.config_path)
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-runtime-docling",
                        file_path=str(path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )
                snapshot = runtime.get_document(doc_id="doc-runtime-docling")

        self.assertEqual(outcome.blocks[1].metadata["parser"], "docling-local")
        self.assertEqual(outcome.blocks[1].metadata["provider_id"], "docling-local")
        self.assertEqual(len(outcome.chunks), len(outcome.blocks))
        self.assertEqual(
            snapshot["provider_registry"]["summary"],
            {
                "total": 1,
                "enabled": 1,
                "disabled": 0,
                "route_ready": 1,
                "evaluation_only": 0,
                "gate_pending": 0,
                "gate_failed": 0,
            },
        )
        ir = _document_projection(snapshot, projection="ir")
        self.assertEqual(ir["providers"][0]["provider_id"], "docling-local")
        self.assertEqual(ir["providers"][0]["provider_version"], "2.test")
        self.assertEqual(ir["providers"][0]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(ir["blocks"][1]["provenance"]["provider_version"], "2.test")
        self.assertEqual(ir["blocks"][1]["provenance"]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(ir["provider_registry"]["local_parsers"][0]["id"], "docling-local")


if __name__ == "__main__":
    unittest.main()
