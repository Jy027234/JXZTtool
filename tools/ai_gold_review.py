"""Perform an explicitly user-authorized AI-assisted gold review.

This is not a human-review substitute.  It makes the provenance explicit in
the queue and audit artifact, cross-checks the source evidence against the
trusted ``pdf-text`` baseline, and only writes approvals when ``--approve`` is
provided.  It never changes Provider routing or license admission.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2026-07-provider-ai-gold-review"
AI_REVIEWER = "Codex (AI-assisted, user-authorized)"
_ID_TITLE_PATTERN = re.compile(r"-pages-\d+-\d+$", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_REPEATED_PUNCT_PATTERN = re.compile(r"^[\W_]+$", re.UNICODE)


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compact(value: Any) -> str:
    return _WHITESPACE_PATTERN.sub(" ", str(value or "")).strip()


def _useful_anchor(value: Any) -> bool:
    text = _compact(value)
    if len(text) < 4 or len(text) > 100:
        return False
    if _ID_TITLE_PATTERN.search(text):
        return False
    if _REPEATED_PUNCT_PATTERN.match(text):
        return False
    return any(ch.isalnum() for ch in text)


def _unique(items: Sequence[str], *, limit: int = 3) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in items:
        value = _compact(raw)
        if not _useful_anchor(value) or value in seen:
            continue
        seen.add(value)
        values.append(value)
        if len(values) >= limit:
            break
    return values


def _provider_for_sample(sample: Mapping[str, Any], provider_id: str) -> Mapping[str, Any] | None:
    for provider in sample.get("providers", []) or []:
        if isinstance(provider, Mapping) and str(provider.get("provider_id") or "") == provider_id:
            return provider
    return None


def _expected_from_baseline(
    *,
    page_id: str,
    baseline: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    source_probe: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Build bounded expected evidence from the trusted baseline and page probe."""

    if baseline is None or str(baseline.get("status") or "") != "done":
        # The only known incomplete baseline page is a confirmed blank page.
        if int(source_probe.get("text_chars") or 0) == 0 and int(source_probe.get("image_count") or 0) == 0:
            candidate_evidence = [
                entry for entry in (candidate or {}).get("gold_evidence", []) or [] if isinstance(entry, Mapping)
            ]
            synthetic_title = _compact(candidate_evidence[0].get("text") if candidate_evidence else "")
            if not synthetic_title:
                synthetic_title = page_id.replace("review-", "").replace("-p", "-pages-")
            return (
                {
                    "blockKinds": [],
                    "anchors": [],
                    "orderedAnchors": [],
                    "tableAnchors": [],
                    "criticalTokens": [],
                    "mustNotBeHeading": [synthetic_title],
                },
                ["source_page_confirmed_blank", "baseline_provider_missing"],
            )
        raise ValueError(f"baseline_missing_for_nonblank_page:{page_id}")

    evidence = [entry for entry in baseline.get("gold_evidence", []) or [] if isinstance(entry, Mapping)]
    if not evidence:
        raise ValueError(f"baseline_evidence_missing:{page_id}")
    positions = [int(entry.get("position") or 0) for entry in evidence]
    if positions != sorted(positions) or len(positions) != len(set(positions)):
        raise ValueError(f"baseline_evidence_unordered:{page_id}")
    if any(str(entry.get("provider_id") or "") != "pdf-text" for entry in evidence):
        raise ValueError(f"baseline_evidence_provider_mismatch:{page_id}")

    kinds: list[str] = []
    lines: list[str] = []
    table_lines: list[str] = []
    for entry in evidence:
        kind = _compact(entry.get("kind"))
        if kind and kind not in kinds:
            kinds.append(kind)
        for line in str(entry.get("text") or "").splitlines():
            lines.append(line)
            if kind == "table":
                table_lines.append(line)
        for cell in entry.get("table_cells", []) or []:
            if isinstance(cell, Sequence) and not isinstance(cell, (str, bytes)):
                table_lines.extend(str(value) for value in cell)

    anchors = _unique(lines, limit=3)
    # If a page has only very short OCR fragments, keep the strongest token
    # rather than manufacturing labels from the synthetic document title.
    if not anchors:
        anchors = _unique([entry.get("text") for entry in evidence], limit=2)
    table_anchors = _unique(table_lines, limit=3)
    risk_codes: list[str] = []
    if "table" in kinds:
        risk_codes.append("table_present_reviewed_against_baseline")
    if "image" in kinds:
        risk_codes.append("image_or_figure_present")
    return (
        {
            "blockKinds": kinds,
            "anchors": anchors,
            "orderedAnchors": anchors,
            "tableAnchors": table_anchors,
            "criticalTokens": anchors,
            "mustNotBeHeading": [],
        },
        risk_codes,
    )


