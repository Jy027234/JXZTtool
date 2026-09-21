"""P0-03: run-scoped artifact storage and atomic publication.

The load-bearing property under test is that the previously published version
stays complete whenever anything fails, and that the new run stays
``unpublished``.  Every test below either proves that property or proves that a
publish request cannot skip it.
"""

from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parsecore.models import Block, BlockType, Chunk, ParseRequest
from parsecore.publication import (
    ExecutionStatus,
    PublicationStatus,
    ValidationStatus,
    can_transition,
)
from parsecore.run_store import (
    PUBLICATION_REJECTED_STALE,
    PublishError,
    RunArtifacts,
    RunNotFoundError,
    RunNotSealableError,
    RunStoreError,
    SQLiteRunStore,
    SealError,
    StalePublicationError,
    publication_ready,
    run_store_for,
)
from parsecore.stores import SQLiteJobStore


def _block(block_id: str, text: str, *, doc_id: str = "doc-1") -> Block:
    return Block(
        block_id=block_id,
        doc_id=doc_id,
        type=BlockType.PARAGRAPH,
        content=text,
        metadata={"page": 1, "parser": "docx-native"},
    )


def _chunk(chunk_id: str, block_ids: tuple[str, ...], text: str, *, doc_id: str = "doc-1") -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id=doc_id, block_ids=block_ids, text=text)


def _views(text: str, *, page: int = 1) -> dict[str, list[dict[str, object]]]:
    return {
        "pages": [{"page_number": page, "text": text}],
        "records": [{"record_id": f"rec-{page}", "page_start": page, "fields": {"text": text}}],
    }


class RunStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database_url = f"sqlite:///{(root / 'parsecore.db').as_posix()}"
        self.jobs = SQLiteJobStore(self.database_url)
        self.store = SQLiteRunStore(self.database_url)
        self.doc_id = "doc-1"
        self.tenant_id = "tenant-a"

    def tearDown(self) -> None:
        # Close whatever holds the database file open before removing the
        # temporary directory: on Windows an open handle blocks deletion, and
        # ``sqlite3.connect`` used as a context manager does not close itself.
        for resource in (self.store, self.jobs):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _publish(
        self,
        *,
        text: str = "old",
        claim_token: str = "claim-old",
        source_version: str = "sha256:old",
        expected_generation: int | None = 0,
        expected_source_version: str | None = None,
        validation_status: str = "passed",
        blocks: tuple[Block, ...] | None = None,
        chunks: tuple[Chunk, ...] | None = None,
        views: dict[str, list[dict[str, object]]] | None = None,
        doc_id: str | None = None,
        seal: bool = True,
    ) -> dict[str, object]:
        doc = doc_id or self.doc_id
        block_list = blocks if blocks is not None else (_block("blk-1", text, doc_id=doc),)
        chunk_list = chunks if chunks is not None else (
            _chunk("chk-1", ("blk-1",), text, doc_id=doc),
        )
        view_map = views if views is not None else _views(text)
        job = self.jobs.create(
            ParseRequest(doc_id=doc, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=doc,
            tenant_id=self.tenant_id,
            source_version=source_version,
            source_bytes=len(text),
            claim_token=claim_token,
            expected_generation=expected_generation,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token=claim_token
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=block_list, chunks=chunk_list, views=view_map),
            claim_token=claim_token,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token=claim_token
        )
        if seal:
            self.store.seal_run(parse_run_id=run.parse_run_id, claim_token=claim_token)
        self.store.record_validation(
            parse_run_id=run.parse_run_id,
            status=validation_status,
            reason="unit-test",
        )
        result = self.store.publish_run(
            parse_run_id=run.parse_run_id,
            expected_source_version=expected_source_version
            if expected_source_version is not None
            else source_version,
            expected_generation=expected_generation,
            claim_token=claim_token,
        )
        return {"run_id": run.parse_run_id, **result.as_payload()}

    def _live_blocks(self, doc_id: str | None = None) -> tuple[Block, ...]:
        return tuple(self.jobs.get_blocks(doc_id=doc_id or self.doc_id, tenant_id=self.tenant_id))

    def _live_chunks(self, doc_id: str | None = None) -> tuple[Chunk, ...]:
        return tuple(self.jobs.get_chunks(doc_id=doc_id or self.doc_id, tenant_id=self.tenant_id))


