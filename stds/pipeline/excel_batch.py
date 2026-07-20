"""Excel 批量输入/输出：读取 operation，逐行解析并回写审计结果。"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from copy import copy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable, Optional

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.table import TableColumn
from openpyxl.worksheet.worksheet import Worksheet

from stds.cascade.resolver import Deps, resolve
from stds.cascade.rules import normalize
from stds.config.settings import settings
from stds.domain.models import Source, StdsElement, StdsResult

logger = logging.getLogger("stds.excel_batch")

OPERATION_HEADER = "operation"
DECISION_HEADER = "决策串"
TRACE_HEADER = "逐步的决策选择（trace）"
TIME_HEADER = "时间"
RESULT_HEADERS = (DECISION_HEADER, TRACE_HEADER, TIME_HEADER)
EXCEL_CELL_TEXT_LIMIT = 32767
TRACE_FIELD_LIMIT = 8000


class ExcelInputError(ValueError):
    """上传的工作簿缺少可解析结构。"""


@dataclass(frozen=True)
class ExcelInputRow:
    sheet_name: str
    row_index: int
    operation: str
    number: int
    line_name: str
    station_op: str
    norm_key: str


@dataclass
class ExcelRowResult:
    input_row: ExcelInputRow
    result: Optional[StdsResult] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.error is not None or self.result is None:
            return "失败"
        if self.result.needs_review or self.result.source == Source.UNRESOLVED:
            return "待复核"
        return "成功"

    def as_preview(self) -> dict:
        return {
            "工作表": self.input_row.sheet_name,
            "Excel行": self.input_row.row_index,
            "operation": self.input_row.operation,
            DECISION_HEADER: self.result.decision if self.result else "",
            TRACE_HEADER: _result_trace(self.result) if self.result else _error_trace(self.error),
            TIME_HEADER: _result_time(self.result) if self.result else None,
            "状态": self.status,
        }


@dataclass
class ExcelBatchOutput:
    output_bytes: bytes
    output_filename: str
    rows: list[ExcelRowResult]
    timings: list["ExcelProgress"]
    total_elapsed_s: float

    @property
    def processed_count(self) -> int:
        return sum(row.result is not None for row in self.rows)

    @property
    def failed_count(self) -> int:
        return sum(row.status == "失败" for row in self.rows)

    @property
    def review_count(self) -> int:
        return sum(row.status == "待复核" for row in self.rows)

    def preview_rows(self) -> list[dict]:
        return [row.as_preview() for row in self.rows]

    @property
    def total_count(self) -> int:
        return len(self.rows)

    @property
    def average_elapsed_s(self) -> float:
        return self.total_elapsed_s / self.total_count if self.total_count else 0.0

    def timing_rows(self) -> list[dict]:
        return [timing.as_preview() for timing in self.timings]


@dataclass(frozen=True)
class ExcelProgress:
    completed_rows: int
    total_rows: int
    operation: str
    affected_rows: int
    item_elapsed_s: float
    total_elapsed_s: float

    def as_preview(self) -> dict:
        return {
            "完成进度": f"{self.completed_rows}/{self.total_rows}",
            "operation": self.operation,
            "本条耗时（秒）": round(self.item_elapsed_s, 2),
            "覆盖 Excel 行数": self.affected_rows,
            "累计耗时（秒）": round(self.total_elapsed_s, 2),
        }


@dataclass(frozen=True)
class _SheetLayout:
    sheet_name: str
    header_row: int
    operation_col: int
    result_cols: dict[str, int]
    totals_row: Optional[int] = None


Resolver = Callable[[StdsElement, Deps], Awaitable[StdsResult]]
ProgressCallback = Callable[[ExcelProgress], object]


def _header_key(value: object) -> str:
    if value is None:
        return ""
    return "".join(str(value).strip().casefold().split())


def _find_header(ws: Worksheet) -> Optional[tuple[int, int]]:
    """在前 100 行寻找大小写/空白不敏感的 operation 表头。"""
    max_scan_row = min(ws.max_row, 100)
    for row in ws.iter_rows(min_row=1, max_row=max_scan_row):
        for cell in row:
            if _header_key(cell.value) == OPERATION_HEADER:
                return cell.row, cell.column
    return None


def _header_map(ws: Worksheet, header_row: int) -> dict[str, int]:
    return {
        _header_key(cell.value): cell.column
        for cell in ws[header_row]
        if _header_key(cell.value)
    }


def _coerce_number(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _copy_style(source, target) -> None:
    if source.has_style:
        target.font = copy(source.font)
        target.fill = copy(source.fill)
        target.border = copy(source.border)
        target.alignment = copy(source.alignment)
        target.number_format = source.number_format
        target.protection = copy(source.protection)


def _find_operation_table(ws: Worksheet, header_row: int, operation_col: int):
    for table in ws.tables.values():
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
        if min_row == header_row and min_col <= operation_col <= max_col:
            return table, (min_col, min_row, max_col, max_row)
    return None, None


def _columns_are_blank(
    ws: Worksheet,
    start_col: int,
    end_col: int,
    start_row: int,
    end_row: int,
) -> bool:
    return all(
        ws.cell(row, col).value in (None, "")
        for row in range(start_row, end_row + 1)
        for col in range(start_col, end_col + 1)
    )


def _extend_table_to_results(ws: Worksheet, table, bounds, result_cols: dict[str, int]) -> None:
    min_col, min_row, max_col, max_row = bounds
    ordered_cols = [result_cols[header] for header in RESULT_HEADERS]
    if ordered_cols[-1] <= max_col:
        return
    if ordered_cols != list(range(max_col + 1, max_col + 1 + len(RESULT_HEADERS))):
        raise ExcelInputError(
            f"工作表“{ws.title}”的 operation 位于 Excel Table 中，"
            "但结果三列无法连续追加到该 Table 右侧"
        )

    new_max_col = ordered_cols[-1]
    expected_width = new_max_col - min_col + 1
    next_column_id = max((column.id for column in table.tableColumns), default=0) + 1
    while len(table.tableColumns) < expected_width:
        position = len(table.tableColumns) + 1
        header_value = ws.cell(min_row, min_col + position - 1).value
        table.tableColumns.append(
            TableColumn(id=next_column_id, name=str(header_value or "Column"))
        )
        next_column_id += 1

    table.ref = (
        f"{get_column_letter(min_col)}{min_row}:"
        f"{get_column_letter(new_max_col)}{max_row}"
    )
    if table.autoFilter is not None:
        table.autoFilter.ref = table.ref


def _prepare_sheet(ws: Worksheet, header_row: int, operation_col: int) -> _SheetLayout:
    headers = _header_map(ws, header_row)
    header_values = [_header_key(cell.value) for cell in ws[header_row]]
    result_keys = tuple(_header_key(header) for header in RESULT_HEADERS)
    existing_start = next(
        (
            index + 1
            for index in range(max(0, len(header_values) - len(result_keys) + 1))
            if tuple(header_values[index:index + len(result_keys)]) == result_keys
        ),
        None,
    )
    source_header = ws.cell(header_row, operation_col)
    operation_table, table_bounds = _find_operation_table(ws, header_row, operation_col)
    totals_row = None
    if operation_table is not None and (
        operation_table.totalsRowCount or operation_table.totalsRowShown
    ):
        totals_row = table_bounds[3]
    if existing_start is not None:
        result_cols = {
            header: existing_start + offset
            for offset, header in enumerate(RESULT_HEADERS)
        }
        # 幂等重跑：只清理由本工具识别出的完整三列，避免空 operation 残留旧结果。
        for row_index in range(header_row + 1, ws.max_row + 1):
            if row_index == totals_row:
                continue
            for col in result_cols.values():
                ws.cell(row_index, col).value = None
    else:
        if table_bounds is not None:
            _, _, table_max_col, table_max_row = table_bounds
            next_col = table_max_col + 1
            if not _columns_are_blank(
                ws,
                next_col,
                next_col + len(RESULT_HEADERS) - 1,
                header_row,
                max(ws.max_row, table_max_row),
            ):
                raise ExcelInputError(
                    f"工作表“{ws.title}”的 operation 位于 Excel Table 中；"
                    "请先确保该 Table 右侧三列为空，再上传解析"
                )
        else:
            next_col = max(ws.max_column, max(headers.values(), default=0)) + 1
        result_cols = {}
        for header in RESULT_HEADERS:
            col = next_col
            next_col += 1
            target = ws.cell(header_row, col, header)
            _copy_style(source_header, target)
            result_cols[header] = col

    if operation_table is not None:
        _extend_table_to_results(ws, operation_table, table_bounds, result_cols)

    widths = {DECISION_HEADER: 30, TRACE_HEADER: 80, TIME_HEADER: 12}
    operation_letter = get_column_letter(operation_col)
    operation_width = ws.column_dimensions[operation_letter].width or 0
    ws.column_dimensions[operation_letter].width = max(operation_width, 45)
    for header, col in result_cols.items():
        letter = get_column_letter(col)
        current = ws.column_dimensions[letter].width or 0
        ws.column_dimensions[letter].width = max(current, widths[header])

    return _SheetLayout(ws.title, header_row, operation_col, result_cols, totals_row)


def _load_inputs(excel_bytes: bytes):
    if not excel_bytes:
        raise ExcelInputError("上传的 Excel 文件为空")
    try:
        workbook = load_workbook(BytesIO(excel_bytes), data_only=False)
        values_workbook = load_workbook(BytesIO(excel_bytes), data_only=True)
    except Exception as exc:
        raise ExcelInputError("无法读取该文件，请确认它是有效的 .xlsx 工作簿") from exc

    layouts: dict[str, _SheetLayout] = {}
    input_rows: list[ExcelInputRow] = []
    sequence = 0
    has_formula_operations = False

    for ws in workbook.worksheets:
        found = _find_header(ws)
        if found is None:
            continue
        header_row, operation_col = found
        layout = _prepare_sheet(ws, header_row, operation_col)
        layouts[ws.title] = layout
        values_ws = values_workbook[ws.title]
        headers = _header_map(ws, header_row)
        number_col = headers.get("number")
        station_col = headers.get("station_op")
        line_col = headers.get("line_name")

        for row_index in range(header_row + 1, ws.max_row + 1):
            if row_index == layout.totals_row:
                continue
            operation_cell = ws.cell(row_index, operation_col)
            if operation_cell.data_type == "f":
                has_formula_operations = True
                raw_operation = values_ws.cell(row_index, operation_col).value
                if raw_operation is None:
                    raise ExcelInputError(
                        f"工作表“{ws.title}”第 {row_index} 行的 operation 是公式，"
                        "但文件中没有已计算值；请先在 Excel 中重新计算并保存"
                    )
            else:
                raw_operation = operation_cell.value
            if raw_operation is None or not str(raw_operation).strip():
                continue
            sequence += 1
            operation = str(raw_operation).strip()
            norm_key = normalize(operation) or operation
            number_value = ws.cell(row_index, number_col).value if number_col else None
            station_value = ws.cell(row_index, station_col).value if station_col else None
            line_value = ws.cell(row_index, line_col).value if line_col else None
            input_rows.append(
                ExcelInputRow(
                    sheet_name=ws.title,
                    row_index=row_index,
                    operation=operation,
                    number=_coerce_number(number_value, sequence),
                    line_name=str(line_value).strip() if line_value not in (None, "") else "Excel导入",
                    station_op=str(station_value).strip() if station_value not in (None, "") else ws.title,
                    norm_key=norm_key,
                )
            )

    if not layouts:
        raise ExcelInputError("未找到 operation 字段；请在任一工作表前 100 行提供该表头")
    if not input_rows:
        raise ExcelInputError("operation 字段下没有可解析的输入")
    if has_formula_operations:
        workbook.calculation.calcMode = "auto"
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
    return workbook, layouts, input_rows


def serialize_trace(trace: list) -> str:
    """将三元组 trace 序列化为稳定、可被下游再次解析的 JSON。"""
    steps = []
    truncated = False
    for item in trace or []:
        if isinstance(item, dict):
            variable = item.get("变量", item.get("variable", item.get("step", "")))
            choice = item.get("选择", item.get("choice", item.get("description", "")))
            reason = item.get("原因", item.get("reason", ""))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            variable, choice, reason = item[:3]
        else:
            variable, choice, reason = "", str(item), ""
        values = [str(variable), str(choice), str(reason)]
        for index, value in enumerate(values):
            if len(value) > TRACE_FIELD_LIMIT:
                values[index] = value[: TRACE_FIELD_LIMIT - 1] + "…"
                truncated = True
        steps.append({"变量": values[0], "选择": values[1], "原因": values[2]})

    marker = {"变量": "TRUNCATED", "选择": "", "原因": "trace 超出 Excel 单元格上限，已截断"}
    serialized = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= EXCEL_CELL_TEXT_LIMIT and not truncated:
        return serialized

    kept = []
    for step in steps:
        candidate = json.dumps(
            [*kept, step, marker], ensure_ascii=False, separators=(",", ":")
        )
        if len(candidate) > EXCEL_CELL_TEXT_LIMIT:
            truncated = True
            break
        kept.append(step)
    if truncated:
        kept.append(marker)
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))


def _error_trace(error: Optional[str]) -> str:
    return serialize_trace([("ERROR", "", error or "未知错误")])


def _result_trace(result: StdsResult) -> str:
    if result.trace:
        return serialize_trace(result.trace)
    if result.needs_review or result.source == Source.UNRESOLVED:
        return serialize_trace(
            [("UNRESOLVED", result.chartcode or "", "未能完成决策解析，需要人工复核")]
        )
    if result.source == Source.MACHINE:
        return serialize_trace(
            [("T2_machine", "设备动作", "判定为设备动作，跳过人工标准时间计算")]
        )
    return serialize_trace([])


def _result_time(result: StdsResult) -> Optional[float]:
    if result.needs_review or result.source == Source.UNRESOLVED:
        return None
    return result.time_s


def _write_results(workbook, layouts: dict[str, _SheetLayout], rows: list[ExcelRowResult]) -> bytes:
    for row in rows:
        ws = workbook[row.input_row.sheet_name]
        layout = layouts[row.input_row.sheet_name]
        operation_cell = ws.cell(row.input_row.row_index, layout.operation_col)
        operation_alignment = copy(operation_cell.alignment)
        operation_alignment.wrap_text = True
        operation_alignment.vertical = "top"
        operation_cell.alignment = operation_alignment
        values = {
            DECISION_HEADER: row.result.decision if row.result else "",
            TRACE_HEADER: _result_trace(row.result) if row.result else _error_trace(row.error),
            TIME_HEADER: _result_time(row.result) if row.result else None,
        }
        for header, value in values.items():
            cell = ws.cell(row.input_row.row_index, layout.result_cols[header])
            if not cell.has_style:
                _copy_style(operation_cell, cell)
            cell.value = value
            if header == TRACE_HEADER:
                alignment = copy(cell.alignment)
                alignment.wrap_text = True
                alignment.vertical = "top"
                cell.alignment = alignment
            elif header == TIME_HEADER:
                cell.number_format = "0.00"

        trace_length = len(str(values[TRACE_HEADER] or ""))
        operation_length = len(row.input_row.operation)
        display_lines = max(
            1,
            (operation_length + 34) // 35,
            (trace_length + 74) // 75,
        )
        current_height = ws.row_dimensions[row.input_row.row_index].height or 15
        ws.row_dimensions[row.input_row.row_index].height = max(
            current_height,
            min(180, display_lines * 15),
        )

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_output_filename(source_name: str) -> str:
    safe_name = Path(source_name or "input.xlsx").name
    stem = Path(safe_name).stem or "input"
    return f"{stem}_解析结果.xlsx"


async def analyze_excel_bytes(
    excel_bytes: bytes,
    source_name: str,
    deps: Deps,
    *,
    resolver: Resolver = resolve,
    concurrency: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> ExcelBatchOutput:
    """解析所有含 operation 表头的工作表，并返回保留原内容的结果工作簿。"""
    batch_started = time.perf_counter()
    workbook, layouts, input_rows = _load_inputs(excel_bytes)
    total = len(input_rows)
    completed = 0
    timings: list[ExcelProgress] = []
    sem = asyncio.Semaphore(max(1, concurrency or settings.CONCURRENCY_LIMIT))

    async def notify_progress(progress: ExcelProgress) -> None:
        if on_progress is None:
            return
        try:
            maybe_awaitable = on_progress(progress)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except Exception:
            logger.debug("Excel progress callback failed", exc_info=True)

    grouped_rows: dict[str, list[ExcelInputRow]] = {}
    for input_row in input_rows:
        grouped_rows.setdefault(input_row.norm_key, []).append(input_row)

    async def analyze_group(group: list[ExcelInputRow]) -> list[ExcelRowResult]:
        nonlocal completed
        representative = group[0]
        item_started: Optional[float] = None
        try:
            async with sem:
                item_started = time.perf_counter()
                element = StdsElement(
                    number=representative.number,
                    operation_des=representative.operation,
                    line_name=representative.line_name,
                    station_op=representative.station_op,
                    freq=1.0,
                    norm_key=representative.norm_key,
                )
                result = await resolver(element, deps)
                return [ExcelRowResult(input_row=input_row, result=result) for input_row in group]
        except Exception as exc:
            logger.exception(
                "Excel operation failed: operation=%r rows=%s",
                representative.operation,
                [(input_row.sheet_name, input_row.row_index) for input_row in group],
            )
            error = f"{type(exc).__name__}: {exc}"
            return [ExcelRowResult(input_row=input_row, error=error) for input_row in group]
        finally:
            item_elapsed_s = (
                time.perf_counter() - item_started if item_started is not None else 0.0
            )
            completed += len(group)
            progress = ExcelProgress(
                completed_rows=completed,
                total_rows=total,
                operation=representative.operation,
                affected_rows=len(group),
                item_elapsed_s=item_elapsed_s,
                total_elapsed_s=time.perf_counter() - batch_started,
            )
            timings.append(progress)
            await notify_progress(progress)

    grouped_results = await asyncio.gather(
        *(analyze_group(group) for group in grouped_rows.values())
    )
    by_location = {
        (row.input_row.sheet_name, row.input_row.row_index): row
        for group_result in grouped_results
        for row in group_result
    }
    rows = [by_location[(row.sheet_name, row.row_index)] for row in input_rows]
    output_bytes = _write_results(workbook, layouts, rows)
    total_elapsed_s = time.perf_counter() - batch_started
    return ExcelBatchOutput(
        output_bytes=output_bytes,
        output_filename=build_output_filename(source_name),
        rows=rows,
        timings=timings,
        total_elapsed_s=total_elapsed_s,
    )
