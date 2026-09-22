"""Verify versioned baseline and evidence manifests without parsing fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA = "parsecore-regression-baseline-registry/v1"


def _get_path(value: Any, dotted_path: str) -> Any:
    current = value
    for part in dotted_path.split("."):
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise KeyError(dotted_path)
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(dotted_path)
    return current


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(
        right, (int, float)
    ) and not isinstance(right, bool):
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
    return left == right


def _relative_file(root: Path, raw: Any, label: str, errors: list[str]) -> Path | None:
    if not isinstance(raw, str) or not raw:
        errors.append(f"{label}: missing relative path")
        return None
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{label}: path must be relative and cannot traverse parents")
        return None
    root = root.resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        errors.append(f"{label}: path resolves outside its root")
        return None
    return resolved


def _load_object(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label}: cannot read JSON ({type(exc).__name__})")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label}: JSON root must be an object")
        return None
    return value


def _verify_artifact(
    item: dict[str, Any],
    *,
    root: Path,
    label: str,
    errors: list[str],
    projection_values: dict[str, list[Any]],
) -> dict[str, Any] | None:
    path = _relative_file(root, item.get("path"), label, errors)
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        errors.append(f"{label}: artifact unavailable ({type(exc).__name__})")
        return None
    if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
        errors.append(f"{label}: SHA-256 mismatch")
        return None
    payload = _load_object(path, label, errors)
    if payload is None:
        return None

    if "schema" in item:
        try:
            schema = _get_path(payload, str(item.get("schema_path", "schema")))
        except KeyError:
            errors.append(f"{label}: schema field is missing")
        else:
            if schema != item.get("schema"):
                errors.append(f"{label}: schema mismatch")
    for dotted_path, expected in (item.get("metadata") or {}).items():
        try:
            actual = _get_path(payload, dotted_path)
        except KeyError:
            actual = None
        if not _same(actual, expected):
            errors.append(f"{label}: metadata projection mismatch at {dotted_path}")
    for name, projection in (item.get("projections") or {}).items():
        if not isinstance(projection, dict) or not isinstance(projection.get("path"), str):
            errors.append(f"{label}: invalid projection {name!r}")
            continue
        try:
            actual = _get_path(payload, projection["path"])
        except KeyError:
            errors.append(f"{label}: projection path is missing for {name!r}")
            continue
        if not _same(actual, projection.get("value")):
            errors.append(f"{label}: projection mismatch for {name!r}")
        projection_values.setdefault(name, []).append(actual)
    return payload


def validate_registry(registry_path: Path, evidence_root: Path) -> list[str]:
    """Verify artifact hashes, metadata, declared projections, and delta arithmetic."""
    errors: list[str] = []
    registry = _load_object(registry_path, "registry", errors)
    if registry is None:
        return errors
    if registry.get("schema") != REGISTRY_SCHEMA or registry.get("schema_version") != 1:
        return [f"registry must use {REGISTRY_SCHEMA} with schema_version 1"]

    values: dict[str, list[Any]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_groups = (
        (registry.get("baseline_artifacts"), registry_path.parent, "baseline"),
        (registry.get("evidence_artifacts"), evidence_root, "evidence"),
    )
    for items, root, kind in artifact_groups:
        if not isinstance(items, list):
            errors.append(f"registry {kind}_artifacts must be a list")
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                errors.append(f"registry {kind}_artifacts contains an invalid entry")
                continue
            artifact_id = item["id"]
            if artifact_id in artifacts:
                errors.append(f"duplicate artifact id {artifact_id!r}")
                continue
            payload = _verify_artifact(
                item,
                root=root,
                label=f"{kind} {artifact_id!r}",
                errors=errors,
                projection_values=values,
            )
            if payload is not None:
                artifacts[artifact_id] = payload

    for name, expected in (registry.get("projections") or {}).items():
        observed = values.get(name, [])
        if not observed or any(not _same(value, expected) for value in observed):
            errors.append(f"registry projection {name!r} is missing or inconsistent")
    for name, observed in values.items():
        if observed and any(not _same(value, observed[0]) for value in observed[1:]):
            errors.append(f"artifact projections disagree for {name!r}")

    comparisons = registry.get("comparisons")
    if not isinstance(comparisons, list):
        errors.append("registry comparisons must be a list")
        return errors
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            errors.append("registry contains an invalid comparison entry")
            continue
        for artifact_id in comparison.get("artifact_ids", []):
            if artifact_id not in artifacts:
                errors.append(f"comparison references unverified artifact {artifact_id!r}")
        for metric, delta in (comparison.get("metric_deltas") or {}).items():
            if not isinstance(delta, dict):
                errors.append(f"comparison metric delta {metric!r} is invalid")
                continue
            reference_values = values.get(delta.get("reference_projection"), [])
            candidate_values = values.get(delta.get("candidate_projection"), [])
            if not reference_values or not candidate_values:
                errors.append(f"comparison metric delta {metric!r} references a missing projection")
                continue
            actual_delta = candidate_values[0] - reference_values[0]
            if not _same(actual_delta, delta.get("delta")):
                errors.append(f"comparison metric delta {metric!r} is inconsistent")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="Validate registry artifacts and projections")
    validate.add_argument("--registry", required=True, type=Path)
    validate.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)

    errors = validate_registry(args.registry, args.evidence_root)
    if errors:
        for error in errors:
            print(f"[registry] FAILED: {error}")
        return 1
    print(f"[registry] VALID: {args.registry.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())