class RegionAssetFamilyTests(RunStoreTestCase):
    """The ``assets`` family: staged descriptors whose files are verified."""

    def _asset_file(self, *, content: bytes = b"\x89PNG\r\n\x1a\nfake") -> dict[str, object]:
        root = Path(self._tmp.name) / "object-store" / "regions"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "doc-1-t-1.png"
        path.write_bytes(content)
        return {
            "block_id": "doc-1-t-1",
            "kind": "pdf_table",
            "page_number": 1,
            "path": str(path),
            "sha256": sha256(content).hexdigest(),
            "bytes": len(content),
        }

    def _stage_and_seal(self, *, assets: tuple[dict[str, object], ...], profile: str) -> str:
        job = self.jobs.create(
            ParseRequest(
                doc_id=self.doc_id,
                file_path="x.pdf",
                tenant_id=self.tenant_id,
                options={"profile": profile},
            )
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:assets",
            source_bytes=8,
            claim_token="claim-assets",
            profile=profile,
            artifact_profile=profile,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-assets"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block("blk-1", "text"),),
                views=_views("text"),
                assets=assets,
            ),
            claim_token="claim-assets",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token="claim-assets"
        )
        return run.parse_run_id

    def test_a_profile_requiring_assets_seals_when_the_run_staged_them(self) -> None:
        parse_run_id = self._stage_and_seal(assets=(self._asset_file(),), profile="assets-only")

        seal = self.store.seal_run(parse_run_id=parse_run_id, claim_token="claim-assets")

        self.assertEqual(seal.manifest["counts"]["assets"], 1)
        self.assertGreater(int(seal.manifest["asset_bytes"]), 0)
        self.assertTrue(str(seal.manifest["asset_hash"]))
        self.assertIn("assets", seal.manifest["required_artifacts"])

    def test_a_profile_requiring_assets_cannot_seal_without_them(self) -> None:
        parse_run_id = self._stage_and_seal(assets=(), profile="assets-only")

        with self.assertRaises(SealError) as captured:
            self.store.seal_run(parse_run_id=parse_run_id, claim_token="claim-assets")

        self.assertIn("assets", str(captured.exception))

    def test_a_changed_or_missing_asset_file_blocks_sealing(self) -> None:
        asset = self._asset_file()
        parse_run_id = self._stage_and_seal(assets=(asset,), profile="assets-only")
        Path(str(asset["path"])).write_bytes(b"\x89PNG\r\n\x1a\nother content")

        with self.assertRaises(SealError) as captured:
            self.store.seal_run(parse_run_id=parse_run_id, claim_token="claim-assets")

        self.assertIn("digest_mismatch", str(captured.exception))

    def test_a_deleted_asset_file_blocks_publication(self) -> None:
        asset = self._asset_file()
        parse_run_id = self._stage_and_seal(assets=(asset,), profile="assets-only")
        self.store.seal_run(parse_run_id=parse_run_id, claim_token="claim-assets")
        self.store.record_validation(
            parse_run_id=parse_run_id, status="passed", reason="unit-test"
        )
        Path(str(asset["path"])).unlink()

        with self.assertRaises(SealError) as captured:
            self.store.publish_run(
                parse_run_id=parse_run_id,
                expected_generation=0,
                claim_token="claim-assets",
                durable_index=False,
            )

        # The evidence a run claims must still exist when it becomes the formal
        # version; the pointer therefore stays where it was.
        self.assertIn("missing", str(captured.exception))
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertIsNone(state["active_run_id"])


