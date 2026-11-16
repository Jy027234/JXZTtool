"""Conservative row recovery for unruled illustrated parts list tables."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_PDF_IPL_TABLE_HEADER = (
    "FIG-ITEM",
    "PART NUMBER",
    "NOMENCLATURE",
    "USAGE CODE",
    "UNITS PER ASSY",
)
_PDF_IPL_ROW_ALIGNMENT_TOLERANCE = 2.0


class PdfIplRowExtractionError(RuntimeError):
    """A crop or line extraction failed after IPL recovery became applicable."""


def _coerce_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not (result == result):
        return None
    return result


def recover_pdf_ipl_table_rows(
    page: Any,
    table: Any,
    cells: list[list[str]],
) -> list[list[str]] | None:
    """Recover visual IPL rows while leaving uncertain geometry untouched.

    ``None`` means the base ``Table.extract()`` result should be retained.
    Crop and line extraction errors are wrapped so the caller can log their
    page context and retain that result.
    """

    if (
        len(cells) != 2
        or len(cells[0]) != len(_PDF_IPL_TABLE_HEADER)
        or len(cells[1]) != len(_PDF_IPL_TABLE_HEADER)
    ):
        return None
    normalized_header = [" ".join(str(value or "").split()).upper() for value in cells[0]]
    if (
        normalized_header[0] != _PDF_IPL_TABLE_HEADER[0]
        or normalized_header[1] != _PDF_IPL_TABLE_HEADER[1]
        or re.fullmatch(r"NOMENCLATURE(?:\s+\d+)*", normalized_header[2]) is None
        or normalized_header[3] != _PDF_IPL_TABLE_HEADER[3]
        or normalized_header[4] != _PDF_IPL_TABLE_HEADER[4]
    ):
        return None

    table_rows = getattr(table, "rows", ()) or ()
    if len(table_rows) != 2:
        return None
    header_cells = tuple(getattr(table_rows[0], "cells", ()) or ())
    data_cells = tuple(getattr(table_rows[1], "cells", ()) or ())
    if (
        len(header_cells) != 5
        or len(data_cells) != 5
        or any(bbox is None for bbox in (*header_cells, *data_cells))
    ):
        return None

    data_row_bbox = getattr(table_rows[1], "bbox", None)
    if not isinstance(data_row_bbox, Sequence) or len(data_row_bbox) != 4:
        return None
    data_top = _coerce_float(data_row_bbox[1])
    data_bottom = _coerce_float(data_row_bbox[3])
    if data_top is None or data_bottom is None or data_bottom <= data_top:
        return None

    line_columns: list[list[tuple[float, str]]] = []
    for column_index, bbox in enumerate(data_cells):
        if not isinstance(bbox, Sequence) or len(bbox) != 4:
            return None
        normalized = tuple(_coerce_float(value) for value in bbox)
        if any(value is None for value in normalized):
            return None
        x0, top, x1, bottom = normalized
        if (
            x1 <= x0
            or abs(top - data_top) > _PDF_IPL_ROW_ALIGNMENT_TOLERANCE
            or abs(bottom - data_bottom) > _PDF_IPL_ROW_ALIGNMENT_TOLERANCE
        ):
            return None
        try:
            extracted = page.crop(bbox).extract_text_lines(layout=False, strip=True)
        except Exception as exc:
            raise PdfIplRowExtractionError(
                "could not read text lines from an IPL table cell crop"
            ) from exc
        column_lines: list[tuple[float, str]] = []
        for line in extracted or ():
            if not isinstance(line, Mapping):
                return None
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            line_top = _coerce_float(line.get("top"))
            line_bottom = _coerce_float(line.get("bottom"))
            if line_top is None or line_bottom is None or line_bottom < line_top:
                return None
            column_lines.append(((line_top + line_bottom) / 2.0, text))
        column_lines.sort(key=lambda item: item[0])
        source_text = "".join(str(cells[1][column_index] or "").split())
        recovered_text = "".join("".join(text for _center, text in column_lines).split())
        if source_text != recovered_text:
            return None
        line_columns.append(column_lines)

    item_lines = line_columns[0]
    part_lines = line_columns[1]
    if len(item_lines) < 2 or len(item_lines) != len(part_lines):
        return None

    anchor_centers: list[float] = []
    for item_center, _item_text in item_lines:
        matches = [
            (index, part_center)
            for index, (part_center, _part_text) in enumerate(part_lines)
            if abs(part_center - item_center) <= _PDF_IPL_ROW_ALIGNMENT_TOLERANCE
        ]
        if len(matches) != 1:
            return None
        part_index, part_center = matches[0]
        if part_index != len(anchor_centers):
            return None
        anchor_centers.append((item_center + part_center) / 2.0)
    if len(anchor_centers) != len(part_lines) or any(
        right - left <= 2 * _PDF_IPL_ROW_ALIGNMENT_TOLERANCE
        for left, right in zip(anchor_centers, anchor_centers[1:])
    ):
        return None

    recovered = [[*cells[0]]]
    row_cells: list[list[str]] = [
        [item_lines[index][1], part_lines[index][1], "", "", ""]
        for index in range(len(anchor_centers))
    ]

    # Usage and quantity values only attach to an anchor at the same baseline.
    # A displaced or repeated value is ambiguous, so let the original extraction
    # stand instead of shifting it into a neighboring record.
    for column_index in (3, 4):
        assigned_rows: set[int] = set()
        for center, text in line_columns[column_index]:
            matches = [
                row_index
                for row_index, anchor in enumerate(anchor_centers)
                if abs(center - anchor) <= _PDF_IPL_ROW_ALIGNMENT_TOLERANCE
            ]
            if len(matches) != 1 or matches[0] in assigned_rows:
                return None
            row_index = matches[0]
            row_cells[row_index][column_index] = text
            assigned_rows.add(row_index)

    # Descriptions and section markers retain their source order and stay in the
    # NOMENCLATURE column. Text between anchors belongs to the prior row until
    # the next anchor begins; it never creates an inferred record.
    description_lines = line_columns[2]
    if description_lines and description_lines[0][0] < anchor_centers[0] - _PDF_IPL_ROW_ALIGNMENT_TOLERANCE:
        return None
    description_rows: list[list[str]] = [[] for _ in anchor_centers]
    for center, text in description_lines:
        row_index = 0
        for candidate_index, anchor in enumerate(anchor_centers):
            if center >= anchor - _PDF_IPL_ROW_ALIGNMENT_TOLERANCE:
                row_index = candidate_index
            else:
                break
        description_rows[row_index].append(text)
    for row_index, text_lines in enumerate(description_rows):
        row_cells[row_index][2] = "\n".join(text_lines)

    recovered.extend(row_cells)
    return recovered
