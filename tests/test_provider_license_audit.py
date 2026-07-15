from __future__ import annotations

from tools.provider_license_audit import audit_providers, classify_license


def test_classify_license_keeps_copyleft_in_review() -> None:
    assert classify_license("Dual Licensed - GNU AFFERO GPL 3.0 or Commercial") == (
        "review_required",
        "copyleft_or_dual_license",
    )


def test_classify_license_recognizes_permissive_metadata() -> None:
    assert classify_license("MIT") == ("observed_permissive", "permissive_license_observed")


def test_classify_license_expression_recognizes_mit() -> None:
    assert classify_license("MIT") == ("observed_permissive", "permissive_license_observed")


def test_audit_builtin_provider_is_not_external_approval() -> None:
    result = audit_providers(["pdf-text"])
    assert result["providers"]["pdf-text"]["status"] == "project_internal"
    assert result["review_required_provider_ids"] == []
