"""单条操作两阶段分析服务。"""
from __future__ import annotations

import asyncio
import json

from stds.cascade.resolver import Deps
from stds.data.cache import AutoCache
from stds.domain.models import Source, StdsResult
from stds.llm.extract_part_name import PartOperationGroup
from stds.pipeline.operation_analysis import analyze_operation
from stds.retrieval.part_weight_index import (
    PartWeightMatch,
    PartWeightSource,
)


def _result(element, time_s=1.25):
    return StdsResult(
        element=element,
        chartcode="060 010",
        decision="LS,",
        time_s=time_s,
        cv="V",
        freq=element.freq,
        source=Source.FORMULA,
        confidence=0.9,
        needs_review=False,
        trace=[("V1", "Laser Scan", "operation-match")],
    )


def _trace_steps(value: str) -> list[dict[str, str]]:
    steps = json.loads(value)
    assert isinstance(steps, list)
    assert steps
    assert all(
        list(step) == ["变量", "选择", "原因"]
        and all(isinstance(field, str) for field in step.values())
        for step in steps
    )
    return steps


def test_manual_single_operation_is_decomposed_and_summed():
    children = ["操作人员转身", "操作人员拿取零件", "操作人员安装零件"]
    hints = []
    decomposed_events = []
    progress_events = []

    async def decomposer(operation):
        assert operation == "操作人员安装零件"
        return children

    async def resolver(element, deps, *, machine_hint=None):
        hints.append(machine_hint)
        return _result(element)

    analysis = asyncio.run(
        analyze_operation(
            "操作人员安装零件",
            object(),
            freq=2.0,
            resolver=resolver,
            decomposer=decomposer,
            on_decomposed=lambda split, elapsed: decomposed_events.append(split),
            on_progress=lambda item, completed, total: progress_events.append(
                (item.operation, completed, total)
            ),
        )
    )

    assert analysis.split.actor == "人工"
    assert [item.operation for item in analysis.items] == children
    assert hints == [False, False, False]
    assert analysis.total_time_s == 3.75
    assert analysis.status == "成功"
    assert len(decomposed_events) == 1
    assert sorted(event[1] for event in progress_events) == [1, 2, 3]
    decomposition_rows = analysis.decomposition_rows()
    assert tuple(decomposition_rows[0]) == (
        "序号",
        "项目名称",
        "工位号",
        "作业描述",
        "翻译后作业描述",
    )
    assert [row["序号"] for row in decomposition_rows] == [1, 1, 1]
    assert [row["项目名称"] for row in decomposition_rows] == ["手动输入"] * 3
    assert [row["工位号"] for row in decomposition_rows] == ["手动输入"] * 3
    assert [row["作业描述"] for row in decomposition_rows] == children
    assert [row["翻译后作业描述"] for row in decomposition_rows] == children


def test_single_operation_children_share_parent_weight_context():
    children = ["夹持低压线束", "移动吊具", "落位低压线束"]
    received = {}

    class WeightIndex:
        available = True

        async def match(self, query):
            return PartWeightMatch(
                query=query,
                matched_name="低压线束",
                part_no="P1",
                weight_kg=0.095,
                similarity=1.0,
                match_type="exact",
                sources=(PartWeightSource("DU", 40, "E40"),),
            )

    async def groups(parent, operations):
        return (
            PartOperationGroup(
                part_name="低压线束",
                child_indexes=[1, 2, 3],
                reason="同一操作链",
            ),
        )

    async def decomposer(_):
        return children

    async def resolver(
        element,
        deps,
        *,
        machine_hint=None,
        numeric_context=None,
        part_context_resolved=False,
    ):
        assert part_context_resolved
        received[element.operation_des] = numeric_context
        return _result(element)

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=WeightIndex(),
        llm_extract_part_groups=groups,
    )
    analysis = asyncio.run(
        analyze_operation(
            "人工A用吊具转运低压线束",
            deps,
            resolver=resolver,
            decomposer=decomposer,
        )
    )

    assert analysis.status == "成功"
    assert set(received) == set(children)
    assert len({id(context) for context in received.values()}) == 1
    assert {context.weight_kg for context in received.values()} == {0.095}


