"""Reusable regression corpus manifest validator (P0-06).

The regression suites in ``var/regression/suite.*.json`` point at baseline
snapshots, which in turn point at fixture documents.  Every consumer (the
self-check gate, ``tools/regression_baseline.py check-suite`` and the CI
detection steps) needs to answer the same question before it can claim
anything about parse quality:

* is the suite structurally usable (entries, baselines, fixture list)?
* are the referenced fixtures present as regular files?
* did any sample change since the baseline was recorded (hash drift)?

Historically that logic was duplicated inline in three CI jobs and drifted:
``disabled`` entries were read as JSON (a directory baseline raised
``PermissionError`` on Windows), a missing baseline file was silently
``continue``-ed, and an empty fixture list reported ``available=true``.

This module is the single implementation.  It never raises for malformed
paths or malformed JSON: every problem is reported as a finding with a
machine-readable ``reason``.

Vocabulary
----------

Entry ``status`` (one per suite entry)::

    verified    baseline + fixtures resolved, all fixtures present, no drift
    hash_drift  fixtures present but a recorded hash/size no longer matches
    missing     at least one fixture is absent
    unavailable baseline absent / not a regular file / unreadable / bad path
    empty       baseline present but declares zero fixtures
    error       malformed entry or unusable path value
    disabled    entry marked ``disabled``/``excluded``/``enabled=false``
    skipped     entry excluded by tag filter (see ``excluded_tags``)

Aggregate semantics::

    available   every selected entry is runnable: baseline readable, at least
                one fixture declared, every fixture present as a regular file
    verified    available AND at least one selected entry AND at least one
                fixture AND zero hash drift

``available`` is the contract the CI smoke fallback keys off (it means "this
runner can actually exercise the baseline"), while ``verified`` is the release
gate contract (it means "the corpus is complete and unchanged").  A corpus can
therefore be ``available`` but not ``verified`` when a sample's content drifted
after the baseline was recorded.

CLI::

    python tools/corpus_manifest.py --suite var/regression/suite.fast.json
    python tools/corpus_manifest.py --suite var/regression/suite.full.json \
        --require-verified --out var/self-check/corpus.full.json

Exit codes::

    0  report produced (corpus verified, or --require-verified not requested)
    1  --require-verified requested and the corpus is not verified
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "2026-09-corpus-manifest"
FIXTURE_ROOT_ENV = "PARSECORE_REGRESSION_FIXTURE_ROOT"
DEFAULT_EXCLUDED_TAGS: tuple[str, ...] = ("slow",)

# Entry statuses.
STATUS_VERIFIED = "verified"
STATUS_HASH_DRIFT = "hash_drift"
STATUS_MISSING = "missing"
STATUS_UNAVAILABLE = "unavailable"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
STATUS_SKIPPED = "skipped"

RUNNABLE_STATUSES = (STATUS_VERIFIED, STATUS_HASH_DRIFT)
UNAVAILABLE_STATUSES = (
    STATUS_MISSING,
    STATUS_UNAVAILABLE,
    STATUS_EMPTY,
    STATUS_ERROR,
)

# Entry-level reasons (never raised, always reported).
REASON_DISABLED_ENTRY = "disabled_entry"
REASON_EXCLUDED_BY_TAG = "excluded_by_tag"
REASON_ENTRY_NOT_OBJECT = "entry_not_an_object"
REASON_INVALID_BASELINE_PATH = "invalid_baseline_path"
REASON_EMPTY_BASELINE_PATH = "empty_baseline_path"
REASON_BASELINE_MISSING = "baseline_missing"
REASON_BASELINE_NOT_FILE = "baseline_not_a_regular_file"
REASON_BASELINE_UNREADABLE = "baseline_unreadable"
REASON_BASELINE_INVALID_JSON = "baseline_invalid_json"
REASON_BASELINE_INVALID_SHAPE = "baseline_invalid_shape"
REASON_EMPTY_FIXTURE_SET = "empty_fixture_set"
REASON_FIXTURE_MISSING = "fixture_missing"
REASON_FIXTURE_NOT_FILE = "fixture_not_a_regular_file"
REASON_FIXTURE_HASH_DRIFT = "fixture_hash_drift"
REASON_FIXTURE_UNSPECIFIED = "fixture_path_unspecified"
REASON_INVALID_PATH = "invalid_path"
# Suite-level reasons.
REASON_SUITE_MISSING = "suite_missing"
REASON_SUITE_NOT_FILE = "suite_not_a_regular_file"
REASON_SUITE_UNREADABLE = "suite_unreadable"
REASON_SUITE_INVALID_JSON = "suite_invalid_json"
REASON_SUITE_INVALID_SHAPE = "suite_invalid_shape"
REASON_SUITE_EMPTY = "suite_empty"
REASON_NO_SELECTED_ENTRIES = "no_selected_entries"

# Fixture-level hash keys understood in a baseline payload.  ``save`` writes
# ``fixture_sha256`` / ``fixture_size_bytes``; older baselines record nothing,
# which is reported as ``hash_unrecorded`` instead of a failure.
HASH_KEYS: tuple[str, ...] = ("fixture_sha256", "sha256", "source_sha256")
SIZE_KEYS: tuple[str, ...] = ("fixture_size_bytes", "size_bytes", "bytes")

UNVERIFIED_REASON_PRIORITY: tuple[str, ...] = (
    REASON_INVALID_PATH,
    REASON_SUITE_MISSING,
    REASON_SUITE_NOT_FILE,
    REASON_SUITE_UNREADABLE,
    REASON_SUITE_INVALID_JSON,
    REASON_SUITE_INVALID_SHAPE,
    REASON_SUITE_EMPTY,
    REASON_NO_SELECTED_ENTRIES,
    REASON_ENTRY_NOT_OBJECT,
    REASON_INVALID_BASELINE_PATH,
    REASON_EMPTY_BASELINE_PATH,
    REASON_BASELINE_MISSING,
    REASON_BASELINE_NOT_FILE,
    REASON_BASELINE_UNREADABLE,
    REASON_BASELINE_INVALID_JSON,
    REASON_BASELINE_INVALID_SHAPE,
    REASON_EMPTY_FIXTURE_SET,
    REASON_FIXTURE_UNSPECIFIED,
    REASON_FIXTURE_NOT_FILE,
    REASON_FIXTURE_MISSING,
    REASON_FIXTURE_HASH_DRIFT,
)


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


def _expand(raw: Any) -> str:
    """Expand ``~`` and environment variables; never raises."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    try:
        return os.path.expandvars(os.path.expanduser(text))
    except (KeyError, ValueError):  # pragma: no cover - defensive
        return text


