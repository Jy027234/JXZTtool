"""P0-02: frozen run, validation and publication contract.

These tests are the executable part of
``docs/parsecore-reliability-mineru-todo-20260920.md`` P0-02.  They lock the
vocabulary and the state machines, and they encode the two claims the whole
contract exists for:

* a completed execution is **not** a validated result;
* a validated result is **not** published until an explicit transition.
"""

from __future__ import annotations

import unittest

from parsecore.models import ParseJobState
from parsecore.publication import (
    ARTIFACT_PROFILES,
    CONCURRENT_PUBLICATION_POLICY,
    CONTRACT_STATUS,
    CURRENT_STORE_CAPABILITIES,
    DEFAULT_VALIDATION_POLICY,
    EXECUTION_TRANSITIONS,
    LEGACY_JOB_STATE_TO_EXECUTION,
    LEGACY_STATES_THAT_ARE_NOT_VALIDATION,
    PARSE_RUN_ID_ALIAS,
    PUBLICATION_TRANSITIONS,
    RUN_STORE_OPERATIONS,
    SCHEMA_VERSION,
    STRICT_PUBLICATION_REQUIRED_CAPABILITIES,
    VALIDATION_TRANSITIONS,
    ArtifactRequirement,
    ExecutionStatus,
    PublicationStatus,
    RunStoreCapabilities,
    RunStoreCapability,
    TransitionError,
    ValidationPolicy,
    ValidationStatus,
    artifact_profile,
    assert_transition,
    can_transition,
    contract_payload,
    execution_status_from_job_state,
    legal_transitions,
    legacy_execution_status,
    run_store_capabilities,
)