def test_single_operation_translates_display_only_and_resolves_original_children():
    original_children = [
        "Manual pick up Front End Module",
        "操作人员检查零件",
        "操作人员 install ECU bracket",
        "Manual pick up Front End Module",
    ]
    expected_display = [
        "操作人员拿取 Front End Module",
        "操作人员检查零件",
        "操作人员安装 ECU bracket",
        "操作人员拿取 Front End Module",
    ]
    translated_inputs = []
    resolved_inputs = []

    async def decomposer(operation):
        assert operation == "Manual assemble module"
        return original_children

    async def translator(operation):
        translated_inputs.append(operation)
        return {
            "Manual pick up Front End Module": "操作人员拿取 Front End Module",
            "操作人员 install ECU bracket": "操作人员安装 ECU bracket",
        }[operation]

    async def resolver(element, deps, *, machine_hint=None):
        resolved_inputs.append(element.operation_des)
        return _result(element)

    analysis = asyncio.run(
        analyze_operation(
            "Manual assemble module",
            object(),
            line_name="项目A",
            station_op="OP010",
            resolver=resolver,
            decomposer=decomposer,
            translator=translator,
        )
    )

    assert resolved_inputs == original_children
    assert sorted(translated_inputs) == sorted(
        [original_children[0], original_children[2]]
    )
    assert [item.operation for item in analysis.items] == original_children
    assert [item.display_operation for item in analysis.items] == expected_display
    assert analysis.split.output_operations == tuple(expected_display)
    assert [
        item.result.element.operation_des for item in analysis.items if item.result
    ] == original_children
    decomposition_rows = analysis.decomposition_rows()
    assert [row["作业描述"] for row in decomposition_rows] == original_children
    assert [row["翻译后作业描述"] for row in decomposition_rows] == (
        expected_display
    )
    detail_rows = analysis.detail_rows()
    assert tuple(detail_rows[0]) == (
        "序号",
        "项目名称",
        "工位号",
        "STDS描述",
        "Decisions",
        "Chart",
        "增值|非增值",
        "Freq",
        "Time(s)",
        "决策链选择的原因",
    )
    assert [row["项目名称"] for row in detail_rows] == ["项目A"] * len(
        expected_display
    )
    assert [row["工位号"] for row in detail_rows] == ["OP010"] * len(
        expected_display
    )
    assert [row["STDS描述"] for row in detail_rows] == expected_display
    for row in detail_rows:
        assert {
            "变量": "V1",
            "选择": "Laser Scan",
            "原因": "operation-match",
        } in _trace_steps(row["决策链选择的原因"])


def test_auto_single_operation_translation_failure_still_uses_chinese_prefix():
    operation = "aUtO Robot Load CTR to pallet"
    translated_inputs = []
    resolved_inputs = []

    async def failing_translator(value):
        translated_inputs.append(value)
        raise RuntimeError("translation service unavailable")

    async def resolver(element, deps, *, machine_hint=None):
        assert machine_hint is True
        resolved_inputs.append(element.operation_des)
        return StdsResult.machine_placeholder(element)

    analysis = asyncio.run(
        analyze_operation(
            operation,
            object(),
            resolver=resolver,
            translator=failing_translator,
        )
    )

    expected_display = "自动 Robot Load CTR to pallet"
    assert translated_inputs == [operation]
    assert resolved_inputs == [operation]
    assert analysis.items[0].operation == operation
    assert analysis.items[0].display_operation == expected_display
    assert analysis.split.output_operations == (expected_display,)
    assert analysis.decomposition_rows()[0]["作业描述"] == operation
    assert analysis.decomposition_rows()[0]["翻译后作业描述"] == expected_display
    assert analysis.detail_rows()[0]["STDS描述"] == expected_display
    detail_row = analysis.detail_rows()[0]
    assert list(detail_row.values())[4:9] == ["NA"] * 5
    assert {
        "变量": "T2_machine",
        "选择": "设备动作",
        "原因": "判定为设备动作，跳过人工标准时间计算",
    } in _trace_steps(detail_row["决策链选择的原因"])


def test_machine_single_operation_is_not_decomposed():
    decomposer_calls = 0
    hints = []

    async def decomposer(operation):
        nonlocal decomposer_calls
        decomposer_calls += 1
        return [operation]

    async def resolver(element, deps, *, machine_hint=None):
        hints.append(machine_hint)
        return StdsResult.machine_placeholder(element)

    analysis = asyncio.run(
        analyze_operation(
            "Auto IPV snap ring seated to F 压装到位",
            object(),
            resolver=resolver,
            decomposer=decomposer,
        )
    )

    assert decomposer_calls == 0
    assert hints == [True]
    assert analysis.split.actor == "设备"
    assert len(analysis.items) == 1
    assert analysis.total_time_s == 0.0


def test_decomposition_failure_falls_back_and_requires_review():
    async def failing_decomposer(operation):
        raise RuntimeError("mock split failure")

    async def resolver(element, deps, *, machine_hint=None):
        return _result(element)

    analysis = asyncio.run(
        analyze_operation(
            "操作人员处理未知零件",
            object(),
            resolver=resolver,
            decomposer=failing_decomposer,
        )
    )

    assert analysis.split.operations == ("操作人员处理未知零件",)
    assert analysis.split.needs_review is True
    assert "mock split failure" in analysis.split.error
    assert analysis.status == "待复核"
    assert analysis.total_time_s is None
