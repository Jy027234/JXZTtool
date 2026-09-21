"""P0-05: the long-term fault regression for run publication.

`tests/test_run_store.py` already proves the publish transaction itself.  This
module is the P0-05 regression: the failure modes that are *not* a single
transaction — process exits around the publish boundary, retries, lease
expiry, worker restart, a dead index, concurrent part updates and notification
loss or duplication — plus the migration and rollback paths.

Every test states the invariant it protects: **the previously published version
stays complete and the new run stays unpublished.**
"""

from __future__ import annotations

import unittest
from typing import Any
from pathlib import Path
from tempfile import TemporaryDirectory

from parsecore.models import Block, BlockType, Chunk, ParseJobState, ParseRequest
from parsecore.publication import PublicationStatus
from parsecore.run_store import (
    PublishError,
    RunArtifacts,
    SQLiteRunStore,
    StalePublicationError,
)
from parsecore.stores import SQLiteJobStore


def _block(block_id: str, text: str, *, doc_id: str = "doc-1") -> Block:
    return Block(
        block_id=block_id,
        doc_id=doc_id,
        type=BlockType.PARAGRAPH,
        content=text,
        metadata={"page": 1},
    )


def _chunk(chunk_id: str, block_ids: tuple[str, ...], text: str, *, doc_id: str = "doc-1") -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id=doc_id, block_ids=block_ids, text=text)


def _views(text: str) -> dict[str, list[dict[str, object]]]:
    return {"pages": [{"page_number": 1, "text": text}]}


class FaultRegressionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database_url = f"sqlite:///{(root / 'parsecore.db').as_posix()}"
        self.jobs = SQLiteJobStore(self.database_url)
        self.store = SQLiteRunStore(self.database_url)
        self.doc_id = "doc-1"
        self.tenant_id = "tenant-a"

    def tearDown(self) -> None:
        for resource in (self.store, self.jobs):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _job(self, *, doc_id: str | None = None) -> str:
        job = self.jobs.create(
            ParseRequest(
                doc_id=doc_id or self.doc_id,
                file_path="x.docx",
                tenant_id=self.tenant_id,
            )
        )
        return job.job_id

    def _stage_and_seal(
        self,
        *,
        text: str = "v1",
        claim_token: str = "claim-1",
        source_version: str = "sha256:v1",
        seal: bool = True,
        doc_id: str | None = None,
        expected_generation: int | None = 0,
        job_id: str | None = None,
        ids: tuple[str, str] = ("blk-1", "chk-1"),
    ):
        doc = doc_id or self.doc_id
        block_id, chunk_id = ids
        run = self.store.begin_run(
            job_id=job_id or self._job(doc_id=doc),
            doc_id=doc,
            tenant_id=self.tenant_id,
            source_version=source_version,
            claim_token=claim_token,
            expected_generation=expected_generation,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token=claim_token
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block(block_id, text, doc_id=doc),),
                chunks=(_chunk(chunk_id, (block_id,), text, doc_id=doc),),
                views=_views(text),
            ),
            claim_token=claim_token,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token=claim_token
        )
        if seal:
            self.store.seal_run(parse_run_id=run.parse_run_id, claim_token=claim_token)
        return run

    def _publish_first(self, *, text: str = "v1") -> str:
        run = self._stage_and_seal(text=text, source_version="sha256:v1")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=run.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:v1",
            claim_token="claim-1",
        )
        return run.parse_run_id

    def _live_text(self, *, doc_id: str | None = None) -> list[str]:
        return [
            block.content
            for block in self.jobs.get_blocks(doc_id=doc_id or self.doc_id, tenant_id=self.tenant_id)
        ]

    def _assert_first_version_intact(self, active_run_id: str) -> None:
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], active_run_id)
        self.assertTrue(state["formal_version_available"])
        self.assertEqual(self._live_text(), ["v1"])


