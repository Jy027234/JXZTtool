"""Run the bounded CAAC approved-catalog golden-page probe.

This tool intentionally reads only an explicit page allowlist.  It emits a
candidate quality report and never writes to the legacy large-PDF output
directories or claims that records are publishable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from parsecore.catalog_contracts import (
    CAAC_APPROVED_CATALOG_EXTRACTOR,
    CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION,
    CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    CAAC_APPROVED_CATALOG_V2_EXTRACTOR_VERSION,
    CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
    CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS,
)
from parsecore.catalog_extractors import (
    CatalogPage,
    anchor_row_numbers,
    build_candidate_record,
    detect_catalog_sections,
    extract_candidate_rows,
)
from parsecore.catalog_omissions import (
    SOURCE_OMISSION_FIELD_PATH,
    normalize_source_omission_review,
    source_omission_review_index,
)


DEFAULT_PAGES = (
    "1-15",
    "27-30",
    "54-57",
    "154-166",
    "4998-5002",
    "16784-16806",
    "16826-16830",
    "17049-17054",
)


def _parse_pages(raw_values: list[str]) -> tuple[int, ...]:
    pages: set[int] = set()
    for raw_value in raw_values:
        for token in str(raw_value).split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                start_text, end_text = token.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start < 1 or end < start:
                    raise ValueError(f"invalid page range: {token}")
                pages.update(range(start, end + 1))
            else:
                page = int(token)
                if page < 1:
                    raise ValueError(f"invalid page number: {token}")
                pages.add(page)
    if not pages:
        raise ValueError("at least one page is required")
    return tuple(sorted(pages))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _extractor_version_for(schema_version: str) -> str:
    if schema_version == CAAC_APPROVED_CATALOG_SCHEMA_VERSION:
        return CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION
    if schema_version == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        return CAAC_APPROVED_CATALOG_V2_EXTRACTOR_VERSION
    raise ValueError(f"unsupported catalog record schema: {schema_version}")


def run_probe(
    *,
    source: Path,
    output: Path,
    pages: tuple[int, ...],
    schema_version: str = CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    source_omission_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - exercised only without parser extras
        raise RuntimeError("pdfplumber is required for the CAAC golden-page probe") from exc

    if schema_version not in CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported catalog record schema: {schema_version}")
    document_hash = _sha256(source)
    omission_review: dict[str, Any] | None = None
    omission_index: dict[tuple[str, int, int, str], Mapping[str, Any]] = {}
    if schema_version == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
        if source_omission_review is None:
            raise ValueError("caac-approved-catalog.v2 requires a source omission review")
        omission_review = normalize_source_omission_review(
            source_omission_review,
            source_snapshot_sha256=document_hash,
        )
        omission_index = source_omission_review_index(omission_review)
    elif source_omission_review is not None:
        raise ValueError("source omission review is only supported by caac-approved-catalog.v2")
    extractor_version = _extractor_version_for(schema_version)
    run_id = output.name if output.name.startswith("run_") else f"run_{output.name}"
    page_observations: list[dict[str, Any]] = []
    candidate_rows_output: list[dict[str, Any]] = []
    candidate_records_by_section: dict[str, list[dict[str, Any]]] = {}
    rejected_rows_output: list[dict[str, Any]] = []
    all_signals: list[dict[str, Any]] = []
    section_counts: Counter[str] = Counter()
    section_candidate_rows: Counter[str] = Counter()
    field_presence: Counter[str] = Counter()
    row_anchor_count = 0
    candidate_row_count = 0

    with pdfplumber.open(str(source)) as document:
        page_count = len(document.pages)
        invalid_pages = [page for page in pages if page > page_count]
        if invalid_pages:
            raise ValueError(f"pages exceed document page count {page_count}: {invalid_pages[:5]}")
        for page_number in pages:
            page = document.pages[page_number - 1]
            text = page.extract_text() or ""
            raw_words = tuple(page.extract_words() or ())
            words = tuple(
                {**dict(word), "id": f"p{page_number:05d}w{index:05d}"}
                for index, word in enumerate(raw_words, start=1)
            )
            catalog_page = CatalogPage(
                pdf_page=page_number,
                text=text,
                words=words,
                printed_page_label=None,
                metadata={
                    "rects": tuple(page.rects or ()),
                    "pageHeight": float(page.height),
                    "pageWidth": float(page.width),
                },
            )
            sections = detect_catalog_sections((catalog_page,))
            anchors, row_signals = anchor_row_numbers(catalog_page)
            candidate_rows: tuple[Mapping[str, Any], ...] = ()
            candidate_signals: tuple[Mapping[str, Any], ...] = ()
            if len(sections) == 1:
                candidate_rows, candidate_signals = extract_candidate_rows(
                    catalog_page,
                    sections[0].section_key,
                )
            for section in sections:
                section_counts[section.section_key] += 1
                section_candidate_rows[section.section_key] += len(candidate_rows)
            row_anchor_count += len(anchors)
            candidate_row_count += len(candidate_rows)
            candidate_rows_output.extend(
                {
                    "sectionKey": sections[0].section_key,
                    "sourceSnapshotSha256": document_hash,
                    **dict(row),
                }
                for row in candidate_rows
            )
            record_signals: list[Mapping[str, Any]] = []
            if len(sections) == 1:
                for row in candidate_rows:
                    record, row_record_signals = build_candidate_record(
                        row=row,
                        section_key=sections[0].section_key,
                        source_snapshot_sha256=document_hash,
                        parser_version="0.1.0",
                        extractor_version=extractor_version,
                        schema_version=schema_version,
                        source_omission=omission_index.get(
                            (
                                sections[0].section_key,
                                int(row.get("rowNumber") or 0),
                                int(row.get("pdfPage") or 0),
                                SOURCE_OMISSION_FIELD_PATH,
                            )
                        ),
                    )
                    record_signals.extend(row_record_signals)
                    if record is None:
                        rejected_rows_output.append(
                            {
                                "sectionKey": sections[0].section_key,
                                "sourceSnapshotSha256": document_hash,
                                **dict(row),
                                "qualitySignals": [dict(signal) for signal in row_record_signals],
                            }
                        )
                    else:
                        candidate_records_by_section.setdefault(sections[0].section_key, []).append(dict(record))
            for row in candidate_rows:
                for field_path in row.get("cells", {}):
                    field_presence[f"{sections[0].section_key}.{field_path}"] += 1
            signals = [dict(signal) for signal in (*candidate_signals, *record_signals)]
            if not signals:
                signals = [dict(signal) for signal in row_signals]
            if not sections:
                signals.append(
                    {
                        "code": "section_boundary_uncertain",
                        "severity": "warning",
                        "message": "no section title/header matched on this sampled page",
                        "pdfPage": page_number,
                    }
                )
            for signal in signals:
                all_signals.append(signal)
            page_observations.append(
                {
                    "pdfPage": page_number,
                    "textPreview": text[:500],
                    "wordCount": len(words),
                    "sections": [section.section_key for section in sections],
                    "rowAnchors": [anchor.row_number for anchor in anchors],
                    "candidateRowCount": len(candidate_rows),
                    "candidateFieldCounts": {
                        field_path: sum(1 for row in candidate_rows if field_path in row.get("cells", {}))
                        for field_path in sorted(
                            {
                                field_path
                                for row in candidate_rows
                                for field_path in row.get("cells", {})
                            }
                        )
                    },
                    "qualitySignalCodes": sorted({str(signal.get("code")) for signal in signals}),
                }
            )

    section_manifest = [
        {
            "sectionKey": key,
            "title": key,
            "pdfPageStart": min(
                observation["pdfPage"]
                for observation in page_observations
                if key in observation["sections"]
            ),
            "pdfPageEnd": max(
                observation["pdfPage"]
                for observation in page_observations
                if key in observation["sections"]
            ),
        }
        for key in sorted(section_counts)
    ]
    manifest = {
        "schemaVersion": schema_version,
        "runId": run_id,
        "document": {
            "fileName": source.name,
            "sha256": document_hash,
            "byteLength": source.stat().st_size,
            "pageCount": page_count,
            "dataCutoffOn": "2024-12-31",
        },
        "parser": {
            "name": "parsecore",
            "version": "0.1.0",
            "extractor": CAAC_APPROVED_CATALOG_EXTRACTOR,
            "extractorVersion": extractor_version,
        },
        "sample": {
            "pageCount": len(pages),
            "pages": list(pages),
            "selection": list(DEFAULT_PAGES),
        },
        "sections": section_manifest,
        "qualitySummary": {
            "status": "candidate",
            "recordCount": 0,
            "candidateCount": sum(len(records) for records in candidate_records_by_section.values()),
            "warningCount": len(all_signals),
            "errorCount": 0,
            "signals": all_signals,
        },
        "artifacts": [
            {
                "dataset": "golden-page-observations",
                "format": "json",
                "path": "quality/observations.json",
                "contentType": "application/json",
                "bytes": 0,
                "records": len(page_observations),
            },
            {
                "dataset": "quality-signals",
                "format": "jsonl",
                "path": "quality/signals.jsonl",
                "contentType": "application/x-ndjson",
                "bytes": 0,
                "records": len(all_signals),
            },
            {
                "dataset": "candidate-rows",
                "format": "jsonl",
                "path": "quality/candidate-rows.jsonl",
                "contentType": "application/x-ndjson",
                "bytes": 0,
                "records": len(candidate_rows_output),
            },
            {
                "dataset": "rejected-rows",
                "format": "jsonl",
                "path": "quality/rejected-rows.jsonl",
                "contentType": "application/x-ndjson",
                "bytes": 0,
                "records": len(rejected_rows_output),
            },
            {
                "dataset": "section-spans",
                "format": "json",
                "path": "sections.json",
                "contentType": "application/json",
                "bytes": 0,
                "records": len(section_manifest),
            },
            {
                "dataset": "field-evidence",
                "format": "jsonl",
                "path": "evidence/fields.jsonl",
                "contentType": "application/x-ndjson",
                "bytes": 0,
                "records": 0,
            },
        ],
    }
    if omission_review is not None:
        review_path = output / "quality" / "source-omission-review.json"
        _write_json(review_path, omission_review)
        manifest["artifacts"].append(
            {
                "dataset": "source-omission-review",
                "format": "json",
                "path": "quality/source-omission-review.json",
                "contentType": "application/json",
                "bytes": review_path.stat().st_size,
                "records": len(omission_review["entries"]),
                "sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            }
        )
    summary = {
        "schemaVersion": schema_version,
        "runId": run_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "sourceSha256": document_hash,
        "sampledPages": len(pages),
        "physicalPages": list(pages),
        "sectionPageHits": dict(sorted(section_counts.items())),
        "rowAnchorCount": row_anchor_count,
        "candidateRowCount": candidate_row_count,
        "sectionCandidateRows": dict(sorted(section_candidate_rows.items())),
        "candidateFieldPresence": dict(sorted(field_presence.items())),
        "qualitySignalCount": len(all_signals),
        "qualitySignalCodes": dict(Counter(str(signal.get("code")) for signal in all_signals)),
        "recordProjection": "candidate_contract_records",
        "candidateRecordCount": sum(len(records) for records in candidate_records_by_section.values()),
        "rejectedRowCount": len(rejected_rows_output),
        "publicationAllowed": False,
    }
    record_artifacts: list[dict[str, Any]] = []
    for section_key, records in sorted(candidate_records_by_section.items()):
        record_path = output / "records" / f"{section_key}.jsonl"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        record_artifacts.append(
            {
                "dataset": section_key,
                "format": "jsonl",
                "path": f"records/{section_key}.jsonl",
                "contentType": "application/x-ndjson",
                "bytes": record_path.stat().st_size,
                "records": len(records),
            }
        )
    manifest["artifacts"].extend(record_artifacts)
    field_evidence_rows: list[dict[str, Any]] = []
    for records in candidate_records_by_section.values():
        for record in records:
            for evidence in record.get("fieldEvidence") or ():
                field_evidence_rows.append(
                    {
                        "recordId": record.get("recordId"),
                        "sectionKey": record.get("sectionKey"),
                        "rowNumber": record.get("rowNumber"),
                        **dict(evidence),
                    }
                )
    sections_path = output / "sections.json"
    _write_json(
        sections_path,
        {
            "schemaVersion": schema_version,
            "runId": run_id,
            "sourceSnapshotSha256": document_hash,
            "sections": section_manifest,
        },
    )
    evidence_path = output / "evidence" / "fields.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in field_evidence_rows),
        encoding="utf-8",
    )
    for dataset, path, record_count in (
        ("section-spans", sections_path, len(section_manifest)),
        ("field-evidence", evidence_path, len(field_evidence_rows)),
    ):
        artifact = next(item for item in manifest["artifacts"] if item["dataset"] == dataset)
        artifact["bytes"] = path.stat().st_size
        artifact["records"] = record_count
    manifest["qualitySummary"]["recordCount"] = sum(
        len(records) for records in candidate_records_by_section.values()
    )
    manifest["qualitySummary"]["candidateCount"] = sum(
        len(records) for records in candidate_records_by_section.values()
    )
    manifest["qualitySummary"]["errorCount"] = len(rejected_rows_output)
    _write_json(output / "quality" / "summary.json", summary)
    _write_json(output / "quality" / "observations.json", page_observations)
    signals_path = output / "quality" / "signals.jsonl"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    signals_path.write_text(
        "".join(json.dumps(signal, ensure_ascii=False) + "\n" for signal in all_signals),
        encoding="utf-8",
    )
    candidate_rows_path = output / "quality" / "candidate-rows.jsonl"
    candidate_rows_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in candidate_rows_output),
        encoding="utf-8",
    )
    rejected_rows_path = output / "quality" / "rejected-rows.jsonl"
    rejected_rows_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rejected_rows_output),
        encoding="utf-8",
    )
    for dataset, path, record_count in (
        ("golden-page-observations", output / "quality" / "observations.json", len(page_observations)),
        ("quality-signals", signals_path, len(all_signals)),
        ("candidate-rows", candidate_rows_path, len(candidate_rows_output)),
        ("rejected-rows", rejected_rows_path, len(rejected_rows_output)),
    ):
        artifact = next(item for item in manifest["artifacts"] if item["dataset"] == dataset)
        artifact["bytes"] = path.stat().st_size
        artifact["records"] = record_count
    _write_json(output / "manifest.json", manifest)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--schema-version",
        choices=sorted(CAAC_SUPPORTED_CATALOG_SCHEMA_VERSIONS),
        default=CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
        help="record contract to emit; V2 requires --source-omission-review",
    )
    parser.add_argument(
        "--source-omission-review",
        type=Path,
        help="reviewed empty-source-cell allow-list required for the V2 contract",
    )
    parser.add_argument(
        "--pages",
        nargs="+",
        default=list(DEFAULT_PAGES),
        help="1-based page numbers/ranges; default is the bounded M1 golden set",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pages = _parse_pages(args.pages)
    source_omission_review = (
        json.loads(args.source_omission_review.read_text(encoding="utf-8"))
        if args.source_omission_review is not None
        else None
    )
    if source_omission_review is not None and not isinstance(source_omission_review, Mapping):
        raise ValueError("source omission review must be a JSON object")
    summary = run_probe(
        source=args.source,
        output=args.output,
        pages=pages,
        schema_version=args.schema_version,
        source_omission_review=source_omission_review,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
