"""Excel-native parsing and spreadsheet-only helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ParserAdapter
from .models import Block, BlockType, ParseRequest, SemanticRole
from .provider_metadata import _provider_metadata
from .tables import GridCell, TableGrid, render_markdown as _render_table_markdown


def _normalize_excel_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value).strip()


def _excel_column_letter(index: int) -> str:
    value = max(1, int(index))
    letters: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


def _excel_cell_ref(row: int, col: int) -> str:
    return f"{_excel_column_letter(col)}{row}"


def _trim_excel_rows(rows: Sequence[Sequence[str]]) -> tuple[list[list[str]], int, int]:
    non_empty_row_indexes = [
        index for index, row in enumerate(rows) if any(str(cell or "").strip() for cell in row)
    ]
    if not non_empty_row_indexes:
        return [], 0, 0
    first_row = non_empty_row_indexes[0]
    last_row = non_empty_row_indexes[-1]
    width = max((len(row) for row in rows), default=0)
    non_empty_col_indexes = [
        index
        for index in range(width)
        if any(
            index < len(row) and str(row[index] or "").strip()
            for row in rows[first_row : last_row + 1]
        )
    ]
    if not non_empty_col_indexes:
        return [], 0, 0
    first_col = non_empty_col_indexes[0]
    last_col = non_empty_col_indexes[-1]
    trimmed = [
        list(row[first_col : last_col + 1])
        for row in rows[first_row : last_row + 1]
    ]
    return trimmed, first_row + 1, first_col + 1


def _split_excel_table_regions(rows: Sequence[Sequence[str]]) -> list[tuple[list[list[str]], int]]:
    regions: list[tuple[list[list[str]], int]] = []
    current: list[list[str]] = []
    start_row = 0
    normalized_rows = [[str(cell or "").strip() for cell in row] for row in rows]
    for offset, row in enumerate(normalized_rows):
        index = offset + 1
        normalized = [str(cell or "").strip() for cell in row]
        next_row = normalized_rows[offset + 1] if offset + 1 < len(normalized_rows) else []
        starts_new_titled_region = (
            bool(current)
            and _excel_non_empty_count(normalized) == 1
            and _excel_non_empty_count(next_row) >= 2
        )
        if any(normalized) and starts_new_titled_region:
            regions.append((current, start_row))
            current = [normalized]
            start_row = index
            continue
        if any(normalized):
            if not current:
                start_row = index
            current.append(normalized)
            continue
        if current:
            regions.append((current, start_row))
            current = []
            start_row = 0
    if current:
        regions.append((current, start_row))
    return regions


def _excel_non_empty_count(row: Sequence[str]) -> int:
    return sum(1 for cell in row if str(cell or "").strip())


def _detect_excel_table_layout(
    rows: Sequence[Sequence[str]],
    *,
    start_row: int,
) -> tuple[list[list[str]], int, str | None, int | None, int | None]:
    normalized_rows = [[str(cell or "").strip() for cell in row] for row in rows]
    if (
        len(normalized_rows) >= 2
        and _excel_non_empty_count(normalized_rows[0]) == 1
        and _excel_non_empty_count(normalized_rows[1]) >= 2
    ):
        title = next(cell for cell in normalized_rows[0] if cell)
        header_row = start_row + 1
        return normalized_rows[1:], header_row, title, start_row, header_row
    header_row = start_row if normalized_rows else None
    return normalized_rows, start_row, None, None, header_row


def _excel_range_ref(start_row: int, start_col: int, end_row: int, end_col: int) -> str:
    return f"{_excel_cell_ref(start_row, start_col)}:{_excel_cell_ref(end_row, end_col)}"


def _excel_range_intersects(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    left_min_row, left_min_col, left_max_row, left_max_col = left
    right_min_row, right_min_col, right_max_row, right_max_col = right
    return not (
        left_max_row < right_min_row
        or right_max_row < left_min_row
        or left_max_col < right_min_col
        or right_max_col < left_min_col
    )


def _excel_table_grid(
    rows: Sequence[Sequence[str]],
    *,
    merged_ranges: Sequence[tuple[str, tuple[int, int, int, int]]],
    start_row: int,
    start_col: int,
) -> tuple[TableGrid, list[str]]:
    """Excel rows as the shared grid, with merged ranges turned into spans.

    Excel already knows which cells are merged, but the parser published those as
    range labels while Word/PDF/Docling publish span records.  Two shapes for one
    contract meant a consumer had to branch per format -- and the IR schema expects
    span objects, so a label list could not even be carried through.  Merges inside
    the table become spans here; merges outside it (a title row above the table)
    are returned as labels, because they are a fact about the source range rather
    than a span of this table.
    """

    cells = [
        GridCell(
            row_index=row_index,
            col_index=col_index,
            text=str(value if value is not None else ""),
            origin_row=row_index,
            origin_col=col_index,
        )
        for row_index, row in enumerate(rows)
        for col_index, value in enumerate(row)
    ]
    row_count = len(rows)
    column_count = max((len(row) for row in rows), default=0)
    outside: list[str] = []
    by_position = {(cell.row_index, cell.col_index): index for index, cell in enumerate(cells)}
    for label, (row_low, col_low, row_high, col_high) in merged_ranges:
        origin_row = int(row_low) - int(start_row)
        origin_col = int(col_low) - int(start_col)
        row_span = int(row_high) - int(row_low) + 1
        col_span = int(col_high) - int(col_low) + 1
        origin_index = by_position.get((origin_row, origin_col))
        if row_span <= 1 and col_span <= 1:
            continue
        if origin_index is None or origin_row < 0 or origin_col < 0:
            outside.append(str(label))
            continue
        origin = cells[origin_index]
        cells[origin_index] = replace(origin, row_span=row_span, col_span=col_span)
        for extra_row in range(origin_row, origin_row + row_span):
            for extra_col in range(origin_col, origin_col + col_span):
                if (extra_row, extra_col) == (origin_row, origin_col):
                    continue
                if by_position.get((extra_row, extra_col)) is None:
                    continue
                cells.append(
                    GridCell(
                        row_index=extra_row,
                        col_index=extra_col,
                        continuation=True,
                        origin_row=origin_row,
                        origin_col=origin_col,
                    )
                )
    cells.sort(key=lambda cell: (cell.row_index, cell.col_index))
    return (
        TableGrid(cells=tuple(cells), row_count=row_count, column_count=column_count),
        outside,
    )


def _excel_cells_metadata(rows: Sequence[Sequence[str]], *, max_cells: int) -> dict[str, Any]:
    row_count = len(rows)
    col_count = max((len(row) for row in rows), default=0)
    total_cells = row_count * col_count
    if total_cells <= max(0, int(max_cells)):
        return {
            "cells": [list(row) for row in rows],
            "cells_truncated": False,
            "cells_total": total_cells,
        }
    preview_rows = max(1, min(row_count, max(1, int(max_cells)) // max(col_count, 1)))
    return {
        "cells_preview": [list(row) for row in rows[:preview_rows]],
        "cells_truncated": True,
        "cells_total": total_cells,
        "cells_preview_rows": preview_rows,
    }


def _render_excel_table_markdown(rows: Sequence[Sequence[str]]) -> str:
    return _render_table_markdown(rows)


class ExcelParser(ParserAdapter):
    name = "excel-native"

    def __init__(
        self,
        *,
        media_types: Sequence[str],
        extensions: Sequence[str],
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._media_types = {item.lower() for item in media_types}
        self._extensions = {item.lower() for item in extensions}
        self._options = dict(options or {})
        self._data_only = bool(self._options.get("data_only", False))
        self._include_hidden_sheets = bool(self._options.get("include_hidden_sheets", True))
        self._max_rows_per_sheet = max(1, int(self._options.get("max_rows_per_sheet", 5000)))
        self._max_cols_per_sheet = max(1, int(self._options.get("max_cols_per_sheet", 100)))
        self._max_metadata_cells = max(1, int(self._options.get("max_metadata_cells", 1000)))

    def supports(self, *, media_type: str | None, suffix: str) -> bool:
        normalized_type = (media_type or "").lower()
        normalized_suffix = suffix.lower()
        return normalized_type in self._media_types or normalized_suffix in self._extensions

    def parse(self, request: ParseRequest) -> Sequence[Block]:
        document_path = Path(request.file_path)
        blocks: list[Block] = [self._title_block(request=request, document_path=document_path)]
        suffix = document_path.suffix.lower()
        if suffix == ".xls" or (
            suffix not in {".xlsx", ".xlsm"}
            and (request.media_type or "").lower() == "application/vnd.ms-excel"
        ):
            blocks.extend(self._parse_xls(request=request, document_path=document_path))
            return tuple(blocks)
        blocks.extend(self._parse_xlsx(request=request, document_path=document_path))
        return tuple(blocks)

    def _title_block(self, *, request: ParseRequest, document_path: Path) -> Block:
        return Block(
            block_id=f"{request.doc_id}-title",
            doc_id=request.doc_id,
            type=BlockType.TITLE,
            content=document_path.stem,
            metadata=_provider_metadata({
                "page": 1,
                "page_type": "body",
                "kind": BlockType.TITLE.value,
                "semantic_role": SemanticRole.TITLE.value,
            }, parser_name=self.name),
        )

    def _parse_xlsx(self, *, request: ParseRequest, document_path: Path) -> list[Block]:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError(
                "excel-native parser requires openpyxl; install parsecore-starter[parsers]"
            ) from exc

        merged_by_sheet: dict[str, list[tuple[str, tuple[int, int, int, int]]]] = {}
        metadata_workbook = None
        try:
            metadata_workbook = load_workbook(
                filename=document_path,
                read_only=False,
                data_only=self._data_only,
            )
            for worksheet in metadata_workbook.worksheets:
                merged_by_sheet[str(worksheet.title)] = [
                    (
                        str(merged_range),
                        (
                            int(merged_range.min_row),
                            int(merged_range.min_col),
                            int(merged_range.max_row),
                            int(merged_range.max_col),
                        ),
                    )
                    for merged_range in worksheet.merged_cells.ranges
                ]
        finally:
            if metadata_workbook is not None:
                close_metadata = getattr(metadata_workbook, "close", None)
                if callable(close_metadata):
                    close_metadata()

        workbook = load_workbook(
            filename=document_path,
            read_only=True,
            data_only=self._data_only,
        )
        blocks: list[Block] = []
        try:
            position = 1
            for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
                hidden_sheet = str(getattr(worksheet, "sheet_state", "visible")) != "visible"
                if hidden_sheet and not self._include_hidden_sheets:
                    continue

                sheet_max_row = min(int(worksheet.max_row or 1), self._max_rows_per_sheet)
                sheet_max_col = min(int(worksheet.max_column or 1), self._max_cols_per_sheet)
                raw_rows: list[list[str]] = []
                formula_cells: set[tuple[int, int]] = set()
                for row in worksheet.iter_rows(
                    min_row=1,
                    max_row=sheet_max_row,
                    min_col=1,
                    max_col=sheet_max_col,
                ):
                    values: list[str] = []
                    for cell in row:
                        value = getattr(cell, "value", None)
                        is_formula = getattr(cell, "data_type", None) == "f" or (
                            isinstance(value, str) and value.startswith("=")
                        )
                        if is_formula:
                            formula_cells.add((int(getattr(cell, "row", 0)), int(getattr(cell, "column", 0))))
                        values.append(_normalize_excel_cell_value(value))
                    raw_rows.append(values)
                appended = self._append_sheet_blocks(
                    blocks=blocks,
                    request=request,
                    sheet_name=str(worksheet.title),
                    sheet_index=sheet_index,
                    hidden_sheet=hidden_sheet,
                    raw_rows=raw_rows,
                    formula_cells=formula_cells,
                    merged_ranges=merged_by_sheet.get(str(worksheet.title), []),
                    original_max_row=int(worksheet.max_row or 0),
                    original_max_col=int(worksheet.max_column or 0),
                    position=position,
                )
                position += appended
        finally:
            close = getattr(workbook, "close", None)
            if callable(close):
                close()
        return blocks

    def _parse_xls(self, *, request: ParseRequest, document_path: Path) -> list[Block]:
        try:
            import xlrd
        except ImportError as exc:
            raise RuntimeError(
                "excel-native parser requires xlrd for .xls files; install parsecore-starter[parsers]"
            ) from exc

        try:
            workbook = xlrd.open_workbook(document_path, formatting_info=True)
        except NotImplementedError:
            workbook = xlrd.open_workbook(document_path, formatting_info=False)
        blocks: list[Block] = []
        position = 1
        for sheet_index, worksheet in enumerate(workbook.sheets(), start=1):
            hidden_sheet = int(getattr(worksheet, "visibility", 0)) != 0
            if hidden_sheet and not self._include_hidden_sheets:
                continue
            sheet_max_row = min(int(worksheet.nrows or 1), self._max_rows_per_sheet)
            sheet_max_col = min(int(worksheet.ncols or 1), self._max_cols_per_sheet)
            raw_rows: list[list[str]] = []
            for row_index in range(sheet_max_row):
                values: list[str] = []
                for col_index in range(sheet_max_col):
                    cell = worksheet.cell(row_index, col_index)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate_as_datetime(cell.value, workbook.datemode)
                        except (TypeError, ValueError, OverflowError):
                            value = cell.value
                    else:
                        value = cell.value
                    values.append(_normalize_excel_cell_value(value))
                raw_rows.append(values)
            merged_ranges = [
                (
                    _excel_range_ref(row_low + 1, col_low + 1, row_high, col_high),
                    (row_low + 1, col_low + 1, row_high, col_high),
                )
                for row_low, row_high, col_low, col_high in getattr(worksheet, "merged_cells", [])
            ]
            appended = self._append_sheet_blocks(
                blocks=blocks,
                request=request,
                sheet_name=str(worksheet.name),
                sheet_index=sheet_index,
                hidden_sheet=hidden_sheet,
                raw_rows=raw_rows,
                formula_cells=set(),
                merged_ranges=merged_ranges,
                original_max_row=int(worksheet.nrows or 0),
                original_max_col=int(worksheet.ncols or 0),
                position=position,
            )
            position += appended
        return blocks

    def _append_sheet_blocks(
        self,
        *,
        blocks: list[Block],
        request: ParseRequest,
        sheet_name: str,
        sheet_index: int,
        hidden_sheet: bool,
        raw_rows: Sequence[Sequence[str]],
        formula_cells: set[tuple[int, int]],
        merged_ranges: Sequence[tuple[str, tuple[int, int, int, int]]],
        original_max_row: int,
        original_max_col: int,
        position: int,
    ) -> int:
        table_regions = _split_excel_table_regions(raw_rows)
        appended = 0
        table_count = len(table_regions)
        for table_index, (region_rows, region_start_row) in enumerate(table_regions, start=1):
            candidate_rows, candidate_start_row, table_title, title_row, header_row = _detect_excel_table_layout(
                region_rows,
                start_row=region_start_row,
            )
            rows, relative_start_row, start_col = _trim_excel_rows(candidate_rows)
            if not rows:
                continue
            start_row = candidate_start_row + relative_start_row - 1
            grid, outside_merges = _excel_table_grid(
                rows,
                merged_ranges=[
                    (label, merged_range)
                    for label, merged_range in merged_ranges
                    if _excel_range_intersects(merged_range, (
                        title_row or start_row,
                        start_col,
                        start_row + len(rows) - 1,
                        start_col + max((len(row) for row in rows), default=1) - 1,
                    ))
                ],
                start_row=start_row,
                start_col=start_col,
            )
            # The merged values fill their spanned positions, so the flat rows, the
            # rendered text and the structured cells all describe one grid.
            merged_rows = grid.rows()
            content = _render_excel_table_markdown(merged_rows)
            if not content:
                continue
            end_row = start_row + len(rows) - 1
            end_col = start_col + max((len(row) for row in rows), default=1) - 1
            source_start_row = title_row or start_row
            source_range = (source_start_row, start_col, end_row, end_col)
            table_range = (start_row, start_col, end_row, end_col)
            table_formula_cells = {
                cell for cell in formula_cells if _excel_range_intersects((cell[0], cell[1], cell[0], cell[1]), table_range)
            }
            merges = grid.merged_cells()
            metadata: dict[str, Any] = _provider_metadata({
                "page": sheet_index,
                "logical_page": sheet_index,
                "page_type": "body",
                "position": position + appended,
                "semantic_role": SemanticRole.TABLE.value,
                "kind": BlockType.TABLE.value,
                "table_type": "spreadsheet",
                "sheet_name": sheet_name,
                "sheet_index": sheet_index,
                "sheet_table_index": table_index,
                "sheet_table_count": table_count,
                "hidden_sheet": hidden_sheet,
                "row_range": f"{start_row}:{end_row}",
                "column_range": f"{_excel_column_letter(start_col)}:{_excel_column_letter(end_col)}",
                "cell_range": _excel_range_ref(start_row, start_col, end_row, end_col),
                "source_cell_range": _excel_range_ref(source_start_row, start_col, end_row, end_col),
                "rows": len(merged_rows),
                "cols": max((len(row) for row in merged_rows), default=0),
                "header_rows": 1 if merged_rows else 0,
                "header_row": header_row,
                "header_values": grid.header_values(1),
                "has_formula": bool(table_formula_cells),
                "formula_count": len(table_formula_cells),
                "truncated": (
                    original_max_row > self._max_rows_per_sheet
                    or original_max_col > self._max_cols_per_sheet
                ),
            }, parser_name=self.name)
            if table_title:
                metadata["table_title"] = table_title
                metadata["title_row"] = title_row
            cells_metadata = _excel_cells_metadata(
                merged_rows, max_cells=self._max_metadata_cells
            )
            if merges:
                metadata["merged_cells"] = merges
                if not cells_metadata["cells_truncated"]:
                    # Spans ship with the cells they describe.  An oversized table
                    # publishes a preview instead of the whole grid, so it keeps the
                    # merge records without duplicating every cell as structured data.
                    metadata["table_cells"] = [cell.as_payload() for cell in grid.cells]
            if outside_merges:
                # A merge above the table (its title row) is not a cell of this
                # table; it stays reported as a source-range fact.
                metadata["source_merged_ranges"] = outside_merges
            metadata.update(cells_metadata)
            blocks.append(
                Block(
                    block_id=f"{request.doc_id}-sheet-{sheet_index}-table-{table_index}",
                    doc_id=request.doc_id,
                    type=BlockType.TABLE,
                    content=content,
                    metadata=metadata,
                )
            )
            appended += 1
        return appended
