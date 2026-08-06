from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.domain.models import StdsElement, ValueOption
from stds.experience import (
    ExperienceEntry,
    ExperienceIndex,
    ParameterExperienceEntry,
)
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

    async def match_chartcode_semantic(
        self,
        operation_des,
        expected_chartcode=None,
    ):
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


class _IndependentExperienceIndex:
    available = True
    digest = "independent-experience-digest"
    source_name = "independent-experience.xlsx"

    def __init__(self, chart_context, parameter_context):
        self.chart_context = chart_context
        self.parameter_context = parameter_context
        self.chart_calls = []
        self.parameter_calls = []

    async def match_chartcode_semantic(
        self,
        operation_des,
        expected_chartcode=None,
    ):
        self.chart_calls.append((operation_des, expected_chartcode))
        return self.chart_context

    async def match_parameters(
        self,
        operation_des,
        expected_chartcode=None,
    ):
        self.parameter_calls.append((operation_des, expected_chartcode))
        return self.parameter_context


class _OrthogonalEmbed:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [0.0, 1.0]


class _AlignedEmbed:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0]


def _element(number: int, operation: str) -> StdsElement:
    return StdsElement(
        number,
        operation,
        "L",
        "S",
        freq=1.0,
        norm_key=operation,
    )


def test_llm_selected_chartcode_still_uses_independent_parameter_pool():
    charts = load_charts()
    parameter_context = _ExperienceContext(
        experience_id="PARAM-TURN-202010",
        operation_label="转身",
        chartcode="202 010",
        match_type="contains",
        similarity=1.0,
        chart_row=0,
        parameter_row=8,
        parameter_text="转身参数经验",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    experience_index = _IndependentExperienceIndex(
        chart_context=None,
        parameter_context=parameter_context,
    )
    selector_calls = []

    async def select_chartcode(operation_des, available_charts):
        selector_calls.append(operation_des)
        return "202 010"

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A向右转身"), deps, machine_hint=False)
    )

    assert selector_calls == ["人工A向右转身"]
    assert experience_index.parameter_calls == [
        ("人工A向右转身", "202 010")
    ]
    assert result.chartcode == "202 010"
    assert result.decision == "T,180,NB"
    assert not any(
        step[0] == "ExperienceChartcode"
        for step in result.trace
    )
    parameter_trace = [
        step
        for step in result.trace
        if step[0] == "ExperienceParameter"
    ]
    assert len(parameter_trace) == 1
    assert "PARAM-TURN-202010" in parameter_trace[0][2]


def test_llm_chart_sends_all_parameter_experiences_to_one_selector():
    charts = load_charts()
    turn = ParameterExperienceEntry(
        experience_id="PARAM-TURN-202010",
        operation_label="转身",
        normalized_operation="转身",
        chartcode="202 010",
        parameter_row=8,
        parameter_text="转身参数经验",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    bend = ParameterExperienceEntry(
        experience_id="PARAM-BEND-202010",
        operation_label="弯腰",
        normalized_operation="弯腰",
        chartcode="202 010",
        parameter_row=9,
        parameter_text="弯腰参数经验",
        variable_hints={
            1: "默认选择 No Twist or Turn",
            3: "未描述角度时默认选择 45 Body Bend",
        },
    )
    experience_index = ExperienceIndex(
        [],
        parameter_entries=[turn, bend],
    )
    selector_calls = []

    async def select_chartcode(operation_des, available_charts):
        return "202 010"

    async def select_parameter_experience(operation_des, chartcode, contexts):
        selector_calls.append((operation_des, chartcode, tuple(contexts)))
        return 0, "当前动作是转身"

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
        llm_select_parameter_experience=select_parameter_experience,
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A向右转身"), deps, machine_hint=False)
    )

    assert len(selector_calls) == 1
    operation, chartcode, candidates = selector_calls[0]
    assert operation == "人工A向右转身"
    assert chartcode == "202 010"
    assert [context.experience_id for context in candidates] == [
        "PARAM-TURN-202010",
        "PARAM-BEND-202010",
    ]
    assert result.decision == "T,180,NB"
    parameter_trace = next(
        step for step in result.trace if step[0] == "ExperienceParameter"
    )
    assert "candidate_count=2" in parameter_trace[2]
    assert "selected_index=0" in parameter_trace[2]
    assert "PARAM-TURN-202010" in parameter_trace[2]


