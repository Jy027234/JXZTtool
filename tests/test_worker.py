from __future__ import annotations

import unittest

from parsecore.bootstrap import build_runtime
from parsecore.models import ParseJobState, ParseRequest
from parsecore.worker import build_worker
from tests.support import TemporaryWorkspace


QUEUE_CONFIG = """
[project]
name = "test-worker"
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


class QueueWorkerTests(unittest.TestCase):
    def test_worker_claims_and_executes_pending_job(self) -> None:
        with TemporaryWorkspace(QUEUE_CONFIG) as workspace:
            document_path = workspace.create_docx("worker.docx", ["Worker Manual", "Drain queue"])
            runtime = build_runtime(workspace.config_path)
            job = runtime.start(
                ParseRequest(
                    doc_id="doc-worker-001",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            self.assertEqual(job.state, ParseJobState.PENDING)

            processed = build_worker(workspace.config_path).drain(max_jobs=1)
            self.assertEqual(processed, 1)

            reloaded = build_runtime(workspace.config_path)
            final_job = reloaded.get_job(job_id=job.job_id)

        self.assertIsNotNone(final_job)
        assert final_job is not None
        self.assertEqual(final_job.state, ParseJobState.DONE)

    def test_queue_claim_skips_pdf_part_when_doc_limit_reached(self) -> None:
        with TemporaryWorkspace(QUEUE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            part_options = {
                "job_kind": "pdf_part",
                "source_doc_id": "doc-worker-pdf",
                "parent_job_id": "parent-job",
                "max_active_parts_per_doc": 1,
            }
            active = runtime.start(
                ParseRequest(
                    doc_id="doc-worker-pdf-part-1",
                    file_path="part-1.pdf",
                    media_type="application/pdf",
                    options={**part_options, "part_id": "part-1"},
                )
            )
            blocked = runtime.start(
                ParseRequest(
                    doc_id="doc-worker-pdf-part-2",
                    file_path="part-2.pdf",
                    media_type="application/pdf",
                    options={**part_options, "part_id": "part-2"},
                )
            )
            other = runtime.start(
                ParseRequest(
                    doc_id="doc-worker-other",
                    file_path="other.pdf",
                    media_type="application/pdf",
                )
            )
            runtime.job_store.update_state(job_id=active.job_id, state=ParseJobState.PARSING)

            claimed = runtime.claim_next_job()

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.job_id, other.job_id)
            reloaded_blocked = runtime.get_job(job_id=blocked.job_id)
            assert reloaded_blocked is not None
            self.assertEqual(reloaded_blocked.state, ParseJobState.PENDING)

            runtime.job_store.update_state(job_id=active.job_id, state=ParseJobState.DONE)
            runtime.job_store.update_state(job_id=claimed.job_id, state=ParseJobState.DONE)
            unblocked = runtime.claim_next_job()

            self.assertIsNotNone(unblocked)
            assert unblocked is not None
            self.assertEqual(unblocked.job_id, blocked.job_id)
