"""Plan, execute, resume, or rerun bounded CAAC catalog parts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from parsecore.catalog_runs import (
    CatalogRunError,
    audit_catalog_run,
    build_catalog_run_ledger,
    execute_catalog_run,
    finalize_catalog_run,
    initialize_catalog_run,
    load_catalog_run,
)
from parsecore.config import ParseProfileSettings, load_settings


def _profile(settings_path: Path, name: str) -> tuple[Any, ParseProfileSettings]:
    settings = load_settings(settings_path)
    if settings.runtime.execution_mode.strip().lower() != "queue-worker":
        raise CatalogRunError("catalog run config must use runtime.execution_mode=queue-worker")
    try:
        profile = settings.profiles[name]
    except KeyError as exc:
        raise CatalogRunError(f"catalog profile not found: {name}") from exc
    if settings.runtime.max_active_parts_per_doc not in {1, 2}:
        raise CatalogRunError(
            "catalog run config runtime.max_active_parts_per_doc must be 1 or 2"
        )
    if settings.runtime.part_timeout_seconds <= 0:
        raise CatalogRunError("catalog run config runtime.part_timeout_seconds must be positive")
    return settings, profile


def _build_ledger(args: argparse.Namespace) -> dict[str, Any]:
    settings, profile = _profile(args.config, args.profile)
    return build_catalog_run_ledger(
        source=args.source,
        output=args.output,
        run_id=args.run_id,
        expected_source_sha256=args.expected_source_sha256,
        expected_page_count=args.expected_page_count,
        data_cutoff_on=args.data_cutoff_on,
        section_key=args.section_key,
        page_start=args.page_start,
        page_end=args.page_end,
        profile_name=args.profile,
        target_pages_per_part=profile.target_pages_per_part,
        part_context_pages=profile.part_context_pages,
        max_active_parts_per_doc=settings.runtime.max_active_parts_per_doc,
        record_schema=profile.record_schema,
        stream_pages=profile.stream_pages,
        stream_lines=profile.stream_lines,
        stream_records=profile.stream_records,
        enable_record_fts=profile.enable_record_fts,
        enable_ocr=profile.enable_ocr,
        source_omission_review=args.source_omission_review,
    )


def _assert_resume_identity(
    ledger: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    checks = (
        ("runId", ledger.get("runId"), expected.get("runId")),
        (
            "document.sha256",
            (ledger.get("document") or {}).get("sha256"),
            (expected.get("document") or {}).get("sha256"),
        ),
        ("profile", ledger.get("profile"), expected.get("profile")),
        ("scope", ledger.get("scope"), expected.get("scope")),
        ("parser", ledger.get("parser"), expected.get("parser")),
        (
            "sourceOmissionReview",
            ledger.get("sourceOmissionReview"),
            expected.get("sourceOmissionReview"),
        ),
    )
    mismatches = [name for name, actual, wanted in checks if actual != wanted]
    if mismatches:
        raise CatalogRunError(
            "resume arguments/config do not match immutable ledger fields: "
            + ", ".join(mismatches)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", default="large-pdf-catalog")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-page-count", type=int, required=True)
    parser.add_argument("--data-cutoff-on", required=True)
    parser.add_argument("--section-key", default="pma_item")
    parser.add_argument("--page-start", type=int, required=True)
    parser.add_argument("--page-end", type=int, required=True)
    parser.add_argument("--max-parts", type=int, required=True)
    parser.add_argument("--max-runtime-seconds", type=float, required=True)
    parser.add_argument("--max-records-per-part", type=int, default=20_000)
    parser.add_argument("--max-records", type=int, default=500_000)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--audit-report", type=Path)
    parser.add_argument("--source-omission-review", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--failed-only", action="store_true")
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--finalize", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        expected_ledger = _build_ledger(args)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry-run",
                        "runId": expected_ledger["runId"],
                        "document": expected_ledger["document"],
                        "profile": expected_ledger["profile"],
                        "scope": expected_ledger["scope"],
                        "summary": expected_ledger["summary"],
                        "firstPart": expected_ledger["parts"][0],
                        "lastPart": expected_ledger["parts"][-1],
                        "publicationAllowed": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.audit:
            if args.schema is None:
                raise CatalogRunError("--audit requires --schema")
            if not (args.output.resolve() / "run-ledger.json").is_file():
                raise CatalogRunError("--audit requires an existing run-ledger.json")
            _assert_resume_identity(load_catalog_run(args.output), expected_ledger)
            report = audit_catalog_run(
                output=args.output,
                schema_path=args.schema,
                max_records=args.max_records,
                max_runtime_seconds=args.max_runtime_seconds,
                report_path=args.report,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["hardGatePassed"] else 1

        if args.finalize:
            if args.schema is None or args.audit_report is None:
                raise CatalogRunError(
                    "--finalize requires --schema and --audit-report"
                )
            if not (args.output.resolve() / "run-ledger.json").is_file():
                raise CatalogRunError("--finalize requires an existing run-ledger.json")
            _assert_resume_identity(load_catalog_run(args.output), expected_ledger)
            result = finalize_catalog_run(
                output=args.output,
                audit_report_path=args.audit_report,
                schema_path=args.schema,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        ledger_path = args.output.resolve() / "run-ledger.json"
        if args.execute:
            if ledger_path.exists():
                raise CatalogRunError("--execute refuses an existing run; use --resume")
            initialize_catalog_run(args.output, expected_ledger)
        else:
            if not ledger_path.is_file():
                raise CatalogRunError("resume mode requires an existing run-ledger.json")
            _assert_resume_identity(load_catalog_run(args.output), expected_ledger)
        result = execute_catalog_run(
            output=args.output,
            source=args.source,
            resume=bool(args.resume or args.failed_only),
            failed_only=bool(args.failed_only),
            max_parts=args.max_parts,
            max_runtime_seconds=args.max_runtime_seconds,
            max_records_per_part=args.max_records_per_part,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("failedParts", 0) == 0 else 1
    except (CatalogRunError, FileNotFoundError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "errorType": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