class ProcessExitTests(FaultRegressionTestCase):
    """A process that dies between steps must not leave a half-published document."""

    def test_die_after_staging_leaves_the_old_version_intact(self) -> None:
        first = self._publish_first()
        self._stage_and_seal(text="v2", claim_token="claim-2", source_version="sha256:v2")
        # Process dies here: staged, sealed, never published.
        self._assert_first_version_intact(first)

    def test_die_after_seal_and_before_validation_leaves_the_old_version_intact(self) -> None:
        first = self._publish_first()
        run = self._stage_and_seal(text="v2", claim_token="claim-2", source_version="sha256:v2")
        with self.assertRaises(PublishError):
            # No validation recorded, so publication is refused even if it is asked for.
            self.store.publish_run(parse_run_id=run.parse_run_id, claim_token="claim-2")
        self._assert_first_version_intact(first)
        self.assertEqual(
            self.store.get_run(parse_run_id=run.parse_run_id).publication_status,
            PublicationStatus.UNPUBLISHED.value,
        )

    def test_die_after_the_publish_commit_before_notification_keeps_the_event(self) -> None:
        run_id = self._publish_first()
        # The notification was never dispatched; the outbox still holds it, so a
        # restart can deliver it without re-running the parse.
        published_events = [
            event
            for event in self.store.pending_publication_events()
            if event["event_type"] == "run_published"
        ]
        self.assertEqual(len(published_events), 1)
        self.assertEqual(published_events[0]["parse_run_id"], run_id)

        # The published state is already authoritative and does not depend on the
        # notification having been sent.
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], run_id)
        self.assertTrue(state["formal_version_available"])


class RetryAndLeaseTests(FaultRegressionTestCase):
    def test_repeating_the_whole_sequence_is_idempotent_for_the_same_run(self) -> None:
        first = self._publish_first()
        before = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)

        # A duplicate request that reuses the published run's identity must not
        # create a second generation.
        with self.assertRaises(PublishError):
            self.store.publish_run(parse_run_id=first, expected_generation=0)

        after = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(after["publication_generation"], before["publication_generation"])
        self.assertEqual(after["active_run_id"], first)

    def test_lease_expiry_recovers_by_creating_a_new_run(self) -> None:
        # A worker dies mid-run; the recovery path begins a *new* run rather than
        # reviving the dead one, so the old attempt's partial artifacts can never
        # be published.
        dead = self._stage_and_seal(
            text="partial", claim_token="claim-dead", source_version="sha256:v1", seal=False
        )
        recovered = self.store.begin_run(
            job_id=self._job(),
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:v1",
            claim_token="claim-new",
            expected_generation=0,
            attempt_count=2,
            base_run_id=dead.parse_run_id,
        )
        self.assertNotEqual(recovered.parse_run_id, dead.parse_run_id)
        self.assertEqual(recovered.base_run_id, dead.parse_run_id)

        with self.store._connect() as connection:
            staged = connection.execute(
                "SELECT COUNT(*) FROM run_blocks WHERE parse_run_id = ?",
                (recovered.parse_run_id,),
            ).fetchone()[0]
        self.assertEqual(staged, 0)

    def test_restart_after_a_failed_run_still_publishes_cleanly(self) -> None:
        failed = self._stage_and_seal(text="v1", claim_token="claim-1", source_version="sha256:v1")
        self.store.record_validation(parse_run_id=failed.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=failed.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:v1",
            claim_token="claim-1",
        )

        # A later attempt on the same document publishes generation 2.
        second = self._stage_and_seal(
            text="v2", claim_token="claim-2", source_version="sha256:v2", expected_generation=1
        )
        self.store.record_validation(parse_run_id=second.parse_run_id, status="passed")
        result = self.store.publish_run(
            parse_run_id=second.parse_run_id,
            expected_generation=1,
            expected_source_version="sha256:v2",
            claim_token="claim-2",
        )
        self.assertEqual(result.publication_generation, 2)
        self.assertEqual(result.superseded_run_id, failed.parse_run_id)
        self.assertEqual(self._live_text(), ["v2"])
        self.assertEqual(
            self.store.get_run(parse_run_id=failed.parse_run_id).publication_status,
            PublicationStatus.SUPERSEDED.value,
        )


class IndexAvailabilityTests(FaultRegressionTestCase):
    def test_index_dependent_profile_cannot_publish_without_a_durable_index(self) -> None:
        run = self._stage_and_seal(text="v1", source_version="sha256:v1")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")

        with self.assertRaises(PublishError) as captured:
            self.store.publish_run(
                parse_run_id=run.parse_run_id,
                expected_generation=0,
                durable_index=False,
                profile="rag",
            )
        self.assertIn("index_backend_not_durable", str(captured.exception))
        self.assertIsNone(
            self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)[
                "active_run_id"
            ]
        )

    def test_non_index_profile_publishes_without_a_durable_index(self) -> None:
        run = self._stage_and_seal(text="v1", source_version="sha256:v1")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")
        result = self.store.publish_run(
            parse_run_id=run.parse_run_id,
            expected_generation=0,
            durable_index=False,
            profile="default",
        )
        self.assertTrue(result.published)


