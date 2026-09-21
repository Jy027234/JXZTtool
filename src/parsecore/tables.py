"""One table grid contract, shared by every projection.

A table reaches the API as block metadata.  Parsers that can read real structure
(Word today, PDF/Excel next) publish ``table_cells``: one record per grid
position with its row/column, span, and origin.  Parsers that only have strings
publish ``cells``: a plain list of rows.

Before this module existed, Reader, IR, knowledge units and records each rebuilt
rows from whichever key they happened to look at.  A merged Word header was then
guessed differently by each consumer, or silently collapsed.  The rules below are
the single place that decides:

- how a grid is reconstructed from either key,
- which text a merged region contributes to a flattened row
  (``fill_merged``: the value of the merge origin, so a flattened row still
  carries the value it is governed by),
- how a table is rendered as Markdown/TSV.

Derived text keeps the origin value in every spanned position; the structured
cells keep ``row_span``/``col_span``/``continuation`` so a consumer that can draw
a real merged cell does not have to re-infer it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: Structured grid key: one record per grid position, merges preserved.
TABLE_CELLS_KEY = "table_cells"
#: Flat key: rows of strings.  Derived from the grid when the parser has one.
CELLS_KEY = "cells"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class GridCell:
    """One cell at its real grid position."""

    row_index: int
    col_index: int
    text: str = ""
    row_span: int = 1
    col_span: int = 1
    #: ``True`` for the grid positions a merged cell spans beyond its origin.
    continuation: bool = False
    origin_row: int = 0
    origin_col: int = 0
    runs: tuple[dict[str, Any], ...] = field(default=())

    @property
    def origin(self) -> str:
        return f"{self.origin_row}.{self.origin_col}"

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "row_index": self.row_index,
            "col_index": self.col_index,
            "text": self.text,
            "row_span": self.row_span,
            "col_span": self.col_span,
            "continuation": self.continuation,
            "origin": self.origin,
        }
        if self.runs:
            payload["runs"] = [dict(run) for run in self.runs]
        return payload


@dataclass(frozen=True, slots=True)
class TableGrid:
    """Cells laid out on their real column grid."""

    cells: tuple[GridCell, ...] = ()
    row_count: int = 0
    column_count: int = 0
    #: Columns each *source* row declared, indexed by row.  A row that declares
    #: fewer columns than the table grid is a source defect worth reporting, and
    #: padding the row to the grid width would hide it.
    declared_row_widths: tuple[int, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.cells

    def origin_cells(self) -> tuple[GridCell, ...]:
        return tuple(cell for cell in self.cells if not cell.continuation)

    def cell_at(self, row_index: int, col_index: int) -> GridCell | None:
        for cell in self.cells:
            if cell.row_index == row_index and cell.col_index == col_index:
                return cell
        return None

    def origin_for(self, cell: GridCell) -> GridCell:
        """The cell that carries the value for a merge continuation."""

        if not cell.continuation:
            return cell
        return self.cell_at(cell.origin_row, cell.origin_col) or cell

    def rows(self, *, fill_merged: bool = True) -> list[list[str]]:
        """Rectangular rows, padded to the grid width.

        ``fill_merged`` repeats a merged cell's value into the positions it
        spans.  A flattened row is consumed as plain columns, so an empty
        continuation would read as "this column has no value" for a row the
        merge actually governs.
        """

        if not self.cells:
            return []
        width = self.column_count or max(cell.col_index for cell in self.cells) + 1
        height = self.row_count or max(cell.row_index for cell in self.cells) + 1
        # Resolve origins through an index: a ledger table can continue one merge
        # down thousands of rows, and a per-cell scan would be quadratic.
        by_position = {(cell.row_index, cell.col_index): cell for cell in self.cells}
        grid: dict[int, dict[int, str]] = {}
        for cell in self.cells:
            text = _as_text(cell.text)
            if fill_merged and cell.continuation:
                origin = by_position.get((cell.origin_row, cell.origin_col))
                if origin is not None:
                    text = _as_text(origin.text)
            grid.setdefault(cell.row_index, {})[cell.col_index] = text
        return [
            [grid.get(row_index, {}).get(col_index, "") for col_index in range(width)]
            for row_index in range(height)
        ]

    def header_values(self, header_rows: int = 1) -> list[str]:
        """Header text per column, with merged headers expanded over their span.

        Levels are joined for a multi-row header.  A repeated value is skipped so
        a vertically merged header is not reported twice.
        """

        rows = self.rows(fill_merged=True)
        if not rows or header_rows <= 0:
            return []
        width = max(len(row) for row in rows)
        headers = ["" for _ in range(width)]
        for row_index in range(min(header_rows, len(rows))):
            row = rows[row_index]
            for col_index in range(width):
                value = (row[col_index] if col_index < len(row) else "").strip()
                if not value or value == headers[col_index]:
                    continue
                headers[col_index] = (
                    f"{headers[col_index]} {value}".strip() if headers[col_index] else value
                )
        return headers

    def merged_cells(self) -> list[dict[str, Any]]:
        """Merge descriptors, derived from the spans the parser recorded."""

        merges: list[dict[str, Any]] = []
        for cell in self.cells:
            if cell.row_span <= 1 and cell.col_span <= 1:
                continue
            merge: dict[str, Any] = {
                "kind": "col_span" if cell.col_span > 1 else "row_span",
                "origin": cell.origin,
                "row_index": cell.row_index,
                "col_index": cell.col_index,
                "row_span": cell.row_span,
                "col_span": cell.col_span,
            }
            merges.append(merge)
        return merges


def cells_from_payload(raw_cells: Any) -> tuple[GridCell, ...]:
    """Read structured ``table_cells`` records (or their API projection)."""

    if not isinstance(raw_cells, Sequence) or isinstance(raw_cells, (str, bytes, bytearray)):
        return ()
    cells: list[GridCell] = []
    for item in raw_cells:
        if not isinstance(item, Mapping):
            continue
        row_index = _as_int(item.get("row_index", item.get("row")), default=0)
        col_index = _as_int(item.get("col_index", item.get("col")), default=0)
        origin_row = _as_int(item.get("origin_row"), default=-1)
        origin_col = _as_int(item.get("origin_col"), default=-1)
        if origin_row < 0 or origin_col < 0:
            origin_parts = str(item.get("origin") or "").split(".")
            if len(origin_parts) == 2:
                origin_row = _as_int(origin_parts[0], default=row_index)
                origin_col = _as_int(origin_parts[1], default=col_index)
            else:
                origin_row, origin_col = row_index, col_index
        runs = item.get("runs")
        cells.append(
            GridCell(
                row_index=row_index,
                col_index=col_index,
                text=_as_text(item.get("text")),
                row_span=max(1, _as_int(item.get("row_span"), default=1) or 1),
                col_span=max(1, _as_int(item.get("col_span"), default=1) or 1),
                continuation=bool(item.get("continuation")),
                origin_row=origin_row,
                origin_col=origin_col,
                runs=tuple(dict(run) for run in runs if isinstance(run, Mapping))
                if isinstance(runs, Sequence) and not isinstance(runs, (str, bytes, bytearray))
                else (),
            )
        )
    return tuple(cells)


def grid_from_cells(raw_cells: Any) -> TableGrid:
    """Build a grid from structured records, deriving spans from the positions."""

    cells = cells_from_payload(raw_cells)
    if not cells:
        return TableGrid()
    return _finalize(cells)


def grid_from_rows(rows: Any) -> TableGrid:
    """Build a grid from plain rows of strings (the ``cells`` contract)."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return TableGrid()
    cells: list[GridCell] = []
    for row_index, row in enumerate(rows):
        if isinstance(row, (str, bytes, bytearray)):
            row = [row]
        if not isinstance(row, Sequence):
            continue
        for col_index, value in enumerate(row):
            cells.append(
                GridCell(
                    row_index=row_index,
                    col_index=col_index,
                    text=_as_text(value),
                    origin_row=row_index,
                    origin_col=col_index,
                )
            )
    if not cells:
        return TableGrid()
    return _finalize(cells)


