from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.domain.models import StdsElement, ValueOption
from stds.llm.pick_value import _experience_default_choice, pick_value


@dataclass(frozen=True)
class _ExperienceContext:
    experience_id: str
    operation_label: str
    chartcode: str
    match_type: str
    similarity: float
    chart_row: int
    parameter_row: Optional[int]
    parameter_text: str
    variable_hints: dict[int, str]


class _TurnBendExperienceIndex:
    available = True
    digest = "experience-digest"
    source_name = "STDS评估经验V1.2.xlsx"

    def __init__(self):
        self.contexts = {
            "转身": _ExperienceContext(
                experience_id="EXP-TURN-202010",
                operation_label="转身",
                chartcode="202 010",
                match_type="contains",
                similarity=1.0,
                chart_row=2,
                parameter_row=2,
                parameter_text="转身参数经验",
                variable_hints={
                    1: "默认选择 Turn",
                    2: "未描述角度时默认选择 180°",
                    3: "未描述弯腰时默认选择 No Bend",
                },
            ),
            "弯腰": _ExperienceContext(
                experience_id="EXP-BEND-202010",
                operation_label="弯腰",
                chartcode="202 010",
                match_type="contains",
                similarity=1.0,
                chart_row=4,
                parameter_row=4,
                parameter_text="弯腰参数经验",
                variable_hints={
                    1: "默认选择 No Twist or Turn",
                    3: "未描述角度时默认选择 45 Body Bend",
                },
            ),
        }

    async def match(self, operation_des, expected_chartcode=None):
        matches = [
            context
            for label, context in self.contexts.items()
            if label in operation_des
            and (
                expected_chartcode is None
                or context.chartcode == expected_chartcode
            )
        ]
        return matches[0] if len(matches) == 1 else None


def _element(number: int, operation: str) -> StdsElement:
    return StdsElement(
        number,
        operation,
        "L",
        "S",
        freq=1.0,
        norm_key=operation,
    )


def test_same_chartcode_keeps_turn_and_bend_parameter_identity():
    """共用 202 010 时仍按经验身份取各自的 Vn 提示。"""
    charts = load_charts()

    async def chart_selector_must_not_run(operation_des, charts):
        raise AssertionError("唯一有效经验应直接选择 Chartcode")

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=chart_selector_must_not_run,
        experience_index=_TurnBendExperienceIndex(),
        experience_scope="experience-digest",
    )

    turn = asyncio.run(
        resolve(_element(1, "人工A转身"), deps, machine_hint=False)
    )
    bend = asyncio.run(
        resolve(_element(2, "人工A弯腰"), deps, machine_hint=False)
    )

    assert turn.chartcode == bend.chartcode == "202 010"
    assert turn.decision == "T,180,NB"
    assert bend.decision == "NTT,45B"

    turn_trace = repr(turn.trace)
    bend_trace = repr(bend.trace)
    assert "EXP-TURN-202010" in turn_trace
    assert "EXP-BEND-202010" not in turn_trace
    assert "EXP-BEND-202010" in bend_trace
    assert "EXP-TURN-202010" not in bend_trace
    assert any(step[0] == "ExperienceChartcode" for step in turn.trace)
    assert any(step[0] == "ExperienceChartcode" for step in bend.trace)


def test_explicit_numeric_fact_precedes_experience_default():
    candidates = [
        ValueOption(1, 1, 1, "10m", "10M,", 10.0, 0, 0),
        ValueOption(1, 1, 2, "25m", "25M,", 25.0, 0, 0),
    ]
    context = _ExperienceContext(
        experience_id="EXP-WALK",
        operation_label="行走",
        chartcode="050 222",
        match_type="exact",
        similarity=1.0,
        chart_row=3,
        parameter_row=3,
        parameter_text="默认10m",
        variable_hints={1: "默认选择 10m"},
    )

    choice, _, reason = asyncio.run(
        pick_value(
            "人工A行走25m",
            candidates,
            experience_hint=context.variable_hints[1],
            experience_context=context,
            experience_source="experience.xlsx",
        )
    )

    assert choice.description == "25m"
    assert reason.startswith("numeric:")


def test_distance_fact_only_applies_to_distance_candidates():
    context = _ExperienceContext(
        experience_id="EXP-WALK",
        operation_label="行走",
        chartcode="050 222",
        match_type="exact",
        similarity=1.0,
        chart_row=3,
        parameter_row=3,
        parameter_text="行走参数",
        variable_hints={1: "默认选择Unobstructed"},
    )
    category_candidates = [
        ValueOption(1, 1, 1, "Unobstructed", "UOBS,", 0.48, 0, 0),
        ValueOption(1, 1, 2, "Obstructed", "OBS,", 0.66, 0, 0),
    ]

    category, _, category_reason = asyncio.run(
        pick_value(
            "人工A行走5m",
            category_candidates,
            experience_hint=context.variable_hints[1],
            experience_context=context,
        )
    )

    assert category.description == "Unobstructed"
    assert category_reason.startswith("experience-default:")

    distance_candidates = [
        ValueOption(
            2, 1, 1, "5 Paces/12.5ft/3.8m", "3.8MX", 5.0, 0, 0
        ),
        ValueOption(
            2, 1, 2, "7 Paces/17.5ft/5.3m", "5.3MX", 7.0, 0, 0
        ),
    ]
    distance, _, distance_reason = asyncio.run(
        pick_value("人工A行走5m", distance_candidates)
    )

    assert distance.description.endswith("5.3m")
    assert distance_reason == "numeric:distance_m=5.0"


def test_conditional_experience_does_not_force_its_default_branch():
    candidates = [
        ValueOption(1, 1, 1, "Unobstructed", "UOBS,", 0.48, 0, 0),
        ValueOption(1, 1, 2, "Obstructed", "OBS,", 0.66, 0, 0),
    ]
    hint = (
        "默认选择Unobstructed（默认值），"
        "除非路径存在障碍时选择Obstructed"
    )

    assert _experience_default_choice(hint, candidates) is None

    fit_candidates = [
        ValueOption(3, 1, 1, "Simple place", "SIM,", 1.0, 0, 0),
        ValueOption(3, 1, 2, "Loose", "LOO,", 2.0, 0, 0),
        ValueOption(3, 1, 3, "Close", "CLO,", 3.0, 0, 0),
    ]
    fit_hint = (
        "基于安装配合要求选择Simple place/Loose/Close，"
        "若无法获得信息，默认选择Simple place（默认值）"
    )
    assert _experience_default_choice(fit_hint, fit_candidates) is None


def test_shared_angle_chart_applies_explicit_angle_to_correct_action_only():
    charts = load_charts()

    async def chart_selector_must_not_run(operation_des, charts):
        raise AssertionError("唯一有效经验应直接选择 Chartcode")

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=chart_selector_must_not_run,
        experience_index=_TurnBendExperienceIndex(),
        experience_scope="experience-digest",
    )

    turn = asyncio.run(
        resolve(_element(1, "人工A转身90度"), deps, machine_hint=False)
    )
    bend = asyncio.run(
        resolve(_element(2, "人工A弯腰90度"), deps, machine_hint=False)
    )

    assert turn.decision == "T,90,NB"
    assert bend.decision == "NTT,90B"
    assert "EXP-BEND-202010" not in repr(turn.trace)
    assert "EXP-TURN-202010" not in repr(bend.trace)
