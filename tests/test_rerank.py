from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from parsecore.config import RerankProviderSettings
from parsecore.rerank import (
    DashScopeCompatibleRerankProvider,
    RerankRequestError,
    build_rerank_provider,
)


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class DashScopeCompatibleRerankProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_key = os.environ.get("PARSECORE_RERANK_API_KEY")
        os.environ["PARSECORE_RERANK_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        if self._previous_key is None:
            os.environ.pop("PARSECORE_RERANK_API_KEY", None)
        else:
            os.environ["PARSECORE_RERANK_API_KEY"] = self._previous_key

    def _settings(self) -> RerankProviderSettings:
        return RerankProviderSettings(
            enabled=True,
            provider="dashscope-compatible",
            model="qwen/qwen3-vl-rerank",
            base_url="https://example.invalid/v1",
            api_key_env="PARSECORE_RERANK_API_KEY",
            timeout_seconds=5.0,
            max_retries=0,
            candidate_limit=20,
            options={"enable_truncation": True},
        )

    def test_posts_confirmed_dashscope_rerank_protocol(self) -> None:
        provider = DashScopeCompatibleRerankProvider(self._settings())
        captured: list[bytes] = []

        def _fake_urlopen(request, timeout=0):
            captured.append(request.data)
            return _FakeHttpResponse(
                {
                    "output": {
                        "results": [
                            {"index": 1, "relevance_score": 0.92},
                            {"index": 0, "relevance_score": 0.31},
                        ]
                    }
                }
            )

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            scores = provider.rerank(
                query="hydraulic inspection",
                documents=["lighting inspection", "hydraulic pressure inspection"],
            )

        self.assertEqual([(item.index, item.score) for item in scores], [(1, 0.92), (0, 0.31)])
        payload = json.loads(captured[0].decode("utf-8"))
        self.assertEqual(payload["model"], "qwen/qwen3-vl-rerank")
        self.assertEqual(payload["input"]["query"], "hydraulic inspection")
        self.assertEqual(payload["input"]["documents"][1], "hydraulic pressure inspection")
        self.assertEqual(payload["parameters"]["top_n"], 2)
        self.assertFalse(payload["parameters"]["return_documents"])
        self.assertTrue(payload["parameters"]["enable_truncation"])

    def test_rejects_out_of_range_response_index(self) -> None:
        provider = DashScopeCompatibleRerankProvider(self._settings())
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeHttpResponse(
                {"output": {"results": [{"index": 4, "relevance_score": 0.8}]}}
            ),
        ):
            with self.assertRaisesRegex(RerankRequestError, "outside the candidate set"):
                provider.rerank(query="q", documents=["one"])

    def test_rejects_fractional_response_index(self) -> None:
        provider = DashScopeCompatibleRerankProvider(self._settings())
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeHttpResponse(
                {"output": {"results": [{"index": 0.5, "relevance_score": 0.8}]}}
            ),
        ):
            with self.assertRaisesRegex(RerankRequestError, "index must be an integer"):
                provider.rerank(query="q", documents=["one"])

    def test_build_provider_supports_fake_route(self) -> None:
        provider = build_rerank_provider(
            RerankProviderSettings(enabled=True, provider="fake")
        )

        self.assertIsNotNone(provider)
        assert provider is not None
        scores = provider.rerank(query="anything", documents=["one", "two"])
        self.assertEqual([item.index for item in scores], [0, 1])


if __name__ == "__main__":
    unittest.main()
