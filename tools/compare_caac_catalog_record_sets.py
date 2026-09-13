"""Create a bounded, read-only equality report for catalog record artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from parsecore.catalog_runs import CatalogRunError, compare_catalog_record_sets


def _write_report(path: Path, report: dict[str, object]) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare catalog record JSONL files without changing publication state."
    )
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument(
        "--golden",
        type=Path,
        help="optional independently fixed full-record baseline",
    )
    parser.add_argument("--section-key", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--max-records", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        record_paths = {
            "primary": args.primary,
            "repeat": args.repeat,
        }
        if args.golden is not None:
            record_paths["golden"] = args.golden
        report = compare_catalog_record_sets(
            record_paths=record_paths,
            section_key=args.section_key,
            expected_source_sha256=args.expected_source_sha256,
            max_records=args.max_records,
        )
        _write_report(args.report, report)
    except (CatalogRunError, OSError) as exc:
        print(
            json.dumps(
                {"status": "error", "errorType": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["hardGatePassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
