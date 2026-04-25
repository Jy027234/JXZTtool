from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from parsecore.config import EmbeddingProviderSettings
from parsecore.embeddings import OpenAiCompatibleEmbeddingProvider
from parsecore.models import Chunk


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class OpenAiCompatibleEmbeddingProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_key = os.environ.get("PARSECORE_EMBEDDING_API_KEY")
        os.environ["PARSECORE_EMBEDDING_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        if self._previous_key is None:
            os.environ.pop("PARSECORE_EMBEDDING_API_KEY", None)
        else:
            os.environ["PARSECORE_EMBEDDING_API_KEY"] = self._previous_key

    def _settings(self, *, batch_size: int = 2) -> EmbeddingProviderSettings:
        return EmbeddingProviderSettings(
            enabled=True,
            provider="openai-compatible",
            model="text-embedding-3-small",
            base_url="https://example.invalid/v1",
            api_key_env="PARSECORE_EMBEDDING_API_KEY",
            timeout_seconds=5.0,
            max_retries=0,
            batch_size=batch_size,
            options={},
        )

    def test_embed_batches_and_populates_vectors(self) -> None:
        provider = OpenAiCompatibleEmbeddingProvider(self._settings(batch_size=2))
        chunks = [
            Chunk(chunk_id="c1", doc_id="d", block_ids=("b1",), text="alpha"),
            Chunk(chunk_id="c2", doc_id="d", block_ids=("b2",), text="beta"),
            Chunk(chunk_id="c3", doc_id="d", block_ids=("b3",), text="gamma"),
        ]
        responses = [
            _FakeHttpResponse({"data": [{"embedding": [1.0, 2.0]}, {"embedding": [3.0, 4.0]}]}),
            _FakeHttpResponse({"data": [{"embedding": [5.0, 6.0]}]}),
        ]

        with patch("urllib.request.urlopen", side_effect=responses) as mocked:
            embedded = provider.embed(doc_id="d", chunks=chunks)

        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(embedded[0].embedding, (1.0, 2.0))
        self.assertEqual(embedded[2].embedding, (5.0, 6.0))

    def test_embed_includes_dimensions_when_configured(self) -> None:
        provider = OpenAiCompatibleEmbeddingProvider(
            EmbeddingProviderSettings(
                enabled=True,
                provider="openai-compatible",
                model="text-embedding-3-small",
                base_url="https://example.invalid/v1",
                api_key_env="PARSECORE_EMBEDDING_API_KEY",
                timeout_seconds=5.0,
                max_retries=0,
                batch_size=8,
                options={"dimensions": 256},
            )
        )
        chunks = [Chunk(chunk_id="c1", doc_id="d", block_ids=("b1",), text="alpha")]
        captured_body: list[bytes] = []

        def _fake_urlopen(request, timeout=0):
            captured_body.append(request.data)
            return _FakeHttpResponse({"data": [{"embedding": [1.0, 2.0]}]})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            provider.embed(doc_id="d", chunks=chunks)

        self.assertTrue(captured_body)
        payload = json.loads(captured_body[0].decode("utf-8"))
        self.assertEqual(payload["dimensions"], 256)


if __name__ == "__main__":
    unittest.main()