def resolve_path(base_dir: Path, raw_path: Any) -> Path:
    """Resolve a manifest path against ``base_dir`` with ``~``/env expansion."""
    expanded = _expand(raw_path)
    if not expanded:
        return base_dir
    path = Path(expanded)
    if path.is_absolute():
        return _try_resolve(path)
    return _try_resolve(base_dir / path)


def _try_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, ValueError):
        return path


def resolve_fixture_root(
    raw_root: Any = None,
    *,
    env_name: str = FIXTURE_ROOT_ENV,
) -> Path | None:
    """Resolve the fixture root from an explicit value, else its env var."""
    configured = str(raw_root or "").strip() or str(os.environ.get(env_name, "") or "")
    expanded = _expand(configured)
    if not expanded:
        return None
    return Path(expanded)


def _probe(path: Path) -> tuple[str, str | None]:
    """Classify a path without raising: file/directory/other/missing/error."""
    try:
        if path.is_file():
            return "file", None
        if path.is_dir():
            return "directory", None
        if path.exists():
            return "other", None
        return "missing", None
    except (OSError, ValueError) as exc:
        return "error", str(exc)


def file_sha256(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fixture_digest(path: Path) -> dict[str, Any] | None:
    """Return ``fixture_sha256``/``fixture_size_bytes`` for a regular file."""
    probed, _message = _probe(Path(path))
    if probed != "file":
        return None
    try:
        size_bytes = Path(path).stat().st_size
        return {
            "fixture_sha256": file_sha256(Path(path)),
            "fixture_size_bytes": int(size_bytes),
        }
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# fixture resolution (single implementation for every caller)
# ---------------------------------------------------------------------------


@dataclass
class FixtureResolution:
    path: Path
    raw: str
    relative: str
    reason: str | None = None
    message: str | None = None


def resolve_fixture_entry(
    entry: Mapping[str, Any],
    *,
    baseline_dir: Path,
    fixture_root: Path | None = None,
    fixture_root_env: str | None = None,
) -> FixtureResolution:
    """Resolve one baseline fixture record to a candidate path.

    Honours, in order: ``fixture_relative_path`` under the fixture root, the
    recorded ``fixture`` path (absolute, ``~`` or env-var expanded), and the
    fixture root with the bare file name.  Mirrors the historical behaviour of
    ``tools.regression_baseline._resolve_fixture_entry_path`` so both callers
    agree on which file a baseline refers to.
    """
    raw_value = entry.get("fixture")
    relative_value = entry.get("fixture_relative_path")
    if raw_value is not None and not isinstance(raw_value, str):
        return FixtureResolution(
            path=baseline_dir,
            raw=str(raw_value),
            relative=str(relative_value or ""),
            reason=REASON_INVALID_PATH,
            message=f"fixture must be a string, got {type(raw_value).__name__}",
        )
    if relative_value is not None and not isinstance(relative_value, str):
        return FixtureResolution(
            path=baseline_dir,
            raw=str(raw_value or ""),
            relative=str(relative_value),
            reason=REASON_INVALID_PATH,
            message=f"fixture_relative_path must be a string, got {type(relative_value).__name__}",
        )

    raw = _expand(raw_value)
    relative = _expand(relative_value)
    resolved_root = fixture_root or resolve_fixture_root(
        None,
        env_name=str(fixture_root_env or FIXTURE_ROOT_ENV),
    )
    baseline_path = resolve_path(baseline_dir, raw) if raw else baseline_dir
    if not raw and not relative:
        return FixtureResolution(
            path=baseline_path,
            raw="",
            relative="",
            reason=REASON_FIXTURE_UNSPECIFIED,
            message="fixture entry records neither 'fixture' nor 'fixture_relative_path'",
        )
    if relative and resolved_root is not None:
        candidate = resolve_path(resolved_root, relative)
        candidate_state, _candidate_message = _probe(candidate)
        baseline_state, _baseline_message = _probe(baseline_path)
        if candidate_state != "missing" or baseline_state == "missing":
            return FixtureResolution(path=candidate, raw=raw, relative=relative)
    if not raw:
        # Only a relative path is recorded and no fixture root is configured:
        # resolve it next to the baseline instead of returning the baseline
        # directory (which used to be probed as if it were the document).
        return FixtureResolution(
            path=resolve_path(baseline_dir, relative),
            raw=raw,
            relative=relative,
        )
    baseline_state, _baseline_message = _probe(baseline_path)
    if baseline_state != "missing":
        return FixtureResolution(path=baseline_path, raw=raw, relative=relative)
    if resolved_root is not None:
        fallback_name = Path(relative or raw).name
        return FixtureResolution(
            path=resolve_path(resolved_root, fallback_name),
            raw=raw,
            relative=relative,
        )
    if relative:
        return FixtureResolution(
            path=resolve_path(baseline_dir, relative),
            raw=raw,
            relative=relative,
        )
    return FixtureResolution(path=baseline_path, raw=raw, relative=relative)


def resolve_fixture_entry_path(
    entry: Mapping[str, Any],
    *,
    baseline_dir: Path,
    fixture_root: Path | None,
    fixture_root_env: str | None = None,
) -> Path:
    """Back-compatible ``Path``-returning wrapper used by regression_baseline."""
    return resolve_fixture_entry(
        entry,
        baseline_dir=baseline_dir,
        fixture_root=fixture_root,
        fixture_root_env=fixture_root_env,
    ).path


# ---------------------------------------------------------------------------
# report model
# ---------------------------------------------------------------------------


@dataclass
class FixtureReport:
    raw: str
    relative: str
    resolved: str | None
    status: str
    reason: str | None
    message: str | None
    size_bytes: int | None
    sha256: str | None
    recorded_sha256: str | None
    recorded_size_bytes: int | None
    hash_verified: bool
    hash_recorded: bool

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def missing(self) -> bool:
        return self.status == STATUS_MISSING

    @property
    def hash_drift(self) -> bool:
        return self.status == STATUS_HASH_DRIFT


@dataclass
class EntryReport:
    index: int
    name: str
    tags: list[str] = field(default_factory=list)
    disabled: bool = False
    skipped: bool = False
    baseline_raw: str = ""
    baseline_path: str | None = None
    baseline_sha256: str | None = None
    fixture_root: str | None = None
    status: str = STATUS_UNAVAILABLE
    reason: str | None = None
    message: str | None = None
    fixture_count: int = 0
    verified_fixture_count: int = 0
    missing_fixture_count: int = 0
    hash_drift_count: int = 0
    hash_unrecorded_count: int = 0
    fixtures: list[FixtureReport] = field(default_factory=list)

    @property
    def selected(self) -> bool:
        return not self.disabled and not self.skipped

    @property
    def available(self) -> bool:
        return self.status in RUNNABLE_STATUSES

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED


@dataclass
class CorpusReport:
    schema_version: str = SCHEMA_VERSION
    suite: str = ""
    suite_sha256: str | None = None
    fixture_root: str | None = None
    include_tags: list[str] = field(default_factory=list)
    excluded_tags: list[str] = field(default_factory=list)
    status: str = STATUS_UNAVAILABLE
    available: bool = False
    verified: bool = False
    reason: str | None = None
    message: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)
    fixture_reasons: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    entries: list[EntryReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _normalize_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    if not isinstance(raw_tags, Sequence):
        return []
    tags: list[str] = []
    for tag in raw_tags:
        text = str(tag).strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def _recorded_digest(fixture: Mapping[str, Any]) -> tuple[str | None, int | None]:
    recorded_sha: str | None = None
    for key in HASH_KEYS:
        value = fixture.get(key)
        if isinstance(value, str) and value.strip():
            recorded_sha = value.strip().lower()
            break
    recorded_size: int | None = None
    for key in SIZE_KEYS:
        value = fixture.get(key)
        if isinstance(value, bool):
            continue
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        recorded_size = parsed
        break
    return recorded_sha, recorded_size


def _empty_totals() -> dict[str, int]:
    return {
        "entries": 0,
        "selected_entries": 0,
        "verified_entries": 0,
        "runnable_entries": 0,
        "unverified_entries": 0,
        "missing_entries": 0,
        "unavailable_entries": 0,
        "empty_entries": 0,
        "hash_drift_entries": 0,
        "error_entries": 0,
        "disabled_entries": 0,
        "skipped_entries": 0,
        "fixtures": 0,
        "verified_fixtures": 0,
        "missing_fixtures": 0,
        "hash_drift_fixtures": 0,
        "hash_unrecorded_fixtures": 0,
    }


def _copy_totals(entries: Sequence[EntryReport]) -> dict[str, int]:
    totals = _empty_totals()
    totals["entries"] = len(entries)
    for entry in entries:
        if entry.disabled:
            totals["disabled_entries"] += 1
            continue
        if entry.skipped:
            totals["skipped_entries"] += 1
            continue
        totals["selected_entries"] += 1
        if entry.status == STATUS_VERIFIED:
            totals["verified_entries"] += 1
        elif entry.status == STATUS_HASH_DRIFT:
            totals["hash_drift_entries"] += 1
        elif entry.status == STATUS_MISSING:
            totals["missing_entries"] += 1
        elif entry.status == STATUS_EMPTY:
            totals["empty_entries"] += 1
        elif entry.status == STATUS_ERROR:
            totals["error_entries"] += 1
        else:
            totals["unavailable_entries"] += 1
        if entry.status in RUNNABLE_STATUSES:
            totals["runnable_entries"] += 1
        else:
            totals["unverified_entries"] += 1
        totals["fixtures"] += entry.fixture_count
        totals["verified_fixtures"] += entry.verified_fixture_count
        totals["missing_fixtures"] += entry.missing_fixture_count
        totals["hash_drift_fixtures"] += entry.hash_drift_count
        totals["hash_unrecorded_fixtures"] += entry.hash_unrecorded_count
    return totals


def _count_reasons(entries: Sequence[EntryReport]) -> tuple[dict[str, int], dict[str, int]]:
    reasons: dict[str, int] = {}
    fixture_reasons: dict[str, int] = {}
    for entry in entries:
        if entry.reason:
            reasons[entry.reason] = reasons.get(entry.reason, 0) + 1
        for fixture in entry.fixtures:
            if fixture.reason:
                fixture_reasons[fixture.reason] = fixture_reasons.get(fixture.reason, 0) + 1
    return dict(sorted(reasons.items())), dict(sorted(fixture_reasons.items()))


def _dominant_reason(report: CorpusReport) -> str:
    for reason in UNVERIFIED_REASON_PRIORITY:
        if report.reasons.get(reason):
            return reason
    for reason in report.reasons:
        return reason
    if report.available and not report.verified:
        return REASON_FIXTURE_HASH_DRIFT
    return REASON_NO_SELECTED_ENTRIES


def _finalize(report: CorpusReport) -> CorpusReport:
    suite_reasons = dict(report.reasons)
    report.totals = _copy_totals(report.entries)
    entry_reasons, fixture_reasons = _count_reasons(report.entries)
    for reason, count in suite_reasons.items():
        entry_reasons.setdefault(reason, count)
    report.reasons = dict(sorted(entry_reasons.items()))
    report.fixture_reasons = fixture_reasons
    totals = report.totals
    if totals["selected_entries"] <= 0:
        report.available = False
        report.verified = False
        if not report.reasons:
            report.reasons = {REASON_NO_SELECTED_ENTRIES: 1}
        if report.status == STATUS_VERIFIED:
            report.status = STATUS_UNAVAILABLE
    else:
        report.available = totals["unverified_entries"] == 0
        report.verified = report.available and totals["hash_drift_entries"] == 0
        if report.verified:
            report.status = STATUS_VERIFIED
        elif totals["error_entries"]:
            report.status = STATUS_ERROR
        elif totals["missing_entries"]:
            report.status = STATUS_MISSING
        elif totals["unavailable_entries"]:
            report.status = STATUS_UNAVAILABLE
        elif totals["empty_entries"]:
            report.status = STATUS_EMPTY
        elif totals["hash_drift_entries"]:
            report.status = STATUS_HASH_DRIFT
        else:
            report.status = STATUS_UNAVAILABLE
    if not report.verified and not report.reason:
        report.reason = _dominant_reason(report)
    if not report.message:
        report.message = summarize(report)
    return report


def summarize(report: CorpusReport) -> str:
    totals = report.totals or _empty_totals()
    return (
        f"{totals['verified_entries']}/{totals['selected_entries']} entries verified"
        f" (available={str(report.available).lower()}"
        f", missing={totals['missing_entries']}"
        f", unavailable={totals['unavailable_entries']}"
        f", empty={totals['empty_entries']}"
        f", hash_drift={totals['hash_drift_entries']}"
        f", error={totals['error_entries']}"
        f", disabled={totals['disabled_entries']}"
        f", skipped={totals['skipped_entries']})"
    )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _load_suite_payload(suite_path: Path, report: CorpusReport) -> list[Any] | None:
    state, message = _probe(suite_path)
    if state == "missing":
        report.reasons = {REASON_SUITE_MISSING: 1}
        report.status = STATUS_UNAVAILABLE
        report.message = f"suite file not found: {suite_path}"
        return None
    if state != "file":
        report.reasons = {REASON_SUITE_NOT_FILE: 1}
        report.status = STATUS_ERROR
        report.message = (
            f"suite path is not a regular file ({state}): {suite_path}"
            + (f" ({message})" if message else "")
        )
        return None
    try:
        raw_text = suite_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.reasons = {REASON_SUITE_UNREADABLE: 1}
        report.status = STATUS_ERROR
        report.message = f"suite file unreadable: {suite_path}: {exc}"
        return None
    report.suite_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        report.reasons = {REASON_SUITE_INVALID_JSON: 1}
        report.status = STATUS_ERROR
        report.message = f"suite file is not valid JSON: {suite_path}: {exc}"
        return None
    if not isinstance(payload, Mapping):
        report.reasons = {REASON_SUITE_INVALID_SHAPE: 1}
        report.status = STATUS_ERROR
        report.message = "suite payload must be a JSON object"
        return None
    entries = payload.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        report.reasons = {REASON_SUITE_INVALID_SHAPE: 1}
        report.status = STATUS_ERROR
        report.message = "suite 'entries' must be a JSON array"
        return None
    if not entries:
        report.reasons = {REASON_SUITE_EMPTY: 1}
        report.status = STATUS_EMPTY
        report.message = "suite declares zero entries"
    return list(entries)


def _fixture_report(
    fixture: Any,
    *,
    index: int,
    baseline_dir: Path,
    fixture_root: Path | None,
    fixture_root_env: str | None,
    compute_hashes: bool,
    digest_cache: dict[str, dict[str, Any] | None] | None = None,
) -> FixtureReport:
    if not isinstance(fixture, Mapping):
        return FixtureReport(
            raw="",
            relative="",
            resolved=None,
            status=STATUS_ERROR,
            reason=REASON_INVALID_PATH,
            message=f"fixture[{index}] must be a JSON object",
            size_bytes=None,
            sha256=None,
            recorded_sha256=None,
            recorded_size_bytes=None,
            hash_verified=False,
            hash_recorded=False,
        )

    recorded_sha, recorded_size = _recorded_digest(fixture)
    resolution = resolve_fixture_entry(
        fixture,
        baseline_dir=baseline_dir,
        fixture_root=fixture_root,
        fixture_root_env=fixture_root_env,
    )
    resolved_text = str(resolution.path)
    if resolution.reason == REASON_FIXTURE_UNSPECIFIED:
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_ERROR,
            reason=REASON_FIXTURE_UNSPECIFIED,
            message=resolution.message,
            size_bytes=None,
            sha256=None,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=bool(recorded_sha or recorded_size is not None),
        )
    if resolution.reason == REASON_INVALID_PATH:
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_ERROR,
            reason=REASON_INVALID_PATH,
            message=resolution.message,
            size_bytes=None,
            sha256=None,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=bool(recorded_sha or recorded_size is not None),
        )

    state, probe_message = _probe(resolution.path)
    if state == "error":
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_ERROR,
            reason=REASON_INVALID_PATH,
            message=f"fixture path unusable: {resolved_text}: {probe_message}",
            size_bytes=None,
            sha256=None,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=bool(recorded_sha or recorded_size is not None),
        )
    if state == "missing":
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_MISSING,
            reason=REASON_FIXTURE_MISSING,
            message=f"fixture not found: {resolved_text}",
            size_bytes=None,
            sha256=None,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=bool(recorded_sha or recorded_size is not None),
        )
    if state != "file":
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_ERROR,
            reason=REASON_FIXTURE_NOT_FILE,
            message=f"fixture is not a regular file ({state}): {resolved_text}",
            size_bytes=None,
            sha256=None,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=bool(recorded_sha or recorded_size is not None),
        )

    size_bytes: int | None = None
    sha256: str | None = None
    if compute_hashes or recorded_sha or recorded_size is not None:
        cache_key = resolved_text
        if digest_cache is not None and cache_key in digest_cache:
            cached = digest_cache[cache_key]
        else:
            cached = fixture_digest(resolution.path)
            if digest_cache is not None:
                digest_cache[cache_key] = cached
        digest = cached
        if digest is None:
            return FixtureReport(
                raw=resolution.raw,
                relative=resolution.relative,
                resolved=resolved_text,
                status=STATUS_ERROR,
                reason=REASON_INVALID_PATH,
                message=f"fixture could not be read: {resolved_text}",
                size_bytes=None,
                sha256=None,
                recorded_sha256=recorded_sha,
                recorded_size_bytes=recorded_size,
                hash_verified=False,
                hash_recorded=bool(recorded_sha or recorded_size is not None),
            )
        sha256 = str(digest["fixture_sha256"])
        size_bytes = int(digest["fixture_size_bytes"])

    hash_recorded = bool(recorded_sha) or recorded_size is not None
    hash_verified = True
    drift_messages: list[str] = []
    if recorded_sha and sha256 and recorded_sha != sha256:
        hash_verified = False
        drift_messages.append(f"sha256 recorded={recorded_sha} actual={sha256}")
    if recorded_size is not None and size_bytes is not None and recorded_size != size_bytes:
        hash_verified = False
        drift_messages.append(f"size recorded={recorded_size} actual={size_bytes}")
    if not hash_verified:
        return FixtureReport(
            raw=resolution.raw,
            relative=resolution.relative,
            resolved=resolved_text,
            status=STATUS_HASH_DRIFT,
            reason=REASON_FIXTURE_HASH_DRIFT,
            message=f"fixture content drifted from baseline: {resolved_text} ({'; '.join(drift_messages)})",
            size_bytes=size_bytes,
            sha256=sha256,
            recorded_sha256=recorded_sha,
            recorded_size_bytes=recorded_size,
            hash_verified=False,
            hash_recorded=hash_recorded,
        )
    return FixtureReport(
        raw=resolution.raw,
        relative=resolution.relative,
        resolved=resolved_text,
        status=STATUS_VERIFIED,
        reason=None,
        message=None,
        size_bytes=size_bytes,
        sha256=sha256,
        recorded_sha256=recorded_sha,
        recorded_size_bytes=recorded_size,
        hash_verified=hash_verified,
        hash_recorded=hash_recorded,
    )


