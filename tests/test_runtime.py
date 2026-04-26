from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, Chunk, ParseJobState, ParseRequest, SemanticRole
from parsecore.runtime import QuotaExceededError
from parsecore.stubs import FakeEmbeddingProvider
from tests.support import TemporaryWorkspace


SAMPLE_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

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


PDF_SAMPLE_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

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
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
""".strip()


EMBEDDING_SAMPLE_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

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

[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "text-embedding-3-small"
base_url = "https://example.invalid/v1"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


QUOTA_ENFORCED_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

[runtime]
quota_enforce = true
quota_window_hours = 24
quota_default_limit_units = 3

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


class ParseRuntimeTests(unittest.TestCase):
    def test_describe_returns_registered_parsers(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        self.assertEqual(description["project"], "test-parsecore")
        self.assertEqual(description["parsers"], ["docx-native"])

    def test_submit_finishes_with_done_state(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Engine Manual", "Inspection procedure"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-001",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        self.assertGreaterEqual(len(outcome.blocks), 2)
        self.assertEqual(len(outcome.chunks), len(outcome.blocks))
        self.assertEqual(outcome.blocks[1].content, "Engine Manual")
        self.assertEqual(outcome.blocks[0].metadata["semantic_role"], SemanticRole.TITLE.value)
        self.assertEqual(outcome.chunks[0].semantic_role, SemanticRole.TITLE.value)
        self.assertEqual(outcome.chunks[1].semantic_role, SemanticRole.PARAGRAPH.value)

    def test_submit_records_ocr_observability_events(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("ocr.docx", ["Engine Manual"])
            runtime = build_runtime(workspace.config_path)
            blocks = (
                Block(
                    block_id="blk-1",
                    doc_id="doc-ocr",
                    type=BlockType.PARAGRAPH,
                    content="native text kept",
                    metadata={
                        "page": 1,
                        "ocr_attempted": True,
                        "ocr_attempt_reason": "empty_text",
                        "ocr_error_reason": "provider_request_failed",
                    },
                ),
                Block(
                    block_id="blk-2",
                    doc_id="doc-ocr",
                    type=BlockType.PARAGRAPH,
                    content="ocr recovered text",
                    metadata={
                        "page": 2,
                        "ocr_attempted": True,
                        "ocr_attempt_reason": "cid_dense",
                        "ocr_fallback_used": True,
                        "ocr_fallback_reason": "cid_dense",
                    },
                ),
            )
            with patch.object(runtime, "_load_blocks_for_request", return_value=blocks), patch.object(
                runtime,
                "_load_chunks_for_request",
                return_value=(),
            ), patch.object(runtime, "_embed_chunks", return_value=()):
                runtime.submit(
                    ParseRequest(
                        doc_id="doc-ocr",
                        file_path=str(document_path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        tenant_id="tenant-ocr",
                        quota_key="ocr-plan",
                    )
                )

        attempted = runtime.event_aggregator.get_events(limit=10, event_type_filter="ocr_attempted")
        fallback = runtime.event_aggregator.get_events(limit=10, event_type_filter="ocr_fallback")
        failed = runtime.event_aggregator.get_events(limit=10, event_type_filter="ocr_failed")

        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0]["tenant_id"], "tenant-ocr")
        self.assertEqual(attempted[0]["quota_key"], "ocr-plan")
        self.assertEqual(attempted[0]["page_count"], 2)
        self.assertEqual(attempted[0]["block_count"], 2)
        self.assertEqual(attempted[0]["attempt_reasons"], ["cid_dense", "empty_text"])

        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0]["page_count"], 1)
        self.assertEqual(fallback[0]["block_count"], 1)

        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["page_count"], 1)
        self.assertEqual(failed[0]["block_count"], 1)
        self.assertEqual(failed[0]["error_reasons"], ["provider_request_failed"])

        counters = runtime.event_aggregator.get_counters()
        self.assertEqual(counters["tenant-ocr:ocr-plan:ocr_attempted"], 2)
        self.assertEqual(counters["tenant-ocr:ocr-plan:ocr_fallback"], 1)
        self.assertEqual(counters["tenant-ocr:ocr-plan:ocr_failed"], 1)

    def test_submit_persists_job_and_document_snapshot(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("manual.docx", ["Revision A", "Replace filter"])
            runtime = build_runtime(workspace.config_path)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="doc-002",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-acme",
                    quota_key="starter",
                    quota_units=2,
                )
            )
            rebuilt = build_runtime(workspace.config_path)
            job = rebuilt.get_job(job_id=outcome.job.job_id)
            document = rebuilt.get_document(doc_id="doc-002")

        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.doc_id, "doc-002")
        self.assertEqual(job.state, ParseJobState.DONE)
        self.assertEqual(job.tenant_id, "tenant-acme")
        self.assertEqual(job.quota_key, "starter")
        self.assertEqual(job.quota_units, 2)
        self.assertEqual(document["job"].job_id, outcome.job.job_id)
        self.assertEqual(len(document["blocks"]), len(outcome.blocks))
        self.assertEqual(len(document["chunks"]), len(outcome.chunks))

    def test_retry_latest_reuses_last_request(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("revision.docx", ["Task Card", "Install panel"])
            runtime = build_runtime(workspace.config_path)
            first = runtime.submit(
                ParseRequest(
                    doc_id="doc-003",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-bravo",
                    quota_key="batch",
                    quota_units=5,
                )
            )
            second = runtime.retry_latest(doc_id="doc-003")

        self.assertNotEqual(first.job.job_id, second.job.job_id)
        self.assertEqual(second.job.doc_id, "doc-003")
        self.assertEqual(second.job.file_path, str(document_path))
        self.assertEqual(second.job.tenant_id, "tenant-bravo")
        self.assertEqual(second.job.quota_key, "batch")
        self.assertEqual(second.job.quota_units, 5)

    def test_list_jobs_and_quota_usage_support_tenant_filters(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_a = workspace.create_docx("tenant-a.docx", ["A"])
            document_b = workspace.create_docx("tenant-b.docx", ["B"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-tenant-a",
                    file_path=str(document_a),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-a",
                    quota_key="starter",
                    quota_units=2,
                )
            )
            runtime.submit(
                ParseRequest(
                    doc_id="doc-tenant-b",
                    file_path=str(document_b),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-b",
                    quota_key="pro",
                    quota_units=4,
                )
            )

            tenant_a_jobs = runtime.list_jobs(tenant_id="tenant-a")
            usage_all = runtime.quota_usage()
            usage_tenant_a = runtime.quota_usage(tenant_id="tenant-a")

        self.assertEqual(len(tenant_a_jobs), 1)
        self.assertEqual(tenant_a_jobs[0].tenant_id, "tenant-a")
        self.assertEqual(usage_all["total_jobs"], 2)
        self.assertEqual(usage_all["total_quota_units"], 6)
        self.assertEqual(usage_tenant_a["tenant_id"], "tenant-a")
        self.assertEqual(usage_tenant_a["total_jobs"], 1)
        self.assertEqual(usage_tenant_a["total_quota_units"], 2)

    def test_quota_enforcement_rejects_request_when_limit_exceeded(self) -> None:
        with TemporaryWorkspace(QUOTA_ENFORCED_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            doc_a = workspace.create_docx("quota-a.docx", ["A"])
            doc_b = workspace.create_docx("quota-b.docx", ["B"])
            runtime.submit(
                ParseRequest(
                    doc_id="doc-quota-a",
                    file_path=str(doc_a),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-q",
                    quota_key="starter",
                    quota_units=2,
                )
            )

            with self.assertRaises(QuotaExceededError):
                runtime.submit(
                    ParseRequest(
                        doc_id="doc-quota-b",
                        file_path=str(doc_b),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        tenant_id="tenant-q",
                        quota_key="starter",
                        quota_units=2,
                    )
                )

    def test_document_and_reparse_are_isolated_by_tenant(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("tenant-doc.docx", ["Tenant content"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-tenant-iso",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-iso-a",
                    quota_key="starter",
                    quota_units=1,
                )
            )

            tenant_a_snapshot = runtime.get_document(doc_id="doc-tenant-iso", tenant_id="tenant-iso-a")
            tenant_b_snapshot = runtime.get_document(doc_id="doc-tenant-iso", tenant_id="tenant-iso-b")

            self.assertIsNotNone(tenant_a_snapshot["job"])
            self.assertIsNone(tenant_b_snapshot["job"])
            self.assertEqual(tuple(tenant_b_snapshot["blocks"]), ())
            self.assertEqual(tuple(tenant_b_snapshot["chunks"]), ())

            with self.assertRaises(LookupError):
                runtime.restart_latest(doc_id="doc-tenant-iso", tenant_id="tenant-iso-b")

    def test_runtime_metrics_returns_failure_rate_and_duration_summary(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            # 成功任务
            success_doc = workspace.create_docx("metrics-ok.docx", ["ok"])
            runtime.submit(
                ParseRequest(
                    doc_id="doc-metrics-ok",
                    file_path=str(success_doc),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-metrics",
                )
            )
            # 失败任务（文件不存在）
            failed_job = runtime.start(
                ParseRequest(
                    doc_id="doc-metrics-fail",
                    file_path=str(workspace.root / "missing-metrics.docx"),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-metrics",
                )
            )
            with self.assertRaises(Exception):
                runtime.execute(job_id=failed_job.job_id)

            metrics = runtime.runtime_metrics(tenant_id="tenant-metrics", sample_size=50, since_hours=24)

        self.assertEqual(metrics["tenant_id"], "tenant-metrics")
        self.assertEqual(metrics["since_hours"], 24.0)
        self.assertGreaterEqual(metrics["total_jobs"], 2)
        self.assertGreaterEqual(metrics["done_jobs"], 1)
        self.assertGreaterEqual(metrics["failed_jobs"], 1)
        self.assertGreater(metrics["failure_rate"], 0.0)
        self.assertIn("durations_s", metrics)
        self.assertGreaterEqual(metrics["durations_s"]["count"], 1)
        self.assertGreaterEqual(metrics["durations_s"]["p99"], metrics["durations_s"]["p50"])

    def test_tenant_dashboard_aggregates_usage_metrics_and_recent_jobs(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            doc_a = workspace.create_docx("dash-a.docx", ["A"])
            doc_b = workspace.create_docx("dash-b.docx", ["B"])
            runtime.submit(
                ParseRequest(
                    doc_id="doc-dash-a",
                    file_path=str(doc_a),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-dashboard",
                    quota_key="starter",
                    quota_units=2,
                )
            )
            runtime.submit(
                ParseRequest(
                    doc_id="doc-dash-b",
                    file_path=str(doc_b),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-dashboard",
                    quota_key="starter",
                    quota_units=3,
                )
            )

            dashboard = runtime.tenant_dashboard(
                tenant_id="tenant-dashboard",
                sample_size=50,
                recent_limit=2,
                since_hours=24,
            )

        self.assertEqual(dashboard["tenant_id"], "tenant-dashboard")
        self.assertEqual(dashboard["since_hours"], 24.0)
        self.assertEqual(dashboard["usage"]["tenant_id"], "tenant-dashboard")
        self.assertEqual(dashboard["metrics"]["tenant_id"], "tenant-dashboard")
        self.assertGreaterEqual(dashboard["usage"]["total_jobs"], 2)
        self.assertGreaterEqual(dashboard["metrics"]["done_jobs"], 2)
        self.assertLessEqual(len(dashboard["recent_jobs"]), 2)

    def test_since_hours_filter_can_exclude_old_jobs(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            doc = workspace.create_docx("old-job.docx", ["old"])
            runtime.submit(
                ParseRequest(
                    doc_id="doc-old",
                    file_path=str(doc),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    tenant_id="tenant-old",
                )
            )
            filtered = runtime.list_jobs(tenant_id="tenant-old", since_hours=0)

        self.assertEqual(filtered, ())

    def test_pdf_submit_splits_page_text_into_multiple_blocks(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class FakeReader:
            def __init__(self, file_path: str) -> None:
                self.pages = [
                    FakePage("Heading line\nDetail line\n\nStep A\nStep B"),
                    FakePage("Final note"),
                ]

        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_text_file("sample.pdf", "placeholder")
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.parsers._load_pdf_reader", return_value=FakeReader):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="pdf-001",
                        file_path=str(document_path),
                        media_type="application/pdf",
                    )
                )

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        self.assertEqual(len(outcome.blocks), 4)
        self.assertEqual(outcome.blocks[1].content, "Heading line\nDetail line")
        self.assertEqual(outcome.blocks[1].metadata["page"], 1)
        self.assertEqual(outcome.blocks[2].content, "Step A\nStep B")
        self.assertEqual(outcome.blocks[2].metadata["page_position"], 2)
        self.assertEqual(outcome.blocks[3].metadata["page"], 2)
        self.assertEqual(outcome.blocks[1].metadata["semantic_role"], SemanticRole.PARAGRAPH.value)

    def test_pdf_submit_tags_toc_entries_with_semantic_role(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class FakeReader:
            def __init__(self, file_path: str) -> None:
                self.pages = [
                    FakePage(
                        "TABLE OF CONTENTS\n"
                        "A .................. 1\n"
                        "B .................. 2\n"
                        "C .................. 3"
                    )
                ]

        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_text_file("sample.pdf", "placeholder")
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.parsers._load_pdf_reader", return_value=FakeReader):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="pdf-002",
                        file_path=str(document_path),
                        media_type="application/pdf",
                    )
                )

        toc_roles = [
            block.metadata["semantic_role"]
            for block in outcome.blocks[1:]
        ]
        self.assertTrue(toc_roles)
        self.assertTrue(all(role == SemanticRole.TOC_ENTRY.value for role in toc_roles))
        self.assertTrue(all(chunk.semantic_role == SemanticRole.TOC_ENTRY.value for chunk in outcome.chunks[1:]))

    def test_submit_applies_embedding_provider_when_enabled(self) -> None:
        with TemporaryWorkspace(EMBEDDING_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("embed.docx", ["Line 1", "Line 2"])
            with patch(
                "parsecore.bootstrap.build_embedding_provider",
                return_value=FakeEmbeddingProvider(),
            ):
                runtime = build_runtime(workspace.config_path)
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-embed",
                        file_path=str(document_path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )

        self.assertTrue(all(chunk.embedding is not None for chunk in outcome.chunks))
        self.assertEqual(len(outcome.chunks[0].embedding), 1536)
        self.assertEqual(
            outcome.chunks[0].embedding[:2],
            (1.0, float(len(outcome.chunks[0].text))),
        )

    def test_embed_chunks_retries_per_batch_and_degrades_partially(self) -> None:
        class SelectiveFailureEmbeddingProvider:
            def __init__(self) -> None:
                self.fail_calls = 0

            def embed(self, *, doc_id: str, chunks):
                if any("FAIL" in (chunk.text or "") for chunk in chunks):
                    self.fail_calls += 1
                    raise RuntimeError("embedding unavailable for FAIL batch")
                return tuple(
                    replace(chunk, embedding=(1.0, float(len(chunk.text))))
                    for chunk in chunks
                )

        config = EMBEDDING_SAMPLE_CONFIG.replace(
            'api_key_env = "PARSECORE_EMBEDDING_API_KEY"',
            'api_key_env = "PARSECORE_EMBEDDING_API_KEY"\n'
            'batch_size = 1\n'
            'max_retries = 1',
        )
        with TemporaryWorkspace(config) as workspace:
            document_path = workspace.create_docx("embed-partial.docx", ["FAIL segment", "OK segment"])
            provider = SelectiveFailureEmbeddingProvider()
            with patch(
                "parsecore.bootstrap.build_embedding_provider",
                return_value=provider,
            ):
                runtime = build_runtime(workspace.config_path)
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="doc-embed-partial",
                        file_path=str(document_path),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                )

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        embedded_count = sum(1 for chunk in outcome.chunks if chunk.embedding is not None)
        skipped_count = sum(1 for chunk in outcome.chunks if chunk.embedding is None)
        self.assertGreaterEqual(embedded_count, 1)
        self.assertGreaterEqual(skipped_count, 1)
        self.assertGreaterEqual(provider.fail_calls, 2)

    def test_submit_rerun_chunks_only_reuses_saved_blocks(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("rerun.docx", ["Base text", "Second line"])
            runtime = build_runtime(workspace.config_path)
            first = runtime.submit(
                ParseRequest(
                    doc_id="doc-rerun",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            rerun = runtime.submit(
                ParseRequest(
                    doc_id="doc-rerun",
                    file_path=str(workspace.root / "missing.docx"),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"mode": "rerun_chunks_only"},
                )
            )

        self.assertEqual(rerun.job.state, ParseJobState.DONE)
        self.assertEqual(
            [block.content for block in rerun.blocks],
            [block.content for block in first.blocks],
        )
        self.assertEqual(
            [chunk.text for chunk in rerun.chunks],
            [chunk.text for chunk in first.chunks],
        )

    def test_search_document_weights_and_filters_by_semantic_role(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-search",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_chunks(
                doc_id="doc-search",
                chunks=[
                    Chunk(
                        chunk_id="title-hit",
                        doc_id="doc-search",
                        block_ids=("blk-1",),
                        text="Hydraulic pressure warning summary",
                        semantic_role=SemanticRole.TITLE.value,
                    ),
                    Chunk(
                        chunk_id="toc-hit",
                        doc_id="doc-search",
                        block_ids=("blk-2",),
                        text="Hydraulic pressure warning procedures",
                        semantic_role=SemanticRole.TOC_ENTRY.value,
                    ),
                    Chunk(
                        chunk_id="warning-hit",
                        doc_id="doc-search",
                        block_ids=("blk-3",),
                        text="WARNING: Hydraulic pressure warning before maintenance.",
                        semantic_role=SemanticRole.WARNING.value,
                    ),
                ],
            )

            hits = runtime.search_document(doc_id="doc-search", query="hydraulic pressure warning")
            warning_only = runtime.search_document(
                doc_id="doc-search",
                query="hydraulic pressure warning",
                semantic_roles=[SemanticRole.WARNING.value],
            )

        self.assertEqual(hits[0].semantic_role, SemanticRole.TITLE.value)
        self.assertEqual(hits[-1].semantic_role, SemanticRole.TOC_ENTRY.value)
        self.assertEqual(len(warning_only), 1)
        self.assertEqual(warning_only[0].semantic_role, SemanticRole.WARNING.value)

    def test_search_document_uses_vector_priority_with_keyword_fallback(self) -> None:
        class QueryEmbeddingProvider:
            def embed(self, *, doc_id: str, chunks):
                from dataclasses import replace

                return tuple(
                    replace(chunk, embedding=(1.0, 0.0)) for chunk in chunks
                )

        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search-hybrid.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.embedding_provider = QueryEmbeddingProvider()
            runtime.submit(
                ParseRequest(
                    doc_id="doc-hybrid",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_chunks(
                doc_id="doc-hybrid",
                chunks=[
                    Chunk(
                        chunk_id="keyword-strong",
                        doc_id="doc-hybrid",
                        block_ids=("blk-1",),
                        text="Hydraulic pressure warning procedures and checklist",
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(0.0, 1.0),
                    ),
                    Chunk(
                        chunk_id="vector-strong",
                        doc_id="doc-hybrid",
                        block_ids=("blk-2",),
                        text="Safety bulletin summary",
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(1.0, 0.0),
                    ),
                ],
            )

            hits = runtime.search_document(
                doc_id="doc-hybrid",
                query="hydraulic pressure warning",
            )
            hits_with_mode, mode = runtime.search_document_with_mode(
                doc_id="doc-hybrid",
                query="hydraulic pressure warning",
            )

        self.assertEqual(hits[0].chunk_id, "vector-strong")
        self.assertEqual(hits[1].chunk_id, "keyword-strong")
        self.assertEqual(mode, "hybrid")
        self.assertEqual(hits_with_mode[0].chunk_id, "vector-strong")

    def test_search_document_reports_keyword_fallback_mode_when_query_embedding_unavailable(self) -> None:
        class FailingQueryEmbeddingProvider:
            def embed(self, *, doc_id: str, chunks):
                raise RuntimeError("embedding unavailable")

        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search-fallback.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.embedding_provider = FailingQueryEmbeddingProvider()
            runtime.submit(
                ParseRequest(
                    doc_id="doc-fallback",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            hits_with_mode, mode = runtime.search_document_with_mode(
                doc_id="doc-fallback",
                query="base",
            )

        self.assertEqual(mode, "keyword-fallback")
        self.assertTrue(hits_with_mode)
