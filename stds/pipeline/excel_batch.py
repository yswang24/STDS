"""Excel 批量输入/输出：读取 operation，逐行解析并回写审计结果。"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from copy import copy
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet.table import TableColumn
from openpyxl.worksheet.worksheet import Worksheet

from stds.cascade.resolver import Deps, resolve
from stds.cascade.rules import normalize
from stds.config.settings import settings
from stds.domain.models import Source, StdsElement, StdsResult
from stds.llm.decompose import decompose_operation
from stds.pipeline.operation_analysis import (
    Decomposer,
    OperationSplit,
    Resolver,
    resolve_with_actor,
    split_operation,
)

logger = logging.getLogger("stds.excel_batch")

OPERATION_HEADER = "operation"
DECISION_HEADER = "决策串"
TRACE_HEADER = "逐步的决策选择（trace）"
TIME_HEADER = "时间"
RESULT_HEADERS = (DECISION_HEADER, TRACE_HEADER, TIME_HEADER)
EXCEL_CELL_TEXT_LIMIT = 32767
TRACE_FIELD_LIMIT = 8000
DETAIL_SHEET_BASE = "STDS_拆解明细"
PHASE_DECOMPOSE = "拆解"
PHASE_ANALYZE = "工时分析"


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
class ExcelDetailResult:
    input_row: ExcelInputRow
    split: OperationSplit
    child_index: int
    operation: str
    result: Optional[StdsResult] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.error is not None or self.result is None:
            return "失败"
        if (
            self.split.needs_review
            or self.result.needs_review
            or self.result.source == Source.UNRESOLVED
        ):
            return "待复核"
        return "成功"

    @property
    def child_count(self) -> int:
        return len(self.split.operations)

    def as_decomposition_preview(self) -> dict:
        return {
            "工作表": self.input_row.sheet_name,
            "Excel行": self.input_row.row_index,
            "原始operation": self.input_row.operation,
            "主体类型": self.split.actor,
            "拆解序号": f"{self.child_index}/{self.child_count}",
            "拆解后operation": self.operation,
            "拆解来源": self.split.source,
            "状态": "待复核" if self.split.needs_review else "成功",
        }

    def as_preview(self) -> dict:
        return {
            **self.as_decomposition_preview(),
            "Chartcode": self.result.chartcode if self.result else "",
            DECISION_HEADER: self.result.decision if self.result else "",
            TRACE_HEADER: _detail_trace(self),
            TIME_HEADER: self.time_value(),
            "状态": self.status,
        }

    def time_value(self) -> Optional[float]:
        if self.split.needs_review or self.result is None:
            return None
        return _result_time(self.result)


@dataclass
class ExcelRowResult:
    input_row: ExcelInputRow
    split: OperationSplit
    details: list[ExcelDetailResult]

    @property
    def status(self) -> str:
        statuses = {detail.status for detail in self.details}
        if "失败" in statuses:
            return "失败"
        if "待复核" in statuses:
            return "待复核"
        return "成功"

    def decision_value(self) -> str:
        if len(self.details) == 1:
            result = self.details[0].result
            return result.decision if result else ""
        values = [
            {
                "拆解序号": f"{detail.child_index}/{detail.child_count}",
                "operation": detail.operation,
                DECISION_HEADER: detail.result.decision if detail.result else "",
            }
            for detail in self.details
        ]
        return _fit_excel_text(json.dumps(values, ensure_ascii=False, separators=(",", ":")))

    def trace_value(self) -> str:
        trace: list = [
            (
                "拆解",
                json.dumps(self.split.operations, ensure_ascii=False),
                f"主体={self.split.actor}; 来源={self.split.source}",
            )
        ]
        if self.split.error:
            trace.append(("拆解待复核", "回退为原动作", self.split.error))
        for detail in self.details:
            prefix = f"{detail.child_index}/{detail.child_count}"
            trace.extend(_result_trace_items(detail.result, detail.error, prefix=prefix))
        return serialize_trace(trace)

    def time_value(self) -> Optional[float]:
        values = [detail.time_value() for detail in self.details]
        if any(value is None for value in values):
            return None
        return round(sum(value for value in values if value is not None), 2)

    def as_preview(self) -> dict:
        return {
            "工作表": self.input_row.sheet_name,
            "Excel行": self.input_row.row_index,
            "operation": self.input_row.operation,
            "主体类型": self.split.actor,
            "拆解数量": len(self.details),
            DECISION_HEADER: self.decision_value(),
            TRACE_HEADER: self.trace_value(),
            TIME_HEADER: self.time_value(),
            "状态": self.status,
        }


@dataclass
class ExcelBatchOutput:
    output_bytes: bytes
    output_filename: str
    rows: list[ExcelRowResult]
    timings: list["ExcelProgress"]
    total_elapsed_s: float
    decompose_elapsed_s: float
    analysis_elapsed_s: float
    detail_sheet_name: str

    @property
    def processed_count(self) -> int:
        return sum(any(detail.result is not None for detail in row.details) for row in self.rows)

    @property
    def failed_count(self) -> int:
        return sum(row.status == "失败" for row in self.rows)

    @property
    def review_count(self) -> int:
        return sum(row.status == "待复核" for row in self.rows)

    def preview_rows(self) -> list[dict]:
        return [row.as_preview() for row in self.rows]

    def decomposition_rows(self) -> list[dict]:
        return [
            detail.as_decomposition_preview()
            for row in self.rows
            for detail in row.details
        ]

    def detail_preview_rows(self) -> list[dict]:
        return [detail.as_preview() for row in self.rows for detail in row.details]

    @property
    def detail_count(self) -> int:
        return sum(len(row.details) for row in self.rows)

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
    phase: str
    completed_rows: int
    total_rows: int
    operation: str
    affected_rows: int
    item_elapsed_s: float
    total_elapsed_s: float
    generated_operations: tuple[str, ...] = ()
    actor: str = ""
    sheet_name: str = ""
    row_index: Optional[int] = None
    child_index: Optional[int] = None
    child_count: Optional[int] = None

    @property
    def overall_ratio(self) -> float:
        phase_ratio = self.completed_rows / self.total_rows if self.total_rows else 1.0
        if self.phase == PHASE_DECOMPOSE:
            return min(0.5, phase_ratio * 0.5)
        return min(1.0, 0.5 + phase_ratio * 0.5)

    def as_preview(self) -> dict:
        return {
            "阶段": self.phase,
            "完成进度": f"{self.completed_rows}/{self.total_rows}",
            "工作表": self.sheet_name,
            "Excel行": self.row_index,
            "operation": self.operation,
            "拆解序号": (
                f"{self.child_index}/{self.child_count}"
                if self.child_index is not None and self.child_count is not None
                else ""
            ),
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


def _fit_excel_text(value: str) -> str:
    if len(value) <= EXCEL_CELL_TEXT_LIMIT:
        return value
    return value[: EXCEL_CELL_TEXT_LIMIT - 1] + "…"


def _result_trace_items(
    result: Optional[StdsResult],
    error: Optional[str] = None,
    *,
    prefix: str = "",
) -> list:
    label = (lambda value: f"{prefix}:{value}" if prefix else value)
    if result is None:
        return [(label("ERROR"), "", error or "未知错误")]
    if result.trace:
        return [
            (label(str(item[0])), item[1], item[2])
            if isinstance(item, (list, tuple)) and len(item) >= 3
            else (label("trace"), str(item), "")
            for item in result.trace
        ]
    if result.needs_review or result.source == Source.UNRESOLVED:
        return [
            (label("UNRESOLVED"), result.chartcode or "", "未能完成决策解析，需要人工复核")
        ]
    if result.source == Source.MACHINE:
        return [
            (label("T2_machine"), "设备动作", "判定为设备动作，跳过人工标准时间计算")
        ]
    return []


def _detail_trace(detail: ExcelDetailResult) -> str:
    trace = [
        (
            "拆解",
            detail.operation,
            f"{detail.child_index}/{detail.child_count}; 主体={detail.split.actor}; 来源={detail.split.source}",
        )
    ]
    if detail.split.error:
        trace.append(("拆解待复核", "回退为原动作", detail.split.error))
    trace.extend(_result_trace_items(detail.result, detail.error))
    return serialize_trace(trace)


def _result_time(result: StdsResult) -> Optional[float]:
    if result.needs_review or result.source == Source.UNRESOLVED:
        return None
    return result.time_s


def _unique_sheet_title(workbook, base: str) -> str:
    if base not in workbook.sheetnames:
        return base
    index = 2
    while f"{base}_{index}" in workbook.sheetnames:
        index += 1
    return f"{base}_{index}"


def _write_detail_sheet(workbook, rows: list[ExcelRowResult]) -> str:
    title = _unique_sheet_title(workbook, DETAIL_SHEET_BASE)
    ws = workbook.create_sheet(title)
    headers = [
        "来源工作表",
        "来源Excel行",
        "number",
        "station_op",
        "原始operation",
        "主体类型",
        "拆解序号",
        "拆解总数",
        "拆解后operation",
        "拆解来源",
        "Chartcode",
        DECISION_HEADER,
        TRACE_HEADER,
        TIME_HEADER,
        "状态",
    ]
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(1, col, header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    output_row = 2
    for row in rows:
        for detail in row.details:
            values = [
                detail.input_row.sheet_name,
                detail.input_row.row_index,
                detail.input_row.number,
                detail.input_row.station_op,
                detail.input_row.operation,
                detail.split.actor,
                detail.child_index,
                detail.child_count,
                detail.operation,
                detail.split.source,
                detail.result.chartcode if detail.result else "",
                detail.result.decision if detail.result else "",
                _detail_trace(detail),
                detail.time_value(),
                detail.status,
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(output_row, col, value)
                cell.alignment = Alignment(vertical="top", wrap_text=col in {5, 9, 12, 13})
            ws.cell(output_row, headers.index(TIME_HEADER) + 1).number_format = "0.00"
            output_row += 1

    widths = [14, 12, 10, 16, 44, 12, 10, 10, 44, 14, 14, 30, 80, 12, 12]
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, output_row - 1)}"
    ws.row_dimensions[1].height = 24
    return title


def _write_results(
    workbook,
    layouts: dict[str, _SheetLayout],
    rows: list[ExcelRowResult],
) -> tuple[bytes, str]:
    for row in rows:
        ws = workbook[row.input_row.sheet_name]
        layout = layouts[row.input_row.sheet_name]
        operation_cell = ws.cell(row.input_row.row_index, layout.operation_col)
        operation_alignment = copy(operation_cell.alignment)
        operation_alignment.wrap_text = True
        operation_alignment.vertical = "top"
        operation_cell.alignment = operation_alignment
        values = {
            DECISION_HEADER: row.decision_value(),
            TRACE_HEADER: row.trace_value(),
            TIME_HEADER: row.time_value(),
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

    detail_sheet_name = _write_detail_sheet(workbook, rows)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue(), detail_sheet_name


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
    decomposer: Decomposer = decompose_operation,
    concurrency: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> ExcelBatchOutput:
    """先拆解 operation，再逐个子动作计算工时并回写完整审计结果。"""
    batch_started = time.perf_counter()
    workbook, layouts, input_rows = _load_inputs(excel_bytes)
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

    # ---------- 阶段 1：主体判定 + Dify Prompt 拆解 ----------
    decompose_started = time.perf_counter()
    decompose_completed = 0

    async def split_group(
        norm_key: str,
        group: list[ExcelInputRow],
    ) -> tuple[str, OperationSplit]:
        nonlocal decompose_completed
        representative = group[0]
        item_started: Optional[float] = None
        split = OperationSplit(
            actor="人工",
            operations=(representative.operation,),
            source="拆解失败回退",
            needs_review=True,
        )
        try:
            async with sem:
                item_started = time.perf_counter()
                split = await split_operation(
                    representative.operation,
                    deps,
                    decomposer=decomposer,
                )
                return norm_key, split
        except Exception as exc:
            logger.exception(
                "Excel operation decomposition failed: operation=%r rows=%s",
                representative.operation,
                [(input_row.sheet_name, input_row.row_index) for input_row in group],
            )
            error = f"{type(exc).__name__}: {exc}"
            split = replace(split, error=error)
            return norm_key, split
        finally:
            item_elapsed_s = (
                time.perf_counter() - item_started if item_started is not None else 0.0
            )
            per_row_elapsed_s = item_elapsed_s / len(group)
            for input_row in group:
                decompose_completed += 1
                progress = ExcelProgress(
                    phase=PHASE_DECOMPOSE,
                    completed_rows=decompose_completed,
                    total_rows=len(input_rows),
                    operation=input_row.operation,
                    affected_rows=1,
                    item_elapsed_s=per_row_elapsed_s,
                    total_elapsed_s=time.perf_counter() - batch_started,
                    generated_operations=split.operations,
                    actor=split.actor,
                    sheet_name=input_row.sheet_name,
                    row_index=input_row.row_index,
                )
                timings.append(progress)
                await notify_progress(progress)

    split_pairs = await asyncio.gather(
        *(
            split_group(norm_key, group)
            for norm_key, group in grouped_rows.items()
        )
    )
    split_by_key = dict(split_pairs)
    decompose_elapsed_s = time.perf_counter() - decompose_started

    rows: list[ExcelRowResult] = []
    for input_row in input_rows:
        split = split_by_key[input_row.norm_key]
        details = [
            ExcelDetailResult(
                input_row=input_row,
                split=split,
                child_index=index,
                operation=operation,
            )
            for index, operation in enumerate(split.operations, start=1)
        ]
        rows.append(ExcelRowResult(input_row=input_row, split=split, details=details))

    # ---------- 阶段 2：对拆解后的每个动作计算工时 ----------
    detail_groups: dict[tuple[str, str], list[ExcelDetailResult]] = {}
    for row in rows:
        for detail in row.details:
            norm_key = normalize(detail.operation) or detail.operation
            detail_groups.setdefault((detail.split.actor, norm_key), []).append(detail)

    analysis_started = time.perf_counter()
    analysis_completed = 0
    total_details = sum(len(group) for group in detail_groups.values())

    async def analyze_group(group: list[ExcelDetailResult]) -> None:
        nonlocal analysis_completed
        representative = group[0]
        item_started: Optional[float] = None
        try:
            async with sem:
                item_started = time.perf_counter()
                element = StdsElement(
                    number=representative.input_row.number,
                    operation_des=representative.operation,
                    line_name=representative.input_row.line_name,
                    station_op=representative.input_row.station_op,
                    freq=1.0,
                    norm_key=normalize(representative.operation) or representative.operation,
                )
                result = await resolve_with_actor(
                    resolver,
                    element,
                    deps,
                    representative.split.actor,
                )

                for detail in group:
                    detail_element = StdsElement(
                        number=detail.input_row.number,
                        operation_des=detail.operation,
                        line_name=detail.input_row.line_name,
                        station_op=detail.input_row.station_op,
                        freq=1.0,
                        norm_key=normalize(detail.operation) or detail.operation,
                    )
                    detail.result = replace(result, element=detail_element)
        except Exception as exc:
            logger.exception(
                "Excel decomposed operation failed: operation=%r rows=%s",
                representative.operation,
                [
                    (detail.input_row.sheet_name, detail.input_row.row_index)
                    for detail in group
                ],
            )
            error = f"{type(exc).__name__}: {exc}"
            for detail in group:
                detail.error = error
        finally:
            item_elapsed_s = (
                time.perf_counter() - item_started if item_started is not None else 0.0
            )
            per_row_elapsed_s = item_elapsed_s / len(group)
            for detail in group:
                analysis_completed += 1
                progress = ExcelProgress(
                    phase=PHASE_ANALYZE,
                    completed_rows=analysis_completed,
                    total_rows=total_details,
                    operation=detail.operation,
                    affected_rows=1,
                    item_elapsed_s=per_row_elapsed_s,
                    total_elapsed_s=time.perf_counter() - batch_started,
                    actor=detail.split.actor,
                    sheet_name=detail.input_row.sheet_name,
                    row_index=detail.input_row.row_index,
                    child_index=detail.child_index,
                    child_count=detail.child_count,
                )
                timings.append(progress)
                await notify_progress(progress)

    await asyncio.gather(
        *(analyze_group(group) for group in detail_groups.values())
    )
    analysis_elapsed_s = time.perf_counter() - analysis_started
    output_bytes, detail_sheet_name = _write_results(workbook, layouts, rows)
    total_elapsed_s = time.perf_counter() - batch_started
    return ExcelBatchOutput(
        output_bytes=output_bytes,
        output_filename=build_output_filename(source_name),
        rows=rows,
        timings=timings,
        total_elapsed_s=total_elapsed_s,
        decompose_elapsed_s=decompose_elapsed_s,
        analysis_elapsed_s=analysis_elapsed_s,
        detail_sheet_name=detail_sheet_name,
    )