def test_semantic_experience_toggle_off_forces_llm_chartcode_selection():
    charts = load_charts()
    chart_entry = ExperienceEntry(
        experience_id="EXP-TURN-202010",
        operation_label="转身",
        normalized_operation="转身",
        chartcode="202 010",
        chart_row=2,
    )
    selector_calls = []

    async def select_chartcode(operation_des, available_charts):
        selector_calls.append(operation_des)
        return "202 010"

    async def pick_first(_operation_des, _candidates):
        return _candidates[0], 1.0, "test"

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
        llm_pick_value=pick_first,
        experience_index=ExperienceIndex(
            [chart_entry],
            embed_backend=_AlignedEmbed(),
        ),
        use_semantic_experience=False,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A转身"), deps, machine_hint=False)
    )

    assert selector_calls == ["人工A转身"]
    assert result.chartcode == "202 010"
    assert not any(
        step[0] == "ExperienceChartcode" for step in result.trace
    )


def test_lexical_only_experience_api_cannot_select_chartcode():
    charts = load_charts()
    lexical_calls = []
    selector_calls = []

    class LexicalOnlyIndex:
        available = True
        parameter_records = ()

        async def match(self, operation_des, expected_chartcode=None):
            lexical_calls.append((operation_des, expected_chartcode))
            return _ExperienceContext(
                experience_id="LEXICAL-TURN",
                operation_label="转身",
                chartcode="202 010",
                match_type="exact",
                similarity=1.0,
                chart_row=2,
                parameter_row=None,
                parameter_text="",
                variable_hints={},
            )

    async def select_chartcode(operation_des, available_charts):
        selector_calls.append(operation_des)
        return "202 010"

    async def pick_first(_operation_des, _candidates):
        return _candidates[0], 1.0, "test"

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        experience_index=LexicalOnlyIndex(),
        llm_select_chartcode=select_chartcode,
        llm_pick_value=pick_first,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A转身"), deps, machine_hint=False)
    )

    assert lexical_calls == []
    assert selector_calls == ["人工A转身"]
    assert result.chartcode == "202 010"
    assert not any(
        step[0] == "ExperienceChartcode" for step in result.trace
    )


def test_semantic_chart_experience_keeps_its_own_bound_parameter_record():
    charts = load_charts()
    chart_context = _ExperienceContext(
        experience_id="CHART-BODY-202010",
        operation_label="身体姿态调整",
        chartcode="202 010",
        match_type="semantic",
        similarity=0.93,
        chart_row=3,
        parameter_row=3,
        parameter_text="不应进入决策树的旧附着参数",
        variable_hints={
            1: "默认选择 No Twist or Turn",
            3: "默认选择 45 Body Bend",
        },
    )
    parameter_context = _ExperienceContext(
        experience_id="PARAM-TURN-202010",
        operation_label="转身",
        chartcode="202 010",
        match_type="contains",
        similarity=1.0,
        chart_row=0,
        parameter_row=9,
        parameter_text="独立转身参数",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    experience_index = _IndependentExperienceIndex(
        chart_context=chart_context,
        parameter_context=parameter_context,
    )

    async def chart_selector_must_not_run(operation_des, available_charts):
        raise AssertionError("Chartcode 经验已选码，不应调用 LLM")

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=chart_selector_must_not_run,
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A向右转身"), deps, machine_hint=False)
    )

    assert experience_index.chart_calls == [
        ("人工A向右转身", None)
    ]
    assert experience_index.parameter_calls == []
    assert result.decision == "NTT,45B"
    chart_trace = next(
        step
        for step in result.trace
        if step[0] == "ExperienceChartcode"
    )
    parameter_trace = next(
        step
        for step in result.trace
        if step[0] == "ExperienceParameter"
    )
    assert "CHART-BODY-202010" in chart_trace[2]
    assert "PARAM-TURN-202010" not in chart_trace[2]
    assert "CHART-BODY-202010" in parameter_trace[2]
    assert "PARAM-TURN-202010" not in parameter_trace[2]
    assert "mode=bound-chartcode-experience" in parameter_trace[2]


