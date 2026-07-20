"""Excel 上传、逐行解析和原工作簿回写。"""
from __future__ import annotations

import asyncio
import json
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table

from stds.domain.models import Source, StdsResult
from stds.pipeline.excel_batch import (
    DECISION_HEADER,
    RESULT_HEADERS,
    TIME_HEADER,
    TRACE_HEADER,
    ExcelInputError,
    analyze_excel_bytes,
    serialize_trace,
)


def _workbook_bytes(*, include_results: bool = False) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "数据表"
    ws["A1"] = "原工作簿说明"
    headers = ["number", "station_op", " Operation "]
    if include_results:
        headers.extend([DECISION_HEADER, TRACE_HEADER, TIME_HEADER])
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(2, col, header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")

    rows = [
        [10, "OP010", "重复操作"],
        [None, None, None],
        [20, "OP020", "重复操作"],
        [30, "OP030", "失败操作"],
    ]
    for row_index, values in enumerate(rows, start=3):
        for col, value in enumerate(values, start=1):
            ws.cell(row_index, col, value)
        ws.cell(row_index, 3).alignment = Alignment(vertical="center")

    notes = wb.create_sheet("说明")
    notes["A1"] = "这个工作表没有 operation，必须原样保留"

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _result(element) -> StdsResult:
    return StdsResult(
        element=element,
        chartcode="060 010",
        decision="LS,",
        time_s=1.2,
        cv="V",
        freq=1.0,
        source=Source.FORMULA,
        confidence=0.9,
        needs_review=False,
        trace=[("V1", "Laser Scan", "single-candidate")],
    )


def test_serialize_trace_is_structured_json():
    value = serialize_trace([("V1", "Loaded Arm", "operation-match")])
    assert json.loads(value) == [
        {"变量": "V1", "选择": "Loaded Arm", "原因": "operation-match"}
    ]


def test_serialize_trace_truncation_stays_valid_json():
    value = serialize_trace([("V1", "choice", "x" * 40000)])
    parsed = json.loads(value)
    assert len(value) <= 32767
    assert parsed[-1]["变量"] == "TRUNCATED"


def test_analyze_excel_preserves_rows_and_appends_three_columns():
    cached_result = None
    resolver_calls = 0
    progress = []

    async def fake_resolver(element, deps):
        nonlocal cached_result, resolver_calls
        resolver_calls += 1
        if element.operation_des == "失败操作":
            raise RuntimeError("mock failure")
        if cached_result is None:
            cached_result = _result(element)
        return cached_result  # 模拟 AutoCache 对重复 operation 返回首条 result 对象

    batch = asyncio.run(
        analyze_excel_bytes(
            _workbook_bytes(),
            "示例.xlsx",
            object(),
            resolver=fake_resolver,
            concurrency=2,
            on_progress=progress.append,
        )
    )

    assert batch.output_filename == "示例_解析结果.xlsx"
    assert batch.processed_count == 2
    assert batch.failed_count == 1
    assert batch.review_count == 0
    assert resolver_calls == 2  # 重复 operation 只解析一次，另一次是失败 operation
    assert progress[-1].completed_rows == 3
    assert progress[-1].total_rows == 3
    assert sorted(item.affected_rows for item in progress) == [1, 2]
    assert all(item.item_elapsed_s >= 0 for item in progress)
    assert all(item.total_elapsed_s >= item.item_elapsed_s for item in progress)
    assert batch.total_count == 3
    assert batch.total_elapsed_s >= progress[-1].total_elapsed_s
    assert batch.average_elapsed_s == pytest.approx(batch.total_elapsed_s / 3)
    assert len(batch.timing_rows()) == 2

    wb = load_workbook(BytesIO(batch.output_bytes), data_only=False)
    ws = wb["数据表"]
    assert ws["A1"].value == "原工作簿说明"
    assert wb["说明"]["A1"].value == "这个工作表没有 operation，必须原样保留"
    assert [ws.cell(2, col).value for col in range(4, 7)] == [
        DECISION_HEADER,
        TRACE_HEADER,
        TIME_HEADER,
    ]
    assert ws["D3"].value == "LS,"
    assert ws["D5"].value == "LS,"  # 重复输入仍按原 Excel 行回写
    assert json.loads(ws["E3"].value)[0]["变量"] == "V1"
    assert ws["F3"].value == 1.2
    assert ws["D4"].value is None  # 空 operation 行不处理
    assert json.loads(ws["E6"].value)[0]["变量"] == "ERROR"
    assert "mock failure" in ws["E6"].value
    assert ws["F6"].value is None
    assert ws["D2"].font.bold is True
    assert ws["D2"].fill.fgColor.rgb == ws["C2"].fill.fgColor.rgb
    assert ws["E3"].alignment.wrap_text is True
    assert ws["F3"].number_format == "0.00"


def test_existing_result_headers_are_reused_not_duplicated():
    async def fake_resolver(element, deps):
        return _result(element)

    batch = asyncio.run(
        analyze_excel_bytes(
            _workbook_bytes(include_results=True),
            "已有结果.xlsx",
            object(),
            resolver=fake_resolver,
        )
    )
    ws = load_workbook(BytesIO(batch.output_bytes))["数据表"]
    headers = [ws.cell(2, col).value for col in range(1, ws.max_column + 1)]
    assert headers.count(DECISION_HEADER) == 1
    assert headers.count(TRACE_HEADER) == 1
    assert headers.count(TIME_HEADER) == 1
    assert ws.max_column == 6


def test_unresolved_is_marked_for_review_and_has_explanatory_trace():
    async def unresolved_resolver(element, deps):
        return StdsResult.unresolved(element, None)

    wb = Workbook()
    ws = wb.active
    ws.append(["operation"])
    ws.append(["无法识别的动作"])
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        analyze_excel_bytes(
            payload.getvalue(),
            "待复核.xlsx",
            object(),
            resolver=unresolved_resolver,
        )
    )
    preview = batch.preview_rows()[0]
    assert batch.review_count == 1
    assert batch.failed_count == 0
    assert preview["状态"] == "待复核"
    assert json.loads(preview[TRACE_HEADER])[0]["变量"] == "UNRESOLVED"
    assert preview[TIME_HEADER] is None
    result_ws = load_workbook(BytesIO(batch.output_bytes))["Sheet"]
    assert result_ws["D2"].value is None


