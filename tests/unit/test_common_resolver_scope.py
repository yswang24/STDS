from __future__ import annotations

import asyncio
from dataclasses import dataclass

from stds.cascade.numeric import NumericContext
from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache, decision_cache_scope
from stds.domain.models import MostChart, Source, StdsElement, ValueOption
from stds.experience.common_index import CommonChartSemanticIndex
from stds.experience.models import CommonChartEntry, CommonChartKind


def _element(operation: str, *, freq: float = 1.0) -> StdsElement:
    return StdsElement(1, operation, "L", "S", freq=freq, norm_key=operation)


def _fixed_common(
    keyword: str,
    *,
    time_s: float = 5.0,
    row: int = 2,
) -> CommonChartEntry:
    return CommonChartEntry(
        operation_label=keyword,
        normalized_operation=keyword,
        chartcode="EST V00",
        decision=f"{time_s:g}S",
        cv="V",
        frequency=1.0,
        source_time_s=time_s,
        time_s=time_s,
        keywords=(keyword,),
        normalized_keywords=(keyword,),
        row=row,
        kind=CommonChartKind.FIXED_TIME,
    )


def _est_chart() -> MostChart:
    return MostChart(
        chartcode="EST V00",
        title="固定估算时间",
        formula="0",
        value_added=False,
        developed_in_seconds=True,
        options={},
    )


class _ExperienceMustNotRun:
    available = True
    parameter_records = (object(),)
    source_name = "experience.xlsx"

    async def match_chartcode_semantic(self, *_args, **_kwargs):
        raise AssertionError("Common 命中后不应继续选择 Chartcode")


class _CommonSemanticEmbed:
    def __init__(self, similarity: float):
        self.similarity = similarity
        self.embed_calls = 0

    def embed(self, texts):
        self.embed_calls += 1
        return [[1.0, 0.0] for _ in texts]

    def embed_one(self, text):
        residual = max(0.0, 1.0 - self.similarity**2) ** 0.5
        return [self.similarity, residual]


def test_uploaded_common_runs_even_when_other_experience_is_available():
    entry = _fixed_common("调整")
    deps = Deps(
        charts={"EST V00": _est_chart()},
        cache=AutoCache(),
        common_entries=(entry,),
        use_common_chart=True,
        experience_index=_ExperienceMustNotRun(),
        experience_scope="upload:digest-a",
    )

    result = asyncio.run(resolve(_element("人工A调整工装"), deps))

    assert result.source is Source.CACHE
    assert result.chartcode == "EST V00"
    assert result.time_s == 5.0
    assert result.decision == "5S"
    assert result.trace[0][0] == "T0.5_common"
    assert result.trace[1][0] == "EST_FIXED_TIME"


def test_common_semantic_top1_is_used_by_resolver_and_audited():
    entry = _fixed_common("托盘落位", time_s=6)
    backend = _CommonSemanticEmbed(0.80)
    common_index = CommonChartSemanticIndex(
        (entry,),
        embed_backend=backend,
    )
    deps = Deps(
        charts={"EST V00": _est_chart()},
        cache=AutoCache(),
        common_entries=(entry,),
        common_index=common_index,
        use_common_chart=True,
        use_semantic_experience=True,
        experience_scope="upload:semantic-common",
    )

    result = asyncio.run(resolve(_element("人工A放好承载物"), deps))

    assert result.chartcode == "EST V00"
    assert result.time_s == 6.0
    assert result.confidence == 0.8
    assert not result.needs_review
    assert "match=semantic" in result.trace[0][2]
    assert "similarity=0.8000" in result.trace[0][2]
    assert backend.embed_calls == 1


def test_common_semantic_toggle_off_keeps_keywords_but_skips_vector_fallback():
    entry = _fixed_common("托盘落位", time_s=6)
    backend = _CommonSemanticEmbed(1.0)

    async def no_chartcode(_operation, _charts):
        return None

    deps = Deps(
        charts={"EST V00": _est_chart()},
        cache=AutoCache(),
        common_entries=(entry,),
        common_index=CommonChartSemanticIndex(
            (entry,),
            embed_backend=backend,
        ),
        use_common_chart=True,
        use_semantic_experience=False,
        experience_scope="upload:no-semantic-common",
        llm_select_chartcode=no_chartcode,
    )

    semantic_only_miss = asyncio.run(
        resolve(_element("人工A放好承载物"), deps, machine_hint=False)
    )
    keyword_hit = asyncio.run(
        resolve(_element("人工A托盘落位"), deps, machine_hint=False)
    )

    assert semantic_only_miss.source is Source.UNRESOLVED
    assert keyword_hit.chartcode == "EST V00"
    assert backend.embed_calls == 0


def test_est_fixed_time_multiplies_only_input_record_frequency():
    entry = _fixed_common("调整", time_s=5.0)
    deps = Deps(
        charts={"EST V00": _est_chart()},
        cache=AutoCache(),
        common_entries=(entry,),
        use_common_chart=True,
        experience_scope="upload:est",
    )

    result = asyncio.run(resolve(_element("人工A调整工装", freq=4), deps))

    assert result.time_s == 20.0
    assert result.freq == 4
    assert "source_frequency=1" in result.trace[1][2]


