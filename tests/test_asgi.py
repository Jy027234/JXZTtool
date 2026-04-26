from __future__ import annotations

import base64
from concurrent.futures import Future
import unittest

from starlette.testclient import TestClient

from parsecore.asgi import _project_pages, create_app
from parsecore.models import Block, BlockType, ParseJobState
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


QUOTA_ENFORCED_API_CONFIG = """
[project]
name = "test-api"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
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


OCR_API_CONFIG = """
[project]
name = "test-api-ocr"
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
name = "image-ocr"
media_types = ["image/png", "image/jpeg"]
extensions = [".png", ".jpg", ".jpeg"]
""".strip()


OCR_DISABLED_API_CONFIG = """
[project]
name = "test-api-ocr-disabled"
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

[providers.ocr]
enabled = false
provider = "rapidocr"

[[parsers]]
name = "image-ocr"
media_types = ["image/png", "image/jpeg"]
extensions = [".png", ".jpg", ".jpeg"]
""".strip()


OCR_REMOTE_API_CONFIG = """
[project]
name = "test-api-ocr-remote"
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

[providers.ocr]
enabled = true
provider = "remote-http"
base_url = "https://ocr.example.invalid"

[[parsers]]
name = "image-ocr"
media_types = ["image/png", "image/jpeg"]
extensions = [".png", ".jpg", ".jpeg"]
""".strip()


PDF_API_CONFIG = """
[project]
name = "test-api-pdf"
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
name = "pdf-text"
media_types = ["application/pdf"]
extensions = [".pdf"]
""".strip()


class ParseApiTests(unittest.TestCase):
    def test_job_lifecycle_endpoints(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("spec.docx", ["Maintenance Manual", "Apply torque"])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                health_payload = health.json()
                self.assertEqual(health_payload["status"], "ok")
                self.assertEqual(health_payload["version"], "0.1.0")
                self.assertIn("services", health_payload)
                self.assertEqual(health_payload["services"]["python_docx"], True)
                self.assertEqual(health_payload["services"]["pdfplumber"], False)
                self.assertEqual(health_payload["services"]["paddleocr"], False)

                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-api-001",
                        "file_path": str(document_path),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "tenant-alpha",
                        "quota_key": "default-plan",
                        "quota_units": 3,
                    },
                )
                self.assertEqual(created.status_code, 202)
                payload = created.json()
                self.assertEqual(payload["doc_id"], "doc-api-001")
                self.assertEqual(payload["state"], ParseJobState.PENDING.value)
                self.assertEqual(payload["tenant_id"], "tenant-alpha")
                self.assertEqual(payload["quota_key"], "default-plan")
                self.assertEqual(payload["quota_units"], 3)
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
                self.assertEqual(final_job["tenant_id"], "tenant-alpha")
                self.assertEqual(final_job["quota_key"], "default-plan")
                self.assertEqual(final_job["quota_units"], 3)

                listed = client.get("/v1/parse/jobs", params={"doc_id": "doc-api-001"})
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(len(listed.json()["items"]), 1)

                listed_by_tenant = client.get(
                    "/v1/parse/jobs",
                    params={"tenant_id": "tenant-alpha"},
                )
                self.assertEqual(listed_by_tenant.status_code, 200)
                self.assertEqual(len(listed_by_tenant.json()["items"]), 1)

                listed_other_tenant = client.get(
                    "/v1/parse/jobs",
                    params={"tenant_id": "tenant-other"},
                )
                self.assertEqual(listed_other_tenant.status_code, 200)
                self.assertEqual(len(listed_other_tenant.json()["items"]), 0)

                usage = client.get("/v1/parse/quotas/usage")
                self.assertEqual(usage.status_code, 200)
                usage_payload = usage.json()
                self.assertGreaterEqual(usage_payload["total_jobs"], 1)
                self.assertGreaterEqual(usage_payload["total_quota_units"], 3)
                self.assertTrue(usage_payload["items"])

                usage_tenant = client.get(
                    "/v1/parse/quotas/usage",
                    params={"tenant_id": "tenant-alpha"},
                )
                self.assertEqual(usage_tenant.status_code, 200)
                usage_tenant_payload = usage_tenant.json()
                self.assertEqual(usage_tenant_payload["tenant_id"], "tenant-alpha")
                self.assertGreaterEqual(usage_tenant_payload["total_jobs"], 1)

                usage_windowed = client.get(
                    "/v1/parse/quotas/usage",
                    params={"tenant_id": "tenant-alpha", "since_hours": 24},
                )
                self.assertEqual(usage_windowed.status_code, 200)
                usage_windowed_payload = usage_windowed.json()
                self.assertEqual(usage_windowed_payload["since_hours"], 24.0)

                bad_usage_since = client.get(
                    "/v1/parse/quotas/usage",
                    params={"since_hours": "oops"},
                )
                self.assertEqual(bad_usage_since.status_code, 400)
                self.assertEqual(bad_usage_since.json()["error"], "invalid_since_hours")

                bad_usage_since_non_positive = client.get(
                    "/v1/parse/quotas/usage",
                    params={"since_hours": "0"},
                )
                self.assertEqual(bad_usage_since_non_positive.status_code, 400)
                self.assertEqual(bad_usage_since_non_positive.json()["error"], "invalid_since_hours")

                metrics = client.get(
                    "/v1/parse/metrics",
                    params={"tenant_id": "tenant-alpha", "sample_size": 20, "since_hours": 24},
                )
                self.assertEqual(metrics.status_code, 200)
                metrics_payload = metrics.json()
                self.assertEqual(metrics_payload["tenant_id"], "tenant-alpha")
                self.assertEqual(metrics_payload["since_hours"], 24.0)
                self.assertGreaterEqual(metrics_payload["total_jobs"], 1)
                self.assertIn("durations_s", metrics_payload)

                bad_metrics = client.get(
                    "/v1/parse/metrics",
                    params={"sample_size": "oops"},
                )
                self.assertEqual(bad_metrics.status_code, 400)
                self.assertEqual(bad_metrics.json()["error"], "invalid_sample_size")

                bad_metrics_since = client.get(
                    "/v1/parse/metrics",
                    params={"since_hours": "x"},
                )
                self.assertEqual(bad_metrics_since.status_code, 400)
                self.assertEqual(bad_metrics_since.json()["error"], "invalid_since_hours")

                dashboard = client.get(
                    "/v1/parse/dashboard",
                    params={"tenant_id": "tenant-alpha", "sample_size": 20, "recent_limit": 3, "since_hours": 24},
                )
                self.assertEqual(dashboard.status_code, 200)
                dashboard_payload = dashboard.json()
                self.assertEqual(dashboard_payload["tenant_id"], "tenant-alpha")
                self.assertEqual(dashboard_payload["since_hours"], 24.0)
                self.assertIn("usage", dashboard_payload)
                self.assertIn("metrics", dashboard_payload)
                self.assertIn("recent_jobs", dashboard_payload)

                bad_dashboard_recent_limit = client.get(
                    "/v1/parse/dashboard",
                    params={"recent_limit": "nope"},
                )
                self.assertEqual(bad_dashboard_recent_limit.status_code, 400)
                self.assertEqual(
                    bad_dashboard_recent_limit.json()["error"],
                    "invalid_recent_limit",
                )

                bad_dashboard_since = client.get(
                    "/v1/parse/dashboard",
                    params={"since_hours": "bad"},
                )
                self.assertEqual(bad_dashboard_since.status_code, 400)
                self.assertEqual(bad_dashboard_since.json()["error"], "invalid_since_hours")

                document = client.get(
                    "/v1/parse/documents/doc-api-001",
                    params={"tenant_id": "tenant-alpha"},
                )
                self.assertEqual(document.status_code, 200)
                document_payload = document.json()
                self.assertEqual(document_payload["job"]["job_id"], job_id)
                self.assertGreaterEqual(len(document_payload["blocks"]), 2)
                self.assertEqual(len(document_payload["chunks"]), len(document_payload["blocks"]))

                wrong_tenant_document = client.get(
                    "/v1/parse/documents/doc-api-001",
                    params={"tenant_id": "tenant-other"},
                )
                self.assertEqual(wrong_tenant_document.status_code, 404)

                retried = client.post(
                    "/v1/parse/documents/doc-api-001/reparse",
                    params={"tenant_id": "tenant-alpha"},
                )
                self.assertEqual(retried.status_code, 202)
                self.assertNotEqual(retried.json()["job_id"], job_id)

                wrong_tenant_reparse = client.post(
                    "/v1/parse/documents/doc-api-001/reparse",
                    params={"tenant_id": "tenant-other"},
                )
                self.assertEqual(wrong_tenant_reparse.status_code, 404)

                searched = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params={"q": "maintenance manual", "limit": 2, "tenant_id": "tenant-alpha"},
                )
                self.assertEqual(searched.status_code, 200)
                self.assertEqual(searched.json()["retrieval_mode"], "keyword-fallback")
                items = searched.json()["items"]
                self.assertGreaterEqual(len(items), 1)
                self.assertEqual(items[0]["semantic_role"], "paragraph")

                wrong_tenant_search = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params={"q": "maintenance manual", "tenant_id": "tenant-other"},
                )
                self.assertEqual(wrong_tenant_search.status_code, 404)

                filtered = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params=[("q", "spec"), ("role", "title"), ("tenant_id", "tenant-alpha")],
                )
                self.assertEqual(filtered.status_code, 200)
                self.assertEqual(filtered.json()["retrieval_mode"], "keyword-fallback")
                self.assertEqual(len(filtered.json()["items"]), 1)
                self.assertEqual(filtered.json()["items"][0]["semantic_role"], "title")

    def test_parse_batch_endpoint_returns_enterprise_compatible_payload(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("compat.docx", ["Maintenance Manual", "Apply torque in sequence"])
            payload = {
                "file_base64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
                "file_name": document_path.name,
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "tenant_id": "tenant-batch",
                "quota_key": "compat",
            }
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post("/v1/parse/batch", json=payload)

            self.assertEqual(response.status_code, 200)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertTrue(body["success"])
            self.assertEqual(body["total_pages"], 1)
            self.assertEqual(body["parser_used"], "python-docx")
            self.assertEqual(body["error"], None)
            self.assertEqual(len(body["pages"]), 1)
            self.assertEqual(body["pages"][0]["page_number"], 1)
            self.assertEqual(body["pages"][0]["page_type"], "body")
            self.assertIn("Maintenance Manual", body["pages"][0]["text"])
            self.assertEqual(body["pages"][0]["tables_markdown"], [])
            self.assertEqual(body["pages"][0]["confidence"], 1.0)

    def test_parse_upload_endpoint_returns_parser_service_shape(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("compat-upload.docx", ["Maintenance Manual", "Apply torque in sequence"])
            file_bytes = document_path.read_bytes()
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/parse",
                    files={
                        "file": (
                            document_path.name,
                            file_bytes,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertEqual(body["file_name"], document_path.name)
            self.assertEqual(
                body["mime_type"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.assertEqual(body["total_pages"], 1)
            self.assertEqual(body["metadata"]["parser"], "python-docx")
            self.assertEqual(body["pages"][0]["page_type"], "body")
            self.assertIn("Maintenance Manual", body["pages"][0]["text"])

    def test_health_reports_pdf_service_when_pdf_parser_registered(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["services"]["pdfplumber"], True)
            self.assertEqual(body["services"]["python_docx"], False)
            self.assertEqual(body["services"]["paddleocr"], False)

    def test_health_reports_ocr_service_when_image_parser_registered_and_engine_available(self) -> None:
        with TemporaryWorkspace(OCR_API_CONFIG) as workspace, patch(
            "parsecore.asgi._is_ocr_service_available",
            return_value=True,
        ):
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["services"]["pdfplumber"], False)
            self.assertEqual(body["services"]["python_docx"], False)
            self.assertEqual(body["services"]["paddleocr"], True)

    def test_health_reports_ocr_service_false_when_provider_disabled(self) -> None:
        with TemporaryWorkspace(OCR_DISABLED_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["services"]["pdfplumber"], False)
            self.assertEqual(body["services"]["python_docx"], False)
            self.assertEqual(body["services"]["paddleocr"], False)

    def test_health_reports_ocr_service_true_when_remote_provider_is_configured(self) -> None:
        with TemporaryWorkspace(OCR_REMOTE_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["services"]["pdfplumber"], False)
            self.assertEqual(body["services"]["python_docx"], False)
            self.assertEqual(body["services"]["paddleocr"], True)

    def test_parse_batch_endpoint_processes_pdf_and_preserves_page_types(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            document_path = workspace.create_pdf(
                "compat.pdf",
                [
                    [
                        "TABLE OF CONTENTS",
                        "1. Scope .......... 1",
                        "2. Procedure .......... 2",
                    ],
                    [
                        "Procedure",
                        "WARNING: Disconnect external power.",
                        "Apply torque in sequence.",
                    ],
                ],
            )
            payload = {
                "file_base64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
                "file_name": document_path.name,
                "media_type": "application/pdf",
                "tenant_id": "tenant-batch-pdf",
                "quota_key": "compat-pdf",
            }
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post("/v1/parse/batch", json=payload)

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["success"])
            self.assertEqual(body["total_pages"], 2)
            self.assertEqual(body["parser_used"], "pdf-text")
            self.assertEqual(body["pages"][0]["page_type"], "toc")
            self.assertEqual(body["pages"][1]["page_type"], "body")
            self.assertIn("1. Scope .......... 1", body["pages"][0]["text"])
            self.assertIn("WARNING: Disconnect external power.", body["pages"][1]["text"])
            self.assertEqual(body["pages"][0]["tables_markdown"], [])

    def test_parse_upload_pdf_alias_matches_versioned_route_and_echoes_ocr_flag(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            document_path = workspace.create_pdf(
                "compat-upload.pdf",
                [
                    ["TABLE OF CONTENTS", "1. Scope .......... 1"],
                    ["Procedure", "Apply torque in sequence."],
                ],
            )
            file_bytes = document_path.read_bytes()
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                root_response = client.post(
                    "/parse",
                    files={"file": (document_path.name, file_bytes, "application/pdf")},
                    data={"enable_ocr": "true"},
                )
                versioned_response = client.post(
                    "/v1/parse",
                    files={"file": (document_path.name, file_bytes, "application/pdf")},
                    data={"enable_ocr": "true"},
                )

            self.assertEqual(root_response.status_code, 200)
            self.assertEqual(versioned_response.status_code, 200)
            root_body = root_response.json()
            versioned_body = versioned_response.json()
            self.assertEqual(root_body["pages"], versioned_body["pages"])
            self.assertEqual(root_body["file_name"], document_path.name)
            self.assertEqual(root_body["mime_type"], "application/pdf")
            self.assertEqual(root_body["metadata"]["parser"], "pdf-text")
            self.assertEqual(root_body["metadata"]["ocr_enabled"], True)
            self.assertEqual(root_body["pages"][0]["page_type"], "toc")
            self.assertEqual(root_body["pages"][1]["page_type"], "body")

    def test_parse_batch_alias_matches_parser_service_path(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("compat-alias.docx", ["Alias route", "Batch parser"]) 
            payload = {
                "file_base64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
                "file_name": document_path.name,
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                alias_response = client.post("/parse/batch", json=payload)
                versioned_response = client.post("/v1/parse/batch", json=payload)

            self.assertEqual(alias_response.status_code, 200)
            self.assertEqual(versioned_response.status_code, 200)
            self.assertEqual(alias_response.json()["success"], True)
            self.assertEqual(alias_response.json()["pages"], versioned_response.json()["pages"])
            self.assertEqual(alias_response.json()["parser_used"], versioned_response.json()["parser_used"])

    def test_parse_batch_endpoint_rejects_invalid_base64(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": "not-valid-base64",
                        "file_name": "broken.docx",
                    },
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertFalse(body["success"])
            self.assertEqual(body["parser_used"], "none")
            self.assertEqual(body["error"], "Invalid base64 encoding")
            self.assertEqual(body["code"], "invalid_base64_encoding")
            self.assertEqual(body["message"], "Invalid base64 encoding")
            self.assertEqual(body["trace_id"], response.headers["x-trace-id"])

    def test_parse_batch_endpoint_returns_429_when_quota_exceeded(self) -> None:
        with TemporaryWorkspace(QUOTA_ENFORCED_API_CONFIG) as workspace:
            document_path = workspace.create_docx("quota.docx", ["A"])
            encoded = base64.b64encode(document_path.read_bytes()).decode("ascii")
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                first = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": encoded,
                        "file_name": document_path.name,
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "tenant-batch-q",
                        "quota_key": "starter",
                        "quota_units": 2,
                    },
                )
                second = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": encoded,
                        "file_name": document_path.name,
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "tenant-batch-q",
                        "quota_key": "starter",
                        "quota_units": 2,
                    },
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 429)
            self.assertIn("x-trace-id", second.headers)
            body = second.json()
            self.assertFalse(body["success"])
            self.assertEqual(body["parser_used"], "none")
            self.assertIn("quota exceeded", body["error"])
            self.assertEqual(body["code"], "quota_exceeded")
            self.assertEqual(body["trace_id"], second.headers["x-trace-id"])

    def test_project_pages_marks_toc_and_collects_tables(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-title",
                    doc_id="doc-compat",
                    type=BlockType.TITLE,
                    content="Manual",
                    metadata={"page": 1, "semantic_role": "title", "parser": "pdf-text"},
                ),
                Block(
                    block_id="blk-toc",
                    doc_id="doc-compat",
                    type=BlockType.PARAGRAPH,
                    content="1. Scope .......... 1",
                    metadata={"page": 1, "semantic_role": "toc_entry", "parser": "pdf-text"},
                ),
                Block(
                    block_id="blk-table",
                    doc_id="doc-compat",
                    type=BlockType.TABLE,
                    content="| col | value |",
                    metadata={"page": 2, "semantic_role": "table", "parser": "pdf-text"},
                ),
            )
        )

        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["page_type"], "toc")
        self.assertEqual(pages[0]["text"], "1. Scope .......... 1")
        self.assertEqual(pages[1]["tables_markdown"], ["| col | value |"])
        self.assertEqual(pages[1]["text"], "")

    def test_trace_id_header_is_echoed_and_error_payload_is_unified(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get(
                    "/v1/parse/jobs/missing-job",
                    headers={"x-trace-id": "trace-from-client"},
                )

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers.get("x-trace-id"), "trace-from-client")
            body = response.json()
            self.assertEqual(body["error"], "job_not_found")
            self.assertEqual(body["code"], "job_not_found")
            self.assertEqual(body["message"], "Parse job not found")
            self.assertEqual(body["trace_id"], "trace-from-client")

    def test_invalid_query_param_returns_traceable_error_payload(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/v1/parse/metrics", params={"sample_size": "oops"})

            self.assertEqual(response.status_code, 400)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertEqual(body["error"], "invalid_sample_size")
            self.assertEqual(body["code"], "invalid_sample_size")
            self.assertEqual(body["message"], "Invalid sample_size")
            self.assertEqual(body["trace_id"], response.headers["x-trace-id"])

    def test_create_job_returns_429_when_inflight_is_full(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx("busy.docx", ["A"])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                # force a tiny capacity and occupy it with a pending future
                runner = app.state.runner
                runner.max_inflight_jobs = 1
                runner.inflight["synthetic-busy"] = Future()

                blocked = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-busy-001",
                        "file_path": str(document_path),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    },
                )

                self.assertEqual(blocked.status_code, 429)
                payload = blocked.json()
                self.assertEqual(payload["error"], "too_many_inflight_jobs")
                self.assertEqual(payload["max_inflight_jobs"], 1)

    def test_create_job_returns_429_when_quota_exceeded(self) -> None:
        with TemporaryWorkspace(QUOTA_ENFORCED_API_CONFIG) as workspace:
            doc_a = workspace.create_docx("quota-a.docx", ["A"])
            doc_b = workspace.create_docx("quota-b.docx", ["B"])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                first = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-q-1",
                        "file_path": str(doc_a),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "tenant-q",
                        "quota_key": "starter",
                        "quota_units": 2,
                    },
                )
                self.assertEqual(first.status_code, 202)

                second = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-q-2",
                        "file_path": str(doc_b),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "tenant-q",
                        "quota_key": "starter",
                        "quota_units": 2,
                    },
                )
                self.assertEqual(second.status_code, 429)
                payload = second.json()
                self.assertEqual(payload["error"], "quota_exceeded")
                self.assertEqual(payload["tenant_id"], "tenant-q")
                self.assertEqual(payload["quota_key"], "starter")
                self.assertEqual(payload["limit_units"], 3)

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

    def test_prometheus_metrics_endpoint_returns_valid_format(self) -> None:
        """Verify /v1/parse/prometheus returns Prometheus-format metrics."""
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                # Create a job to generate events
                doc = workspace.create_docx("test-metrics.docx", ["sample content"])
                payload = {
                    "doc_id": "doc-metrics",
                    "file_path": str(doc),
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "tenant_id": "metrics-tenant",
                    "quota_key": "default",
                    "quota_units": 1,
                }
                response = client.post("/v1/parse/jobs", json=payload)
                self.assertEqual(response.status_code, 202)
                
                # Get metrics
                metrics_response = client.get("/v1/parse/prometheus")
                self.assertEqual(metrics_response.status_code, 200)
                self.assertEqual(metrics_response.headers["content-type"], "text/plain; charset=utf-8")
                
                metrics_text = metrics_response.text
                # Should contain HELP and TYPE headers
                self.assertIn("# HELP parse_quota_exceeded_total", metrics_text)
                self.assertIn("# TYPE parse_quota_exceeded_total counter", metrics_text)
                self.assertIn("# HELP parse_inflight_full_total", metrics_text)
                self.assertIn("# HELP parse_embedding_retry_total", metrics_text)
                # Just check that ringbuffer_size is present (format may vary slightly)
                self.assertIn("parse_ringbuffer_size", metrics_text)

    def test_events_endpoint_returns_recent_events(self) -> None:
        """Verify /v1/parse/events returns recent events with filtering."""
        with TemporaryWorkspace(QUOTA_ENFORCED_API_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                # Try to exceed quota to generate quota_exceeded events
                doc1 = workspace.create_docx("doc1.docx", ["content1"])
                doc2 = workspace.create_docx("doc2.docx", ["content2"])
                
                # First request with 1 unit should succeed (limit is 3 per window)
                payload1 = {
                    "doc_id": "doc-event-1",
                    "file_path": str(doc1),
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "tenant_id": "event-tenant",
                    "quota_key": "small",
                    "quota_units": 1,
                }
                response1 = client.post("/v1/parse/jobs", json=payload1)
                self.assertEqual(response1.status_code, 202)
                
                # Second request with 3 units should exceed quota and return 429 (1+3=4 > 3)
                payload2 = {
                    "doc_id": "doc-event-2",
                    "file_path": str(doc2),
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "tenant_id": "event-tenant",
                    "quota_key": "small",
                    "quota_units": 3,
                }
                response2 = client.post("/v1/parse/jobs", json=payload2)
                self.assertEqual(response2.status_code, 429)
                self.assertEqual(response2.json()["error"], "quota_exceeded")
                
                # Query events endpoint
                events_response = client.get("/v1/parse/events")
                self.assertEqual(events_response.status_code, 200)
                events_data = events_response.json()
                
                self.assertIn("events", events_data)
                self.assertIn("counters", events_data)
                
                # Should have at least one quota_exceeded event
                quota_events = [e for e in events_data["events"] if e["event_type"] == "quota_exceeded"]
                self.assertGreaterEqual(len(quota_events), 1)
                self.assertIn("trace_id", quota_events[0])
                
                # Filter by event_type
                filtered_response = client.get("/v1/parse/events?event_type=quota_exceeded")
                filtered_data = filtered_response.json()
                all_are_quota = all(e["event_type"] == "quota_exceeded" for e in filtered_data["events"])
                self.assertTrue(all_are_quota)
                
                # Filter by tenant_id
                tenant_response = client.get("/v1/parse/events?tenant_id=event-tenant")
                tenant_data = tenant_response.json()
                all_are_tenant = all(e["tenant_id"] == "event-tenant" for e in tenant_data["events"])
                self.assertTrue(all_are_tenant)
            self.assertTrue(all_are_tenant)

    def test_events_and_prometheus_include_ocr_summary_events(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                runtime_obj = app.state.runtime
                doc = workspace.create_docx("ocr-events.docx", ["content"])
                blocks = (
                    Block(
                        block_id="blk-1",
                        doc_id="doc-ocr-events",
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
                        doc_id="doc-ocr-events",
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
                with patch.object(runtime_obj, "_load_blocks_for_request", return_value=blocks), patch.object(
                    runtime_obj,
                    "_load_chunks_for_request",
                    return_value=(),
                ):
                    response = client.post(
                        "/v1/parse/jobs",
                        json={
                            "doc_id": "doc-ocr-events",
                            "file_path": str(doc),
                            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "tenant_id": "ocr-tenant",
                            "quota_key": "ocr-plan",
                        },
                    )
                    self.assertEqual(response.status_code, 202)
                    job_id = response.json()["job_id"]

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

                    attempt_response = client.get(
                        "/v1/parse/events",
                        params={"event_type": "ocr_attempted", "tenant_id": "ocr-tenant"},
                    )
                    self.assertEqual(attempt_response.status_code, 200)
                    attempt_data = attempt_response.json()
                    self.assertEqual(len(attempt_data["events"]), 1)
                    self.assertEqual(attempt_data["events"][0]["doc_id"], "doc-ocr-events")
                    self.assertEqual(attempt_data["events"][0]["page_count"], 2)
                    self.assertEqual(attempt_data["events"][0]["block_count"], 2)
                    self.assertEqual(
                        attempt_data["events"][0]["attempt_reasons"],
                        ["cid_dense", "empty_text"],
                    )
                    self.assertEqual(
                        attempt_data["counters"]["ocr-tenant:ocr-plan:ocr_attempted"],
                        2,
                    )

                    failed_response = client.get(
                        "/v1/parse/events",
                        params={"event_type": "ocr_failed", "tenant_id": "ocr-tenant"},
                    )
                    self.assertEqual(failed_response.status_code, 200)
                    failed_data = failed_response.json()
                    self.assertEqual(len(failed_data["events"]), 1)
                    self.assertEqual(failed_data["events"][0]["page_count"], 1)
                    self.assertEqual(failed_data["events"][0]["block_count"], 1)
                    self.assertEqual(
                        failed_data["events"][0]["error_reasons"],
                        ["provider_request_failed"],
                    )
                    self.assertEqual(
                        failed_data["counters"]["ocr-tenant:ocr-plan:ocr_failed"],
                        1,
                    )

                    metrics_response = client.get("/v1/parse/prometheus")
                    self.assertEqual(metrics_response.status_code, 200)
                    metrics_text = metrics_response.text
                    self.assertIn(
                        'parse_ocr_attempt_total{tenant_id="ocr-tenant",quota_key="ocr-plan"} 2',
                        metrics_text,
                    )
                    self.assertIn(
                        'parse_ocr_fallback_total{tenant_id="ocr-tenant",quota_key="ocr-plan"} 1',
                        metrics_text,
                    )
                    self.assertIn(
                        'parse_ocr_failed_total{tenant_id="ocr-tenant",quota_key="ocr-plan"} 1',
                        metrics_text,
                    )

    def test_dashboard_includes_observability_data(self) -> None:
        """Verify tenant_dashboard includes observability events and counters."""
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                # Create a job
                doc = workspace.create_docx("test-dashboard.docx", ["content"])
                payload = {
                    "doc_id": "doc-dashboard",
                    "file_path": str(doc),
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "tenant_id": "dashboard-tenant",
                    "quota_key": "default",
                }
                response = client.post("/v1/parse/jobs", json=payload)
                self.assertEqual(response.status_code, 202)
                
                # Get dashboard
                dashboard_response = client.get("/v1/parse/dashboard?tenant_id=dashboard-tenant")
                self.assertEqual(dashboard_response.status_code, 200)
                dashboard = dashboard_response.json()
                
                # Verify observability data is included
                self.assertIn("observability", dashboard)
                self.assertIn("recent_events", dashboard["observability"])
                self.assertIn("event_counters", dashboard["observability"])
                self.assertIsInstance(dashboard["observability"]["recent_events"], list)
                self.assertIsInstance(dashboard["observability"]["event_counters"], dict)