class HappyPathTests(RunStoreTestCase):
    def test_full_sequence_publishes_and_switches_the_pointer(self) -> None:
        result = self._publish(text="hello")

        self.assertTrue(result["published"])
        self.assertEqual(result["publication_generation"], 1)
        self.assertEqual(result["consumer_signal"], "published")
        self.assertEqual(result["publication_status"], PublicationStatus.PUBLISHED.value)

        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertTrue(state["formal_version_available"])
        self.assertEqual(state["active_run_id"], result["run_id"])

        self.assertEqual([block.content for block in self._live_blocks()], ["hello"])
        self.assertEqual([chunk.text for chunk in self._live_chunks()], ["hello"])
        views = self.jobs.get_document_views(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(views["pages"][0]["text"], "hello")

    def test_second_publication_supersedes_the_first(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")
        second = self._publish(
            text="v2",
            claim_token="claim-v2",
            source_version="sha256:v2",
            expected_generation=1,
            blocks=(_block("blk-2", "v2"),),
            chunks=(_chunk("chk-2", ("blk-2",), "v2"),),
        )

        self.assertTrue(second["published"])
        self.assertEqual(second["publication_generation"], 2)
        self.assertEqual(second["superseded_run_id"], first["run_id"])
        self.assertEqual(
            self.store.get_run(parse_run_id=str(first["run_id"])).publication_status,
            PublicationStatus.SUPERSEDED.value,
        )
        self.assertEqual([block.content for block in self._live_blocks()], ["v2"])

    def test_publication_state_is_not_ready_before_any_publication(self) -> None:
        state = self.store.publication_state(doc_id="never-published", tenant_id=self.tenant_id)
        self.assertFalse(state["formal_version_available"])
        self.assertEqual(state["consumer_signal"], "not_ready")
        self.assertIsNone(state["active_run_id"])

    def test_live_rows_are_stamped_with_the_published_run(self) -> None:
        result = self._publish(text="stamped")
        connection = sqlite3.connect(self.store._path)
        run_ids = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT run_id FROM blocks WHERE doc_id = ?", (self.doc_id,)
            )
        }
        chunk_run_ids = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT run_id FROM chunks WHERE doc_id = ?", (self.doc_id,)
            )
        }
        connection.close()
        self.assertEqual(run_ids, {result["run_id"]})
        self.assertEqual(chunk_run_ids, {result["run_id"]})


