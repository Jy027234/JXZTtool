"""P0-03 first slice evidence: capability and health surfaces report publication.

The capability contract says a backend must not claim it can publish atomically
unless it can. This module checks that the claim is wired through to the
surfaces an operator actually looks at: ``/health``, ``/v1/runtime`` and
``runtime.capability_report()``.
"""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from parsecore.api_health import clear_probe_cache, health_capabilities
from parsecore.bootstrap import build_runtime
from parsecore.models import Block, BlockType, Chunk
from tests.support import TemporaryWorkspace

CONFIG = """
[project]
name = "test-run-publication-surface"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"

[storage]
database_url = "__DB_URL__"

[index]
mode = "hybrid"

[[parsers]]
name = "docx-native"
extensions = [".docx"]
""".strip()


class IndexRunIsolationTests(unittest.TestCase):
    """A durable index a profile requires must be one that can be rolled back."""

    def test_the_index_adapters_declare_whether_they_isolate_runs(self) -> None:
        from parsecore.stores import PgVectorIndex
        from parsecore.stubs import NullIndex

        # Neither keys its rows by run yet, so neither may claim run isolation --
        # the pgvector one replaces a document's rows on every upsert.
        self.assertFalse(PgVectorIndex.supports_run_isolation)
        self.assertFalse(NullIndex.supports_run_isolation)

    def test_durable_index_follows_the_index_declaration_not_its_class(self) -> None:
        from parsecore.run_store import RunArtifacts
        from parsecore.stubs import NullIndex

        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            self.assertFalse(runtime._index_supports_run_isolation())

            # A `rag` profile needs a durable index; with the default index the
            # publish is refused instead of publishing an index tier that cannot be
            # returned to what the run produced.
            refusal = runtime.publish_run(
                doc_id="doc-rag",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="b1",
                            doc_id="doc-rag",
                            type=BlockType.PARAGRAPH,
                            content="text",
                            metadata={"page": 1},
                        ),
                    ),
                    chunks=(
                        Chunk(chunk_id="c1", doc_id="doc-rag", block_ids=("b1",), text="text"),
                    ),
                    views={"pages": [{"page_number": 1, "text": "text"}]},
                ),
                job_id="job-rag",
                profile="rag",
                validation_status="passed",
            )
            class _IsolatingIndex(NullIndex):
                supports_run_isolation = True

            runtime.index = _IsolatingIndex()
            self.assertTrue(runtime._index_supports_run_isolation())
            with_isolating = runtime.publish_run(
                doc_id="doc-rag",
                artifacts=RunArtifacts(
                    blocks=(
                        Block(
                            block_id="b1",
                            doc_id="doc-rag",
                            type=BlockType.PARAGRAPH,
                            content="text",
                            metadata={"page": 1},
                        ),
                    ),
                    chunks=(
                        Chunk(chunk_id="c1", doc_id="doc-rag", block_ids=("b1",), text="text"),
                    ),
                    views={"pages": [{"page_number": 1, "text": "text"}]},
                ),
                job_id="job-rag-2",
                profile="rag",
                validation_status="passed",
            )

        self.assertFalse(refusal["published"])
        self.assertEqual(refusal["reason"], "run_not_publishable_by_policy")
        self.assertIn("index_backend_not_durable", refusal["message"])
        # The declaration is what decides, so an index that can isolate runs is
        # accepted without the runtime knowing its class.
        self.assertTrue(with_isolating["published"], with_isolating.get("message"))


class PublicationCapabilitySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_probe_cache()

    def tearDown(self) -> None:
        clear_probe_cache()

    def test_capability_report_exposes_the_store_contract(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        entry = next(
            item for item in report["capabilities"] if item["name"] == "run_publication"
        )
        self.assertEqual(entry["status"], "available")
        self.assertEqual(entry["requirement"], "optional")
        self.assertEqual(entry["detail"]["schema_version"], "2026-09-run-publication-contract-v1")
        self.assertTrue(entry["detail"]["strict_publication_ready"])
        self.assertEqual(entry["detail"]["artifact_profile"], "default")
        self.assertIn("compare_and_set_active_pointer", entry["detail"]["supported"])

    def test_health_reports_publication_without_blocking_delivery(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            health = health_capabilities(runtime)

        self.assertTrue(health["accepting_jobs"])
        self.assertTrue(health["formal_parse_ready"])
        # Publication availability must not be confused with a degraded provider.
        self.assertNotIn(
            "run_publication", [item["name"] for item in health["unavailable_capabilities"]]
        )

    def test_runtime_description_includes_the_publication_capability(self) -> None:
        with TemporaryWorkspace(CONFIG) as workspace:
            runtime = build_runtime(workspace.config_path)
            description = runtime.describe()

        names = [item["name"] for item in description["capabilities"]["capabilities"]]
        self.assertIn("run_publication", names)

    def test_health_endpoint_exposes_the_capability(self) -> None:
        from parsecore.asgi import create_app

        with TemporaryWorkspace(CONFIG) as workspace:
            app = create_app(workspace.config_path)
            with TestClient(app) as client:
                body = client.get("/health").json()

        entry = next(
            item
            for item in body["capabilities"]["report"]["capabilities"]
            if item["name"] == "run_publication"
        )
        self.assertEqual(entry["status"], "available")

    def test_run_publication_can_be_disabled_by_configuration(self) -> None:
        disabled = CONFIG.replace(
            'execution_mode = "inline"',
            'execution_mode = "inline"\nrun_publication_enabled = false',
        )
        with TemporaryWorkspace(disabled) as workspace:
            runtime = build_runtime(workspace.config_path)
            report = runtime.capability_report()

        self.assertFalse(runtime.run_publication_ready)
        entry = next(
            item for item in report["capabilities"] if item["name"] == "run_publication"
        )
        self.assertEqual(entry["status"], "disabled")
        self.assertEqual(entry["requirement"], "disabled")


if __name__ == "__main__":
    unittest.main()
