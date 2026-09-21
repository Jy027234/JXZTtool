"""P0-03 batch 2: run identity reaches every formal read.

P0-04's acceptance asks that the status API, Reader and RAG agree on the same
run.  This module proves the mechanism: every projection of one snapshot reports
the same ``publication`` block, and that block names the published run instead of
whatever job happened to run last.

Scope note: reads are not *pinned* to a run yet — nothing publishes by default in
the embedded flow, so requiring a published run would break existing reads.  What
is enforced here is that the served identity is stated and consistent, and that a
consumer can tell a formal version from a legacy latest-job view.
"""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, ParseRequest
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

CONFIG = """
[project]
name = "test-read-run-identity"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"

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


def _block(block_id: str, text: str, *, doc_id: str) -> Block:
    return Block(
        block_id=block_id,
        doc_id=doc_id,
        type=BlockType.PARAGRAPH,
        content=text,
        metadata={"page": 1},
    )


class PublicationReadContextTests(unittest.TestCase):
    def _seed_document(self, workspace, runtime, doc_id: str = "doc-1"):
        document_path = workspace.create_docx("spec.docx", ["Maintenance Manual"])
        return runtime.submit(
            ParseRequest(
                doc_id=doc_id,
                file_path=str(document_path),
                media_type=DOCX_MEDIA_TYPE,
            )
        )

    _publish_sequence = 0

    def _publish(
        self,
        runtime,
        *,
        doc_id: str = "doc-1",
        text: str = "formal text",
        expected_generation: int = 0,
    ):
        # A distinct id per publication: reusing a block id inside the same
        # document would be an id collision, which the store refuses.
        type(self)._publish_sequence += 1
        sequence = type(self)._publish_sequence
        return runtime.publish_run(
            doc_id=doc_id,
            artifacts=RunArtifacts(
                blocks=(_block(f"blk-{sequence}", text, doc_id=doc_id),),
                views={"pages": [{"page_number": 1, "text": text}]},
            ),
            job_id=f"job-{doc_id}-{sequence}",
            source_version=f"sha256:{text}-{sequence}",
            expected_generation=expected_generation,
        )

    def test_an_unknown_document_states_that_it_serves_no_run(self) -> None:
        # A submitted parse now publishes (TODO item 5), so "no served run" is the
        # state of a document that was never parsed.
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            snapshot = runtime.get_document(doc_id="never-parsed")

        context = snapshot["publication"]
        self.assertIsNone(context["served_run_id"])
        self.assertFalse(context["pinned"])
        self.assertEqual(context["consumer_signal"], "not_ready")
        self.assertFalse(context["latest_job_used_as_formal_version"])

    def test_published_document_states_the_served_run(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self._seed_document(workspace, runtime)
            # The seed already published generation 1; publish again to check the
            # served identity follows the newest published run.
            published = self._publish(runtime, expected_generation=1, text="second")
            snapshot = runtime.get_document(doc_id="doc-1")

        context = snapshot["publication"]
        self.assertEqual(context["served_run_id"], published["parse_run_id"])
        self.assertEqual(context["run_identity"], published["parse_run_id"])
        self.assertTrue(context["pinned"])
        self.assertEqual(context["publication_status"], "published")
        self.assertEqual(context["consumer_signal"], "published")
        self.assertEqual(context["publication_generation"], 2)

    def test_every_projection_reports_the_same_run(self) -> None:
        from parsecore.api_payloads import _document_projection

        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self._seed_document(workspace, runtime)
            published = self._publish(runtime, expected_generation=1)
            snapshot = runtime.get_document(doc_id="doc-1")

        served = {
            projection: _document_projection(snapshot, projection=projection)["publication"][
                "served_run_id"
            ]
            for projection in ("compat", "structured", "full", "ir", "coverage", "reader")
        }
        self.assertEqual(set(served.values()), {published["parse_run_id"]})

    def test_all_projection_schemas_accept_the_publication_block(self) -> None:
        from jsonschema import Draft202012Validator

        from parsecore.api_payloads import _document_projection
        from parsecore.payload_schemas import payload_schema

        sample_schema_for = {
            "ir": "document-ir",
            "coverage": "document-coverage",
            "reader": "document-reader",
        }
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self._seed_document(workspace, runtime)
            self._publish(runtime, expected_generation=1)
            snapshot = runtime.get_document(doc_id="doc-1")

            for projection, schema_name in sample_schema_for.items():
                with self.subTest(projection=projection):
                    payload = _document_projection(snapshot, projection=projection)
                    self.assertIn("publication", payload)
                    Draft202012Validator(payload_schema(schema_name)).validate(payload)

    def test_http_projections_agree(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                self._seed_document(workspace, runtime)
                published = self._publish(runtime, expected_generation=1)

                served = {}
                for path, projection in (
                    ("/v1/parse/documents/doc-1", "full"),
                    ("/v1/parse/documents/doc-1?projection=reader", "reader"),
                    ("/v1/parse/documents/doc-1/coverage", "coverage"),
                ):
                    served[projection] = client.get(path).json()["publication"]["served_run_id"]
                publication = client.get("/v1/parse/documents/doc-1/publication").json()

        self.assertEqual(set(served.values()), {published["parse_run_id"]})
        self.assertEqual(publication["active_run_id"], published["parse_run_id"])
        self.assertEqual(publication["consumer_signal"], "published")

    def test_a_newer_published_run_changes_the_served_identity(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self._seed_document(workspace, runtime)
            # The seed published generation 1; ``before`` is that run.
            before_state = runtime.publication_state(doc_id="doc-1")
            before = runtime.get_document(doc_id="doc-1")["publication"]

            second = self._publish(runtime, text="v2", expected_generation=1)
            after = runtime.get_document(doc_id="doc-1")["publication"]

        self.assertEqual(before["served_run_id"], before_state["active_run_id"])
        self.assertEqual(after["served_run_id"], second["parse_run_id"])
        self.assertEqual(after["publication_generation"], 2)


if __name__ == "__main__":
    unittest.main()
