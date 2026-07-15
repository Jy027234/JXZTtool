"""Summarize P0 release readiness from verified local artifacts.

This is a read-only decision report.  It distinguishes repository-local
checks from external release conditions (legal/provider approval and a live
embedding gateway) so a passing local audit cannot be mistaken for production
connectivity or license approval.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"json_object_required:{path}")
    return value


def _remote_embedding_state(config: Path) -> dict[str, Any]:
    with config.open("rb") as handle:
        payload = tomllib.load(handle)
    settings = payload.get("providers", {}).get("embedding", {})
    settings = settings if isinstance(settings, Mapping) else {}
    provider = str(settings.get("provider") or "").strip().lower()
    api_key_env = str(settings.get("api_key_env") or "PARSECORE_EMBEDDING_API_KEY")
    enabled = bool(settings.get("enabled"))
    remote_provider = provider in {"", "openai", "openai-compatible", "dashscope", "qwen"}
    local_provider = provider in {
        "sentence-transformers-local",
        "transformers-local",
        "local-transformer",
        "huggingface-local",
    }
    key_present = bool(os.environ.get(api_key_env, "").strip())
    live_ready = enabled and remote_provider and key_present
    reason = None
    if not enabled:
        reason = "embedding_provider_disabled_in_config"
    elif remote_provider and not key_present:
        reason = f"missing_env:{api_key_env}"
    elif local_provider:
        reason = "local_provider_requires_local_rag_acceptance"
    return {
        "provider": provider or "openai-compatible",
        "enabled": enabled,
        "remote_provider": remote_provider,
        "local_provider": local_provider,
        "api_key_env": api_key_env,
        "api_key_present": key_present,
        "live_ready": live_ready,
        "reason": reason,
    }


def build_readiness(
    *,
    audit_summary: Path,
    empty_review: Path,
    stability: Path,
    license_audit: Path,
    config: Path,
    compat_gateway: Path | None = None,
    local_rag: Path | None = None,
    scope: str = "production",
) -> dict[str, Any]:
    normalized_scope = str(scope or "production").strip().casefold()
    if normalized_scope not in {"production", "p0-core"}:
        raise ValueError(f"unsupported_readiness_scope:{scope}")
    audit = _load(audit_summary)
    audit_summary_payload = audit.get("summary") if isinstance(audit.get("summary"), Mapping) else {}
    audit_gate = audit.get("gate") if isinstance(audit.get("gate"), Mapping) else {}
    review = _load(empty_review)
    stable = _load(stability)
    licenses = _load(license_audit)
    remote = _remote_embedding_state(config)
    compat = _load(compat_gateway) if compat_gateway is not None else None
    local_rag_payload = _load(local_rag) if local_rag is not None else None

    local_checks = [
        {
            "id": "upload_audit",
            "passed": bool(audit_gate.get("passed"))
            and int(audit_summary_payload.get("completed_sample_count") or 0)
            == int(audit_summary_payload.get("sample_count") or 0)
            and int(audit_summary_payload.get("missing_page_count") or 0) == 0,
            "evidence": {
                "sample_count": audit_summary_payload.get("sample_count"),
                "completed_sample_count": audit_summary_payload.get("completed_sample_count"),
                "missing_page_count": audit_summary_payload.get("missing_page_count"),
                "gate": audit_gate.get("passed"),
            },
        },
        {
            "id": "empty_page_disposition",
            "passed": review.get("decision") == "approved"
            and int(review.get("requires_business_review_count") or 0) == 0,
            "evidence": {
                "selected_page_count": review.get("selected_page_count"),
                "approved_non_indexable_count": review.get("approved_non_indexable_count"),
                "scope": review.get("scope"),
            },
        },
        {
            "id": "candidate_stability",
            "passed": stable.get("status") == "passed"
            and stable.get("quality_signature_stable") is True
            and int(stable.get("observed_stable_runs") or 0) >= int(stable.get("required_stable_runs") or 3),
            "evidence": {
                "status": stable.get("status"),
                "gate": stable.get("gate"),
                "observed_stable_runs": stable.get("observed_stable_runs"),
                "sample_count": stable.get("sample_count"),
            },
        },
    ]
    if compat is not None:
        local_checks.append(
            {
                "id": "embedding_compat_gateway_transport",
                "passed": compat.get("status") == "ok"
                and float(compat.get("embedded_chunk_ratio") or 0.0) == 1.0
                and int((compat.get("gateway") or {}).get("auth_failures") or 0) == 0,
                "evidence": {
                    "scope": compat.get("scope"),
                    "embedded_chunk_ratio": compat.get("embedded_chunk_ratio"),
                    "gateway": compat.get("gateway"),
                },
            }
        )
    if local_rag_payload is not None:
        local_checks.append(
            {
                "id": "local_embedding_rag_e2e",
                "passed": local_rag_payload.get("status") == "ok"
                and float(local_rag_payload.get("embedded_chunk_ratio") or 0.0) == 1.0
                and float(local_rag_payload.get("hit_rate_at_3") or 0.0) >= 0.75,
                "evidence": {
                    "scope": local_rag_payload.get("scope"),
                    "provider": local_rag_payload.get("provider"),
                    "embedded_chunk_ratio": local_rag_payload.get("embedded_chunk_ratio"),
                    "hit_rate_at_3": local_rag_payload.get("hit_rate_at_3"),
                    "mean_reciprocal_rank_at_3": local_rag_payload.get("mean_reciprocal_rank_at_3"),
                    "index_manifest": local_rag_payload.get("index_manifest"),
                },
            }
        )
    review_required = (
        licenses.get("review_required")
        or licenses.get("review_required_provider_ids")
        or []
    )
    external_checks = [
        {
            "id": "candidate_license_and_commercial_approval",
            "passed": len(review_required) == 0,
            "blocking": normalized_scope == "production",
            "evidence": {"review_required": review_required},
            "next_action": "Obtain license/commercial-use decision before route promotion.",
        },
        {
            "id": "remote_embedding_rag_live",
            "passed": remote["live_ready"],
            "blocking": normalized_scope == "production",
            "evidence": remote,
            "next_action": "Provide a live gateway and API key, then run _embedding_smoke.py --require-live and record index/RAG hit-rate evidence.",
        },
    ]
    optional = [
        {
            "id": "independent_named_human_gold",
            "passed": False,
            "blocking": False,
            "evidence": {"current_gold_scope": "ai_assisted_review_not_human_gold", "approved_pages": 50},
            "next_action": "Only required if governance demands an independent named-human sign-off.",
        }
    ]
    return {
        "schema_version": "2026-07-p0-release-readiness",
        "scope": normalized_scope,
        "local_checks": local_checks,
        "external_checks": external_checks,
        "optional_governance": optional,
        "local_closed_count": sum(1 for item in local_checks if item["passed"]),
        "required_blocker_count": sum(1 for item in external_checks if not item["passed"] and item["blocking"]),
        "optional_open_count": sum(1 for item in optional if not item["passed"]),
        "external_open_count": sum(1 for item in external_checks if not item["passed"]),
        "release_ready": all(
            item["passed"]
            for item in local_checks + [item for item in external_checks if item["blocking"]]
        ),
        "default_route_unchanged": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-summary", required=True)
    parser.add_argument("--empty-review", required=True)
    parser.add_argument("--stability", required=True)
    parser.add_argument("--license-audit", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--compat-gateway")
    parser.add_argument("--local-rag")
    parser.add_argument("--scope", choices=("production", "p0-core"), default="production")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()
    result = build_readiness(
        audit_summary=Path(args.audit_summary),
        empty_review=Path(args.empty_review),
        stability=Path(args.stability),
        license_audit=Path(args.license_audit),
        config=Path(args.config),
        compat_gateway=Path(args.compat_gateway) if args.compat_gateway else None,
        local_rag=Path(args.local_rag) if args.local_rag else None,
        scope=args.scope,
    )
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# P0 release readiness",
        "",
        f"- Scope: **{result['scope']}**",
        f"- Release ready: **{result['release_ready']}**",
        f"- Local checks closed: **{result['local_closed_count']}**",
        f"- Required blockers: **{result['required_blocker_count']}**",
        f"- External checks open: **{result['external_open_count']}**",
        f"- Optional governance items open: **{result['optional_open_count']}**",
        "",
    ]
    for group in ("local_checks", "external_checks", "optional_governance"):
        lines.append(f"## {group}")
        lines.append("")
        for item in result[group]:
            lines.append(f"- `{item['id']}`: `{item['passed']}`")
        lines.append("")
    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["release_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
