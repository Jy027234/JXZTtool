from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, Sequence

from .config import EmbeddingProviderSettings
from .contracts import EmbeddingProvider
from .models import Chunk


class EmbeddingConfigurationError(RuntimeError):
    """Raised when the embedding provider is not properly configured."""


class EmbeddingRequestError(RuntimeError):
    """Raised after retries are exhausted on a remote embedding call."""


class OpenAiCompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, settings: EmbeddingProviderSettings) -> None:
        if not settings.enabled:
            raise EmbeddingConfigurationError(
                "Embedding provider is disabled; set providers.embedding.enabled = true"
            )
        if not settings.base_url or not settings.model:
            raise EmbeddingConfigurationError(
                "providers.embedding requires both base_url and model"
            )
        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            raise EmbeddingConfigurationError(
                f"environment variable {settings.api_key_env} is empty; cannot call embeddings"
            )
        self._settings = settings
        self._api_key = api_key

    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        if not chunks:
            return tuple(chunks)

        endpoint = self._settings.base_url.rstrip("/") + "/embeddings"
        batch_size = max(1, int(self._settings.batch_size))
        embedded: list[Chunk] = []
        for start in range(0, len(chunks), batch_size):
            batch = list(chunks[start : start + batch_size])
            vectors = self._embed_batch(endpoint=endpoint, chunks=batch)
            if len(vectors) != len(batch):
                raise EmbeddingRequestError(
                    f"embedding provider returned {len(vectors)} vectors for {len(batch)} chunks"
                )
            for chunk, vector in zip(batch, vectors, strict=False):
                embedded.append(replace(chunk, embedding=tuple(float(item) for item in vector)))
        return tuple(embedded)

    def _embed_batch(self, *, endpoint: str, chunks: Sequence[Chunk]) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "input": [chunk.text for chunk in chunks],
        }
        if "dimensions" in self._settings.options:
            payload["dimensions"] = self._settings.options["dimensions"]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        attempts = max(1, self._settings.max_retries + 1)
        for _attempt in range(attempts):
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(  # noqa: S310 - explicit https endpoint
                    request, timeout=self._settings.timeout_seconds
                ) as response:
                    raw = response.read().decode("utf-8")
                data = json.loads(raw)
                items = data.get("data") or []
                vectors: list[list[float]] = []
                for item in items:
                    embedding = item.get("embedding")
                    if not isinstance(embedding, list):
                        raise EmbeddingRequestError("embedding response item missing vector")
                    vectors.append([float(value) for value in embedding])
                return vectors
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                continue
        raise EmbeddingRequestError(
            f"embedding call failed after {attempts} attempts: {last_error}"
        )


def build_embedding_provider(
    settings: EmbeddingProviderSettings,
) -> OpenAiCompatibleEmbeddingProvider | None:
    if not settings.enabled:
        return None
    provider = (settings.provider or "").lower()
    if provider in {"", "openai-compatible", "openai", "dashscope", "qwen"}:
        return OpenAiCompatibleEmbeddingProvider(settings)
    raise EmbeddingConfigurationError(
        f"unsupported embedding provider: {settings.provider!r}"
    )


__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingRequestError",
    "OpenAiCompatibleEmbeddingProvider",
    "build_embedding_provider",
]