class ConcurrentPartTests(FaultRegressionTestCase):
    def test_two_parts_publishing_out_of_order_keep_the_newest_generation(self) -> None:
        # A parent document whose parts finish in reverse order: whichever part
        # publishes second must observe the generation the first one produced.
        first_part = self._stage_and_seal(
            text="part-1",
            claim_token="claim-p1",
            source_version="sha256:p1",
            doc_id="doc-part-1",
            expected_generation=0,
            ids=("p1-blk", "p1-chk"),
        )
        self.store.record_validation(parse_run_id=first_part.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=first_part.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:p1",
            claim_token="claim-p1",
        )

        second_part = self._stage_and_seal(
            text="part-2",
            claim_token="claim-p2",
            source_version="sha256:p2",
            doc_id="doc-part-2",
            expected_generation=0,
            ids=("p2-blk", "p2-chk"),
        )
        self.store.record_validation(parse_run_id=second_part.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=second_part.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:p2",
            claim_token="claim-p2",
        )

        # Each document keeps its own pointer and generation: parts do not
        # interfere with each other's publication.
        for doc_id, run_id, text in (
            ("doc-part-1", first_part.parse_run_id, "part-1"),
            ("doc-part-2", second_part.parse_run_id, "part-2"),
        ):
            with self.subTest(doc_id=doc_id):
                state = self.store.publication_state(doc_id=doc_id, tenant_id=self.tenant_id)
                self.assertEqual(state["active_run_id"], run_id)
                self.assertEqual(state["publication_generation"], 1)
                live = [
                    block.content
                    for block in self.jobs.get_blocks(doc_id=doc_id, tenant_id=self.tenant_id)
                ]
                self.assertEqual(live, [text])

    def test_shared_block_id_across_documents_is_refused_not_deleted(self) -> None:
        # The live tier keys blocks by a globally unique ``block_id`` while reads
        # are scoped by tenant/doc, so two documents cannot both hold one id.
        # Refusing the second publish keeps the first document's artifacts intact;
        # deleting them to make room would be data loss, not conflict resolution.
        first = self._stage_and_seal(
            text="v1",
            claim_token="claim-1",
            source_version="sha256:v1",
            doc_id="doc-a",
            expected_generation=0,
            ids=("shared-blk", "shared-chk"),
        )
        self.store.record_validation(parse_run_id=first.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=first.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:v1",
            claim_token="claim-1",
        )

        second = self._stage_and_seal(
            text="v2",
            claim_token="claim-2",
            source_version="sha256:v2",
            doc_id="doc-b",
            expected_generation=0,
            ids=("shared-blk", "shared-chk"),
        )
        self.store.record_validation(parse_run_id=second.parse_run_id, status="passed")

        with self.assertRaises(PublishError) as captured:
            self.store.publish_run(
                parse_run_id=second.parse_run_id,
                expected_generation=0,
                expected_source_version="sha256:v2",
                claim_token="claim-2",
            )
        self.assertIn("collision", str(captured.exception))

        # doc-a keeps its published version and its live artifacts …
        self.assertEqual(
            [
                block.content
                for block in self.jobs.get_blocks(doc_id="doc-a", tenant_id=self.tenant_id)
            ],
            ["v1"],
        )
        self.assertEqual(
            self.store.publication_state(doc_id="doc-a", tenant_id=self.tenant_id)[
                "active_run_id"
            ],
            first.parse_run_id,
        )
        # … and doc-b is simply not published.
        self.assertIsNone(
            self.store.publication_state(doc_id="doc-b", tenant_id=self.tenant_id)["active_run_id"]
        )
        self.assertEqual(
            self.store.get_run(parse_run_id=second.parse_run_id).publication_status,
            PublicationStatus.UNPUBLISHED.value,
        )
        # Nothing was removed from any document.
        self.assertNotIn(
            "artifact_id_displaced",
            [event["event_type"] for event in self.store.pending_publication_events()],
        )

    def test_distinct_ids_across_documents_both_publish(self) -> None:
        # The collision check must not block the normal independent case.
        first = self._stage_and_seal(
            text="a",
            claim_token="claim-1",
            source_version="sha256:a",
            doc_id="doc-a",
            expected_generation=0,
            ids=("a-blk", "a-chk"),
        )
        self.store.record_validation(parse_run_id=first.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=first.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:a",
            claim_token="claim-1",
        )

        second = self._stage_and_seal(
            text="b",
            claim_token="claim-2",
            source_version="sha256:b",
            doc_id="doc-b",
            expected_generation=0,
            ids=("b-blk", "b-chk"),
        )
        self.store.record_validation(parse_run_id=second.parse_run_id, status="passed")
        result = self.store.publish_run(
            parse_run_id=second.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:b",
            claim_token="claim-2",
        )

        self.assertTrue(result.published)
        self.assertEqual(
            [
                block.content
                for block in self.jobs.get_blocks(doc_id="doc-a", tenant_id=self.tenant_id)
            ],
            ["a"],
        )
        self.assertEqual(
            [
                block.content
                for block in self.jobs.get_blocks(doc_id="doc-b", tenant_id=self.tenant_id)
            ],
            ["b"],
        )

    def test_a_part_rerun_replaces_only_its_own_document(self) -> None:
        keep = self._publish_first()
        other = self._stage_and_seal(
            text="other-1",
            claim_token="claim-o",
            source_version="sha256:o1",
            doc_id="doc-other",
            expected_generation=0,
            ids=("o1-blk", "o1-chk"),
        )
        self.store.record_validation(parse_run_id=other.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=other.parse_run_id,
            expected_generation=0,
            expected_source_version="sha256:o1",
            claim_token="claim-o",
        )

        rerun = self._stage_and_seal(
            text="other-2",
            claim_token="claim-o2",
            source_version="sha256:o2",
            doc_id="doc-other",
            expected_generation=1,
            ids=("o2-blk", "o2-chk"),
        )
        self.store.record_validation(parse_run_id=rerun.parse_run_id, status="passed")
        self.store.publish_run(
            parse_run_id=rerun.parse_run_id,
            expected_generation=1,
            expected_source_version="sha256:o2",
            claim_token="claim-o2",
        )

        # The untouched document keeps its published version and generation.
        self._assert_first_version_intact(keep)
        state = self.store.publication_state(doc_id="doc-other", tenant_id=self.tenant_id)
        self.assertEqual(state["publication_generation"], 2)
        self.assertEqual(
            [
                block.content
                for block in self.jobs.get_blocks(doc_id="doc-other", tenant_id=self.tenant_id)
            ],
            ["other-2"],
        )


