"""单条操作两阶段分析服务。"""
from __future__ import annotations

import asyncio

from stds.domain.models import Source, StdsResult
from stds.pipeline.operation_analysis import analyze_operation


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
    assert tuple(decomposition_rows[0]) == ("序号", "工位号", "操作内容")
    assert [row["序号"] for row in decomposition_rows] == [1, 1, 1]
    assert [row["工位号"] for row in decomposition_rows] == ["手动输入"] * 3
    assert [row["操作内容"] for row in decomposition_rows] == children


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