def test_parent_weight_attempt_without_match_does_not_block_common():
    entry = _fixed_common("调整")
    deps = Deps(
        charts={"EST V00": _est_chart()},
        cache=AutoCache(),
        common_entries=(entry,),
        use_common_chart=True,
        experience_scope="upload:no-weight",
    )

    result = asyncio.run(
        resolve(
            _element("人工A调整工装"),
            deps,
            numeric_context=None,
            part_context_resolved=True,
        )
    )

    assert result.chartcode == "EST V00"
    assert result.time_s == 5.0


def test_weight_context_overrides_common_complete_decision():
    light = ValueOption(1, 1, 1, "light", "LIGHT", 0.2, 0, 0)
    chart = MostChart(
        chartcode="WEIGHT TEST",
        title="重量测试",
        formula="V1",
        value_added=False,
        developed_in_seconds=True,
        options={(1, 1): [light]},
    )

    async def select_chartcode(_operation, _charts):
        return "WEIGHT TEST"

    async def pick_value(_operation, candidates, **kwargs):
        assert kwargs["numeric_context"].weight_kg == 0.2
        return candidates[0], 1.0, "part-weight:test"

    deps = Deps(
        charts={"EST V00": _est_chart(), "WEIGHT TEST": chart},
        cache=AutoCache(),
        common_entries=(_fixed_common("拿取螺栓"),),
        use_common_chart=True,
        experience_scope="upload:weight",
        llm_select_chartcode=select_chartcode,
        llm_pick_value=pick_value,
    )
    context = NumericContext(
        weight_kg=0.2,
        query_name="螺栓",
        matched_name="螺栓",
        similarity=1.0,
        match_type="exact",
        source="test",
    )

    result = asyncio.run(
        resolve(_element("人工A拿取螺栓"), deps, numeric_context=context)
    )

    assert result.source is Source.FORMULA
    assert result.chartcode == "WEIGHT TEST"
    assert result.time_s == 0.2
    assert not any(step[0] == "T0.5_common" for step in result.trace)


def test_cache_isolated_by_upload_digest_and_common_toggle():
    cache = AutoCache()
    charts = {"EST V00": _est_chart()}
    operation = "人工A调整工装"

    deps_a = Deps(
        charts=charts,
        cache=cache,
        common_entries=(_fixed_common("调整", time_s=5),),
        use_common_chart=True,
        experience_scope="upload:a",
    )
    deps_b = Deps(
        charts=charts,
        cache=cache,
        common_entries=(_fixed_common("调整", time_s=7),),
        use_common_chart=True,
        experience_scope="upload:b",
    )

    first = asyncio.run(resolve(_element(operation), deps_a))
    switched = asyncio.run(resolve(_element(operation), deps_b))

    assert first.time_s == 5.0
    assert switched.time_s == 7.0
    assert decision_cache_scope(deps_a) != decision_cache_scope(deps_b)

    async def no_chartcode(_operation, _charts):
        return None

    common_off = Deps(
        charts=charts,
        cache=cache,
        common_entries=deps_a.common_entries,
        use_common_chart=False,
        experience_scope="upload:a",
        llm_select_chartcode=no_chartcode,
    )
    disabled = asyncio.run(resolve(_element(operation), common_off))
    assert disabled.source is Source.UNRESOLVED


@dataclass(frozen=True)
class _ParameterContext:
    experience_id: str = "PARAM-TURN"
    operation_label: str = "转身"
    chartcode: str = "202 010"
    match_type: str = "contains"
    similarity: float = 1.0
    chart_row: int = 0
    parameter_row: int = 2
    parameter_text: str = "参数V1：默认选择 Turn；参数V2：默认选择 180"
    variable_hints: dict = None

    def __post_init__(self):
        object.__setattr__(self, "variable_hints", {1: "Turn", 2: "180", 3: "No Bend"})


class _ParameterOnlyExperience:
    available = True
    entries = ()
    parameter_records = (object(),)
    source_name = "parameter.xlsx"

    async def match_chartcode_semantic(self, *_args, **_kwargs):
        return None

    async def match_parameters(self, _operation, expected_chartcode=None):
        assert expected_chartcode == "202 010"
        return _ParameterContext()


def test_parameter_experience_globally_applies_and_blocks_history_reuse():
    from stds.data.charts_loader import load_charts

    class HistoryMustNotRun:
        async def knn(self, *_args, **_kwargs):
            raise AssertionError("参数经验启用时不得提前复用 T1 完整决策")

    async def select_chartcode(_operation, _charts):
        return "202 010"

    deps = Deps(
        charts=load_charts(),
        cache=AutoCache(),
        history_index=HistoryMustNotRun(),
        experience_index=_ParameterOnlyExperience(),
        experience_scope="upload:parameters",
        llm_select_chartcode=select_chartcode,
    )

    result = asyncio.run(resolve(_element("人工A向右转身"), deps, machine_hint=False))

    assert result.chartcode == "202 010"
    assert result.decision == "T,180,NB"
    assert any(step[0] == "ExperienceParameter" for step in result.trace)