class PreconditionTests(RunStoreTestCase):
    def test_execution_must_succeed_before_publishing(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),), views=_views("x")),
        )
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="failed")

        # A failed run cannot even be sealed, and certainly cannot be published.
        with self.assertRaises(RunNotSealableError) as captured:
            self.store.seal_run(parse_run_id=run.parse_run_id)
        self.assertEqual(captured.exception.code, "run_not_sealable")

        with self.assertRaises(PublishError):
            self.store.publish_run(parse_run_id=run.parse_run_id)
        self.assertIsNone(self.store.publication_state(doc_id=self.doc_id)["active_run_id"])

    def test_unsealed_run_cannot_be_published(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),), views=_views("x")),
        )
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")

        with self.assertRaises(PublishError) as captured:
            self.store.publish_run(parse_run_id=run.parse_run_id)
        self.assertIn("not sealed", str(captured.exception))

    def test_validation_not_run_blocks_publication(self) -> None:
        with TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'db.sqlite').as_posix()}"
            jobs = SQLiteJobStore(url)
            store = SQLiteRunStore(url)
            job = jobs.create(ParseRequest(doc_id="d", file_path="x.docx"))
            run = store.begin_run(job_id=job.job_id, doc_id="d")
            store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
            store.save_run_artifacts(
                parse_run_id=run.parse_run_id,
                artifacts=RunArtifacts(blocks=(_block("b", "x", doc_id="d"),), views=_views("x")),
            )
            store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")
            store.seal_run(parse_run_id=run.parse_run_id)

            with self.assertRaises(PublishError) as captured:
                store.publish_run(parse_run_id=run.parse_run_id)
            self.assertIn("validation_not_run", str(captured.exception))
            self.assertIsNone(store.publication_state(doc_id="d")["active_run_id"])

    def test_needs_review_and_rejected_never_publish(self) -> None:
        for status in ("needs_review", "rejected", "error"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                url = f"sqlite:///{(Path(tmp) / 'db.sqlite').as_posix()}"
                jobs = SQLiteJobStore(url)
                store = SQLiteRunStore(url)
                job = jobs.create(ParseRequest(doc_id="d", file_path="x.docx"))
                run = store.begin_run(job_id=job.job_id, doc_id="d")
                store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
                store.save_run_artifacts(
                    parse_run_id=run.parse_run_id,
                    artifacts=RunArtifacts(
                        blocks=(_block("b", "x", doc_id="d"),), views=_views("x")
                    ),
                )
                store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")
                store.seal_run(parse_run_id=run.parse_run_id)
                store.record_validation(parse_run_id=run.parse_run_id, status=status)
                with self.assertRaises(PublishError):
                    store.publish_run(parse_run_id=run.parse_run_id)

    def test_warning_blocked_by_default_policy(self) -> None:
        with self.assertRaises(PublishError) as captured:
            self._publish(validation_status="warning")
        self.assertIn("policy_blocks_warning", str(captured.exception))

    def test_legacy_imported_runs_may_never_be_published(self) -> None:
        with self.assertRaises(PublishError) as captured:
            self.store._policy_for("legacy-import-no-validation")
        self.assertIn("legacy", str(captured.exception))

    def test_unknown_validation_policy_version_is_rejected(self) -> None:
        with self.assertRaises(PublishError):
            self.store._policy_for("2027-imaginary-policy")

    def test_missing_required_artifact_blocks_sealing(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        # Blocks but no page view: the reader projection would be empty.
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),)),
        )
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")

        with self.assertRaises(SealError) as captured:
            self.store.seal_run(parse_run_id=run.parse_run_id)
        self.assertIn("reader", str(captured.exception))

    def test_chunk_referencing_an_unknown_block_blocks_sealing(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block("b1", "x"),),
                chunks=(_chunk("c1", ("b-dangling",), "x"),),
                views=_views("x"),
            ),
        )
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")

        with self.assertRaises(SealError) as captured:
            self.store.seal_run(parse_run_id=run.parse_run_id)
        self.assertIn("b-dangling", str(captured.exception))

    def test_artifacts_are_immutable_after_sealing(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="running")
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),), views=_views("x")),
        )
        self.store.set_execution_status(parse_run_id=run.parse_run_id, status="succeeded")
        self.store.seal_run(parse_run_id=run.parse_run_id)

        with self.assertRaises(SealError):
            self.store.save_run_artifacts(
                parse_run_id=run.parse_run_id,
                artifacts=RunArtifacts(blocks=(_block("b", "tampered"),), views=_views("tampered")),
            )
        with self.assertRaises(SealError):
            self.store.seal_run(parse_run_id=run.parse_run_id)

    def test_unknown_run_id_is_reported_and_never_published(self) -> None:
        # A read of an unknown run is a clean ``None``; a write against one is an
        # error, because that means the caller lost its run identity.
        self.assertIsNone(self.store.get_run(parse_run_id="run-does-not-exist"))
        with self.assertRaises(RunStoreError):
            self.store.publish_run(parse_run_id="run-does-not-exist")
        with self.assertRaises(RunNotFoundError):
            self.store.set_execution_status(parse_run_id="run-does-not-exist", status="running")


