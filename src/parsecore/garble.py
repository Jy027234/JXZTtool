from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_CID_TOKEN_RE = re.compile(r"\(cid:\d+\)")
_PDF_NAME_TOKEN_RE = re.compile(r"/(?:(?:\d+)|(?:i\d+))(?![\w])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")


@dataclass(slots=True, frozen=True)
class GarbleSignals:
    total_chars: int
    total_cid_tokens: int
    total_pdf_name_tokens: int
    control_char_count: int
    printable_ratio: float
    repeated_short_token_ratio: float
    cjk_ratio: float
    latin_ratio: float


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def analyze_text_garble(text: str) -> GarbleSignals:
    content = str(text or "")
    total_chars = len(content)
    cid_tokens = len(_CID_TOKEN_RE.findall(content))
    pdf_name_tokens = len(_PDF_NAME_TOKEN_RE.findall(content))
    control_char_count = len(_CONTROL_CHAR_RE.findall(content))

    printable_count = sum(1 for ch in content if ch.isprintable() or ch in "\r\n\t")
    printable_ratio = _safe_ratio(printable_count, total_chars)

    tokens = [token.lower() for token in _TOKEN_RE.findall(content)]
    short_tokens = [token for token in tokens if len(token) <= 2]
    repeated_short = 0
    if short_tokens:
        counts: dict[str, int] = {}
        for token in short_tokens:
            counts[token] = counts.get(token, 0) + 1
        repeated_short = sum(count for count in counts.values() if count >= 3)
    repeated_short_ratio = _safe_ratio(repeated_short, len(tokens))

    cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
    latin_count = sum(1 for ch in content if ("a" <= ch.lower() <= "z"))
    cjk_ratio = _safe_ratio(cjk_count, total_chars)
    latin_ratio = _safe_ratio(latin_count, total_chars)

    return GarbleSignals(
        total_chars=total_chars,
        total_cid_tokens=cid_tokens,
        total_pdf_name_tokens=pdf_name_tokens,
        control_char_count=control_char_count,
        printable_ratio=round(printable_ratio, 6),
        repeated_short_token_ratio=round(repeated_short_ratio, 6),
        cjk_ratio=round(cjk_ratio, 6),
        latin_ratio=round(latin_ratio, 6),
    )


def analyze_text_fragments_garble(fragments: Iterable[str]) -> GarbleSignals:
    joined = "\n".join(str(fragment or "") for fragment in fragments)
    return analyze_text_garble(joined)


def detect_page_garble_reason(
    text: str,
    *,
    min_cid_tokens: int,
    min_cid_char_ratio: float,
    min_pdf_name_tokens: int = 5,
    min_pdf_name_char_ratio: float = 0.08,
    min_printable_ratio: float = 0.75,
    max_control_char_ratio: float = 0.03,
    min_repeated_short_ratio: float = 0.35,
) -> str | None:
    stripped = str(text or "").strip()
    if not stripped:
        return "empty_text"

    signals = analyze_text_garble(stripped)
    cid_chars = sum(len(match.group(0)) for match in _CID_TOKEN_RE.finditer(stripped))
    if signals.total_cid_tokens >= min_cid_tokens and _safe_ratio(cid_chars, signals.total_chars) >= min_cid_char_ratio:
        return "cid_dense"

    pdf_name_chars = sum(len(match.group(0)) for match in _PDF_NAME_TOKEN_RE.finditer(stripped))
    if (
        signals.total_pdf_name_tokens >= min_pdf_name_tokens
        and _safe_ratio(pdf_name_chars, signals.total_chars) >= min_pdf_name_char_ratio
    ):
        return "pdf_name_dense"

    control_char_ratio = _safe_ratio(signals.control_char_count, signals.total_chars)
    if control_char_ratio >= max_control_char_ratio:
        return "control_char_dense"

    if signals.printable_ratio < min_printable_ratio:
        return "low_printable_ratio"

    if signals.repeated_short_token_ratio >= min_repeated_short_ratio:
        return "repeated_short_tokens"

    # Mixed-script anomaly where one script dominates while the other is sparse.
    if signals.cjk_ratio > 0.15 and 0 < signals.latin_ratio < 0.02:
        return "script_distribution_anomaly"

    return None
