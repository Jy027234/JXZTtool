"""Config-managed second-stage reranking providers.

The supported DashScope-compatible route follows the live model-router
contract used by Qwen rerank models:

``POST /rerank`` with ``input.query`` / ``input.documents`` and an
``output.results`` response containing ``index`` / ``relevance_score``.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from .config import RerankProviderSettings
from .contracts import RerankProvider
from .models import RerankScore
from .stubs import FakeRerankProvider


class RerankConfigurationError(RuntimeError):
    """Raised when an enabled rerank provider lacks safe configuration."""


class RerankRequestError(RuntimeError):
    """Raised when a rerank response cannot safely reorder candidates."""


class DashScopeCompatibleRerankProvider(RerankProvider):
    """Call a DashScope-compatible ``/rerank`` endpoint without leaking keys."""

    def __init__(self, settings: RerankProviderSettings) -> None:
        if not settings.enabled:
            raise RerankConfigurationError(
                "Rerank provider is disabled; set providers.rerank.enabled = true"
            )
        if not settings.base_url or not settings.model:
            raise RerankConfigurationError(
                "providers.rerank requires both base_url and model"
            )
        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            raise RerankConfigurationError(
                f"environment variable {settings.api_key_env} is empty; cannot call rerank"
            )
        self._settings = settings
        self._api_key = api_key

    def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
    ) -> Sequence[RerankScore]:
        if not documents:
            return ()
        endpoint = self._settings.base_url.rstrip("/") + "/rerank"
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "input": {
                "query": str(query),
                "documents": [str(document) for document in documents],
            },
            "parameters": {
                **dict(self._settings.options or {}),
                "top_n": len(documents),
                "return_documents": False,
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        attempts = max(1, int(self._settings.max_retries) + 1)
        for _attempt in range(attempts):
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(  # noqa: S310 - explicit configured HTTPS endpoint
                    request,
                    timeout=self._settings.timeout_seconds,
                ) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return self._parse_results(data, candidate_count=len(documents))
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                last_error = exc
                continue
        raise RerankRequestError(
            f"rerank call failed after {attempts} attempts: {type(last_error).__name__}"
        )

    @staticmethod
    def _parse_results(data: Any, *, candidate_count: int) -> tuple[RerankScore, ...]:
        if not isinstance(data, Mapping):
            raise RerankRequestError("rerank response must be a JSON object")
        output = data.get("output")
        if not isinstance(output, Mapping):
            raise RerankRequestError("rerank response missing output")
        raw_results = output.get("results")
        if not isinstance(raw_results, list):
            raise RerankRequestError("rerank response missing output.results")

        parsed: list[RerankScore] = []
        seen_indexes: set[int] = set()
        for item in raw_results:
            if not isinstance(item, Mapping):
                raise RerankRequestError("rerank result must be an object")
            raw_index = item.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise RerankRequestError("rerank result index must be an integer")
            try:
                score = float(item.get("relevance_score"))
            except (TypeError, ValueError) as exc:
                raise RerankRequestError("rerank result missing numeric score") from exc
            index = raw_index
            if index < 0 or index >= candidate_count:
                raise RerankRequestError("rerank result index is outside the candidate set")
            if index in seen_indexes:
                raise RerankRequestError("rerank response contains duplicate candidate indexes")
            if not math.isfinite(score):
                raise RerankRequestError("rerank result score must be finite")
            seen_indexes.add(index)
            parsed.append(RerankScore(index=index, score=score))
        if not parsed:
            raise RerankRequestError("rerank response contains no results")
        return tuple(parsed)


def build_rerank_provider(
    settings: RerankProviderSettings,
) -> RerankProvider | None:
    if not settings.enabled:
        return None
    provider = str(settings.provider or "").strip().lower()
    if provider in {"fake", "test", "stub"}:
        return FakeRerankProvider()
    if provider in {
        "dashscope-compatible",
        "dashscope-rerank",
        "qwen-rerank",
        "aliyun-rerank",
        "model-router-rerank",
    }:
        return DashScopeCompatibleRerankProvider(settings)
    raise RerankConfigurationError(
        f"unsupported rerank provider: {settings.provider!r}"
    )


__all__ = [
    "DashScopeCompatibleRerankProvider",
    "RerankConfigurationError",
    "RerankRequestError",
    "build_rerank_provider",
]
