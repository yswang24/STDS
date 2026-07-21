"""固定模板 Excel 批量输入/输出：拆解 operation 后逐行输出工时结果。"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import unicodedata
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

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

INPUT_SHEET_NAME = "数据表"
NUMBER_HEADER = "序号"
STATION_HEADER = "工位号"
OUTPUT_OPERATION_HEADER = "操作内容"
INPUT_HEADERS = (NUMBER_HEADER, STATION_HEADER, OUTPUT_OPERATION_HEADER)
DECISION_HEADER = "决策描述"
CHARTCODE_HEADER = "动作代码"
CV_HEADER = "增值/非增值(C/V)"
FREQ_HEADER = "频率"
TRACE_HEADER = "逐步的决策选择（trace）"
TIME_HEADER = "时间"
OUTPUT_HEADERS = (
    NUMBER_HEADER,
    STATION_HEADER,
    OUTPUT_OPERATION_HEADER,
    DECISION_HEADER,
    CHARTCODE_HEADER,
    CV_HEADER,
    FREQ_HEADER,
    TIME_HEADER,
    TRACE_HEADER,
)
EXCEL_CELL_TEXT_LIMIT = 32767
TRACE_FIELD_LIMIT = 8000
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
        decision, chartcode, cv, freq, time_value = self.analysis_values()
        return {
            **self.as_decomposition_preview(),
            CHARTCODE_HEADER: chartcode,
            DECISION_HEADER: decision,
            CV_HEADER: cv,
            FREQ_HEADER: freq,
            TRACE_HEADER: _detail_trace(self),
            TIME_HEADER: time_value,
            "状态": self.status,
        }

    def analysis_values(self) -> tuple[object, object, object, object, object]:
        """返回五个分析字段；无人工工时分析结果时统一输出 NA。"""
        result = self.result
        if result is None or result.source in {Source.MACHINE, Source.UNRESOLVED}:
            return ("NA", "NA", "NA", "NA", "NA")
        time_value = self.time_value()
        return (
            result.decision or "NA",
            result.chartcode or "NA",
            result.cv or "NA",
            result.freq if result.freq is not None else "NA",
            time_value if time_value is not None else "NA",
        )

    def output_values(self) -> list:
        """返回固定九列输出；拆解子动作沿用原始序号和工位号。"""
        decision, chartcode, cv, freq, time_value = self.analysis_values()
        return [
            self.input_row.number,
            self.input_row.station_op,
            self.operation,
            decision,
            chartcode,
            cv,
            freq,
            time_value,
            _detail_trace(self),
        ]

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


ProgressCallback = Callable[[ExcelProgress], object]


def _clean_header(value: object) -> str:
    """清理表头中的 BOM、零宽字符及各种空白，不做字段名称映射。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    invisible = {"\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"}
    return "".join(char for char in text if not char.isspace() and char not in invisible)


def _load_inputs(excel_bytes: bytes):
    """清洗并校验 数据表!A:C，忽略后续列且不做字段名称映射。"""
    if not excel_bytes:
        raise ExcelInputError("上传的 Excel 文件为空")
    try:
        workbook = load_workbook(BytesIO(excel_bytes), data_only=False)
        values_workbook = load_workbook(BytesIO(excel_bytes), data_only=True)
    except Exception as exc:
        raise ExcelInputError("无法读取该文件，请确认它是有效的 .xlsx 工作簿") from exc

    if INPUT_SHEET_NAME not in workbook.sheetnames:
        raise ExcelInputError(f"固定模板缺少“{INPUT_SHEET_NAME}”工作表")

    ws = workbook[INPUT_SHEET_NAME]
    values_ws = values_workbook[INPUT_SHEET_NAME]
    actual_headers = tuple(_clean_header(ws.cell(1, col).value) for col in range(1, 4))
    if actual_headers != INPUT_HEADERS:
        raise ExcelInputError(
            "数据表第 1 行必须依次为：" + "、".join(INPUT_HEADERS)
        )

    input_rows: list[ExcelInputRow] = []
    has_formula_operations = False

    for row_index in range(2, ws.max_row + 1):
        operation_cell = ws.cell(row_index, 3)
        if operation_cell.data_type == "f":
            has_formula_operations = True
            raw_operation = values_ws.cell(row_index, 3).value
            if raw_operation is None:
                raise ExcelInputError(
                    f"数据表第 {row_index} 行的 operation 是公式，"
                    "但文件中没有已计算值；请先在 Excel 中重新计算并保存"
                )
        else:
            raw_operation = operation_cell.value
        if raw_operation is None or not str(raw_operation).strip():
            continue

        number_value = ws.cell(row_index, 1).value
        station_value = ws.cell(row_index, 2).value
        if number_value in (None, ""):
            raise ExcelInputError(f"数据表第 {row_index} 行的序号不能为空")
        if station_value in (None, ""):
            raise ExcelInputError(f"数据表第 {row_index} 行的工位号不能为空")

        operation = str(raw_operation).strip()
        input_rows.append(
            ExcelInputRow(
                sheet_name=INPUT_SHEET_NAME,
                row_index=row_index,
                operation=operation,
                number=number_value,
                line_name="Excel导入",
                station_op=str(station_value).strip(),
                norm_key=normalize(operation) or operation,
            )
        )

    if not input_rows:
        raise ExcelInputError("操作内容字段下没有可解析的输入")
    if has_formula_operations:
        workbook.calculation.calcMode = "auto"
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
    return workbook, input_rows


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


def _write_results(workbook, rows: list[ExcelRowResult]) -> tuple[bytes, str]:
    """将 数据表 重写为固定九列的逐条拆解结果。"""
    ws = workbook[INPUT_SHEET_NAME]
    for table_name in list(ws.tables):
        del ws.tables[table_name]
    if ws.max_row:
        ws.delete_rows(1, ws.max_row)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for col, header in enumerate(OUTPUT_HEADERS, start=1):
        cell = ws.cell(1, col, header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    output_row = 2
    for row in rows:
        for detail in row.details:
            for col, value in enumerate(detail.output_values(), start=1):
                cell = ws.cell(output_row, col, value)
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=col in {3, 4, 9},
                )
            ws.cell(output_row, OUTPUT_HEADERS.index(FREQ_HEADER) + 1).number_format = "0.##"
            ws.cell(output_row, OUTPUT_HEADERS.index(TIME_HEADER) + 1).number_format = "0.00"
            trace_length = len(str(ws.cell(output_row, 9).value or ""))
            operation_length = len(str(ws.cell(output_row, 3).value or ""))
            display_lines = max(
                1,
                (operation_length + 34) // 35,
                (trace_length + 74) // 75,
            )
            ws.row_dimensions[output_row].height = min(180, display_lines * 15)
            output_row += 1

    widths = [10, 14, 44, 30, 14, 20, 10, 12, 80]
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:I{max(1, output_row - 1)}"
    ws.row_dimensions[1].height = 30

    output = BytesIO()
    workbook.save(output)
    return output.getvalue(), INPUT_SHEET_NAME


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
    workbook, input_rows = _load_inputs(excel_bytes)
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
    output_bytes, detail_sheet_name = _write_results(workbook, rows)
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
