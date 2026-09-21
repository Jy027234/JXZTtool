"""P0-03 at the runtime and API level: publication state, not-ready semantics.

``tests/test_run_store.py`` proves the storage transaction.  This module proves
that the runtime and the HTTP surface report it honestly:

* a document with no published run returns an explicit ``not_ready`` signal
  instead of a document that merely looks empty;
* a backend that cannot publish atomically says so;
* ``job.state = done`` is not reported as a published formal version.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.models import Block, BlockType, Chunk, ParseRequest
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace


CONFIG = """
[project]
name = "test-run-publication"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"

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

def _block(block_id: str, text: str, *, doc_id: str) -> Block:
    return Block(
        block_id=block_id,
        doc_id=doc_id,
        type=BlockType.PARAGRAPH,
        content=text,
        metadata={"page": 1},
    )


class RuntimePublicationTests(unittest.TestCase):
    def test_a_submitted_parse_publishes_and_reads_pin_that_run(self) -> None:
        # TODO item 5: the current execution path is wired to the run store, so a
        # successful submit publishes and the read serves that run.
        from parsecore.bootstrap import build_runtime

        with TemporaryWorkspace(CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-1",
                    file_path=str(document_path),
                    media_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
            )
            state = runtime.publication_state(doc_id="doc-1")
            snapshot = runtime.get_document(doc_id="doc-1")

        self.assertEqual(outcome.job.state.value, "done")
        self.assertTrue(state["formal_version_available"])
        self.assertEqual(state["consumer_signal"], "published")
        self.assertEqual(snapshot["publication"]["served_run_id"], state["active_run_id"])
        self.assertTrue(snapshot["publication"]["pinned"])
        self.assertEqual(state["publication_generation"], 1)

    def test_publishing_through_the_runtime_switches_the_pointer(self) -> None:
        from parsecore.bootstrap import build_runtime

        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self.assertTrue(runtime.run_publication_ready)
            # A fresh document has no published version yet.
            before = runtime.publication_state(doc_id="doc-1")
            self.assertEqual(before["publication_generation"], 0)

            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(_block("b1", "published text", doc_id="doc-1"),),
                    chunks=(
                        Chunk(
                            chunk_id="c1",
                            doc_id="doc-1",
                            block_ids=("b1",),
                            text="published text",
                        ),
                    ),
                    views={
                        "pages": [{"page_number": 1, "text": "published text"}],
                        "records": [
                            {
                                "record_id": "r1",
                                "page_start": 1,
                                "fields": {"text": "published text"},
                            }
                        ],
                    },
                ),
                job_id="job-1",
                source_version="sha256:abc",
                validation_status="passed",
                validation_reason="unit",
                expected_generation=0,
            )

            self.assertTrue(result["published"])
            state = runtime.publication_state(doc_id="doc-1")
            runs = runtime.list_runs(doc_id="doc-1")
            capabilities = runtime.capability_report()

        self.assertEqual(state["consumer_signal"], "published")
        self.assertTrue(state["formal_version_available"])
        self.assertEqual(state["active_run_id"], result["parse_run_id"])
        self.assertEqual(len(runs), 1)
        publication_entry = next(
            item for item in capabilities["capabilities"] if item["name"] == "run_publication"
        )
        self.assertEqual(publication_entry["status"], "available")

    def test_a_warning_result_completes_unpublished_instead_of_failing(self) -> None:
        # A finding below the policy's publish threshold ("a signature page needs
        # review") is not a parse failure.  The job must finish, the run must stay
        # unpublished, and the document must not be reported as a formal version —
        # otherwise a healthy parse of a real 297-page manual is reported as an
        # execution error, and the quality gate cannot even measure it.
        from unittest.mock import patch

        from parsecore.bootstrap import build_runtime
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual"])
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-warning",
                        file_path=str(document_path),
                        media_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                    )
                )
            state = runtime.publication_state(doc_id="doc-warning")
            runs = runtime.list_runs(doc_id="doc-warning")

        self.assertEqual(outcome.job.state.value, "done")
        self.assertIsNone(outcome.job.failure_reason)
        self.assertFalse(state["formal_version_available"])
        # Parsed but not published: a preview exists and is named, while the
        # formal signal stays "no formal version".
        self.assertEqual(state["consumer_signal"], "preview_only")
        self.assertEqual(state["preview_run_id"], runs[0].parse_run_id)
        self.assertEqual(state["preview"]["validation_status"], "warning")
        self.assertFalse(state["preview"]["pinned"])
        self.assertFalse(state["preview"]["is_formal_version"])
        self.assertIsNone(state["active_run_id"])
        # The run and its validation report survive as an unpublished preview.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].validation_status, "warning")

    def test_a_newer_unpublished_run_is_a_preview_next_to_the_published_one(self) -> None:
        # OLD is published, NEW parses but is not publishable.  The formal version
        # must stay OLD and be marked pinned, while NEW is reported as a preview:
        # a consumer has to be able to tell the two apart.
        from unittest.mock import patch

        from parsecore.bootstrap import build_runtime
        from parsecore.publication import ValidationStatus
        from parsecore.run_store import RunArtifacts
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["NEW content"])
            runtime = build_runtime(workspace.config_path)
            published = runtime.publish_run(
                doc_id="doc-old-new",
                artifacts=RunArtifacts(
                    blocks=(_block("b1", "OLD content", doc_id="doc-old-new"),),
                    views={"pages": [{"page_number": 1, "text": "OLD content"}]},
                ),
                job_id="job-old",
                source_version="sha256:old",
                validation_status="passed",
            )
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-old-new",
                        file_path=str(document_path),
                        media_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                    )
                )
            state = runtime.publication_state(doc_id="doc-old-new")
            snapshot = runtime.get_document(doc_id="doc-old-new")

        self.assertEqual(outcome.job.state.value, "done")
        self.assertEqual(state["consumer_signal"], "published")
        self.assertEqual(state["active_run_id"], published["parse_run_id"])
        self.assertTrue(state["formal_version_available"])
        self.assertIsNotNone(state["preview_run_id"])
        self.assertNotEqual(state["preview_run_id"], published["parse_run_id"])
        self.assertEqual(state["preview"]["validation_status"], "warning")
        self.assertFalse(state["preview"]["pinned"])
        self.assertFalse(state["preview"]["is_formal_version"])
        # The formal read still serves OLD, pinned, and never the newest job.
        self.assertEqual(snapshot["publication"]["served_run_id"], published["parse_run_id"])
        self.assertTrue(snapshot["publication"]["pinned"])
        self.assertFalse(snapshot["publication"]["latest_job_used_as_formal_version"])
        self.assertIn("OLD content", "\n".join(block.content for block in snapshot["blocks"]))

    def test_a_document_with_nothing_parsed_has_no_preview(self) -> None:
        from parsecore.bootstrap import build_runtime

        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            state = runtime.publication_state(doc_id="never-parsed")

        self.assertEqual(state["consumer_signal"], "not_ready")
        self.assertIsNone(state["preview_run_id"])
        self.assertIsNone(state["preview"])

    def test_a_rejected_result_fails_the_job_and_publishes_nothing(self) -> None:
        from unittest.mock import patch

        from parsecore.bootstrap import build_runtime
        from parsecore.publication import ValidationStatus
        from parsecore.run_store import PublishError
        from parsecore.validation import ValidationReport

        rejected_report = ValidationReport(
            status=ValidationStatus.REJECTED,
            reason="placeholder_result",
            rejections=("placeholder_result",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual"])
            runtime = build_runtime(workspace.config_path)
            job = runtime.start(
                ParseRequest(
                    doc_id="doc-rejected",
                    file_path=str(document_path),
                    media_type=(
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
            )
            with patch("parsecore.runtime.validate_run", return_value=rejected_report):
                with self.assertRaises(PublishError):
                    runtime.execute(job_id=job.job_id)
            stored = runtime.job_store.get_job(job_id=job.job_id)
            state = runtime.publication_state(doc_id="doc-rejected")

        self.assertEqual(stored.state.value, "failed")
        self.assertIn("validation_rejected", str(stored.failure_reason))
        self.assertFalse(state["formal_version_available"])
        self.assertIsNone(state["active_run_id"])

    def test_rejected_publication_reports_the_reason_and_keeps_not_ready(self) -> None:
        from parsecore.bootstrap import build_runtime

        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(_block("b1", "needs review", doc_id="doc-1"),),
                    views={"pages": [{"page_number": 1, "text": "needs review"}]},
                ),
                job_id="job-1",
                validation_status="needs_review",
            )
            state = runtime.publication_state(doc_id="doc-1")

        self.assertFalse(result["published"])
        # A policy refusal is distinguishable from a broken publish: the run is
        # fine, a human decision is missing.
        self.assertEqual(result["reason"], "run_not_publishable_by_policy")
        self.assertIn("validation_needs_review", result["message"])
        self.assertEqual(result["consumer_signal"], "not_ready")
        self.assertIsNone(state["active_run_id"])
        # The run itself is the preview: named, unpinned, not a formal version.
        self.assertEqual(state["preview_run_id"], result["parse_run_id"])
        self.assertEqual(state["preview"]["run_id"], result["parse_run_id"])
        self.assertFalse(state["preview"]["pinned"])
        self.assertFalse(state["preview"]["is_formal_version"])

    def test_publish_is_rejected_when_the_backend_cannot_publish_atomically(self) -> None:
        from parsecore.bootstrap import build_runtime

        # A memory:// store has no run isolation, so the runtime must refuse to
        # claim publication instead of publishing non-atomically.  The workspace
        # substitute always injects the SQLite url, so build the config path by
        # hand here.
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "parsecore.toml"
            config_path.write_text(
                CONFIG.replace('database_url = "__DB_URL__"', 'database_url = "memory://"'),
                encoding="utf-8",
            )
            runtime = build_runtime(config_path)
            ready = runtime.run_publication_ready
            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(_block("b1", "x", doc_id="doc-1"),),
                    views={"pages": [{"page_number": 1, "text": "x"}]},
                ),
            )
            capabilities = runtime.capability_report()

        self.assertFalse(ready)
        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "run_store_not_ready")
        entry = next(
            item for item in capabilities["capabilities"] if item["name"] == "run_publication"
        )
        self.assertEqual(entry["status"], "degraded")
        self.assertEqual(entry["reason"], "run_store_capabilities_incomplete")


