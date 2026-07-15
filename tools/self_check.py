"""ParseCore self-check gate.

Runs the local reliability and runtime checks that now replace the old
jobcard dual-run loop as the default quality gate.

Checks:
1. Unit tests
2. Runtime describe smoke
3. Payload contract validation
4. Regression baseline suite (optional, enabled by default)
5. Local provider comparison suite (optional, explicitly enabled)

The script prints a JSON summary and also writes it to ``var/self-check``.

Exit codes:
0 -> all requested checks passed
1 -> at least one required check failed
2 -> no required check failed, but a check degraded or timed out
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
# When executed as ``python tools/self_check.py``, Python puts ``tools/``
# (rather than the repository root) on sys.path.  Provider-suite preflight
# imports sibling tools modules, so make the script entry point work without
# requiring callers to set PYTHONPATH manually.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parsecore.pdf_parts import detect_pdf_page_count


FAST_PROFILE = "fast"
FULL_PROFILE = "full"
PERF_PROFILE = "perf"
PROFILE_ALIASES = {
    "slow": FULL_PROFILE,
}
PERF_COMPARE_METRICS = (
    "elapsed_s",
    "ocr_total_s",
    "call_s",
    "provider_s",
    "rec_s",
    "max_page_ocr_s",
)
PROFILE_TIMEOUTS = {
    FAST_PROFILE: 900,
    FULL_PROFILE: 4200,
    PERF_PROFILE: 4200,
}
PROFILE_INCLUDE_TAGS = {
    FAST_PROFILE: (),
    FULL_PROFILE: (),
    PERF_PROFILE: (),
}
FAST_PROVIDER_PAGE_BUDGET_KEY = "fast_page_budget"
TIMEOUT_TREE_CLEANUP_GRACE_SECONDS = 10
TIMEOUT_TREE_KILL_SECONDS = 15


@dataclass
class CheckResult:
    name: str
    status: str
    exit_code: int | None
    elapsed_s: float
    summary: str
    details: dict[str, Any]
    tail: list[str]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ParseCore self-check gate")
    parser.add_argument(
        "--profile",
        choices=(FAST_PROFILE, FULL_PROFILE, PERF_PROFILE, "slow"),
        default=FAST_PROFILE,
        help="fast runs the daily gate, full/slow runs the extended gate, perf runs the dedicated heavy-sample tracking lane",
    )
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument(
        "--suite",
        default=None,
        help="override the regression suite path; defaults depend on --profile",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="override the output JSON path; defaults depend on --profile",
    )
    parser.add_argument(
        "--compare-report",
        default=None,
        help="optional previous self-check JSON report used to compute perf deltas",
    )
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument(
        "--skip-payload-contracts",
        action="store_true",
        help="skip the payload contract validation check",
    )
    parser.add_argument(
        "--regression-timeout-seconds",
        type=int,
        default=None,
        help="override the regression-suite timeout; defaults depend on --profile",
    )
    parser.add_argument(
        "--provider-suite",
        default=None,
        help="optional provider comparison suite path for the local-provider release gate",
    )
    parser.add_argument(
        "--provider-fixture-root",
        default=None,
        help="optional fixture root passed to the provider comparison suite",
    )
    parser.add_argument(
        "--provider-profile",
        default="default",
        help="provider routing profile passed to the provider comparison suite",
    )
    parser.add_argument(
        "--skip-provider-comparison",
        action="store_true",
        help="skip the provider comparison suite even when --provider-suite is set",
    )
    parser.add_argument(
        "--provider-comparison-timeout-seconds",
        type=int,
        default=None,
        help="override the provider-comparison timeout; defaults depend on --profile",
    )
    parser.add_argument(
        "--reuse-parser-instances",
        action="store_true",
        help="reuse one parser instance per provider in the candidate provider suite for warm-state measurement",
    )
    parser.add_argument(
        "--large-pdf-benchmark",
        default=None,
        help="optional large PDF benchmark config path for the large-sample stress gate",
    )
    return parser


def _normalize_profile(profile: str) -> str:
    return PROFILE_ALIASES.get(profile, profile)


def _default_suite_for_profile(profile: str) -> str:
    normalized_profile = _normalize_profile(profile)
    if normalized_profile == PERF_PROFILE:
        return str(ROOT / "var" / "regression" / "suite.perf.json")
    if normalized_profile == FULL_PROFILE:
        return str(ROOT / "var" / "regression" / "suite.full.json")
    return str(ROOT / "var" / "regression" / "suite.fast.json")


def _default_out_for_profile(profile: str) -> str:
    normalized_profile = _normalize_profile(profile)
    if normalized_profile == PERF_PROFILE:
        return str(ROOT / "var" / "self-check" / "latest.perf.json")
    if normalized_profile == FULL_PROFILE:
        return str(ROOT / "var" / "self-check" / "latest.full.json")
    return str(ROOT / "var" / "self-check" / "latest.json")


def _provider_comparison_artifact_paths(
    *,
    profile: str,
    out_path: str | Path,
) -> tuple[Path, Path]:
    normalized_profile = _normalize_profile(profile)
    suffix = f".{normalized_profile}" if normalized_profile else ""
    parent = Path(out_path).parent
    return (
        parent / f"provider-comparison{suffix}.json",
        parent / f"provider-comparison{suffix}.md",
    )


def _default_provider_suite_for_profile(profile: str) -> str | None:
    normalized_profile = _normalize_profile(profile)
    if normalized_profile == FAST_PROFILE:
        candidate = ROOT / "var" / "regression" / "provider-suite.fast.json"
    elif normalized_profile == PERF_PROFILE:
        candidate = ROOT / "var" / "regression" / "provider-suite.perf.json"
    elif normalized_profile == FULL_PROFILE:
        candidate = ROOT / "var" / "regression" / "provider-suite.full.json"
    else:
        return None
    if candidate.exists():
        return str(candidate)
    return None


def _resolve_regression_profile(
    profile: str,
    explicit_timeout_seconds: int | None,
) -> tuple[str, tuple[str, ...], int]:
    normalized_profile = _normalize_profile(profile)
    include_tags = PROFILE_INCLUDE_TAGS.get(normalized_profile, ())
    timeout_seconds = (
        explicit_timeout_seconds
        if explicit_timeout_seconds is not None
        else PROFILE_TIMEOUTS.get(normalized_profile, PROFILE_TIMEOUTS[FAST_PROFILE])
    )
    return normalized_profile, include_tags, timeout_seconds


def _build_regression_suite_args(suite: str, include_tags: tuple[str, ...]) -> list[str]:
    args = [sys.executable, "tools/regression_baseline.py", "check-suite", "--suite", suite]
    for tag in include_tags:
        args.extend(["--include-tag", tag])
    return args


def _build_provider_comparison_args(
    *,
    config: str,
    suite: str,
    fixture_root: str | None = None,
    profile: str = "default",
    reuse_parser_instances: bool = False,
) -> list[str]:
    args = [
        sys.executable,
        "tools/provider_comparison_report.py",
        "--config",
        config,
        "--suite",
        suite,
        "--profile",
        profile,
        "--progress",
    ]
    if fixture_root:
        args.extend(["--fixture-root", fixture_root])
    if reuse_parser_instances:
        args.append("--reuse-parser-instances")
    return args


def _default_provider_suite_preflight(
    suite: str,
    *,
    fixture_root: str | None,
    profile: str = FAST_PROFILE,
) -> tuple[bool, dict[str, Any]]:
    from tools import provider_comparison_report

    try:
        resolved_fixture_root = provider_comparison_report._fixture_root_path(fixture_root)
        sample_specs, _gate_policy = provider_comparison_report._load_suite_samples(
            suite,
            fixture_root=resolved_fixture_root,
        )
    except Exception as exc:
        return False, {
            "reason": "suite_resolution_failed",
            "message": f"provider suite resolution failed: {exc}",
        }
    if not sample_specs:
        return False, {
            "reason": "empty_suite",
            "message": "provider suite resolved to zero samples",
        }
    missing = [str(spec.path) for spec in sample_specs if not Path(spec.path).exists()]
    if missing:
        return False, {
            "reason": "missing_fixtures",
            "message": f"provider suite skipped because {len(missing)} fixture(s) are unavailable",
            "missing_fixtures": missing,
            "resolved_fixture_root": (
                str(resolved_fixture_root) if resolved_fixture_root is not None else None
            ),
        }
    details: dict[str, Any] = {
        "sample_count": len(sample_specs),
        "resolved_fixture_root": (
            str(resolved_fixture_root) if resolved_fixture_root is not None else None
        ),
    }
    if _normalize_profile(profile) != FAST_PROFILE:
        return True, details

    try:
        suite_payload = json.loads(Path(suite).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, {
            **details,
            "reason": "fast_page_budget_unreadable",
            "message": f"fast provider suite page budget could not be read: {exc}",
        }
    raw_budget = suite_payload.get(FAST_PROVIDER_PAGE_BUDGET_KEY) if isinstance(suite_payload, Mapping) else None
    if not isinstance(raw_budget, Mapping):
        return False, {
            **details,
            "reason": "fast_page_budget_missing",
            "message": f"fast provider suite must declare {FAST_PROVIDER_PAGE_BUDGET_KEY}",
        }

    budget: dict[str, int] = {}
    for field in ("max_pages_per_sample", "max_total_pages", "large_pdf_min_page_count"):
        value = raw_budget.get(field)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        if isinstance(value, bool) or parsed <= 0:
            return False, {
                **details,
                "reason": "fast_page_budget_invalid",
                "message": f"fast provider suite {FAST_PROVIDER_PAGE_BUDGET_KEY}.{field} must be a positive integer",
            }
        budget[field] = parsed

    source_page_counts: dict[Path, int] = {}
    pdf_samples: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    total_selected_pages = 0
    for sample in sample_specs:
        source_path = Path(sample.path)
        if source_path.suffix.lower() != ".pdf":
            continue
        try:
            source_page_count = source_page_counts.get(source_path)
            if source_page_count is None:
                source_page_count = detect_pdf_page_count(str(source_path))
                source_page_counts[source_path] = source_page_count
        except Exception as exc:
            return False, {
                **details,
                "reason": "fast_page_count_unavailable",
                "message": f"fast provider suite could not inspect PDF page count for {source_path}: {exc}",
                "page_budget": budget,
            }

        page_range = (
            (int(sample.page_start), int(sample.page_end))
            if sample.page_start is not None and sample.page_end is not None
            else None
        )
        selected_page_count = (
            page_range[1] - page_range[0] + 1 if page_range is not None else source_page_count
        )
        sample_details = {
            "name": sample.name or source_path.name,
            "document": str(source_path),
            "source_page_count": source_page_count,
            "page_range": (
                {"start": page_range[0], "end": page_range[1]} if page_range is not None else None
            ),
            "selected_page_count": selected_page_count,
        }
        pdf_samples.append(sample_details)
        total_selected_pages += selected_page_count

        if source_page_count >= budget["large_pdf_min_page_count"] and page_range is None:
            violations.append({"code": "large_pdf_requires_page_range", **sample_details})
        if page_range is not None and page_range[1] > source_page_count:
            violations.append({"code": "page_range_exceeds_document", **sample_details})
        if selected_page_count > budget["max_pages_per_sample"]:
            violations.append({"code": "sample_page_budget_exceeded", **sample_details})

    page_budget_details = {
        **budget,
        "selected_pdf_pages": total_selected_pages,
        "pdf_sample_count": len(pdf_samples),
        "samples": pdf_samples,
    }
    details["page_budget"] = page_budget_details
    if total_selected_pages > budget["max_total_pages"]:
        violations.append(
            {
                "code": "total_page_budget_exceeded",
                "selected_pdf_pages": total_selected_pages,
                "max_total_pages": budget["max_total_pages"],
            }
        )
    if violations:
        violation_codes = ", ".join(str(item["code"]) for item in violations)
        return False, {
            **details,
            "reason": "fast_page_budget_violation",
            "message": f"fast provider suite page budget rejected: {violation_codes}",
            "violations": violations,
        }
    return True, details


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    source_path = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_path if not existing else os.pathsep.join((source_path, existing))
    return env


def _tail_lines(text: str, *, limit: int = 20) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _write_stdout(text: str) -> None:
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(text.encode("utf-8", errors="replace"))
        stdout_buffer.write(b"\n")
        stdout_buffer.flush()
        return
    print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combined_subprocess_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return "\n".join(
        part for part in (_subprocess_text(stdout), _subprocess_text(stderr)) if part
    ).strip()


def _subprocess_start_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": ROOT,
        "env": _command_env(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        # taskkill /T uses the direct child PID, while a separate process group
        # prevents a timeout signal from joining the parent self-check process.
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _fallback_kill_process(process: Any, details: dict[str, Any]) -> None:
    try:
        process.kill()
        details["fallback_root_kill"] = "sent"
    except (OSError, AttributeError) as exc:
        details["fallback_root_kill"] = f"failed: {exc}"


def _terminate_timed_out_process_tree(process: Any) -> dict[str, Any]:
    """Terminate a timed-out child and its descendants without touching the parent gate."""
    pid = int(getattr(process, "pid", 0) or 0)
    details: dict[str, Any] = {"attempted": True, "pid": pid, "platform": os.name}
    if pid <= 0:
        details.update({"strategy": "unavailable", "succeeded": False})
        _fallback_kill_process(process, details)
        return details

    if os.name == "nt":
        details["strategy"] = "taskkill_tree"
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_TREE_KILL_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            details["taskkill_error"] = str(exc)
            details["succeeded"] = False
            _fallback_kill_process(process, details)
            return details
        details["taskkill_exit_code"] = completed.returncode
        details["taskkill_stdout_tail"] = _tail_lines(_subprocess_text(completed.stdout), limit=5)
        details["taskkill_stderr_tail"] = _tail_lines(_subprocess_text(completed.stderr), limit=5)
        details["succeeded"] = completed.returncode == 0
        if not details["succeeded"]:
            _fallback_kill_process(process, details)
        return details

    details["strategy"] = "posix_process_group"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        details["succeeded"] = True
        return details
    except ProcessLookupError:
        details.update({"succeeded": True, "already_exited": True})
        return details
    except (OSError, ValueError) as exc:
        details["process_group_error"] = str(exc)
        details["succeeded"] = False
        _fallback_kill_process(process, details)
        return details


def _timeout_exception_output(error: subprocess.TimeoutExpired) -> tuple[str, str]:
    stdout = getattr(error, "stdout", None)
    if stdout is None:
        stdout = getattr(error, "output", None)
    return _subprocess_text(stdout), _subprocess_text(getattr(error, "stderr", None))


def _collect_timed_out_process_output(
    process: Any,
    timeout_error: subprocess.TimeoutExpired,
) -> tuple[str, str, dict[str, Any]]:
    """Drain output after tree cleanup without allowing inherited pipes to hang the gate."""
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT_TREE_CLEANUP_GRACE_SECONDS)
        return _subprocess_text(stdout), _subprocess_text(stderr), {"drain_status": "completed"}
    except subprocess.TimeoutExpired as drain_error:
        details: dict[str, Any] = {"drain_status": "timed_out"}
        _fallback_kill_process(process, details)
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_TREE_CLEANUP_GRACE_SECONDS)
            details["drain_status"] = "completed_after_root_kill"
            return _subprocess_text(stdout), _subprocess_text(stderr), details
        except subprocess.TimeoutExpired as final_error:
            details["drain_status"] = "still_open_after_root_kill"
            stdout, stderr = _timeout_exception_output(final_error)
            if not stdout and not stderr:
                stdout, stderr = _timeout_exception_output(drain_error)
            if not stdout and not stderr:
                stdout, stderr = _timeout_exception_output(timeout_error)
            return stdout, stderr, details


def _run_subprocess(
    name: str,
    args: list[str],
    *,
    timeout_seconds: int | None = None,
) -> tuple[CheckResult, str]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(args, **_subprocess_start_kwargs())
    except OSError as exc:
        result = CheckResult(
            name=name,
            status="failed",
            exit_code=1,
            elapsed_s=round(time.monotonic() - started, 3),
            summary=f"failed to start command: {exc}",
            details={"start_error": str(exc)},
            tail=[],
        )
        return result, ""

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        combined = _combined_subprocess_output(stdout, stderr)
        return_code = process.returncode if isinstance(process.returncode, int) else 1
        result = CheckResult(
            name=name,
            status="passed" if return_code == 0 else "failed",
            exit_code=return_code,
            elapsed_s=round(time.monotonic() - started, 3),
            summary="command completed",
            details={},
            tail=_tail_lines(combined),
        )
        return result, combined
    except subprocess.TimeoutExpired as exc:
        cleanup = _terminate_timed_out_process_tree(process)
        stdout, stderr, drain_details = _collect_timed_out_process_output(process, exc)
        output = _combined_subprocess_output(stdout, stderr)
        result = CheckResult(
            name=name,
            status="timeout",
            exit_code=None,
            elapsed_s=round(time.monotonic() - started, 3),
            summary=f"timed out after {timeout_seconds}s",
            details={
                "timeout_seconds": timeout_seconds,
                "timeout_cleanup": cleanup,
                "timeout_output_drain": drain_details,
            },
            tail=_tail_lines(output),
        )
        return result, output


def _run_unit_tests() -> CheckResult:
    result, output = _run_subprocess(
        "unit_tests",
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
    )
    ran_match = re.search(r"Ran\s+(\d+)\s+tests\s+in\s+([0-9.]+)s", output)
    skipped_match = re.search(r"skipped=(\d+)", output)
    if ran_match:
        result.details["tests_ran"] = int(ran_match.group(1))
        result.details["reported_elapsed_s"] = float(ran_match.group(2))
    if skipped_match:
        result.details["skipped"] = int(skipped_match.group(1))
    if result.status == "passed":
        tests_ran = result.details.get("tests_ran", "?")
        skipped = result.details.get("skipped", 0)
        result.summary = f"{tests_ran} tests passed, skipped={skipped}"
    else:
        result.summary = result.tail[-1] if result.tail else "unit tests failed"
    return result


def _run_runtime_describe(config: str) -> CheckResult:
    result, output = _run_subprocess(
        "runtime_describe",
        [sys.executable, "-m", "parsecore.cli", "describe", "--config", config],
    )
    if result.status != "passed":
        result.summary = result.tail[-1] if result.tail else "runtime describe failed"
        return result
    payload = json.loads(output)
    result.details = {
        "project": payload.get("project"),
        "index_mode": payload.get("index_mode"),
        "execution_mode": payload.get("runtime", {}).get("execution_mode"),
        "parsers": payload.get("parsers", []),
    }
    result.summary = (
        f"project={result.details['project']} index_mode={result.details['index_mode']} "
        f"execution_mode={result.details['execution_mode']}"
    )
    return result


def _run_payload_contract_check() -> CheckResult:
    result, output = _run_subprocess(
        "payload_contracts",
        [sys.executable, "-m", "parsecore.cli", "payload-contract-check"],
    )
    if result.status != "passed":
        result.summary = result.tail[-1] if result.tail else "payload contract check failed"
        return result
    payload = _parse_json_output(output)
    if payload is None:
        result.status = "failed"
        result.exit_code = result.exit_code if result.exit_code is not None else 1
        result.details["parse_error"] = "payload contract check output was not JSON"
        result.summary = result.tail[-1] if result.tail else "payload contract check output was not JSON"
        return result
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    result.details = {
        "status": payload.get("status"),
        "registry_schema_version": payload.get("registry_schema_version"),
        "summary": summary,
        "schemas": payload.get("schemas"),
        "payloads": payload.get("payloads"),
    }
    result.summary = (
        f"schemas={summary.get('schema_count', 0)}"
        f" payloads={summary.get('payload_count', 0)}"
        f" failed_schemas={summary.get('failed_schema_count', 0)}"
        f" failed_payloads={summary.get('failed_payload_count', 0)}"
    )
    return result


def _last_suite_label(lines: list[str]) -> str | None:
    for line in reversed(lines):
        if line.startswith("[suite] "):
            return line.replace("[suite] ", "", 1)
    return None


def _extract_structure_metrics(lines: list[str]) -> dict[str, float]:
    metrics: dict[str, list[float]] = {
        "toc_recog": [],
        "chapter_cov": [],
        "noise_ratio": [],
        "heading_bind": [],
        "evidence_bind": [],
    }
    for line in lines:
        if not line.startswith("[check] ") or "(baseline" not in line:
            continue
        for key in metrics:
            match = re.search(rf"{key}=([0-9.]+)", line)
            if match:
                metrics[key].append(float(match.group(1)))
    return {
        key: round(sum(values) / len(values), 4)
        for key, values in metrics.items()
        if values
    }


def _coerce_number(text: str) -> int | float:
    if any(ch in text for ch in (".", "e", "E")):
        return float(text)
    return int(text)


def _metric_delta(current: int | float, baseline: int | float) -> int | float:
    if isinstance(current, int) and isinstance(baseline, int):
        return current - baseline
    return round(float(current) - float(baseline), 4)


def _split_label_and_metrics(body: str) -> tuple[str, str]:
    metric_match = re.search(r"\s[A-Za-z_][A-Za-z0-9_]*=", body)
    if not metric_match:
        return body.strip(), ""
    return body[: metric_match.start()].strip(), body[metric_match.start() + 1 :].strip()


def _extract_metric_pairs(text: str) -> dict[str, dict[str, int | float]]:
    metrics: dict[str, dict[str, int | float]] = {}
    for match in re.finditer(
        r"([A-Za-z_][A-Za-z0-9_]*)=([-+]?[0-9]+(?:\.[0-9]+)?)\s+\(baseline\s+([-+]?[0-9]+(?:\.[0-9]+)?)\)",
        text,
    ):
        key = match.group(1)
        current = _coerce_number(match.group(2))
        baseline = _coerce_number(match.group(3))
        metrics[key] = {
            "current": current,
            "baseline": baseline,
            "delta": _metric_delta(current, baseline),
        }
    return metrics


def _extract_perf_samples(lines: list[str]) -> list[dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    for line in lines:
        finished = re.match(
            r"^\[run\]\s+(?P<name>.+): parsing finished in (?P<elapsed>[0-9]+(?:\.[0-9]+)?)s$",
            line,
        )
        if finished:
            name = finished.group("name").strip()
            sample = samples.setdefault(name, {"name": name, "metrics": {}, "ocr_metrics": {}})
            sample["elapsed_s"] = round(float(finished.group("elapsed")), 3)
            continue

        if line.startswith("[check][ocr] "):
            name, metrics_blob = _split_label_and_metrics(line[len("[check][ocr] ") :].strip())
            if metrics_blob:
                sample = samples.setdefault(name, {"name": name, "metrics": {}, "ocr_metrics": {}})
                sample["ocr_metrics"] = _extract_metric_pairs(metrics_blob)
            continue

        if line.startswith("[check] ") and "(baseline" in line:
            name, metrics_blob = _split_label_and_metrics(line[len("[check] ") :].strip())
            if metrics_blob:
                sample = samples.setdefault(name, {"name": name, "metrics": {}, "ocr_metrics": {}})
                sample["metrics"] = _extract_metric_pairs(metrics_blob)

    ordered: list[dict[str, Any]] = []
    for name in sorted(samples):
        sample = samples[name]
        if "elapsed_s" not in sample:
            sample["elapsed_s"] = None
        ordered.append(sample)
    return ordered


def _build_perf_overview(samples: list[dict[str, Any]]) -> dict[str, Any]:
    overview: dict[str, Any] = {"sample_count": len(samples)}
    if not samples:
        return overview

    samples_with_elapsed = [sample for sample in samples if sample.get("elapsed_s") is not None]
    if samples_with_elapsed:
        slowest = max(samples_with_elapsed, key=lambda item: float(item.get("elapsed_s") or 0.0))
        overview["slowest_sample"] = {
            "name": slowest["name"],
            "elapsed_s": slowest["elapsed_s"],
        }

    def _best_ocr_metric(metric_name: str, label: str) -> None:
        candidates = [
            sample
            for sample in samples
            if metric_name in (sample.get("ocr_metrics") or {})
        ]
        if not candidates:
            return
        hottest = max(
            candidates,
            key=lambda item: float(item["ocr_metrics"][metric_name]["current"]),
        )
        overview[label] = {
            "name": hottest["name"],
            metric_name: hottest["ocr_metrics"][metric_name]["current"],
        }

    _best_ocr_metric("ocr_total_s", "highest_ocr_total_sample")
    _best_ocr_metric("rec_s", "highest_rec_sample")
    return overview


def _perf_samples_from_report(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tracking = payload.get("perf_tracking")
    if isinstance(tracking, dict):
        samples = tracking.get("samples")
        if isinstance(samples, list):
            return [sample for sample in samples if isinstance(sample, dict)]
    for item in payload.get("checks", []) or []:
        if not isinstance(item, dict) or item.get("name") != "regression_suite":
            continue
        details = item.get("details") or {}
        samples = details.get("perf_samples")
        if isinstance(samples, list):
            return [sample for sample in samples if isinstance(sample, dict)]
    return []


def _perf_metric_value(sample: dict[str, Any], metric_name: str) -> int | float | None:
    if metric_name == "elapsed_s":
        value = sample.get("elapsed_s")
        if isinstance(value, (int, float)):
            return value
        return None
    ocr_metric = (sample.get("ocr_metrics") or {}).get(metric_name)
    if isinstance(ocr_metric, dict):
        value = ocr_metric.get("current")
        if isinstance(value, (int, float)):
            return value
    base_metric = (sample.get("metrics") or {}).get(metric_name)
    if isinstance(base_metric, dict):
        value = base_metric.get("current")
        if isinstance(value, (int, float)):
            return value
    return None


def _compare_perf_samples(
    current_samples: list[dict[str, Any]],
    previous_payload: dict[str, Any],
    *,
    compare_report: str,
) -> dict[str, Any]:
    previous_samples = _perf_samples_from_report(previous_payload)
    previous_index = {
        str(sample.get("name") or ""): sample
        for sample in previous_samples
        if isinstance(sample, dict) and str(sample.get("name") or "").strip()
    }
    comparisons: list[dict[str, Any]] = []
    for current in current_samples:
        name = str(current.get("name") or "").strip()
        if not name or name not in previous_index:
            continue
        previous = previous_index[name]
        metrics: dict[str, dict[str, int | float]] = {}
        for metric_name in PERF_COMPARE_METRICS:
            current_value = _perf_metric_value(current, metric_name)
            previous_value = _perf_metric_value(previous, metric_name)
            if current_value is None or previous_value is None:
                continue
            metrics[metric_name] = {
                "current": current_value,
                "previous": previous_value,
                "delta": _metric_delta(current_value, previous_value),
            }
        if metrics:
            comparisons.append({"name": name, "metrics": metrics})

    return {
        "available": bool(comparisons),
        "compare_report": compare_report,
        "sample_count": len(comparisons),
        "samples": comparisons,
    }


def _run_regression_suite(
    suite: str,
    timeout_seconds: int,
    *,
    include_tags: tuple[str, ...] = (),
    profile: str = FAST_PROFILE,
) -> CheckResult:
    result, output = _run_subprocess(
        "regression_suite",
        _build_regression_suite_args(suite, include_tags),
        timeout_seconds=timeout_seconds,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    ok_count = sum(1 for line in lines if line == "[check] OK: all metrics within budget")
    skip_count = sum(1 for line in lines if line.startswith("[suite] SKIP"))
    last_suite = _last_suite_label(lines)
    structure_metrics = _extract_structure_metrics(lines)
    result.details.update(
        {
            "ok_count": ok_count,
            "skip_count": skip_count,
            "last_suite": last_suite,
            "structure_metrics": structure_metrics,
            "profile": profile,
            "include_tags": list(include_tags),
        }
    )
    perf_samples: list[dict[str, Any]] = []
    if profile == PERF_PROFILE:
        perf_samples = _extract_perf_samples(lines)
        result.details["perf_samples"] = perf_samples
        result.details["perf_overview"] = _build_perf_overview(perf_samples)
    if result.status == "passed":
        result.summary = f"profile={profile} ok={ok_count} skipped={skip_count}"
    elif result.status == "timeout":
        label = last_suite or "unknown suite"
        result.summary = f"profile={profile} timed out on {label} after ok={ok_count} skipped={skip_count}"
    else:
        label = last_suite or "unknown suite"
        result.summary = f"profile={profile} failed on {label} after ok={ok_count} skipped={skip_count}"
    if structure_metrics:
        result.summary += (
            f" toc={structure_metrics.get('toc_recog', 0.0):.4f}"
            f" chapter={structure_metrics.get('chapter_cov', 0.0):.4f}"
            f" noise={structure_metrics.get('noise_ratio', 0.0):.4f}"
            f" bind={structure_metrics.get('heading_bind', 0.0):.4f}"
            f" evidence={structure_metrics.get('evidence_bind', 0.0):.4f}"
        )
    if profile == PERF_PROFILE and perf_samples:
        overview = result.details.get("perf_overview") or {}
        slowest = overview.get("slowest_sample") or {}
        hottest = overview.get("highest_ocr_total_sample") or {}
        if slowest:
            result.summary += (
                f" slowest={slowest.get('name')}:{float(slowest.get('elapsed_s', 0.0)):.1f}s"
            )
        if hottest:
            result.summary += (
                f" ocr_total={hottest.get('name')}:{float(hottest.get('ocr_total_s', 0.0)):.3f}s"
            )
    return result


def _parse_json_output(output: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(output)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = output.find("{")
        end = output.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(output[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None


def _run_provider_comparison_suite(
    *,
    config: str,
    suite: str,
    fixture_root: str | None,
    profile: str,
    timeout_seconds: int,
    reuse_parser_instances: bool = False,
) -> CheckResult:
    result, output = _run_subprocess(
        "provider_comparison_suite",
        _build_provider_comparison_args(
            config=config,
            suite=suite,
            fixture_root=fixture_root,
            profile=profile,
            reuse_parser_instances=reuse_parser_instances,
        ),
        timeout_seconds=timeout_seconds,
    )
    result.details.update(
        {
            "name": "provider_comparison_suite",
            "suite": suite,
            "fixture_root": fixture_root,
            "profile": profile,
            "reuse_parser_instances": bool(reuse_parser_instances),
        }
    )
    payload = _parse_json_output(output)
    if payload is None:
        if result.status == "passed":
            result.status = "failed"
            result.exit_code = result.exit_code if result.exit_code is not None else 1
        result.details["parse_error"] = "provider comparison output was not JSON"
        result.summary = result.tail[-1] if result.tail else "provider comparison output was not JSON"
        return result

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    gate_summary = payload.get("gate_summary") if isinstance(payload.get("gate_summary"), dict) else {}
    provider_identity_summary = (
        payload.get("provider_identity_summary")
        if isinstance(payload.get("provider_identity_summary"), dict)
        else {}
    )
    provider_admission_summary = (
        payload.get("provider_admission_summary")
        if isinstance(payload.get("provider_admission_summary"), dict)
        else {}
    )
    result.details.update(
        {
            "summary": summary,
            "gate_summary": gate_summary,
            "provider_identity_summary": provider_identity_summary,
            "provider_admission_summary": provider_admission_summary,
            "report_schema_version": payload.get("schema_version"),
            "resolved_suite": payload.get("suite"),
            "resolved_fixture_root": payload.get("fixture_root"),
            "report_payload": payload,
            "sample_count": summary.get("sample_count", 0),
        }
    )
    gate = str(gate_summary.get("gate") or "unknown")
    identity_drift = int(provider_identity_summary.get("providers_with_multiple_provider_versions") or 0) + int(
        provider_identity_summary.get("providers_with_multiple_adapter_versions") or 0
    )
    admission_overview = (
        provider_admission_summary.get("summary")
        if isinstance(provider_admission_summary.get("summary"), dict)
        else {}
    )
    result.summary = (
        f"profile={profile} gate={gate}"
        f" samples={summary.get('sample_count', 0)}"
        f" completed={summary.get('completed_provider_runs', 0)}"
        f" failed={summary.get('failed_provider_runs', 0)}"
        f" skipped={summary.get('skipped_provider_runs', 0)}"
        f" quality_warn={gate_summary.get('provider_quality_warning_runs', 0)}"
        f" read_order_warn={gate_summary.get('provider_reading_order_warning_runs', 0)}"
        f" route_mismatch={gate_summary.get('samples_best_provider_differs_from_route_primary', 0)}"
        f" identity_drift={identity_drift}"
        f" admission_ready={admission_overview.get('route_ready_count', 0)}"
        f" admission_update={admission_overview.get('providers_requiring_config_update', 0)}"
        f" parser_lifecycle={((payload.get('measurement') or {}).get('parser_lifecycle') or 'unknown')}"
    )
    return result


def _skipped_provider_comparison_suite(
    *,
    suite: str,
    fixture_root: str | None,
    profile: str,
    reason: str,
    details: dict[str, Any] | None = None,
    status: str = "skipped",
) -> CheckResult:
    payload = {
        "suite": suite,
        "fixture_root": fixture_root,
        "profile": profile,
    }
    if details:
        payload.update(details)
    return CheckResult(
        name="provider_comparison_suite",
        status=status,
        exit_code=1 if status == "failed" else None,
        elapsed_s=0.0,
        summary=reason,
        details=payload,
        tail=[],
    )


def _persist_provider_comparison_artifacts(
    *,
    profile: str,
    out_path: str | Path,
    report_payload: Mapping[str, Any],
) -> dict[str, str]:
    from tools import provider_comparison_report

    json_path, markdown_path = _provider_comparison_artifact_paths(profile=profile, out_path=out_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(provider_comparison_report.render_markdown(dict(report_payload)), encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def _run_large_pdf_benchmark(config_path: str, *, config: str) -> CheckResult:
    """Run large PDF benchmark gate from a config JSON file."""
    from tools import large_pdf_stress

    started = time.monotonic()
    try:
        benchmark_config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="large_pdf_benchmark",
            status="failed",
            exit_code=1,
            elapsed_s=round(time.monotonic() - started, 3),
            summary=f"failed to load benchmark config: {exc}",
            details={"config_path": config_path},
            tail=[],
        )

    pdf_path = benchmark_config.get("pdf_path")
    pdf_exists = pdf_path and Path(pdf_path).exists()
    generate_pages = benchmark_config.get("total_pages", 17101) if not pdf_exists else 0

    try:
        report = large_pdf_stress.build_report(
            config=config,
            pdf=pdf_path if pdf_exists else None,
            generate_pages=max(1, int(generate_pages)),
            target_pages_per_part=benchmark_config.get("target_pages_per_part", 50),
            max_active_parts_per_doc=benchmark_config.get("max_active_parts_per_doc"),
            profile=benchmark_config.get("profile", "large-pdf"),
            execute_parts=benchmark_config.get("execute_parts", False),
            materialize_part_files=benchmark_config.get("materialize_part_files", True),
            parallel_parts=max(1, int(benchmark_config.get("parallel_parts", 1) or 1)),
        )
    except Exception as exc:
        return CheckResult(
            name="large_pdf_benchmark",
            status="failed",
            exit_code=1,
            elapsed_s=round(time.monotonic() - started, 3),
            summary=f"benchmark execution failed: {exc}",
            details={"config_path": config_path},
            tail=[],
        )

    gate = large_pdf_stress.evaluate_gate(report, benchmark_config)
    summary = report.get("summary") or {}
    status = "passed" if gate["passed"] else "failed"
    passed_checks = sum(1 for c in gate["checks"] if c["passed"])
    total_checks = len(gate["checks"])
    return CheckResult(
        name="large_pdf_benchmark",
        status=status,
        exit_code=0 if gate["passed"] else 1,
        elapsed_s=round(time.monotonic() - started, 3),
        summary=(
            f"gate={'passed' if gate['passed'] else 'failed'} "
            f"checks={passed_checks}/{total_checks} "
            f"pages={summary.get('total_pages', 0)} "
            f"parts={summary.get('planned_parts', 0)} "
            f"plan_elapsed_s={summary.get('plan_elapsed_s', 0)}"
        ),
        details={
            "config_path": config_path,
            "gate": gate,
            "report_status": report.get("status"),
            "pdf_path": report.get("pdf"),
            "generated_pdf": report.get("generated_pdf", False),
            "plan_elapsed_s": summary.get("plan_elapsed_s"),
            "planned_parts": summary.get("planned_parts"),
            "error_count": summary.get("error_count"),
        },
        tail=[],
    )


def _overall_status(results: list[CheckResult]) -> tuple[str, int]:
    if any(item.status == "failed" for item in results):
        return "failed", 1
    if any(item.status == "timeout" for item in results):
        return "degraded", 2
    return "ok", 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    profile = _normalize_profile(args.profile)
    suite = args.suite or _default_suite_for_profile(profile)
    provider_suite = args.provider_suite or _default_provider_suite_for_profile(profile)
    out_path_str = args.out or _default_out_for_profile(profile)
    results = [
        _run_unit_tests(),
        _run_runtime_describe(args.config),
    ]
    if not args.skip_payload_contracts:
        results.append(_run_payload_contract_check())
    regression_include_tags: tuple[str, ...] = ()
    regression_timeout_seconds: int | None = None
    provider_comparison_timeout_seconds: int | None = None
    if not args.skip_regression:
        _, regression_include_tags, regression_timeout_seconds = _resolve_regression_profile(
            profile,
            args.regression_timeout_seconds,
        )
        results.append(
            _run_regression_suite(
                suite,
                regression_timeout_seconds,
                include_tags=regression_include_tags,
                profile=profile,
            )
        )
    if provider_suite and not args.skip_provider_comparison:
        provider_comparison_timeout_seconds = (
            args.provider_comparison_timeout_seconds
            if args.provider_comparison_timeout_seconds is not None
            else PROFILE_TIMEOUTS.get(profile, PROFILE_TIMEOUTS[FAST_PROFILE])
        )
        can_run_provider_suite, preflight = _default_provider_suite_preflight(
            provider_suite,
            fixture_root=args.provider_fixture_root,
            profile=profile,
        )
        if can_run_provider_suite:
            provider_result = _run_provider_comparison_suite(
                config=args.config,
                suite=provider_suite,
                fixture_root=args.provider_fixture_root,
                profile=args.provider_profile,
                timeout_seconds=provider_comparison_timeout_seconds,
                reuse_parser_instances=bool(args.reuse_parser_instances),
            )
            provider_result.details.setdefault("preflight", preflight)
            results.append(provider_result)
        else:
            preflight_reason = str(preflight.get("reason") or "")
            results.append(
                _skipped_provider_comparison_suite(
                    suite=provider_suite,
                    fixture_root=args.provider_fixture_root,
                    profile=args.provider_profile,
                    reason=str(preflight.get("message") or "provider suite skipped"),
                    details=preflight,
                    status="failed" if preflight_reason.startswith("fast_") else "skipped",
                )
            )

    provider_comparison_artifacts: dict[str, str] | None = None
    for item in results:
        if item.name != "provider_comparison_suite":
            continue
        report_payload = item.details.pop("report_payload", None)
        if isinstance(report_payload, Mapping):
            provider_comparison_artifacts = _persist_provider_comparison_artifacts(
                profile=profile,
                out_path=out_path_str,
                report_payload=report_payload,
            )
            item.details["report_json"] = provider_comparison_artifacts["json"]
            item.details["report_markdown"] = provider_comparison_artifacts["markdown"]
            item.details["output_json_path"] = provider_comparison_artifacts["json"]
            item.details["output_md_path"] = provider_comparison_artifacts["markdown"]
        break

    # P5-T12: optional large PDF benchmark gate
    if args.large_pdf_benchmark:
        results.append(_run_large_pdf_benchmark(args.large_pdf_benchmark, config=args.config))

    overall, exit_code = _overall_status(results)
    payload = {
        "status": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile,
        "config": str(args.config),
        "suite": None if args.skip_regression else str(suite),
        "provider_suite": (
            str(provider_suite)
            if provider_suite and not args.skip_provider_comparison
            else None
        ),
        "provider_fixture_root": (
            str(args.provider_fixture_root)
            if args.provider_fixture_root and provider_suite and not args.skip_provider_comparison
            else None
        ),
        "provider_profile": args.provider_profile,
        "reuse_parser_instances": bool(args.reuse_parser_instances),
        "provider_comparison_artifacts": provider_comparison_artifacts,
        "out": str(out_path_str),
        "regression_include_tags": list(regression_include_tags),
        "regression_timeout_seconds": regression_timeout_seconds,
        "provider_comparison_timeout_seconds": provider_comparison_timeout_seconds,
        "checks": [asdict(item) for item in results],
    }
    if profile == PERF_PROFILE:
        perf_tracking: dict[str, Any] = {
            "samples": [],
            "overview": {"sample_count": 0},
            "comparison": None,
        }
        for item in payload["checks"]:
            if item.get("name") != "regression_suite":
                continue
            details = item.get("details") or {}
            perf_tracking["samples"] = list(details.get("perf_samples") or [])
            perf_tracking["overview"] = dict(details.get("perf_overview") or {"sample_count": 0})
            break
        if args.compare_report:
            compare_path = Path(args.compare_report)
            if compare_path.exists():
                try:
                    previous_payload = json.loads(compare_path.read_text(encoding="utf-8"))
                    perf_tracking["comparison"] = _compare_perf_samples(
                        perf_tracking["samples"],
                        previous_payload,
                        compare_report=str(compare_path),
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    perf_tracking["comparison"] = {
                        "available": False,
                        "compare_report": str(compare_path),
                        "reason": f"failed to load compare report: {exc}",
                        "sample_count": 0,
                        "samples": [],
                    }
            else:
                perf_tracking["comparison"] = {
                    "available": False,
                    "compare_report": str(compare_path),
                    "reason": "compare report not found",
                    "sample_count": 0,
                    "samples": [],
                }
        payload["perf_tracking"] = perf_tracking

    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    out_path = Path(out_path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload_text, encoding="utf-8")
    _write_stdout(payload_text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
