"""Conservative, versioned extraction for the CAAC approved catalog.

Every projected row remains a candidate until a downstream quality gate and
review promote it.  The extractor preserves coordinate evidence and supports
owned/context page windows so large runs can resume without emitting overlap.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..catalog_contracts import (
    CAAC_APPROVED_CATALOG_EXTRACTOR,
    CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION,
    CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    CAAC_APPROVED_CATALOG_V2_EXTRACTOR_VERSION,
    CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
)
from ..catalog_omissions import (
    SOURCE_OMISSION_FIELD_PATH,
    normalize_source_omission_review,
    source_omission_review_index,
)
from .base import CatalogExtractionResult, CatalogExtractor, CatalogPage


@dataclass(frozen=True, slots=True)
class CatalogSectionSpan:
    section_key: str
    title: str
    pdf_page_start: int
    pdf_page_end: int
    signals: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ColumnBoundary:
    key: str
    left: float
    right: float


@dataclass(frozen=True, slots=True)
class RowAnchor:
    row_number: int
    pdf_page: int
    top: float
    bottom: float
    words: tuple[Mapping[str, Any], ...]


_SECTION_HEADER_SPECS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "tc": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.modelOriginal", ("型别", "型号")),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "foreign_product_recognition": (
        ("holder.nameOriginal", ("持有人", "持证人")),
        ("item.nameOriginal", ("名称",)),
        ("item.modelOriginal", ("型号",)),
        ("approval.numberOriginal", ("国内证件编号",)),
        ("attributes.foreignCountry", ("颁发国",)),
        ("attributes.foreignApprovalNumber", ("颁发国证件编号",)),
        ("approval.latestApprovedOn", ("颁发国批准日期",)),
    ),
    "tda": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持有人", "持证人")),
        ("item.modelOriginal", ("型号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "pc": (
        ("approval.numberOriginal", ("项目单编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.modelOriginal", ("型号/型别", "型号型别")),
        ("item.descriptionOriginal", ("型号证书编号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "stc": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("attributes.originalModel", ("原产品型号",)),
        ("attributes.originalApprovalNumber", ("原产品型号", "合格证编号")),
        ("item.modelOriginal", ("现产品型号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "mda": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("attributes.modelCertificateNumber", ("型号证书编号",)),
        ("item.productTypeOriginal", ("产品型别",)),
        ("item.descriptionOriginal", ("设计更改/修理设计描述", "设计更改修理设计描述")),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "pma_holder": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("制造人名称",)),
        ("item.descriptionOriginal", ("批准的质量手册",)),
        ("attributes.qualityManualNumber", ("编号",)),
        ("attributes.qualityManualRevision", ("版次以及以后批准的版次",)),
        ("approval.latestApprovedOn", ("批准日期",)),
    ),
    "pma_item": (
        ("approval.numberOriginal", ("项目单编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.nameOriginal", ("项目名称",)),
        ("item.modelOriginal", ("型号",)),
        ("item.partNumberOriginal", ("件号",)),
        ("item.replacedManufacturerOriginal", ("被替换零部件制造人",)),
        ("item.replacedPartNumberOriginal", ("被替换零部件件号",)),
        ("approval.expiresOn", ("有效期至",)),
    ),
    "ctsoa": (
        ("approval.numberOriginal", ("项目单编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.nameOriginal", ("项目名称",)),
        ("item.modelOriginal", ("型号",)),
        ("item.ctsoCodesOriginal", ("批准标记的CTSO", "批准标记CTSO")),
        ("approval.expiresOn", ("有效期至",)),
    ),
    "foreign_tso_recognition": (
        ("holder.nameOriginal", ("持有人", "持证人")),
        ("item.nameOriginal", ("项目名称",)),
        ("item.modelOriginal", ("认可型号",)),
        ("approval.numberOriginal", ("对应TSOA证件号",)),
        ("item.ctsoCodesOriginal", ("对应TSO编号认可当局", "对应TSO编号认可的当局", "对应TSO编号")),
        ("attributes.recognitionNumberOriginal", ("认可编号",)),
        ("approval.latestApprovedOn", ("认可日期",)),
    ),
    "vtc": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.productTypeOriginal", ("产品类型",)),
        ("item.modelOriginal", ("型别", "型号")),
        ("attributes.authority", ("所属当局",)),
        ("attributes.foreignApprovalNumber", ("国外审定证件号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "vstc": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.productTypeOriginal", ("产品类型",)),
        ("item.modelOriginal", ("适用机型",)),
        ("item.descriptionOriginal", ("叙述",)),
        ("attributes.authority", ("所属当局",)),
        ("attributes.foreignApprovalNumber", ("国外审定证件号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
    "vda": (
        ("approval.numberOriginal", ("证件编号",)),
        ("holder.nameOriginal", ("持证人", "持有人")),
        ("item.productTypeOriginal", ("产品类别",)),
        ("item.nameOriginal", ("产品名称",)),
        ("item.modelOriginal", ("型（件）号", "型件号")),
        ("item.ctsoCodesOriginal", ("CTSO",)),
        ("attributes.authority", ("所属当局",)),
        ("attributes.foreignApprovalNumber", ("国外审定证件号",)),
        ("approval.latestApprovedOn", ("最新批准日期",)),
    ),
}


_ROW_NUMBER_RE = re.compile(r"^[0-9]{1,8}$")
_SPACE_RE = re.compile(r"\s+")
_CJK_WRAP_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_DATE_RE = re.compile(r"^(?P<year>\d{4})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?$")
_DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(?P<year>\d{4})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?(?!\d)"
)
_CTSO_CODE_RE = re.compile(r"\bCTSO[-\s]?C[0-9]{1,4}[A-Z]?\b", re.IGNORECASE)
_APPROVAL_TOKEN_SUFFIX = r"[A-Z0-9][A-Z0-9._()/-]*"
_APPROVAL_TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "tc": re.compile(rf"(?:TC|TDA){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "foreign_product_recognition": re.compile(rf"TC{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "tda": re.compile(rf"TDA{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "pc": re.compile(rf"(?:PC|LSA){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "stc": re.compile(rf"STC{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    # MAD is retained because it is the source spelling on one sampled row.
    "mda": re.compile(rf"(?:MDA|MAD){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "pma_holder": re.compile(rf"PMA{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "pma_item": re.compile(rf"PMA{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "ctsoa": re.compile(rf"(?:CTSOA|TSOA){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "foreign_tso_recognition": re.compile(rf"(?:CTSOA|TSOA){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "vtc": re.compile(rf"(?:VTC|TV){_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "vstc": re.compile(rf"VSTC{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
    "vda": re.compile(rf"VDA{_APPROVAL_TOKEN_SUFFIX}", re.IGNORECASE),
}
_APPROVAL_OVERFLOW_DESTINATION = {
    "foreign_tso_recognition": "item.modelOriginal",
    "vda": "holder.nameOriginal",
    "vstc": "holder.nameOriginal",
}
_APPROVAL_IDENTIFIER_ISOLATION_SECTIONS = frozenset(
    {"foreign_tso_recognition", "vda", "vstc"}
)

# These columns are allowed to contain a visually wrapped value.  The
# ordinary row band is still used for identifier/date columns, but a wrapped
# item value has to be partitioned as a *cell block* around the row anchor.
# This is important on the TC pages where one approval row can list several
# models and the first/last model line is commonly above/below the serial
# number baseline.
_MULTILINE_CELL_KEYS = frozenset(
    {
        "item.modelOriginal",
        "item.productTypeOriginal",
        "attributes.originalModel",
    }
)

_APPROVAL_TYPE_BY_SECTION = {
    "pma_holder": "pma",
    "pma_item": "pma",
}
_RECORD_KIND_BY_SECTION = {
    "tc": "certificate",
    "foreign_product_recognition": "recognition",
    "tda": "project",
    "pc": "project",
    "stc": "project",
    "mda": "project",
    "pma_holder": "holder_approval",
    "pma_item": "approved_item",
    "ctsoa": "approved_item",
    "foreign_tso_recognition": "recognition",
    "vtc": "recognition",
    "vstc": "recognition",
    "vda": "recognition",
}


def _normalized_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").replace("\u3000", " ")).strip()


def _normalize_holder_original(value: Any) -> str:
    """Remove layout-only spaces between CJK characters in holder names."""

    return _CJK_WRAP_SPACE_RE.sub("", _normalized_text(value))


def _signal(code: str, *, page: int | None = None, message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "severity": "warning",
        "message": message or code,
    }
    if page is not None:
        payload["pdfPage"] = page
    return payload


def _first_nonempty_line(text: str) -> str:
    for line in str(text or "").splitlines():
        normalized = _normalized_text(line)
        if normalized:
            return normalized
    return ""


def _section_from_header(header: str) -> tuple[str, str] | None:
    """Match only a repeated section title/header, never arbitrary row text."""

    if "已获批准的" in header and "PMA" in header and "产品" in header:
        return "pma_item", header
    if "零部件制造人批准书" in header or "PMA制造人" in header or "PMA持证人" in header:
        return "pma_holder", header
    if "获得国外当局认可的技术标准规定项目" in header:
        return "foreign_tso_recognition", header
    if "获得国外认可的民用航空产品" in header:
        return "foreign_product_recognition", header
    if "技术标准规定项目批准书" in header or re.search(r"\bCTSOA\b", header, re.IGNORECASE):
        return "ctsoa", header
    if "补充型号认可证" in header or re.search(r"\bVSTC\b", header, re.IGNORECASE):
        return "vstc", header
    if "型号认可证" in header or re.search(r"(?<!VS)\bVTC\b", header, re.IGNORECASE):
        return "vtc", header
    if "设计批准认可证" in header or re.search(r"\bVDA\b", header, re.IGNORECASE):
        return "vda", header
    if "补充型号合格证" in header or re.search(r"\bSTC\b", header, re.IGNORECASE):
        return "stc", header
    if "改装设计批准书" in header or re.search(r"\bMDA\b", header, re.IGNORECASE):
        return "mda", header
    if "型号设计批准书" in header or re.search(r"\bTDA\b", header, re.IGNORECASE):
        return "tda", header
    if "生产许可证" in header or re.search(r"\bPC\b", header, re.IGNORECASE):
        return "pc", header
    if "型号合格证" in header or re.search(r"(?<!V)\bTC\b", header, re.IGNORECASE):
        return "tc", header
    return None


def detect_catalog_sections(pages: Sequence[CatalogPage]) -> tuple[CatalogSectionSpan, ...]:
    """Detect section spans from titles/headers, preserving physical PDF pages."""

    matches: list[tuple[int, str, str]] = []
    for page in pages:
        matched = _section_from_header(_first_nonempty_line(page.text))
        if matched is not None:
            matches.append((page.pdf_page, matched[0], matched[1]))

    spans: list[CatalogSectionSpan] = []
    for index, (page_number, section_key, title) in enumerate(matches):
        next_page = matches[index + 1][0] - 1 if index + 1 < len(matches) else page_number
        if next_page < page_number:
            next_page = page_number
        spans.append(
            CatalogSectionSpan(
                section_key=section_key,
                title=title,
                pdf_page_start=page_number,
                pdf_page_end=next_page,
            )
        )
    return tuple(spans)


def _word_number(value: Any, *, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _word_bounds(word: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    x0 = _word_number(word.get("x0"))
    x1 = _word_number(word.get("x1"))
    top = _word_number(word.get("top"))
    bottom = _word_number(word.get("bottom"))
    if None in (x0, x1, top, bottom):
        return None
    return (float(x0), float(x1), float(top), float(bottom))


def anchor_row_numbers(
    page: CatalogPage,
    *,
    first_column_right: float | None = None,
    y_tolerance: float = 2.5,
) -> tuple[tuple[RowAnchor, ...], tuple[Mapping[str, Any], ...]]:
    """Find numeric row anchors only inside the first visual column."""

    rows: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    signals: list[Mapping[str, Any]] = []
    if first_column_right is None:
        header_bounds = next(
            (
                _word_bounds(word)
                for word in page.words
                if _normalized_text(word.get("text") or word.get("textOriginal")) == "序号"
                and _word_bounds(word) is not None
            ),
            None,
        )
        if header_bounds is None:
            signals.append(
                _signal(
                    "header_not_recognized",
                    page=page.pdf_page,
                    message="序号 header lacks usable coordinates",
                )
            )
            return (), tuple(signals)
        first_column_right = header_bounds[1] + 8.0
    for word in page.words:
        bounds = _word_bounds(word)
        if bounds is None:
            signals.append(_signal("record_boundary_uncertain", page=page.pdf_page, message="word lacks usable coordinates"))
            continue
        _, _, top, _ = bounds
        bucket = int(round(top / max(y_tolerance, 0.5)))
        rows[bucket].append(word)

    anchors: list[RowAnchor] = []
    for row_words in rows.values():
        ordered = sorted(row_words, key=lambda word: _word_bounds(word)[0] if _word_bounds(word) else float("inf"))
        candidates: list[tuple[int, Mapping[str, Any], tuple[float, float, float, float]]] = []
        for word in ordered:
            bounds = _word_bounds(word)
            if bounds is None:
                continue
            x0, x1, _, _ = bounds
            text = _normalized_text(word.get("text") or word.get("textOriginal"))
            if x0 <= first_column_right and x1 <= first_column_right + 8 and _ROW_NUMBER_RE.fullmatch(text):
                candidates.append((int(text), word, bounds))
        if len(candidates) != 1:
            if candidates:
                signals.append(_signal("row_anchor_out_of_column", page=page.pdf_page, message="row has multiple numeric anchors"))
            continue
        row_number, _, first_bounds = candidates[0]
        top = min((_word_bounds(word)[2] for word in ordered if _word_bounds(word)), default=first_bounds[2])
        bottom = max((_word_bounds(word)[3] for word in ordered if _word_bounds(word)), default=first_bounds[3])
        anchors.append(RowAnchor(row_number, page.pdf_page, top, bottom, tuple(ordered)))

    if page.words and not anchors:
        signals.append(_signal("record_boundary_uncertain", page=page.pdf_page, message="no coordinate-safe row anchor found"))
    return tuple(sorted(anchors, key=lambda anchor: (anchor.top, anchor.row_number))), tuple(signals)


def map_words_to_columns(
    words: Sequence[Mapping[str, Any]],
    headers: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], tuple[Mapping[str, Any], ...]]:
    """Map words to column intervals derived from header x-centres."""

    normalized_headers: list[tuple[str, float]] = []
    signals: list[Mapping[str, Any]] = []
    for header in headers:
        bounds = _word_bounds(header)
        key = _normalized_text(header.get("key") or header.get("text"))
        if bounds is None or not key:
            signals.append(_signal("header_not_recognized", message="header lacks key or coordinates"))
            continue
        normalized_headers.append((key, (bounds[0] + bounds[1]) / 2.0))
    normalized_headers.sort(key=lambda item: item[1])
    if not normalized_headers:
        return {}, tuple(signals or [_signal("header_not_recognized")])

    boundaries: list[ColumnBoundary] = []
    for index, (key, centre) in enumerate(normalized_headers):
        left = float("-inf") if index == 0 else (normalized_headers[index - 1][1] + centre) / 2.0
        right = float("inf") if index == len(normalized_headers) - 1 else (centre + normalized_headers[index + 1][1]) / 2.0
        boundaries.append(ColumnBoundary(key, left, right))

    mapped: dict[str, list[Mapping[str, Any]]] = {boundary.key: [] for boundary in boundaries}
    for word in words:
        bounds = _word_bounds(word)
        if bounds is None:
            signals.append(_signal("column_shift_suspected", message="word lacks coordinates"))
            continue
        centre = (bounds[0] + bounds[1]) / 2.0
        matches = [boundary for boundary in boundaries if boundary.left <= centre < boundary.right]
        if len(matches) != 1:
            signals.append(_signal("column_shift_suspected", message="word does not map to one header interval"))
            continue
        mapped[matches[0].key].append(word)
    return {key: tuple(value) for key, value in mapped.items()}, tuple(signals)


def _compact_label(value: Any) -> str:
    return re.sub(r"[\s/（）()：:、,，]", "", _normalized_text(value)).casefold()


def infer_catalog_header_columns(
    page: CatalogPage,
    section_key: str,
    *,
    header_y_tolerance: float = 5.0,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """Infer field columns from the physical ``序号`` header row."""

    serial_word = next(
        (
            word
            for word in page.words
            if _normalized_text(word.get("text") or word.get("textOriginal")) == "序号"
            and _word_bounds(word) is not None
        ),
        None,
    )
    if serial_word is None:
        return (), (_signal("header_not_recognized", page=page.pdf_page, message="序号 header not found"),)
    serial_bounds = _word_bounds(serial_word)
    assert serial_bounds is not None
    header_top = serial_bounds[2]
    header_words = [
        word
        for word in page.words
        if (bounds := _word_bounds(word)) is not None and abs(bounds[2] - header_top) <= header_y_tolerance
    ]
    header_words.sort(key=lambda word: _word_bounds(word)[0] if _word_bounds(word) else float("inf"))
    specs = _SECTION_HEADER_SPECS.get(section_key, ())
    used_header_ids: set[int] = {id(serial_word)}
    columns: list[Mapping[str, Any]] = [
        {
            "key": "rowNumber",
            "text": "序号",
            "x0": serial_bounds[0],
            "x1": serial_bounds[1],
            "top": serial_bounds[2],
            "bottom": serial_bounds[3],
        }
    ]
    signals: list[Mapping[str, Any]] = []
    for field_path, aliases in specs:
        compact_aliases = {_compact_label(alias) for alias in aliases}
        match = next(
            (
                word
                for word in header_words
                if id(word) not in used_header_ids
                and _compact_label(word.get("text") or word.get("textOriginal")) in compact_aliases
            ),
            None,
        )
        if match is None:
            signals.append(
                _signal(
                    "header_not_recognized",
                    page=page.pdf_page,
                    message=f"header missing for {field_path}",
                )
            )
            continue
        bounds = _word_bounds(match)
        assert bounds is not None
        used_header_ids.add(id(match))
        columns.append(
            {
                "key": field_path,
                "text": _normalized_text(match.get("text") or match.get("textOriginal")),
                "x0": bounds[0],
                "x1": bounds[1],
                "top": bounds[2],
                "bottom": bounds[3],
            }
        )
    return tuple(sorted(columns, key=lambda word: _word_bounds(word)[0] if _word_bounds(word) else float("inf"))), tuple(signals)


def _word_text(word: Mapping[str, Any]) -> str:
    return _normalized_text(word.get("text") or word.get("textOriginal"))


def _approval_identifier_words(
    section_key: str,
    words: Sequence[Mapping[str, Any]],
    *,
    anchor_top: float | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """Select the identifier token from a visually overlapping approval cell.

    PDF text extraction can place the first holder/model word in the approval
    interval when the two columns touch or a cell is vertically wrapped.  The
    approval number is a single identifier in every CAAC catalog section, so
    use the section vocabulary to keep that token and return the remaining
    words for reassignment to the adjacent cell.  No value is synthesized or
    normalized here; the source word and its coordinates remain the evidence.
    """

    pattern = _APPROVAL_TOKEN_PATTERNS.get(section_key)
    if pattern is None or section_key not in _APPROVAL_IDENTIFIER_ISOLATION_SECTIONS:
        return (), tuple(words)
    ordered = tuple(
        sorted(
            words,
            key=lambda word: (
                _word_bounds(word)[2] if _word_bounds(word) is not None else float("inf"),
                _word_bounds(word)[0] if _word_bounds(word) is not None else float("inf"),
            ),
        )
    )
    selected: list[Mapping[str, Any]] = []
    overflow: list[Mapping[str, Any]] = []
    for word in ordered:
        text = _word_text(word)
        candidate = text.strip(" ,;，；:：")
        if pattern.fullmatch(candidate):
            selected.append(word)
        else:
            overflow.append(word)
    if not selected:
        return (), ordered
    # Keep the first identifier and expose any other token as overflow.  A
    # second identifier in one approval cell is an exact-match review case,
    # not a reason to concatenate two business identifiers.
    selected_word = min(
        selected,
        key=lambda word: (
            abs((_word_bounds(word)[2] if _word_bounds(word) is not None else 0.0) - anchor_top)
            if anchor_top is not None
            else 0.0,
            _word_bounds(word)[2] if _word_bounds(word) is not None else float("inf"),
            _word_bounds(word)[0] if _word_bounds(word) is not None else float("inf"),
        ),
    )
    return (selected_word,), tuple(word for word in ordered if word is not selected_word)


def _is_catalog_furniture_word(
    word: Mapping[str, Any],
    *,
    page_height: float | None = None,
) -> bool:
    """Identify page furniture that can fall into a wrapped data cell."""

    text = _word_text(word)
    if "截至" in text or "数据截止" in text:
        return True
    # Section/page labels such as ``TC-1`` sit in the same x column as the
    # last model on some PDF pages.  Only treat this compact label as
    # furniture when it is close to the physical page bottom; otherwise a
    # legitimate model such as ``B-1`` must remain source data.
    bounds = _word_bounds(word)
    if (
        page_height is not None
        and bounds is not None
        and bounds[2] >= page_height - 80.0
        and re.fullmatch(r"[A-Z]{2,8}-\d{1,3}", text)
    ):
        return True
    return False


def _visual_line_groups(
    words: Sequence[Mapping[str, Any]],
    *,
    y_tolerance: float = 2.5,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """Group column words into visual lines while preserving source words."""

    ordered = sorted(
        (word for word in words if _word_bounds(word) is not None),
        key=lambda word: (
            _word_bounds(word)[2],
            _word_bounds(word)[0],
        ),
    )
    groups: list[list[Mapping[str, Any]]] = []
    line_top: float | None = None
    for word in ordered:
        bounds = _word_bounds(word)
        assert bounds is not None
        if line_top is None or abs(bounds[2] - line_top) > y_tolerance:
            groups.append([])
            line_top = bounds[2]
        groups[-1].append(word)
    return tuple(
        tuple(
            sorted(
                group,
                key=lambda word: _word_bounds(word)[0]
                if _word_bounds(word) is not None
                else float("inf"),
            )
        )
        for group in groups
    )


def _visual_cell_text(words: Sequence[Mapping[str, Any]]) -> str:
    """Join a cell in visual line order, then left-to-right within each line."""

    return " ".join(
        _word_text(word)
        for line in _visual_line_groups(words)
        for word in line
    ).strip()


def _reassign_stc_original_model_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
) -> None:
    """Return STC original-model text that crosses the header-centre boundary.

    On the STC tables a long original model can visually reach into the
    midpoint-derived original-certificate interval. The physical left edge of
    the original-certificate header remains a reliable boundary: words that
    begin to its left belong to the original model, not to the certificate
    number cell.
    """

    model_key = "attributes.originalModel"
    approval_key = "attributes.originalApprovalNumber"
    approval_header = next(
        (
            header
            for header in headers
            if _normalized_text(header.get("key")) == approval_key
            and _word_bounds(header) is not None
        ),
        None,
    )
    if approval_header is None:
        return
    approval_bounds = _word_bounds(approval_header)
    assert approval_bounds is not None
    approval_words = tuple(mapped.get(approval_key, ()))
    overflow = tuple(
        word
        for word in approval_words
        if (bounds := _word_bounds(word)) is not None and bounds[0] < approval_bounds[0] - 1.0
    )
    if not overflow:
        return
    mapped[approval_key] = tuple(word for word in approval_words if word not in overflow)
    mapped[model_key] = tuple((*mapped.get(model_key, ()), *overflow))


def _reassign_mda_column_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
    *,
    page_height: float | None,
) -> None:
    """Restore MDA values that cross a header-centre-derived column boundary.

    MDA has a narrow product-type column followed by a wide description column
    and then a narrow approval-date column. Long descriptions can reach into
    the midpoint-derived date interval, while a short product type such as
    737 can visually sit just left of the description header but still map to
    it. The physical left edges of the two right-hand headers retain the
    authoritative table boundaries.
    """

    for key, words in tuple(mapped.items()):
        mapped[key] = tuple(
            word
            for word in words
            if not (
                _is_catalog_furniture_word(word, page_height=page_height)
                and re.fullmatch(r"MDA-\d{1,3}", _word_text(word))
            )
        )

    def move_words_left_of_header(*, source_key: str, target_key: str) -> None:
        source_header = next(
            (
                header
                for header in headers
                if _normalized_text(header.get("key")) == source_key
                and _word_bounds(header) is not None
            ),
            None,
        )
        if source_header is None:
            return
        source_bounds = _word_bounds(source_header)
        assert source_bounds is not None
        source_words = tuple(mapped.get(source_key, ()))
        overflow = tuple(
            word
            for word in source_words
            if (bounds := _word_bounds(word)) is not None
            and bounds[0] < source_bounds[0] - 1.0
        )
        if not overflow:
            return
        mapped[source_key] = tuple(
            word for word in source_words if word not in overflow
        )
        mapped[target_key] = tuple((*mapped.get(target_key, ()), *overflow))

    move_words_left_of_header(
        source_key="item.descriptionOriginal",
        target_key="item.productTypeOriginal",
    )
    move_words_left_of_header(
        source_key="approval.latestApprovedOn",
        target_key="item.descriptionOriginal",
    )


def _reassign_vstc_column_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
    *,
    page_height: float | None,
) -> None:
    """Restore VSTC cells around the narrow model/description boundary.

    The VSTC table uses centered header labels but fixed physical data-cell
    starts.  Consequently, the midpoint between the header labels puts the
    first word of a description (for example ``TCAS`` or ``Installation``)
    into the model column, and the final description word can drift into the
    authority column.  The source grid is consistent across this chapter:
    model values begin just right of the product-type header, descriptions
    begin roughly 32 points right of the model header, and authority text
    begins at its header's left edge.  Reassign only words crossing those
    physical boundaries; source text and coordinate evidence remain intact.
    """

    for key, words in tuple(mapped.items()):
        mapped[key] = tuple(
            word
            for word in words
            if not (
                _is_catalog_furniture_word(word, page_height=page_height)
                and re.fullmatch(r"VSTC-\d{1,3}", _word_text(word))
            )
        )

    def header_bounds_for(key: str) -> tuple[float, float, float, float] | None:
        header = next(
            (
                candidate
                for candidate in headers
                if _normalized_text(candidate.get("key")) == key
                and _word_bounds(candidate) is not None
            ),
            None,
        )
        return _word_bounds(header) if header is not None else None

    product_bounds = header_bounds_for("item.productTypeOriginal")
    model_bounds = header_bounds_for("item.modelOriginal")
    authority_bounds = header_bounds_for("attributes.authority")
    if product_bounds is None or model_bounds is None or authority_bounds is None:
        return

    def move_words_right_of(
        *, source_key: str, target_key: str, left_boundary: float
    ) -> None:
        source_words = tuple(mapped.get(source_key, ()))
        overflow = tuple(
            word
            for word in source_words
            if (bounds := _word_bounds(word)) is not None
            and bounds[0] >= left_boundary
        )
        if not overflow:
            return
        mapped[source_key] = tuple(word for word in source_words if word not in overflow)
        mapped[target_key] = tuple((*mapped.get(target_key, ()), *overflow))

    def move_words_left_of(
        *, source_key: str, target_key: str, left_boundary: float
    ) -> None:
        source_words = tuple(mapped.get(source_key, ()))
        overflow = tuple(
            word
            for word in source_words
            if (bounds := _word_bounds(word)) is not None
            and bounds[0] < left_boundary
        )
        if not overflow:
            return
        mapped[source_key] = tuple(word for word in source_words if word not in overflow)
        mapped[target_key] = tuple((*mapped.get(target_key, ()), *overflow))

    move_words_right_of(
        source_key="item.productTypeOriginal",
        target_key="item.modelOriginal",
        left_boundary=product_bounds[1] + 2.0,
    )
    move_words_right_of(
        source_key="item.modelOriginal",
        target_key="item.descriptionOriginal",
        left_boundary=model_bounds[1] + 32.0,
    )
    move_words_left_of(
        source_key="attributes.authority",
        target_key="item.descriptionOriginal",
        left_boundary=authority_bounds[0] - 1.0,
    )


def _reassign_vda_column_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
) -> None:
    """Restore VDA cells whose data grid is offset from centered headers.

    The VDA pages use stable, fixed data-cell starts, while several header
    labels are centred within their cells.  Mapping by adjacent header centres
    therefore moves holder suffixes (for example ``Inc.``) into product type,
    short product names into product type, and model continuations into CTSO.
    Reassign only words that cross the known physical starts derived from the
    same page's header geometry.  This preserves the original words and their
    coordinate evidence while avoiding invented text.
    """

    def header_bounds_for(key: str) -> tuple[float, float, float, float] | None:
        header = next(
            (
                candidate
                for candidate in headers
                if _normalized_text(candidate.get("key")) == key
                and _word_bounds(candidate) is not None
            ),
            None,
        )
        return _word_bounds(header) if header is not None else None

    product_bounds = header_bounds_for("item.productTypeOriginal")
    name_bounds = header_bounds_for("item.nameOriginal")
    ctso_bounds = header_bounds_for("item.ctsoCodesOriginal")
    if product_bounds is None or name_bounds is None or ctso_bounds is None:
        return

    def move_words_left_of(
        *, source_key: str, target_key: str, right_boundary: float
    ) -> None:
        source_words = tuple(mapped.get(source_key, ()))
        overflow = tuple(
            word
            for word in source_words
            if (bounds := _word_bounds(word)) is not None
            and bounds[0] < right_boundary
        )
        if not overflow:
            return
        mapped[source_key] = tuple(word for word in source_words if word not in overflow)
        mapped[target_key] = tuple((*mapped.get(target_key, ()), *overflow))

    def move_words_right_of(
        *, source_key: str, target_key: str, left_boundary: float
    ) -> None:
        source_words = tuple(mapped.get(source_key, ()))
        overflow = tuple(
            word
            for word in source_words
            if (bounds := _word_bounds(word)) is not None
            and bounds[0] >= left_boundary
        )
        if not overflow:
            return
        mapped[source_key] = tuple(word for word in source_words if word not in overflow)
        mapped[target_key] = tuple((*mapped.get(target_key, ()), *overflow))

    # Product-type data starts at the left edge of its label; holder text is
    # allowed to fill the preceding cell until that physical boundary.
    move_words_left_of(
        source_key="item.productTypeOriginal",
        target_key="holder.nameOriginal",
        right_boundary=product_bounds[0],
    )
    # Product names start immediately after the product-type header's right
    # edge, rather than at the midpoint between the two centred labels.
    move_words_right_of(
        source_key="item.productTypeOriginal",
        target_key="item.nameOriginal",
        left_boundary=product_bounds[1] + 1.0,
    )
    # Retain the existing VDA name/model separation, but restore the words to
    # the model cell instead of silently dropping them from the name cell.
    move_words_right_of(
        source_key="item.nameOriginal",
        target_key="item.modelOriginal",
        left_boundary=name_bounds[1] + 10.0,
    )
    # The CTSO label begins around 11 points to the right of the actual CTSO
    # data-cell start on this source layout.  Model continuations in that gap
    # must remain model text; CTSO entries themselves begin at about x=505.
    move_words_left_of(
        source_key="item.ctsoCodesOriginal",
        target_key="item.modelOriginal",
        right_boundary=ctso_bounds[0] - 12.0,
    )


def _reassign_vtc_column_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
) -> None:
    """Restore VTC holder suffixes that cross a centred-header midpoint.

    VTC holder names occupy a materially wider physical cell than their
    centred header implies. A suffix such as ``GmbH`` can therefore be
    assigned to product type when mapping by adjacent header centres. The
    product-type data cell starts at the physical left edge of its header, so
    only words to the left of that edge are returned to the holder cell.
    """

    product_header = next(
        (
            header
            for header in headers
            if _normalized_text(header.get("key")) == "item.productTypeOriginal"
            and _word_bounds(header) is not None
        ),
        None,
    )
    product_bounds = _word_bounds(product_header) if product_header is not None else None
    if product_bounds is None:
        return
    product_words = tuple(mapped.get("item.productTypeOriginal", ()))
    overflow = tuple(
        word
        for word in product_words
        if (bounds := _word_bounds(word)) is not None
        and bounds[0] < product_bounds[0] - 1.0
    )
    if not overflow:
        return
    mapped["item.productTypeOriginal"] = tuple(
        word for word in product_words if word not in overflow
    )
    mapped["holder.nameOriginal"] = tuple(
        (*mapped.get("holder.nameOriginal", ()), *overflow)
    )


def _reassign_tda_column_overflow(
    mapped: dict[str, tuple[Mapping[str, Any], ...]],
    headers: Sequence[Mapping[str, Any]],
) -> None:
    """Restore TDA holder suffixes shifted by the centred model header.

    The TDA model label is centred well to the right of its physical data-cell
    start.  A holder suffix such as ``a.s`` can therefore enter the model
    interval under midpoint mapping even though the source visually keeps it
    with the holder.  Move only words meaningfully left of the model-cell
    start; model text aligned with that start remains untouched.
    """

    model_header = next(
        (
            header
            for header in headers
            if _normalized_text(header.get("key")) == "item.modelOriginal"
            and _word_bounds(header) is not None
        ),
        None,
    )
    model_bounds = _word_bounds(model_header) if model_header is not None else None
    if model_bounds is None:
        return
    model_words = tuple(mapped.get("item.modelOriginal", ()))
    overflow = tuple(
        word
        for word in model_words
        if (bounds := _word_bounds(word)) is not None
        and bounds[0] < model_bounds[0] - 1.0
    )
    if not overflow:
        return
    mapped["item.modelOriginal"] = tuple(
        word for word in model_words if word not in overflow
    )
    mapped["holder.nameOriginal"] = tuple(
        (*mapped.get("holder.nameOriginal", ()), *overflow)
    )


def _visual_line_center(line: Sequence[Mapping[str, Any]]) -> float:
    bounds = [_word_bounds(word) for word in line]
    valid = [item for item in bounds if item is not None]
    if not valid:
        return 0.0
    return (min(item[2] for item in valid) + max(item[3] for item in valid)) / 2.0


def _partition_multiline_column(
    words: Sequence[Mapping[str, Any]],
    anchors: Sequence[RowAnchor],
    *,
    header_bottom: float,
    page_height: float | None,
) -> tuple[tuple[Mapping[str, Any], ...], ...] | None:
    """Assign wrapped column lines to row anchors by block-centre distance.

    A PDF table often vertically centres a multi-line cell on the serial
    number baseline.  Splitting at the midpoint between serial baselines
    therefore loses the first/last line of the cell.  This helper treats each
    visual line as an ordered token and finds the lowest-cost contiguous
    partition whose block centre is closest to each row anchor.  Sparse or
    ambiguous columns return ``None`` so the caller can retain its
    conservative row-band result.
    """

    if not anchors:
        return None
    data_words: list[Mapping[str, Any]] = []
    for word in words:
        bounds = _word_bounds(word)
        if bounds is None or not _word_text(word):
            continue
        if bounds[2] < header_bottom + 1.5:
            continue
        # A word above the first anchor may be a continuation from the
        # preceding physical page *or* the first wrapped line of the first
        # row.  Keep it in the candidate partition; the caller emits the
        # explicit continuation_pending signal so publication still remains
        # fail-closed until the adjacent page is reviewed.
        if _is_catalog_furniture_word(word, page_height=page_height):
            continue
        data_words.append(word)
    line_groups = _visual_line_groups(data_words)
    if not line_groups:
        return None
    line_centres = tuple(_visual_line_center(line) for line in line_groups)
    anchor_centres = tuple((anchor.top + anchor.bottom) / 2.0 for anchor in anchors)

    gaps = [
        anchor_centres[index + 1] - anchor_centres[index]
        for index in range(len(anchor_centres) - 1)
        if anchor_centres[index + 1] > anchor_centres[index]
    ]
    typical_gap = sorted(gaps)[len(gaps) // 2] if gaps else 12.0
    near_tolerance = max(5.0, min(18.0, typical_gap * 0.75))
    every_anchor_has_nearby_line = all(
        any(abs(line_centre - anchor_centre) <= near_tolerance for line_centre in line_centres)
        for anchor_centre in anchor_centres
    )
    # A sparse column may legitimately omit values.  Without an independent
    # truth signal, repartitioning such a column would be more dangerous than
    # retaining the conservative row-band result, so only run this algorithm
    # when every anchor has nearby source text and there are enough lines to
    # give each row a non-empty block.
    if len(line_groups) < len(anchors) or not every_anchor_has_nearby_line:
        return None

    # A single physical line per row is the overwhelmingly common case.  It
    # also avoids a cubic DP on dense PMA pages where no repartitioning is
    # needed.
    if len(line_groups) == len(anchors):
        return tuple(tuple(line_groups[index]) for index in range(len(anchors)))

    # Prefix sums make a candidate block centre O(1); the state search stays
    # bounded to one column/page and is only used when the simple one-line
    # path above cannot decide the assignment.
    prefix = [0.0]
    for centre in line_centres:
        prefix.append(prefix[-1] + centre)

    def block_cost(anchor_index: int, start: int, end: int) -> float:
        centre = (prefix[end] - prefix[start]) / (end - start)
        scale = max(typical_gap * 0.6, 4.0)
        return ((centre - anchor_centres[anchor_index]) / scale) ** 2

    row_count = len(anchors)
    line_count = len(line_groups)
    infinity = float("inf")
    costs = [[infinity] * (line_count + 1) for _ in range(row_count + 1)]
    previous: list[list[tuple[int, int] | None]] = [
        [None] * (line_count + 1) for _ in range(row_count + 1)
    ]
    costs[0][0] = 0.0
    for row_index in range(row_count):
        for start in range(line_count + 1):
            base = costs[row_index][start]
            if base == infinity:
                continue
            minimum_end = start + 1
            maximum_end = line_count - (row_count - row_index - 1)
            for end in range(minimum_end, maximum_end + 1):
                candidate = base + block_cost(row_index, start, end)
                if candidate < costs[row_index + 1][end]:
                    costs[row_index + 1][end] = candidate
                    previous[row_index + 1][end] = (start, end)

    if costs[row_count][line_count] == infinity:
        return None

    assigned_groups: list[tuple[Mapping[str, Any], ...]] = [()] * row_count
    end = line_count
    for row_index in range(row_count, 0, -1):
        state = previous[row_index][end]
        if state is None:
            return None
        start, row_end = state
        assigned_groups[row_index - 1] = tuple(
            word for line in line_groups[start:row_end] for word in line
        )
        end = start
    return tuple(assigned_groups)


def _word_bbox(words: Sequence[Mapping[str, Any]]) -> list[float] | None:
    bounds = [_word_bounds(word) for word in words]
    valid = [item for item in bounds if item is not None]
    if not valid:
        return None
    return [
        min(item[0] for item in valid),
        min(item[2] for item in valid),
        max(item[1] for item in valid),
        max(item[3] for item in valid),
    ]


def _horizontal_rule_tops(page: CatalogPage) -> tuple[float, ...]:
    """Return usable filled-rect rule positions when the PDF exposes them."""

    raw_rects = page.metadata.get("rects") if isinstance(page.metadata, Mapping) else ()
    tops: list[float] = []
    for rect in raw_rects or ():
        if not isinstance(rect, Mapping):
            continue
        try:
            top = float(rect.get("top"))
            bottom = float(rect.get("bottom"))
            width = float(rect.get("width"))
        except (TypeError, ValueError):
            continue
        if width >= 500.0 and bottom - top <= 3.0:
            tops.append(top)
    return tuple(sorted(set(tops)))


def _row_band(
    page: CatalogPage,
    anchors: Sequence[RowAnchor],
    index: int,
    *,
    header_bottom: float,
) -> tuple[float, float]:
    """Compute a row band from PDF rules, falling back to anchor midpoints."""

    anchor = anchors[index]
    rule_tops = _horizontal_rule_tops(page)
    above = [top for top in rule_tops if top <= anchor.top + 0.5]
    below = [top for top in rule_tops if top > anchor.top + 0.5]
    if above and below:
        start = max(above) + 0.5
        end = min(below) - 0.5
        rows_in_rule = sum(
            1 for candidate in anchors if start <= candidate.top < end
        )
        if end > start and end - start <= 120.0 and rows_in_rule <= 1:
            return start, end
        if index == len(anchors) - 1:
            fallback_start = (
                header_bottom + 1.5
                if index == 0
                else (anchors[index - 1].top + anchor.top) / 2.0
            )
            if end > fallback_start:
                return fallback_start, end
    start = (
        header_bottom + 1.5
        if index == 0
        else (anchors[index - 1].top + anchor.top) / 2.0
    )
    end = (
        (anchor.top + anchors[index + 1].top) / 2.0
        if index + 1 < len(anchors)
        else float(page.metadata.get("pageHeight") or max((_word_bounds(word)[3] for word in page.words if _word_bounds(word)), default=anchor.bottom) + 5.0)
    )
    return start, end


def extract_candidate_rows(
    page: CatalogPage,
    section_key: str,
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """Build conservative row/cell candidates without asserting publication."""

    headers, header_signals = infer_catalog_header_columns(page, section_key)
    anchors, anchor_signals = anchor_row_numbers(page)
    signals: list[Mapping[str, Any]] = [*header_signals, *anchor_signals]
    if not headers or not anchors:
        return (), tuple(signals)
    serial_word = next(
        (
            word
            for word in page.words
            if _normalized_text(word.get("text") or word.get("textOriginal")) == "序号"
            and _word_bounds(word) is not None
        ),
        None,
    )
    if serial_word is not None:
        serial_bounds = _word_bounds(serial_word)
        assert serial_bounds is not None
        prefix_words = [
            word
            for word in page.words
            if (bounds := _word_bounds(word)) is not None
            and bounds[2] > serial_bounds[3] + 2.0
            and bounds[2] < anchors[0].top - 1.5
            and _normalized_text(word.get("text") or word.get("textOriginal"))
        ]
        if prefix_words:
            signals.append(
                _signal(
                    "continuation_pending",
                    page=page.pdf_page,
                    message=(
                        f"{len(prefix_words)} words precede first serial row; "
                        "continuation/merged-cell review is required"
                    ),
                )
            )
    serial_word = serial_word or next(
        (
            word
            for word in page.words
            if _normalized_text(word.get("text") or word.get("textOriginal")) == "序号"
            and _word_bounds(word) is not None
        ),
        None,
    )
    header_bottom = _word_bounds(serial_word)[3] if serial_word is not None and _word_bounds(serial_word) else anchors[0].top - 5.0
    ordered_words = [word for word in page.words if _word_bounds(word) is not None]
    ordered_words.sort(key=lambda word: (_word_bounds(word)[2], _word_bounds(word)[0]))
    # Row bands are intentionally conservative for identifiers and dates, but
    # they are not sufficient for vertically centred multi-line cells.  Build
    # per-column assignments once so all words in a wrapped item block stay
    # with the row whose serial anchor centres that block.
    multiline_assignments: dict[str, tuple[tuple[Mapping[str, Any], ...], ...]] = {}
    page_height = _word_number(page.metadata.get("pageHeight")) if isinstance(page.metadata, Mapping) else None
    data_words = [
        word
        for word in ordered_words
        if (bounds := _word_bounds(word)) is not None and bounds[2] >= header_bottom + 1.5
    ]
    mapped_data_words, _ = map_words_to_columns(data_words, headers)
    if section_key == "stc":
        _reassign_stc_original_model_overflow(mapped_data_words, headers)
    elif section_key == "mda":
        _reassign_mda_column_overflow(
            mapped_data_words,
            headers,
            page_height=page_height,
        )
    elif section_key == "vstc":
        _reassign_vstc_column_overflow(
            mapped_data_words,
            headers,
            page_height=page_height,
        )
    elif section_key == "vtc":
        _reassign_vtc_column_overflow(mapped_data_words, headers)
    elif section_key == "vda":
        _reassign_vda_column_overflow(mapped_data_words, headers)
    elif section_key == "tda":
        _reassign_tda_column_overflow(mapped_data_words, headers)
    multiline_field_keys = _MULTILINE_CELL_KEYS
    if section_key == "vstc":
        multiline_field_keys = _MULTILINE_CELL_KEYS | {"item.descriptionOriginal"}
    for field_key in multiline_field_keys:
        field_words = mapped_data_words.get(field_key)
        if field_words:
            assignments = _partition_multiline_column(
                field_words,
                anchors,
                header_bottom=header_bottom,
                page_height=page_height,
            )
            if assignments is not None:
                multiline_assignments[field_key] = assignments
    rows: list[Mapping[str, Any]] = []
    for index, anchor in enumerate(anchors):
        band_start, band_end = _row_band(
            page,
            anchors,
            index,
            header_bottom=header_bottom,
        )
        row_words = [
            word
            for word in ordered_words
            if band_start <= _word_bounds(word)[2] < band_end
        ]
        mapped, map_signals = map_words_to_columns(row_words, headers)
        if section_key == "stc":
            _reassign_stc_original_model_overflow(mapped, headers)
        elif section_key == "mda":
            _reassign_mda_column_overflow(
                mapped,
                headers,
                page_height=page_height,
            )
        elif section_key == "vstc":
            _reassign_vstc_column_overflow(
                mapped,
                headers,
                page_height=page_height,
            )
        elif section_key == "vtc":
            _reassign_vtc_column_overflow(mapped, headers)
        elif section_key == "vda":
            _reassign_vda_column_overflow(mapped, headers)
        elif section_key == "tda":
            _reassign_tda_column_overflow(mapped, headers)
        signals.extend({**dict(signal), "pdfPage": page.pdf_page} for signal in map_signals)
        cells: dict[str, str] = {}
        evidence: list[Mapping[str, Any]] = []
        row_quality_codes = {str(signal.get("code")) for signal in map_signals}
        for field_key, assignments in multiline_assignments.items():
            if index < len(assignments) and field_key in mapped:
                # Approval-overflow handling below may append text to the
                # holder cell; only replace columns whose source header is
                # present in this row's conservative band.
                mapped[field_key] = assignments[index]
        approval_words = mapped.get("approval.numberOriginal", ())
        if approval_words and section_key in _APPROVAL_IDENTIFIER_ISOLATION_SECTIONS:
            selected_identifier, overflow = _approval_identifier_words(
                section_key,
                approval_words,
                anchor_top=anchor.top,
            )
            if selected_identifier:
                if overflow:
                    destination = _APPROVAL_OVERFLOW_DESTINATION.get(
                        section_key,
                        "holder.nameOriginal",
                    )
                    destination_words = tuple(
                        word
                        for word in overflow
                        if not (
                            section_key == "foreign_tso_recognition"
                            and _is_catalog_furniture_word(word)
                        )
                    )
                    if destination in mapped:
                        mapped[destination] = tuple(
                            (*mapped[destination], *destination_words)
                        )
                    row_quality_codes.add("approval_identifier_extraneous_text")
                    signals.append(
                        _signal(
                            "approval_identifier_extraneous_text",
                            page=page.pdf_page,
                            message=(
                                f"approval cell contained non-identifier words; "
                                f"reassigned to {destination}"
                            ),
                        )
                    )
                mapped["approval.numberOriginal"] = selected_identifier
            else:
                row_quality_codes.add("approval_identifier_pattern_unmatched")
                signals.append(
                    _signal(
                        "approval_identifier_pattern_unmatched",
                        page=page.pdf_page,
                        message=(
                            f"approval cell did not match the {section_key} "
                            "identifier vocabulary; raw words retained"
                        ),
                    )
                )
        for key, cell_words in mapped.items():
            if key == "rowNumber" or not cell_words:
                continue
            cell_words = tuple(cell_words)
            if key == "approval.numberOriginal":
                # Approval/project identifiers are single-line anchors.  A
                # wrapped holder or item line must never be appended to the
                # identifier merely because it falls in the same x interval.
                same_baseline = [
                    word
                    for word in cell_words
                    if (bounds := _word_bounds(word)) is not None
                    and abs(bounds[2] - anchor.top) <= 2.5
                ]
                if same_baseline:
                    overflow = [word for word in cell_words if word not in same_baseline]
                    if overflow and "holder.nameOriginal" in mapped:
                        mapped["holder.nameOriginal"] = tuple(
                            (*mapped["holder.nameOriginal"], *overflow)
                        )
                    cell_words = tuple(same_baseline)
            if key == "item.nameOriginal":
                # The PMA product pages repeat this footer inside the item
                # name column.  It is page furniture, not an item name.
                cell_words = tuple(
                    word
                    for word in cell_words
                    if _word_text(word) != "活门零件项目清单"
                )
                if not cell_words:
                    continue
            text = _visual_cell_text(cell_words)
            if not text:
                continue
            cells[key] = text
            evidence.append(
                {
                    "fieldPath": key,
                    "textOriginal": text,
                    "pdfPage": page.pdf_page,
                    "bbox": _word_bbox(cell_words),
                    "sourceWordIds": [str(word.get("id") or "") for word in cell_words if word.get("id")],
                }
            )
        rows.append(
            {
                "rowNumber": anchor.row_number,
                "pdfPage": page.pdf_page,
                "cells": cells,
                "fieldEvidence": evidence,
                "qualitySignalCodes": sorted(row_quality_codes),
            }
        )
    return tuple(rows), tuple(signals)


def _normalize_search_value(value: Any) -> str:
    text = _normalized_text(value)
    return text.replace("－", "-").replace("–", "-").replace("—", "-").casefold()


def _normalize_identifier(value: Any) -> str:
    return _normalize_search_value(value).upper()


def _normalize_date(value: Any) -> tuple[str | None, Mapping[str, Any] | None]:
    text = _normalized_text(value)
    if not text:
        return None, None
    matches = list(_DATE_TOKEN_RE.finditer(text))
    if not matches:
        return None, _signal("date_parse_failed", message=f"date could not be normalized: {text}")
    normalized_dates = [
        f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
        for match in matches
    ]
    chosen = max(normalized_dates)
    residue = _DATE_TOKEN_RE.sub(" ", text)
    residue = residue.strip(" ,;、，；:：()（）[]【】")
    if len(matches) > 1:
        return chosen, _signal(
            "date_multiple_candidates",
            message=f"multiple date candidates preserved in raw text: {text}",
        )
    if _normalized_text(residue):
        return chosen, _signal(
            "date_text_contains_extra",
            message=f"date cell contains non-date text preserved in raw text: {text}",
        )
    return chosen, None


def _explicit_ctso_codes(value: Any) -> tuple[str, ...]:
    text = _normalized_text(value)
    if not text:
        return ()
    return tuple(dict.fromkeys(match.upper().replace(" ", "") for match in _CTSO_CODE_RE.findall(text)))


def build_candidate_record(
    *,
    row: Mapping[str, Any],
    section_key: str,
    source_snapshot_sha256: str,
    parser_version: str,
    extractor_version: str = CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION,
    schema_version: str = CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
    source_omission: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any] | None, tuple[Mapping[str, Any], ...]]:
    """Project one conservative row into a versioned catalog record shape.

    A missing approval number or holder is rejected from the candidate record
    stream and returned as a quality signal unless V2 receives an exact,
    reviewed source-cell-empty holder exception.  The exception never creates
    a placeholder holder value.
    """

    if schema_version not in {
        CAAC_APPROVED_CATALOG_SCHEMA_VERSION,
        CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION,
    }:
        raise ValueError(f"unsupported catalog record schema: {schema_version}")
    cells = row.get("cells") if isinstance(row.get("cells"), Mapping) else {}
    approval_original = _normalized_text(cells.get("approval.numberOriginal"))
    holder_original = _normalize_holder_original(cells.get("holder.nameOriginal"))
    signals: list[Mapping[str, Any]] = []
    source_missing_holder = False
    source_omission_payload: dict[str, Any] | None = None
    if not approval_original:
        signals.append(_signal("required_field_missing", page=int(row.get("pdfPage") or 0), message="approval number is empty"))
    if not holder_original:
        if (
            schema_version == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION
            and isinstance(source_omission, Mapping)
        ):
            source_missing_holder = True
            source_omission_payload = {
                "reviewId": source_omission["reviewId"],
                "fieldPath": source_omission["fieldPath"],
                "reasonCode": source_omission["reasonCode"],
                "pdfPage": source_omission["pdfPage"],
                "bbox": list(source_omission["bbox"]),
                "sourceSnapshotSha256": source_snapshot_sha256,
                "reviewedOn": source_omission["reviewedOn"],
            }
        else:
            signals.append(_signal("required_field_missing", page=int(row.get("pdfPage") or 0), message="holder is empty"))
    elif source_omission is not None:
        raise ValueError("source omission review cannot be applied to a populated holder cell")
    if signals:
        return None, tuple(signals)

    latest_date, latest_signal = _normalize_date(cells.get("approval.latestApprovedOn"))
    expires_date, expires_signal = _normalize_date(cells.get("approval.expiresOn"))
    if latest_signal is not None:
        signals.append({**dict(latest_signal), "pdfPage": int(row.get("pdfPage") or 0)})
    if expires_signal is not None:
        signals.append({**dict(expires_signal), "pdfPage": int(row.get("pdfPage") or 0)})

    approval_normalized = _normalize_identifier(approval_original)
    holder_normalized = _normalize_search_value(holder_original)
    item_values = {
        "nameOriginal": _normalized_text(cells.get("item.nameOriginal")),
        "modelOriginal": _normalized_text(cells.get("item.modelOriginal")),
        "partNumberOriginal": _normalized_text(cells.get("item.partNumberOriginal")),
        "replacedManufacturerOriginal": _normalized_text(cells.get("item.replacedManufacturerOriginal")),
        "replacedPartNumberOriginal": _normalized_text(cells.get("item.replacedPartNumberOriginal")),
        "productTypeOriginal": _normalized_text(cells.get("item.productTypeOriginal")),
        "descriptionOriginal": _normalized_text(cells.get("item.descriptionOriginal")),
    }
    ctso_codes = _explicit_ctso_codes(cells.get("item.ctsoCodesOriginal"))
    item_values["ctsoCodesOriginal"] = list(ctso_codes)
    item_values["ctsoCodesNormalized"] = list(ctso_codes)
    attributes = {
        key.split(".", 1)[1]: _normalized_text(value)
        for key, value in cells.items()
        if str(key).startswith("attributes.") and _normalized_text(value)
    }
    identity_payload = {
        "approvalType": _APPROVAL_TYPE_BY_SECTION.get(section_key, section_key),
        "sectionKey": section_key,
        "approvalNumber": approval_normalized,
        "holder": (
            holder_normalized
            if not source_missing_holder
            else f"source-missing:{source_omission_payload['reviewId']}"
        ),
        "item": item_values,
        "attributes": attributes,
    }
    identity_hash = hashlib.sha256(
        json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    page_number = int(row.get("pdfPage") or 0)
    row_number = int(row.get("rowNumber") or 0)
    field_evidence: list[Mapping[str, Any]] = []
    source_cells: list[Mapping[str, Any]] = []
    for evidence in row.get("fieldEvidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        field_path = _normalized_text(evidence.get("fieldPath"))
        if not field_path:
            continue
        source_word_ids = [str(item) for item in evidence.get("sourceWordIds") or () if str(item)]
        field_evidence.append(
            {
                **dict(evidence),
                "parserVersion": parser_version,
                "extractorVersion": extractor_version,
                "confidence": 0.0,
                "reviewStatus": "unreviewed",
                "sourceWordIds": source_word_ids,
                "sourceCellId": None,
            }
        )
        source_cells.append(
            {
                "columnKey": field_path,
                "textOriginal": str(evidence.get("textOriginal") or ""),
                "pdfPage": page_number,
                "bbox": evidence.get("bbox"),
                "sourceWordIds": source_word_ids,
            }
        )
    if source_missing_holder:
        if source_omission_payload is None:  # pragma: no cover - guarded above
            raise ValueError("source omission payload is missing")
        omission_page = int(source_omission_payload["pdfPage"])
        omission_bbox = list(source_omission_payload["bbox"])
        omission_cell_id = f"source-omission:{source_omission_payload['reviewId']}"
        field_evidence.append(
            {
                "fieldPath": SOURCE_OMISSION_FIELD_PATH,
                "textOriginal": "",
                "pdfPage": omission_page,
                "bbox": omission_bbox,
                "sourceWordIds": [],
                "sourceCellId": omission_cell_id,
                "parserVersion": parser_version,
                "extractorVersion": extractor_version,
                "confidence": 1.0,
                "reviewStatus": "reviewed",
            }
        )
        source_cells.append(
            {
                "columnKey": SOURCE_OMISSION_FIELD_PATH,
                "textOriginal": "",
                "pdfPage": omission_page,
                "bbox": omission_bbox,
                "sourceWordIds": [],
            }
        )
        signals.append(
            _signal(
                "source_omission_reviewed",
                page=omission_page,
                message="holder source cell is explicitly reviewed as empty",
            )
        )
    signals.extend(
        {
            "code": str(code),
            "severity": "warning",
            "message": str(code),
            "pdfPage": page_number,
        }
        for code in row.get("qualitySignalCodes") or ()
    )
    quality_signals = tuple(dict(signal) for signal in signals)
    raw_text = " ".join(str(value) for value in cells.values() if _normalized_text(value)).strip()
    evidence_pages = [
        int(evidence.get("pdfPage") or page_number)
        for evidence in field_evidence
        if int(evidence.get("pdfPage") or page_number) > 0
    ]
    source_page_start = min((page_number, *evidence_pages))
    source_page_end = max((page_number, *evidence_pages))
    record = {
        "schemaVersion": schema_version,
        "recordId": f"{source_snapshot_sha256[:16]}:{section_key}:{page_number}:{row_number}:{identity_hash[:16]}",
        "sectionKey": section_key,
        "approvalType": _APPROVAL_TYPE_BY_SECTION.get(section_key, section_key),
        "recordKind": _RECORD_KIND_BY_SECTION.get(section_key, "approved_item"),
        "rowNumber": row_number,
        "approval": {
            "numberOriginal": approval_original,
            "numberNormalized": approval_normalized,
            "latestApprovedOn": latest_date,
            "expiresOn": expires_date,
        },
        "holder": (
            {
                "status": "source_missing",
                "nameOriginal": None,
                "nameNormalized": None,
                "sourceOmission": source_omission_payload,
            }
            if source_missing_holder
            else (
                {
                    "status": "identified",
                    "nameOriginal": holder_original,
                    "nameNormalized": holder_normalized,
                }
                if schema_version == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION
                else {
                    "nameOriginal": holder_original,
                    "nameNormalized": holder_normalized,
                }
            )
        ),
        "item": item_values,
        "attributes": attributes,
        "source": {
            "pdfPageStart": source_page_start,
            "pdfPageEnd": source_page_end,
            "printedPageLabel": None,
            "rawText": raw_text,
            "cells": source_cells,
            "sourceSnapshotSha256": source_snapshot_sha256,
        },
        "quality": {
            "status": "candidate",
            "confidence": 0.0,
            "signals": list(quality_signals),
        },
        "fieldEvidence": field_evidence,
    }
    return record, quality_signals


_PMA_INHERITABLE_FIELDS = (
    "approval.numberOriginal",
    "holder.nameOriginal",
    "item.nameOriginal",
    "item.replacedManufacturerOriginal",
    "approval.latestApprovedOn",
    "approval.expiresOn",
)


def _row_evidence_by_field(row: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for evidence in row.get("fieldEvidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        field_path = _normalized_text(evidence.get("fieldPath"))
        if field_path and field_path not in result:
            result[field_path] = evidence
    return result


def _inherit_pma_merged_cells(
    row: Mapping[str, Any],
    previous_row: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
    """Conservatively inherit contiguous PMA project-level merged cells."""

    if previous_row is None:
        return row, ()
    cells = dict(row.get("cells") or {})
    previous_cells = dict(previous_row.get("cells") or {})
    previous_approval = _normalize_identifier(
        previous_cells.get("approval.numberOriginal")
    )
    current_approval = _normalize_identifier(cells.get("approval.numberOriginal"))
    if not previous_approval:
        return row, ()
    previous_number = int(previous_row.get("rowNumber") or 0)
    current_number = int(row.get("rowNumber") or 0)
    contiguous = previous_number > 0 and current_number == previous_number + 1
    if current_approval and current_approval != previous_approval:
        return row, ()
    if not current_approval and not contiguous:
        return row, ()

    previous_evidence = _row_evidence_by_field(previous_row)
    evidence = [
        dict(item)
        for item in row.get("fieldEvidence") or ()
        if isinstance(item, Mapping)
    ]
    inherited_fields: list[str] = []
    for field_path in _PMA_INHERITABLE_FIELDS:
        if _normalized_text(cells.get(field_path)):
            continue
        inherited_value = _normalized_text(previous_cells.get(field_path))
        inherited_evidence = previous_evidence.get(field_path)
        if not inherited_value or inherited_evidence is None:
            continue
        cells[field_path] = inherited_value
        evidence.append(dict(inherited_evidence))
        inherited_fields.append(field_path)
    if not inherited_fields:
        return row, ()

    signal_codes = {
        str(code) for code in row.get("qualitySignalCodes") or () if str(code)
    }
    signal_codes.add("inherited_merged_cell")
    effective_row = {
        **dict(row),
        "cells": cells,
        "fieldEvidence": evidence,
        "qualitySignalCodes": sorted(signal_codes),
        "inheritedFields": inherited_fields,
    }
    return effective_row, (
        _signal(
            "inherited_merged_cell",
            page=int(row.get("pdfPage") or 0),
            message="inherited contiguous PMA project fields: "
            + ", ".join(inherited_fields),
        ),
    )


class CaacApprovedCatalogV1Extractor(CatalogExtractor):
    schema_version = CAAC_APPROVED_CATALOG_SCHEMA_VERSION
    extractor_name = CAAC_APPROVED_CATALOG_EXTRACTOR
    extractor_version = CAAC_APPROVED_CATALOG_EXTRACTOR_VERSION

    def extract(
        self,
        *,
        pages: Sequence[CatalogPage],
        source_snapshot_sha256: str,
        parser_version: str,
        continuation_state: Mapping[str, Any] | None = None,
    ) -> CatalogExtractionResult:
        """Extract candidate records from an owned/context page window.

        Callers may set ``ownedPageStart``/``ownedPageEnd`` and ``sectionKey``
        in ``continuation_state``.  Context pages update continuation state but
        never emit rows or records.
        """

        signals: list[Mapping[str, Any]] = []
        records: list[Mapping[str, Any]] = []
        field_evidence: list[Mapping[str, Any]] = []
        candidate_rows: list[Mapping[str, Any]] = []
        rejected_rows: list[Mapping[str, Any]] = []
        state = dict(continuation_state or {})
        default_start = min((page.pdf_page for page in pages), default=1)
        default_end = max((page.pdf_page for page in pages), default=default_start)
        owned_page_start = int(state.get("ownedPageStart") or default_start)
        owned_page_end = int(state.get("ownedPageEnd") or default_end)
        if owned_page_start < 1 or owned_page_end < owned_page_start:
            raise ValueError("invalid catalog owned page range")
        requested_section = _normalized_text(state.get("sectionKey"))
        source_omissions: dict[tuple[str, int, int, str], Mapping[str, Any]] = {}
        if self.schema_version == CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION:
            raw_review = state.get("sourceOmissionReview")
            if not isinstance(raw_review, Mapping):
                raise ValueError("V2 catalog extraction requires sourceOmissionReview")
            normalized_review = normalize_source_omission_review(
                raw_review,
                source_snapshot_sha256=source_snapshot_sha256,
                section_key=requested_section or None,
            )
            source_omissions = source_omission_review_index(normalized_review)
        elif state.get("sourceOmissionReview") is not None:
            raise ValueError("V1 catalog extraction does not accept sourceOmissionReview")
        active_section = _normalized_text(state.get("lastSectionKey"))
        previous_pma_row = (
            state.get("lastPmaRow")
            if isinstance(state.get("lastPmaRow"), Mapping)
            else None
        )
        seen_record_ids: set[str] = set()

        for page in pages:
            page_sections = detect_catalog_sections((page,))
            section_key = (
                page_sections[0].section_key
                if len(page_sections) == 1
                else active_section
            )
            if len(page_sections) == 1:
                active_section = page_sections[0].section_key
            owned = owned_page_start <= page.pdf_page <= owned_page_end
            if not owned:
                signals.append(
                    _signal(
                        "cross_part_context_used",
                        page=page.pdf_page,
                        message=(
                            f"context page {page.pdf_page} read for owned range "
                            f"{owned_page_start}-{owned_page_end}; output suppressed"
                        ),
                    )
                )
            if not section_key:
                if owned:
                    signals.append(
                        _signal(
                            "section_boundary_uncertain",
                            page=page.pdf_page,
                            message="no CAAC catalog section header recognized",
                        )
                    )
                continue
            if requested_section and section_key != requested_section:
                if owned:
                    signals.append(
                        _signal(
                            "section_boundary_uncertain",
                            page=page.pdf_page,
                            message=(
                                f"expected section {requested_section}, detected {section_key}"
                            ),
                        )
                    )
                continue
            rows, row_signals = extract_candidate_rows(page, section_key)
            if owned:
                signals.extend(row_signals)
            for raw_row in rows:
                row = raw_row
                inheritance_signals: tuple[Mapping[str, Any], ...] = ()
                if section_key == "pma_item":
                    row, inheritance_signals = _inherit_pma_merged_cells(
                        raw_row,
                        previous_pma_row,
                    )
                    previous_pma_row = row
                if not owned:
                    continue
                signals.extend(inheritance_signals)
                candidate_row = {
                    "sectionKey": section_key,
                    "sourceSnapshotSha256": source_snapshot_sha256,
                    **dict(row),
                }
                candidate_rows.append(candidate_row)
                record, record_signals = build_candidate_record(
                    row=row,
                    section_key=section_key,
                    source_snapshot_sha256=source_snapshot_sha256,
                    parser_version=parser_version,
                    extractor_version=self.extractor_version,
                    schema_version=self.schema_version,
                    source_omission=source_omissions.get(
                        (
                            section_key,
                            int(row.get("rowNumber") or 0),
                            int(row.get("pdfPage") or 0),
                            SOURCE_OMISSION_FIELD_PATH,
                        )
                    ),
                )
                signals.extend(record_signals)
                if record is None:
                    rejected_rows.append(
                        {
                            **candidate_row,
                            "qualitySignals": [
                                dict(signal) for signal in record_signals
                            ],
                        }
                    )
                    continue
                record_id = str(record.get("recordId") or "")
                if record_id in seen_record_ids:
                    signals.append(
                        _signal(
                            "part_overlap_duplicate_removed",
                            page=page.pdf_page,
                            message=f"duplicate recordId removed: {record_id}",
                        )
                    )
                    continue
                seen_record_ids.add(record_id)
                records.append(record)
                field_evidence.extend(
                    {
                        "recordId": record_id,
                        "sectionKey": section_key,
                        "rowNumber": record.get("rowNumber"),
                        **dict(evidence),
                    }
                    for evidence in record.get("fieldEvidence") or ()
                    if isinstance(evidence, Mapping)
                )

        state.update(
            {
                "lastSectionKey": active_section or None,
                "lastPdfPage": pages[-1].pdf_page if pages else state.get("lastPdfPage"),
                "lastPmaRow": dict(previous_pma_row) if previous_pma_row else None,
                "ownedPageStart": owned_page_start,
                "ownedPageEnd": owned_page_end,
            }
        )
        return CatalogExtractionResult(
            records=tuple(records),
            field_evidence=tuple(field_evidence),
            candidate_rows=tuple(candidate_rows),
            rejected_rows=tuple(rejected_rows),
            quality_signals=tuple(signals),
            continuation_state=state,
        )


class CaacApprovedCatalogV2Extractor(CaacApprovedCatalogV1Extractor):
    """V2 extractor with a fail-closed reviewed empty-holder exception."""

    schema_version = CAAC_APPROVED_CATALOG_V2_SCHEMA_VERSION
    extractor_version = CAAC_APPROVED_CATALOG_V2_EXTRACTOR_VERSION


__all__ = [
    "CaacApprovedCatalogV1Extractor",
    "CaacApprovedCatalogV2Extractor",
    "CatalogSectionSpan",
    "ColumnBoundary",
    "RowAnchor",
    "anchor_row_numbers",
    "detect_catalog_sections",
    "build_candidate_record",
    "extract_candidate_rows",
    "infer_catalog_header_columns",
    "map_words_to_columns",
]
