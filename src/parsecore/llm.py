"""LLM provider abstraction for semantic block decisions.

Phase A4 scaffolding only. The pipeline does not yet call into this module;
parsers will start invoking it once the dependency-side rewrite (A3) lands.

Design notes:

- `BlockClassifier` and `BoundaryRefiner` are the two protocols the parser
  pipeline will consume. Implementations can be remote (cloud LLM) or local
  (small on-device model) without touching call sites.
- `QwenDashscopeLlmClient` is the first concrete implementation, targeting the
  DashScope OpenAI-compatible endpoint. It uses ``urllib.request`` so we do not
  introduce a hard dependency on ``httpx`` / ``openai`` at this stage.
- API keys are read from the environment variable named in ``api_key_env``.
  In embedded-SDK mode, the host application should set this env var via its
  own credential management before constructing the client.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .config import LlmProviderSettings


@dataclass(slots=True, frozen=True)
class BlockCandidate:
    """A tentative block emitted by the dependency-side parser.

    The classifier sees these and returns a structural label so downstream
    splitting / merging can be driven by semantics rather than regex.
    """

    text: str
    page_number: int
    layout_hints: Mapping[str, Any]


@dataclass(slots=True, frozen=True)
class BlockLabel:
    label: str
    confidence: float


@runtime_checkable
class BlockClassifier(Protocol):
    def classify(self, candidates: Sequence[BlockCandidate]) -> Sequence[BlockLabel]: ...


@runtime_checkable
class BoundaryRefiner(Protocol):
    def refine(
        self,
        candidates: Sequence[BlockCandidate],
        labels: Sequence[BlockLabel],
    ) -> Sequence[BlockCandidate]: ...


class LlmConfigurationError(RuntimeError):
    """Raised when the LLM provider is not properly configured."""


class LlmRequestError(RuntimeError):
    """Raised after retries are exhausted on a remote LLM call."""


class QwenDashscopeLlmClient:
    """Minimal DashScope (OpenAI-compatible) chat completion client.

    This client is intentionally transport-light: it talks to the
    ``/chat/completions`` endpoint with ``urllib.request`` and exposes a single
    ``complete(prompt, system=None)`` helper. Higher-level classifier /
    refiner implementations will compose this client.
    """

    def __init__(self, settings: LlmProviderSettings) -> None:
        if not settings.enabled:
            raise LlmConfigurationError(
                "LLM provider is disabled; set providers.llm.enabled = true to use it"
            )
        if not settings.base_url or not settings.model:
            raise LlmConfigurationError(
                "providers.llm requires both base_url and model to be set"
            )
        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            raise LlmConfigurationError(
                f"environment variable {settings.api_key_env} is empty; cannot call LLM"
            )
        self._settings = settings
        self._api_key = api_key

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        endpoint = self._settings.base_url.rstrip("/") + "/chat/completions"
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
        }
        for key in ("temperature", "top_p", "max_tokens"):
            if key in self._settings.options:
                payload[key] = self._settings.options[key]

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        attempts = max(1, self._settings.max_retries + 1)
        for attempt in range(attempts):
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(  # noqa: S310 - explicit https endpoint
                    request, timeout=self._settings.timeout_seconds
                ) as response:
                    raw = response.read().decode("utf-8")
                data = json.loads(raw)
                choices = data.get("choices") or []
                if not choices:
                    raise LlmRequestError("LLM response has no choices")
                content = (choices[0].get("message") or {}).get("content")
                if not isinstance(content, str):
                    raise LlmRequestError("LLM response content is not a string")
                return content
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                break

        raise LlmRequestError(f"LLM call failed after {attempts} attempts: {last_error}")


def build_llm_client(settings: LlmProviderSettings) -> QwenDashscopeLlmClient | None:
    """Factory used by future parser stages.

    Returns ``None`` when the provider is disabled so callers can fall back to
    pure dependency-side logic without raising.
    """

    if not settings.enabled:
        return None
    provider = (settings.provider or "").lower()
    if provider in {"", "qwen-dashscope", "dashscope", "qwen"}:
        return QwenDashscopeLlmClient(settings)
    raise LlmConfigurationError(f"unsupported llm provider: {settings.provider!r}")


# --- A4 hookup: LLM-backed boundary refiner ----------------------------------


class LlmBoundaryRefiner:
    """Refines low-confidence paragraph boundaries with an LLM.

    Used by ``PdfTextParser`` as the final step of the per-page splitter
    pipeline. Only paragraphs flagged as low-confidence are sent to the LLM;
    everything else passes through unchanged. On any failure (network,
    malformed JSON, hard call cap reached) the original paragraph is kept,
    so this layer can never cause a parse regression.
    """

    _DEFAULT_SYSTEM = (
        "You split aviation/maintenance manual paragraphs into structurally "
        "coherent sub-paragraphs. Preserve original wording exactly; only insert "
        "boundaries between numbered/lettered items, NOTE/WARNING/CAUTION blocks, "
        "and SB-style procedural sections. Output a strict JSON array of strings."
    )

    def __init__(
        self,
        client: QwenDashscopeLlmClient,
        *,
        max_calls_per_doc: int = 50,
        system_prompt: str | None = None,
    ) -> None:
        self._client = client
        self._max_calls = max(0, int(max_calls_per_doc))
        self._system = system_prompt or self._DEFAULT_SYSTEM
        self._cache: dict[str, list[str]] = {}
        self._calls_used = 0
        self._calls_failed = 0

    def reset(self) -> None:
        """Clear per-document state. Call once per document."""

        self._cache.clear()
        self._calls_used = 0
        self._calls_failed = 0

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def calls_failed(self) -> int:
        return self._calls_failed

    def refine_paragraph(self, paragraph: str) -> list[str]:
        """Return refined sub-paragraphs, or ``[paragraph]`` on any failure."""

        key = paragraph
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if self._calls_used >= self._max_calls:
            return [paragraph]
        prompt = (
            "Split the following paragraph into structurally coherent "
            "sub-paragraphs. Preserve text exactly. Respond ONLY with a JSON "
            "array of strings, no commentary, no markdown fence.\n\n"
            "PARAGRAPH:\n" + paragraph
        )
        try:
            self._calls_used += 1
            response = self._client.complete(prompt, system=self._system)
        except (LlmRequestError, LlmConfigurationError):
            self._calls_failed += 1
            self._cache[key] = [paragraph]
            return [paragraph]
        parts = _parse_json_string_array(response)
        if not parts:
            self._calls_failed += 1
            self._cache[key] = [paragraph]
            return [paragraph]
        # Sanity guard: refined output must collectively contain most of the
        # original characters; otherwise the model rewrote content. Reject.
        original_compact = "".join(paragraph.split())
        refined_compact = "".join("".join(part.split()) for part in parts)
        if len(refined_compact) < int(len(original_compact) * 0.85):
            self._calls_failed += 1
            self._cache[key] = [paragraph]
            return [paragraph]
        self._cache[key] = parts
        return parts


def _parse_json_string_array(response: str) -> list[str] | None:
    """Best-effort extraction of a JSON string array from an LLM response."""

    text = response.strip()
    if text.startswith("```"):
        # Strip markdown fence if the model added one despite the instruction.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    payload = text[start : end + 1]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    items: list[str] = []
    for entry in data:
        if not isinstance(entry, str):
            return None
        stripped = entry.strip()
        if stripped:
            items.append(stripped)
    return items or None