class FencingTests(RunStoreTestCase):
    def test_stale_generation_is_rejected_and_the_old_version_survives(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")

        # A competitor that still believes generation 0 is current.
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:v2",
            claim_token="claim-b",
            expected_generation=0,
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-b"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block("blk-b", "v2"),),
                chunks=(_chunk("chk-b", ("blk-b",), "v2"),),
                views=_views("v2"),
            ),
            claim_token="claim-b",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token="claim-b"
        )
        self.store.seal_run(parse_run_id=run.parse_run_id, claim_token="claim-b")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")

        with self.assertRaises(StalePublicationError) as captured:
            self.store.publish_run(
                parse_run_id=run.parse_run_id,
                expected_generation=0,
                expected_source_version="sha256:v2",
                claim_token="claim-b",
            )
        self.assertEqual(captured.exception.code, PUBLICATION_REJECTED_STALE)

        # The published version and the live tier are untouched.
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], first["run_id"])
        self.assertEqual(state["publication_generation"], 1)
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])
        self.assertEqual(
            self.store.get_run(parse_run_id=run.parse_run_id).publication_status,
            PublicationStatus.UNPUBLISHED.value,
        )

    def test_stale_source_version_is_rejected(self) -> None:
        self._publish(text="v1", source_version="sha256:v1")
        with self.assertRaises(StalePublicationError):
            self._publish(
                text="v2",
                source_version="sha256:v2",
                expected_source_version="sha256:not-the-run-source",
                expected_generation=1,
                blocks=(_block("blk-2", "v2"),),
                chunks=(_chunk("chk-2", ("blk-2",), "v2"),),
            )
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])

    def test_claim_fencing_rejects_a_writer_that_lost_its_claim(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            claim_token="claim-owner",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-owner"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),), views=_views("x")),
            claim_token="claim-owner",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token="claim-owner"
        )
        self.store.seal_run(parse_run_id=run.parse_run_id, claim_token="claim-owner")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")

        # A lease-expired worker that still holds the old token must not publish.
        with self.assertRaises(PublishError) as captured:
            self.store.publish_run(parse_run_id=run.parse_run_id, claim_token="stale-claim")
        self.assertIn("claim fencing", str(captured.exception))
        self.assertIsNone(self.store.publication_state(doc_id=self.doc_id)["active_run_id"])

    def test_cancelled_run_cannot_be_published_by_a_late_worker(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id, claim_token="claim-1"
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-1"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("b", "x"),), views=_views("x")),
            claim_token="claim-1",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="cancelled", claim_token="claim-1"
        )
        with self.assertRaises(RunNotSealableError):
            self.store.seal_run(parse_run_id=run.parse_run_id, claim_token="claim-1")
        with self.assertRaises(PublishError):
            self.store.publish_run(parse_run_id=run.parse_run_id, claim_token="claim-1")