def test_modern_index_falls_back_to_attached_legacy_parameter_hints():
    charts = load_charts()
    legacy_entry = ExperienceEntry(
        experience_id="LEGACY-TURN-202010",
        operation_label="转身",
        normalized_operation="转身",
        chartcode="202 010",
        chart_row=2,
        parameter_row=2,
        parameter_text="旧版附着转身参数",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    experience_index = ExperienceIndex(
        [legacy_entry],
        source_name="legacy-experience.xlsx",
        embed_backend=_AlignedEmbed(),
    )

    async def chart_selector_must_not_run(operation_des, available_charts):
        raise AssertionError("旧 Chartcode 经验已选码，不应调用 LLM")

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=chart_selector_must_not_run,
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A转身"), deps, machine_hint=False)
    )

    assert result.decision == "T,180,NB"
    parameter_trace = next(
        step
        for step in result.trace
        if step[0] == "ExperienceParameter"
    )
    assert "LEGACY-TURN-202010" in parameter_trace[2]
    assert "mode=bound-chartcode-experience" in parameter_trace[2]


def test_semantic_chart_hit_does_not_switch_to_another_parameter_record():
    charts = load_charts()
    chart_entry = ExperienceEntry(
        experience_id="CHART-TURN-202010",
        operation_label="转身",
        normalized_operation="转身",
        chartcode="202 010",
        chart_row=2,
        parameter_row=2,
        parameter_text="不得串用的旧附着参数",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    bend_parameter = ParameterExperienceEntry(
        experience_id="PARAM-BEND-202010",
        operation_label="弯腰",
        normalized_operation="弯腰",
        chartcode="202 010",
        parameter_row=8,
        parameter_text="弯腰独立参数",
        variable_hints={
            1: "默认选择 No Twist or Turn",
            3: "默认选择 45 Body Bend",
        },
    )
    experience_index = ExperienceIndex(
        [chart_entry],
        parameter_entries=[bend_parameter],
        embed_backend=_AlignedEmbed(),
    )

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A转身"), deps, machine_hint=False)
    )

    assert result.chartcode == "202 010"
    assert result.decision == "T,180,NB"
    assert any(
        step[0] == "ExperienceChartcode"
        for step in result.trace
    )
    assert any(step[0] == "ExperienceParameter" for step in result.trace)
    assert "CHART-TURN-202010" in repr(result.trace)
    assert "PARAM-BEND-202010" not in repr(result.trace)


def test_llm_chart_does_not_requery_chart_experience_for_parameters():
    charts = load_charts()
    legacy_context = _ExperienceContext(
        experience_id="LEGACY-CONSTRAINED-TURN",
        operation_label="转身",
        chartcode="202 010",
        match_type="contains",
        similarity=1.0,
        chart_row=6,
        parameter_row=6,
        parameter_text="旧版约束参数",
        variable_hints={
            1: "默认选择 Turn",
            2: "未描述角度时默认选择 180°",
            3: "未描述弯腰时默认选择 No Bend",
        },
    )
    empty_pool_context = _ExperienceContext(
        experience_id="EMPTY-POOL-TURN",
        operation_label="转身",
        chartcode="202 010",
        match_type="contains",
        similarity=1.0,
        chart_row=0,
        parameter_row=9,
        parameter_text="校验后为空",
        variable_hints={},
    )

    class _ConstrainedLegacyIndex:
        available = True
        source_name = "mixed-experience.xlsx"

        def __init__(self):
            self.chart_calls = []
            self.parameter_calls = []

        async def match_chartcode_semantic(
            self,
            operation_des,
            expected_chartcode=None,
        ):
            self.chart_calls.append((operation_des, expected_chartcode))
            return legacy_context if expected_chartcode else None

        async def match_parameters(
            self,
            operation_des,
            expected_chartcode=None,
        ):
            self.parameter_calls.append((operation_des, expected_chartcode))
            return empty_pool_context

    experience_index = _ConstrainedLegacyIndex()

    async def select_chartcode(operation_des, available_charts):
        return "202 010"

    async def pick_first(operation_des, candidates, **kwargs):
        assert "experience_hint" not in kwargs
        return candidates[0], 1.0, "test-first"

    deps = Deps(
        charts=charts,
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
        llm_pick_value=pick_first,
        experience_index=experience_index,
    )

    result = asyncio.run(
        resolve(_element(1, "人工A向右转身"), deps, machine_hint=False)
    )

    assert experience_index.chart_calls == [
        ("人工A向右转身", None),
    ]
    assert experience_index.parameter_calls == [
        ("人工A向右转身", "202 010")
    ]
    assert result.decision == "NTT,NB"
    assert not any(
        step[0] == "ExperienceChartcode"
        for step in result.trace
    )
    assert not any(
        step[0] == "ExperienceParameter" for step in result.trace
    )
    assert "LEGACY-CONSTRAINED-TURN" not in repr(result.trace)
    assert "EMPTY-POOL-TURN" not in repr(result.trace)


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
