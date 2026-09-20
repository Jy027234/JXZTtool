"""Frozen run, validation and publication contract (P0-02).

This module is the **design artefact** for
``docs/parsecore-reliability-mineru-todo-20260920.md`` P0-02 and the executable
part of it: the vocabulary, the legal state machines, the legacy ``job.state``
mapping and the storage capability names that P0-03 implements.

Scope boundary
--------------
P0-02 freezes vocabulary and interfaces.  It does **not** change completion or
publication behaviour: no runtime path reads these states yet, no store
implements the run protocol yet, and nothing in this module is wired into
``ParseRuntime``.  P0-03 implements staged storage and the atomic active-pointer
switch; P0-04 implements validation and the publish gate.

Why a second identity next to ``job_id``
----------------------------------------
``job_id`` identifies a scheduling request.  A retry of the same request must not
overwrite the previous attempt's partial artifacts, so a retry gets a **new**
``parse_run_id``.  ``job_id`` stays as an execution-phase compatibility field.

The publication rule
--------------------
``publication_status`` starts at ``unpublished`` for **every** run, including a
run whose parse succeeded.  Only an explicit, validated transition makes a run
``published``.  Finishing a parse is therefore no longer a publication signal:
consumers must not treat ``job.state = done`` (or ``ProductAdapter.after_parse``)
as permission to ingest results.  That single change is what makes the three
reproduced delivery failures impossible to mistake for success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .models import ParseJobState


SCHEMA_VERSION = "2026-09-run-publication-contract-v1"
"""Version of the frozen contract itself, exposed in payloads and samples."""

CONTRACT_STATUS = "frozen"
"""``frozen`` while the contract is closed for breaking changes.

P0-02 is the freeze point: a later work package that needs a new field must add
an optional field or a new contract version, never repurpose an existing one.
"""


class ExecutionStatus(str, Enum):
    """Whether the execution attempt is still running.

    Describes *execution only*.  A ``SUCCEEDED`` run is not necessarily valid and
    is never automatically published.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ValidationStatus(str, Enum):
    """Whether the produced artifacts are trustworthy.

    ``NOT_RUN`` is the default for a run that has artifacts but no recorded
    validation conclusion.  Unknown or failed validation must never collapse to
    :attr:`PASSED`.
    """

    NOT_RUN = "not_run"
    PASSED = "passed"
    WARNING = "warning"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    ERROR = "error"


class PublicationStatus(str, Enum):
    """Whether downstream consumers may read this run as the formal version."""

    UNPUBLISHED = "unpublished"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


