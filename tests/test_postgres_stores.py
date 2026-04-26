"""Postgres + pgvector store smoke tests.

These tests are skipped unless ``PARSECORE_TEST_POSTGRES_URL`` is set to a
reachable database. The intent is parity with ``test_runtime`` for the
SQLite path: create a job, save blocks/chunks, claim, list, and exercise
``PgVectorIndex.upsert``.
"""

from __future__ import annotations

import os
import unittest
import uuid

from parsecore.models import Block, BlockType, Chunk, ParseJobState, ParseRequest
from parsecore.stores import PgVectorIndex, PostgresJobStore


_PG_URL = os.environ.get("PARSECORE_TEST_POSTGRES_URL")


@unittest.skipUnless(_PG_URL, "PARSECORE_TEST_POSTGRES_URL not set")
class PostgresJobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PostgresJobStore(_PG_URL)
        self.doc_id = f"doc-{uuid.uuid4().hex[:10]}"

    def test_create_and_get_job(self) -> None:
        request = ParseRequest(
            doc_id=self.doc_id,
            file_path="/tmp/x.pdf",
            media_type="application/pdf",
            options={"foo": "bar"},
            tenant_id="tenant-pg",
            quota_key="pro",
            quota_units=4,
        )
        job = self.store.create(request)
        self.assertEqual(job.state, ParseJobState.PENDING)
        fetched = self.store.get_job(job_id=job.job_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.options, {"foo": "bar"})
        self.assertEqual(fetched.tenant_id, "tenant-pg")
        self.assertEqual(fetched.quota_key, "pro")
        self.assertEqual(fetched.quota_units, 4)

    def test_save_and_read_blocks_chunks(self) -> None:
        request = ParseRequest(
            doc_id=self.doc_id,
            file_path="/tmp/x.pdf",
            media_type="application/pdf",
            options={},
        )
        self.store.create(request)
        block = Block(
            block_id="blk-1",
            doc_id=self.doc_id,
            type=BlockType.PARAGRAPH,
            content="hello",
            metadata={"page": 1},
        )
        self.store.save_blocks(doc_id=self.doc_id, blocks=[block])
        chunk = Chunk(
            chunk_id="chk-1",
            doc_id=self.doc_id,
            block_ids=("blk-1",),
            text="hello",
        )
        self.store.save_chunks(doc_id=self.doc_id, chunks=[chunk])
        self.assertEqual(len(self.store.get_blocks(doc_id=self.doc_id)), 1)
        self.assertEqual(len(self.store.get_chunks(doc_id=self.doc_id)), 1)

    def test_claim_next_job_round_trip(self) -> None:
        request = ParseRequest(
            doc_id=self.doc_id,
            file_path="/tmp/x.pdf",
            media_type="application/pdf",
            options={},
        )
        created = self.store.create(request)
        claimed = self.store.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.job_id, created.job_id)
        self.assertEqual(claimed.state, ParseJobState.PARSING)


@unittest.skipUnless(_PG_URL, "PARSECORE_TEST_POSTGRES_URL not set")
class PgVectorIndexTests(unittest.TestCase):
    def test_upsert_round_trip(self) -> None:
        index = PgVectorIndex(_PG_URL, dim=4)
        doc_id = f"doc-{uuid.uuid4().hex[:10]}"
        chunks = [
            Chunk(
                chunk_id=f"chk-{i}",
                doc_id=doc_id,
                block_ids=(f"blk-{i}",),
                text=f"t{i}",
                embedding=(0.1 * i, 0.2, 0.3, 0.4),
            )
            for i in range(3)
        ]
        index.upsert(doc_id=doc_id, chunks=chunks)
        # Re-upsert with no embeddings should clear rows for the doc.
        index.upsert(doc_id=doc_id, chunks=[])

    def test_dim_mismatch_raises(self) -> None:
        index = PgVectorIndex(_PG_URL, dim=4)
        doc_id = f"doc-{uuid.uuid4().hex[:10]}"
        bad = Chunk(
            chunk_id="chk-bad",
            doc_id=doc_id,
            block_ids=("blk-bad",),
            text="x",
            embedding=(0.1, 0.2),  # dim=2
        )
        with self.assertRaises(ValueError):
            index.upsert(doc_id=doc_id, chunks=[bad])


if __name__ == "__main__":
    unittest.main()
