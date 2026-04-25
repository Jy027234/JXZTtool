from __future__ import annotations

import unittest

from parsecore.jobcard import JobcardProductAdapter, build_jobcard_document_patch, mount_into_fastapi
from parsecore.models import Block, BlockType, Chunk, ParseJob, ParseJobState, ParseOutcome, ParseRequest


class JobcardIntegrationTests(unittest.TestCase):
    def test_build_jobcard_document_patch_maps_parsecore_result(self) -> None:
        outcome = ParseOutcome(
            job=ParseJob(
                job_id="job-001",
                doc_id="doc-001",
                file_path="samples/spec.docx",
                state=ParseJobState.DONE,
                updated_at="2026-04-23T00:00:00+00:00",
            ),
            blocks=(
                Block(
                    block_id="blk-001",
                    doc_id="doc-001",
                    type=BlockType.TITLE,
                    content="AMM 72-00-00",
                    metadata={"page": 1},
                ),
                Block(
                    block_id="blk-002",
                    doc_id="doc-001",
                    type=BlockType.PARAGRAPH,
                    content="Inspection procedure",
                    metadata={"page": 1},
                ),
            ),
            chunks=(
                Chunk(
                    chunk_id="chk-001",
                    doc_id="doc-001",
                    block_ids=("blk-001", "blk-002"),
                    text="AMM 72-00-00 Inspection procedure",
                ),
            ),
        )

        patch = build_jobcard_document_patch(outcome)

        self.assertEqual(patch["parseStatus"], "PARSED")
        self.assertEqual(patch["parsedTextContent"]["totalPages"], 1)
        self.assertEqual(len(patch["parsedTextContent"]["pages"]), 1)
        self.assertEqual(patch["parsecore"]["job"]["job_id"], "job-001")

    def test_build_jobcard_document_patch_preserves_content_spacing_and_skips_title(self) -> None:
        outcome = ParseOutcome(
            job=ParseJob(
                job_id="job-002",
                doc_id="doc-002",
                file_path="samples/spec.pdf",
                state=ParseJobState.DONE,
                updated_at="2026-04-23T00:00:00+00:00",
            ),
            blocks=(
                Block(
                    block_id="blk-title",
                    doc_id="doc-002",
                    type=BlockType.TITLE,
                    content="sample",
                    metadata={"page": 1},
                ),
                Block(
                    block_id="blk-101",
                    doc_id="doc-002",
                    type=BlockType.PARAGRAPH,
                    content="TO:  Holders\n Electrical Harness",
                    metadata={"page": 1},
                ),
                Block(
                    block_id="blk-102",
                    doc_id="doc-002",
                    type=BlockType.PARAGRAPH,
                    content="SUBJECT:  Revision Notice",
                    metadata={"page": 1},
                ),
            ),
            chunks=(
                Chunk(
                    chunk_id="chk-002",
                    doc_id="doc-002",
                    block_ids=("blk-101", "blk-102"),
                    text="TO:  Holders\n Electrical Harness\n\nSUBJECT:  Revision Notice",
                ),
            ),
        )

        patch = build_jobcard_document_patch(outcome)

        self.assertEqual(
            patch["parsedTextContent"]["plainText"],
            "TO:  Holders\n Electrical Harness\n\nSUBJECT:  Revision Notice",
        )
        self.assertEqual(patch["parsedTextContent"]["pages"][0]["blockIds"], ["blk-101", "blk-102"])
        self.assertEqual(
            patch["parsedTextContent"]["pages"][0]["text"],
            "TO:  Holders\n Electrical Harness\n\nSUBJECT:  Revision Notice",
        )

    def test_jobcard_adapter_emits_patch_events(self) -> None:
        captured: list[tuple[str, dict]] = []
        adapter = JobcardProductAdapter(lambda doc_id, patch: captured.append((doc_id, patch)))
        request = ParseRequest(doc_id="doc-010", file_path="samples/spec.docx")
        job = ParseJob(job_id="job-010", doc_id="doc-010", file_path="samples/spec.docx", state=ParseJobState.PENDING)

        adapter.before_parse(request=request, job=job)

        self.assertEqual(len(adapter.events), 1)
        self.assertEqual(captured[0][0], "doc-010")
        self.assertEqual(captured[0][1]["parseStatus"], "PARSING")

    def test_mount_into_fastapi_mounts_sub_app(self) -> None:
        mounted: list[tuple[str, object]] = []

        class FakeApp:
            def mount(self, path: str, app: object) -> None:
                mounted.append((path, app))

        mount_into_fastapi(FakeApp(), config_path="parsecore.toml", prefix="/internal/parsecore")
        self.assertEqual(mounted[0][0], "/internal/parsecore")