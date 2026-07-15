from __future__ import annotations

import unittest

from parsecore.audit_placeholders import is_non_content_audit_placeholder


class AuditPlaceholderClassificationTests(unittest.TestCase):
    def test_recognizes_each_supported_empty_page_reason(self) -> None:
        for missing_reason in ("page_without_extractable_content", "ocr_empty_text"):
            with self.subTest(missing_reason=missing_reason):
                self.assertTrue(
                    is_non_content_audit_placeholder(
                        semantic_role=" PARSE_ARTIFACT ",
                        metadata={
                            "index_policy": " skip ",
                            "missing_reason": missing_reason,
                        },
                    )
                )

    def test_does_not_exclude_user_content_from_partial_metadata_match(self) -> None:
        metadata = {"index_policy": "skip", "missing_reason": "ocr_empty_text"}

        self.assertFalse(
            is_non_content_audit_placeholder(
                semantic_role="paragraph",
                metadata=metadata,
            )
        )
        self.assertFalse(
            is_non_content_audit_placeholder(
                semantic_role="parse_artifact",
                metadata={**metadata, "index_policy": "index"},
            )
        )
        self.assertFalse(
            is_non_content_audit_placeholder(
                semantic_role="parse_artifact",
                metadata={**metadata, "missing_reason": "ocr_empty_text_summary"},
            )
        )