def grid_from_metadata(metadata: Mapping[str, Any] | None) -> TableGrid:
    """The grid a block actually carries, preferring real structure.

    ``table_cells`` is authoritative when present; ``cells`` (and the truncated
    ``cells_preview`` for oversized tables) is the flat fallback.
    """

    if not isinstance(metadata, Mapping):
        return TableGrid()
    grid = grid_from_cells(metadata.get(TABLE_CELLS_KEY))
    if not grid.empty:
        return grid
    raw = metadata.get(CELLS_KEY)
    if raw is None:
        raw = metadata.get("cells_preview")
    return grid_from_rows(raw)


def rows_from_table_payload(
    table: Mapping[str, Any] | None, *, fill_merged: bool = True
) -> list[list[str]]:
    """Rows for a projected table record (structured cells first)."""

    return resolve_table_grid(table).rows(fill_merged=fill_merged)


def resolve_table_grid(table: Mapping[str, Any] | None) -> TableGrid:
    """The grid behind a projected table record."""

    if not isinstance(table, Mapping):
        return TableGrid()
    grid = grid_from_cells(table.get("cells"))
    if not grid.empty:
        return grid
    return grid_from_cells(table.get(TABLE_CELLS_KEY))


def _finalize(cells: Iterable[GridCell]) -> TableGrid:
    materialized = tuple(cells)
    row_count = max(cell.row_index for cell in materialized) + 1
    column_count = max(cell.col_index for cell in materialized) + 1
    return TableGrid(cells=materialized, row_count=row_count, column_count=column_count)


