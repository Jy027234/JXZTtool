from __future__ import annotations

import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import Chunk, ParseJobState, ParseRequest, SemanticRole
from parsecore.stubs import FakeEmbeddingProvider
from tests.support import TemporaryWorkspace


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


PDF_SAMPLE_CONFIG = """
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
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
""".strip()


EMBEDDING_SAMPLE_CONFIG = """
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

[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


class ParseRuntimeTests(unittest.TestCase):
    def test_describe_returns_registered_parsers(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        self.assertEqual(description["project"], "test-parsecore")
        self.assertEqual(description["parsers"], ["docx-native"])

    def test_submit_finishes_with_done_state(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Engine Manual", "Inspection procedure"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-001",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        self.assertGreaterEqual(len(outcome.blocks), 2)
        self.assertEqual(len(outcome.chunks), len(outcome.blocks))
        self.assertEqual(outcome.blocks[1].content, "Engine Manual")
        self.assertEqual(outcome.blocks[0].metadata["semantic_role"], SemanticRole.TITLE.value)
        self.assertEqual(outcome.chunks[0].semantic_role, SemanticRole.TITLE.value)
        self.assertEqual(outcome.chunks[1].semantic_role, SemanticRole.PARAGRAPH.value)

    def test_submit_persists_job_and_document_snapshot(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("manual.docx", ["Revision A", "Replace filter"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-002",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            rebuilt = build_runtime(workspace.config_path)
            job = rebuilt.get_job(job_id=outcome.job.job_id)
            document = rebuilt.get_document(doc_id="doc-002")

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.doc_id, "doc-002")
        self.assertEqual(job.state, ParseJobState.DONE)
        self.assertEqual(document["job"].job_id, outcome.job.job_id)
        self.assertEqual(len(document["blocks"]), len(outcome.blocks))
        self.assertEqual(len(document["chunks"]), len(outcome.chunks))

    def test_retry_latest_reuses_last_request(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("revision.docx", ["Task Card", "Install panel"])
            runtime = build_runtime(workspace.config_path)
            first = runtime.submit(
                ParseRequest(
                    doc_id="doc-003",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            second = runtime.retry_latest(doc_id="doc-003")

        self.assertNotEqual(first.job.job_id, second.job.job_id)
        self.assertEqual(second.job.doc_id, "doc-003")
        self.assertEqual(second.job.file_path, str(document_path))

    def test_pdf_submit_splits_page_text_into_multiple_blocks(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class FakeReader:
            def __init__(self, file_path: str) -> None:
                self.pages = [
                    FakePage("Heading line\nDetail line\n\nStep A\nStep B"),
                    FakePage("Final note"),
                ]

        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_text_file("sample.pdf", "placeholder")
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.parsers._load_pdf_reader", return_value=FakeReader):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="pdf-001",
                        file_path=str(document_path),
                        media_type="application/pdf",
                    )
                )

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        self.assertEqual(len(outcome.blocks), 4)
        self.assertEqual(outcome.blocks[1].content, "Heading line\nDetail line")
        self.assertEqual(outcome.blocks[1].metadata["page"], 1)
        self.assertEqual(outcome.blocks[2].content, "Step A\nStep B")
        self.assertEqual(outcome.blocks[2].metadata["page_position"], 2)
        self.assertEqual(outcome.blocks[3].metadata["page"], 2)
        self.assertEqual(outcome.blocks[1].metadata["semantic_role"], SemanticRole.PARAGRAPH.value)

    def test_pdf_submit_tags_toc_entries_with_semantic_role(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class FakeReader:
            def __init__(self, file_path: str) -> None:
                self.pages = [
                    FakePage(
                        "TABLE OF CONTENTS\n"
                        "A .................. 1\n"
                        "B .................. 2\n"
                        "C .................. 3"
                    )
                ]

        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_text_file("sample.pdf", "placeholder")
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.parsers._load_pdf_reader", return_value=FakeReader):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="pdf-002",
                        file_path=str(document_path),
                        media_type="application/pdf",
                    )
                )

        toc_roles = [
            block.metadata["semantic_role"]
            for block in outcome.blocks[1:]
        ]
        self.assertTrue(toc_roles)
        self.assertTrue(all(role == SemanticRole.TOC_ENTRY.value for role in toc_roles))
        self.assertTrue(all(chunk.semantic_role == SemanticRole.TOC_ENTRY.value for chunk in outcome.chunks[1:]))

    def test_submit_applies_embedding_provider_when_enabled(self) -> None:
        with TemporaryWorkspace(EMBEDDING_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("embed.docx", ["Line 1", "Line 2"])
            with patch(
                "parsecore.bootstrap.build_embedding_provider",
                return_value=FakeEmbeddingProvider(),
            ):
                runtime = build_runtime(workspace.config_path)
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-embed",
                        file_path=str(document_path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )

        self.assertTrue(all(chunk.embedding is not None for chunk in outcome.chunks))
        self.assertEqual(outcome.chunks[0].embedding, (1.0, float(len(outcome.chunks[0].text))))

    def test_submit_rerun_chunks_only_reuses_saved_blocks(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("rerun.docx", ["Base text", "Second line"])
            runtime = build_runtime(workspace.config_path)
            first = runtime.submit(
                ParseRequest(
                    doc_id="doc-rerun",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            rerun = runtime.submit(
                ParseRequest(
                    doc_id="doc-rerun",
                    file_path=str(workspace.root / "missing.docx"),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"mode": "rerun_chunks_only"},
                )
            )

        self.assertEqual(rerun.job.state, ParseJobState.DONE)
        self.assertEqual(
            [block.content for block in rerun.blocks],
            [block.content for block in first.blocks],
        )
        self.assertEqual(
            [chunk.text for chunk in rerun.chunks],
            [chunk.text for chunk in first.chunks],
        )

    def test_search_document_weights_and_filters_by_semantic_role(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-search",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_chunks(
                doc_id="doc-search",
                chunks=[
                    Chunk(
                        chunk_id="title-hit",
                        doc_id="doc-search",
                        block_ids=("blk-1",),
                        text="Hydraulic pressure warning summary",
                        semantic_role=SemanticRole.TITLE.value,
                    ),
                    Chunk(
                        chunk_id="toc-hit",
                        doc_id="doc-search",
                        block_ids=("blk-2",),
                        text="Hydraulic pressure warning procedures",
                        semantic_role=SemanticRole.TOC_ENTRY.value,
                    ),
                    Chunk(
                        chunk_id="warning-hit",
                        doc_id="doc-search",
                        block_ids=("blk-3",),
                        text="WARNING: Hydraulic pressure warning before maintenance.",
                        semantic_role=SemanticRole.WARNING.value,
                    ),
                ],
            )

            hits = runtime.search_document(doc_id="doc-search", query="hydraulic pressure warning")
            warning_only = runtime.search_document(
                doc_id="doc-search",
                query="hydraulic pressure warning",
                semantic_roles=[SemanticRole.WARNING.value],
            )

        self.assertEqual(hits[0].semantic_role, SemanticRole.TITLE.value)
        self.assertEqual(hits[-1].semantic_role, SemanticRole.TOC_ENTRY.value)
        self.assertEqual(len(warning_only), 1)
        self.assertEqual(warning_only[0].semantic_role, SemanticRole.WARNING.value)

    def test_search_document_uses_vector_priority_with_keyword_fallback(self) -> None:
        class QueryEmbeddingProvider:
            def embed(self, *, doc_id: str, chunks):
                from dataclasses import replace

                return tuple(
                    replace(chunk, embedding=(1.0, 0.0)) for chunk in chunks
                )

        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search-hybrid.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.embedding_provider = QueryEmbeddingProvider()
            runtime.submit(
                ParseRequest(
                    doc_id="doc-hybrid",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_chunks(
                doc_id="doc-hybrid",
                chunks=[
                    Chunk(
                        chunk_id="keyword-strong",
                        doc_id="doc-hybrid",
                        block_ids=("blk-1",),
                        text="Hydraulic pressure warning procedures and checklist",
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(0.0, 1.0),
                    ),
                    Chunk(
                        chunk_id="vector-strong",
                        doc_id="doc-hybrid",
                        block_ids=("blk-2",),
                        text="Safety bulletin summary",
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(1.0, 0.0),
                    ),
                ],
            )

            hits = runtime.search_document(
                doc_id="doc-hybrid",
                query="hydraulic pressure warning",
            )
            hits_with_mode, mode = runtime.search_document_with_mode(
                doc_id="doc-hybrid",
                query="hydraulic pressure warning",
            )

        self.assertEqual(hits[0].chunk_id, "vector-strong")
        self.assertEqual(hits[1].chunk_id, "keyword-strong")
        self.assertEqual(mode, "hybrid")
        self.assertEqual(hits_with_mode[0].chunk_id, "vector-strong")

    def test_search_document_reports_keyword_fallback_mode_when_query_embedding_unavailable(self) -> None:
        class FailingQueryEmbeddingProvider:
            def embed(self, *, doc_id: str, chunks):
                raise RuntimeError("embedding unavailable")

        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search-fallback.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.embedding_provider = FailingQueryEmbeddingProvider()
            runtime.submit(
                ParseRequest(
                    doc_id="doc-fallback",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            hits_with_mode, mode = runtime.search_document_with_mode(
                doc_id="doc-fallback",
                query="base",
            )

        self.assertEqual(mode, "keyword-fallback")
        self.assertTrue(hits_with_mode)
