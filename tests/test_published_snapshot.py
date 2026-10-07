from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsecore.models import Block, BlockType, Chunk, ParseRequest
from parsecore.publication import PublicationStatus
from parsecore.run_store import (
    PublicationPolicyError,
    RunArtifacts,
    SQLiteRunStore,
)
from parsecore.stores import SQLiteJobStore


def _block(doc_id: str, run_tag: str) -> Block:
    return Block(
        block_id=f"{doc_id}-{run_tag}-block",
        doc_id=doc_id,
        type=BlockType.PARAGRAPH,
        content=f"body-{run_tag}",
        metadata={"run": run_tag},
    )


def _chunk(doc_id: str, run_tag: str) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}-{run_tag}-chunk",
        doc_id=doc_id,
        block_ids=(f"{doc_id}-{run_tag}-block",),
        text=f"chunk-{run_tag}",
        embedding=(0.1, 0.2),
    )


class PublishedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="parsecore-published-snapshot-")
        database_path = Path(self._tmp.name) / "parsecore.db"
        self.database_url = f"sqlite:///{database_path.as_posix()}"
        self.store = SQLiteRunStore(self.database_url)
        self.jobs = SQLiteJobStore(self.database_url)
        self.doc_id = "doc-snapshot"
        self.tenant_id = "tenant-snapshot"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stage_run(
        self,
        run_tag: str,
        *,
        generation: int,
        validation_status: str = "passed",
        artifact_profile: str = "default",
        include_records: bool = False,
    ) -> str:
        job = self.jobs.create(
            ParseRequest(
                doc_id=self.doc_id,
                file_path=f"{run_tag}.pdf",
                tenant_id=self.tenant_id,
            )
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version=f"sha256:{run_tag}",
            claim_token=f"claim-{run_tag}",
            expected_generation=generation,
            profile=artifact_profile,
            artifact_profile=artifact_profile,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id,
            status="running",
            claim_token=f"claim-{run_tag}",
        )
        views = {
            "pages": [{"page_number": 1, "text": f"page-{run_tag}"}],
            "lines": [{"line_id": f"line-{run_tag}", "text": f"line-{run_tag}"}],
            "index_manifest": [{"run": run_tag, "units": [run_tag]}],
        }
        if include_records:
            views["records"] = [
                {
                    "record_id": f"record-{run_tag}-{index}",
                    "page_start": index,
                    "page_end": index,
                    "raw_text": f"needle-{index}",
                    "fields": {"status": "ready" if index != 2 else "hold"},
                }
                for index in range(1, 4)
            ]
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block(self.doc_id, run_tag),),
                chunks=(_chunk(self.doc_id, run_tag),),
                views=views,
            ),
            claim_token=f"claim-{run_tag}",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id,
            status="succeeded",
            claim_token=f"claim-{run_tag}",
        )
        self.store.seal_run(parse_run_id=run.parse_run_id, claim_token=f"claim-{run_tag}")
        self.store.record_validation(
            parse_run_id=run.parse_run_id,
            status=validation_status,
            reason="snapshot-test",
        )
        return run.parse_run_id

    def _publish(self, run_id: str, *, generation: int, profile: str = "default") -> None:
        self.store.publish_run(
            parse_run_id=run_id,
            expected_generation=generation,
            expected_source_version=f"sha256:{'a' if generation == 0 else 'b'}",
            claim_token=f"claim-{'a' if generation == 0 else 'b'}",
            profile=profile,
        )

    def test_reads_typed_sealed_artifacts_and_only_requested_views(self) -> None:
        run_id = self._stage_run("a", generation=0)
        self._publish(run_id, generation=0)
        with self.store._write_transaction() as conn:
            conn.execute(
                "UPDATE blocks SET content = 'live-block' WHERE tenant_id = ? AND doc_id = ?",
                (self.tenant_id, self.doc_id),
            )
            conn.execute(
                "UPDATE chunks SET text = 'live-chunk' WHERE tenant_id = ? AND doc_id = ?",
                (self.tenant_id, self.doc_id),
            )

        snapshot = self.store.read_published_snapshot(
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            view_types=("pages", "index_manifest"),
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["record"].parse_run_id, run_id)
        self.assertEqual(snapshot["publication"], {
            "schema_version": "2026-09-publication-read-context-v1",
            "served_run_id": run_id,
            "run_identity": run_id,
            "pinned": True,
            "is_formal_version": True,
            "publication_status": PublicationStatus.PUBLISHED.value,
            "publication_generation": 1,
            "consumer_signal": "published",
            "latest_job_used_as_formal_version": False,
        })
        artifacts = snapshot["artifacts"]
        self.assertIsInstance(artifacts.blocks[0], Block)
        self.assertIsInstance(artifacts.chunks[0], Chunk)
        self.assertEqual(artifacts.blocks[0].content, "body-a")
        self.assertEqual(artifacts.chunks[0].text, "chunk-a")
        self.assertEqual(set(artifacts.views), {"pages", "index_manifest"})
        self.assertEqual(artifacts.views["index_manifest"][0]["run"], "a")

    def test_record_queries_are_run_scoped_and_omit_full_blocks_and_chunks(self) -> None:
        run_id = self._stage_run("a", generation=0, include_records=True)
        self._publish(run_id, generation=0)
        with self.store._write_transaction() as conn:
            conn.execute(
                "UPDATE document_views SET payload_json = '{\"record_id\":\"live\"}'"
                " WHERE tenant_id = ? AND doc_id = ? AND view_type = 'records'",
                (self.tenant_id, self.doc_id),
            )

        snapshot = self.store.read_published_snapshot(
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            record_query={
                "query": "needle",
                "field_filters": {"status": "ready"},
                "limit": 1,
                "offset": 1,
            },
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["artifacts"].blocks, ())
        self.assertEqual(snapshot["artifacts"].chunks, ())
        self.assertEqual(snapshot["records"]["total"], 2)
        self.assertEqual(snapshot["records"]["limit"], 1)
        self.assertEqual(snapshot["records"]["offset"], 1)
        self.assertTrue(snapshot["records"]["persisted"])
        self.assertEqual(snapshot["records"]["items"][0]["record_id"], "record-a-3")

    def test_page_only_record_query_uses_sql_pagination(self) -> None:
        run_id = self._stage_run("a", generation=0, include_records=True)
        self._publish(run_id, generation=0)

        snapshot = self.store.read_published_snapshot(
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            record_query={"page_start": 2, "page_end": 3, "limit": 1, "offset": 1},
        )

        self.assertEqual(snapshot["records"]["total"], 2)
        self.assertEqual(snapshot["records"]["items"][0]["record_id"], "record-a-3")

    def test_active_pointer_and_sealed_artifacts_share_one_read_snapshot(self) -> None:
        first = self._stage_run("a", generation=0)
        self._publish(first, generation=0)
        second = self._stage_run("b", generation=1)

        with self.store._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        original_row_to_run = SQLiteRunStore._row_to_run
        published_second = False

        def publish_after_pointer_read(row):
            nonlocal published_second
            record = original_row_to_run(row)
            if not published_second and record.parse_run_id == first:
                published_second = True
                self._publish(second, generation=1)
            return record

        with patch.object(SQLiteRunStore, "_row_to_run", side_effect=publish_after_pointer_read):
            snapshot = self.store.read_published_snapshot(
                doc_id=self.doc_id,
                tenant_id=self.tenant_id,
            )

        self.assertTrue(published_second)
        self.assertEqual(snapshot["record"].parse_run_id, first)
        self.assertEqual(snapshot["artifacts"].blocks[0].content, "body-a")
        self.assertEqual(snapshot["artifacts"].chunks[0].text, "chunk-a")

    def test_missing_active_run_returns_none(self) -> None:
        self.assertIsNone(
            self.store.read_published_snapshot(
                doc_id=self.doc_id,
                tenant_id=self.tenant_id,
            )
        )

    def test_reviewed_publish_cannot_bypass_rag_durable_index_requirement(self) -> None:
        run_id = self._stage_run(
            "a",
            generation=0,
            validation_status="warning",
            artifact_profile="rag",
        )

        with self.assertRaises(PublicationPolicyError) as captured:
            self.store.publish_run(
                parse_run_id=run_id,
                expected_generation=0,
                expected_source_version="sha256:a",
                claim_token="claim-a",
                durable_index=False,
                profile="rag",
                reviewed={"reason": "reviewed"},
            )
        self.assertEqual(captured.exception.policy_reason, "index_backend_not_durable")
        self.assertIsNone(
            self.store.get_active_run(doc_id=self.doc_id, tenant_id=self.tenant_id)
        )


if __name__ == "__main__":
    unittest.main()
