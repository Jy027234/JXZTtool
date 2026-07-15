from __future__ import annotations

import json
import unittest

from parsecore.provider_failures import classify_provider_failure


class TestProviderFailureClassification(unittest.TestCase):
    def test_classifies_observed_embedding_http_400_as_invalid_input(self) -> None:
        self.assertEqual(
            classify_provider_failure(
                "embedding call failed after 3 attempts: HTTP Error 400: Bad Request"
            ),
            "invalid_input",
        )

    def test_classifies_stable_operational_categories(self) -> None:
        cases = (
            (RuntimeError("HTTP Error 429: Too Many Requests"), "rate_limited"),
            (TimeoutError("gateway timed out"), "timeout"),
            (PermissionError("access is denied"), "permission_denied"),
            (ImportError("missing dependency"), "provider_unavailable"),
            (RuntimeError("gateway unavailable"), "provider_unavailable"),
            (RuntimeError("embedding response item missing vector"), "invalid_response"),
            (RuntimeError("provider disabled by configuration"), "configuration_error"),
            (RuntimeError("unsupported media type"), "unsupported"),
            (RuntimeError("parser crashed"), "provider_failed"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(classify_provider_failure(error), expected)

    def test_json_decode_error_is_invalid_response(self) -> None:
        error = json.JSONDecodeError("invalid payload", "{", 1)
        self.assertEqual(classify_provider_failure(error), "invalid_response")


if __name__ == "__main__":
    unittest.main()
