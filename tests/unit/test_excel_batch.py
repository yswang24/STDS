"""三列中文固定模板 Excel 上传、拆解和九列明细输出。"""
from __future__ import annotations

import asyncio
import json
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook

from stds.domain.models import Source, StdsResult
from stds.pipeline.excel_batch import (
    DECISION_HEADER,
    INPUT_HEADERS,
    INPUT_SHEET_NAME,
    OUTPUT_HEADERS,
    TIME_HEADER,
    TRACE_HEADER,
    ExcelInputError,
    analyze_excel_bytes,
    serialize_trace,
)


async def _passthrough_decomposer(operation: str) -> list[str]:
    return [operation]


async def _analyze_excel_bytes(*args, **kwargs):
    kwargs.setdefault("decomposer", _passthrough_decomposer)
    return await analyze_excel_bytes(*args, **kwargs)


def _workbook_bytes(rows: list[list] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append(list(INPUT_HEADERS))
    for row in rows or [[1, "OP010", "人工拿取零件"]]:
        ws.append(row)
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _result(element, *, decision: str = "T,90,NB") -> StdsResult:
    return StdsResult(
        element=element,
        chartcode="202 010",
        decision=decision,
        time_s=1.2,
        cv="V",
        freq=1.0,
        source=Source.FORMULA,
        confidence=0.9,
        needs_review=False,
        trace=[("V1", "Turn", "operation-match")],
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


def test_fixed_template_is_flattened_to_exact_nine_columns():
    child_operations = [
        "操作人员拿取吊具",
        "操作人员移动吊具",
        "操作人员使用吊具抓取中心支撑壳体",
        "操作人员移动吊具",
        "操作人员操作吊具释放中心支撑壳体",
    ]

    async def fake_decomposer(operation):
        assert operation == "操作人员用吊具转运中心支撑壳体"
        return child_operations

    async def fake_resolver(element, deps, *, machine_hint=None):
        assert machine_hint is False
        return _result(element)

    batch = asyncio.run(
        analyze_excel_bytes(
            _workbook_bytes(
                [[2, "OP010", "操作人员用吊具转运中心支撑壳体"]]
            ),
            "1.STDS-PF清单 副本.xlsx",
            object(),
            resolver=fake_resolver,
            decomposer=fake_decomposer,
        )
    )

    wb = load_workbook(BytesIO(batch.output_bytes), data_only=False)
    ws = wb[INPUT_SHEET_NAME]
    assert tuple(ws.cell(1, col).value for col in range(1, 10)) == OUTPUT_HEADERS
    assert ws.max_column == 9
    assert ws.max_row == 6
    assert [ws.cell(row, 1).value for row in range(2, 7)] == [2] * 5
    assert [ws.cell(row, 2).value for row in range(2, 7)] == ["OP010"] * 5
    assert [ws.cell(row, 3).value for row in range(2, 7)] == child_operations
    assert [ws.cell(row, 4).value for row in range(2, 7)] == ["T,90,NB"] * 5
    assert [ws.cell(row, 5).value for row in range(2, 7)] == ["202 010"] * 5
    assert [ws.cell(row, 6).value for row in range(2, 7)] == ["V"] * 5
    assert [ws.cell(row, 7).value for row in range(2, 7)] == [1.0] * 5
    assert [ws.cell(row, 8).value for row in range(2, 7)] == [1.2] * 5
    assert all(ws.cell(row, 7).number_format == "0.##" for row in range(2, 7))
    assert all(ws.cell(row, 8).number_format == "0.00" for row in range(2, 7))
    assert all(json.loads(ws.cell(row, 9).value) for row in range(2, 7))
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:I6"
    assert batch.output_filename == "1.STDS-PF清单 副本_解析结果.xlsx"
    assert batch.detail_sheet_name == INPUT_SHEET_NAME
    front_end_rows = batch.detail_preview_rows()
    assert tuple(front_end_rows[0]) == OUTPUT_HEADERS
    assert [row["序号"] for row in front_end_rows] == [2] * 5
    assert [row["工位号"] for row in front_end_rows] == ["OP010"] * 5
    assert [row["操作内容"] for row in front_end_rows] == child_operations
    assert [row["决策描述"] for row in front_end_rows] == ["T,90,NB"] * 5
    assert [row["动作代码"] for row in front_end_rows] == ["202 010"] * 5
    assert [row["增值/非增值(C/V)"] for row in front_end_rows] == ["V"] * 5
    assert [row["频率"] for row in front_end_rows] == [1.0] * 5
    assert [row["时间"] for row in front_end_rows] == [1.2] * 5
    assert all(json.loads(row[TRACE_HEADER]) for row in front_end_rows)


def test_duplicate_operations_are_analyzed_once_but_each_source_row_is_output():
    resolver_calls = 0
    progress = []

    async def fake_resolver(element, deps, *, machine_hint=None):
        nonlocal resolver_calls
        resolver_calls += 1
        return _result(element)

    batch = asyncio.run(
        _analyze_excel_bytes(
            _workbook_bytes(
                [
                    [10, "OP010", "重复操作"],
                    [20, "OP020", "重复操作"],
                ]
            ),
            "重复.xlsx",
            object(),
            resolver=fake_resolver,
            on_progress=progress.append,
        )
    )

    assert resolver_calls == 1
    assert batch.total_count == 2
    assert batch.detail_count == 2
    assert len(batch.timing_rows()) == 4
    assert all(item.item_elapsed_s >= 0 for item in progress)
    assert progress[-1].overall_ratio == 1.0
    ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert [ws.cell(row, 1).value for row in (2, 3)] == [10, 20]
    assert [ws.cell(row, 2).value for row in (2, 3)] == ["OP010", "OP020"]


def test_final_operation_column_is_translated_without_changing_analysis_input():
    translated_inputs = []
    analyzed_inputs = []

    async def fake_decomposer(operation):
        return [
            "Manual pick up Front End Module",
            "操作人员 install ECU bracket",
            "操作人员拿取中文零件",
        ]

    async def fake_translator(operation):
        translated_inputs.append(operation)
        return {
            "Manual pick up Front End Module": "操作人员拿取 Front End Module",
            "操作人员 install ECU bracket": "操作人员安装 ECU bracket",
        }[operation]

    async def fake_resolver(element, deps, *, machine_hint=None):
        analyzed_inputs.append(element.operation_des)
        return _result(element)

    batch = asyncio.run(
        analyze_excel_bytes(
            _workbook_bytes([[1, "OP010", "Manual assemble module"]]),
            "中英混合.xlsx",
            object(),
            resolver=fake_resolver,
            decomposer=fake_decomposer,
            translator=fake_translator,
        )
    )

    expected_output = [
        "操作人员拿取 Front End Module",
        "操作人员安装 ECU bracket",
        "操作人员拿取中文零件",
    ]
    assert analyzed_inputs == [
        "Manual pick up Front End Module",
        "操作人员 install ECU bracket",
        "操作人员拿取中文零件",
    ]
    assert sorted(translated_inputs) == sorted(analyzed_inputs[:2])
    assert [detail.operation for detail in batch.rows[0].details] == analyzed_inputs
    assert [row["操作内容"] for row in batch.detail_preview_rows()] == expected_output
    ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert [ws.cell(row, 3).value for row in range(2, 5)] == expected_output


def test_output_translation_is_deduplicated_and_failure_falls_back_to_original():
    translator_calls = 0

    async def fake_translator(operation):
        nonlocal translator_calls
        translator_calls += 1
        raise RuntimeError("translation service unavailable")

    async def fake_resolver(element, deps, *, machine_hint=None):
        return _result(element)

    batch = asyncio.run(
        _analyze_excel_bytes(
            _workbook_bytes(
                [
                    [1, "OP010", "Manual pick part"],
                    [2, "OP020", "Manual pick part"],
                ]
            ),
            "翻译回退.xlsx",
            object(),
            resolver=fake_resolver,
            translator=fake_translator,
        )
    )

    assert translator_calls == 1
    assert [row["操作内容"] for row in batch.detail_preview_rows()] == [
        "Manual pick part",
        "Manual pick part",
    ]


def test_unresolved_row_has_na_result_fields_and_explanatory_trace():
    async def unresolved_resolver(element, deps, *, machine_hint=None):
        return StdsResult.unresolved(element, None)

    batch = asyncio.run(
        _analyze_excel_bytes(
            _workbook_bytes([[3, "OP030", "无法识别的动作"]]),
            "待复核.xlsx",
            object(),
            resolver=unresolved_resolver,
        )
    )

    assert batch.review_count == 1
    ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert [ws.cell(2, col).value for col in range(4, 9)] == ["NA"] * 5
    assert any(
        item["变量"].endswith("UNRESOLVED")
        for item in json.loads(ws.cell(2, 9).value)
    )


def test_machine_operation_is_not_decomposed_and_outputs_na_analysis_fields():
    decomposer_calls = 0
    hints = []

    async def should_not_decompose(operation):
        nonlocal decomposer_calls
        decomposer_calls += 1
        return [operation]

    async def fake_resolver(element, deps, *, machine_hint=None):
        hints.append(machine_hint)
        return StdsResult.machine_placeholder(element)

    batch = asyncio.run(
        analyze_excel_bytes(
            _workbook_bytes([[1, "OP010", "设备自动托盘进入"]]),
            "设备.xlsx",
            object(),
            resolver=fake_resolver,
            decomposer=should_not_decompose,
        )
    )

    assert decomposer_calls == 0
    assert hints == [True]
    ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert ws.cell(2, 3).value == "设备自动托盘进入"
    assert [ws.cell(2, col).value for col in range(4, 9)] == ["NA"] * 5
    assert list(batch.detail_preview_rows()[0].values())[3:8] == ["NA"] * 5


def test_english_header_aliases_are_supported():
    async def fake_resolver(element, deps, *, machine_hint=None):
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append([" NUMBER ", "Station_Op", "\ufeffoperation\n", "父记录"])
    ws.append([1, "OP010", "人工拿取零件"])
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        _analyze_excel_bytes(
            payload.getvalue(),
            "英文表头.xlsx",
            object(),
            resolver=fake_resolver,
        )
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert result_ws.cell(2, 1).value == 1
    assert result_ws.cell(2, 2).value == "OP010"
    assert result_ws.cell(2, 3).value == "人工拿取零件"


def test_unknown_header_alias_is_rejected():
    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append(["num", "station", "op"])
    ws.append([1, "OP010", "人工拿取零件"])
    payload = BytesIO()
    wb.save(payload)

    with pytest.raises(ExcelInputError, match="序号/number"):
        asyncio.run(_analyze_excel_bytes(payload.getvalue(), "未知表头.xlsx", object()))


def test_header_hidden_characters_and_whitespace_are_cleaned():
    async def fake_resolver(element, deps, *, machine_hint=None):
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append(["\ufeff 序\n号 ", "\u3000工位号\t", "\u200b操作 内容\u2060"])
    ws.append([1, "OP010", "人工拿取零件"])
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        _analyze_excel_bytes(
            payload.getvalue(),
            "隐藏字符模板.xlsx",
            object(),
            resolver=fake_resolver,
        )
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert result_ws.cell(2, 1).value == 1
    assert result_ws.cell(2, 2).value == "OP010"
    assert result_ws.cell(2, 3).value == "人工拿取零件"


def test_columns_after_first_three_are_ignored():
    async def fake_resolver(element, deps, *, machine_hint=None):
        return _result(element)

    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append([*INPUT_HEADERS, "决策描述", "动作代码", "增值/非增值(C/V)", "频率", "时间"])
    ws.append([1, "OP010", "人工拿取零件", "旧决策", "旧代码", "C", 9, 99])
    payload = BytesIO()
    wb.save(payload)

    batch = asyncio.run(
        _analyze_excel_bytes(
            payload.getvalue(),
            "已有结果列.xlsx",
            object(),
            resolver=fake_resolver,
        )
    )
    result_ws = load_workbook(BytesIO(batch.output_bytes))[INPUT_SHEET_NAME]
    assert result_ws.cell(2, 3).value == "人工拿取零件"
    assert result_ws.cell(2, 4).value == "T,90,NB"
    assert result_ws.cell(2, 5).value == "202 010"
    assert result_ws.cell(2, 7).value == 1.0
    assert result_ws.cell(2, 8).value == 1.2


def test_missing_number_or_station_is_rejected():
    with pytest.raises(ExcelInputError, match="序号不能为空"):
        asyncio.run(
            _analyze_excel_bytes(
                _workbook_bytes([[None, "OP010", "人工拿取零件"]]),
                "缺序号.xlsx",
                object(),
            )
        )
    with pytest.raises(ExcelInputError, match="工位号不能为空"):
        asyncio.run(
            _analyze_excel_bytes(
                _workbook_bytes([[1, None, "人工拿取零件"]]),
                "缺工位.xlsx",
                object(),
            )
        )


def test_operation_formula_without_cached_value_is_rejected():
    wb = Workbook()
    ws = wb.active
    ws.title = INPUT_SHEET_NAME
    ws.append(list(INPUT_HEADERS))
    ws.append([1, "OP010", "=A2"])
    payload = BytesIO()
    wb.save(payload)

    with pytest.raises(ExcelInputError, match="没有已计算值"):
        asyncio.run(_analyze_excel_bytes(payload.getvalue(), "公式.xlsx", object()))


@pytest.mark.parametrize("payload", [b"", b"not-an-xlsx"])
def test_invalid_excel_has_user_friendly_error(payload):
    with pytest.raises(ExcelInputError):
        asyncio.run(_analyze_excel_bytes(payload, "bad.xlsx", object()))
