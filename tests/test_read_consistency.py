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

    def test_the_read_names_the_job_behind_the_run_it_served(self) -> None:
        # Regressed case: the published run held OLD content while a newer
        # unpublished run existed, so the snapshot reported the *newest* job next
        # to OLD content.  Every projection and the export payload derive the
        # version identity from `snapshot["job"]`, so the read has to name the job
        # behind the run it served, and the newer run appears as the preview.
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)

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

            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                newer = runtime.submit(
                    ParseRequest(
                        doc_id="doc-1",
                        file_path=str(document_path),
                        media_type=DOCX_MEDIA_TYPE,
                    )
                )

            snapshot = runtime.get_document(doc_id="doc-1")
            state = runtime.publication_state(doc_id="doc-1")
            records = runtime.get_document_records_projection(doc_id="doc-1", limit=5)
            runs = runtime.list_runs(doc_id="doc-1")

        self.assertEqual(newer.job.state.value, "done")
        self.assertNotEqual(newer.job.job_id, "job-old")
        # The served version's identity, not the newest job's.
        self.assertEqual(snapshot["job"].job_id, "job-old")
        self.assertEqual(snapshot["publication"]["served_run_id"], published["parse_run_id"])
        self.assertTrue(snapshot["publication"]["pinned"])
        # The newer run is reported as the preview, with its own identity.
        preview_runs = [run for run in runs if run.parse_run_id == state["preview_run_id"]]
        self.assertTrue(preview_runs)
        self.assertEqual(preview_runs[0].job_id, newer.job.job_id)
        self.assertNotEqual(state["preview_run_id"], published["parse_run_id"])
        self.assertIn("OLD content", [block.content for block in snapshot["blocks"]])
        # An export states the same version it served.
        self.assertEqual((records or {}).get("parse_run_id"), "job-old")

    def test_the_projection_payload_reports_the_served_identity(self) -> None:
        from parsecore.api_payloads import _document_projection
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
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
            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                runtime.submit(
                    ParseRequest(
                        doc_id="doc-1",
                        file_path=str(document_path),
                        media_type=DOCX_MEDIA_TYPE,
                    )
                )
            snapshot = runtime.get_document(doc_id="doc-1")
            payload = _document_projection(snapshot, projection="structured")

        self.assertEqual(payload["parse_run_id"], "job-old")
        self.assertEqual(payload["publication"]["served_run_id"], published["parse_run_id"])
        self.assertFalse(payload["publication"]["latest_job_used_as_formal_version"])

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
