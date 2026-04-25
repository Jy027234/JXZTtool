from __future__ import annotations

import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import ParseJobState, ParseRequest
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