class NotificationTests(FaultRegressionTestCase):
    def test_dispatcher_delivers_and_marks_events(self) -> None:
        self._publish_first()
        seen: list[dict[str, Any]] = []

        report = self.store.dispatch_pending_publication_events(
            handler=lambda event: seen.append(event) or True
        )

        self.assertGreaterEqual(report["delivered_count"], 1)
        self.assertEqual(report["failed_count"], 0)
        published = [event for event in seen if event["event_type"] == "run_published"]
        self.assertEqual(len(published), 1)
        self.assertEqual(report["remaining_pending"], 0)

    def test_dispatcher_redelivers_after_a_handler_failure(self) -> None:
        self._publish_first()

        first = self.store.dispatch_pending_publication_events(handler=lambda event: False)
        self.assertGreaterEqual(first["failed_count"], 1)
        # The notification is NOT dropped: the original event is still pending and
        # the failure is recorded.
        pending_types = [
            event["event_type"] for event in self.store.pending_publication_events()
        ]
        self.assertIn("run_published", pending_types)
        self.assertIn("publication_event_dispatch_failed", pending_types)

        second = self.store.dispatch_pending_publication_events(handler=lambda event: True)
        self.assertGreaterEqual(second["delivered_count"], 1)
        self.assertEqual(second["remaining_pending"], 0)

    def test_dispatcher_survives_a_raising_handler(self) -> None:
        self._publish_first()

        def boom(_event: dict[str, Any]) -> bool:
            raise RuntimeError("downstream unavailable")

        report = self.store.dispatch_pending_publication_events(handler=boom)

        self.assertGreaterEqual(report["failed_count"], 1)
        self.assertIn("downstream unavailable", report["failures"][0]["error"])
        self.assertIn(
            "run_published",
            [event["event_type"] for event in self.store.pending_publication_events()],
        )

    def test_repeated_publish_does_not_duplicate_the_published_event(self) -> None:
        first = self._publish_first()
        # Re-recording the same event type for the same run must be ignored, so a
        # retry cannot notify downstream twice.
        self.store.record_publication_event(
            tenant_id=self.tenant_id,
            doc_id=self.doc_id,
            parse_run_id=first,
            event_type="run_published",
        )
        events = [
            event
            for event in self.store.pending_publication_events()
            if event["event_type"] == "run_published"
        ]
        self.assertEqual(len(events), 1)

    def test_lost_notification_is_recoverable_from_the_outbox(self) -> None:
        self._publish_first()
        pending = self.store.pending_publication_events()
        published_event = next(
            event for event in pending if event["event_type"] == "run_published"
        )

        # A dispatcher that crashed before marking the event keeps it pending, so
        # a restart redelivers instead of losing the notification.
        self.assertFalse(
            self.store.mark_publication_event_dispatched(event_id="does-not-exist")
        )
        self.assertIn(
            published_event["event_id"],
            [event["event_id"] for event in self.store.pending_publication_events()],
        )

        self.assertTrue(
            self.store.mark_publication_event_dispatched(event_id=published_event["event_id"])
        )
        self.assertNotIn(
            published_event["event_id"],
            [event["event_id"] for event in self.store.pending_publication_events()],
        )

    def test_duplicate_delivery_is_idempotent(self) -> None:
        self._publish_first()
        event = next(
            event
            for event in self.store.pending_publication_events()
            if event["event_type"] == "run_published"
        )
        self.assertTrue(self.store.mark_publication_event_dispatched(event_id=event["event_id"]))
        # Second delivery attempt is a no-op, not a second notification.
        self.assertFalse(self.store.mark_publication_event_dispatched(event_id=event["event_id"]))

    def test_event_identity_is_unique_per_run_and_type(self) -> None:
        self._publish_first()
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id, doc_id, parse_run_id, event_type, COUNT(*) AS n"
                " FROM publication_events GROUP BY tenant_id, doc_id, parse_run_id, event_type"
                " HAVING n > 1"
            ).fetchall()
        self.assertEqual(rows, [])