class StateMachineTests(unittest.TestCase):
    def test_execution_machine_is_terminal_after_success_or_failure(self) -> None:
        self.assertEqual(
            EXECUTION_TRANSITIONS[ExecutionStatus.SUCCEEDED], frozenset()
        )
        self.assertEqual(EXECUTION_TRANSITIONS[ExecutionStatus.FAILED], frozenset())
        self.assertEqual(EXECUTION_TRANSITIONS[ExecutionStatus.CANCELLED], frozenset())

    def test_execution_requires_running_before_a_conclusion(self) -> None:
        self.assertTrue(
            can_transition("execution_status", ExecutionStatus.PENDING, ExecutionStatus.RUNNING)
        )
        self.assertFalse(
            can_transition("execution_status", ExecutionStatus.PENDING, ExecutionStatus.SUCCEEDED)
        )
        self.assertTrue(
            can_transition("execution_status", ExecutionStatus.RUNNING, ExecutionStatus.SUCCEEDED)
        )

    def test_execution_cannot_leave_a_terminal_state(self) -> None:
        for terminal in (
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        ):
            for target in ExecutionStatus:
                with self.subTest(terminal=terminal.value, target=target.value):
                    # Re-applying the same state is idempotent (retries rely on
                    # it); every other target is illegal from a terminal state.
                    self.assertEqual(
                        can_transition("execution_status", terminal, target),
                        target is terminal,
                    )

    def test_validation_never_defaults_to_passed(self) -> None:
        # not_run may move anywhere, but nothing may move *to* not_run by accident
        # except error recovery, and passed is never a starting point.
        self.assertNotIn(ValidationStatus.NOT_RUN, VALIDATION_TRANSITIONS[ValidationStatus.PASSED])
        self.assertEqual(
            sorted(state.value for state in VALIDATION_TRANSITIONS[ValidationStatus.NOT_RUN]),
            ["error", "needs_review", "passed", "rejected", "warning"],
        )

    def test_publication_requires_unpublished_start(self) -> None:
        self.assertEqual(
            sorted(
                state.value
                for state in PUBLICATION_TRANSITIONS[PublicationStatus.UNPUBLISHED]
            ),
            ["published", "withdrawn"],
        )
        self.assertEqual(
            sorted(
                state.value
                for state in PUBLICATION_TRANSITIONS[PublicationStatus.PUBLISHED]
            ),
            ["superseded", "withdrawn"],
        )
        self.assertEqual(PUBLICATION_TRANSITIONS[PublicationStatus.WITHDRAWN], frozenset())

    def test_transitions_are_idempotent_but_wrong_ones_raise(self) -> None:
        self.assertTrue(
            can_transition("publication_status", PublicationStatus.PUBLISHED, PublicationStatus.PUBLISHED)
        )
        assert_transition(
            "publication_status", PublicationStatus.PUBLISHED, PublicationStatus.SUPERSEDED
        )
        with self.assertRaises(TransitionError) as captured:
            assert_transition(
                "publication_status",
                PublicationStatus.WITHDRAWN,
                PublicationStatus.PUBLISHED,
                reason="withdrawn is terminal",
            )
        self.assertEqual(captured.exception.machine, "publication_status")
        self.assertIn("withdrawn is terminal", str(captured.exception))

    def test_unknown_states_and_machines_are_rejected(self) -> None:
        self.assertFalse(can_transition("execution_status", "teleported", "running"))
        with self.assertRaises(KeyError):
            legal_transitions("no_such_machine")

    def test_legal_transitions_is_serialisable(self) -> None:
        table = legal_transitions("validation_status")
        self.assertEqual(sorted(table), sorted(state.value for state in ValidationStatus))
        self.assertIsInstance(table["passed"], list)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_every_legacy_job_state_is_mapped(self) -> None:
        self.assertEqual(
            sorted(LEGACY_JOB_STATE_TO_EXECUTION),
            sorted(state.value for state in ParseJobState),
        )
        self.assertEqual(
            LEGACY_STATES_THAT_ARE_NOT_VALIDATION,
            frozenset(state.value for state in ParseJobState),
        )

    def test_done_maps_to_succeeded_not_to_validation_or_publication(self) -> None:
        self.assertIs(
            legacy_execution_status(ParseJobState.DONE), ExecutionStatus.SUCCEEDED
        )
        projection = execution_status_from_job_state(ParseJobState.DONE)
        self.assertEqual(projection["execution_status"], "succeeded")
        self.assertEqual(projection["validation_status"], "not_run")
        self.assertEqual(projection["validation_status_source"], "not_derived_from_job_state")
        self.assertEqual(projection["publication_status"], "unpublished")

    def test_partial_is_execution_success_but_never_publishable(self) -> None:
        self.assertIs(
            legacy_execution_status(ParseJobState.PARTIAL), ExecutionStatus.SUCCEEDED
        )
        allowed, reason = DEFAULT_VALIDATION_POLICY.may_publish(
            validation_status=ValidationStatus.NOT_RUN, profile="default"
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "validation_not_run")

    def test_failed_and_pending_states_map_predictably(self) -> None:
        mapping = {
            "pending": ExecutionStatus.PENDING,
            "parsing": ExecutionStatus.RUNNING,
            "structuring": ExecutionStatus.RUNNING,
            "embedding": ExecutionStatus.RUNNING,
            "failed": ExecutionStatus.FAILED,
        }
        for legacy, expected in mapping.items():
            with self.subTest(legacy=legacy):
                self.assertIs(legacy_execution_status(legacy), expected)

    def test_unknown_legacy_state_raises_instead_of_guessing(self) -> None:
        with self.assertRaises(KeyError):
            legacy_execution_status("probably_fine")

    def test_parse_run_id_alias_is_documented_and_pending_switch(self) -> None:
        contract = contract_payload()
        notes = contract["legacy_notes"]
        self.assertEqual(PARSE_RUN_ID_ALIAS, "job_id")
        self.assertEqual(notes["parse_run_id_alias"], "job_id")
        self.assertEqual(
            notes["parse_run_id_alias_switch_contract_version"], SCHEMA_VERSION
        )
        self.assertTrue(notes["done_is_not_published"])
        self.assertTrue(notes["done_is_not_validated"])


