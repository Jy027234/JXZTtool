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
from .stubs import FakeEmbeddingProvider


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


class LocalTransformerEmbeddingProvider(EmbeddingProvider):
    """Optional local Transformer mean-pooling embedding provider.

    This provider is intentionally opt-in.  It keeps the base package free of
    heavyweight ML dependencies and never changes the default OpenAI-compatible
    route.  ``settings.model`` may be a Hugging Face model id or a local model
    directory; ``options.local_files_only`` can be used for air-gapped runs.
    """

    def __init__(self, settings: EmbeddingProviderSettings) -> None:
        if not settings.enabled:
            raise EmbeddingConfigurationError(
                "Embedding provider is disabled; set providers.embedding.enabled = true"
            )
        model_name = str(settings.model or settings.options.get("model_path") or "").strip()
        if not model_name:
            raise EmbeddingConfigurationError(
                "local transformer embedding requires providers.embedding.model or options.model_path"
            )
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - optional dependency path
            raise EmbeddingConfigurationError(
                "local transformer embedding requires the optional 'local-embedding' dependencies"
            ) from exc

        options = dict(settings.options or {})
        local_files_only = bool(options.get("local_files_only", False))
        device_name = str(options.get("device", "cpu") or "cpu").strip().lower()
        if device_name == "auto":
            device_name = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self._device = torch.device(device_name)
            self._tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                local_files_only=local_files_only,
            )
            self._model = AutoModel.from_pretrained(
                model_name,
                local_files_only=local_files_only,
            )
            self._model.to(self._device)
            self._model.eval()
        except Exception as exc:
            raise EmbeddingConfigurationError(
                f"failed to load local transformer embedding model {model_name!r}: {exc}"
            ) from exc
        self._torch = torch
        self._model_name = model_name
        self._batch_size = max(1, int(settings.batch_size))
        self._max_length = max(8, int(options.get("max_length", 256)))
        self._normalize = bool(options.get("normalize", True))

    @staticmethod
    def _mean_pool(last_hidden_state: Any, attention_mask: Any) -> Any:
        mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
        summed = (last_hidden_state * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        return summed / counts

    def embed(self, *, doc_id: str, chunks: Sequence[Chunk]) -> Sequence[Chunk]:
        if not chunks:
            return tuple(chunks)
        embedded: list[Chunk] = []
        for start in range(0, len(chunks), self._batch_size):
            batch = list(chunks[start : start + self._batch_size])
            encoded = self._tokenizer(
                [chunk.text for chunk in batch],
                padding=True,
                truncation=True,
                max_length=self._max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with self._torch.no_grad():
                outputs = self._model(**encoded)
                vectors = self._mean_pool(outputs.last_hidden_state, encoded["attention_mask"])
                if self._normalize:
                    vectors = self._torch.nn.functional.normalize(vectors, p=2, dim=1)
            rows = vectors.detach().cpu().tolist()
            for chunk, vector in zip(batch, rows, strict=False):
                embedded.append(replace(chunk, embedding=tuple(float(value) for value in vector)))
        return tuple(embedded)

    @property
    def model_name(self) -> str:
        return self._model_name

def build_embedding_provider(
    settings: EmbeddingProviderSettings,
) -> OpenAiCompatibleEmbeddingProvider | LocalTransformerEmbeddingProvider | None:
    if not settings.enabled:
        return None
    provider = (settings.provider or "").lower()
    if provider in {"fake", "test", "stub"}:
        return FakeEmbeddingProvider()
    if provider in {
        "sentence-transformers-local",
        "transformers-local",
        "local-transformer",
        "huggingface-local",
    }:
        return LocalTransformerEmbeddingProvider(settings)
    if provider in {"", "openai-compatible", "openai", "dashscope", "qwen"}:
        return OpenAiCompatibleEmbeddingProvider(settings)
    raise EmbeddingConfigurationError(
        f"unsupported embedding provider: {settings.provider!r}"
    )


__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingRequestError",
    "LocalTransformerEmbeddingProvider",
    "OpenAiCompatibleEmbeddingProvider",
    "build_embedding_provider",
]
