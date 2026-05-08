from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True, frozen=True)
class OcrPageDecision:
    page_number: int
    attempted: bool
    fallback_used: bool
    rejected: bool
    failed: bool
    attempt_reason: str | None
    acceptance_reason: str | None
    rejection_reason: str | None
    error_reason: str | None
    native_text_token_count: int
    final_text_token_count: int


@dataclass(slots=True, frozen=True)
class OcrDecisionTrace:
    attempted_pages: int
    fallback_pages: int
    rejected_pages: int
    failed_pages: int
    attempt_reasons: tuple[str, ...]
    acceptance_reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    error_reasons: tuple[str, ...]
    native_text_token_count: int
    final_text_token_count: int
    pages: tuple[OcrPageDecision, ...]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_ocr_decision_trace(blocks: Iterable[Any]) -> OcrDecisionTrace:
    page_state: dict[int, dict[str, Any]] = {}
    for block in blocks:
        metadata = getattr(block, "metadata", {}) or {}
        page_number = _as_int(metadata.get("page", 0))
        entry = page_state.setdefault(
            page_number,
            {
                "attempted": False,
                "fallback_used": False,
                "failed": False,
                "attempt_reason": None,
                "acceptance_reason": None,
                "rejection_reason": None,
                "error_reason": None,
                "native_text_token_count": 0,
                "final_text_token_count": 0,
            },
        )

        if bool(metadata.get("ocr_attempted")):
            entry["attempted"] = True
        if bool(metadata.get("ocr_fallback_used")):
            entry["fallback_used"] = True
        if metadata.get("ocr_error_reason"):
            entry["failed"] = True

        attempt_reason = metadata.get("ocr_attempt_reason")
        if attempt_reason and not entry["attempt_reason"]:
            entry["attempt_reason"] = str(attempt_reason)

        acceptance_reason = metadata.get("ocr_acceptance_reason")
        if acceptance_reason and not entry["acceptance_reason"]:
            entry["acceptance_reason"] = str(acceptance_reason)

        rejection_reason = metadata.get("ocr_rejection_reason")
        if rejection_reason and not entry["rejection_reason"]:
            entry["rejection_reason"] = str(rejection_reason)

        error_reason = metadata.get("ocr_error_reason")
        if error_reason and not entry["error_reason"]:
            entry["error_reason"] = str(error_reason)

        entry["native_text_token_count"] = max(
            int(entry["native_text_token_count"]),
            _as_int(metadata.get("native_text_token_count", 0)),
        )
        entry["final_text_token_count"] = max(
            int(entry["final_text_token_count"]),
            _as_int(metadata.get("final_text_token_count", 0)),
        )

    pages: list[OcrPageDecision] = []
    attempt_reasons: set[str] = set()
    acceptance_reasons: set[str] = set()
    rejection_reasons: set[str] = set()
    error_reasons: set[str] = set()

    attempted_pages = 0
    fallback_pages = 0
    rejected_pages = 0
    failed_pages = 0
    native_tokens_total = 0
    final_tokens_total = 0

    for page_number in sorted(page_state):
        state = page_state[page_number]
        attempted = bool(state["attempted"])
        fallback_used = bool(state["fallback_used"])
        failed = bool(state["failed"])
        rejected = attempted and not fallback_used

        if attempted:
            attempted_pages += 1
        if fallback_used:
            fallback_pages += 1
        if rejected:
            rejected_pages += 1
        if failed:
            failed_pages += 1

        native_tokens_total += int(state["native_text_token_count"])
        final_tokens_total += int(state["final_text_token_count"])

        if state["attempt_reason"]:
            attempt_reasons.add(str(state["attempt_reason"]))
        if state["acceptance_reason"]:
            acceptance_reasons.add(str(state["acceptance_reason"]))
        if state["rejection_reason"]:
            rejection_reasons.add(str(state["rejection_reason"]))
        if state["error_reason"]:
            error_reasons.add(str(state["error_reason"]))

        pages.append(
            OcrPageDecision(
                page_number=page_number,
                attempted=attempted,
                fallback_used=fallback_used,
                rejected=rejected,
                failed=failed,
                attempt_reason=state["attempt_reason"],
                acceptance_reason=state["acceptance_reason"],
                rejection_reason=state["rejection_reason"],
                error_reason=state["error_reason"],
                native_text_token_count=int(state["native_text_token_count"]),
                final_text_token_count=int(state["final_text_token_count"]),
            )
        )

    return OcrDecisionTrace(
        attempted_pages=attempted_pages,
        fallback_pages=fallback_pages,
        rejected_pages=rejected_pages,
        failed_pages=failed_pages,
        attempt_reasons=tuple(sorted(attempt_reasons)),
        acceptance_reasons=tuple(sorted(acceptance_reasons)),
        rejection_reasons=tuple(sorted(rejection_reasons)),
        error_reasons=tuple(sorted(error_reasons)),
        native_text_token_count=native_tokens_total,
        final_text_token_count=final_tokens_total,
        pages=tuple(pages),
    )


def ocr_decision_trace_payload(trace: OcrDecisionTrace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ocr_attempted_pages": trace.attempted_pages,
        "ocr_fallback_pages": trace.fallback_pages,
        "ocr_rejected_pages": trace.rejected_pages,
        "ocr_failed_pages": trace.failed_pages,
        "native_text_token_count": trace.native_text_token_count,
        "final_text_token_count": trace.final_text_token_count,
    }
    if trace.attempt_reasons:
        payload["ocr_attempt_reasons"] = list(trace.attempt_reasons)
    if trace.acceptance_reasons:
        payload["ocr_acceptance_reasons"] = list(trace.acceptance_reasons)
    if trace.rejection_reasons:
        payload["ocr_rejection_reasons"] = list(trace.rejection_reasons)
    if trace.error_reasons:
        payload["ocr_error_reasons"] = list(trace.error_reasons)
    return payload


__all__ = [
    "OcrDecisionTrace",
    "OcrPageDecision",
    "build_ocr_decision_trace",
    "ocr_decision_trace_payload",
]