def render_markdown(rows: Sequence[Sequence[Any]], *, header_rows: int = 1) -> str:
    """Markdown table.  A merge a cell spans is expressed by repeating its value.

    Markdown cannot express ``rowspan``/``colspan``, so flattened text repeats the
    merged value; the structured cells keep the real span for consumers that can
    use it.
    """

    padded = _pad_rows(rows, newline_replacement=None)
    if not padded:
        return ""
    width = len(padded[0])
    header_count = min(max(1, header_rows), len(padded))
    header = padded[0]
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    body = padded[header_count:]
    if not body and header_count > 1:
        body = padded[1:]
    lines.extend(
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |" for row in body
    )
    return "\n".join(lines)


def render_tsv(rows: Sequence[Sequence[Any]]) -> str:
    padded = _pad_rows(rows, newline_replacement=" ")
    if not padded:
        return ""
    return "\n".join("\t".join(cell for cell in row) for row in padded)


def _escape_markdown_cell(value: Any) -> str:
    return _as_text(value).replace("|", "\\|").replace("\n", "<br>").strip()


def _pad_rows(
    rows: Sequence[Sequence[Any]], *, newline_replacement: str | None = " "
) -> list[list[str]]:
    """Drop empty rows, trim each cell, pad every row to the widest.

    ``newline_replacement`` keeps an embedded newline from breaking a
    line-oriented format; Markdown passes ``None`` so the cell can be rendered
    with an explicit ``<br>`` instead.
    """

    normalized: list[list[str]] = []
    for row in rows or ():
        if isinstance(row, (str, bytes, bytearray)):
            row = [row]
        if not isinstance(row, Sequence):
            continue
        values: list[str] = []
        for cell in row:
            text = _as_text(cell).replace("\r\n", "\n").strip()
            if newline_replacement is not None:
                text = text.replace("\n", newline_replacement).strip()
            values.append(text)
        if not any(values):
            continue
        normalized.append(values)
    if not normalized:
        return []
    width = max(len(row) for row in normalized)
    if width <= 0:
        return []
    return [row + [""] * (width - len(row)) for row in normalized]