def test_unrelated_time_column_is_preserved_and_result_group_is_appended():
    async def fake_resolver(element, deps):
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.append(["operation", TIME_HEADER])
    ws.append(["人工操作", 99])
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        analyze_excel_bytes(payload.getvalue(), "原时间.xlsx", object(), resolver=fake_resolver)
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))["Sheet"]
    assert result_ws["B2"].value == 99
    assert [result_ws.cell(1, col).value for col in range(3, 6)] == list(RESULT_HEADERS)
    assert result_ws["E2"].value == 1.2


def test_operation_formula_without_cached_value_is_rejected():
    wb = Workbook()
    ws = wb.active
    ws.append(["operation", "part"])
    ws.append(["=B2", "拿取泡棉"])
    payload = BytesIO()
    wb.save(payload)

    with pytest.raises(ExcelInputError, match="没有已计算值"):
        asyncio.run(analyze_excel_bytes(payload.getvalue(), "公式.xlsx", object()))


def test_excel_table_is_extended_to_include_result_columns():
    async def fake_resolver(element, deps):
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.append(["number", "station_op", "operation"])
    ws.append([1, "OP010", "人工操作一"])
    ws.append([2, "OP010", "人工操作二"])
    ws.add_table(Table(displayName="OperationsTable", ref="A1:C3"))
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        analyze_excel_bytes(payload.getvalue(), "表格.xlsx", object(), resolver=fake_resolver)
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))["Sheet"]
    table = result_ws.tables["OperationsTable"]
    assert table.ref == "A1:F3"
    assert [column.name for column in table.tableColumns] == [
        "number",
        "station_op",
        "operation",
        *RESULT_HEADERS,
    ]


def test_excel_table_totals_row_is_not_analyzed_or_overwritten():
    seen = []

    async def fake_resolver(element, deps):
        seen.append(element.operation_des)
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.append(["operation", "数量"])
    ws.append(["人工动作", 1])
    ws.append(["总计", 1])
    table = Table(
        displayName="OperationsWithTotal",
        ref="A1:B3",
        totalsRowCount=1,
        totalsRowShown=True,
    )
    ws.add_table(table)
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        analyze_excel_bytes(payload.getvalue(), "合计行.xlsx", object(), resolver=fake_resolver)
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))["Sheet"]
    assert seen == ["人工动作"]
    assert batch.processed_count == 1
    assert result_ws["C3"].value is None
    assert result_ws.tables["OperationsWithTotal"].ref == "A1:E3"


@pytest.mark.parametrize("payload", [b"", b"not-an-xlsx"])
def test_invalid_excel_has_user_friendly_error(payload):
    with pytest.raises(ExcelInputError):
        asyncio.run(analyze_excel_bytes(payload, "bad.xlsx", object()))
