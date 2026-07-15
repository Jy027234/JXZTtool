from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from parsecore.api_payloads import _document_projection
from parsecore.bootstrap import build_runtime
from parsecore.models import BlockType, ParseRequest, SemanticRole
from parsecore.parsers import build_parser
from tests.support import TemporaryWorkspace


PYMUPDF4LLM_RUNTIME_CONFIG = """
[project]
name = "test-pymupdf4llm"
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
id = "pymupdf4llm-local"
enabled = true
priority = 80
media_types = ["application/pdf"]
extensions = [".pdf"]
profiles = ["default"]
capabilities = ["markdown", "rag-baseline"]

[[parsers]]
name = "pymupdf4llm-local"
media_types = ["application/pdf"]
extensions = [".pdf"]
options = { page_chunks = true, detect_tables = true }
""".strip()


class PyMuPdf4LlmParserTests(unittest.TestCase):
    def test_parser_converts_page_chunks_to_blocks_and_tables(self) -> None:
        calls: list[dict[str, object]] = []

        def to_markdown(file_path: str, *, page_chunks: bool = False):
            calls.append({"file_path": file_path, "page_chunks": page_chunks})
            return [
                {
                    "metadata": {"page": 1},
                    "text": "# Section 1\n\nInspect pump.\n\n| Part | Qty |\n| --- | --- |\n| Pump | 1 |",
                },
                {
                    "metadata": {"page": 2},
                    "text": "Continue inspection.",
                },
            ]

        fake_module = SimpleNamespace(__version__="0.test", to_markdown=to_markdown)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pymupdf4llm": fake_module}):
            path = Path(tmp) / "manual.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            parser = build_parser(
                "pymupdf4llm-local",
                media_types=["application/pdf"],
                extensions=[".pdf"],
                options={},
            )
            blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-pymupdf4llm",
                        file_path=str(path),
                        media_type="application/pdf",
                    )
                )
            )

        self.assertEqual(calls[0]["page_chunks"], True)
        self.assertEqual(blocks[0].type, BlockType.TITLE)
        self.assertEqual(blocks[0].content, "manual")
        self.assertEqual(blocks[0].metadata["parser"], "pymupdf4llm-local")
        self.assertEqual(blocks[0].metadata["provider_id"], "pymupdf4llm-local")
        section = blocks[1]
        self.assertEqual(section.type, BlockType.TITLE)
        self.assertEqual(section.content, "Section 1")
        self.assertEqual(section.metadata["semantic_role"], SemanticRole.BODY_SECTION.value)
        paragraph = blocks[2]
        self.assertEqual(paragraph.type, BlockType.PARAGRAPH)
        self.assertEqual(paragraph.content, "Inspect pump.")
        table = blocks[3]
        self.assertEqual(table.type, BlockType.TABLE)
        self.assertEqual(table.metadata["semantic_role"], SemanticRole.TABLE.value)
        self.assertEqual(table.metadata["rows"], 2)
        self.assertEqual(table.metadata["cols"], 2)
        self.assertEqual(table.metadata["cells"], [["Part", "Qty"], ["Pump", "1"]])
        self.assertEqual(table.metadata["table_index"], 1)
        self.assertEqual(blocks[4].metadata["page"], 2)
        self.assertEqual(blocks[4].metadata["source_kind"], "pymupdf4llm_markdown")

    def test_parser_falls_back_to_single_markdown_string(self) -> None:
        fake_module = SimpleNamespace(
            to_markdown=lambda _file_path, **_kwargs: "# Heading\n\nAlpha\n\fBeta"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pymupdf4llm": fake_module}):
            path = Path(tmp) / "single.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            parser = build_parser(
                "pymupdf4llm-local",
                media_types=["application/pdf"],
                extensions=[".pdf"],
                options={"detect_tables": False},
            )
            blocks = tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-single",
                        file_path=str(path),
                        media_type="application/pdf",
                    )
                )
            )

        self.assertEqual([block.metadata["page"] for block in blocks[1:]], [1, 1, 2])
        self.assertEqual([block.content for block in blocks[1:]], ["Heading", "Alpha", "Beta"])

    def test_parser_passes_explicit_provider_tuning_options_without_changing_defaults(self) -> None:
        calls: list[dict[str, object]] = []

        def to_markdown(
            _file_path: str,
            *,
            page_chunks: bool = False,
            ignore_graphics: bool = False,
            graphics_limit: int | None = None,
        ):
            calls.append({
                "page_chunks": page_chunks,
                "ignore_graphics": ignore_graphics,
                "graphics_limit": graphics_limit,
            })
            return [{"metadata": {"page": 1}, "text": "Tuning probe"}]

        fake_module = SimpleNamespace(__version__="0.tuning", to_markdown=to_markdown)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pymupdf4llm": fake_module}):
            path = Path(tmp) / "tuning.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            parser = build_parser(
                "pymupdf4llm-local",
                media_types=["application/pdf"],
                extensions=[".pdf"],
                options={"ignore_graphics": True, "graphics_limit": 500},
            )
            tuple(
                parser.parse(
                    ParseRequest(
                        doc_id="doc-tuning",
                        file_path=str(path),
                        media_type="application/pdf",
                    )
                )
            )

        self.assertEqual(
            calls,
            [{"page_chunks": True, "ignore_graphics": True, "graphics_limit": 500}],
        )

    def test_missing_dependency_raises_clear_error(self) -> None:
        parser = build_parser(
            "pymupdf4llm-local",
            media_types=["application/pdf"],
            extensions=[".pdf"],
            options={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            with patch("parsecore.parsers.importlib.import_module", side_effect=ImportError("missing")):
                with self.assertRaisesRegex(RuntimeError, "pymupdf4llm is required"):
                    parser.parse(
                        ParseRequest(
                            doc_id="doc-missing",
                            file_path=str(path),
                            media_type="application/pdf",
                        )
                    )

    def test_runtime_can_use_pymupdf4llm_parser_when_explicitly_configured(self) -> None:
        fake_module = SimpleNamespace(
            __version__="0.runtime",
            to_markdown=lambda _file_path, **_kwargs: [
                {"metadata": {"page": 1}, "text": "# Manual\n\nRuntime paragraph."}
            ],
        )
        with TemporaryWorkspace(PYMUPDF4LLM_RUNTIME_CONFIG) as workspace:
            assert workspace.root is not None
            path = workspace.root / "runtime.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            with patch.dict(sys.modules, {"pymupdf4llm": fake_module}):
                runtime = build_runtime(workspace.config_path)
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-runtime-pymupdf4llm",
                        file_path=str(path),
                        media_type="application/pdf",
                    )
                )
                snapshot = runtime.get_document(doc_id="doc-runtime-pymupdf4llm")

        self.assertEqual(outcome.blocks[1].metadata["parser"], "pymupdf4llm-local")
        self.assertEqual(outcome.blocks[1].metadata["provider_id"], "pymupdf4llm-local")
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
        self.assertEqual(ir["providers"][0]["provider_id"], "pymupdf4llm-local")
        self.assertEqual(ir["providers"][0]["provider_version"], "0.runtime")
        self.assertEqual(ir["providers"][0]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(ir["blocks"][1]["provenance"]["provider_version"], "0.runtime")
        self.assertEqual(ir["blocks"][1]["provenance"]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(ir["provider_registry"]["local_parsers"][0]["id"], "pymupdf4llm-local")


if __name__ == "__main__":
    unittest.main()
