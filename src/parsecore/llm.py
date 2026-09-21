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
import re
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

    **The model never writes the text.**  The program splits the paragraph at
    legal boundaries of its own (line breaks, numbered/lettered items,
    NOTE/WARNING/CAUTION blocks), numbers those fragments, and asks the model
    only for the *relations* between them: which fragments form one paragraph
    and in what order.  The result is assembled from the original fragments, so
    a model answer can move boundaries but cannot rewrite a number, a part
    number or a unit — the previous protocol asked for the rewritten text back
    and accepted it whenever it was long enough, which let ``AB-123 torque 25``
    come back as ``AB-124 torque 95``.

    Execution state is **per request**, not per instance.  The refiner is built
    once in ``bootstrap`` and injected into a shared parser, so instance-level
    counters meant that one document's ``reset()`` cleared another document's
    call budget and cache while it was still running.  ``begin_request()``
    returns a :class:`RefinerRequestState` that the caller threads through its
    own parse, and the instance keeps only the configuration.
    """

    _DEFAULT_SYSTEM = (
        "You decide paragraph boundaries in aviation/maintenance manual text. "
        "You are given numbered fragments of one paragraph. Reply with the "
        "relations between them only: which fragments belong together and in "
        "what order. Never retype, translate, correct or summarise the text."
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

    def begin_request(self) -> "RefinerRequestState":
        """Start a per-request state.

        The returned object holds this document's call budget, cache and counters.
        Two documents parsed concurrently through the same shared refiner get
        independent state, so neither can reset the other's budget.
        """

        return RefinerRequestState(
            client=self._client,
            system=self._system,
            max_calls=self._max_calls,
        )

    def reset(self) -> None:
        """Deprecated no-op kept for callers that still call it.

        It exists only so an older call site cannot fail; instance state no longer
        participates in a parse. Use :meth:`begin_request` instead.
        """

        return None

    @property
    def calls_used(self) -> int:
        """Always 0: call accounting lives on the per-request state."""

        return 0

    @property
    def calls_failed(self) -> int:
        """Always 0: call accounting lives on the per-request state."""

        return 0

    def refine_paragraph(self, paragraph: str) -> list[str]:
        """One-off refinement using a fresh request state.

        Convenience for callers that refine a single paragraph; a full parse must
        use :meth:`begin_request` so the call budget spans the whole document.
        """

        return self.begin_request().refine_paragraph(paragraph)


class RefinerRequestState:
    """Per-request refinement state: call budget, cache and counters."""

    _INSTRUCTION = (
        "Fragments of one paragraph, already split at legal boundaries:\n\n"
        "{fragments}\n\n"
        "Group the fragments into paragraphs and order those groups. Reply with "
        "exactly this JSON object:\n"
        '{{"groups": [[1, 2], [3]], "order": [0, 1], "reason": "..."}}\n'
        "- groups: each group lists the fragment ids to join, in reading order\n"
        "- order: the order of the groups, as indexes into groups\n"
        "- every fragment id 1..{count} must appear exactly once\n"
        "- do not retype or rewrite any text\n"
        "Reply with that JSON object only, no commentary, no markdown fence."
    )

    def __init__(
        self,
        *,
        client: QwenDashscopeLlmClient,
        system: str,
        max_calls: int,
    ) -> None:
        self._client = client
        self._system = system
        self._max_calls = max(0, int(max_calls))
        self._cache: dict[str, list[str]] = {}
        self._calls_used = 0
        self._calls_failed = 0
        self._rejections: dict[str, int] = {}
        self._last_rejection = ""

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def calls_failed(self) -> int:
        return self._calls_failed

    @property
    def calls_remaining(self) -> int:
        return max(0, self._max_calls - self._calls_used)

    @property
    def rejections(self) -> Mapping[str, int]:
        """Why refinement was refused, counted by reason code.

        A refusal keeps the original paragraph; the reason is what makes the
        refusal auditable instead of a silent no-op.
        """

        return dict(self._rejections)

    @property
    def last_rejection(self) -> str:
        return self._last_rejection

    def refine_paragraph(self, paragraph: str) -> list[str]:
        """Return refined sub-paragraphs, or ``[paragraph]`` on any failure."""

        key = paragraph
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        fragments = _split_refine_fragments(paragraph)
        if len(fragments) < 2:
            # No legal boundary to move: there is nothing for the model to decide,
            # so this paragraph costs no call at all.
            self._reject("no_legal_boundary")
            self._cache[key] = [paragraph]
            return [paragraph]
        if len(fragments) > _MAX_REFINE_FRAGMENTS:
            self._reject("too_many_fragments")
            self._cache[key] = [paragraph]
            return [paragraph]
        if self._calls_used >= self._max_calls:
            self._reject("call_budget_exhausted")
            self._cache[key] = [paragraph]
            return [paragraph]
        try:
            self._calls_used += 1
            response = self._client.complete(
                self._build_prompt(fragments), system=self._system
            )
        except (LlmRequestError, LlmConfigurationError):
            self._calls_failed += 1
            self._reject("request_failed")
            self._cache[key] = [paragraph]
            return [paragraph]
        parts, reason = _assemble_refined_parts(
            response=response, fragments=fragments, original=paragraph
        )
        if parts is None:
            self._calls_failed += 1
            self._reject(reason)
            self._cache[key] = [paragraph]
            return [paragraph]
        self._cache[key] = parts
        return parts

    def _build_prompt(self, fragments: Sequence["_RefineFragment"]) -> str:
        lines = "\n".join(
            f"{fragment.fragment_id}| {fragment.display}" for fragment in fragments
        )
        return self._INSTRUCTION.format(fragments=lines, count=len(fragments))

    def _reject(self, reason: str) -> None:
        self._last_rejection = reason
        self._rejections[reason] = self._rejections.get(reason, 0) + 1


#: Fragments with their own id and the exact source slice they came from.
@dataclass(slots=True, frozen=True)
class _RefineFragment:
    fragment_id: int
    raw: str
    display: str


#: Boundaries the model may choose between.  Only boundaries the source itself
#: provides are offered, so a split can never land inside a word, a number or a
#: part number -- the program decides what is legal, the model only picks.
_REFINE_MARKER_BOUNDARY = re.compile(
    r"(?<=[.;:!?])\s+(?=(?:\(\d{1,3}\)|\d{1,3}[.)]\s|[a-z][.)]\s"
    r"|note\b|warning\b|caution\b|注\s*[:：]|步骤\s*\d))",
    re.IGNORECASE,
)

#: A prompt with more fragments than this is not worth sending (and a huge
#: paragraph is exactly the case where the answer is least reliable).
_MAX_REFINE_FRAGMENTS = 60


def _split_refine_fragments(paragraph: str) -> tuple[_RefineFragment, ...]:
    """Split at the paragraph's own legal boundaries, keeping every character.

    ``"".join(fragment.raw for fragment in fragments) == paragraph`` holds, so
    the assembled result can only ever be a re-grouping of the original text.
    """

    if not paragraph.strip():
        return ()
    boundaries = {0, len(paragraph)}
    for index, char in enumerate(paragraph):
        if char == "\n" and index + 1 < len(paragraph):
            boundaries.add(index + 1)
    for match in _REFINE_MARKER_BOUNDARY.finditer(paragraph):
        if 0 < match.end() < len(paragraph):
            boundaries.add(match.end())
    ordered = sorted(boundaries)
    fragments: list[_RefineFragment] = []
    pending = ""
    for start, end in zip(ordered, ordered[1:]):
        raw = paragraph[start:end]
        display = raw.strip()
        if not display:
            # Whitespace-only slice: it belongs to a neighbouring fragment and is
            # not worth an id of its own.
            pending += raw
            continue
        fragments.append(
            _RefineFragment(
                fragment_id=len(fragments) + 1, raw=pending + raw, display=display
            )
        )
        pending = ""
    if pending and fragments:
        last = fragments[-1]
        fragments[-1] = _RefineFragment(
            fragment_id=last.fragment_id, raw=last.raw + pending, display=last.display
        )
    if "".join(fragment.raw for fragment in fragments) != paragraph:
        # The slices must reconstruct the source exactly; if they ever do not,
        # there is nothing safe to send and the caller keeps the paragraph.
        return ()
    return tuple(fragments)


def _assemble_refined_parts(
    *,
    response: str,
    fragments: Sequence[_RefineFragment],
    original: str,
) -> tuple[list[str] | None, str]:
    """Turn a model answer into paragraphs, or explain why it was refused.

    The answer is only allowed to say *which* fragments go together and in what
    order.  Anything else -- an unknown id, a dropped or repeated fragment, a
    group that is not a list, an order that is not a permutation -- is refused,
    and the caller keeps the original paragraph.
    """

    plan = _parse_refine_plan(response)
    if plan is None:
        return None, "invalid_response"
    count = len(fragments)
    seen: list[int] = []
    for group in plan.groups:
        if not group:
            return None, "empty_group"
        for fragment_id in group:
            if fragment_id < 1 or fragment_id > count:
                return None, "unknown_fragment"
            seen.append(fragment_id)
    if len(seen) != len(set(seen)):
        return None, "duplicate_fragment"
    if set(seen) != set(range(1, count + 1)):
        return None, "missing_fragment"
    if plan.order is not None and sorted(plan.order) != list(range(len(plan.groups))):
        return None, "invalid_order"
    groups = plan.groups
    if plan.order is not None:
        groups = tuple(groups[index] for index in plan.order)
    parts: list[str] = []
    for group in groups:
        text = "".join(fragments[fragment_id - 1].raw for fragment_id in group).strip()
        if text:
            parts.append(text)
    if not parts:
        return None, "empty_result"
    # Defence in depth. The ids already cover every fragment exactly once, so the
    # result is a permutation of verbatim slices by construction; comparing the
    # multisets fails closed if the assembly itself is ever broken. The comparison
    # is order-insensitive because reordering fragments is a legal answer.
    selected = sorted(_compact(fragments[fragment_id - 1].raw) for group in groups for fragment_id in group)
    expected = sorted(_compact(fragment.raw) for fragment in fragments)
    if selected != expected:
        return None, "assembly_mismatch"
    if len(parts) == 1 and _compact(parts[0]) == _compact(original):
        return None, "no_change"
    return parts, ""


@dataclass(slots=True, frozen=True)
class _RefinePlan:
    groups: tuple[tuple[int, ...], ...]
    order: tuple[int, ...] | None


def _compact(text: str) -> str:
    """The text with all whitespace removed, for verbatim comparison."""

    return "".join(str(text or "").split())


def _parse_refine_plan(response: str) -> _RefinePlan | None:
    """Best-effort extraction of the relations object from an LLM response."""

    payload = _extract_json_object(response)
    if payload is None:
        return None
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        return None
    groups: list[tuple[int, ...]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, list):
            return None
        group: list[int] = []
        for value in raw_group:
            # A stringified id is a formatting slip, not a semantic one.
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            group.append(value)
        groups.append(tuple(group))
    raw_order = payload.get("order")
    order: tuple[int, ...] | None = None
    if raw_order is not None:
        if not isinstance(raw_order, list) or not raw_order:
            return None
        order_values: list[int] = []
        for value in raw_order:
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value.strip())
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            order_values.append(value)
        order = tuple(order_values)
    return _RefinePlan(groups=tuple(groups), order=order)


def _extract_json_object(response: str) -> Mapping[str, Any] | None:
    """Read the first JSON object in a response, tolerating a markdown fence."""

    text = str(response or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None