class PublicationEndpointTests(unittest.TestCase):
    def test_publication_endpoint_reports_not_ready_without_a_published_run(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/v1/parse/documents/doc-1/publication")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], "2026-09-document-publication-v1")
        self.assertEqual(body["publication_status"], "unpublished")
        self.assertFalse(body["formal_version_available"])
        self.assertEqual(body["consumer_signal"], "not_ready")
        self.assertEqual(body["publication_generation"], 0)
        self.assertIsNone(body["preview_run_id"])
        self.assertIsNone(body["preview"])
        self.assertEqual(body["legacy"]["legacy_formal_signal_sufficient"], False)
        # The endpoint must never suggest that the newest job is the formal version.
        self.assertFalse(body["diagnostics"]["latest_job_used_for_publication"])

    def test_publication_endpoint_reports_published_state(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                published = runtime.publish_run(
                    doc_id="doc-1",
                    artifacts=RunArtifacts(
                        blocks=(_block("b1", "formal", doc_id="doc-1"),),
                        views={"pages": [{"page_number": 1, "text": "formal"}]},
                    ),
                    job_id="job-1",
                    source_version="sha256:formal",
                    validation_status="passed",
                )
                response = client.get("/v1/parse/documents/doc-1/publication")

        self.assertTrue(published["published"])
        body = response.json()
        self.assertEqual(body["publication_status"], "published")
        self.assertTrue(body["formal_version_available"])
        self.assertEqual(body["consumer_signal"], "published")
        self.assertEqual(body["active_run_id"], published["parse_run_id"])
        self.assertEqual(body["publication_generation"], 1)
        self.assertEqual(body["validation"]["policy_version"], "2026-09-validation-policy-v1")

    def test_publication_payload_matches_the_frozen_schema(self) -> None:
        from jsonschema import Draft202012Validator

        from parsecore.payload_schemas import payload_schema

        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                client.app.state.runtime.publish_run(
                    doc_id="doc-1",
                    artifacts=RunArtifacts(
                        blocks=(_block("b1", "formal", doc_id="doc-1"),),
                        views={"pages": [{"page_number": 1, "text": "formal"}]},
                    ),
                    job_id="job-1",
                    source_version="sha256:formal",
                    validation_status="passed",
                )
                body = client.get("/v1/parse/documents/doc-1/publication").json()

        Draft202012Validator(payload_schema("document-publication")).validate(body)


if __name__ == "__main__":
    unittest.main()