def _entry_report(
    entry: Any,
    *,
    index: int,
    suite_dir: Path,
    default_fixture_root: Path | None,
    include_tags: Sequence[str],
    excluded_tags: Sequence[str],
    compute_hashes: bool,
    digest_cache: dict[str, dict[str, Any] | None] | None = None,
) -> EntryReport:
    if not isinstance(entry, Mapping):
        return EntryReport(
            index=index,
            name=f"entry-{index}",
            status=STATUS_ERROR,
            reason=REASON_ENTRY_NOT_OBJECT,
            message=f"suite entries[{index}] must be a JSON object",
        )

    raw_name = entry.get("name")
    name = str(raw_name).strip() if isinstance(raw_name, str) and raw_name.strip() else f"entry-{index}"
    tags = _normalize_tags(entry.get("tags"))
    disabled = bool(entry.get("disabled")) or bool(entry.get("excluded")) or entry.get("enabled") is False
    report = EntryReport(index=index, name=name, tags=tags, disabled=disabled)
    if disabled:
        report.status = STATUS_DISABLED
        report.reason = REASON_DISABLED_ENTRY
        note = entry.get("_description") or entry.get("disabled_reason") or entry.get("_comment")
        report.message = (
            f"entry '{name}' is disabled in the suite"
            + (f": {note}" if isinstance(note, str) and note.strip() else "")
        )
        return report

    excluded = [tag for tag in excluded_tags if tag in tags and tag not in include_tags]
    if excluded:
        report.skipped = True
        report.status = STATUS_SKIPPED
        report.reason = REASON_EXCLUDED_BY_TAG
        report.message = f"entry '{name}' excluded by tag(s): {', '.join(excluded)}"
        return report

    raw_baseline = entry.get("baseline")
    if raw_baseline is not None and not isinstance(raw_baseline, str):
        report.status = STATUS_ERROR
        report.reason = REASON_INVALID_BASELINE_PATH
        report.message = f"baseline must be a string, got {type(raw_baseline).__name__}"
        return report
    baseline_raw = _expand(raw_baseline)
    report.baseline_raw = baseline_raw
    if not baseline_raw:
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_EMPTY_BASELINE_PATH
        report.message = (
            f"entry '{name}' declares no baseline path"
            " (an empty path would resolve to the suite directory)"
        )
        return report

    baseline_path = resolve_path(suite_dir, baseline_raw)
    report.baseline_path = str(baseline_path)
    state, probe_message = _probe(baseline_path)
    if state == "missing":
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_BASELINE_MISSING
        report.message = f"baseline not found: {baseline_path}"
        return report
    if state != "file":
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_BASELINE_NOT_FILE
        report.message = (
            f"baseline is not a regular file ({state}): {baseline_path}"
            + (f" ({probe_message})" if probe_message else "")
        )
        return report
    try:
        baseline_text = baseline_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_BASELINE_UNREADABLE
        report.message = f"baseline unreadable: {baseline_path}: {exc}"
        return report
    report.baseline_sha256 = hashlib.sha256(baseline_text.encode("utf-8")).hexdigest()
    try:
        baseline_payload = json.loads(baseline_text)
    except ValueError as exc:
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_BASELINE_INVALID_JSON
        report.message = f"baseline is not valid JSON: {baseline_path}: {exc}"
        return report
    if not isinstance(baseline_payload, Mapping):
        report.status = STATUS_UNAVAILABLE
        report.reason = REASON_BASELINE_INVALID_SHAPE
        report.message = f"baseline payload must be a JSON object: {baseline_path}"
        return report

    baseline_root = default_fixture_root
    if baseline_root is None:
        baseline_root = resolve_fixture_root(
            None,
            env_name=str(baseline_payload.get("fixture_root_env") or FIXTURE_ROOT_ENV),
        )
    report.fixture_root = str(baseline_root) if baseline_root is not None else None

    raw_fixtures = baseline_payload.get("fixtures")
    if raw_fixtures is None:
        raw_fixtures = []
    if not isinstance(raw_fixtures, list):
        report.status = STATUS_EMPTY
        report.reason = REASON_EMPTY_FIXTURE_SET
        report.message = f"baseline 'fixtures' must be a JSON array: {baseline_path}"
        return report
    if not raw_fixtures:
        report.status = STATUS_EMPTY
        report.reason = REASON_EMPTY_FIXTURE_SET
        report.message = f"baseline declares zero fixtures: {baseline_path}"
        return report

    fixture_reports = [
        _fixture_report(
            fixture,
            index=fixture_index,
            baseline_dir=baseline_path.parent,
            fixture_root=default_fixture_root,
            fixture_root_env=str(baseline_payload.get("fixture_root_env") or FIXTURE_ROOT_ENV),
            compute_hashes=compute_hashes,
            digest_cache=digest_cache,
        )
        for fixture_index, fixture in enumerate(raw_fixtures)
    ]
    report.fixtures = fixture_reports
    report.fixture_count = len(fixture_reports)
    report.verified_fixture_count = sum(1 for item in fixture_reports if item.verified)
    report.missing_fixture_count = sum(1 for item in fixture_reports if item.status == STATUS_MISSING)
    report.hash_drift_count = sum(1 for item in fixture_reports if item.status == STATUS_HASH_DRIFT)
    report.hash_unrecorded_count = sum(
        1
        for item in fixture_reports
        if item.status in (STATUS_VERIFIED, STATUS_HASH_DRIFT) and not item.hash_recorded
    )

    problems = [item for item in fixture_reports if item.status != STATUS_VERIFIED]
    if not problems:
        report.status = STATUS_VERIFIED
        report.reason = None
    else:
        priority = (
            STATUS_ERROR,
            STATUS_MISSING,
            STATUS_HASH_DRIFT,
        )
        for status in priority:
            matching = [item for item in problems if item.status == status]
            if matching:
                report.status = status
                report.reason = matching[0].reason
                report.message = matching[0].message
                break
    return report


