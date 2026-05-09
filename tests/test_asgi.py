from __future__ import annotations

import base64
from concurrent.futures import Future
import os
from pathlib import Path
import time
import unittest

from starlette.testclient import TestClient

from parsecore.asgi import BackgroundParseRunner, _project_pages, create_app
from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, ParseJobState, ParseRequest
from parsecore.stubs import FakeEmbeddingProvider
from tests.support import TemporaryWorkspace, build_docx_table
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


LIMITED_UPLOAD_API_CONFIG = """
[project]
name = "test-api-upload-limit"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
max_upload_bytes = 4

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
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
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


TEXT_API_CONFIG = """
[project]
name = "test-api-text"
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
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


STRICT_TEXT_API_CONFIG = """
[project]
name = "test-api-text-strict-paths"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
allow_external_file_paths = false

[storage]
database_url = "__DB_URL__"
object_store = "local://__OBJECT_STORE__"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


REMOTE_OBJECT_STORE_UPLOAD_CONFIG = """
[project]
name = "test-api-upload-non-local-store"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25

[storage]
database_url = "__DB_URL__"
object_store = "s3://parsecore-test/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


UPLOAD_BRIDGE_HARDENED_CONFIG = """
[project]
name = "test-api-upload-bridge-hardened"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
staged_upload_retention_seconds = 60
staged_upload_api_key_env = "PARSECORE_UPLOAD_BRIDGE_API_KEY"

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
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


UPLOAD_BRIDGE_RETENTION_CONFIG = """
[project]
name = "test-api-upload-bridge-retention"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
staged_upload_retention_seconds = 1

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
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
""".strip()


API_KEY_PROTECTED_CONFIG = """
[project]
name = "test-api-protected"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_workers = 2
poll_interval_ms = 25
api_key_env = "PARSECORE_API_KEY"

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
    def test_background_runner_respects_part_active_limit(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            runner = BackgroundParseRunner(runtime, max_workers=1, max_inflight_jobs=10)
            try:
                first = runtime.start(
                    ParseRequest(
                        doc_id="doc-runner-part-1",
                        file_path="first.pdf",
                        media_type="application/pdf",
                        options={
                            "job_kind": "pdf_part",
                            "source_doc_id": "doc-runner",
                            "parent_job_id": "parent-job",
                            "part_id": "part-1",
                            "max_active_parts_per_doc": 1,
                        },
                    )
                )
                second = runtime.start(
                    ParseRequest(
                        doc_id="doc-runner-part-2",
                        file_path="second.pdf",
                        media_type="application/pdf",
                        options={
                            "job_kind": "pdf_part",
                            "source_doc_id": "doc-runner",
                            "parent_job_id": "parent-job",
                            "part_id": "part-2",
                            "max_active_parts_per_doc": 1,
                        },
                    )
                )
                runner.inflight[first.job_id] = Future()

                self.assertFalse(runner._can_start_job(second))
                runner._submit_existing_job(second, allow_queue=True)
                self.assertEqual(runner.queued_job_ids, [second.job_id])
            finally:
                runner.shutdown()

    def test_background_runner_cancel_removes_queued_part(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            document_path = workspace.create_pdf("runner-cancel.pdf", [["one"], ["two"]])
            runtime = build_runtime(workspace.config_path)
            runtime.start(
                ParseRequest(
                    doc_id="doc-runner-cancel",
                    file_path=str(document_path),
                    media_type="application/pdf",
                    options={"profile": "large-pdf"},
                )
            )
            planned = runtime.start_pdf_part_jobs(
                doc_id="doc-runner-cancel",
                target_pages_per_part=1,
                max_active_parts_per_doc=1,
            )
            first, second = planned["part_jobs"]
            runner = BackgroundParseRunner(runtime, max_workers=1, max_inflight_jobs=1)
            try:
                runner.inflight[first.job_id] = Future()
                runner._submit_existing_job(second, allow_queue=True)
                self.assertEqual(runner.queued_job_ids, [second.job_id])

                result = runner.cancel_pdf_part(
                    doc_id="doc-runner-cancel",
                    part_id=second.options["part_id"],
                    tenant_id=second.tenant_id,
                )

                self.assertTrue(result["cancelled"])
                self.assertEqual(runner.queued_job_ids, [])
                runner._job_done(first.job_id)
                self.assertNotIn(second.job_id, runner.inflight)
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    runtime.execute(job_id=second.job_id)
            finally:
                runner.shutdown()

    def test_upload_bridge_stages_file_for_later_job_submission(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                staged = client.post(
                    "/v1/parse/uploads",
                    data={
                        "doc_id": "doc-bridge-001",
                        "tenant_id": "tenant-alpha",
                        "quota_key": "default-plan",
                        "quota_units": "2",
                    },
                    files={"file": ("manual.txt", b"alpha\nbeta", "text/plain")},
                )
                self.assertEqual(staged.status_code, 201)
                staged_payload = staged.json()
                self.assertEqual(staged_payload["doc_id"], "doc-bridge-001")
                self.assertEqual(staged_payload["state"], "staged")
                self.assertIn("parsecore_server_file_path", staged_payload)
                self.assertTrue(Path(staged_payload["parsecore_server_file_path"]).exists())
                self.assertEqual(staged_payload["profile"], "default")

                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-bridge-001",
                        "file_path": staged_payload["parsecore_server_file_path"],
                        "media_type": "text/plain",
                        "tenant_id": "tenant-alpha",
                        "quota_key": "default-plan",
                        "quota_units": 2,
                        "options": {"enable_ocr": False, "file_name": "manual.txt"},
                    },
                )
                self.assertEqual(created.status_code, 202)
                created_payload = created.json()
                self.assertEqual(created_payload["doc_id"], "doc-bridge-001")
                self.assertEqual(created_payload["tenant_id"], "tenant-alpha")
                self.assertEqual(created_payload["quota_units"], 2)
                self.assertEqual(created_payload["options"]["profile"], "default")

                final_job = None
                for _ in range(20):
                    current = client.get(f"/v1/parse/jobs/{created_payload['job_id']}")
                    self.assertEqual(current.status_code, 200)
                    final_job = current.json()
                    if final_job["state"] == ParseJobState.DONE.value:
                        break

                self.assertIsNotNone(final_job)
                assert final_job is not None
                self.assertEqual(final_job["state"], ParseJobState.DONE.value)

    def test_upload_bridge_path_can_create_job_when_external_paths_disabled(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                staged = client.post(
                    "/v1/parse/uploads",
                    data={"doc_id": "doc-strict-bridge"},
                    files={"file": ("strict.txt", b"strict alpha", "text/plain")},
                )
                self.assertEqual(staged.status_code, 201)
                staged_payload = staged.json()

                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-strict-bridge",
                        "file_path": staged_payload["parsecore_server_file_path"],
                        "media_type": "text/plain",
                    },
                )
                self.assertEqual(created.status_code, 202)

    def test_create_job_persists_top_level_profile_in_options(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            document_path = workspace.create_text_file("profiled.txt", "profile alpha")
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-profile-001",
                        "file_path": str(document_path),
                        "media_type": "text/plain",
                        "profile": "table-heavy",
                    },
                )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["options"]["profile"], "table-heavy")
            self.assertEqual(payload["options"]["requested_profile"], "table-heavy")
            self.assertEqual(payload["options"]["profile_source"], "requested")
            self.assertTrue(payload["options"]["profile_known"])

    def test_create_job_reports_unknown_profile_without_rejecting(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            document_path = workspace.create_text_file("profile-typo.txt", "profile typo")
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-profile-typo",
                        "file_path": str(document_path),
                        "media_type": "text/plain",
                        "profile": "large_pdf",
                    },
                )

            self.assertEqual(response.status_code, 202)
            options = response.json()["options"]
            self.assertEqual(options["profile"], "large_pdf")
            self.assertFalse(options["profile_known"])
            self.assertEqual(options["profile_warning"], "unknown_profile")

    def test_parse_profiles_endpoint_describes_supported_profiles(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.get("/v1/parse/profiles")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["default_profile"], "default")
            self.assertEqual(payload["auto_profile"], "auto")
            self.assertIn("large-pdf", payload["supported_profiles"])
            self.assertIn("scan-pdf", payload["recommended_async_profiles"])
            self.assertEqual(payload["default_auto_rule_thresholds"]["max_page_count"], 500)
            self.assertTrue(
                any(
                    profile["name"] == "large-pdf" and profile["recommended_async"]
                    for profile in payload["profiles"]
                )
            )

            runtime = client.get("/v1/runtime")
            self.assertEqual(runtime.status_code, 200)
            self.assertIn("table-heavy", runtime.json()["profiles"]["supported_profiles"])

    def test_upload_bridge_rejects_non_local_object_store(self) -> None:
        with TemporaryWorkspace(REMOTE_OBJECT_STORE_UPLOAD_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/uploads",
                    data={"doc_id": "doc-non-local-store"},
                    files={"file": ("remote.txt", b"alpha", "text/plain")},
                )

        self.assertEqual(response.status_code, 500)
        payload = response.json()
        self.assertEqual(payload["code"], "staged_upload_requires_local_object_store")
        self.assertEqual(payload["detail"]["object_store"], "s3://parsecore-test/uploads")

    def test_upload_bridge_can_create_job_immediately(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/uploads",
                    data={
                        "doc_id": "doc-bridge-002",
                        "tenant_id": "tenant-beta",
                        "quota_key": "default-plan",
                        "quota_units": "1",
                        "create_job": "true",
                    },
                    files={"file": ("manual.txt", b"bridge upload text", "text/plain")},
                )
                self.assertEqual(created.status_code, 202)
                payload = created.json()
                self.assertEqual(payload["doc_id"], "doc-bridge-002")
                self.assertEqual(payload["tenant_id"], "tenant-beta")
                self.assertEqual(payload["quota_units"], 1)
                self.assertTrue(payload["create_job"])
                self.assertIn("job_id", payload)
                self.assertIn("parsecore_server_file_path", payload)
                self.assertTrue(Path(payload["parsecore_server_file_path"]).exists())

                current = client.get(f"/v1/parse/jobs/{payload['job_id']}")
                self.assertEqual(current.status_code, 200)
                current_payload = current.json()
                self.assertEqual(current_payload["doc_id"], "doc-bridge-002")
                self.assertIn(current_payload["state"], {ParseJobState.PENDING.value, ParseJobState.PARSING.value, ParseJobState.STRUCTURING.value, ParseJobState.DONE.value})

    def test_upload_bridge_uses_staged_limit_and_resolves_profile(self) -> None:
        with TemporaryWorkspace(LIMITED_UPLOAD_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/uploads",
                    data={"doc_id": "doc-large-stage", "profile": "auto"},
                    files={"file": ("ledger.xlsx", b"12345", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                )

            self.assertEqual(response.status_code, 201)
            payload = response.json()
            self.assertEqual(payload["profile"], "excel-ledger")
            self.assertEqual(payload["profile_source"], "auto")
            self.assertTrue(Path(payload["parsecore_server_file_path"]).exists())

    def test_upload_bridge_requires_dedicated_api_key_when_configured(self) -> None:
        with TemporaryWorkspace(UPLOAD_BRIDGE_HARDENED_CONFIG) as workspace:
            with patch.dict(os.environ, {"PARSECORE_UPLOAD_BRIDGE_API_KEY": "bridge-secret"}, clear=False):
                app = create_app(workspace.config_path)
                with TestClient(app) as client:
                    unauthorized = client.post(
                        "/v1/parse/uploads",
                        data={"doc_id": "doc-bridge-protected"},
                        files={"file": ("manual.txt", b"protected", "text/plain")},
                    )
                    self.assertEqual(unauthorized.status_code, 401)
                    self.assertEqual(unauthorized.json()["code"], "upload_bridge_unauthorized")
                    self.assertEqual(unauthorized.headers.get("www-authenticate"), "Bearer")

                    authorized = client.post(
                        "/v1/parse/uploads",
                        data={"doc_id": "doc-bridge-protected"},
                        files={"file": ("manual.txt", b"protected", "text/plain")},
                        headers={"x-api-key": "bridge-secret"},
                    )
                    self.assertEqual(authorized.status_code, 201)
                    self.assertTrue(Path(authorized.json()["parsecore_server_file_path"]).exists())

                    runtime_endpoint = client.get("/v1/runtime")
                    self.assertEqual(runtime_endpoint.status_code, 200)

                    profiles_endpoint = client.get("/v1/parse/profiles")
                    self.assertEqual(profiles_endpoint.status_code, 200)
                    self.assertIn("default", profiles_endpoint.json()["supported_profiles"])

    def test_create_app_fails_fast_when_upload_bridge_api_key_env_is_empty(self) -> None:
        with TemporaryWorkspace(UPLOAD_BRIDGE_HARDENED_CONFIG) as workspace:
            with patch.dict(os.environ, {"PARSECORE_UPLOAD_BRIDGE_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(ValueError, "PARSECORE_UPLOAD_BRIDGE_API_KEY"):
                    create_app(workspace.config_path)

    def test_upload_bridge_cleans_expired_staged_files(self) -> None:
        with TemporaryWorkspace(UPLOAD_BRIDGE_RETENTION_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                first = client.post(
                    "/v1/parse/uploads",
                    data={"doc_id": "doc-bridge-cleanup-1"},
                    files={"file": ("manual.txt", b"cleanup-one", "text/plain")},
                )
                self.assertEqual(first.status_code, 201)
                upload_dir = Path(first.json()["parsecore_server_file_path"]).parent

                expired = upload_dir / "expired-sentinel.txt"
                expired.write_text("old", encoding="utf-8")
                expired_timestamp = time.time() - 120
                os.utime(expired, (expired_timestamp, expired_timestamp))

                fresh = upload_dir / "fresh-sentinel.txt"
                fresh.write_text("new", encoding="utf-8")

                second = client.post(
                    "/v1/parse/uploads",
                    data={"doc_id": "doc-bridge-cleanup-2"},
                    files={"file": ("manual.txt", b"cleanup-two", "text/plain")},
                )
                self.assertEqual(second.status_code, 201)
                self.assertFalse(expired.exists())
                self.assertTrue(fresh.exists())
                self.assertTrue(Path(second.json()["parsecore_server_file_path"]).exists())

    def test_api_key_protects_runtime_endpoint_but_not_health(self) -> None:
        with TemporaryWorkspace(API_KEY_PROTECTED_CONFIG) as workspace:
            with patch.dict(os.environ, {"PARSECORE_API_KEY": "test-secret"}, clear=False):
                app = create_app(workspace.config_path)
                with TestClient(app) as client:
                    health = client.get("/health")
                    self.assertEqual(health.status_code, 200)

                    unauthorized = client.get("/v1/runtime")
                    self.assertEqual(unauthorized.status_code, 401)
                    self.assertEqual(unauthorized.json()["code"], "unauthorized")
                    self.assertEqual(unauthorized.headers.get("www-authenticate"), "Bearer")

                    authorized = client.get(
                        "/v1/runtime",
                        headers={"x-api-key": "test-secret"},
                    )
                    self.assertEqual(authorized.status_code, 200)
                    self.assertEqual(authorized.json()["runtime"]["api_auth_enabled"], True)

                    unauthorized_profiles = client.get("/v1/parse/profiles")
                    self.assertEqual(unauthorized_profiles.status_code, 401)

                    authorized_profiles = client.get(
                        "/v1/parse/profiles",
                        headers={"x-api-key": "test-secret"},
                    )
                    self.assertEqual(authorized_profiles.status_code, 200)
                    self.assertIn("large-pdf", authorized_profiles.json()["supported_profiles"])

    def test_api_key_accepts_bearer_authorization_header(self) -> None:
        with TemporaryWorkspace(API_KEY_PROTECTED_CONFIG) as workspace:
            with patch.dict(os.environ, {"PARSECORE_API_KEY": "test-secret"}, clear=False):
                app = create_app(workspace.config_path)
                with TestClient(app) as client:
                    response = client.get(
                        "/v1/runtime",
                        headers={"Authorization": "Bearer test-secret"},
                    )
                    self.assertEqual(response.status_code, 200)

    def test_create_app_fails_fast_when_api_key_env_is_empty(self) -> None:
        with TemporaryWorkspace(API_KEY_PROTECTED_CONFIG) as workspace:
            with patch.dict(os.environ, {"PARSECORE_API_KEY": ""}, clear=False):
                with self.assertRaisesRegex(ValueError, "PARSECORE_API_KEY"):
                    create_app(workspace.config_path)

    def test_create_job_rejects_external_file_path_when_strict(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            document_path = workspace.create_text_file("outside.txt", "blocked")
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-blocked-001",
                        "file_path": str(document_path),
                        "media_type": "text/plain",
                    },
                )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["code"], "file_path_not_allowed")
        self.assertEqual(payload["detail"]["allow_external_file_paths"], False)

    def test_create_job_accepts_object_store_file_when_strict(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            document_path = workspace.create_text_file("object-store/allowed.txt", "alpha")
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-allowed-001",
                        "file_path": str(document_path),
                        "media_type": "text/plain",
                    },
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["job_id"]

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

    def test_create_job_validates_required_path_payload(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                missing_doc_id = client.post(
                    "/v1/parse/jobs",
                    json={"file_path": str(workspace.root / "object-store" / "x.txt")},
                )
                missing_file_path = client.post(
                    "/v1/parse/jobs",
                    json={"doc_id": "doc-missing-file"},
                )

        self.assertEqual(missing_doc_id.status_code, 400)
        self.assertEqual(missing_doc_id.json()["code"], "missing_doc_id")
        self.assertEqual(missing_file_path.status_code, 400)
        self.assertEqual(missing_file_path.json()["code"], "missing_file_path")

    def test_create_job_rejects_invalid_path_payloads(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            directory_path = workspace.root / "object-store"
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                missing_file = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-missing-path",
                        "file_path": str(directory_path / "missing.txt"),
                        "media_type": "text/plain",
                    },
                )
                directory = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-directory-path",
                        "file_path": str(directory_path),
                        "media_type": "text/plain",
                    },
                )

        self.assertEqual(missing_file.status_code, 400)
        self.assertEqual(missing_file.json()["code"], "invalid_file_path")
        self.assertEqual(directory.status_code, 400)
        self.assertEqual(directory.json()["code"], "invalid_file_path")

    def test_create_job_rejects_resolved_path_traversal_when_strict(self) -> None:
        with TemporaryWorkspace(STRICT_TEXT_API_CONFIG) as workspace:
            outside_path = workspace.create_text_file("outside-traversal.txt", "blocked")
            (workspace.root / "object-store" / "_api_uploads").mkdir(parents=True, exist_ok=True)
            traversal_path = (
                workspace.root
                / "object-store"
                / "_api_uploads"
                / ".."
                / ".."
                / outside_path.name
            )
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-traversal-001",
                        "file_path": str(traversal_path),
                        "media_type": "text/plain",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "file_path_not_allowed")

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
                self.assertEqual(document_payload["index_manifest"]["layers"][0]["name"], "primary")
                self.assertEqual(document_payload["index_manifest"]["layers"][1]["name"], "structure")

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

                high_precision = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params={"q": "maintenance", "tenant_id": "tenant-alpha", "index_layer": "high_precision"},
                )
                self.assertEqual(high_precision.status_code, 200)
                self.assertEqual(high_precision.json()["index_layer"], "high_precision")

                invalid_layer = client.get(
                    "/v1/parse/documents/doc-api-001/search",
                    params={"q": "maintenance", "tenant_id": "tenant-alpha", "index_layer": "invalid"},
                )
                self.assertEqual(invalid_layer.status_code, 400)
                self.assertEqual(invalid_layer.json()["code"], "invalid_index_layer")

                structure = client.get(
                    "/v1/parse/documents/doc-api-001/structure-search",
                    params={"q": "maintenance manual", "tenant_id": "tenant-alpha", "tag": "page:front_matter"},
                )
                self.assertEqual(structure.status_code, 200)
                self.assertEqual(structure.json()["retrieval_mode"], "structure-keyword")
                self.assertGreaterEqual(len(structure.json()["items"]), 1)

                tasks = client.get(
                    "/v1/parse/documents/doc-api-001/tasks/search",
                    params={"q": "maintenance", "tenant_id": "tenant-alpha"},
                )
                self.assertEqual(tasks.status_code, 200)
                self.assertIn("retrieval_mode", tasks.json())

                index_metrics = client.get(
                    "/v1/parse/indexes/metrics",
                    params={"tenant_id": "tenant-alpha", "since_hours": 24},
                )
                self.assertEqual(index_metrics.status_code, 200)
                self.assertIn("structure", index_metrics.json()["layer_counts"])
                self.assertIn("high_precision", index_metrics.json())
                self.assertIn("search_effectiveness", index_metrics.json())
                self.assertIn("search_effectiveness_trends", index_metrics.json())

                custom_trend = client.get(
                    "/v1/parse/indexes/metrics",
                    params=[
                        ("tenant_id", "tenant-alpha"),
                        ("trend_window_hours", "2"),
                        ("trend_window_hours", "12"),
                    ],
                )
                self.assertEqual(custom_trend.status_code, 200)
                self.assertEqual(custom_trend.json()["trend_windows_hours"], [2.0, 12.0])
                self.assertIn("2h", custom_trend.json()["search_effectiveness_trends"])
                self.assertIn("12h", custom_trend.json()["search_effectiveness_trends"])

                invalid_trend = client.get(
                    "/v1/parse/indexes/metrics",
                    params={"tenant_id": "tenant-alpha", "trend_window_hours": "0"},
                )
                self.assertEqual(invalid_trend.status_code, 400)
                self.assertEqual(invalid_trend.json()["code"], "invalid_trend_window_hours")

    def test_document_projection_structured_exposes_v2_tables_and_quality(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            document_path = workspace.create_docx_with_body(
                "table.docx",
                build_docx_table([["Part"], ["Bolt", "2"]]),
            )
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-table-v2",
                        "file_path": str(document_path),
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "options": {"profile": "table-heavy"},
                    },
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["job_id"]

                for _ in range(20):
                    current = client.get(f"/v1/parse/jobs/{job_id}")
                    self.assertEqual(current.status_code, 200)
                    if current.json()["state"] == ParseJobState.DONE.value:
                        break

                structured = client.get(
                    "/v1/parse/documents/doc-table-v2",
                    params={"projection": "structured"},
                )
                self.assertEqual(structured.status_code, 200)
                payload = structured.json()
                self.assertEqual(payload["schema_version"], "2026-06")
                self.assertEqual(payload["projection"], "structured")
                self.assertEqual(payload["profile"], "table-heavy")
                self.assertEqual(payload["profile_resolution"]["resolved_profile"], "table-heavy")
                self.assertEqual(payload["profile_resolution"]["source"], "requested")
                self.assertTrue(payload["profile_resolution"]["profile_known"])
                self.assertNotIn("blocks", payload)
                self.assertEqual(len(payload["tables"]), 1)
                table = payload["tables"][0]
                self.assertEqual(table["header_rows"], 1)
                self.assertEqual(table["cells"][0]["text"], "Part")
                self.assertTrue(any(signal["code"] == "table_ragged_rows" for signal in payload["quality_signals"]))
                self.assertEqual(payload["parse_units"][0]["table_count"], 1)

                quality = client.get("/v1/parse/documents/doc-table-v2/quality")
                self.assertEqual(quality.status_code, 200)
                quality_payload = quality.json()
                self.assertEqual(quality_payload["projection"], "quality")
                self.assertEqual(quality_payload["profile_resolution"]["resolved_profile"], "table-heavy")
                self.assertGreaterEqual(quality_payload["quality_summary"]["total"], 1)

                parts = client.get("/v1/parse/documents/doc-table-v2/parts")
                self.assertEqual(parts.status_code, 200)
                parts_payload = parts.json()
                self.assertEqual(parts_payload["projection"], "parts")
                self.assertEqual(parts_payload["part_summary"]["total"], 1)
                self.assertEqual(parts_payload["part_summary"]["warning_parts"], 1)
                self.assertEqual(parts_payload["parts"][0]["state"], "warning")
                self.assertIn("table_ragged_rows", parts_payload["parts"][0]["quality_signal_codes"])
                self.assertFalse(parts_payload["parts"][0]["rerun_supported"])

                filtered_parts = client.get(
                    "/v1/parse/documents/doc-table-v2/parts",
                    params={"state": "warning|failed"},
                )
                self.assertEqual(filtered_parts.status_code, 200)
                self.assertEqual(filtered_parts.json()["part_summary"]["filtered"], 1)

                invalid_part_filter = client.get(
                    "/v1/parse/documents/doc-table-v2/parts",
                    params={"state": "bogus"},
                )
                self.assertEqual(invalid_part_filter.status_code, 400)
                self.assertEqual(invalid_part_filter.json()["code"], "invalid_part_state")

                exported_tables = client.get(
                    "/v1/parse/documents/doc-table-v2/exports",
                    params={"dataset": "tables", "format": "csv"},
                )
                self.assertEqual(exported_tables.status_code, 200)
                self.assertEqual(exported_tables.headers["content-type"], "text/csv; charset=utf-8")
                self.assertIn("doc-table-v2-tables.csv", exported_tables.headers["content-disposition"])
                self.assertIn("table_id", exported_tables.text)
                self.assertIn("doc-table-v2:p1:t1", exported_tables.text)

                exported_signals = client.get(
                    "/v1/parse/documents/doc-table-v2/exports",
                    params={"dataset": "quality_signals", "format": "jsonl"},
                )
                self.assertEqual(exported_signals.status_code, 200)
                self.assertEqual(exported_signals.headers["content-type"], "application/x-ndjson; charset=utf-8")
                self.assertIn('"code":"table_ragged_rows"', exported_signals.text)

                invalid_export = client.get(
                    "/v1/parse/documents/doc-table-v2/exports",
                    params={"dataset": "pages", "format": "jsonl"},
                )
                self.assertEqual(invalid_export.status_code, 400)
                self.assertEqual(invalid_export.json()["code"], "invalid_export_dataset")

                export_job = client.post(
                    "/v1/parse/documents/doc-table-v2/export-jobs",
                    json={
                        "include": ["tables", "quality_signals"],
                        "formats": {"tables": "csv", "quality_signals": "jsonl"},
                        "filters": {"severity": ["warning"]},
                    },
                )
                self.assertEqual(export_job.status_code, 202)
                export_payload = export_job.json()
                self.assertEqual(export_payload["state"], "done")
                self.assertEqual(export_payload["doc_id"], "doc-table-v2")
                self.assertEqual(
                    [(item["dataset"], item["format"]) for item in export_payload["files"]],
                    [("tables", "csv"), ("quality_signals", "jsonl")],
                )

                export_manifest = client.get(f"/v1/parse/export-jobs/{export_payload['export_id']}")
                self.assertEqual(export_manifest.status_code, 200)
                self.assertEqual(export_manifest.json()["export_id"], export_payload["export_id"])
                self.assertEqual(export_manifest.json()["manifest_schema_version"], "2026-05")
                self.assertEqual(export_manifest.json()["tenant_id"], "default")
                self.assertEqual(export_manifest.json()["request"]["include"], ["tables", "quality_signals"])
                self.assertEqual(export_manifest.json()["request"]["filters"], {"severity": ["warning"]})
                self.assertEqual(export_manifest.json()["files"][0]["records"], 1)

                export_download = client.get(
                    f"/v1/parse/export-jobs/{export_payload['export_id']}/download",
                    params={"file": "quality_signals.jsonl"},
                )
                self.assertEqual(export_download.status_code, 200)
                self.assertIn("table_ragged_rows", export_download.text)

                compat = client.get(
                    "/v1/parse/documents/doc-table-v2",
                    params={"projection": "compat"},
                )
                self.assertEqual(compat.status_code, 200)
                compat_payload = compat.json()
                self.assertEqual(compat_payload["schema_version"], "2026-04")
                self.assertEqual(compat_payload["projection"], "compat")
                self.assertNotIn("profile_resolution", compat_payload)
                self.assertIn("pages", compat_payload)

    def test_pdf_parts_plan_endpoint_creates_child_jobs_and_parent_read_model(self) -> None:
        with TemporaryWorkspace(PDF_API_CONFIG) as workspace:
            document_path = workspace.create_pdf("partitioned-api.pdf", [["one"], ["two"], ["three"]])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-pdf-api-parts",
                        "file_path": str(document_path),
                        "media_type": "application/pdf",
                        "profile": "large-pdf",
                    },
                )
                self.assertEqual(created.status_code, 202)
                source_job_id = created.json()["job_id"]
                for _ in range(30):
                    current = client.get(f"/v1/parse/jobs/{source_job_id}")
                    self.assertEqual(current.status_code, 200)
                    if current.json()["state"] == ParseJobState.DONE.value:
                        break

                planned = client.post(
                    "/v1/parse/documents/doc-pdf-api-parts/parts/plan",
                    json={"target_pages_per_part": 2},
                )
                self.assertEqual(planned.status_code, 202)
                plan_payload = planned.json()
                self.assertEqual(plan_payload["total_pages"], 3)
                self.assertEqual(len(plan_payload["parts"]), 2)
                parent_job_id = plan_payload["parent_job"]["job_id"]

                parent = None
                for _ in range(50):
                    current = client.get(f"/v1/parse/jobs/{parent_job_id}")
                    self.assertEqual(current.status_code, 200)
                    parent = current.json()
                    if parent["state"] == ParseJobState.DONE.value:
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(parent)
                assert parent is not None
                self.assertEqual(parent["state"], ParseJobState.DONE.value)

                parts = client.get("/v1/parse/documents/doc-pdf-api-parts/parts")
                self.assertEqual(parts.status_code, 200)
                parts_payload = parts.json()
                self.assertEqual(parts_payload["part_summary"]["total"], 2)
                self.assertEqual(parts_payload["part_summary"]["states"], {"done": 2})
                self.assertTrue(all(part["rerun_supported"] for part in parts_payload["parts"]))

                structured = client.get(
                    "/v1/parse/documents/doc-pdf-api-parts",
                    params={"projection": "structured"},
                )
                self.assertEqual(structured.status_code, 200)
                structured_payload = structured.json()
                self.assertEqual([page["page_number"] for page in structured_payload["pages"]], [1, 2, 3])
                self.assertEqual(len(structured_payload["parse_units"]), 2)

                rerun = client.post(
                    "/v1/parse/documents/doc-pdf-api-parts/parts/doc-pdf-api-parts-part-2/rerun"
                )
                self.assertEqual(rerun.status_code, 202)
                rerun_job_id = rerun.json()["job"]["job_id"]
                for _ in range(30):
                    current = client.get(f"/v1/parse/jobs/{rerun_job_id}")
                    self.assertEqual(current.status_code, 200)
                    if current.json()["state"] == ParseJobState.DONE.value:
                        break
                    time.sleep(0.05)
                rerun_parts = client.get("/v1/parse/documents/doc-pdf-api-parts/parts")
                self.assertEqual(rerun_parts.status_code, 200)
                part_two = next(
                    part for part in rerun_parts.json()["parts"] if part["part_id"] == "doc-pdf-api-parts-part-2"
                )
                self.assertEqual(part_two["state"], "done")
                self.assertEqual(part_two["job_id"], rerun_job_id)

                invalid = client.get(
                    "/v1/parse/documents/doc-pdf-api-parts",
                    params={"projection": "future"},
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(invalid.json()["code"], "invalid_projection")

    def test_pdf_parts_batch_rerun_and_cancel_pending_part(self) -> None:
        queue_config = PDF_API_CONFIG.replace('execution_mode = "inline"', 'execution_mode = "queue-worker"')
        with TemporaryWorkspace(queue_config) as workspace:
            document_path = workspace.create_pdf("partitioned-api-queue.pdf", [["one"], ["two"], ["three"]])
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                created = client.post(
                    "/v1/parse/jobs",
                    json={
                        "doc_id": "doc-pdf-api-control",
                        "file_path": str(document_path),
                        "media_type": "application/pdf",
                        "profile": "large-pdf",
                    },
                )
                self.assertEqual(created.status_code, 202)

                planned = client.post(
                    "/v1/parse/documents/doc-pdf-api-control/parts/plan",
                    json={"target_pages_per_part": 1, "max_active_parts_per_doc": 1},
                )
                self.assertEqual(planned.status_code, 202)
                self.assertEqual(len(planned.json()["part_jobs"]), 3)

                cancelled = client.post(
                    "/v1/parse/documents/doc-pdf-api-control/parts/doc-pdf-api-control-part-2/cancel"
                )
                self.assertEqual(cancelled.status_code, 202)
                self.assertTrue(cancelled.json()["cancelled"])
                self.assertEqual(cancelled.json()["state"], "cancelled")

                parts = client.get(
                    "/v1/parse/documents/doc-pdf-api-control/parts",
                    params={"state": "cancelled"},
                )
                self.assertEqual(parts.status_code, 200)
                self.assertEqual(parts.json()["part_summary"]["filtered"], 1)
                self.assertEqual(parts.json()["parts"][0]["last_error"], "cancelled")

                malformed = client.post(
                    "/v1/parse/documents/doc-pdf-api-control/parts/rerun",
                    json={"part_ids": {}, "failed_only": False},
                )
                self.assertEqual(malformed.status_code, 400)
                self.assertEqual(malformed.json()["code"], "invalid_part_ids")

                no_failed = client.post(
                    "/v1/parse/documents/doc-pdf-api-control/parts/rerun",
                    json={"part_ids": ["doc-pdf-api-control-part-1"]},
                )
                self.assertEqual(no_failed.status_code, 409)
                self.assertEqual(no_failed.json()["submitted"], [])

                rerun = client.post(
                    "/v1/parse/documents/doc-pdf-api-control/parts/rerun",
                    json={"failed_only": True},
                )
                self.assertEqual(rerun.status_code, 202)
                self.assertEqual(
                    [item["part_id"] for item in rerun.json()["submitted"]],
                    ["doc-pdf-api-control-part-2"],
                )

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
            self.assertEqual(body["pages"][0]["page_type"], "front_matter")
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
            self.assertEqual(body["pages"][0]["page_type"], "front_matter")
            self.assertIn("Maintenance Manual", body["pages"][0]["text"])
            self.assertIn("quality", body)
            self.assertIn("raw_quality", body)
            self.assertIn("output_quality", body)
            self.assertEqual(body["quality"]["score"], body["output_quality"]["score"])

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
            self.assertIn("service_details", body)
            self.assertEqual(body["service_details"]["pdfplumber"]["registered"], True)
            self.assertEqual(body["service_details"]["pdfplumber"]["reason"], "ok")

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

    def test_parse_batch_endpoint_rejects_oversized_file(self) -> None:
        with TemporaryWorkspace(LIMITED_UPLOAD_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": base64.b64encode(b"12345").decode("ascii"),
                        "file_name": "too-big.txt",
                        "media_type": "text/plain",
                    },
                )

            self.assertEqual(response.status_code, 413)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertFalse(body["success"])
            self.assertEqual(body["parser_used"], "none")
            self.assertEqual(body["code"], "document_too_large_for_sync")
            self.assertEqual(body["message"], "Document is too large for synchronous parsing; use the asynchronous upload/job flow")
            self.assertEqual(body["detail"]["actual_bytes"], 5)
            self.assertEqual(body["detail"]["limit_bytes"], 4)
            self.assertEqual(body["detail"]["recommended_endpoint"], "/v1/parse/uploads")
            self.assertEqual(body["detail"]["recommended_job_endpoint"], "/v1/parse/jobs")
            self.assertEqual(body["detail"]["profile"], "auto")

    def test_parse_batch_profile_can_recommend_async_before_hard_limit(self) -> None:
        encoded = base64.b64encode(b"alpha").decode("ascii")
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                blocked = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": encoded,
                        "file_name": "manual.txt",
                        "media_type": "text/plain",
                        "profile": "large-pdf",
                    },
                )
                forced = client.post(
                    "/v1/parse/batch",
                    json={
                        "file_base64": encoded,
                        "file_name": "manual.txt",
                        "media_type": "text/plain",
                        "profile": "large-pdf",
                        "force_sync": True,
                    },
                )

            self.assertEqual(blocked.status_code, 413)
            blocked_body = blocked.json()
            self.assertFalse(blocked_body["success"])
            self.assertEqual(blocked_body["code"], "document_too_large_for_sync")
            self.assertEqual(blocked_body["detail"]["resolved_profile"], "large-pdf")
            self.assertEqual(forced.status_code, 200)
            self.assertTrue(forced.json()["success"])

    def test_parse_upload_endpoint_rejects_oversized_file(self) -> None:
        with TemporaryWorkspace(LIMITED_UPLOAD_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/parse",
                    files={"file": ("too-big.txt", b"12345", "text/plain")},
                )

            self.assertEqual(response.status_code, 413)
            self.assertIn("x-trace-id", response.headers)
            body = response.json()
            self.assertEqual(body["error"], "document_too_large_for_sync")
            self.assertEqual(body["code"], "document_too_large_for_sync")
            self.assertEqual(body["message"], "Document is too large for synchronous parsing; use the asynchronous upload/job flow")
            self.assertEqual(body["detail"]["actual_bytes"], 5)
            self.assertEqual(body["detail"]["limit_bytes"], 4)
            self.assertEqual(body["detail"]["recommended_endpoint"], "/v1/parse/uploads")
            self.assertEqual(body["detail"]["recommended_job_endpoint"], "/v1/parse/jobs")
            self.assertEqual(body["detail"]["profile"], "auto")

    def test_parse_upload_profile_can_recommend_async_before_hard_limit(self) -> None:
        with TemporaryWorkspace(TEXT_API_CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                blocked = client.post(
                    "/v1/parse",
                    data={"profile": "large-pdf"},
                    files={"file": ("manual.txt", b"alpha", "text/plain")},
                )
                forced = client.post(
                    "/v1/parse",
                    data={"profile": "large-pdf", "force_sync": "true"},
                    files={"file": ("manual.txt", b"alpha", "text/plain")},
                )

            self.assertEqual(blocked.status_code, 413)
            blocked_body = blocked.json()
            self.assertEqual(blocked_body["code"], "document_too_large_for_sync")
            self.assertEqual(blocked_body["detail"]["resolved_profile"], "large-pdf")
            self.assertEqual(forced.status_code, 200)

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

    def test_project_pages_title_only_page_keeps_tables_schema(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-title-only",
                    doc_id="doc-title-only",
                    type=BlockType.TITLE,
                    content="Maintenance Overview",
                    metadata={"page": 1, "semantic_role": "title", "parser": "pdf-text"},
                ),
            )
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["page_type"], "cover")
        self.assertEqual(pages[0]["tables_markdown"], [])
        self.assertEqual(pages[0]["tables"], [])
        self.assertEqual(pages[0]["text"], "")

    def test_project_pages_includes_ocr_decision_fields(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-ocr-1",
                    doc_id="doc-ocr-page",
                    type=BlockType.PARAGRAPH,
                    content="Recovered OCR text",
                    metadata={
                        "page": 1,
                        "semantic_role": "paragraph",
                        "ocr_attempted": True,
                        "ocr_fallback_used": True,
                        "ocr_attempt_reason": "cid_dense",
                        "ocr_acceptance_reason": "fallback_applied",
                        "native_text_token_count": 4,
                        "final_text_token_count": 12,
                    },
                ),
            )
        )

        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0]["ocr_attempted"])
        self.assertTrue(pages[0]["ocr_fallback"])
        self.assertEqual(pages[0]["ocr_attempt_reasons"], ["cid_dense"])
        self.assertEqual(pages[0]["ocr_acceptance_reasons"], ["fallback_applied"])
        self.assertEqual(pages[0]["native_text_token_count"], 4)
        self.assertEqual(pages[0]["final_text_token_count"], 12)

    def test_project_pages_schema_contract_for_mixed_page_variants(self) -> None:
        pages = _project_pages(
            (
                Block(
                    block_id="blk-p1-title",
                    doc_id="doc-schema-matrix",
                    type=BlockType.TITLE,
                    content="Cover",
                    metadata={"page": 1, "semantic_role": "title", "parser": "pdf-text"},
                ),
                Block(
                    block_id="blk-p2-table",
                    doc_id="doc-schema-matrix",
                    type=BlockType.TABLE,
                    content="| col | value |",
                    metadata={"page": 2, "semantic_role": "table", "parser": "pdf-text"},
                ),
                Block(
                    block_id="blk-p3-ocr-rejected",
                    doc_id="doc-schema-matrix",
                    type=BlockType.PARAGRAPH,
                    content="native unreadable text",
                    metadata={
                        "page": 3,
                        "semantic_role": "paragraph",
                        "ocr_attempted": True,
                        "ocr_rejected": True,
                        "ocr_attempt_reason": "cid_dense",
                        "ocr_rejection_reason": "provider_request_failed",
                        "ocr_error_reason": "provider_request_failed",
                        "native_text_token_count": 2,
                        "final_text_token_count": 2,
                    },
                ),
            )
        )

        self.assertEqual(len(pages), 3)
        for page in pages:
            self.assertIn("page_number", page)
            self.assertIn("page_type", page)
            self.assertIn("text", page)
            self.assertIn("tables_markdown", page)
            self.assertIn("tables", page)
            self.assertIn("artifacts", page)
            self.assertIn("confidence", page)

        self.assertEqual(pages[0]["page_number"], 1)
        self.assertEqual(pages[0]["tables"], [])
        self.assertEqual(pages[0]["text"], "")

        self.assertEqual(pages[1]["page_number"], 2)
        self.assertEqual(pages[1]["tables_markdown"], ["| col | value |"])
        self.assertEqual(len(pages[1]["tables"]), 1)

        self.assertEqual(pages[2]["page_number"], 3)
        self.assertTrue(pages[2]["ocr_attempted"])
        self.assertTrue(pages[2]["ocr_rejected"])
        self.assertEqual(pages[2]["ocr_attempt_reasons"], ["cid_dense"])
        self.assertEqual(pages[2]["ocr_rejection_reasons"], ["provider_request_failed"])

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
                            "ocr_rejection_reason": "provider_request_failed",
                            "ocr_error_reason": "provider_request_failed",
                            "native_text_token_count": 0,
                            "final_text_token_count": 0,
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
                            "ocr_acceptance_reason": "fallback_applied",
                            "native_text_token_count": 8,
                            "final_text_token_count": 26,
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
                        attempt_data["events"][0]["acceptance_reasons"],
                        ["fallback_applied"],
                    )
                    self.assertEqual(
                        attempt_data["events"][0]["rejection_reasons"],
                        ["provider_request_failed"],
                    )
                    self.assertEqual(attempt_data["events"][0]["native_text_token_count"], 8)
                    self.assertEqual(attempt_data["events"][0]["final_text_token_count"], 26)
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

                    rejected_response = client.get(
                        "/v1/parse/events",
                        params={"event_type": "ocr_rejected", "tenant_id": "ocr-tenant"},
                    )
                    self.assertEqual(rejected_response.status_code, 200)
                    rejected_data = rejected_response.json()
                    self.assertEqual(len(rejected_data["events"]), 1)
                    self.assertEqual(rejected_data["events"][0]["page_count"], 1)
                    self.assertEqual(
                        rejected_data["events"][0]["rejection_reasons"],
                        ["provider_request_failed"],
                    )
                    self.assertEqual(
                        rejected_data["counters"]["ocr-tenant:ocr-plan:ocr_rejected"],
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
                    self.assertIn(
                        'parse_ocr_rejected_total{tenant_id="ocr-tenant",quota_key="ocr-plan"} 1',
                        metrics_text,
                    )

    def test_parse_batch_response_includes_ocr_decision_trace(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                runtime_obj = app.state.runtime
                doc = workspace.create_docx("ocr-trace-batch.docx", ["content"])
                blocks = (
                    Block(
                        block_id="blk-ocr-trace-1",
                        doc_id="doc-ocr-trace",
                        type=BlockType.PARAGRAPH,
                        content="(cid:12) (cid:34)",
                        metadata={
                            "page": 1,
                            "ocr_attempted": True,
                            "ocr_attempt_reason": "cid_dense",
                            "ocr_fallback_used": True,
                            "ocr_acceptance_reason": "fallback_applied",
                            "native_text_token_count": 2,
                            "final_text_token_count": 10,
                        },
                    ),
                )
                with patch.object(runtime_obj, "_load_blocks_for_request", return_value=blocks), patch.object(
                    runtime_obj,
                    "_load_chunks_for_request",
                    return_value=(),
                ):
                    payload = {
                        "file_base64": base64.b64encode(doc.read_bytes()).decode("ascii"),
                        "file_name": doc.name,
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "ocr-trace-tenant",
                        "quota_key": "ocr-trace-plan",
                    }
                    response = client.post("/v1/parse/batch", json=payload)

                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertIn("ocr_decision_trace", body)
                self.assertEqual(body["ocr_decision_trace"]["ocr_attempted_pages"], 1)
                self.assertEqual(body["ocr_decision_trace"]["ocr_fallback_pages"], 1)
                self.assertEqual(body["ocr_decision_trace"]["ocr_rejected_pages"], 0)
                self.assertEqual(body["ocr_decision_trace"]["native_text_token_count"], 2)
                self.assertEqual(body["ocr_decision_trace"]["final_text_token_count"], 10)

    def test_parse_batch_payload_schema_snapshot_for_ocr_trace(self) -> None:
        with TemporaryWorkspace(SAMPLE_CONFIG) as workspace:
            app = create_app(config_path=workspace.config_path)
            with TestClient(app) as client:
                runtime_obj = app.state.runtime
                doc = workspace.create_docx("ocr-trace-schema.docx", ["content"])
                blocks = (
                    Block(
                        block_id="blk-snapshot-1",
                        doc_id="doc-ocr-schema",
                        type=BlockType.PARAGRAPH,
                        content="native unreadable",
                        metadata={
                            "page": 1,
                            "ocr_attempted": True,
                            "ocr_attempt_reason": "cid_dense",
                            "ocr_rejected": True,
                            "ocr_rejection_reason": "provider_request_failed",
                            "ocr_error_reason": "provider_request_failed",
                            "native_text_token_count": 2,
                            "final_text_token_count": 2,
                        },
                    ),
                    Block(
                        block_id="blk-snapshot-2",
                        doc_id="doc-ocr-schema",
                        type=BlockType.PARAGRAPH,
                        content="Recovered readable OCR text",
                        metadata={
                            "page": 2,
                            "ocr_attempted": True,
                            "ocr_attempt_reason": "cid_dense",
                            "ocr_fallback_used": True,
                            "ocr_acceptance_reason": "fallback_applied",
                            "native_text_token_count": 1,
                            "final_text_token_count": 5,
                        },
                    ),
                )
                with patch.object(runtime_obj, "_load_blocks_for_request", return_value=blocks), patch.object(
                    runtime_obj,
                    "_load_chunks_for_request",
                    return_value=(),
                ):
                    payload = {
                        "file_base64": base64.b64encode(doc.read_bytes()).decode("ascii"),
                        "file_name": doc.name,
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "tenant_id": "ocr-schema-tenant",
                        "quota_key": "ocr-schema-plan",
                    }
                    response = client.post("/v1/parse/batch", json=payload)

                self.assertEqual(response.status_code, 200)
                body = response.json()

                self.assertTrue(
                    {
                        "success",
                        "total_pages",
                        "pages",
                        "parser_used",
                        "quality",
                        "raw_quality",
                        "output_quality",
                        "ocr_decision_trace",
                        "error",
                    }.issubset(set(body.keys()))
                )

                trace = body["ocr_decision_trace"]
                self.assertEqual(
                    set(trace.keys()),
                    {
                        "ocr_attempted_pages",
                        "ocr_fallback_pages",
                        "ocr_rejected_pages",
                        "ocr_failed_pages",
                        "native_text_token_count",
                        "final_text_token_count",
                        "ocr_attempt_reasons",
                        "ocr_acceptance_reasons",
                        "ocr_rejection_reasons",
                        "ocr_error_reasons",
                    },
                )
                self.assertEqual(trace["ocr_attempted_pages"], 2)
                self.assertEqual(trace["ocr_fallback_pages"], 1)
                self.assertEqual(trace["ocr_rejected_pages"], 1)
                self.assertEqual(trace["ocr_failed_pages"], 1)
                self.assertEqual(trace["native_text_token_count"], 3)
                self.assertEqual(trace["final_text_token_count"], 7)

                page_required_keys = {
                    "page_number",
                    "page_type",
                    "text",
                    "tables_markdown",
                    "tables",
                    "artifacts",
                    "confidence",
                }
                for page in body["pages"]:
                    self.assertTrue(page_required_keys.issubset(set(page.keys())))

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
