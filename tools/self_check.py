"""ParseCore self-check gate.

Runs the local reliability and runtime checks that now replace the old
jobcard dual-run loop as the default quality gate.

Checks:
1. Unit tests
2. Runtime describe smoke
3. Regression baseline suite (optional, enabled by default)

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
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
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
        "--regression-timeout-seconds",
        type=int,
        default=None,
        help="override the regression-suite timeout; defaults depend on --profile",
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


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    source_path = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_path if not existing else os.pathsep.join((source_path, existing))
    return env


def _tail_lines(text: str, *, limit: int = 20) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def _run_subprocess(
    name: str,
    args: list[str],
    *,
    timeout_seconds: int | None = None,
) -> tuple[CheckResult, str]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=ROOT,
            env=_command_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        result = CheckResult(
            name=name,
            status="passed" if completed.returncode == 0 else "failed",
            exit_code=completed.returncode,
            elapsed_s=round(time.monotonic() - started, 3),
            summary="command completed",
            details={},
            tail=_tail_lines(combined),
        )
        return result, combined
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part for part in ((exc.stdout or ""), (exc.stderr or "")) if part
        ).strip()
        result = CheckResult(
            name=name,
            status="timeout",
            exit_code=None,
            elapsed_s=round(time.monotonic() - started, 3),
            summary=f"timed out after {timeout_seconds}s",
            details={"timeout_seconds": timeout_seconds},
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
    out_path_str = args.out or _default_out_for_profile(profile)
    results = [
        _run_unit_tests(),
        _run_runtime_describe(args.config),
    ]
    regression_include_tags: tuple[str, ...] = ()
    regression_timeout_seconds: int | None = None
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

    overall, exit_code = _overall_status(results)
    payload = {
        "status": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile,
        "config": str(args.config),
        "suite": None if args.skip_regression else str(suite),
        "out": str(out_path_str),
        "regression_include_tags": list(regression_include_tags),
        "regression_timeout_seconds": regression_timeout_seconds,
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

    out_path = Path(out_path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())