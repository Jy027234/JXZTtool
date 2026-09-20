"""Publication validation rules (P0-04).

Turns the existing quality/coverage rules into a **validation conclusion** that
gates publication.  The same evaluation runs in two places and must agree:

* before publishing a run (``runtime.publish_run`` / ``RunStore.publish_run``);
* when a status API reports what a run's conclusion is.

Design rules
------------
* A run that has artifacts but was never validated is ``not_run`` — never
  ``passed``.  The store enforces that independently; this module only decides
  what the validator concluded.
* ``passed`` means every required artifact is present **and** no error-severity
  rule fired.  A warning-severity rule produces ``warning``, which the
  :class:`~parsecore.publication.ValidationPolicy` may or may not let through.
* A validator failure is ``error``, not ``passed``: a broken validator must not
  silently unlock publication.
* The rules reuse :mod:`parsecore.quality` values rather than recomputing
  different numbers, so a reader that inspects the run sees the same metrics the
  gate used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .publication import (
    ARTIFACT_PROFILES,
    DEFAULT_VALIDATION_POLICY,
    ArtifactRequirement,
    ValidationStatus,
)
from .quality import evaluate_parse_quality

VALIDATION_RULE_VERSION = "2026-09-publication-validation-rules-v1"

#: Artifact family -> the staged view that satisfies it.
_ARTIFACT_STAGE_KEYS: Mapping[str, tuple[str, ...]] = {
    "blocks": (),
    "chunks": (),
    "reader": ("pages",),
    "records": ("records",),
    "rag": ("chunks",),
    "assets": ("assets",),
    "export": ("export",),
}


@dataclass(slots=True, frozen=True)
class ValidationRule:
    """One named rule outcome."""

    code: str
    status: ValidationStatus
    message: str
    metric: str = ""
    observed: Any = None
    threshold: Any = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "status": self.status.value,
            "message": self.message,
        }
        if self.metric:
            payload["metric"] = self.metric
        if self.observed is not None:
            payload["observed"] = self.observed
        if self.threshold is not None:
            payload["threshold"] = self.threshold
        return payload


@dataclass(slots=True, frozen=True)
class ValidationReport:
    """The persisted validation conclusion for a run."""

    status: ValidationStatus
    reason: str
    rule_version: str = VALIDATION_RULE_VERSION
    policy_version: str = DEFAULT_VALIDATION_POLICY.version
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: Rules that reject the run.  Kept separate from ``errors`` because
    #: ``errors`` means "the validator itself failed", which must never be
    #: confused with "the validator concluded the run is not acceptable".
    rejections: tuple[str, ...] = ()
    #: Rules that require a human decision before publication.
    reviews: tuple[str, ...] = ()
    #: Non-gating observations.
    informational: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    rules: tuple[ValidationRule, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "rule_version": self.rule_version,
            "policy_version": self.policy_version,
            "warning_codes": list(self.warnings),
            "rejection_codes": list(self.rejections),
            "review_codes": list(self.reviews),
            "error_codes": list(self.errors),
            "informational_codes": list(self.informational),
            "metrics": dict(self.metrics),
            "evidence_refs": list(self.evidence_refs),
            "rules": [rule.as_payload() for rule in self.rules],
        }


def _artifact_count(artifact: str, counts: Mapping[str, int]) -> int:
    normalized = str(artifact or "").strip().lower()
    if normalized in {"blocks", "chunks"}:
        return int(counts.get(normalized, 0))
    keys = _ARTIFACT_STAGE_KEYS.get(normalized, (normalized,))
    if not keys:
        return int(counts.get(normalized, 0))
    return min(int(counts.get(key, 0)) for key in keys)


def _threshold(options: Mapping[str, Any], key: str, default: float | None) -> float | None:
    raw = options.get(key, default)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def validate_run(
    *,
    blocks: Sequence[Any] = (),
    chunks: Sequence[Any] = (),
    views: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    quality_signals: Sequence[Mapping[str, Any]] = (),
    artifact_profile: str = "default",
    required_artifacts: Sequence[str] | None = None,
    options: Mapping[str, Any] | None = None,
    placeholder_parsers: Sequence[str] = (),
    policy_version: str | None = None,
) -> ValidationReport:
    """Evaluate the publication rules and return the conclusion.

    ``quality_signals`` are the same signal dicts the document projections
    expose (``{"code", "severity", "message", ...}``), so an API consumer and
    the gate cannot disagree about which signals fired.
    """

    view_map = {str(key): tuple(value or ()) for key, value in (views or {}).items()}
    counts: dict[str, int] = {
        "blocks": len(blocks),
        "chunks": len(chunks),
        **{name: len(items) for name, items in view_map.items()},
    }
    profile_name = str(artifact_profile or "default")
    profile = ARTIFACT_PROFILES.get(profile_name)
    if profile is None:
        return ValidationReport(
            status=ValidationStatus.ERROR,
            reason="unknown_artifact_profile",
            policy_version=str(policy_version or DEFAULT_VALIDATION_POLICY.version),
            errors=("unknown_artifact_profile",),
            metrics={"artifact_profile": profile_name},
        )

    required = (
        tuple(required_artifacts) if required_artifacts is not None else profile.required_artifacts()
    )
    rules: list[ValidationRule] = []

    # 1. Required artifacts must be present.  A profile that declares an
    #    artifact optional may miss it, but the miss is reported.
    missing_required: list[str] = []
    for artifact in required:
        if _artifact_count(artifact, counts) <= 0:
            missing_required.append(str(artifact))
            rules.append(
                ValidationRule(
                    code=f"required_artifact_missing:{artifact}",
                    status=ValidationStatus.REJECTED,
                    message=f"required artifact {artifact!r} is missing or empty for profile {profile_name!r}",
                    metric=f"artifact_count.{artifact}",
                    observed=0,
                    threshold=1,
                )
            )
    degraded_optional: list[str] = []
    for artifact in sorted(profile.requirements):
        requirement = profile.requirement_for(artifact)
        if requirement is not ArtifactRequirement.OPTIONAL or artifact in required:
            continue
        if _artifact_count(artifact, counts) <= 0:
            degraded_optional.append(str(artifact))

    # 2. A placeholder result is not a formal result.
    if placeholder_parsers:
        rules.append(
            ValidationRule(
                code="placeholder_result",
                status=ValidationStatus.REJECTED,
                message=(
                    "the result came from a placeholder parser: "
                    + ", ".join(sorted(str(name) for name in placeholder_parsers))
                ),
                metric="placeholder_parsers",
                observed=list(placeholder_parsers),
            )
        )

    # 3. Structural quality from the shared quality module.
    quality = evaluate_parse_quality(list(blocks))
    metrics: dict[str, Any] = {
        "artifact_profile": profile_name,
        "quality_score": quality.score,
        "quality_flags": sorted(quality.flags),
        "artifact_counts": counts,
        "degraded_optional_artifacts": degraded_optional,
    }
    if quality.total_cid_tokens:
        metrics["total_cid_tokens"] = quality.total_cid_tokens
    if quality.ocr_failed_pages:
        metrics["ocr_failed_pages"] = quality.ocr_failed_pages
    if quality.suspect_signature_pages:
        metrics["suspect_signature_pages"] = quality.suspect_signature_pages

    if not blocks:
        rules.append(
            ValidationRule(
                code="empty_document",
                status=ValidationStatus.REJECTED,
                message="the run produced no blocks",
                metric="block_count",
                observed=0,
                threshold=1,
            )
        )
    if quality.total_cid_tokens:
        rules.append(
            ValidationRule(
                code="cid_garble",
                status=ValidationStatus.REJECTED,
                message="CID garble tokens remain in the parsed text",
                metric="total_cid_tokens",
                observed=quality.total_cid_tokens,
                threshold=0,
            )
        )
    if quality.ocr_failed_pages:
        rules.append(
            ValidationRule(
                code="ocr_failed_pages",
                status=ValidationStatus.NEEDS_REVIEW,
                message="OCR failed on one or more pages",
                metric="ocr_failed_pages",
                observed=quality.ocr_failed_pages,
                threshold=0,
            )
        )
    if quality.suspect_signature_pages:
        rules.append(
            ValidationRule(
                code="suspect_signature_pages",
                status=ValidationStatus.WARNING,
                message="signature-heavy pages may be misclassified",
                metric="suspect_signature_pages",
                observed=quality.suspect_signature_pages,
                threshold=0,
            )
        )

    # 4. Quality signals the caller already computed (projection and coverage
    #    signals).  Only ``error`` severity rejects; every other severity is
    #    advisory, so an unfamiliar severity string degrades to a warning rather
    #    than being dropped or silently promoted to a rejection.
    for signal in quality_signals:
        code = str(signal.get("code") or "").strip()
        if not code:
            continue
        severity = str(signal.get("severity") or "warning").strip().lower()
        if severity == "error":
            rules.append(
                ValidationRule(
                    code=f"quality_signal:{code}",
                    status=ValidationStatus.REJECTED,
                    message=str(signal.get("message") or f"error quality signal {code}"),
                    metric="quality_signal",
                    observed=code,
                )
            )
        elif severity in {"info", "informational"}:
            rules.append(
                ValidationRule(
                    code=f"quality_signal:{code}",
                    status=ValidationStatus.INFO,
                    message=str(signal.get("message") or f"informational signal {code}"),
                    metric="quality_signal",
                    observed=code,
                )
            )
        else:
            rules.append(
                ValidationRule(
                    code=f"quality_signal:{code}",
                    status=ValidationStatus.WARNING,
                    message=str(signal.get("message") or f"warning quality signal {code}"),
                    metric="quality_signal",
                    observed=code,
                )
            )

    # 5. Reading-order confidence, when the parser reported it.
    confidence_values = [
        float(value)
        for value in _reading_order_confidences(blocks)
        if value is not None
    ]
    threshold = _threshold(
        dict(options or {}), "min_reading_order_confidence", 0.75
    )
    if confidence_values and threshold is not None:
        worst = min(confidence_values)
        metrics["min_reading_order_confidence"] = worst
        if worst < threshold:
            rules.append(
                ValidationRule(
                    code="low_reading_order_confidence",
                    status=ValidationStatus.WARNING,
                    message="reading order confidence is below the configured threshold",
                    metric="min_reading_order_confidence",
                    observed=round(worst, 4),
                    threshold=threshold,
                )
            )

    # 6. Optional artifact misses are informational: a deployment that does not
    #    vectorise must not be forced onto the warning policy just because RAG
    #    artifacts are absent.
    if degraded_optional:
        rules.append(
            ValidationRule(
                code="optional_artifacts_empty",
                status=ValidationStatus.INFO,
                message="optional artifacts are empty: " + ", ".join(degraded_optional),
                metric="degraded_optional_artifacts",
                observed=list(degraded_optional),
            )
        )

    return _conclude(
        rules,
        counts=counts,
        extra_metrics=metrics,
        policy_version=str(policy_version or DEFAULT_VALIDATION_POLICY.version),
    )


def _reading_order_confidences(blocks: Iterable[Any]) -> tuple[float | None, ...]:
    values: list[float | None] = []
    for block in blocks:
        metadata = getattr(block, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        raw = metadata.get("reading_order_confidence")
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            values.append(None)
    return tuple(values)


def _conclude(
    rules: Sequence[ValidationRule],
    *,
    counts: Mapping[str, int],
    extra_metrics: Mapping[str, Any] | None = None,
    policy_version: str,
) -> ValidationReport:
    """Reduce rule outcomes to one status.

    Precedence: ``error`` > ``rejected`` > ``needs_review`` > ``warning`` >
    ``passed``.  ``info`` rules are recorded but never decide the status: an
    informational finding must not push a deployment onto the warning policy.
    A rule that says "error" means the rules themselves could not reach a
    conclusion, which must never read as a pass.
    """

    errors = tuple(rule.code for rule in rules if rule.status is ValidationStatus.ERROR)
    rejected = tuple(rule.code for rule in rules if rule.status is ValidationStatus.REJECTED)
    review = tuple(rule.code for rule in rules if rule.status is ValidationStatus.NEEDS_REVIEW)
    warnings = tuple(rule.code for rule in rules if rule.status is ValidationStatus.WARNING)
    informational = tuple(rule.code for rule in rules if rule.status is ValidationStatus.INFO)

    if errors:
        status, reason = ValidationStatus.ERROR, errors[0]
    elif rejected:
        status, reason = ValidationStatus.REJECTED, rejected[0]
    elif review:
        status, reason = ValidationStatus.NEEDS_REVIEW, review[0]
    elif warnings:
        status, reason = ValidationStatus.WARNING, warnings[0]
    else:
        status, reason = ValidationStatus.PASSED, "all_required_rules_passed"

    metrics: dict[str, Any] = dict(extra_metrics or {})
    metrics["counts"] = dict(counts)
    if informational:
        metrics["informational_rules"] = list(informational)

    return ValidationReport(
        status=status,
        reason=reason,
        policy_version=policy_version,
        warnings=warnings,
        errors=errors,
        rejections=rejected,
        reviews=review,
        informational=informational,
        metrics=metrics,
        evidence_refs=tuple(
            dict.fromkeys(
                [
                    f"rule:{rule.code}"
                    for rule in rules
                    if rule.status
                    not in (ValidationStatus.PASSED, ValidationStatus.INFO)
                ]
            )
        ),
        rules=tuple(rules),
    )


def validate_staged_run(
    *,
    store: Any,
    parse_run_id: str,
    artifact_profile: str = "default",
    required_artifacts: Sequence[str] | None = None,
    options: Mapping[str, Any] | None = None,
    policy_version: str | None = None,
) -> ValidationReport:
    """Validate a run directly from its sealed staging area.

    Used when the caller no longer holds the artifacts in memory: the staged
    rows are the immutable fact, so validating them is equivalent to validating
    what will be published.
    """

    blocks, chunks, views = _load_staged(store=store, parse_run_id=parse_run_id)
    return validate_run(
        blocks=blocks,
        chunks=chunks,
        views=views,
        artifact_profile=artifact_profile,
        required_artifacts=required_artifacts,
        options=options,
        policy_version=policy_version,
    )


def _load_staged(
    *, store: Any, parse_run_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    with store._connect() as connection:  # noqa: SLF001 - same-package collaborator
        block_rows = connection.execute(
            "SELECT payload_json FROM run_blocks WHERE parse_run_id = ? ORDER BY position",
            (parse_run_id,),
        ).fetchall()
        chunk_rows = connection.execute(
            "SELECT payload_json FROM run_chunks WHERE parse_run_id = ? ORDER BY position",
            (parse_run_id,),
        ).fetchall()
        view_rows = connection.execute(
            "SELECT view_type, payload_json FROM run_document_views"
            " WHERE parse_run_id = ? ORDER BY view_type, position",
            (parse_run_id,),
        ).fetchall()
    views: dict[str, list[dict[str, Any]]] = {}
    for row in view_rows:
        views.setdefault(str(row["view_type"]), []).append(json.loads(row["payload_json"]))
    return (
        [json.loads(row["payload_json"]) for row in block_rows],
        [json.loads(row["payload_json"]) for row in chunk_rows],
        views,
    )


__all__ = [
    "VALIDATION_RULE_VERSION",
    "ValidationReport",
    "ValidationRule",
    "validate_run",
    "validate_staged_run",
]