def validate_suite(
    suite: str | Path,
    *,
    fixture_root: str | Path | None = None,
    include_tags: Sequence[str] = (),
    excluded_tags: Sequence[str] = DEFAULT_EXCLUDED_TAGS,
    compute_hashes: bool = True,
) -> CorpusReport:
    """Validate one suite manifest and return a JSON-serialisable report."""
    include = _normalize_tags(list(include_tags))
    excluded = _normalize_tags(list(excluded_tags))
    report = CorpusReport(
        suite=str(suite),
        include_tags=include,
        excluded_tags=excluded,
    )
    resolved_root = (
        resolve_fixture_root(fixture_root)
        if fixture_root is not None and str(fixture_root).strip()
        else None
    )
    report.fixture_root = str(resolved_root) if resolved_root is not None else None
    suite_path = resolve_path(Path.cwd(), suite)
    report.suite = str(suite_path)

    entries = _load_suite_payload(suite_path, report)
    if entries is None:
        return _finalize(report)

    digest_cache: dict[str, dict[str, Any] | None] = {}
    report.entries = [
        _entry_report(
            entry,
            index=index,
            suite_dir=suite_path.parent,
            default_fixture_root=resolved_root,
            include_tags=include,
            excluded_tags=excluded,
            compute_hashes=compute_hashes,
            digest_cache=digest_cache,
        )
        for index, entry in enumerate(entries)
    ]
    return _finalize(report)