class ArtifactProfileTests(unittest.TestCase):
    def test_default_profile_keeps_rag_optional(self) -> None:
        profile = artifact_profile("default")
        self.assertEqual(
            profile.requirement_for("reader"), ArtifactRequirement.REQUIRED
        )
        # A deployment without embedding must not be rejected.
        self.assertEqual(profile.requirement_for("rag"), ArtifactRequirement.OPTIONAL)
        self.assertEqual(profile.required_artifacts(), ("reader",))

    def test_rag_profile_requires_rag_and_records_profile_disables_it(self) -> None:
        self.assertEqual(
            set(artifact_profile("rag").required_artifacts()), {"reader", "rag"}
        )
        records = artifact_profile("records")
        self.assertEqual(records.requirement_for("rag"), ArtifactRequirement.DISABLED)
        self.assertIn("rag", records.disabled_artifacts())
        self.assertIn("export", records.required_artifacts())

    def test_unknown_artifact_defaults_to_optional_and_profile_lookup_is_strict(self) -> None:
        self.assertEqual(
            artifact_profile("default").requirement_for("telemetry"),
            ArtifactRequirement.OPTIONAL,
        )
        with self.assertRaises(KeyError):
            artifact_profile("no-such-profile")

    def test_profile_payload_is_serialisable(self) -> None:
        payload = ARTIFACT_PROFILES["records"].as_payload()
        self.assertEqual(payload["name"], "records")
        self.assertEqual(payload["requirements"]["rag"], "disabled")
        self.assertIn("export", payload["required_artifacts"])


class ValidationPolicyTests(unittest.TestCase):
    def test_not_run_rejected_error_and_needs_review_block_publication(self) -> None:
        for status in (
            ValidationStatus.NOT_RUN,
            ValidationStatus.NEEDS_REVIEW,
            ValidationStatus.REJECTED,
            ValidationStatus.ERROR,
        ):
            with self.subTest(status=status.value):
                allowed, reason = DEFAULT_VALIDATION_POLICY.may_publish(
                    validation_status=status
                )
                self.assertFalse(allowed)
                self.assertTrue(reason)

    def test_passed_publishes_and_warning_does_not_by_default(self) -> None:
        allowed, reason = DEFAULT_VALIDATION_POLICY.may_publish(
            validation_status=ValidationStatus.PASSED
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "policy_allows_passed")

        allowed, reason = DEFAULT_VALIDATION_POLICY.may_publish(
            validation_status=ValidationStatus.WARNING, warning_codes=("low_coverage",)
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "policy_blocks_warning")

    def test_warning_allowlist_is_explicit_and_versioned(self) -> None:
        policy = ValidationPolicy(
            version="2026-09-validation-policy-v2",
            publish_on_warning=True,
            warning_allowlist=("header_footer_noise",),
        )
        allowed, reason = policy.may_publish(
            validation_status=ValidationStatus.WARNING,
            warning_codes=("header_footer_noise",),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "warning_allowlisted")

        allowed, reason = policy.may_publish(
            validation_status=ValidationStatus.WARNING,
            warning_codes=("missing_critical_field",),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "warning_not_allowlisted")
        self.assertEqual(policy.as_payload()["version"], "2026-09-validation-policy-v2")

    def test_rag_profile_requires_a_durable_index(self) -> None:
        allowed, reason = DEFAULT_VALIDATION_POLICY.may_publish(
            validation_status=ValidationStatus.PASSED,
            profile="rag",
            durable_index=False,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "index_backend_not_durable")

        # The default profile is still publishable without a durable index.
        allowed, _ = DEFAULT_VALIDATION_POLICY.may_publish(
            validation_status=ValidationStatus.PASSED,
            profile="default",
            durable_index=False,
        )
        self.assertTrue(allowed)

    def test_unknown_validation_status_raises(self) -> None:
        with self.assertRaises(KeyError):
            DEFAULT_VALIDATION_POLICY.may_publish(validation_status="looks_fine")