class FailureInjectionTests(RunStoreTestCase):
    """A failure before, during or after publication must not corrupt the old version."""

    def test_failure_before_sealing_leaves_the_previous_version_complete(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")

        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:v2",
            claim_token="claim-2",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-2"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(blocks=(_block("blk-2", "v2"),), views=_views("v2")),
            claim_token="claim-2",
        )
        # Process dies here: no seal, no publish.
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], first["run_id"])
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])
        self.assertEqual(
            self.store.get_run(parse_run_id=run.parse_run_id).publication_status,
            PublicationStatus.UNPUBLISHED.value,
        )

    def test_failure_during_the_publish_transaction_rolls_everything_back(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")

        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:v2",
            claim_token="claim-2",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-2"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block("blk-2", "v2"),),
                chunks=(_chunk("chk-2", ("blk-2",), "v2"),),
                views=_views("v2"),
            ),
            claim_token="claim-2",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token="claim-2"
        )
        self.store.seal_run(parse_run_id=run.parse_run_id, claim_token="claim-2")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")

        # Inject a failure inside the transaction, after the live tier has been
        # rewritten but before the pointer could move.
        original = self.store._record_event

        def boom(*args, **kwargs):
            if kwargs.get("event_type") == "run_published":
                raise sqlite3.OperationalError("injected failure during publish")
            return original(*args, **kwargs)

        self.store._record_event = boom  # type: ignore[method-assign]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.publish_run(
                    parse_run_id=run.parse_run_id,
                    expected_generation=1,
                    expected_source_version="sha256:v2",
                    claim_token="claim-2",
                )
        finally:
            self.store._record_event = original  # type: ignore[method-assign]

        # Rolled back: v1 is still live, the pointer still points at run 1, and
        # run 2 is still unpublished.
        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], first["run_id"])
        self.assertEqual(state["publication_generation"], 1)
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])
        self.assertEqual([chunk.text for chunk in self._live_chunks()], ["v1"])
        views = self.jobs.get_document_views(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(views["pages"][0]["text"], "v1")
        self.assertEqual(
            self.store.get_run(parse_run_id=run.parse_run_id).publication_status,
            PublicationStatus.UNPUBLISHED.value,
        )

    def test_tampered_staging_blocks_publication(self) -> None:
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id, claim_token="claim-1"
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-1"
        )
        self.store.save_run_artifacts(
            parse_run_id=run.parse_run_id,
            artifacts=RunArtifacts(
                blocks=(_block("b1", "x"),), chunks=(_chunk("c1", ("b1",), "x"),), views=_views("x")
            ),
            claim_token="claim-1",
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="succeeded", claim_token="claim-1"
        )
        self.store.seal_run(parse_run_id=run.parse_run_id, claim_token="claim-1")
        self.store.record_validation(parse_run_id=run.parse_run_id, status="passed")

        # Simulate out-of-band tampering with the sealed staging area.
        connection = sqlite3.connect(self.store._path)
        connection.execute(
            "UPDATE run_blocks SET payload_json = ? WHERE parse_run_id = ?",
            (
                json.dumps(
                    {
                        "block_id": "b1",
                        "type": "paragraph",
                        "content": "TAMPERED",
                        "metadata": {},
                    }
                ),
                run.parse_run_id,
            ),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(SealError) as captured:
            self.store.publish_run(
                parse_run_id=run.parse_run_id,
                expected_generation=0,
                claim_token="claim-1",
            )
        self.assertIn("hash", str(captured.exception))
        self.assertIsNone(self.store.publication_state(doc_id=self.doc_id)["active_run_id"])

    def test_a_failed_run_does_not_disturb_the_published_version(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")

        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        run = self.store.begin_run(
            job_id=job.job_id, doc_id=self.doc_id, tenant_id=self.tenant_id, claim_token="claim-2"
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="running", claim_token="claim-2"
        )
        self.store.set_execution_status(
            parse_run_id=run.parse_run_id, status="failed", claim_token="claim-2"
        )

        state = self.store.publication_state(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(state["active_run_id"], first["run_id"])
        self.assertTrue(state["formal_version_available"])
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])

    def test_a_retry_creates_a_new_run_and_does_not_reuse_the_old_artifacts(self) -> None:
        first = self._publish(text="attempt-1", source_version="sha256:1")

        # Same job id, new attempt: the contract requires a new run identity.
        job = self.jobs.create(
            ParseRequest(doc_id=self.doc_id, file_path="x.docx", tenant_id=self.tenant_id)
        )
        retry = self.store.begin_run(
            job_id=job.job_id,
            doc_id=self.doc_id,
            tenant_id=self.tenant_id,
            source_version="sha256:1",
            claim_token="claim-retry",
            attempt_count=2,
            base_run_id=str(first["run_id"]),
        )
        self.assertNotEqual(retry.parse_run_id, first["run_id"])
        self.assertEqual(retry.base_run_id, first["run_id"])
        with self.store._connect() as connection:
            staged = connection.execute(
                "SELECT COUNT(*) FROM run_blocks WHERE parse_run_id = ?",
                (retry.parse_run_id,),
            ).fetchone()[0]
        self.assertEqual(staged, 0)
        self.assertEqual(
            self.store.get_run(parse_run_id=str(first["run_id"])).publication_status,
            PublicationStatus.PUBLISHED.value,
        )


class OutboxTests(RunStoreTestCase):
    def test_publication_writes_a_pending_event(self) -> None:
        result = self._publish(text="x")
        events = self.store.pending_publication_events()
        published = [event for event in events if event["event_type"] == "run_published"]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["parse_run_id"], result["run_id"])
        self.assertEqual(published[0]["publication_generation"], 1)

        self.assertTrue(
            self.store.mark_publication_event_dispatched(event_id=published[0]["event_id"])
        )
        # Dispatching twice is a no-op rather than a duplicate notification.
        self.assertFalse(
            self.store.mark_publication_event_dispatched(event_id=published[0]["event_id"])
        )
        remaining = [
            event["event_type"] for event in self.store.pending_publication_events()
        ]
        self.assertNotIn("run_published", remaining)

    def test_a_rolled_back_publish_leaves_no_run_published_event(self) -> None:
        with self.assertRaises(PublishError):
            self._publish(validation_status="needs_review")
        event_types = [
            event["event_type"] for event in self.store.pending_publication_events()
        ]
        self.assertNotIn("run_published", event_types)


