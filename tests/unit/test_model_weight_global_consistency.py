"""模型判断重量在一次 Deps/批任务内保持全局一致的回归测试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from stds.cascade.numeric import NumericContext, PartIdentityContext
from stds.cascade.resolver import Deps, resolve, resolve_part_weight_groups
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.domain.models import MostChart, Source, StdsElement, ValueOption
from stds.llm.extract_part_name import PartOperationGroup
from stds.llm.pick_value import pick_value
from stds.retrieval.model_weight_pool import ModelWeightPool


def _weight_option(weight_kg: float, value_number: int) -> ValueOption:
    return ValueOption(
        variable_number=1,
        range_number=1,
        value_number=value_number,
        description=f"{weight_kg:g} kg",
        metric_abbrev=f"{weight_kg:g}KGX,",
        formula_value=weight_kg,
        next_variable=0,
        next_range=0,
    )


def _weight_chart() -> MostChart:
    candidates = [_weight_option(0.23, 1), _weight_option(0.45, 2)]
    return MostChart(
        chartcode="TEST MODEL WEIGHT",
        title="Get object by weight",
        formula="V1",
        value_added=False,
        developed_in_seconds=True,
        options={(1, 1): candidates},
    )


def _element(number: int, operation: str) -> StdsElement:
    return StdsElement(
        number=number,
        operation_des=operation,
        line_name="Line",
        station_op="Station",
        freq=1.0,
        norm_key=f"{number}:{operation}",
    )


def test_parent_group_keeps_one_shared_part_identity_when_weight_table_misses():
    match_calls = []

    class MissingWeightIndex:
        available = True

        async def match(self, query):
            match_calls.append(query)
            return None

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(
                part_name="Tray",
                child_indexes=[1, 2],
                reason="同一个 Tray 的连续操作",
            ),
        )

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=MissingWeightIndex(),
        llm_extract_part_groups=extract_groups,
    )

    resolution = asyncio.run(
        resolve_part_weight_groups(
            "人工A用吊具将Tray落位到小车",
            ("人工A夹持Tray", "人工A落位Tray"),
            deps,
        )
    )

    assert resolution.attempted is True
    assert resolution.contexts == {}
    assert sorted(resolution.identity_contexts) == [1, 2]
    assert resolution.identity_contexts[1] is resolution.identity_contexts[2]
    assert resolution.identity_contexts[1].part_name == "Tray"
    assert resolution.identity_contexts[1].identity_key == "extracted_name:tray"
    assert match_calls == ["Tray"]


def test_model_weight_identity_preserves_part_number_punctuation():
    class MissingWeightIndex:
        available = True

        async def match(self, query):
            return None

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(part_name="P-1", child_indexes=[1]),
            PartOperationGroup(part_name="P1", child_indexes=[2]),
        )

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=MissingWeightIndex(),
        llm_extract_part_groups=extract_groups,
    )

    resolution = asyncio.run(
        resolve_part_weight_groups(
            "分别拿取P-1和P1",
            ("拿取P-1", "拿取P1"),
            deps,
        )
    )

    assert resolution.identity_contexts[1].identity_key == (
        "extracted_name:p-1"
    )
    assert resolution.identity_contexts[2].identity_key == (
        "extracted_name:p1"
    )


def test_same_part_across_concurrent_parents_calls_final_llm_once(monkeypatch):
    """两个父工序共享一个 Deps，表重 miss 后只能产生一次模型重量判断。"""

    match_calls = 0

    class MissingWeightIndex:
        available = True

        async def match(self, query):
            nonlocal match_calls
            match_calls += 1
            # 让两个父工序都进入请求级重量检索 singleflight。
            await asyncio.sleep(0.01)
            return None

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(
                part_name="Tray",
                child_indexes=[1],
                reason=parent,
            ),
        )

    selector_calls = 0
    selectors_ready = asyncio.Event()

    async def select_chartcode(operation, charts):
        nonlocal selector_calls
        selector_calls += 1
        if selector_calls == 2:
            selectors_ready.set()
        await selectors_ready.wait()
        return "TEST MODEL WEIGHT"

    llm_calls = 0
    llm_started = asyncio.Event()
    release_llm = asyncio.Event()

    async def fake_structured(prompt, schema):
        nonlocal llm_calls
        llm_calls += 1
        llm_started.set()
        await release_llm.wait()
        return schema(index=1, reason="模型判断 Tray 属于 0.45kg 档")

    monkeypatch.setattr("stds.llm.pick_value.structured", fake_structured)

    class HistoryIndex:
        async def knn(self, text, k):
            raise AssertionError("存在零件身份时不得绕过模型重量池复用历史决策")

    chart = _weight_chart()
    deps = Deps(
        charts={chart.chartcode: chart},
        cache=AutoCache(),
        history_index=HistoryIndex(),
        part_weight_index=MissingWeightIndex(),
        llm_extract_part_groups=extract_groups,
        llm_select_chartcode=select_chartcode,
    )

    async def scenario():
        first_group, second_group = await asyncio.gather(
            resolve_part_weight_groups(
                "父工序A：人工A拿取4个Tray",
                ("人工A拿取4个Tray",),
                deps,
            ),
            resolve_part_weight_groups(
                "父工序B：人工B安装Tray",
                ("人工B安装Tray",),
                deps,
            ),
        )
        first_identity = first_group.identity_contexts[1]
        second_identity = second_group.identity_contexts[1]
        first_task = asyncio.create_task(
            resolve(
                _element(1, "人工A拿取4个Tray"),
                deps,
                machine_hint=False,
                part_identity_context=first_identity,
                part_context_resolved=True,
            )
        )
        second_task = asyncio.create_task(
            resolve(
                _element(2, "人工B安装Tray"),
                deps,
                machine_hint=False,
                part_identity_context=second_identity,
                part_context_resolved=True,
            )
        )
        await llm_started.wait()
        # 给另一父工序机会到达同一零件的锁；它应等待而不是再次调用模型。
        await asyncio.sleep(0.01)
        release_llm.set()
        results = await asyncio.gather(first_task, second_task)
        return first_identity, second_identity, results

    first_identity, second_identity, results = asyncio.run(scenario())

    assert first_identity.identity_key == second_identity.identity_key
    assert match_calls == 1
    assert selector_calls == 2
    assert llm_calls == 1
    assert [result.source for result in results] == [Source.FORMULA, Source.FORMULA]
    assert [result.decision for result in results] == ["0.45KGX", "0.45KGX"]
    # 描述里的“4个”不是频次；拆分后的每一条都保持单次工时。
    assert [result.time_s for result in results] == [0.45, 0.45]
    assert all(result.freq == 1.0 for result in results)
    reasons = [step[2] for result in results for step in result.trace]
    assert sum("model-weight-pool:selected" in reason for reason in reasons) == 1
    assert sum("model-weight-pool:mapped-exact" in reason for reason in reasons) == 1
    assert all(any(step[0] == "PartIdentity" for step in result.trace) for result in results)


def test_different_parts_are_isolated_in_one_request(monkeypatch):
    chart = _weight_chart()
    llm_operations = []

    async def fake_structured(prompt, schema):
        if "Tray" in prompt:
            llm_operations.append("Tray")
            return schema(index=1, reason="Tray 较重")
        llm_operations.append("螺栓")
        return schema(index=0, reason="螺栓较轻")

    monkeypatch.setattr("stds.llm.pick_value.structured", fake_structured)

    async def select_chartcode(operation, charts):
        return chart.chartcode

    deps = Deps(
        charts={chart.chartcode: chart},
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
    )
    tray = PartIdentityContext("Tray", "extracted_name:tray", "parent", "G1")
    bolt = PartIdentityContext("螺栓", "extracted_name:螺栓", "parent", "G1")

    async def scenario():
        initial = await asyncio.gather(
            resolve(
                _element(1, "人工A拿取Tray"),
                deps,
                machine_hint=False,
                part_identity_context=tray,
                part_context_resolved=True,
            ),
            resolve(
                _element(2, "人工A拿取螺栓"),
                deps,
                machine_hint=False,
                part_identity_context=bolt,
                part_context_resolved=True,
            ),
        )
        reused = await asyncio.gather(
            resolve(
                _element(3, "人工B安装Tray"),
                deps,
                machine_hint=False,
                part_identity_context=tray,
                part_context_resolved=True,
            ),
            resolve(
                _element(4, "人工B安装螺栓"),
                deps,
                machine_hint=False,
                part_identity_context=bolt,
                part_context_resolved=True,
            ),
        )
        return initial, reused

    initial, reused = asyncio.run(scenario())

    assert sorted(llm_operations) == ["Tray", "螺栓"]
    assert [result.decision for result in initial] == ["0.45KGX", "0.23KGX"]
    assert [result.decision for result in reused] == ["0.45KGX", "0.23KGX"]
    assert all(
        any("model-weight-pool:mapped-exact" in step[2] for step in result.trace)
        for result in reused
    )


def test_deterministic_weight_facts_and_experience_beat_cached_model_band(
    monkeypatch,
):
    """表重、描述明示重量、确定性经验都不能被已缓存模型档覆盖。"""

    candidates = [_weight_option(0.23, 1), _weight_option(0.45, 2)]
    pool = ModelWeightPool()
    identity = PartIdentityContext(
        part_name="Tray",
        identity_key="extracted_name:tray",
        source="parent-operation-group",
        group_id="G1",
    )
    table_context = NumericContext(
        weight_kg=0.1,
        query_name="Tray",
        matched_name="Tray",
        similarity=1.0,
        match_type="exact",
        source="Weight!E2",
    )
    experience_context = SimpleNamespace(
        experience_id="EXP-1",
        operation_label="拿取Tray",
        parameter_row=12,
    )

    async def forbidden_structured(prompt, schema):
        raise AssertionError("确定性路径或模型缓存命中均不应再次调用 LLM")

    monkeypatch.setattr("stds.llm.pick_value.structured", forbidden_structured)

    async def scenario():
        async def choose_heavy():
            return candidates[1], 0.7, "模型首次判断为重档"

        await pool.resolve(identity, candidates, choose_heavy)
        by_table = await pick_value(
            "人工A拿取Tray",
            candidates,
            numeric_context=table_context,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        by_explicit_fact = await pick_value(
            "人工A拿取重量0.1 kg的Tray",
            candidates,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        by_experience = await pick_value(
            "人工A拿取Tray",
            candidates,
            experience_hint="默认选择 0.23 kg",
            experience_context=experience_context,
            experience_source="STDS评估经验V1.2.xlsx",
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        cached_model = await pick_value(
            "人工B移动Tray",
            candidates,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        return by_table, by_explicit_fact, by_experience, cached_model

    by_table, by_explicit_fact, by_experience, cached_model = asyncio.run(
        scenario()
    )

    assert by_table[0] is candidates[0]
    assert by_table[2].startswith("part-weight:")
    assert by_explicit_fact[0] is candidates[0]
    assert by_explicit_fact[2] == "numeric:weight_kg=0.1"
    assert by_experience[0] is candidates[0]
    assert by_experience[2].startswith("experience-default:")
    assert cached_model[0] is candidates[1]
    assert "model-weight-pool:mapped-exact" in cached_model[2]


def test_cross_chart_weight_band_mapping_uses_near_then_safe_ceiling():
    pool = ModelWeightPool()
    identity = PartIdentityContext(
        part_name="水冷板",
        identity_key="extracted_name:水冷板",
        source="parent-operation-group",
        group_id="G1",
    )
    source_candidates = [
        _weight_option(0.9, 1),
        _weight_option(3.2, 2),
        _weight_option(6.8, 3),
    ]
    near_candidates = [_weight_option(3.15, 1), _weight_option(6.8, 2)]
    coarse_candidates = [
        _weight_option(0.9, 1),
        _weight_option(6.8, 2),
        _weight_option(11.3, 3),
    ]

    async def scenario():
        async def choose_3_2kg():
            return source_candidates[1], 0.7, "模型判断为3.2kg档"

        async def forbidden():
            raise AssertionError("跨 Chartcode 必须复用已缓存的物理重量档")

        selected = await pool.resolve(identity, source_candidates, choose_3_2kg)
        near = await pool.resolve(identity, near_candidates, forbidden)
        ceiling = await pool.resolve(identity, coarse_candidates, forbidden)
        return selected, near, ceiling

    selected, near, ceiling = asyncio.run(scenario())

    assert selected[0] is source_candidates[1]
    assert "band_kg=3.2" in selected[2]
    assert near[0] is near_candidates[0]
    assert "model-weight-pool:mapped-near" in near[2]
    assert "cached_band_kg=3.2" in near[2]
    assert ceiling[0] is coarse_candidates[1]
    assert "model-weight-pool:mapped-ceiling" in ceiling[2]
    assert "band_kg=6.8" in ceiling[2]


def test_real_chart_same_pound_band_beats_kg_rounding_difference():
    """数据库中的 1lb 有 0.45kg/0.4357kg 两种写法，仍须视为同档。"""

    charts = load_charts()
    source_candidates = charts["050 221"].candidates(3, 1)
    target_candidates = charts["050 03B"].candidates(3, 2)
    source_one_lb = next(
        candidate
        for candidate in source_candidates
        if candidate.description.strip().startswith("1 lb")
    )
    identity = PartIdentityContext(
        part_name="Tray",
        identity_key="extracted_name:tray",
    )
    pool = ModelWeightPool()

    async def scenario():
        async def choose_one_lb():
            return source_one_lb, 0.7, "模型选择 1lb"

        async def forbidden():
            raise AssertionError("同一磅档必须复用")

        await pool.resolve(identity, source_candidates, choose_one_lb)
        return await pool.resolve(identity, target_candidates, forbidden)

    mapped = asyncio.run(scenario())

    assert mapped[0].description.startswith("1 lb / .4357")
    assert "model-weight-pool:mapped-equivalent-lbs" in mapped[2]
    assert "cached_band_kg=0.45" in mapped[2]


def test_explicit_weight_is_found_when_description_also_contains_distance(
    monkeypatch,
):
    candidates = [
        _weight_option(0.9, 1),
        _weight_option(7.0, 2),
        _weight_option(11.25, 3),
    ]
    identity = PartIdentityContext(
        part_name="Tray",
        identity_key="extracted_name:tray",
    )

    async def forbidden_structured(prompt, schema):
        raise AssertionError("明示 7kg 必须先于模型重量池")

    monkeypatch.setattr("stds.llm.pick_value.structured", forbidden_structured)

    choice = asyncio.run(
        pick_value(
            "人工A移动1m后拿取重量7kg的Tray",
            candidates,
            part_identity_context=identity,
            model_weight_pool=ModelWeightPool(),
        )
    )

    assert choice[0] is candidates[1]
    assert choice[2] == "numeric:weight_kg=7.0"


def test_cached_weight_above_chart_max_is_marked_for_review(monkeypatch):
    chart = _weight_chart()
    identity = PartIdentityContext(
        part_name="重型Tray",
        identity_key="extracted_name:重型tray",
    )
    pool = ModelWeightPool()
    source_candidates = [_weight_option(7.0, 1), _weight_option(11.25, 2)]

    async def select_chartcode(operation, charts):
        return chart.chartcode

    async def forbidden_structured(prompt, schema):
        raise AssertionError("已有模型重量不得再次调用 LLM")

    monkeypatch.setattr("stds.llm.pick_value.structured", forbidden_structured)
    deps = Deps(
        charts={chart.chartcode: chart},
        cache=AutoCache(),
        llm_select_chartcode=select_chartcode,
        model_weight_pool=pool,
    )

    async def scenario():
        async def choose_7kg():
            return source_candidates[0], 0.7, "模型选择 7kg"

        await pool.resolve(identity, source_candidates, choose_7kg)
        return await resolve(
            _element(9, "人工A拿取重型Tray"),
            deps,
            machine_hint=False,
            part_identity_context=identity,
            part_context_resolved=True,
        )

    result = asyncio.run(scenario())

    assert result.decision == "0.45KGX"
    assert result.needs_review is True
    assert result.confidence == 0.65
    assert any(step[0] == "ModelWeightReview" for step in result.trace)
