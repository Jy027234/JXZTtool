from __future__ import annotations

from bisect import bisect_left
from typing import Any, Mapping, Sequence


def figure_text_breaks(
    text: str,
    *,
    lines: Sequence[Mapping[str, Any]],
    figure_bboxes: Sequence[tuple[float, float, float, float]],
    page_width: float,
    page_height: float,
) -> dict[tuple[float, float, float, float], int]:
    """Separate native text across figures when the original lines can be located."""
    source_lines = text.split("\n")
    nonblank = [(index, value.strip()) for index, value in enumerate(source_lines) if value.strip()]
    if [value for _, value in nonblank] != [str(line["text"]).strip() for line in lines]:
        return {}
    tops = [float(line["top"]) for line in lines]
    if not tops or tops != sorted(tops):
        return {}
    breaks: dict[tuple[float, float, float, float], int] = {}
    for bbox in figure_bboxes:
        next_line = bisect_left(tops, bbox[3])
        if any(float(line["bottom"]) > bbox[1] for line in lines[:next_line]):
            continue
        breaks[bbox] = nonblank[next_line][0] if next_line < len(lines) else len(source_lines)
    if not breaks:
        return {}

    # An interruption between body paragraphs or a body section between two
    # figures can be located reliably. A cover, a running identifier or the
    # label of a standalone diagram does not provide that evidence.
    body_lines = [
        source_index
        for (source_index, value), line in zip(nonblank, lines, strict=True)
        if page_height * 0.12 < float(line["top"]) < page_height * 0.9
        and float(line["x1"]) - float(line["x0"]) >= page_width * 0.4
        and value.endswith((".", "。", "!", "！", "?", "？"))
    ]
    if not body_lines:
        return {}
    boundaries = breaks.values()
    separates_body = any(body_lines[0] < boundary <= body_lines[-1] for boundary in boundaries)
    surrounds_body = min(boundaries) == nonblank[0][0] and body_lines[-1] < max(boundaries)
    return breaks if separates_body or surrounds_body else {}


def build_page_content_sequence(
    paragraphs: Sequence[str],
    *,
    page_layout: Any | None,
    figure_anchors: Mapping[int, int] | None = None,
) -> list[tuple[str, int]]:
    paragraph_count = len(paragraphs)
    tables = list(getattr(page_layout, "tables", []) or [])
    figures = list(getattr(page_layout, "figure_regions", []) or [])
    if not tables and not figures:
        return [("paragraph", index) for index in range(paragraph_count)]

    height = float(page_layout.height or 0.0)
    items: list[tuple[int, float, str, int]] = []
    for kind, regions in (("table", tables), ("image", figures)):
        for index, region in enumerate(regions):
            top = float(region.bbox[1]) if len(region.bbox) >= 2 else 0.0
            if kind == "image" and figure_anchors is not None and index in figure_anchors:
                anchor = figure_anchors[index]
            elif height > 0.0:
                anchor = round((max(0.0, min(top, height)) / height) * paragraph_count)
            else:
                anchor = paragraph_count
            items.append((anchor, top, kind, index))
    items.sort()

    sequence: list[tuple[str, int]] = []
    cursor = 0
    for paragraph_index in range(paragraph_count + 1):
        while cursor < len(items) and items[cursor][0] == paragraph_index:
            sequence.append((items[cursor][2], items[cursor][3]))
            cursor += 1
        if paragraph_index < paragraph_count:
            sequence.append(("paragraph", paragraph_index))
    return sequence
