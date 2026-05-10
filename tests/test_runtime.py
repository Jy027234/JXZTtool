from __future__ import annotations

from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch

from parsecore.api_payloads import _document_projection
from parsecore.bootstrap import build_runtime
from parsecore.cli import main as cli_main
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


QUEUE_RETRY_CONFIG = """
[project]
name = "test-parsecore"
mode = "embedded-sdk"

[runtime]
execution_mode = "queue-worker"
max_attempts = 2
retry_backoff_seconds = 0
retry_backoff_max_seconds = 0
job_timeout_seconds = 1

[storage]
database_url = "memory://"
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


PDF_TABLE_STAGE_CONFIG = """
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
options = { enrichment = { table_structure = { enabled = true, header_rows = 1, output_format = "markdown" } } }
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
        self.assertEqual(description["index_layers"], ["primary", "structure", "high_precision"])
        self.assertIn("pipelines", description)
        self.assertIn("pipeline_cache", description)
        self.assertEqual(description["pipeline_cache"]["size"], 1)

    def test_describe_normalizes_legacy_jobcard_adapter_to_embedded(self) -> None:
        legacy_config = SAMPLE_CONFIG.replace('adapter = "embedded"', 'adapter = "jobcard"')
        with TemporaryWorkspace(legacy_config) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        self.assertEqual(description["product_adapter"], "embedded")

    def test_pipeline_registry_describes_format_backend_stage_matrix(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        pipelines = description["pipelines"]
        self.assertEqual(len(pipelines), 1)
        pipeline = pipelines[0]
        self.assertEqual(pipeline["name"], "pdf-text/default")
        self.assertEqual(pipeline["format"], "pdf")
        self.assertEqual(pipeline["backend"], "native-text")
        self.assertIn("normalized-items", pipeline["stages"])
        self.assertIn("table-structure", pipeline["runtime_stages"])
        self.assertIn("layout-reading-order", pipeline["parser_backed_stages"])
        self.assertIn("table-detection", pipeline["parser_backed_stages"])
        self.assertIn("ocr-fallback", pipeline["parser_backed_stages"])
        self.assertEqual(pipeline["chunker"], "artifact-chunker")

    def test_pipeline_registry_warmup_and_artifact_chunking_are_active(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("artifact.docx", ["Engine Manual", "Inspection procedure"])
            runtime = build_runtime(workspace.config_path)
            self.assertIsNotNone(runtime.pipeline_registry)
            request = ParseRequest(
                doc_id="doc-artifact",
                file_path=str(document_path),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

            pipeline = runtime.pipeline_registry.resolve(request, purpose="parse")
            blocks = pipeline.parse_blocks(request=request)
            artifact = pipeline.build_document(request=request, blocks=blocks)
            chunks = pipeline.build_chunks(request=request, blocks=blocks)
            description = runtime.describe()

        self.assertGreaterEqual(description["pipeline_cache"]["hits"], 1)
        self.assertEqual(artifact.metadata["summary"]["item_count"], len(blocks))
        self.assertEqual(artifact.items[0].kind, SemanticRole.TITLE.value)
        self.assertEqual(artifact.items[0].semantic_role, SemanticRole.TITLE.value)
        self.assertIn("role:title", artifact.items[0].metadata["structure_tags"])
        self.assertEqual(artifact.metadata["pipeline_name"], "docx-native/default")
        self.assertEqual(artifact.metadata["pipeline_observability"]["pipeline_name"], "docx-native/default")
        self.assertTrue(artifact.metadata["pipeline_observability"]["options_hash"])
        self.assertIsInstance(artifact.metadata["pipeline_observability"]["cache_hit"], bool)
        self.assertIn("active_runtime_stages", artifact.metadata["pipeline_observability"])
        self.assertEqual(chunks[0].semantic_role, SemanticRole.TITLE.value)
        self.assertEqual(chunks[1].semantic_role, SemanticRole.PARAGRAPH.value)

    def test_table_structure_stage_enriches_table_items_and_chunks_when_enabled(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            request = ParseRequest(
                doc_id="doc-table-stage",
                file_path=str(workspace.root / "table.pdf"),
                media_type="application/pdf",
                options={
                    "enrichment": {
                        "table_structure": {
                            "enabled": True,
                            "header_rows": 1,
                            "output_format": "markdown",
                        }
                    }
                },
            )
            pipeline = runtime.pipeline_registry.resolve(request, purpose="parse")
            blocks = (
                Block(
                    block_id="blk-title",
                    doc_id="doc-table-stage",
                    type=BlockType.TITLE,
                    content="Parts List",
                    metadata={"page": 1, "parser": "pdf-text", "semantic_role": SemanticRole.TITLE.value},
                ),
                Block(
                    block_id="blk-table",
                    doc_id="doc-table-stage",
                    type=BlockType.TABLE,
                    content="Part\tQty\nBolt\t2",
                    metadata={
                        "page": 1,
                        "parser": "pdf-text",
                        "semantic_role": SemanticRole.TABLE.value,
                        "cells": [["Part", "Qty"], ["Bolt", "2"]],
                        "rows": 2,
                        "cols": 2,
                    },
                ),
            )

            artifact = pipeline.build_document(request=request, blocks=blocks)
            chunks = pipeline.build_chunks(request=request, blocks=blocks)

        self.assertIn("table-structure", artifact.metadata["active_runtime_stages"])
        self.assertEqual(artifact.metadata["table_structure"]["enriched_items"], 1)
        self.assertEqual(artifact.items[1].metadata["table_markdown"], "| Part | Qty |\n| --- | --- |\n| Bolt | 2 |")
        self.assertIn("role:table", artifact.items[1].metadata["structure_tags"])
        self.assertEqual(artifact.items[1].provenance["semantic_role"], SemanticRole.TABLE.value)
        self.assertEqual(chunks[1].semantic_role, SemanticRole.TABLE.value)
        self.assertEqual(chunks[1].text, "| Part | Qty |\n| --- | --- |\n| Bolt | 2 |")

    def test_execute_persists_document_views_for_pages_lines_and_records(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            source_path = workspace.root / "catalog.pdf"
            source_path.write_bytes(b"%PDF-1.4\n% test\n")
            blocks = (
                Block(
                    block_id="doc-views-title",
                    doc_id="doc-views",
                    type=BlockType.TITLE,
                    content="Catalog",
                    metadata={"page": 1, "semantic_role": "title", "parser": "pdf-text"},
                ),
                Block(
                    block_id="doc-views-p1",
                    doc_id="doc-views",
                    type=BlockType.PARAGRAPH,
                    content="No Certificate Holder LatestDate\n1 TC001A ACME 2025-01-10",
                    metadata={"page": 1, "semantic_role": "paragraph", "parser": "pdf-text"},
                ),
            )
            chunks = (
                Chunk(
                    chunk_id="chunk-1",
                    doc_id="doc-views",
                    block_ids=("doc-views-p1",),
                    text="1 TC001A ACME 2025-01-10",
                    language="en",
                    semantic_role="paragraph",
                ),
            )

            with patch.object(runtime, "_load_blocks_for_request", return_value=blocks), patch.object(
                runtime,
                "_load_document_for_request",
                return_value=None,
            ), patch.object(runtime, "_load_chunks_for_request", return_value=chunks), patch.object(
                runtime,
                "_embed_chunks",
                return_value=chunks,
            ):
                runtime.submit(
                    ParseRequest(
                        doc_id="doc-views",
                        file_path=str(source_path),
                        media_type="application/pdf",
                        options={"profile": "large-pdf-catalog"},
                    )
                )

            views = runtime.job_store.get_document_views(doc_id="doc-views", tenant_id="default")
            self.assertEqual(len(views["pages"]), 1)
            self.assertGreaterEqual(len(views["lines"]), 2)
            self.assertEqual(len(views["records"]), 1)
            self.assertEqual(views["records"][0]["source"], "text-block")
            self.assertEqual(views["records"][0]["fields"]["certificate_or_project_no"], "TC001A")

            snapshot = runtime.get_document(doc_id="doc-views", tenant_id="default")
            self.assertEqual(snapshot["document_views"], {})
            snapshot_with_records = runtime.get_document(
                doc_id="doc-views",
                tenant_id="default",
                document_view_types=("records",),
            )
            self.assertEqual(len(snapshot_with_records["document_views"]["records"]), 1)
            records_payload = runtime.get_document_records_projection(
                doc_id="doc-views",
                tenant_id="default",
                query="ACME",
                limit=10,
                offset=0,
            )
            self.assertIsNotNone(records_payload)
            self.assertEqual(records_payload["total"], 1)
            self.assertEqual(records_payload["items"][0]["record_id"], views["records"][0]["record_id"])

    def test_pipeline_observability_reports_cache_hit_after_repeated_resolution(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            request = ParseRequest(
                doc_id="doc-observe",
                file_path=str(workspace.create_docx("observe.docx", ["A", "B"])),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                options={"enrichment": {"layout_reading_order": {"enabled": True}}},
            )

            first_pipeline = runtime.pipeline_registry.resolve(request, purpose="parse")
            first_blocks = first_pipeline.parse_blocks(request=request)
            first_artifact = first_pipeline.build_document(request=request, blocks=first_blocks)

            second_pipeline = runtime.pipeline_registry.resolve(request, purpose="parse")
            second_blocks = second_pipeline.parse_blocks(request=request)
            second_artifact = second_pipeline.build_document(request=request, blocks=second_blocks)

        self.assertFalse(first_artifact.metadata["pipeline_observability"]["cache_hit"])
        self.assertTrue(second_artifact.metadata["pipeline_observability"]["cache_hit"])
        self.assertEqual(
            first_artifact.metadata["pipeline_observability"]["options_hash"],
            second_artifact.metadata["pipeline_observability"]["options_hash"],
        )

    def test_table_structure_stage_can_be_disabled_per_request(self) -> None:
        with TemporaryWorkspace(PDF_TABLE_STAGE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            request = ParseRequest(
                doc_id="doc-table-stage-off",
                file_path=str(workspace.root / "table.pdf"),
                media_type="application/pdf",
                options={
                    "enrichment": {
                        "table_structure": {
                            "enabled": False,
                        }
                    }
                },
            )
            pipeline = runtime.pipeline_registry.resolve(request, purpose="parse")
            blocks = (
                Block(
                    block_id="blk-title",
                    doc_id="doc-table-stage-off",
                    type=BlockType.TITLE,
                    content="Parts List",
                    metadata={"page": 1, "parser": "pdf-text", "semantic_role": SemanticRole.TITLE.value},
                ),
                Block(
                    block_id="blk-table",
                    doc_id="doc-table-stage-off",
                    type=BlockType.TABLE,
                    content="Part\tQty\nBolt\t2",
                    metadata={
                        "page": 1,
                        "parser": "pdf-text",
                        "semantic_role": SemanticRole.TABLE.value,
                        "cells": [["Part", "Qty"], ["Bolt", "2"]],
                        "rows": 2,
                        "cols": 2,
                    },
                ),
            )

            artifact = pipeline.build_document(request=request, blocks=blocks)
            chunks = pipeline.build_chunks(request=request, blocks=blocks)

        self.assertIn("table-structure", artifact.metadata["skipped_runtime_stages"])
        self.assertNotIn("table_structure", artifact.metadata)
        self.assertNotIn("table_markdown", artifact.items[1].metadata)
        self.assertEqual(chunks[1].text, "Part\tQty\nBolt\t2")

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
        self.assertIsNotNone(document["index_manifest"])
        self.assertEqual(document["index_manifest"]["layers"][0]["name"], "primary")
        self.assertEqual(document["index_manifest"]["layers"][1]["name"], "structure")

    def test_submit_populates_null_index_with_structure_layer_manifest(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("phase7.docx", ["Title", "Procedure note"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-phase7-index",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

        upsert = runtime.index.upserts[-1]
        self.assertEqual(upsert["doc_id"], "doc-phase7-index")
        self.assertGreaterEqual(upsert["structure_items"], 2)
        self.assertEqual(upsert["index_manifest"]["layers"][1]["name"], "structure")

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

    def test_queue_worker_failure_is_requeued_until_dead_letter(self) -> None:
        with TemporaryWorkspace(QUEUE_RETRY_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            job = runtime.start(
                ParseRequest(
                    doc_id="doc-retry",
                    file_path=str(workspace.root / "missing.docx"),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

            claimed = runtime.claim_next_job()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            with self.assertRaises(Exception):
                runtime.execute(job_id=claimed.job_id)

            retry_job = runtime.get_job(job_id=job.job_id)
            assert retry_job is not None
            self.assertEqual(retry_job.state, ParseJobState.PENDING)
            self.assertEqual(retry_job.attempt_count, 1)
            self.assertIn("retry_scheduled", retry_job.failure_reason or "")
            self.assertIn("next_attempt_at", retry_job.options)
            self.assertIsNotNone(retry_job.next_attempt_at)
            self.assertIsNone(retry_job.claim_token)

            claimed_again = runtime.claim_next_job()
            self.assertIsNotNone(claimed_again)
            assert claimed_again is not None
            with self.assertRaises(Exception):
                runtime.execute(job_id=claimed_again.job_id)

            dead = runtime.get_job(job_id=job.job_id)
            assert dead is not None
            self.assertEqual(dead.state, ParseJobState.FAILED)
            self.assertEqual(dead.attempt_count, 2)
            self.assertIsNotNone(dead.dead_lettered_at)

    def test_recover_timed_out_jobs_requeues_active_job(self) -> None:
        with TemporaryWorkspace(QUEUE_RETRY_CONFIG) as workspace:
            document_path = workspace.create_docx("timeout.docx", ["timeout"])
            runtime = build_runtime(workspace.config_path)
            job = runtime.start(
                ParseRequest(
                    doc_id="doc-timeout",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            claimed = runtime.claim_next_job()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertIsNotNone(claimed.claim_token)
            old_token = claimed.claim_token
            runtime.job_store.jobs[job.job_id].lease_expires_at = "2000-01-01T00:00:00+00:00"

            recovered = runtime.recover_timed_out_jobs()

            self.assertEqual([item["job_id"] for item in recovered["timed_out"]], [job.job_id])
            requeued = runtime.get_job(job_id=job.job_id)
            assert requeued is not None
            self.assertEqual(requeued.state, ParseJobState.PENDING)
            self.assertIn("job_timeout", requeued.failure_reason or "")
            self.assertIsNone(requeued.claim_token)
            with self.assertRaisesRegex(RuntimeError, "stale_claim"):
                runtime.job_store.update_state(
                    job_id=job.job_id,
                    state=ParseJobState.DONE,
                    expected_claim_token=old_token,
                    clear_claim=True,
                )

    def test_stale_worker_cannot_save_blocks_after_timeout_reclaim(self) -> None:
        with TemporaryWorkspace(QUEUE_RETRY_CONFIG) as workspace:
            document_path = workspace.create_docx("stale.docx", ["stale"])
            runtime = build_runtime(workspace.config_path)
            job = runtime.start(
                ParseRequest(
                    doc_id="doc-stale-worker",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            claimed = runtime.claim_next_job()
            self.assertIsNotNone(claimed)
            assert claimed is not None

            def expire_claim(_request):
                runtime.job_store.jobs[job.job_id].lease_expires_at = "2000-01-01T00:00:00+00:00"
                runtime.recover_timed_out_jobs()
                return (
                    Block(
                        block_id="old-block",
                        doc_id="doc-stale-worker",
                        type=BlockType.PARAGRAPH,
                        content="old worker content",
                    ),
                )

            with patch.object(runtime, "_load_blocks_for_request", side_effect=expire_claim):
                with self.assertRaisesRegex(RuntimeError, "stale_claim"):
                    runtime.execute(job_id=claimed.job_id, claim_token=claimed.claim_token)

            self.assertEqual(tuple(runtime.job_store.get_blocks(doc_id="doc-stale-worker")), ())
            requeued = runtime.get_job(job_id=job.job_id)
            assert requeued is not None
            self.assertEqual(requeued.state, ParseJobState.PENDING)

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

    def test_pdf_snapshot_infers_manual_anatomy_for_heading_like_pages(self) -> None:
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
                        "7.1 Maintenance License .................. 7-1\n"
                        "APPENDIX A Reference Table .................. A-1"
                    ),
                    FakePage(
                        "7.1 Maintenance License\n"
                        "Apply for maintenance license.\n"
                        "Keep records."
                    ),
                    FakePage(
                        "RECORD OF REVISIONS\n"
                        "Revision A 2024-01-01"
                    ),
                    FakePage(
                        "APPENDIX A Reference Table\n"
                        "Reference material."
                    ),
                ]

        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_text_file("sample.pdf", "placeholder")
            runtime = build_runtime(workspace.config_path)
            with patch("parsecore.parsers._load_pdf_reader", return_value=FakeReader):
                outcome = runtime.submit(
                    ParseRequest(
                        doc_id="pdf-manual-anatomy",
                        file_path=str(document_path),
                        media_type="application/pdf",
                    )
                )
                snapshot = runtime.get_document(doc_id="pdf-manual-anatomy")

        self.assertEqual(outcome.job.state, ParseJobState.DONE)
        manifest = snapshot.get("index_manifest") or {}
        anatomy = manifest.get("manual_anatomy") or {}
        structure = manifest.get("structure_quality") or {}
        chapter_titles = [entry.get("text") for entry in anatomy.get("chapter_tree") or []]
        non_business_roles = [entry.get("semantic_role") for entry in anatomy.get("non_business_items") or []]

        self.assertIn("7.1 Maintenance License", chapter_titles)
        self.assertIn("APPENDIX A Reference Table", chapter_titles)
        self.assertIn("toc_entry", non_business_roles)
        self.assertIn("revision_record", non_business_roles)
        self.assertEqual(structure.get("toc_recognition_rate"), 1.0)
        self.assertGreater(structure.get("chapter_coverage_rate", 0.0), 0.0)
        self.assertGreater(structure.get("heading_body_binding_rate", 0.0), 0.0)
        self.assertGreater(structure.get("evidence_binding_strength", 0.0), 0.0)

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

    def test_search_document_structure_filters_by_role_and_tag(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("structure.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-structure",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_blocks(
                doc_id="doc-structure",
                blocks=[
                    Block(
                        block_id="blk-step",
                        doc_id="doc-structure",
                        type=BlockType.PARAGRAPH,
                        content="1. Remove access panel and disconnect power.",
                        metadata={"page": 1, "parser": "docx-native", "semantic_role": "paragraph", "page_type": "body"},
                    ),
                    Block(
                        block_id="blk-warning",
                        doc_id="doc-structure",
                        type=BlockType.PARAGRAPH,
                        content="WARNING: Hydraulic pressure remains in the line.",
                        metadata={"page": 1, "parser": "docx-native", "semantic_role": "warning", "page_type": "body"},
                    ),
                ],
            )

            hits = runtime.search_document_structure(
                doc_id="doc-structure",
                query="hydraulic pressure",
                semantic_roles=["warning"],
                structure_tags=["page:body"],
            )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].semantic_role, "warning")
        self.assertIn("page:body", hits[0].structure_tags)

    def test_search_document_tasks_returns_task_like_entries(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("tasks.docx", ["base"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-tasks",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            runtime.job_store.save_blocks(
                doc_id="doc-tasks",
                blocks=[
                    Block(
                        block_id="blk-step",
                        doc_id="doc-tasks",
                        type=BlockType.PARAGRAPH,
                        content="Step 3 Remove the inspection cover.",
                        metadata={"page": 1, "parser": "docx-native", "semantic_role": "paragraph", "page_type": "body"},
                    ),
                    Block(
                        block_id="blk-body",
                        doc_id="doc-tasks",
                        type=BlockType.PARAGRAPH,
                        content="General description text.",
                        metadata={"page": 1, "parser": "docx-native", "semantic_role": "paragraph", "page_type": "body"},
                    ),
                ],
            )

            hits, mode = runtime.search_document_tasks_with_mode(
                doc_id="doc-tasks",
                query="inspection cover",
            )

        self.assertEqual(mode, "structure-keyword")
        self.assertEqual(len(hits), 1)
        self.assertIn("Step 3", hits[0].text)

    def test_index_metrics_reports_layer_coverage(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("metrics.docx", ["Title", "Step 1 Do work"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-index-metrics",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            metrics = runtime.index_metrics()

        self.assertGreaterEqual(metrics["documents"], 1)
        self.assertIn("primary", metrics["layer_counts"])
        self.assertIn("structure", metrics["layer_counts"])

    def test_index_metrics_reports_high_precision_summary(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx(
                "metrics-high-precision.docx",
                ["Title", "hydraulic pressure warning checklist " * 8],
            )
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-index-metrics-high-precision",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"index": {"embedding_tiers": ["small", "large"]}},
                )
            )
            runtime.search_document_with_mode(
                doc_id="doc-index-metrics-high-precision",
                query="hydraulic pressure warning checklist",
                index_layer="high_precision",
            )
            metrics = runtime.index_metrics()

        self.assertIn("high_precision", metrics)
        summary = metrics["high_precision"]
        self.assertGreaterEqual(summary["documents"], 1)
        self.assertGreater(summary["items"], 0)
        self.assertGreater(summary["document_coverage"], 0.0)
        self.assertGreater(summary["item_ratio_vs_primary"], 0.0)
        self.assertGreater(summary["query_count"], 0)
        self.assertGreater(summary["query_hit_rate"], 0.0)
        self.assertIn("high_precision", metrics["search_effectiveness"])
        self.assertIn("search_effectiveness_trends", metrics)
        self.assertIn("1h", metrics["search_effectiveness_trends"])
        self.assertIn("6h", metrics["search_effectiveness_trends"])
        self.assertIn("24h", metrics["search_effectiveness_trends"])

    def test_index_metrics_search_effectiveness_survives_runtime_rebuild(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx(
                "metrics-rebuild.docx",
                ["Title", "hydraulic pressure warning checklist " * 8],
            )
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-index-metrics-rebuild",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"index": {"embedding_tiers": ["small", "large"]}},
                )
            )
            runtime.search_document_with_mode(
                doc_id="doc-index-metrics-rebuild",
                query="hydraulic pressure warning checklist",
                index_layer="high_precision",
            )

            rebuilt = build_runtime(workspace.config_path)
            metrics = rebuilt.index_metrics()

        self.assertIn("high_precision", metrics["search_effectiveness"])
        self.assertGreater(metrics["high_precision"]["query_count"], 0)

    def test_index_metrics_supports_custom_trend_windows(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx(
                "metrics-custom-trend.docx",
                ["Title", "hydraulic pressure warning checklist " * 8],
            )
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-index-metrics-custom-trend",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"index": {"embedding_tiers": ["small", "large"]}},
                )
            )
            runtime.search_document_with_mode(
                doc_id="doc-index-metrics-custom-trend",
                query="hydraulic pressure warning checklist",
                index_layer="high_precision",
            )
            metrics = runtime.index_metrics(trend_windows_hours=[2, 12])

        self.assertEqual(metrics["trend_windows_hours"], [2.0, 12.0])
        self.assertIn("2h", metrics["search_effectiveness_trends"])
        self.assertIn("12h", metrics["search_effectiveness_trends"])

    def test_batch_reindex_rebuilds_chunks_for_latest_documents(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("batch.docx", ["Title", "Body"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-batch-reindex",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )

            report = runtime.batch_reindex(doc_ids=["doc-batch-reindex"])

        self.assertEqual(report["documents"], 1)
        self.assertTrue(report["processed"])
        self.assertEqual(report["processed"][0]["mode"], "rerun_chunks_only")

    def test_cli_batch_reindex_emits_json_report(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("cli-batch.docx", ["Title", "Body"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-cli-batch",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            )
            buffer = io.StringIO()
            with patch("parsecore.cli.build_runtime", return_value=runtime), patch("sys.stdout", buffer):
                exit_code = cli_main([
                    "batch-reindex",
                    "--config",
                    str(workspace.config_path),
                    "--doc-id",
                    "doc-cli-batch",
                ])

        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["processed"][0]["doc_id"], "doc-cli-batch")

    def test_index_manifest_includes_high_precision_layer_when_large_tier_enabled(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("high-precision.docx", ["Title", "Step 1"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-high-precision",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"index": {"embedding_tiers": ["small", "large"]}},
                )
            )
            snapshot = runtime.get_document(doc_id="doc-high-precision")

        manifest = snapshot["index_manifest"]
        layer_names = {str(layer.get("name")) for layer in manifest.get("layers", [])}
        self.assertIn("high_precision", layer_names)
        high_precision_layer = next(layer for layer in manifest.get("layers", []) if layer.get("name") == "high_precision")
        self.assertEqual(high_precision_layer.get("embedding_tier"), "large")

    def test_search_document_with_high_precision_layer_filters_results(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("search-layer.docx", ["Title"])
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-search-layer",
                    file_path=str(document_path),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    options={"index": {"embedding_tiers": ["small", "large"]}},
                )
            )
            runtime.job_store.save_chunks(
                doc_id="doc-search-layer",
                chunks=[
                    Chunk(
                        chunk_id="chk-short",
                        doc_id="doc-search-layer",
                        block_ids=("blk-1",),
                        text="short",
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(1.0, 0.0),
                    ),
                    Chunk(
                        chunk_id="chk-long",
                        doc_id="doc-search-layer",
                        block_ids=("blk-2",),
                        text="hydraulic pressure warning checklist " * 6,
                        semantic_role=SemanticRole.PARAGRAPH.value,
                        embedding=(1.0, 0.0),
                    ),
                ],
            )
            runtime.index.upsert(
                doc_id="doc-search-layer",
                chunks=runtime.job_store.get_chunks(doc_id="doc-search-layer"),
                index_manifest={
                    "doc_id": "doc-search-layer",
                    "tenant_id": "default",
                    "index_version": "manual-test",
                    "layers": [
                        {"name": "primary", "kind": "chunk", "item_count": 2},
                        {
                            "name": "high_precision",
                            "kind": "chunk",
                            "item_count": 1,
                            "chunk_ids": ["chk-long"],
                        },
                    ],
                },
            )
            hits, _mode = runtime.search_document_with_mode(
                doc_id="doc-search-layer",
                query="hydraulic warning checklist",
                index_layer="high_precision",
            )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].chunk_id, "chk-long")

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

    def test_partitioned_pdf_jobs_merge_parent_document_and_parts(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("partitioned.pdf", [["one"], ["two"], ["three"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-partitioned",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )

            planned = runtime.start_pdf_part_jobs(
                doc_id="doc-partitioned",
                target_pages_per_part=2,
            )

            self.assertEqual(planned["parent_job"].state, ParseJobState.PARTIAL)
            self.assertEqual(len(planned["part_jobs"]), 2)
            self.assertEqual(
                [job.doc_id for job in planned["part_jobs"]],
                ["doc-partitioned-part-1", "doc-partitioned-part-2"],
            )

            for job in planned["part_jobs"]:
                runtime.execute(job_id=job.job_id)

            snapshot = runtime.get_document(doc_id="doc-partitioned")
            self.assertEqual(snapshot["job"].state, ParseJobState.DONE)
            self.assertEqual(len(snapshot["partition_parts"]), 2)
            self.assertTrue(all(part["state"] == "done" for part in snapshot["partition_parts"]))
            manifest = snapshot["index_manifest"]
            part_index = manifest["part_index"]
            self.assertEqual(part_index["strategy"], "pdf_part")
            self.assertEqual(part_index["part_count"], 2)
            self.assertEqual(
                [part["page_range"] for part in part_index["parts"]],
                [{"start": 1, "end": 2}, {"start": 3, "end": 3}],
            )
            self.assertEqual(
                [part["index_version"] for part in part_index["parts"]],
                [job.job_id for job in planned["part_jobs"]],
            )
            self.assertTrue(
                all(
                    chunk_id.startswith(
                        f"doc-partitioned:merged:{part['part_id']}:"
                    )
                    for part in part_index["parts"]
                    for chunk_id in part["chunk_ids"]
                )
            )

            payload = _document_projection(snapshot, projection="structured")
            self.assertEqual(payload["state"], "done")
            self.assertEqual([unit["page_start"] for unit in payload["parse_units"]], [1, 3])
            self.assertEqual([page["page_number"] for page in payload["pages"]], [1, 2, 3])
            self.assertEqual([page["text"] for page in payload["pages"]], ["one", "two", "three"])
            views = runtime.job_store.get_document_views(doc_id="doc-partitioned")
            self.assertEqual([page["text"] for page in views["pages"]], ["one", "two", "three"])
            self.assertEqual(len(views["lines"]), 3)

    def test_execute_refuses_partition_parent_job(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("parent-refuse.pdf", [["one"], ["two"]])
            runtime = build_runtime(workspace.config_path)
            parent = runtime.start(
                ParseRequest(
                    doc_id="doc-parent-refuse",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"job_kind": "pdf_parent", "partitioned": True},
                )
            )

            with self.assertRaisesRegex(RuntimeError, "pdf_parent_not_executable"):
                runtime.execute(job_id=parent.job_id)

            self.assertEqual(tuple(runtime.job_store.get_blocks(doc_id="doc-parent-refuse")), ())
            reloaded = runtime.get_job(job_id=parent.job_id)
            assert reloaded is not None
            self.assertEqual(reloaded.state, ParseJobState.PENDING)

    def test_partitioned_pdf_parent_index_replaces_only_changed_part_prefix(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("partitioned-incremental.pdf", [["one"], ["two"], ["three"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-partitioned-incremental",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )
            planned = runtime.start_pdf_part_jobs(
                doc_id="doc-partitioned-incremental",
                target_pages_per_part=1,
            )

            for job in planned["part_jobs"]:
                runtime.execute(job_id=job.job_id)

            parent_upserts = [
                upsert
                for upsert in runtime.index.upserts
                if upsert["doc_id"] == "doc-partitioned-incremental"
            ]
            self.assertGreaterEqual(len(parent_upserts), 3)
            self.assertTrue(all(upsert.get("mode") == "replace_by_prefix" for upsert in parent_upserts[-3:]))
            self.assertEqual([upsert["chunks"] for upsert in parent_upserts[-3:]], [2, 2, 2])
            self.assertEqual(
                [upsert["index_manifest"]["layers"][0]["item_count"] for upsert in parent_upserts[-3:]],
                [2, 4, 6],
            )
            final_part_index = parent_upserts[-1]["index_manifest"]["part_index"]
            self.assertEqual(final_part_index["part_count"], 3)
            self.assertEqual(final_part_index["indexed_part_count"], 3)
            self.assertEqual(
                [part["page_range"] for part in final_part_index["parts"]],
                [{"start": 1, "end": 1}, {"start": 2, "end": 2}, {"start": 3, "end": 3}],
            )
            self.assertEqual(
                [part["index_version"] for part in final_part_index["parts"]],
                [job.job_id for job in planned["part_jobs"]],
            )
            self.assertEqual(
                len(runtime.job_store.get_chunks(doc_id="doc-partitioned-incremental")),
                6,
            )

    def test_store_prefix_replacement_removes_old_part_artifacts(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            prefix = "doc-parent:merged:part-1:"
            runtime.job_store.save_blocks(
                doc_id="doc-parent",
                blocks=[
                    Block(
                        block_id=f"{prefix}old-a",
                        doc_id="doc-parent",
                        type=BlockType.PARAGRAPH,
                        content="old one",
                    ),
                    Block(
                        block_id=f"{prefix}old-b",
                        doc_id="doc-parent",
                        type=BlockType.PARAGRAPH,
                        content="old two",
                    ),
                    Block(
                        block_id="doc-parent:merged:part-2:keep",
                        doc_id="doc-parent",
                        type=BlockType.PARAGRAPH,
                        content="keep",
                    ),
                ],
            )
            runtime.job_store.save_chunks(
                doc_id="doc-parent",
                chunks=[
                    Chunk(
                        chunk_id=f"{prefix}old-a",
                        doc_id="doc-parent",
                        block_ids=(f"{prefix}old-a",),
                        text="old one",
                    ),
                    Chunk(
                        chunk_id=f"{prefix}old-b",
                        doc_id="doc-parent",
                        block_ids=(f"{prefix}old-b",),
                        text="old two",
                    ),
                    Chunk(
                        chunk_id="doc-parent:merged:part-2:keep",
                        doc_id="doc-parent",
                        block_ids=("doc-parent:merged:part-2:keep",),
                        text="keep",
                    ),
                ],
            )
            runtime.job_store.save_document_views(
                doc_id="doc-parent",
                pages=[
                    {"page_number": 1, "text": "old one", "page_start": 1, "page_end": 1},
                    {"page_number": 2, "text": "keep", "page_start": 2, "page_end": 2},
                ],
                lines=[
                    {"line_id": f"{prefix}old-a:line:1", "page_number": 1, "text": "old one"},
                    {"line_id": "doc-parent:merged:part-2:keep:line:1", "page_number": 2, "text": "keep"},
                ],
                records=[
                    {
                        "record_id": f"{prefix}old-a:text:r1:l1",
                        "block_id": f"{prefix}old-a",
                        "page_start": 1,
                        "page_end": 1,
                        "fields": {"text": "old one"},
                    },
                    {
                        "record_id": "doc-parent:merged:part-2:keep:text:r1:l1",
                        "block_id": "doc-parent:merged:part-2:keep",
                        "page_start": 2,
                        "page_end": 2,
                        "fields": {"text": "keep"},
                    },
                ],
            )

            runtime.job_store.replace_blocks_by_prefix(
                doc_id="doc-parent",
                block_id_prefix=prefix,
                blocks=[
                    Block(
                        block_id=f"{prefix}new",
                        doc_id="doc-parent",
                        type=BlockType.PARAGRAPH,
                        content="new",
                    )
                ],
            )
            runtime.job_store.replace_chunks_by_prefix(
                doc_id="doc-parent",
                chunk_id_prefix=prefix,
                chunks=[
                    Chunk(
                        chunk_id=f"{prefix}new",
                        doc_id="doc-parent",
                        block_ids=(f"{prefix}new",),
                        text="new",
                    )
                ],
            )
            runtime.job_store.replace_document_views_by_prefix(
                doc_id="doc-parent",
                item_id_prefix=prefix,
                pages=[{"page_number": 1, "text": "new", "page_start": 1, "page_end": 1}],
                lines=[{"line_id": f"{prefix}new:line:1", "page_number": 1, "text": "new"}],
                records=[
                    {
                        "record_id": f"{prefix}new:text:r1:l1",
                        "block_id": f"{prefix}new",
                        "page_start": 1,
                        "page_end": 1,
                        "fields": {"text": "new"},
                    }
                ],
            )

            self.assertEqual(
                [block.block_id for block in runtime.job_store.get_blocks(doc_id="doc-parent")],
                [f"{prefix}new", "doc-parent:merged:part-2:keep"],
            )
            self.assertEqual(
                [chunk.chunk_id for chunk in runtime.job_store.get_chunks(doc_id="doc-parent")],
                [f"{prefix}new", "doc-parent:merged:part-2:keep"],
            )
            views = runtime.job_store.get_document_views(doc_id="doc-parent")
            self.assertEqual([page["text"] for page in views["pages"]], ["new", "keep"])
            self.assertEqual([line["text"] for line in views["lines"]], ["new", "keep"])
            self.assertEqual([record["fields"]["text"] for record in views["records"]], ["new", "keep"])

    def test_rerun_pdf_part_creates_replacement_child_job(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("rerun-part.pdf", [["one"], ["two"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-rerun-part",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )
            planned = runtime.start_pdf_part_jobs(doc_id="doc-rerun-part", target_pages_per_part=1)
            for job in planned["part_jobs"]:
                runtime.execute(job_id=job.job_id)
            before_manifest = runtime.get_document(doc_id="doc-rerun-part")["index_manifest"]
            before_part_two = next(
                part
                for part in before_manifest["part_index"]["parts"]
                if part["part_id"] == "doc-rerun-part-part-2"
            )

            rerun = runtime.rerun_pdf_part(
                doc_id="doc-rerun-part",
                part_id="doc-rerun-part-part-2",
            )
            runtime.execute(job_id=rerun["job"].job_id)

            parts = runtime.partition_parts_for_document(doc_id="doc-rerun-part")
            part_two = next(part for part in parts if part["part_id"] == "doc-rerun-part-part-2")
            self.assertEqual(part_two["state"], "done")
            self.assertEqual(part_two["attempts"], 1)
            self.assertNotEqual(part_two["job_id"], planned["part_jobs"][1].job_id)
            after_manifest = runtime.get_document(doc_id="doc-rerun-part")["index_manifest"]
            after_part_two = next(
                part
                for part in after_manifest["part_index"]["parts"]
                if part["part_id"] == "doc-rerun-part-part-2"
            )
            self.assertEqual(after_part_two["index_version"], rerun["job"].job_id)
            self.assertNotEqual(after_part_two["index_version"], before_part_two["index_version"])
            self.assertEqual(after_part_two["page_range"], {"start": 2, "end": 2})
            self.assertTrue(after_part_two["chunk_ids"])
            views = runtime.job_store.get_document_views(doc_id="doc-rerun-part")
            self.assertEqual([page["text"] for page in views["pages"]], ["one", "two"])
            self.assertEqual(len(views["lines"]), 2)

    def test_batch_rerun_defaults_to_failed_parts_only(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("batch-rerun.pdf", [["one"], ["two"], ["three"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-batch-rerun",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )
            planned = runtime.start_pdf_part_jobs(doc_id="doc-batch-rerun", target_pages_per_part=1)
            runtime.execute(job_id=planned["part_jobs"][0].job_id)
            runtime.execute(job_id=planned["part_jobs"][1].job_id)
            runtime.job_store.update_state(
                job_id=planned["part_jobs"][2].job_id,
                state=ParseJobState.FAILED,
                failure_reason="simulated failure",
            )

            rerun = runtime.rerun_pdf_parts(doc_id="doc-batch-rerun", failed_only=True)

            self.assertEqual([item["part_id"] for item in rerun["submitted"]], ["doc-batch-rerun-part-3"])
            self.assertEqual(len(rerun["skipped"]), 2)
            self.assertTrue(all(item["reason"] == "not_failed" for item in rerun["skipped"]))

    def test_cancel_pdf_part_marks_pending_child_as_cancelled(self) -> None:
        with TemporaryWorkspace(PDF_SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_pdf("cancel-part.pdf", [["one"], ["two"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-cancel-part",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )
            runtime.start_pdf_part_jobs(doc_id="doc-cancel-part", target_pages_per_part=1)

            cancelled = runtime.cancel_pdf_part(
                doc_id="doc-cancel-part",
                part_id="doc-cancel-part-part-2",
            )

            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(cancelled["state"], "cancelled")
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                runtime.execute(job_id=cancelled["job"].job_id)
            parts = runtime.partition_parts_for_document(doc_id="doc-cancel-part")
            part_two = next(part for part in parts if part["part_id"] == "doc-cancel-part-part-2")
            self.assertEqual(part_two["state"], "cancelled")
            self.assertEqual(part_two["last_error"], "cancelled")
            metrics = runtime.runtime_metrics(sample_size=50)
            self.assertGreaterEqual(metrics["part_jobs"]["parts_total"], 2)
            self.assertEqual(metrics["part_jobs"]["parts_cancelled"], 1)
