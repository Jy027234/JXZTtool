from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.models import ParseJobState
from parsecore.stubs import FakeEmbeddingProvider
from tests.support import TemporaryWorkspace
from unittest.mock import patch


SAMPLE_CONFIG = """
[project]
name = "test-api"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25

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


class ParseApiTests(unittest.TestCase):
    def test_job_lifecycle_endpoints(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual", "Apply torque"])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["status"], "ok")

                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-api-001",
                        "file_path": str(document_path),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    },
                )
                self.assertEqual(created.status_code, 202)
                payload = created.json()
                self.assertEqual(payload["doc_id"], "doc-api-001")
                self.assertEqual(payload["state"], ParseJobState.PENDING.value)
                job_id = payload["job_id"]

                final_job = None
                for _ in range(20):
                    current = client.get(f"/v1/parse/jobs/{job_id}")
                    self.assertEqual(current.status_code, 200)
                    final_job = current.json()
                    if final_job["state"] == ParseJobState.DONE.value:
                        break

                self.assertIsNotNone(final_job)
                assert final_job is not None
                self.assertEqual(final_job["state"], ParseJobState.DONE.value)

                listed = client.get("/v1/parse/jobs", params={"doc_id": "doc-api-001"})
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(len(listed.json()["items"]), 1)

                document = client.get("/v1/parse/documents/doc-api-001")
                self.assertEqual(document.status_code, 200)
                document_payload = document.json()
                self.assertEqual(document_payload["job"]["job_id"], job_id)
                self.assertGreaterEqual(len(document_payload["blocks"]), 2)
                self.assertEqual(len(document_payload["chunks"]), len(document_payload["blocks"]))

                retried = client.post("/v1/parse/documents/doc-api-001/reparse")
                self.assertEqual(retried.status_code, 202)
                self.assertNotEqual(retried.json()["job_id"], job_id)

                searched = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params={"q": "maintenance manual", "limit": 2},
                )
                self.assertEqual(searched.status_code, 200)
                self.assertEqual(searched.json()["retrieval_mode"], "keyword-fallback")
                items = searched.json()["items"]
                self.assertGreaterEqual(len(items), 1)
                self.assertEqual(items[0]["semantic_role"], "paragraph")

                filtered = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params=[("q", "spec"), ("role", "title")],
                )
                self.assertEqual(filtered.status_code, 200)
                self.assertEqual(filtered.json()["retrieval_mode"], "keyword-fallback")
                self.assertEqual(len(filtered.json()["items"]), 1)
                self.assertEqual(filtered.json()["items"][0]["semantic_role"], "title")

    def test_rechunk_and_reembed_endpoints_submit_explicit_derived_jobs(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual", "Apply torque"])
            with patch(
                "parsecore.bootstrap.build_embedding_provider",
                return_value=FakeEmbeddingProvider(),
            ):
                app = create_app(workspace.config_path)
                with TestClient(app) as client:
                    created = client.post(
                        "/v1/parse/jobs",
                        json={
                            "doc_id": "doc-api-002",
                            "file_path": str(document_path),
                            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        },
                    )
                    self.assertEqual(created.status_code, 202)
                    base_job_id = created.json()["job_id"]

                    for _ in range(20):
                        current = client.get(f"/v1/parse/jobs/{base_job_id}")
                        if current.json()["state"] == ParseJobState.DONE.value:
                            break

                    rechunked = client.post("/v1/parse/documents/doc-api-002/rechunk")
                    self.assertEqual(rechunked.status_code, 202)
                    self.assertEqual(
                        rechunked.json()["options"].get("mode"),
                        "rerun_chunks_only",
                    )

                    reembedded = client.post("/v1/parse/documents/doc-api-002/re-embed")
                    self.assertEqual(reembedded.status_code, 202)
                    self.assertEqual(
                        reembedded.json()["options"].get("mode"),
                        "rerun_embeddings_only",
                    )

                    reembed_job_id = reembedded.json()["job_id"]
                    for _ in range(20):
                        current = client.get(f"/v1/parse/jobs/{reembed_job_id}")
                        if current.json()["state"] == ParseJobState.DONE.value:
                            break

                    document = client.get("/v1/parse/documents/doc-api-002")
                    self.assertEqual(document.status_code, 200)
                    chunks = document.json()["chunks"]
                    self.assertTrue(chunks)
                    self.assertTrue(all(chunk["embedding"] is not None for chunk in chunks))


QUEUE_CONFIG = """
[project]
name = "test-api-queue"
mode = "embedded-sdk"

[runtime]
execution_mode = "queue-worker"
max_workers = 1
poll_interval_ms = 25

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


class ParseQueueApiTests(unittest.TestCase):
    def test_queue_mode_requires_worker_to_complete_jobs(self) -> None:
        from parsecore.worker import build_worker

        with TemporaryWorkspace(QUEUE_CONFIG) as workspace:
            document_path = workspace.create_docx("queued.docx", ["Queue Manual", "Wait for worker"])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-api-queue-001",
                        "file_path": str(document_path),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    },
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["job_id"]

                pending = client.get(f"/v1/parse/jobs/{job_id}")
                self.assertEqual(pending.status_code, 200)
                self.assertEqual(pending.json()["state"], ParseJobState.PENDING.value)

                processed = build_worker(workspace.config_path).drain(max_jobs=1)
                self.assertEqual(processed, 1)

                completed = client.get(f"/v1/parse/jobs/{job_id}")
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["state"], ParseJobState.DONE.value)