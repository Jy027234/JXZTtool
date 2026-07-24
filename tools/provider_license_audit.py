"""Collect local Provider package license evidence without approving usage.

Package metadata is evidence only.  A provider with an unknown or copyleft
license remains ``review_required`` until the product's distribution and
commercial-use policy explicitly approves it.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2026-07-provider-license-audit"

PROVIDER_PACKAGES: dict[str, tuple[str, ...]] = {
    "pdf-text": (),
    "pymupdf4llm-local": ("pymupdf4llm", "PyMuPDF"),
    "docling-local": ("docling", "docling-core", "docling-parse"),
    "mineru-local": ("mineru",),
}


def classify_license(license_text: str | None) -> tuple[str, str]:
    normalized = " ".join(str(license_text or "").split()).strip()
    lowered = normalized.lower()
    if not normalized:
        return "review_required", "license_metadata_missing"
    if "agpl" in lowered or "gpl" in lowered or "copyleft" in lowered:
        return "review_required", "copyleft_or_dual_license"
    if any(token in lowered for token in ("mit", "apache", "bsd", "mpl", "isc")):
        return "observed_permissive", "permissive_license_observed"
    return "review_required", "license_policy_unknown"


def _package_evidence(package_name: str) -> dict[str, Any]:
    try:
        package_metadata = metadata(package_name)
        package_version = version(package_name)
    except PackageNotFoundError:
        return {"package": package_name, "installed": False}
    license_text = (
        str(package_metadata.get("License") or "").strip()
        or str(package_metadata.get("License-Expression") or "").strip()
        or None
    )
    project_urls = package_metadata.get_all("Project-URL") or []
    return {
        "package": package_name,
        "installed": True,
        "version": package_version,
        "license": license_text,
        "license_expression": package_metadata.get("License-Expression"),
        "home_page": package_metadata.get("Home-page"),
        "project_urls": list(project_urls),
    }


def audit_providers(provider_ids: Sequence[str]) -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for provider_id in provider_ids:
        package_names = PROVIDER_PACKAGES.get(provider_id)
        if package_names is None:
            providers[provider_id] = {
                "provider_id": provider_id,
                "status": "review_required",
                "reason": "provider_package_mapping_missing",
                "packages": [],
            }
            continue
        if not package_names:
            providers[provider_id] = {
                "provider_id": provider_id,
                "status": "project_internal",
                "reason": "built_in_provider_no_external_package",
                "packages": [],
            }
            continue
        packages = [_package_evidence(name) for name in package_names]
        installed = [item for item in packages if item.get("installed")]
        if not installed:
            status, reason = "not_installed", "required_package_not_installed"
        else:
            classifications = [classify_license(item.get("license")) for item in installed]
            if any(item[0] == "review_required" for item in classifications):
                status, reason = "review_required", next(
                    item[1] for item in classifications if item[0] == "review_required"
                )
            elif len(installed) != len(packages):
                status, reason = "review_required", "dependency_package_not_installed"
            else:
                status, reason = "observed_permissive", "all_package_licenses_observed"
        providers[provider_id] = {
            "provider_id": provider_id,
            "status": status,
            "reason": reason,
            "packages": packages,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "approval_scope": "evidence_only_no_route_or_compliance_approval",
        "providers": providers,
        "review_required_provider_ids": [
            provider_id
            for provider_id, item in providers.items()
            if item.get("status") == "review_required"
        ],
    }


def render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# Provider license audit",
        "",
        f"- scope: `{result.get('approval_scope')}`",
        "",
        "| provider | status | reason | packages |",
        "| --- | --- | --- | --- |",
    ]
    for provider_id, item in (result.get("providers") or {}).items():
        packages = ", ".join(
            f"{pkg.get('package')}={pkg.get('version', 'missing')}"
            for pkg in item.get("packages", [])
        ) or "-"
        lines.append(
            f"| {provider_id} | {item.get('status')} | {item.get('reason')} | {packages} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect local Provider license evidence")
    parser.add_argument("--provider", action="append", dest="providers")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", default=None)
    args = parser.parse_args(argv)
    provider_ids = args.providers or tuple(PROVIDER_PACKAGES)
    result = audit_providers(provider_ids)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        out_md = Path(args.out_md)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"status": "ok", "review_required": result["review_required_provider_ids"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
