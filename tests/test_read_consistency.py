"""Reproduce the documented failure: OLD published, NEW rerun fails, read mixes them.

Documented evidence (docs TODO §2):
  "先发布 OLD，随后解析 NEW 并使分块失败，读取返回 NEW 正文，却仍标注 OLD 的
   published/pinned 身份。"

This test asserts the required behaviour, so it fails while the bug exists: after
a failed NEW rerun the formal read must serve OLD content with OLD identity.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, ParseRequest
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

CONFIG = """
[project]
name = "test-legacy-read-consistency"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_attempts = 1

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


class PublishedResultSurvivesFailedRerunTests(unittest.TestCase):
    def test_failed_rerun_keeps_the_published_content_and_identity(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)

            # 1. Publish OLD.
            published = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="old-b1",
                            doc_id="doc-1",
                            type=BlockType.PARAGRAPH,
                            content="OLD content",
                            metadata={"page": 1},
                        ),
                    ),
                    views={"pages": [{"page_number": 1, "text": "OLD content"}]},
                ),
                job_id="job-old",
                source_version="sha256:old",
                validation_status="passed",
            )
            self.assertTrue(published["published"])

            # 2. Rerun with NEW content, then make chunking fail.
            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch.object(
                runtime,
                "_load_chunks_for_request",
                side_effect=RuntimeError("injected chunk failure"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.submit(
                        ParseRequest(
                            doc_id="doc-1",
                            file_path=str(document_path),
                            media_type=DOCX_MEDIA_TYPE,
                        )
                    )

            # 3. The formal read must still be OLD, content and identity together.
            snapshot = runtime.get_document(doc_id="doc-1")
            context = snapshot["publication"]

        contents = [block.content for block in snapshot["blocks"]]
        self.assertIn("OLD content", contents, f"formal read served {contents!r}")
        self.assertNotIn("NEW content", contents)
        self.assertTrue(context["pinned"])
        self.assertEqual(context["served_run_id"], published["parse_run_id"])
        self.assertEqual(context["consumer_signal"], "published")

    def test_failed_first_run_is_never_reported_as_formal(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            document_path = workspace.create_docx("only.docx", ["ONLY content"])
            with patch.object(
                runtime,
                "_load_chunks_for_request",
                side_effect=RuntimeError("injected chunk failure"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.submit(
                        ParseRequest(
                            doc_id="doc-1",
                            file_path=str(document_path),
                            media_type=DOCX_MEDIA_TYPE,
                        )
                    )
            context = runtime.get_document(doc_id="doc-1")["publication"]

        self.assertFalse(context["pinned"])
        self.assertIsNone(context["served_run_id"])
        self.assertEqual(context["consumer_signal"], "not_ready")


if __name__ == "__main__":
    unittest.main()