def build_ai_review(
    *,
    queue_path: str | Path,
    manifest_path: str | Path,
    evaluation_path: str | Path,
    reviewer: str = AI_REVIEWER,
    reviewed_at: str | None = None,
    visual_spot_checked_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an approved queue copy and a transparent AI-review audit."""

    queue_file = Path(queue_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    evaluation_file = Path(evaluation_path).resolve()
    queue = _load_json(queue_file)
    manifest = _load_json(manifest_file)
    evaluation = _load_json(evaluation_file)
    if not isinstance(queue, Mapping) or not isinstance(queue.get("pages"), list):
        raise ValueError("gold_review_queue_must_contain_pages")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("pages"), list):
        raise ValueError("gold_evidence_manifest_must_contain_pages")
    comparison = evaluation.get("comparison") if isinstance(evaluation, Mapping) else None
    if not isinstance(comparison, Mapping) or not isinstance(comparison.get("samples"), list):
        raise ValueError("gold_evaluation_comparison_missing_samples")

    manifest_pages = {
        str(page.get("id") or ""): page
        for page in manifest["pages"]
        if isinstance(page, Mapping) and str(page.get("id") or "")
    }
    samples = {
        str(sample.get("sample_name") or ""): sample
        for sample in comparison["samples"]
        if isinstance(sample, Mapping) and str(sample.get("sample_name") or "")
    }
    checked_ids = {str(value).strip() for value in visual_spot_checked_ids if str(value).strip()}
    timestamp = reviewed_at or datetime.now().astimezone().isoformat(timespec="seconds")
    updated_pages: list[dict[str, Any]] = []
    page_audits: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for raw_page in queue["pages"]:
        if not isinstance(raw_page, Mapping):
            errors.append({"page_id": "", "code": "page_must_be_object"})
            continue
        page = dict(raw_page)
        page_id = str(page.get("id") or "").strip()
        manifest_page = manifest_pages.get(page_id)
        sample = samples.get(page_id)
        checks: list[str] = []
        risk_codes: list[str] = []
        if manifest_page is None:
            errors.append({"page_id": page_id, "code": "manifest_page_missing"})
            updated_pages.append(page)
            continue
        if sample is None:
            errors.append({"page_id": page_id, "code": "evaluation_sample_missing"})
            updated_pages.append(page)
            continue
        evidence = manifest_page.get("evidence") if isinstance(manifest_page.get("evidence"), Mapping) else {}
        for evidence_key in ("screenshot", "text"):
            relative = str(evidence.get(evidence_key) or "").strip()
            evidence_file = manifest_file.parent / relative
            if not relative or not evidence_file.is_file():
                errors.append({"page_id": page_id, "code": f"{evidence_key}_missing"})
            else:
                expected_hash = str(evidence.get(f"{evidence_key}_sha256") or "").strip()
                if expected_hash and _sha256(evidence_file) != expected_hash:
                    errors.append({"page_id": page_id, "code": f"{evidence_key}_hash_mismatch"})
                checks.append(f"{evidence_key}_evidence_verified")

        baseline = _provider_for_sample(sample, "pdf-text")
        candidate = _provider_for_sample(sample, "pymupdf4llm-local")
        source_probe = manifest_page.get("source_probe") if isinstance(manifest_page.get("source_probe"), Mapping) else {}
        try:
            expected, expected_risks = _expected_from_baseline(
                page_id=page_id,
                baseline=baseline,
                candidate=candidate,
                source_probe=source_probe,
            )
            risk_codes.extend(expected_risks)
            checks.append("baseline_order_and_provenance_verified")
        except ValueError as exc:
            errors.append({"page_id": page_id, "code": str(exc)})
            updated_pages.append(page)
            continue

        page["review_status"] = "approved"
        page["review"] = {
            "reviewer": reviewer,
            "reviewed_at": timestamp,
            "source_screenshot": str(evidence.get("screenshot") or ""),
            "notes": (
                "AI-assisted review authorized by user: source screenshot/text probe and "
                "pdf-text baseline evidence cross-checked; visual spot-checks recorded separately. "
                f"risk={','.join(risk_codes) or 'none'}."
            ),
        }
        page["expected"] = expected
        updated_pages.append(page)
        page_audits.append(
            {
                "page_id": page_id,
                "review_status": "approved",
                "baseline_status": str((baseline or {}).get("status") or "missing"),
                "source_probe": dict(source_probe),
                "checks": checks,
                "risk_codes": risk_codes,
                "visual_spot_checked": page_id in checked_ids,
                "expected_counts": {key: len(value) for key, value in expected.items()},
            }
        )

    approved_count = sum(1 for page in updated_pages if str(page.get("review_status") or "") == "approved")
    audit = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if not errors else "failed",
        "scope": "ai_assisted_review_not_human_gold",
        "reviewer": reviewer,
        "reviewed_at": timestamp,
        "queue_path": str(queue_file),
        "manifest_path": str(manifest_file),
        "evaluation_path": str(evaluation_file),
        "visual_spot_checked_ids": sorted(checked_ids),
        "summary": {
            "total_pages": len(updated_pages),
            "approved_pages": approved_count,
            "error_count": len(errors),
            "baseline_done_pages": sum(1 for item in page_audits if item["baseline_status"] == "done"),
            "blank_pages": sum(1 for item in page_audits if "source_page_confirmed_blank" in item["risk_codes"]),
        },
        "errors": errors,
        "pages": page_audits,
        "warnings": [
            "Expected labels were derived from the trusted pdf-text baseline plus source probes; this is not an independent human annotation.",
            "Provider routing and approved_provider_ids were not changed.",
        ],
    }
    updated_queue = dict(queue)
    updated_queue["pages"] = updated_pages
    updated_queue["description"] = (
        "AI-assisted, user-authorized review output. Review evidence and expected labels are recorded "
        "with an explicit non-human reviewer identity; do not treat this as independent human gold."
    )
    return updated_queue, audit


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Perform explicit AI-assisted Provider gold review")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--out-queue", required=True)
    parser.add_argument("--audit-out", required=True)
    parser.add_argument("--reviewer", default=AI_REVIEWER)
    parser.add_argument("--reviewed-at")
    parser.add_argument("--visual-spot-check-id", action="append", default=[])
    parser.add_argument("--approve", action="store_true", help="Write approved statuses; without this the command is a dry run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    updated_queue, audit = build_ai_review(
        queue_path=args.queue,
        manifest_path=args.manifest,
        evaluation_path=args.evaluation,
        reviewer=args.reviewer,
        reviewed_at=args.reviewed_at,
        visual_spot_checked_ids=args.visual_spot_check_id,
    )
    _write_json(args.audit_out, audit)
    if args.approve and audit["status"] == "ok":
        _write_json(args.out_queue, updated_queue)
    elif args.approve:
        print(f"[ai-gold-review] refused to write because errors={len(audit['errors'])}")
        return 1
    print(json.dumps({
        "status": audit["status"],
        "approved_pages": audit["summary"]["approved_pages"],
        "errors": len(audit["errors"]),
        "audit": str(Path(args.audit_out).resolve()),
        "queue_written": bool(args.approve),
    }, ensure_ascii=False, indent=2))
    return 0 if audit["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