class ArtifactRequirement(str, Enum):
    """Per-profile delivery requirement for one artifact family.

    ``REQUIRED`` failure blocks publication; ``OPTIONAL`` failure is reported as a
    degradation; ``DISABLED`` is a deliberate profile choice and is not a fault.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"


class RunEvent(str, Enum):
    """Observable transitions used by the event log and by migration tooling."""

    RUN_BEGUN = "run_begun"
    ARTIFACTS_SAVED = "artifacts_saved"
    RUN_SEALED = "run_sealed"
    VALIDATION_RECORDED = "validation_recorded"
    RUN_PUBLISHED = "run_published"
    RUN_SUPERSEDED = "run_superseded"
    RUN_WITHDRAWN = "run_withdrawn"
    PUBLICATION_REJECTED_STALE = "publication_rejected_stale"


# ---------------------------------------------------------------------------
# Artifact profiles
# ---------------------------------------------------------------------------

class RequiredArtifact(str, Enum):
    """Artifact families a profile may declare as required.

    A family is satisfied by the staged artifacts that carry it:

    * ``blocks`` / ``chunks`` — the staged block and chunk rows;
    * ``reader`` — the ``pages`` document view the reader projection reads;
    * ``records`` — the ``records`` document view;
    * ``rag`` — chunks that reached the index tier;
    * ``assets`` / ``export`` — asset and export packages (no staging surface
      yet, so a profile requiring them cannot seal until P0-03 stages them).
    """

    READER = "reader"
    RECORDS = "records"
    RAG = "rag"
    ASSETS = "assets"
    EXPORT = "export"
    BLOCKS = "blocks"
    CHUNKS = "chunks"
    PAGES = "pages"
    LINES = "lines"
    RECORD_FTS = "record_fts"


@dataclass(frozen=True, slots=True)
class ArtifactProfile:
    """Required/optional/disabled artifact declaration for one parse profile."""

    name: str
    requirements: Mapping[str, ArtifactRequirement] = field(default_factory=dict)

    def requirement_for(self, artifact: str) -> ArtifactRequirement:
        normalized = str(artifact or "").strip().lower()
        value = self.requirements.get(normalized)
        if value is None:
            return ArtifactRequirement.OPTIONAL
        return value if isinstance(value, ArtifactRequirement) else ArtifactRequirement(str(value))

    def required_artifacts(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name, value in self.requirements.items()
                if _as_requirement(value) is ArtifactRequirement.REQUIRED
            )
        )

    def disabled_artifacts(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name, value in self.requirements.items()
                if _as_requirement(value) is ArtifactRequirement.DISABLED
            )
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requirements": {
                str(name): _as_requirement(value).value
                for name, value in sorted(self.requirements.items())
            },
            "required_artifacts": list(self.required_artifacts()),
            "disabled_artifacts": list(self.disabled_artifacts()),
        }


def _as_requirement(value: Any) -> ArtifactRequirement:
    if isinstance(value, ArtifactRequirement):
        return value
    return ArtifactRequirement(str(value).strip().lower())


#: The starter profiles.  ``default`` keeps the historical mainline behaviour:
#: vectorisation is opt-in, so RAG is optional rather than required and a
#: deployment without embedding is not rejected.
ARTIFACT_PROFILES: Mapping[str, ArtifactProfile] = {
    "default": ArtifactProfile(
        name="default",
        requirements={
            RequiredArtifact.BLOCKS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.READER.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.RECORDS.value: ArtifactRequirement.OPTIONAL,
            RequiredArtifact.RAG.value: ArtifactRequirement.OPTIONAL,
            RequiredArtifact.ASSETS.value: ArtifactRequirement.OPTIONAL,
            RequiredArtifact.EXPORT.value: ArtifactRequirement.OPTIONAL,
        },
    ),
    "rag": ArtifactProfile(
        name="rag",
        requirements={
            RequiredArtifact.BLOCKS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.READER.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.RAG.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.RECORDS.value: ArtifactRequirement.OPTIONAL,
            RequiredArtifact.ASSETS.value: ArtifactRequirement.OPTIONAL,
            RequiredArtifact.EXPORT.value: ArtifactRequirement.OPTIONAL,
        },
    ),
    "records": ArtifactProfile(
        name="records",
        requirements={
            RequiredArtifact.BLOCKS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.READER.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.RECORDS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.EXPORT.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.RAG.value: ArtifactRequirement.DISABLED,
            RequiredArtifact.ASSETS.value: ArtifactRequirement.OPTIONAL,
        },
    ),
    "assets-only": ArtifactProfile(
        name="assets-only",
        requirements={
            RequiredArtifact.BLOCKS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.ASSETS.value: ArtifactRequirement.REQUIRED,
            RequiredArtifact.READER.value: ArtifactRequirement.DISABLED,
            RequiredArtifact.RECORDS.value: ArtifactRequirement.DISABLED,
            RequiredArtifact.RAG.value: ArtifactRequirement.DISABLED,
            RequiredArtifact.EXPORT.value: ArtifactRequirement.OPTIONAL,
        },
    ),
}


def artifact_profile(name: str) -> ArtifactProfile:
    normalized = str(name or "").strip().lower() or "default"
    profile = ARTIFACT_PROFILES.get(normalized)
    if profile is None:
        raise KeyError(f"unknown artifact profile: {name!r}")
    return profile


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------

#: Legal ``execution_status`` transitions.  A terminal state never moves again;
#: recovery of a lease-expired run creates a new ``parse_run_id`` instead.
EXECUTION_TRANSITIONS: Mapping[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.PENDING: frozenset({ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED}),
    ExecutionStatus.RUNNING: frozenset(
        {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.CANCELLED: frozenset(),
}

#: Legal ``validation_status`` transitions.  ``not_run`` may be left at any time,
#: and a re-validation may move ``warning``/``needs_review`` to a terminal
#: conclusion once a human decides.  ``passed`` is only left when new evidence
#: invalidates it.
VALIDATION_TRANSITIONS: Mapping[ValidationStatus, frozenset[ValidationStatus]] = {
    ValidationStatus.NOT_RUN: frozenset(
        {
            ValidationStatus.PASSED,
            ValidationStatus.WARNING,
            ValidationStatus.NEEDS_REVIEW,
            ValidationStatus.REJECTED,
            ValidationStatus.ERROR,
        }
    ),
    ValidationStatus.PASSED: frozenset(
        {ValidationStatus.WARNING, ValidationStatus.NEEDS_REVIEW, ValidationStatus.REJECTED}
    ),
    ValidationStatus.WARNING: frozenset(
        {ValidationStatus.PASSED, ValidationStatus.NEEDS_REVIEW, ValidationStatus.REJECTED}
    ),
    ValidationStatus.NEEDS_REVIEW: frozenset(
        {ValidationStatus.PASSED, ValidationStatus.WARNING, ValidationStatus.REJECTED}
    ),
    ValidationStatus.REJECTED: frozenset({ValidationStatus.NEEDS_REVIEW, ValidationStatus.ERROR}),
    ValidationStatus.ERROR: frozenset(
        {ValidationStatus.NOT_RUN, ValidationStatus.NEEDS_REVIEW, ValidationStatus.REJECTED}
    ),
}

#: Legal ``publication_status`` transitions.  ``superseded`` is produced by
#: publishing a newer generation; ``withdrawn`` is a deliberate operator action
#: and keeps the artifacts readable for diagnostics.
PUBLICATION_TRANSITIONS: Mapping[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.UNPUBLISHED: frozenset(
        {PublicationStatus.PUBLISHED, PublicationStatus.WITHDRAWN}
    ),
    PublicationStatus.PUBLISHED: frozenset(
        {PublicationStatus.SUPERSEDED, PublicationStatus.WITHDRAWN}
    ),
    PublicationStatus.SUPERSEDED: frozenset({PublicationStatus.WITHDRAWN}),
    PublicationStatus.WITHDRAWN: frozenset(),
}


class TransitionError(ValueError):
    """Raised when a proposed state change is not in the frozen transition table."""

    def __init__(self, *, machine: str, current: str, target: str, reason: str = "") -> None:
        detail = f"{machine}: {current!r} -> {target!r} is not a legal transition"
        if reason:
            detail = f"{detail} ({reason})"
        super().__init__(detail)
        self.machine = machine
        self.current = current
        self.target = target


_TRANSITIONS: Mapping[str, Mapping[Any, frozenset[Any]]] = {
    "execution_status": EXECUTION_TRANSITIONS,
    "validation_status": VALIDATION_TRANSITIONS,
    "publication_status": PUBLICATION_TRANSITIONS,
}


def legal_transitions(machine: str) -> dict[str, list[str]]:
    """Serialisable transition table for docs, payloads and tests."""

    table = _TRANSITIONS.get(str(machine or "").strip())
    if table is None:
        raise KeyError(f"unknown state machine: {machine!r}")
    return {
        str(state.value): sorted(target.value for target in targets)
        for state, targets in table.items()
    }


def can_transition(machine: str, current: Any, target: Any) -> bool:
    """Return whether ``current -> target`` is legal in the frozen table."""

    table = _TRANSITIONS.get(str(machine or "").strip())
    if table is None:
        raise KeyError(f"unknown state machine: {machine!r}")
    current_state, target_state = _coerce_state(table, current), _coerce_state(table, target)
    if current_state is None or target_state is None:
        return False
    if current_state is target_state:
        # Idempotent re-application is allowed; P0-03 relies on it for retries.
        return True
    return target_state in table[current_state]


def assert_transition(machine: str, current: Any, target: Any, *, reason: str = "") -> None:
    if not can_transition(machine, current, target):
        raise TransitionError(
            machine=str(machine),
            current=_state_value(current),
            target=_state_value(target),
            reason=reason,
        )


def _coerce_state(table: Mapping[Any, frozenset[Any]], value: Any) -> Any:
    if value in table:
        return value
    normalized = _state_value(value)
    for state in table:
        if _state_value(state) == normalized:
            return state
    return None


def _state_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------
# Legacy compatibility mapping
# ---------------------------------------------------------------------------

#: ``job.state`` -> ``execution_status``.  Documented explicitly because the
#: legacy enum carries two meanings that the new contract separates:
#: ``done`` means "execution finished", not "result is valid" and not "result is
#: published".  ``partial`` maps to ``succeeded`` at execution level, while the
#: reason it must not be published lives in ``validation_status`` instead.
LEGACY_JOB_STATE_TO_EXECUTION: Mapping[str, ExecutionStatus] = {
    ParseJobState.PENDING.value: ExecutionStatus.PENDING,
    ParseJobState.PARSING.value: ExecutionStatus.RUNNING,
    ParseJobState.STRUCTURING.value: ExecutionStatus.RUNNING,
    ParseJobState.EMBEDDING.value: ExecutionStatus.RUNNING,
    ParseJobState.PARTIAL.value: ExecutionStatus.SUCCEEDED,
    ParseJobState.DONE.value: ExecutionStatus.SUCCEEDED,
    ParseJobState.FAILED.value: ExecutionStatus.FAILED,
}

#: Legacy job states that may never be interpreted as a validation conclusion.
LEGACY_STATES_THAT_ARE_NOT_VALIDATION: frozenset[str] = frozenset(
    {state.value for state in ParseJobState}
)

#: Interim compatibility alias for the existing ``parse_run_id`` payload field.
#:
#: Until P0-03 stores real run identities, ``parse_run_id`` equals ``job_id`` and
#: hosts may keep treating it as the job identity.  After P0-03 the value becomes
#: the run identity, so a consumer that needs to correlate both must read
#: ``job_id`` and ``parse_run_id`` as separate fields.  The switch is announced by
#: :data:`PARSE_RUN_ID_ALIAS` moving from ``"job_id"`` to ``"run_id"`` in the
#: published contract version, never by silently changing the value.
PARSE_RUN_ID_ALIAS = "job_id"

#: Contract version at which ``parse_run_id`` starts carrying the run identity.
PARSE_RUN_ID_ALIAS_SWITCH_CONTRACT_VERSION = "2026-09-run-publication-contract-v1"


def legacy_execution_status(job_state: Any) -> ExecutionStatus:
    """Map a legacy ``job.state`` value to ``execution_status``.

    An unknown state raises rather than guessing: P0-02 requires that an
    unrecognised state cannot be read as success.
    """

    normalized = _state_value(job_state)
    mapped = LEGACY_JOB_STATE_TO_EXECUTION.get(normalized)
    if mapped is None:
        raise KeyError(f"unknown legacy job state: {job_state!r}")
    return mapped


def execution_status_from_job_state(job_state: Any) -> dict[str, Any]:
    """Compatibility projection for hosts migrating off ``job.state``."""

    execution = legacy_execution_status(job_state)
    return {
        "job_state": _state_value(job_state),
        "execution_status": execution.value,
        # Deliberately constant: no legacy job state implies a validation result.
        "validation_status": ValidationStatus.NOT_RUN.value,
        "validation_status_source": "not_derived_from_job_state",
        "publication_status": PublicationStatus.UNPUBLISHED.value,
    }


# ---------------------------------------------------------------------------
# Validation and publication policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Versioned policy deciding whether a validated run may be published.

    The policy is persisted next to the run so a later policy change cannot
    retroactively rewrite an older run's conclusion (P0-04 requirement).
    """

    version: str = "2026-09-validation-policy-v1"
    publish_on_passed: bool = True
    publish_on_warning: bool = False
    warning_allowlist: tuple[str, ...] = ()
    require_durable_index_for_profiles: tuple[str, ...] = ("rag",)
    require_source_integrity: bool = True

    def may_publish(
        self,
        *,
        validation_status: Any,
        warning_codes: tuple[str, ...] = (),
        profile: str = "default",
        durable_index: bool = True,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for publishing under this policy."""

        status = _validation_value(validation_status)
        if status is ValidationStatus.PASSED:
            allowed = bool(self.publish_on_passed)
            reason = "policy_allows_passed" if allowed else "policy_blocks_passed"
        elif status is ValidationStatus.WARNING:
            if not self.publish_on_warning:
                allowed, reason = False, "policy_blocks_warning"
            else:
                unexpected = tuple(
                    code for code in warning_codes if code not in self.warning_allowlist
                )
                allowed = not unexpected
                reason = "warning_allowlisted" if allowed else "warning_not_allowlisted"
        elif status is ValidationStatus.NOT_RUN:
            allowed, reason = False, "validation_not_run"
        else:
            allowed, reason = False, f"validation_{status.value}"
        if not allowed:
            return False, reason
        # ``require_source_integrity`` is asserted by the validator that produced
        # ``validation_status``; it cannot be re-derived from the status alone.
        if (
            not durable_index
            and str(profile or "").strip().lower() in self.require_durable_index_for_profiles
        ):
            return False, "index_backend_not_durable"
        return True, reason

    def as_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "publish_on_passed": self.publish_on_passed,
            "publish_on_warning": self.publish_on_warning,
            "warning_allowlist": list(self.warning_allowlist),
            "require_durable_index_for_profiles": list(self.require_durable_index_for_profiles),
            "require_source_integrity": self.require_source_integrity,
        }


def _validation_value(value: Any) -> ValidationStatus:
    if isinstance(value, ValidationStatus):
        return value
    normalized = _state_value(value)
    for state in ValidationStatus:
        if state.value == normalized:
            return state
    raise KeyError(f"unknown validation status: {value!r}")


DEFAULT_VALIDATION_POLICY = ValidationPolicy()


# ---------------------------------------------------------------------------
# Storage capability contract (implemented by P0-03)
# ---------------------------------------------------------------------------

#: Storage operations the run/publication contract requires.  P0-03 decides
#: which backends can serve them; a backend that cannot must not take part in
#: strict publication (its capability report says so).
RUN_STORE_OPERATIONS: tuple[str, ...] = (
    "begin_run",
    "save_run_artifacts",
    "seal_run",
    "get_run",
    "get_active_run",
    "publish_run",
    "restore_live_from_run",
    "list_runs",
    "record_validation",
    "record_publication_event",
)

#: Capability keys a store advertises through ``run_store_capabilities()``.
RUN_STORE_CAPABILITY_KEYS: tuple[str, ...] = (
    "run_isolated_artifacts",
    "staged_artifacts",
    "atomic_active_pointer",
    "compare_and_set_active_pointer",
    "claim_fencing",
    "source_version_check",
    "publication_generations",
    "validation_persistence",
    "publication_outbox",
)

#: Capabilities without which a store may not participate in strict publication.
STRICT_PUBLICATION_REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "run_isolated_artifacts",
    "staged_artifacts",
    "atomic_active_pointer",
    "compare_and_set_active_pointer",
    "claim_fencing",
)


class RunStoreCapability(str, Enum):
    """Advertised storage capabilities, used to gate strict publication.

    ``latest_job`` is intentionally **not** in this list: reading the newest job
    stays a diagnostic query and must never be used to decide what is published.
    """

    RUN_ISOLATED_ARTIFACTS = "run_isolated_artifacts"
    STAGED_ARTIFACTS = "staged_artifacts"
    ATOMIC_ACTIVE_POINTER = "atomic_active_pointer"
    COMPARE_AND_SET_ACTIVE_POINTER = "compare_and_set_active_pointer"
    CLAIM_FENCING = "claim_fencing"
    SOURCE_VERSION_CHECK = "source_version_check"
    PUBLICATION_GENERATIONS = "publication_generations"
    VALIDATION_PERSISTENCE = "validation_persistence"
    PUBLICATION_OUTBOX = "publication_outbox"


@dataclass(frozen=True, slots=True)
class RunStoreCapabilities:
    """What a store implementation can actually guarantee."""

    supported: tuple[str, ...] = ()

    def supports(self, capability: Any) -> bool:
        value = capability.value if isinstance(capability, Enum) else str(capability or "").strip().lower()
        return value in {str(item) for item in self.supported}

    def missing(self, required: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(name for name in required if not self.supports(name))

    @property
    def strict_publication_ready(self) -> bool:
        return not self.missing(STRICT_PUBLICATION_REQUIRED_CAPABILITIES)

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "supported": sorted(str(item) for item in self.supported),
            "strict_publication_required": list(STRICT_PUBLICATION_REQUIRED_CAPABILITIES),
            "strict_publication_ready": self.strict_publication_ready,
            "missing_for_strict_publication": list(
                self.missing(STRICT_PUBLICATION_REQUIRED_CAPABILITIES)
            ),
        }


#: Capabilities of the stores shipped today.  Saying a backend is ready here is
#: a claim that must be backed by tests: the SQLite store implements run
#: isolation, staging, the CAS pointer switch and claim fencing (P0-03), while
#: Postgres and the in-memory store still serve the legacy single-version path.
SQLITE_RUN_STORE_CAPABILITIES = RunStoreCapabilities(
    supported=(
        RunStoreCapability.RUN_ISOLATED_ARTIFACTS.value,
        RunStoreCapability.STAGED_ARTIFACTS.value,
        RunStoreCapability.ATOMIC_ACTIVE_POINTER.value,
        RunStoreCapability.COMPARE_AND_SET_ACTIVE_POINTER.value,
        RunStoreCapability.CLAIM_FENCING.value,
        RunStoreCapability.SOURCE_VERSION_CHECK.value,
        RunStoreCapability.PUBLICATION_GENERATIONS.value,
        RunStoreCapability.VALIDATION_PERSISTENCE.value,
    )
)

CURRENT_STORE_CAPABILITIES: Mapping[str, RunStoreCapabilities] = {
    "sqlite": SQLITE_RUN_STORE_CAPABILITIES,
    "postgres": RunStoreCapabilities(supported=()),
    "memory": RunStoreCapabilities(supported=()),
}


def run_store_capabilities(backend: str) -> RunStoreCapabilities:
    normalized = str(backend or "").strip().lower()
    if normalized.startswith("sqlite"):
        normalized = "sqlite"
    elif normalized.startswith(("postgresql", "postgres")):
        normalized = "postgres"
    elif normalized in {"", "memory", "memory://"}:
        normalized = "memory"
    return CURRENT_STORE_CAPABILITIES.get(normalized, RunStoreCapabilities(supported=()))


# ---------------------------------------------------------------------------
# Concurrent publication policy
# ---------------------------------------------------------------------------

#: Policy for two jobs finishing on the same ``tenant/doc`` at the same time.
#: "Last writer wins" is explicitly **not** the policy.
CONCURRENT_PUBLICATION_POLICY: Mapping[str, str] = {
    "policy": "fenced_expected_generation",
    "description": (
        "A publish request must carry the expected source_version and the "
        "expected publication_generation observed when the run began. The "
        "authoritative store compares both inside one transaction that also "
        "checks run state, claim ownership and validation conclusion; a mismatch "
        "returns a conflict instead of overwriting a newer published version."
    ),
    "stale_run_outcome": PublicationStatus.UNPUBLISHED.value,
    "stale_run_diagnostic": "publication_rejected_stale",
    "newer_generation_wins": "true",
    "last_writer_wins": "false",
    "latest_job_used_for_publication": "false",
}


# ---------------------------------------------------------------------------
# Contract payload
# ---------------------------------------------------------------------------

def contract_payload() -> dict[str, Any]:
    """Machine-readable description of the frozen contract.

    Exposed through ``/v1/parse/schemas`` consumers and used by tests so the
    tables in the design documentation cannot drift from the code.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "status": CONTRACT_STATUS,
        "identities": {
            "job_id": "scheduling request identity; retries reuse it",
            "parse_run_id": "one execution attempt; a retry creates a new run",
            "base_run_id": "origin run for derived runs (rechunk/reembed)",
            "active_run_id": "tenant/doc pointer to the published run",
            "publication_generation": "monotonic counter for concurrent publish control",
            "source_version": "immutable input content hash plus size",
        },
        "statuses": {
            "execution_status": [state.value for state in ExecutionStatus],
            "validation_status": [state.value for state in ValidationStatus],
            "publication_status": [state.value for state in PublicationStatus],
        },
        "transitions": {
            "execution_status": legal_transitions("execution_status"),
            "validation_status": legal_transitions("validation_status"),
            "publication_status": legal_transitions("publication_status"),
        },
        "legacy_job_state_mapping": {
            state: status.value for state, status in LEGACY_JOB_STATE_TO_EXECUTION.items()
        },
        "legacy_notes": {
            "done_is_not_published": True,
            "done_is_not_validated": True,
            "partial_is_not_publishable_by_default": True,
            "parse_run_id_alias": PARSE_RUN_ID_ALIAS,
            "parse_run_id_alias_switch_contract_version": PARSE_RUN_ID_ALIAS_SWITCH_CONTRACT_VERSION,
        },
        "artifact_profiles": {
            name: profile.as_payload() for name, profile in sorted(ARTIFACT_PROFILES.items())
        },
        "validation_policy": DEFAULT_VALIDATION_POLICY.as_payload(),
        "store_contract": {
            "operations": list(RUN_STORE_OPERATIONS),
            "capability_keys": list(RUN_STORE_CAPABILITY_KEYS),
            "strict_publication_required_capabilities": list(
                STRICT_PUBLICATION_REQUIRED_CAPABILITIES
            ),
            "current_backends": {
                name: capabilities.as_payload()
                for name, capabilities in sorted(CURRENT_STORE_CAPABILITIES.items())
            },
        },
        "concurrent_publication_policy": dict(CONCURRENT_PUBLICATION_POLICY),
        "consumer_migration": {
            "new_formal_signal": "publication_status == 'published'",
            "legacy_formal_signal": "job.state == 'done' or ProductAdapter.after_parse",
            "legacy_formal_signal_status": "deprecated_and_not_sufficient",
            "formal_read_without_publication": "not_ready_response",
            "partial_results": "explicit_preview_only",
        },
    }


