from __future__ import annotations

import asyncio

import pytest

from stds.cascade.numeric import NumericContext
from stds.cascade.resolver import Deps, resolve
from stds.cascade.rules import is_explicit_machine_action, rule_machine
from stds.data.cache import AutoCache
from stds.domain.models import Source, StdsElement, StdsResult


def _element(operation: str) -> StdsElement:
    return StdsElement(
        number=1,
        operation_des=operation,
        line_name="Line",
        station_op="Station",
        freq=1.0,
        norm_key=operation,
    )


def test_exact_robot_parent_is_machine_by_rule():
    operation = "2个机器人拧紧65颗上盖螺栓，6±1Nm"

    assert is_explicit_machine_action(operation) is True
    assert rule_machine(operation) is True


@pytest.mark.parametrize(
    "operation",
    [
        "机器人自动识别人员进入区域",
        "AGV避让人员",
        "设备自动搬运人工岛零件",
    ],
)
def test_machine_subject_is_not_overridden_by_human_object(operation: str):
    assert is_explicit_machine_action(operation) is True
    assert rule_machine(operation) is True


@pytest.mark.parametrize(
    "operation",
    [
        "机器人拧紧上盖螺栓",
        "机械手拿取零件",
        "AGV搬运料架",
        "设备压装轴承",
        "2 个 机器人拧紧上盖螺栓",
        "两台机械手抓取零件",
        "自动扫码",
        "Auto tighten fasteners",
    ],
)
def test_explicit_machine_helper_does_not_depend_on_decomposer(operation: str):
    assert is_explicit_machine_action(operation) is True


@pytest.mark.parametrize(
    "operation",
    [
        "人工A拿取拧紧枪",
        "操作人员启动自动设备",
        "员工操作机器人拧紧螺栓",
        "Manual operator starts AGV",
    ],
)
def test_explicit_human_tool_action_is_not_machine(operation: str):
    assert is_explicit_machine_action(operation) is False
    assert rule_machine(operation) is False


def test_resolver_explicit_machine_overrides_false_hint_and_populated_cache():
    operation = "2个机器人拧紧65颗上盖螺栓，6±1Nm"
    element = _element(operation)
    cache = AutoCache()
    cache.put(
        element.norm_key,
        StdsResult(
            element=element,
            chartcode="STALE FORMULA",
            decision="SHOULD_NOT_LEAK",
            time_s=99.0,
            cv="V",
            freq=1.0,
            source=Source.FORMULA,
            confidence=1.0,
            needs_review=False,
        ),
    )
    deps = Deps(charts={}, cache=cache)

    result = asyncio.run(resolve(element, deps, machine_hint=False))

    assert result.source is Source.MACHINE
    assert result.chartcode is None
    assert result.decision == ""
    assert result.time_s == 0.0


def test_resolver_explicit_machine_precedes_experience_and_weight_context():
    class ExperienceIndex:
        available = True

        async def match_chartcode_semantic(self, *_args, **_kwargs):
            raise AssertionError("machine action must return before experience")

    class WeightIndex:
        available = True

        async def match(self, *_args, **_kwargs):
            raise AssertionError("machine action must return before weight lookup")

    async def extract_part_name(_operation: str):
        raise AssertionError("machine action must return before part extraction")

    element = _element("机械手拿取并安装2个零件")
    deps = Deps(
        charts={},
        cache=AutoCache(),
        experience_index=ExperienceIndex(),
        part_weight_index=WeightIndex(),
        llm_extract_part_name=extract_part_name,
    )
    numeric_context = NumericContext(
        weight_kg=1.0,
        query_name="零件",
        matched_name="零件",
        similarity=1.0,
        match_type="exact",
    )

    result = asyncio.run(
        resolve(
            element,
            deps,
            machine_hint=False,
            numeric_context=numeric_context,
        )
    )

    assert result.source is Source.MACHINE
    assert result.chartcode is None
    assert result.time_s == 0.0
