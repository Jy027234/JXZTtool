"""Create a user-authorized AI-assisted disposition for empty PDF pages.

The review is deliberately narrow: it can approve a page as non-indexable only
when the source probe reports no text and no image resources, and the rendered
evidence is either pure white or near-blank vector decoration.  It never turns
an image-bearing page into an approved empty page and never changes parser
output or route configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def _classification(row: Mapping[str, Any]) -> str:
    if bool(row.get("visual_blank")):
        return "pure_blank_page"
    ratio = float(row.get("non_white_ratio") or 0.0)
    if ratio <= 0.01:
        return "near_blank_vector_page"
    return "visual_content_present"


def _review_row(row: Mapping[str, Any], *, reviewer: str) -> dict[str, Any]:
    text_chars = int(row.get("text_chars_probe") or 0)
    image_count = int(row.get("image_count_probe") or 0)
    classification = _classification(row)
    approved = (
        text_chars == 0
        and image_count == 0
        and classification in {"pure_blank_page", "near_blank_vector_page"}
    )
    return {
        "evidence_id": row.get("evidence_id"),
        "sample_id": row.get("sample_id"),
        "source": row.get("source"),
        "source_sha256": row.get("source_sha256"),
        "source_page_number": row.get("source_page_number"),
        "text_chars_probe": text_chars,
        "image_count_probe": image_count,
        "content_stream_bytes": row.get("content_stream_bytes"),
        "visual_blank": row.get("visual_blank"),
        "non_white_ratio": row.get("non_white_ratio"),
        "classification": classification,
        "decision": "approved_non_indexable" if approved else "requires_business_review",
        "reviewer": reviewer,
    }


def build_review(
    *,
    manifest: Path,
    sample_ids: Sequence[str] | None,
    reviewer: str,
    out_json: Path,
    out_md: Path,
) -> dict[str, Any]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    pages = payload.get("pages") if isinstance(payload, Mapping) else None
    if not isinstance(pages, list):
        raise ValueError("evidence_manifest_pages_missing")
    selected_ids = {str(value) for value in (sample_ids or []) if str(value)}
    selected = [
        row
        for row in pages
        if isinstance(row, Mapping)
        and (not selected_ids or str(row.get("sample_id") or "") in selected_ids)
    ]
    if not selected:
        raise ValueError("no_evidence_pages_selected")
    reviews = [_review_row(row, reviewer=reviewer) for row in selected]
    approved = [row for row in reviews if row["decision"] == "approved_non_indexable"]
    pending = [row for row in reviews if row["decision"] != "approved_non_indexable"]
    summary = {
        "schema_version": "2026-07-empty-page-review",
        "scope": "ai_assisted_review_not_business_signoff",
        "reviewer": reviewer,
        "evidence_manifest": str(manifest.resolve()),
        "sample_ids": sorted(selected_ids) if selected_ids else sorted({str(row.get("sample_id") or "") for row in selected}),
        "selected_page_count": len(reviews),
        "approved_non_indexable_count": len(approved),
        "requires_business_review_count": len(pending),
        "classification_counts": {
            classification: sum(1 for row in reviews if row["classification"] == classification)
            for classification in sorted({str(row["classification"]) for row in reviews})
        },
        "decision": "approved" if not pending else "pending_business_review",
        "pages": reviews,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Empty-page AI-assisted review",
        "",
        f"- Scope: `{summary['scope']}`",
        f"- Reviewer: `{reviewer}`",
        f"- Selected pages: **{len(reviews)}**",
        f"- Approved non-indexable: **{len(approved)}**",
        f"- Requires business review: **{len(pending)}**",
        "",
        "Approval requires both source probes (`text_chars_probe=0`, `image_count_probe=0`) "
        "and a pure-white or near-blank rendered page. This artifact does not alter parser output or route configuration.",
        "",
    ]
    for row in reviews:
        lines.append(
            f"- `{row['evidence_id']}`: `{row['decision']}`; "
            f"classification=`{row['classification']}`; "
            f"text={row['text_chars_probe']}, images={row['image_count_probe']}"
        )
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--sample-id", action="append", dest="sample_ids")
    parser.add_argument(
        "--reviewer",
        default="Codex (AI-assisted, user-authorized)",
    )
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()
    summary = build_review(
        manifest=Path(args.manifest),
        sample_ids=args.sample_ids,
        reviewer=args.reviewer,
        out_json=Path(args.out_json),
        out_md=Path(args.out_md),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["decision"] == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