class StoreCapabilityTests(unittest.TestCase):
    def test_no_shipped_backend_is_strict_publication_ready_yet(self) -> None:
        # P0-02 records the gap; P0-03 closes it. Until then a store must not be
        # able to claim it can publish atomically.
        for backend in ("sqlite", "postgres", "memory"):
            with self.subTest(backend=backend):
                capabilities = run_store_capabilities(backend)
                self.assertFalse(capabilities.strict_publication_ready)
                self.assertEqual(
                    capabilities.missing(STRICT_PUBLICATION_REQUIRED_CAPABILITIES),
                    STRICT_PUBLICATION_REQUIRED_CAPABILITIES,
                )

    def test_capability_lookup_normalizes_database_urls(self) -> None:
        self.assertFalse(run_store_capabilities("sqlite:///./var/x.db").strict_publication_ready)
        self.assertFalse(run_store_capabilities("postgresql://h/db").strict_publication_ready)
        self.assertFalse(run_store_capabilities("").strict_publication_ready)
        self.assertFalse(run_store_capabilities("mysql://h/db").strict_publication_ready)

    def test_capability_payload_reports_the_missing_set(self) -> None:
        payload = RunStoreCapabilities(
            supported=(RunStoreCapability.RUN_ISOLATED_ARTIFACTS.value,)
        ).as_payload()
        self.assertFalse(payload["strict_publication_ready"])
        self.assertIn("atomic_active_pointer", payload["missing_for_strict_publication"])
        self.assertNotIn("run_isolated_artifacts", payload["missing_for_strict_publication"])

    def test_latest_job_is_not_a_publication_capability(self) -> None:
        # Reading the newest job stays diagnostic and must never gate publication.
        self.assertNotIn(
            "latest_job", [item.value for item in RunStoreCapability]
        )
        self.assertIn(
            "atomic_active_pointer",
            [item.value for item in RunStoreCapability],
        )

    def test_store_operations_cover_the_documented_surface(self) -> None:
        for operation in (
            "begin_run",
            "save_run_artifacts",
            "seal_run",
            "get_active_run",
            "publish_run",
        ):
            with self.subTest(operation=operation):
                self.assertIn(operation, RUN_STORE_OPERATIONS)
        self.assertNotIn("latest_job", RUN_STORE_OPERATIONS)


class ConcurrentPublicationPolicyTests(unittest.TestCase):
    def test_last_writer_wins_is_explicitly_disabled(self) -> None:
        self.assertEqual(CONCURRENT_PUBLICATION_POLICY["policy"], "fenced_expected_generation")
        self.assertEqual(CONCURRENT_PUBLICATION_POLICY["last_writer_wins"], "false")
        self.assertEqual(CONCURRENT_PUBLICATION_POLICY["newer_generation_wins"], "true")
        self.assertEqual(
            CONCURRENT_PUBLICATION_POLICY["latest_job_used_for_publication"], "false"
        )
        self.assertEqual(
            CONCURRENT_PUBLICATION_POLICY["stale_run_outcome"],
            PublicationStatus.UNPUBLISHED.value,
        )


class ContractPayloadTests(unittest.TestCase):
    def test_contract_is_frozen_and_self_consistent(self) -> None:
        contract = contract_payload()
        self.assertEqual(contract["schema_version"], SCHEMA_VERSION)
        self.assertEqual(contract["status"], CONTRACT_STATUS)
        self.assertEqual(contract["status"], "frozen")

        # The documented transitions must come from the code, not a copy.
        self.assertEqual(
            contract["transitions"]["execution_status"],
            legal_transitions("execution_status"),
        )
        self.assertEqual(
            contract["transitions"]["publication_status"],
            legal_transitions("publication_status"),
        )
        self.assertEqual(
            contract["statuses"]["publication_status"],
            [state.value for state in PublicationStatus],
        )

    def test_contract_records_consumer_migration(self) -> None:
        migration = contract_payload()["consumer_migration"]
        self.assertEqual(migration["new_formal_signal"], "publication_status == 'published'")
        self.assertEqual(
            migration["legacy_formal_signal_status"], "deprecated_and_not_sufficient"
        )
        self.assertEqual(migration["formal_read_without_publication"], "not_ready_response")
        self.assertEqual(migration["partial_results"], "explicit_preview_only")


if __name__ == "__main__":
    unittest.main()