# ---------------------------------------------------------------------------
# presentation / CLI
# ---------------------------------------------------------------------------


def compact_dict(report: CorpusReport) -> dict[str, Any]:
    """Summary view of a corpus report (no fixture detail), for embedding."""
    return {
        "schema_version": report.schema_version,
        "suite": report.suite,
        "suite_sha256": report.suite_sha256,
        "fixture_root": report.fixture_root,
        "status": report.status,
        "available": report.available,
        "verified": report.verified,
        "reason": report.reason,
        "message": report.message,
        "reasons": dict(report.reasons),
        "totals": dict(report.totals),
        "entries": [
            {
                "name": entry.name,
                "status": entry.status,
                "reason": entry.reason,
                "baseline": entry.baseline_path,
                "fixture_count": entry.fixture_count,
                "verified_fixture_count": entry.verified_fixture_count,
            }
            for entry in report.entries
        ],
    }


def unverified_entries(report: CorpusReport) -> list[EntryReport]:
    return [entry for entry in report.entries if entry.selected and not entry.verified]


def github_outputs(report: CorpusReport, *, report_path: str | None = None) -> dict[str, str]:
    totals = report.totals or _empty_totals()
    outputs = {
        # Legacy contract kept by every CI detection step.
        "available": str(bool(report.available)).lower(),
        "verified": str(bool(report.verified)).lower(),
        "status": report.status,
        "reason": report.reason or "",
        "message": report.message or "",
        "summary": summarize(report),
        "entry_count": str(totals["entries"]),
        "selected_entries": str(totals["selected_entries"]),
        "verified_entries": str(totals["verified_entries"]),
        "missing_entries": str(totals["missing_entries"]),
        "unavailable_entries": str(totals["unavailable_entries"]),
        "empty_entries": str(totals["empty_entries"]),
        "hash_drift_entries": str(totals["hash_drift_entries"]),
        "error_entries": str(totals["error_entries"]),
        "disabled_entries": str(totals["disabled_entries"]),
        "skipped_entries": str(totals["skipped_entries"]),
        "fixture_count": str(totals["fixtures"]),
        "missing_fixtures": str(totals["missing_fixtures"]),
        "hash_drift_fixtures": str(totals["hash_drift_fixtures"]),
        "hash_unrecorded_fixtures": str(totals["hash_unrecorded_fixtures"]),
    }
    if report_path:
        outputs["report"] = str(report_path)
    return outputs