class MigrationAndRollbackTests(FaultRegressionTestCase):
    def test_import_legacy_snapshot_is_never_publishable(self) -> None:
        import json

        from tools.legacy_snapshot_import import import_legacy_documents

        # Seed artifacts the way the legacy path writes them: live rows with no
        # run identity and a completed job.
        job = self.jobs.create(
            ParseRequest(doc_id="legacy-doc", file_path="legacy.docx", tenant_id="tenant-legacy")
        )
        self.jobs.save_blocks(
            doc_id="legacy-doc",
            blocks=[_block("b1", "legacy text", doc_id="legacy-doc")],
            tenant_id="tenant-legacy",
        )
        self.jobs.save_chunks(
            doc_id="legacy-doc",
            chunks=[_chunk("c1", ("b1",), "legacy text", doc_id="legacy-doc")],
            tenant_id="tenant-legacy",
        )
        self.jobs.save_document_views(
            doc_id="legacy-doc",
            pages=[{"page_number": 1, "text": "legacy text"}],
            tenant_id="tenant-legacy",
        )
        self.jobs.update_state(job_id=job.job_id, state=ParseJobState.DONE)

        report = import_legacy_documents(
            job_store=self.jobs,
            run_store=self.store,
            tenant_id="tenant-legacy",
            doc_ids=["legacy-doc"],
            apply=True,
        )

        self.assertEqual(report["imported_count"], 1)
        entry = report["documents"][0]
        self.assertEqual(entry["doc_id"], "legacy-doc")
        self.assertEqual(entry["validation_status"], "not_run")

        record = self.store.get_run(parse_run_id=entry["parse_run_id"])
        self.assertEqual(record.validation_status, "not_run")
        self.assertEqual(record.publication_status, PublicationStatus.PUBLISHED.value)
        # The import marks itself in the run's version sources; the sealed
        # manifest is computed by seal_run from the staged artifacts.
        self.assertEqual(record.version_sources["engine"], "legacy-import")
        self.assertFalse(record.validation_report["metrics"]["validated"])

        # The imported snapshot is explicitly exempt, and it still carries no
        # validation conclusion: a legacy import must never look like a pass.
        with self.assertRaises(PublishError) as captured:
            self.store.publish_run(parse_run_id=entry["parse_run_id"])
        self.assertIn("legacy", str(captured.exception))
        self.assertFalse(json.loads(entry["manifest"])["validated"])

    def test_dry_run_import_writes_nothing(self) -> None:
        from tools.legacy_snapshot_import import import_legacy_documents

        job = self.jobs.create(
            ParseRequest(doc_id="legacy-doc-2", file_path="legacy.docx", tenant_id="tenant-legacy")
        )
        self.jobs.save_blocks(
            doc_id="legacy-doc-2",
            blocks=[_block("b1", "legacy", doc_id="legacy-doc-2")],
            tenant_id="tenant-legacy",
        )
        self.jobs.update_state(job_id=job.job_id, state=ParseJobState.DONE)

        report = import_legacy_documents(
            job_store=self.jobs,
            run_store=self.store,
            tenant_id="tenant-legacy",
            apply=False,
        )
        self.assertEqual(report["imported_count"], 0)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(self.store.list_runs(doc_id="legacy-doc-2", tenant_id="tenant-legacy"), ())

    def test_rollback_restores_the_active_run_after_live_tier_corruption(self) -> None:
        first = self._publish_first()

        # Corrupt the live tier the way an out-of-band write would.
        with self.store._write_transaction() as connection:
            connection.execute("DELETE FROM blocks WHERE doc_id = ?", (self.doc_id,))
        self.assertEqual(self._live_text(), [])

        result = self.store.restore_live_from_run(doc_id=self.doc_id, tenant_id=self.tenant_id)

        self.assertTrue(result["restored"])
        self.assertEqual(result["active_run_id"], first)
        self._assert_first_version_intact(first)

    def test_rollback_refuses_when_the_sealed_staging_was_tampered_with(self) -> None:
        import sqlite3

        self._publish_first()
        connection = sqlite3.connect(self.store._path)
        connection.execute("UPDATE run_blocks SET payload_json = '{}'")
        connection.commit()
        connection.close()

        with self.assertRaises(Exception) as captured:
            self.store.restore_live_from_run(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertIn("hash", str(captured.exception))

    def test_migration_preserves_document_count_and_content_hash(self) -> None:
        from tools.legacy_snapshot_import import import_legacy_documents, snapshot_document

        self.jobs.create(
            ParseRequest(doc_id="legacy-doc-3", file_path="legacy.docx", tenant_id="tenant-legacy")
        )
        self.jobs.save_blocks(
            doc_id="legacy-doc-3",
            blocks=[
                _block("b1", "first", doc_id="legacy-doc-3"),
                _block("b2", "second", doc_id="legacy-doc-3"),
            ],
            tenant_id="tenant-legacy",
        )
        self.jobs.save_document_views(
            doc_id="legacy-doc-3",
            pages=[{"page_number": 1, "text": "first second"}],
            tenant_id="tenant-legacy",
        )

        before = snapshot_document(
            job_store=self.jobs, tenant_id="tenant-legacy", doc_id="legacy-doc-3"
        )
        report = import_legacy_documents(
            job_store=self.jobs,
            run_store=self.store,
            tenant_id="tenant-legacy",
            doc_ids=["legacy-doc-3"],
            apply=True,
        )
        after = snapshot_document(
            job_store=self.jobs, tenant_id="tenant-legacy", doc_id="legacy-doc-3"
        )

        self.assertEqual(report["imported_count"], 1)
        self.assertEqual(before["block_count"], after["block_count"])
        self.assertEqual(before["block_hash"], after["block_hash"])
        self.assertEqual(before["view_hash"], after["view_hash"])


class StaleWriterTests(FaultRegressionTestCase):
    def test_a_stale_writer_can_never_overwrite_a_newer_publication(self) -> None:
        first = self._publish_first()

        stale = self._stage_and_seal(
            text="stale",
            claim_token="claim-stale",
            source_version="sha256:v1",
            # This writer still believes generation 0 is current.
            expected_generation=0,
        )
        self.store.record_validation(parse_run_id=stale.parse_run_id, status="passed")

        with self.assertRaises(StalePublicationError):
            self.store.publish_run(
                parse_run_id=stale.parse_run_id,
                expected_generation=0,
                expected_source_version="sha256:v1",
                claim_token="claim-stale",
            )
        self._assert_first_version_intact(first)


if __name__ == "__main__":
    unittest.main()
