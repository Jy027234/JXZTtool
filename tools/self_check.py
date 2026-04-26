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
    parser.add_argument("--config", default=str(ROOT / "parsecore.toml"))
    parser.add_argument("--suite", default=str(ROOT / "var" / "regression" / "suite.json"))
    parser.add_argument(
        "--out",
        default=str(ROOT / "var" / "self-check" / "latest.json"),
    )
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument("--regression-timeout-seconds", type=int, default=600)
    return parser


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


def _run_regression_suite(suite: str, timeout_seconds: int) -> CheckResult:
    result, output = _run_subprocess(
        "regression_suite",
        [sys.executable, "tools/regression_baseline.py", "check-suite", "--suite", suite],
        timeout_seconds=timeout_seconds,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    ok_count = sum(1 for line in lines if line == "[check] OK: all metrics within budget")
    skip_count = sum(1 for line in lines if line.startswith("[suite] SKIP"))
    last_suite = _last_suite_label(lines)
    result.details.update(
        {
            "ok_count": ok_count,
            "skip_count": skip_count,
            "last_suite": last_suite,
        }
    )
    if result.status == "passed":
        result.summary = f"ok={ok_count} skipped={skip_count}"
    elif result.status == "timeout":
        label = last_suite or "unknown suite"
        result.summary = f"timed out on {label} after ok={ok_count} skipped={skip_count}"
    else:
        label = last_suite or "unknown suite"
        result.summary = f"failed on {label} after ok={ok_count} skipped={skip_count}"
    return result


def _overall_status(results: list[CheckResult]) -> tuple[str, int]:
    if any(item.status == "failed" for item in results):
        return "failed", 1
    if any(item.status == "timeout" for item in results):
        return "degraded", 2
    return "ok", 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    results = [
        _run_unit_tests(),
        _run_runtime_describe(args.config),
    ]
    if not args.skip_regression:
        results.append(_run_regression_suite(args.suite, args.regression_timeout_seconds))

    overall, exit_code = _overall_status(results)
    payload = {
        "status": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": str(args.config),
        "suite": None if args.skip_regression else str(args.suite),
        "checks": [asdict(item) for item in results],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())