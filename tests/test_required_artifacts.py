"""P0-04: required artifacts gate delivery, optional ones only degrade it.

The reproduced problem was that ``_persist_document_views()`` failures were
logged and nothing else: a job still finished ``done`` with ``pages = 0``.  This
module proves the failure now has a consequence that matches the artifact
profile.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from parsecore.bootstrap import build_runtime
from parsecore.models import ParseRequest
from tests.support import TemporaryWorkspace

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

CONFIG = """
[project]
name = "test-required-artifacts"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
__RESOURCE_EXTRA__

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[quality_gate]
min_reading_order_confidence = 0.75

[product]
adapter = "embedded"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


def _config(*, resource_extra: str = "", profile: str | None = None) -> str:
    lines = [resource_extra] if resource_extra else []
    if profile:
        lines.append(f'run_artifact_profile = "{profile}"')
    return CONFIG.replace("__RESOURCE_EXTRA__", "\n".join(lines))


class RequiredViewDeliveryTests(unittest.TestCase):
    def _submit(self, workspace: TemporaryWorkspace, runtime):
        document_path = workspace.create_docx(
            "spec.docx", ["Maintenance Manual", "Apply torque"]
        )
        return runtime.submit(
            ParseRequest(
                doc_id="doc-views",
                file_path=str(document_path),
                media_type=DOCX_MEDIA_TYPE,
            )
        )

    def test_missing_required_view_artifact_blocks_delivery(self) -> None:
        # ``default`` requires a reader-visible page view.  The view rows are now
        # produced from the same snapshot that is staged into the run, so the
        # failure surfaces at seal time and the job must fail rather than deliver.
        with TemporaryWorkspace(_config(resource_extra="max_attempts = 1")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.object(
                runtime,
                "_document_views_for_snapshot",
                return_value={"pages": []},
            ):
                with self.assertRaises(RuntimeError) as captured:
                    self._submit(workspace, runtime)
            failed_job = runtime.job_store.get_latest_job(doc_id="doc-views")
            state = runtime.publication_state(doc_id="doc-views")

        self.assertIn("reader", str(captured.exception))
        self.assertIsNotNone(failed_job)
        self.assertEqual(failed_job.state.value, "failed")
        self.assertIsNone(state["active_run_id"])
        self.assertEqual(state["consumer_signal"], "not_ready")

    def test_required_view_failure_takes_the_normal_failure_path(self) -> None:
        # The check happens before DONE, so the job follows the ordinary failure
        # path and the caller sees the real error instead of a stale-claim error
        # produced by trying to fail a job that was already marked done.
        with TemporaryWorkspace(_config(resource_extra="max_attempts = 2")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.object(
                runtime, "_document_views_for_snapshot", return_value={"pages": []}
            ):
                with self.assertRaises(RuntimeError) as captured:
                    self._submit(workspace, runtime)
            job = runtime.job_store.get_latest_job(doc_id="doc-views")

        self.assertIn("reader", str(captured.exception))
        self.assertIsNotNone(job)
        # Inline execution does not reschedule: the job is failed with the real
        # reason and is not dead-lettered.
        self.assertEqual(job.state.value, "failed")
        self.assertIsNone(job.dead_lettered_at)

    def test_optional_artifact_failure_only_degrades(self) -> None:
        # The point is required versus optional, and the artifact catalog is how a
        # deployment declares which is which.  With the reader declared optional an
        # empty page view degrades the delivery; the previous test shows that with
        # the default profile the same failure blocks delivery.
        from parsecore.publication import (
            ARTIFACT_PROFILES,
            ArtifactProfile,
            ArtifactRequirement,
        )

        optional_reader = ArtifactProfile(
            name="optional-reader",
            requirements={
                "blocks": ArtifactRequirement.REQUIRED,
                "reader": ArtifactRequirement.OPTIONAL,
            },
        )
        with TemporaryWorkspace(_config(profile="optional-reader")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.dict(
                ARTIFACT_PROFILES, {"optional-reader": optional_reader}, clear=False
            ), patch.object(
                runtime, "_document_views_for_snapshot", return_value={"pages": []}
            ):
                outcome = self._submit(workspace, runtime)
            state = runtime.publication_state(doc_id="doc-views")

        self.assertEqual(outcome.job.state.value, "done")
        self.assertTrue(outcome.blocks)
        # The run published with an explicit degradation rather than failing.
        self.assertTrue(state["formal_version_available"])

    def test_successful_persist_is_unaffected(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            outcome = self._submit(workspace, runtime)
            views = runtime.job_store.get_document_views(
                doc_id="doc-views", tenant_id="default"
            )

        # The published run's views are copied into the live tier by publish.
        self.assertEqual(outcome.job.state.value, "done")
        self.assertTrue(views["pages"])

    def test_artifact_requirement_follows_the_configured_profile(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            self.assertEqual(runtime._artifact_requirement("reader"), "required")
            self.assertEqual(runtime._artifact_requirement("rag"), "optional")
            self.assertEqual(runtime._artifact_requirement("records"), "optional")

        with TemporaryWorkspace(_config(profile="records")) as workspace:
            runtime = build_runtime(workspace.config_path)
            self.assertEqual(runtime._artifact_requirement("records"), "required")
            self.assertEqual(runtime._artifact_requirement("rag"), "disabled")


class ValidationWiringTests(unittest.TestCase):
    """The runtime's publish path must use the shared rules by default."""

    def _artifacts(self):
        from parsecore.models import Block, BlockType
        from parsecore.run_store import RunArtifacts

        return RunArtifacts(
            blocks=(
                Block(
                    block_id="b1",
                    doc_id="doc-1",
                    type=BlockType.PARAGRAPH,
                    content="Apply the specified torque to the fastener.",
                    metadata={"page": 1},
                ),
            ),
            views={"pages": [{"page_number": 1, "text": "Apply torque"}]},
        )

    def test_publish_runs_the_shared_rules_and_records_the_report(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            result = runtime.publish_run(
                doc_id="doc-1", artifacts=self._artifacts(), job_id="job-1"
            )

        self.assertTrue(result["published"])
        self.assertEqual(result["reason"], "policy_allows_passed")
        self.assertIn("validation_report", result)
        self.assertEqual(result["validation_report"]["status"], "passed")
        self.assertEqual(
            result["validation_report"]["rule_version"],
            "2026-09-publication-validation-rules-v1",
        )

    def test_placeholder_result_is_blocked_by_the_rules(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=self._artifacts(),
                job_id="job-1",
                placeholder_parsers=("mineru-local",),
            )

        self.assertFalse(result["published"])
        self.assertEqual(result["reason"], "run_publish_failed")
        self.assertIn("validation_rejected", result["message"])
        self.assertEqual(result["validation_report"]["status"], "rejected")
        self.assertIn(
            "placeholder_result", result["validation_report"]["rejection_codes"]
        )

    def test_error_severity_signal_rejects_publication(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=self._artifacts(),
                job_id="job-1",
                quality_signals=(
                    {
                        "code": "source_reference_broken",
                        "severity": "error",
                        "message": "block b9 is referenced but not present",
                    },
                ),
            )

        self.assertFalse(result["published"])
        self.assertIn(
            "quality_signal:source_reference_broken",
            result["validation_report"]["rejection_codes"],
        )

    def test_thresholds_come_from_the_quality_gate_config(self) -> None:
        strict = _config().replace(
            "min_reading_order_confidence = 0.75",
            "min_reading_order_confidence = 0.99",
        )
        with TemporaryWorkspace(strict) as workspace:
            runtime = build_runtime(workspace.config_path)
            options = runtime._validation_options()

        self.assertAlmostEqual(options["min_reading_order_confidence"], 0.99, places=4)
        self.assertAlmostEqual(options["min_text_page_coverage"], 0.98, places=4)

    def test_explicit_status_still_works_for_hosts_and_tests(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            result = runtime.publish_run(
                doc_id="doc-1",
                artifacts=self._artifacts(),
                job_id="job-1",
                validation_status="needs_review",
            )

        self.assertFalse(result["published"])
        self.assertEqual(result["validation_status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
