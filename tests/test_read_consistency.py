"""Reproduce the documented failure: OLD published, NEW rerun fails, read mixes them.

Documented evidence (docs TODO §2):
  "先发布 OLD，随后解析 NEW 并使分块失败，读取返回 NEW 正文，却仍标注 OLD 的
   published/pinned 身份。"

This test asserts the required behaviour, so it fails while the bug exists: after
a failed NEW rerun the formal read must serve OLD content with OLD identity.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, ParseRequest
from parsecore.run_store import RunArtifacts
from tests.support import TemporaryWorkspace

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

CONFIG = """
[project]
name = "test-legacy-read-consistency"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
max_attempts = 1

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


class PublishedResultSurvivesFailedRerunTests(unittest.TestCase):
    def test_failed_rerun_keeps_the_published_content_and_identity(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)

            # 1. Publish OLD.
            published = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="old-b1",
                            doc_id="doc-1",
                            type=BlockType.PARAGRAPH,
                            content="OLD content",
                            metadata={"page": 1},
                        ),
                    ),
                    views={"pages": [{"page_number": 1, "text": "OLD content"}]},
                ),
                job_id="job-old",
                source_version="sha256:old",
                validation_status="passed",
            )
            self.assertTrue(published["published"])

            # 2. Rerun with NEW content, then make chunking fail.
            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch.object(
                runtime,
                "_load_chunks_for_request",
                side_effect=RuntimeError("injected chunk failure"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.submit(
                        ParseRequest(
                            doc_id="doc-1",
                            file_path=str(document_path),
                            media_type=DOCX_MEDIA_TYPE,
                        )
                    )

            # 3. The formal read must still be OLD, content and identity together.
            snapshot = runtime.get_document(doc_id="doc-1")
            context = snapshot["publication"]

        contents = [block.content for block in snapshot["blocks"]]
        self.assertIn("OLD content", contents, f"formal read served {contents!r}")
        self.assertNotIn("NEW content", contents)
        self.assertTrue(context["pinned"])
        self.assertEqual(context["served_run_id"], published["parse_run_id"])
        self.assertEqual(context["consumer_signal"], "published")

    def test_the_read_names_the_job_behind_the_run_it_served(self) -> None:
        # Regressed case: the published run held OLD content while a newer
        # unpublished run existed, so the snapshot reported the *newest* job next
        # to OLD content.  Every projection and the export payload derive the
        # version identity from `snapshot["job"]`, so the read has to name the job
        # behind the run it served, and the newer run appears as the preview.
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)

            published = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="old-b1",
                            doc_id="doc-1",
                            type=BlockType.PARAGRAPH,
                            content="OLD content",
                            metadata={"page": 1},
                        ),
                    ),
                    views={"pages": [{"page_number": 1, "text": "OLD content"}]},
                ),
                job_id="job-old",
                source_version="sha256:old",
                validation_status="passed",
            )

            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                newer = runtime.submit(
                    ParseRequest(
                        doc_id="doc-1",
                        file_path=str(document_path),
                        media_type=DOCX_MEDIA_TYPE,
                    )
                )

            snapshot = runtime.get_document(doc_id="doc-1")
            state = runtime.publication_state(doc_id="doc-1")
            records = runtime.get_document_records_projection(doc_id="doc-1", limit=5)
            runs = runtime.list_runs(doc_id="doc-1")

        self.assertEqual(newer.job.state.value, "done")
        self.assertNotEqual(newer.job.job_id, "job-old")
        # The served version's identity, not the newest job's.
        self.assertEqual(snapshot["job"].job_id, "job-old")
        self.assertEqual(snapshot["publication"]["served_run_id"], published["parse_run_id"])
        self.assertTrue(snapshot["publication"]["pinned"])
        # The newer run is reported as the preview, with its own identity.
        preview_runs = [run for run in runs if run.parse_run_id == state["preview_run_id"]]
        self.assertTrue(preview_runs)
        self.assertEqual(preview_runs[0].job_id, newer.job.job_id)
        self.assertNotEqual(state["preview_run_id"], published["parse_run_id"])
        self.assertIn("OLD content", [block.content for block in snapshot["blocks"]])
        # An export states the same version it served.
        self.assertEqual((records or {}).get("parse_run_id"), "job-old")

    def test_the_projection_payload_reports_the_served_identity(self) -> None:
        from parsecore.api_payloads import _document_projection
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            published = runtime.publish_run(
                doc_id="doc-1",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="old-b1",
                            doc_id="doc-1",
                            type=BlockType.PARAGRAPH,
                            content="OLD content",
                            metadata={"page": 1},
                        ),
                    ),
                    views={"pages": [{"page_number": 1, "text": "OLD content"}]},
                ),
                job_id="job-old",
                source_version="sha256:old",
                validation_status="passed",
            )
            document_path = workspace.create_docx("new.docx", ["NEW content"])
            with patch("parsecore.runtime.validate_run", return_value=warning_report):
                runtime.submit(
                    ParseRequest(
                        doc_id="doc-1",
                        file_path=str(document_path),
                        media_type=DOCX_MEDIA_TYPE,
                    )
                )
            snapshot = runtime.get_document(doc_id="doc-1")
            payload = _document_projection(snapshot, projection="structured")

        self.assertEqual(payload["parse_run_id"], "job-old")
        self.assertEqual(payload["publication"]["served_run_id"], published["parse_run_id"])
        self.assertFalse(payload["publication"]["latest_job_used_as_formal_version"])

    def test_every_export_dataset_states_the_version_it_served(self) -> None:
        # pages / lines / coverage / reader each build their payload from the same
        # `get_document` snapshot, so all four must carry the served version's
        # identity -- not the identity of the newer preview run that exists beside
        # it.  records and the structured projection are covered above.
        from starlette.testclient import TestClient

        from parsecore.asgi import create_app
        from parsecore.publication import ValidationStatus
        from parsecore.validation import ValidationReport

        warning_report = ValidationReport(
            status=ValidationStatus.WARNING,
            reason="suspect_signature_pages",
            warnings=("suspect_signature_pages",),
        )
        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime
                published = runtime.publish_run(
                    doc_id="doc-1",
                    artifacts=RunArtifacts(
                        blocks=(
                            Block(
                                block_id="old-b1",
                                doc_id="doc-1",
                                type=BlockType.PARAGRAPH,
                                content="OLD content",
                                metadata={"page": 1},
                            ),
                        ),
                        views={
                            "pages": [{"page_number": 1, "text": "OLD content"}],
                            "lines": [{"line_id": "old-b1:line:1", "page_number": 1, "text": "OLD content"}],
                            "records": [
                                {
                                    "record_id": "old-b1:text:r1:l1",
                                    "block_id": "old-b1",
                                    "page_start": 1,
                                    "page_end": 1,
                                    "fields": {"text": "OLD content"},
                                }
                            ],
                        },
                    ),
                    job_id="job-old",
                    source_version="sha256:old",
                    validation_status="passed",
                )
                document_path = workspace.create_docx("new.docx", ["NEW content"])
                with patch("parsecore.runtime.validate_run", return_value=warning_report):
                    runtime.submit(
                        ParseRequest(
                            doc_id="doc-1",
                            file_path=str(document_path),
                            media_type=DOCX_MEDIA_TYPE,
                        )
                    )
                exported = {
                    dataset: client.get(
                        "/v1/parse/documents/doc-1/exports",
                        params={"dataset": dataset, "format": "jsonl"},
                    )
                    for dataset in ("pages", "lines", "coverage", "reader")
                }

        self.assertEqual(published["publication_generation"], 1)
        for dataset, response in exported.items():
            self.assertEqual(response.status_code, 200, dataset)
            first_line = response.text.strip().splitlines()[0]
            row = json.loads(first_line)
            self.assertEqual(row.get("parse_run_id"), "job-old", dataset)
            self.assertEqual(row.get("doc_id"), "doc-1", dataset)

    def test_failed_first_run_is_never_reported_as_formal(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            document_path = workspace.create_docx("only.docx", ["ONLY content"])
            with patch.object(
                runtime,
                "_load_chunks_for_request",
                side_effect=RuntimeError("injected chunk failure"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.submit(
                        ParseRequest(
                            doc_id="doc-1",
                            file_path=str(document_path),
                            media_type=DOCX_MEDIA_TYPE,
                        )
                    )
            context = runtime.get_document(doc_id="doc-1")["publication"]

        self.assertFalse(context["pinned"])
        self.assertIsNone(context["served_run_id"])
        self.assertEqual(context["consumer_signal"], "not_ready")

    def test_combined_reader_and_records_export_uses_one_published_run(self) -> None:
        from starlette.testclient import TestClient

        from parsecore.asgi import create_app

        isolated_config = CONFIG.replace(
            'object_store = "local://./var/uploads"',
            'object_store = "local://__OBJECT_STORE__"',
        )
        with TemporaryWorkspace(isolated_config) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                runtime = client.app.state.runtime

                def publish_run(*, job_id: str, marker: str) -> None:
                    result = runtime.publish_run(
                        doc_id="doc-1",
                        artifacts=RunArtifacts(
                            blocks=(
                                Block(
                                    block_id=f"{marker.lower()}-block",
                                    doc_id="doc-1",
                                    type=BlockType.PARAGRAPH,
                                    content=f"{marker} reader marker",
                                    metadata={"page": 1},
                                ),
                            ),
                            views={
                                "pages": [
                                    {"page_number": 1, "text": f"{marker} reader marker"}
                                ],
                                "records": [
                                    {
                                        "record_id": f"{marker.lower()}-record-page-two",
                                        "doc_id": "doc-1",
                                        "page_start": 2,
                                        "fields": {"text": f"{marker} record marker"},
                                    },
                                    {
                                        "record_id": f"{marker.lower()}-record-invalid-page",
                                        "doc_id": "doc-1",
                                        "page_start": "unknown",
                                        "fields": {"text": f"{marker} record marker"},
                                    },
                                    {
                                        "record_id": f"{marker.lower()}-record-page-one",
                                        "doc_id": "doc-1",
                                        "page": 1,
                                        "fields": {"text": f"{marker} record marker"},
                                    },
                                ]
                            },
                        ),
                        job_id=job_id,
                        source_version=f"sha256:{marker.lower()}",
                        validation_status="passed",
                    )
                    assert result["published"]

                publish_run(job_id="job-old", marker="OLD")
                original_get_document = runtime.get_document
                publication_switched = False

                def read_old_then_publish_new(**kwargs):
                    nonlocal publication_switched
                    snapshot = original_get_document(**kwargs)
                    if not publication_switched:
                        publication_switched = True
                        publish_run(job_id="job-new", marker="NEW")
                    return snapshot

                with patch.object(runtime, "get_document", side_effect=read_old_then_publish_new):
                    first_response = client.post(
                        "/v1/parse/documents/doc-1/export-jobs",
                        json={
                            "include": ["reader", "records"],
                            "formats": {"reader": "jsonl", "records": "jsonl"},
                        },
                    )

                self.assertTrue(publication_switched)
                first_job = first_response.json()
                first_download_manifest = client.get(first_job["download_endpoint"])
                first_reader = client.get(
                    first_job["download_endpoint"], params={"file": "reader.jsonl"}
                )
                first_records = client.get(
                    first_job["download_endpoint"], params={"file": "records.jsonl"}
                )

                second_response = client.post(
                    "/v1/parse/documents/doc-1/export-jobs",
                    json={
                        "include": ["reader", "records"],
                        "formats": {"reader": "jsonl", "records": "jsonl"},
                    },
                )
                second_job = second_response.json()
                second_download_manifest = client.get(second_job["download_endpoint"])
                second_reader = client.get(
                    second_job["download_endpoint"], params={"file": "reader.jsonl"}
                )
                second_records = client.get(
                    second_job["download_endpoint"], params={"file": "records.jsonl"}
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_download_manifest.status_code, 200)
        self.assertEqual(first_download_manifest.json()["parse_run_id"], "job-old")
        self.assertEqual(first_job["parse_run_id"], "job-old")
        self.assertEqual(first_reader.status_code, 200)
        self.assertEqual(first_records.status_code, 200)
        self.assertIn("OLD reader marker", first_reader.text)
        self.assertNotIn("NEW reader marker", first_reader.text)
        self.assertIn("old-record", first_records.text)
        self.assertNotIn("new-record", first_records.text)
        self.assertEqual(
            [json.loads(line)["record_id"] for line in first_records.text.splitlines()],
            [
                "old-record-invalid-page",
                "old-record-page-one",
                "old-record-page-two",
            ],
        )

        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(second_download_manifest.status_code, 200)
        self.assertEqual(second_download_manifest.json()["parse_run_id"], "job-new")
        self.assertEqual(second_job["parse_run_id"], "job-new")
        self.assertEqual(second_reader.status_code, 200)
        self.assertEqual(second_records.status_code, 200)
        self.assertIn("NEW reader marker", second_reader.text)
        self.assertNotIn("OLD reader marker", second_reader.text)
        self.assertIn("new-record", second_records.text)
        self.assertNotIn("old-record", second_records.text)
        self.assertEqual(
            [json.loads(line)["record_id"] for line in second_records.text.splitlines()],
            [
                "new-record-invalid-page",
                "new-record-page-one",
                "new-record-page-two",
            ],
        )

    def test_combined_export_rebuilds_legacy_records_from_the_selected_run(self) -> None:
        from starlette.testclient import TestClient

        from parsecore.asgi import create_app
        from parsecore.tables import rows_from_table_payload

        isolated_config = CONFIG.replace(
            'object_store = "local://./var/uploads"',
            'object_store = "local://__OBJECT_STORE__"',
        )
        export_path = "/v1/parse/documents/doc-1/export-jobs"
        export_request = {
            "include": ["reader", "records"],
            "formats": {"reader": "jsonl", "records": "jsonl"},
        }
        with TemporaryWorkspace(isolated_config) as workspace, TestClient(
            create_app(workspace.config_path)
        ) as client:
            runtime = client.app.state.runtime

            def publish_run(version: str) -> str:
                published = runtime.publish_run(
                    doc_id="doc-1",
                    artifacts=RunArtifacts(
                        blocks=(
                            Block(
                                block_id=f"table-{version}",
                                doc_id="doc-1",
                                type=BlockType.TABLE,
                                content="",
                                metadata={
                                    "page": 1,
                                    "cells": [["Part Number", "Quantity"], [f"PN-{version}", "1"]],
                                },
                            ),
                        ),
                        views={"pages": [{"page_number": 1, "text": f"PN-{version}"}]},
                    ),
                    job_id=f"job-{version}",
                    source_version=f"sha256:{version}",
                    validation_status="passed",
                )
                self.assertTrue(published["published"])
                return published["parse_run_id"]

            old_run_id = publish_run("old")
            read_document = runtime.get_document

            def read_old_then_publish_new(**kwargs):
                snapshot = read_document(**kwargs)
                if snapshot["publication"]["served_run_id"] == old_run_id:
                    self.assertFalse(snapshot["document_views"].get("records"))
                    publish_run("new")
                return snapshot

            with patch.object(runtime, "get_document", side_effect=read_old_then_publish_new):
                first_export = client.post(export_path, json=export_request)
            second_export = client.post(export_path, json=export_request)

            for version, response in (("old", first_export), ("new", second_export)):
                with self.subTest(version=version):
                    self.assertEqual(response.status_code, 202, response.text)
                    job = response.json()
                    download = job["download_endpoint"]
                    manifest = client.get(download)
                    self.assertEqual(manifest.status_code, 200, manifest.text)
                    self.assertEqual(job["parse_run_id"], f"job-{version}")
                    self.assertEqual(manifest.json()["parse_run_id"], f"job-{version}")

                    datasets = {}
                    for dataset in ("reader", "records"):
                        file_response = client.get(download, params={"file": f"{dataset}.jsonl"})
                        self.assertEqual(file_response.status_code, 200, file_response.text)
                        datasets[dataset] = [json.loads(line) for line in file_response.text.splitlines()]

                    block_id = f"table-{version}"
                    expected_row = [f"PN-{version}", "1"]
                    reader = datasets["reader"]
                    self.assertEqual([block["source_block_ids"] for block in reader], [[block_id]])
                    self.assertEqual(
                        rows_from_table_payload(reader[0]["table"]),
                        [["Part Number", "Quantity"], expected_row],
                    )
                    self.assertEqual(
                        [(record["block_id"], record["raw_cells"]) for record in datasets["records"]],
                        [(block_id, expected_row)],
                    )


if __name__ == "__main__":
    unittest.main()