__all__ = [
    "ARTIFACT_PROFILES",
    "ArtifactProfile",
    "ArtifactRequirement",
    "CONCURRENT_PUBLICATION_POLICY",
    "CONTRACT_STATUS",
    "CURRENT_STORE_CAPABILITIES",
    "DEFAULT_VALIDATION_POLICY",
    "EXECUTION_TRANSITIONS",
    "ExecutionStatus",
    "LEGACY_JOB_STATE_TO_EXECUTION",
    "LEGACY_STATES_THAT_ARE_NOT_VALIDATION",
    "PARSE_RUN_ID_ALIAS",
    "PARSE_RUN_ID_ALIAS_SWITCH_CONTRACT_VERSION",
    "PUBLICATION_TRANSITIONS",
    "PublicationStatus",
    "RequiredArtifact",
    "RUN_STORE_CAPABILITY_KEYS",
    "RUN_STORE_OPERATIONS",
    "RunEvent",
    "RunStoreCapabilities",
    "RunStoreCapability",
    "SCHEMA_VERSION",
    "SQLITE_RUN_STORE_CAPABILITIES",
    "STRICT_PUBLICATION_REQUIRED_CAPABILITIES",
    "TransitionError",
    "VALIDATION_TRANSITIONS",
    "ValidationPolicy",
    "ValidationStatus",
    "artifact_profile",
    "assert_transition",
    "can_transition",
    "contract_payload",
    "execution_status_from_job_state",
    "legal_transitions",
    "legacy_execution_status",
    "run_store_capabilities",
]