class RunHistoryTests(RunStoreTestCase):
    def test_restore_rebuilds_the_live_tier_from_the_active_run(self) -> None:
        published = self._publish(text="v1", source_version="sha256:v1")

        # Simulate an operator rollback: the publish transaction rewrites the
        # live tier, so restoring means re-applying the active run's sealed
        # artifacts rather than trusting whatever is live now.
        with self.store._write_transaction() as conn:
            conn.execute("DELETE FROM blocks WHERE doc_id = ?", (self.doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (self.doc_id,))
            conn.execute("DELETE FROM document_views WHERE doc_id = ?", (self.doc_id,))
        self.assertEqual(self._live_blocks(), ())

        result = self.store.restore_live_from_run(doc_id=self.doc_id, tenant_id=self.tenant_id)

        self.assertTrue(result["restored"])
        self.assertEqual(result["active_run_id"], published["run_id"])
        self.assertEqual([block.content for block in self._live_blocks()], ["v1"])
        self.assertEqual([chunk.text for chunk in self._live_chunks()], ["v1"])
        views = self.jobs.get_document_views(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual(views["pages"][0]["text"], "v1")

    def test_restore_is_a_no_op_without_an_active_run(self) -> None:
        result = self.store.restore_live_from_run(doc_id="never-published", tenant_id=self.tenant_id)
        self.assertFalse(result["restored"])
        self.assertEqual(result["reason"], "no_active_run")

    def test_restore_refuses_to_rebuild_from_tampered_staging(self) -> None:
        self._publish(text="v1", source_version="sha256:v1")
        connection = sqlite3.connect(self.store._path)
        connection.execute("UPDATE run_blocks SET payload_json = '{}'")
        connection.commit()
        connection.close()

        with self.assertRaises(SealError):
            self.store.restore_live_from_run(doc_id=self.doc_id, tenant_id=self.tenant_id)

    def test_list_runs_is_newest_first(self) -> None:
        first = self._publish(text="v1", source_version="sha256:v1")
        second = self._publish(
            text="v2",
            source_version="sha256:v2",
            expected_generation=1,
            blocks=(_block("blk-2", "v2"),),
            chunks=(_chunk("chk-2", ("blk-2",), "v2"),),
        )
        runs = self.store.list_runs(doc_id=self.doc_id, tenant_id=self.tenant_id)
        self.assertEqual({run.parse_run_id for run in runs}, {first["run_id"], second["run_id"]})
        self.assertEqual(len(runs), 2)

    def test_state_machine_helpers_agree_with_the_frozen_tables(self) -> None:
        self.assertFalse(
            can_transition("execution_status", ExecutionStatus.SUCCEEDED, ExecutionStatus.RUNNING)
        )
        self.assertTrue(
            can_transition("validation_status", ValidationStatus.NOT_RUN, ValidationStatus.PASSED)
        )
        self.assertFalse(
            can_transition("publication_status", PublicationStatus.WITHDRAWN, PublicationStatus.PUBLISHED)
        )


class StoreSelectionTests(unittest.TestCase):
    def test_run_store_is_only_built_for_supported_backends(self) -> None:
        self.assertIsNone(run_store_for("postgresql://localhost/db"))
        self.assertIsNone(run_store_for("memory://"))
        with TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'db.sqlite').as_posix()}"
            self.assertIsInstance(run_store_for(url), SQLiteRunStore)

    def test_publication_ready_requires_the_required_capabilities(self) -> None:
        self.assertFalse(publication_ready(None))
        self.assertFalse(publication_ready(object()))

        class Partial:
            def run_store_capabilities(self):
                from parsecore.publication import RunStoreCapabilities

                return RunStoreCapabilities(supported=("staged_artifacts",))

        self.assertFalse(publication_ready(Partial()))

        with TemporaryDirectory() as tmp:
            url = f"sqlite:///{(Path(tmp) / 'db.sqlite').as_posix()}"
            self.assertTrue(publication_ready(SQLiteRunStore(url)))


if __name__ == "__main__":
    unittest.main()
