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

    def test_required_reader_view_failure_fails_the_run(self) -> None:
        # ``default`` requires the reader-visible page view, so a write failure
        # must not be reported as a successful parse.
        with TemporaryWorkspace(_config(resource_extra="max_attempts = 1")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.object(
                runtime.job_store,
                "save_document_views",
                side_effect=RuntimeError("injected view write failure"),
            ):
                with self.assertRaises(RuntimeError) as captured:
                    self._submit(workspace, runtime)
            failed_job = runtime.job_store.get_latest_job(doc_id="doc-views")

        self.assertIn("required_artifact_not_persisted:reader", str(captured.exception))
        self.assertIsNotNone(failed_job)
        self.assertEqual(failed_job.state.value, "failed")
        self.assertIn("required_artifact_not_persisted:reader", failed_job.failure_reason)

    def test_required_view_failure_takes_the_normal_failure_path(self) -> None:
        # The check happens before DONE, so the job follows the ordinary failure
        # path and the caller sees the real error instead of a stale-claim error
        # produced by trying to fail a job that was already marked done.
        with TemporaryWorkspace(_config(resource_extra="max_attempts = 2")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.object(
                runtime.job_store,
                "save_document_views",
                side_effect=RuntimeError("injected view write failure"),
            ):
                with self.assertRaises(RuntimeError) as captured:
                    self._submit(workspace, runtime)
            job = runtime.job_store.get_latest_job(doc_id="doc-views")

        self.assertIn("required_artifact_not_persisted:reader", str(captured.exception))
        self.assertIsNotNone(job)
        # Inline execution does not reschedule: the job is failed with the real
        # reason and is not dead-lettered.
        self.assertEqual(job.state.value, "failed")
        self.assertIn("required_artifact_not_persisted:reader", job.failure_reason)
        self.assertIsNone(job.dead_lettered_at)

    def test_optional_reader_view_failure_only_degrades(self) -> None:
        # ``assets-only`` disables the reader artifact, so the same write failure
        # is a degradation rather than a failed delivery.
        with TemporaryWorkspace(_config(profile="assets-only")) as workspace:
            runtime = build_runtime(workspace.config_path)
            with patch.object(
                runtime.job_store,
                "save_document_views",
                side_effect=RuntimeError("injected view write failure"),
            ):
                outcome = self._submit(workspace, runtime)

        self.assertEqual(outcome.job.state.value, "done")
        self.assertTrue(outcome.blocks)

    def test_successful_persist_is_unaffected(self) -> None:
        with TemporaryWorkspace(_config()) as workspace:
            runtime = build_runtime(workspace.config_path)
            outcome = self._submit(workspace, runtime)
            views = runtime.job_store.get_document_views(
                doc_id="doc-views", tenant_id="default"
            )

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