def write_github_output(path: str | Path, outputs: Mapping[str, str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def format_report(report: CorpusReport, *, verbose: bool = False) -> list[str]:
    totals = report.totals or _empty_totals()
    fixture_root_text = report.fixture_root or f"<{FIXTURE_ROOT_ENV} unset>"
    lines = [
        f"corpus manifest: {report.suite}",
        f"  fixture_root: {fixture_root_text}",
        (
            f"  status: {report.status}"
            f"  available: {str(report.available).lower()}"
            f"  verified: {str(report.verified).lower()}"
            + (f"  reason: {report.reason}" if report.reason else "")
        ),
        f"  {summarize(report)}",
        (
            f"  fixtures: {totals['fixtures']} total"
            f", verified={totals['verified_fixtures']}"
            f", missing={totals['missing_fixtures']}"
            f", hash_drift={totals['hash_drift_fixtures']}"
            f", hash_unrecorded={totals['hash_unrecorded_fixtures']}"
        ),
    ]
    for entry in report.entries:
        label = entry.name
        if entry.disabled:
            lines.append(f"  [disabled] {label}: {entry.message or REASON_DISABLED_ENTRY}")
            continue
        if entry.skipped:
            lines.append(f"  [skipped] {label}: {entry.message or REASON_EXCLUDED_BY_TAG}")
            continue
        if entry.verified:
            lines.append(
                f"  [verified] {label}: {entry.verified_fixture_count}/{entry.fixture_count} fixture(s)"
                f" via {entry.baseline_path}"
            )
            continue
        lines.append(
            f"  [{entry.status}] {label}: {entry.reason} - {entry.message}"
        )
        if verbose:
            for fixture in entry.fixtures:
                if fixture.status == STATUS_VERIFIED:
                    continue
                lines.append(
                    f"      - {fixture.status}: {fixture.reason}: {fixture.resolved}"
                )
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a ParseCore regression suite corpus manifest",
    )
    parser.add_argument("--suite", required=True, help="Suite JSON path, e.g. var/regression/suite.fast.json")
    parser.add_argument(
        "--fixture-root",
        default=None,
        help=f"Fixture root override (default: ${FIXTURE_ROOT_ENV})",
    )
    parser.add_argument(
        "--include-tag",
        action="append",
        dest="include_tags",
        default=[],
        help="Include entries carrying this tag (default: excluded tags are skipped)",
    )
    parser.add_argument(
        "--exclude-tag",
        action="append",
        dest="exclude_tags",
        default=None,
        help=f"Entry tag that marks an entry as not selected (default: {','.join(DEFAULT_EXCLUDED_TAGS)})",
    )
    parser.add_argument("--out", default=None, help="Write the full JSON report to this path")
    parser.add_argument(
        "--github-output",
        default=None,
        help="Append GitHub Actions step outputs here (defaults to $GITHUB_OUTPUT when set)",
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="Exit non-zero when the corpus is not fully verified (release acceptance)",
    )
    parser.add_argument(
        "--no-hash",
        action="store_true",
        help="Skip sha256 computation for fixtures (presence checks only)",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the JSON report")
    parser.add_argument("--verbose", action="store_true", help="Print per-fixture findings")
    parser.add_argument("--json", action="store_true", help="Print the JSON report to stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    excluded_tags = (
        tuple(args.exclude_tags) if args.exclude_tags is not None else DEFAULT_EXCLUDED_TAGS
    )
    report = validate_suite(
        args.suite,
        fixture_root=args.fixture_root,
        include_tags=tuple(args.include_tags or ()),
        excluded_tags=excluded_tags,
        compute_hashes=not args.no_hash,
    )

    report_path: str | None = None
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report_path = str(out_path)

    github_output_path = args.github_output or os.environ.get("GITHUB_OUTPUT") or ""
    if github_output_path:
        write_github_output(
            github_output_path,
            github_outputs(report, report_path=report_path),
        )

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif not args.quiet:
        for line in format_report(report, verbose=args.verbose):
            print(line)

    if args.require_verified and not report.verified:
        print(
            f"corpus manifest NOT VERIFIED: {report.reason}: {report.message}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
