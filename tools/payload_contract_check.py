"""Validate frozen ParseCore payload contracts against representative sample payloads.

Exit codes:
0 -> all registered schemas are valid and all sample payloads match
1 -> at least one schema or sample payload validation failed
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from jsonschema import Draft202012Validator

from parsecore.payload_contract_samples import build_payload_contract_samples
from parsecore.payload_schemas import (
    PAYLOAD_SCHEMA_REGISTRY_VERSION,
    payload_schema,
    payload_schema_names,
    payload_schema_registry,
)


CHECK_SCHEMA_VERSION = "2026-06-payload-contract-check"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate frozen ParseCore payload contracts")
    parser.add_argument(
        "--out",
        default=None,
        help="optional JSON output path; stdout is always written",
    )
    return parser


def run_check() -> dict[str, Any]:
    registry = payload_schema_registry()
    sample_payloads = build_payload_contract_samples()
    schema_results: list[dict[str, Any]] = []
    payload_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for name in payload_schema_names():
        schema = payload_schema(name)
        meta = schema.get("x-parsecore") if isinstance(schema.get("x-parsecore"), dict) else {}
        try:
            Draft202012Validator.check_schema(schema)
            schema_results.append(
                {
                    "name": name,
                    "status": "passed",
                    "schema_version": meta.get("schema_version"),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive; exercised via result surface
            entry = {
                "name": name,
                "status": "failed",
                "schema_version": meta.get("schema_version"),
                "error": str(exc),
            }
            schema_results.append(entry)
            failures.append({"kind": "schema", **entry})
            continue

        payload = sample_payloads.get(name)
        if not isinstance(payload, dict):
            entry = {
                "name": name,
                "status": "failed",
                "error": "sample payload missing",
            }
            payload_results.append(entry)
            failures.append({"kind": "payload", **entry})
            continue

        try:
            Draft202012Validator(schema).validate(payload)
            payload_results.append(
                {
                    "name": name,
                    "status": "passed",
                    "projection": payload.get("projection"),
                    "schema_version": payload.get("schema_version"),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive; exercised via result surface
            entry = {
                "name": name,
                "status": "failed",
                "projection": payload.get("projection"),
                "schema_version": payload.get("schema_version"),
                "error": str(exc),
            }
            payload_results.append(entry)
            failures.append({"kind": "payload", **entry})

    return {
        "schema_version": CHECK_SCHEMA_VERSION,
        "registry_schema_version": PAYLOAD_SCHEMA_REGISTRY_VERSION,
        "status": "passed" if not failures else "failed",
        "summary": {
            "schema_count": len(schema_results),
            "payload_count": len(payload_results),
            "failed_schema_count": sum(1 for item in schema_results if item["status"] != "passed"),
            "failed_payload_count": sum(1 for item in payload_results if item["status"] != "passed"),
        },
        "registry": registry,
        "schemas": schema_results,
        "payloads": payload_results,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_check()
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload_text)
            handle.write("\n")
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(payload_text.encode("utf-8", errors="replace"))
        stdout_buffer.write(b"\n")
        stdout_buffer.flush()
    else:
        print(payload_text)
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
