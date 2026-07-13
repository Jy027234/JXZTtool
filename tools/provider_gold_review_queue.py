"""Create pending, human-reviewable pages for a ParseCore provider gold corpus.

The command never calls a parser and never marks a page approved.  It only
selects evenly distributed PDF pages and writes the fields a reviewer must
fill before the page can participate in provider promotion.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "2026-07-provider-gold-review-queue"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evenly_spaced_pages(total_pages: int, count: int) -> list[int]:
    """Return unique 1-based page numbers including both ends when possible."""
    total = max(0, int(total_pages))
    requested = min(total, max(0, int(count)))
    if requested == 0:
        return []
    if requested == 1:
        return [1]
    return sorted({1 + round(index * (total - 1) / (requested - 1)) for index in range(requested)})


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover - covered by runtime image checks
        raise RuntimeError("pypdf_required_for_gold_review_queue") from exc
    return len(PdfReader(str(path)).pages)


def build_review_queue(
    *,
    source_map_path: str | Path,
    pages_per_document: int = 25,
    document_ids: Sequence[str] = (),
    page_count_reader: Callable[[Path], int] = _pdf_page_count,
) -> dict[str, Any]:
    source_map_file = Path(source_map_path).resolve()
    source_map = _load_json(source_map_file)
    if not isinstance(source_map, Mapping):
        raise ValueError("source_map_must_be_object")
    requested_ids = [str(value).strip() for value in document_ids if str(value).strip()]
    source_ids = requested_ids or sorted(str(key) for key in source_map)
    if not source_ids:
        raise ValueError("source_map_has_no_documents")

    pages: list[dict[str, Any]] = []
    for document_id in source_ids:
        raw_source = str(source_map.get(document_id) or "").strip()
        if not raw_source:
            raise ValueError(f"gold_source_map_missing:{document_id}")
        source = Path(raw_source)
        if not source.is_absolute():
            source = (source_map_file.parent / source).resolve()
        if not source.exists():
            raise ValueError(f"gold_source_not_found:{document_id}:{source}")
        if source.suffix.casefold() != ".pdf":
            raise ValueError(f"gold_source_must_be_pdf:{document_id}:{source}")
        total_pages = page_count_reader(source)
        for page_number in evenly_spaced_pages(total_pages, pages_per_document):
            pages.append({
                "id": f"review-{document_id}-p{page_number}",
                "document_id": document_id,
                "page_number": page_number,
                "review_status": "pending",
                "source": "generated-review-queue",
                "review": {
                    "reviewer": "",
                    "reviewed_at": "",
                    "source_screenshot": "",
                    "notes": "Fill all expected fields and then set review_status to approved or rejected.",
                },
                "expected": {
                    "blockKinds": [],
                    "anchors": [],
                    "orderedAnchors": [],
                    "tableAnchors": [],
                    "criticalTokens": [],
                    "mustNotBeHeading": [],
                },
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "description": (
            "Generated pending review queue. Entries are not gold labels and cannot "
            "be used for provider promotion until a named reviewer fills the expected "
            "evidence and explicitly changes review_status to approved."
        ),
        "minimum_approved_pages": 50,
        "minimum_stable_runs": 3,
        "minimum_score_improvement": 5,
        "approved_provider_ids": ["pdf-text"],
        "pages": pages,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate pending human-review pages for a provider gold corpus")
    parser.add_argument("--source-map", required=True, help="JSON mapping document_id to a local PDF path")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--pages-per-document", type=int, default=25)
    parser.add_argument("--document-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = build_review_queue(
        source_map_path=args.source_map,
        pages_per_document=args.pages_per_document,
        document_ids=args.document_id,
    )
    output = Path(args.out_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[provider-gold-review-queue] wrote {output} ({len(payload['pages'])} pending pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
