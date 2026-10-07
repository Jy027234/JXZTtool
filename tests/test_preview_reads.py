from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.models import Block, BlockType
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace


CONFIG = """
[project]
name = "test-run-preview-reads"
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


def _artifacts(doc_id: str, text: str, suffix: str) -> RunArtifacts:
    block_id = f"blk-{suffix}"
    return RunArtifacts(
        blocks=(
            Block(
                block_id=block_id,
                doc_id=doc_id,
                type=BlockType.PARAGRAPH,
                content=text,
                metadata={"page": 1},
            ),
        ),
        views={
            "pages": [{"page_number": 1, "text": text}],
            "lines": [{"line_id": f"{block_id}:line:1", "page_number": 1, "text": text}],
            "records": [
                {
                    "record_id": f"{block_id}:text:r1:l1",
                    "block_id": block_id,
                    "page_start": 1,
                    "page_end": 1,
                    "fields": {"text": text},
                }
            ],
        },
    )


class PreviewReadTests(unittest.TestCase):
    def test_preview_is_readable_without_a_formal_version_and_never_changes_default_get(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                preview = runtime.publish_run(
                    doc_id="doc-preview-only",
                    artifacts=_artifacts("doc-preview-only", "NEW preview body", "new-only"),
                    job_id="job-new-only",
                    source_version="sha256:new-only",
                    validation_status="warning",
                )
                run_id = preview["parse_run_id"]
                sdk_snapshot = runtime.get_document_preview(
                    doc_id="doc-preview-only",
                    parse_run_id=run_id,
                    document_view_types=("pages",),
                )
                self.assertEqual(sdk_snapshot["publication"]["served_run_id"], run_id)
                self.assertFalse(sdk_snapshot["publication"]["is_formal_version"])
                self.assertEqual(
                    sdk_snapshot["document_views"]["pages"][0]["text"], "NEW preview body"
                )

                for projection in ("full", "structured", "reader"):
                    response = client.get(
                        "/v1/parse/documents/doc-preview-only/preview",
                        params={"run_id": run_id, "projection": projection},
                    )
                    self.assertEqual(response.status_code, 200, projection)
                    payload = response.json()
                    self.assertIn("NEW preview body", json.dumps(payload, ensure_ascii=False))
                    self.assertEqual(payload["publication"]["served_run_id"], run_id)
                    self.assertFalse(payload["publication"]["pinned"])
                    self.assertFalse(payload["publication"]["is_formal_version"])

                formal = client.get("/v1/parse/documents/doc-preview-only")

        self.assertFalse(preview["published"])
        self.assertEqual(formal.status_code, 404)

    def test_old_and_new_runs_stay_distinct_until_review_then_formal_reads_switch(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                old = runtime.publish_run(
                    doc_id="doc-old-new",
                    tenant_id="tenant-a",
                    artifacts=_artifacts("doc-old-new", "OLD formal body", "old"),
                    job_id="job-old",
                    source_version="sha256:old",
                    validation_status="passed",
                )
                new = runtime.publish_run(
                    doc_id="doc-old-new",
                    tenant_id="tenant-a",
                    artifacts=_artifacts("doc-old-new", "NEW preview body", "new"),
                    job_id="job-new",
                    source_version="sha256:new",
                    validation_status="warning",
                )
                run_id = new["parse_run_id"]

                preview = client.get(
                    "/v1/parse/documents/doc-old-new/preview",
                    params={"run_id": run_id, "tenant_id": "tenant-a", "projection": "reader"},
                )
                formal_before = client.get(
                    "/v1/parse/documents/doc-old-new", params={"tenant_id": "tenant-a"}
                )
                missing_run_id = client.get(
                    "/v1/parse/documents/doc-old-new/preview",
                    params={"tenant_id": "tenant-a"},
                )
                wrong_doc = client.get(
                    "/v1/parse/documents/other-doc/preview",
                    params={"run_id": run_id, "tenant_id": "tenant-a"},
                )
                wrong_tenant = client.get(
                    "/v1/parse/documents/doc-old-new/preview",
                    params={"run_id": run_id, "tenant_id": "tenant-b"},
                )

                runtime.publish_reviewed_run(
                    doc_id="doc-old-new",
                    tenant_id="tenant-a",
                    parse_run_id=run_id,
                    reason="Reviewed the new body",
                    reviewer="qa",
                )
                formal_after = client.get(
                    "/v1/parse/documents/doc-old-new", params={"tenant_id": "tenant-a"}
                )
                records = client.get(
                    "/v1/parse/documents/doc-old-new/records",
                    params={"tenant_id": "tenant-a"},
                )
                exported = client.get(
                    "/v1/parse/documents/doc-old-new/exports",
                    params={"tenant_id": "tenant-a", "dataset": "pages", "format": "jsonl"},
                )
                preview_after_publish = client.get(
                    "/v1/parse/documents/doc-old-new/preview",
                    params={"run_id": run_id, "tenant_id": "tenant-a"},
                )

        self.assertTrue(old["published"])
        self.assertFalse(new["published"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["publication"]["served_run_id"], run_id)
        self.assertFalse(preview.json()["publication"]["is_formal_version"])
        self.assertIn("NEW preview body", json.dumps(preview.json(), ensure_ascii=False))
        self.assertEqual(formal_before.status_code, 200)
        self.assertEqual(formal_before.json()["publication"]["served_run_id"], old["parse_run_id"])
        self.assertTrue(formal_before.json()["publication"]["is_formal_version"])
        self.assertIn("OLD formal body", json.dumps(formal_before.json(), ensure_ascii=False))
        self.assertEqual(missing_run_id.status_code, 400)
        self.assertEqual(wrong_doc.status_code, 404)
        self.assertEqual(wrong_tenant.status_code, 404)

        self.assertEqual(formal_after.status_code, 200)
        self.assertEqual(formal_after.json()["publication"]["served_run_id"], run_id)
        self.assertTrue(formal_after.json()["publication"]["is_formal_version"])
        self.assertIn("NEW preview body", json.dumps(formal_after.json(), ensure_ascii=False))
        self.assertEqual(records.status_code, 200)
        self.assertEqual(records.json()["publication"]["served_run_id"], run_id)
        self.assertEqual(records.json()["parse_run_id"], "job-new")
        self.assertIn("NEW preview body", json.dumps(records.json(), ensure_ascii=False))
        self.assertEqual(exported.status_code, 200)
        exported_row = json.loads(exported.text.strip().splitlines()[0])
        self.assertEqual(exported_row["parse_run_id"], "job-new")
        self.assertEqual(exported_row["text"], "NEW preview body")
        self.assertEqual(preview_after_publish.status_code, 409)

    def test_missing_unsealed_pending_and_rejected_runs_are_refused(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                store = runtime.run_store
                assert store is not None
                pending = store.begin_run(
                    job_id="job-pending", doc_id="doc-states", tenant_id="tenant-a"
                )
                unsealed = store.begin_run(
                    job_id="job-unsealed", doc_id="doc-states", tenant_id="tenant-a"
                )
                store.set_execution_status(parse_run_id=unsealed.parse_run_id, status="running")
                store.save_run_artifacts(
                    parse_run_id=unsealed.parse_run_id,
                    artifacts=_artifacts("doc-states", "unsealed body", "unsealed"),
                )
                store.set_execution_status(parse_run_id=unsealed.parse_run_id, status="succeeded")
                store.record_validation(parse_run_id=unsealed.parse_run_id, status="warning")
                sealed_not_run = store.begin_run(
                    job_id="job-not-run", doc_id="doc-states", tenant_id="tenant-a"
                )
                store.set_execution_status(
                    parse_run_id=sealed_not_run.parse_run_id, status="running"
                )
                store.save_run_artifacts(
                    parse_run_id=sealed_not_run.parse_run_id,
                    artifacts=_artifacts("doc-states", "not validated body", "not-run"),
                )
                store.set_execution_status(
                    parse_run_id=sealed_not_run.parse_run_id, status="succeeded"
                )
                store.seal_run(parse_run_id=sealed_not_run.parse_run_id)
                sealed_error = store.begin_run(
                    job_id="job-error", doc_id="doc-states", tenant_id="tenant-a"
                )
                store.set_execution_status(
                    parse_run_id=sealed_error.parse_run_id, status="running"
                )
                store.save_run_artifacts(
                    parse_run_id=sealed_error.parse_run_id,
                    artifacts=_artifacts("doc-states", "failed validation body", "error"),
                )
                store.set_execution_status(
                    parse_run_id=sealed_error.parse_run_id, status="succeeded"
                )
                store.seal_run(parse_run_id=sealed_error.parse_run_id)
                store.record_validation(parse_run_id=sealed_error.parse_run_id, status="error")
                rejected = runtime.publish_run(
                    doc_id="doc-states",
                    tenant_id="tenant-a",
                    artifacts=_artifacts("doc-states", "rejected body", "rejected"),
                    job_id="job-rejected",
                    source_version="sha256:rejected",
                    validation_status="rejected",
                )
                cases = (
                    ("missing", 404),
                    (pending.parse_run_id, 409),
                    (unsealed.parse_run_id, 409),
                    (sealed_not_run.parse_run_id, 409),
                    (sealed_error.parse_run_id, 409),
                    (rejected["parse_run_id"], 409),
                )
                responses = {
                    run_id: client.get(
                        "/v1/parse/documents/doc-states/preview",
                        params={"run_id": run_id, "tenant_id": "tenant-a"},
                    )
                    for run_id, _ in cases
                }

        self.assertFalse(rejected["published"])
        for run_id, status_code in cases:
            with self.subTest(run_id=run_id):
                self.assertEqual(responses[run_id].status_code, status_code)

    def test_preview_route_reports_unsupported_backend(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                runtime.run_store = None
                response = client.get(
                    "/v1/parse/documents/doc-preview/preview",
                    params={"run_id": "run-preview"},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "run_preview_unavailable")

    def test_preview_qualification_and_artifact_queries_share_one_read_transaction(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                run = runtime.publish_run(
                    doc_id="doc-transaction",
                    artifacts=_artifacts("doc-transaction", "sealed body", "transaction"),
                    job_id="job-transaction",
                    source_version="sha256:transaction",
                    validation_status="warning",
                )
                store = runtime.run_store
                assert store is not None
                original = store._read_transaction
                query_transaction_states: list[bool] = []

                @contextmanager
                def tracked_read_transaction():
                    with original() as connection:
                        class ConnectionProxy:
                            def execute(self, sql, parameters=()):
                                query_transaction_states.append(connection.in_transaction)
                                return connection.execute(sql, parameters)

                        yield ConnectionProxy()

                with patch.object(store, "_read_transaction", tracked_read_transaction):
                    result = store.read_preview_snapshot(
                        doc_id="doc-transaction",
                        parse_run_id=run["parse_run_id"],
                        view_types=("pages",),
                    )

        self.assertGreater(len(query_transaction_states), 3)
        self.assertTrue(all(query_transaction_states))
        self.assertEqual(result["artifacts"].views["pages"][0]["text"], "sealed body")


if __name__ == "__main__":
    unittest.main()
