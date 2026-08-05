from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from stds.cascade.numeric import NumericContext
from stds.domain.models import ValueOption
from stds.llm.pick_value import pick_value
from stds.retrieval.model_weight_pool import (
    ModelWeightPool,
    model_weight_identity_key,
)


@dataclass(frozen=True)
class _Identity:
    identity_key: str = ""
    part_name: str = ""
    group_id: str = ""
    source: str = ""


def _option(weight: float, *, value_number: int = 1) -> ValueOption:
    return ValueOption(
        variable_number=1,
        range_number=1,
        value_number=value_number,
        description=f"{weight:g} kg",
        metric_abbrev=f"{weight:g}KGX,",
        formula_value=weight,
        next_variable=0,
        next_range=0,
    )


def test_identity_is_normalized_and_prefers_explicit_key():
    context = _Identity(
        identity_key="  Water-Cooling_Plate ",
        part_name="另一个名称",
        group_id="G1",
    )

    assert model_weight_identity_key(context) == "water-cooling_plate"
    assert model_weight_identity_key(_Identity(part_name="Ｔｒａｙ")) == "tray"
    assert model_weight_identity_key(_Identity(group_id="父工序-12")) == "父工序-12"
    assert model_weight_identity_key(_Identity(part_name="P-1")) != (
        model_weight_identity_key(_Identity(part_name="P1"))
    )
    assert model_weight_identity_key(_Identity(part_name="A/B")) != (
        model_weight_identity_key(_Identity(part_name="AB"))
    )


def test_same_part_reuses_physical_band_and_maps_other_candidate_sets():
    pool = ModelWeightPool()
    first_candidates = [_option(0.23), _option(0.45, value_number=2)]
    mapped_candidates = [_option(0.2), _option(0.7, value_number=2)]
    calls = 0

    async def chooser():
        nonlocal calls
        calls += 1
        return first_candidates[1], 0.7, "first model choice"

    async def should_not_run():
        raise AssertionError("same part must reuse its first physical band")

    async def scenario():
        first = await pool.resolve(
            _Identity(part_name="Tray", group_id="P1", source="llm/V2"),
            first_candidates,
            chooser,
        )
        mapped = await pool.resolve(
            _Identity(part_name="ＴＲＡＹ", group_id="P9", source="llm/V3"),
            mapped_candidates,
            should_not_run,
        )
        return first, mapped

    first, mapped = asyncio.run(scenario())

    assert calls == 1
    assert first[0] is first_candidates[1]
    assert "model-weight-pool:selected" in first[2]
    assert mapped[0] is mapped_candidates[1]
    assert "model-weight-pool:mapped-ceiling" in mapped[2]
    assert "cached_band_kg=0.45" in mapped[2]
    assert "source=llm/V2" in mapped[2]


def test_mapping_prefers_exact_band_and_clamps_outside_current_range():
    pool = ModelWeightPool()
    original = [_option(0.45), _option(7.0, value_number=2)]
    calls = 0

    async def chooser():
        nonlocal calls
        calls += 1
        return original[1], 0.7, "heavy"

    async def should_not_run():
        raise AssertionError("cached identity must not invoke chooser")

    async def scenario():
        await pool.resolve(_Identity(part_name="水冷板"), original, chooser)
        exact_candidates = [_option(7.0), _option(10.0, value_number=2)]
        exact = await pool.resolve(
            _Identity(part_name="主水冷板" , identity_key="水冷板"),
            exact_candidates,
            should_not_run,
        )
        boundary_candidates = [_option(0.23), _option(0.45, value_number=2)]
        clamped = await pool.resolve(
            _Identity(part_name="水冷板"),
            boundary_candidates,
            should_not_run,
        )
        return exact, exact_candidates, clamped, boundary_candidates

    exact, exact_candidates, clamped, boundary_candidates = asyncio.run(scenario())

    assert calls == 1
    assert exact[0] is exact_candidates[0]
    assert "model-weight-pool:mapped-exact" in exact[2]
    assert clamped[0] is boundary_candidates[1]
    assert "model-weight-pool:clamped-high-review" in clamped[2]
    assert clamped[1] == 0.55


def test_same_identity_is_singleflight_but_different_identities_are_isolated():
    pool = ModelWeightPool()
    candidates = [_option(0.23), _option(0.45, value_number=2)]
    release = asyncio.Event()
    same_calls = 0
    other_calls = 0

    async def same_chooser():
        nonlocal same_calls
        same_calls += 1
        await release.wait()
        return candidates[1], 0.7, "same"

    async def other_chooser():
        nonlocal other_calls
        other_calls += 1
        return candidates[0], 0.7, "other"

    async def scenario():
        tasks = [
            asyncio.create_task(
                pool.resolve(_Identity(part_name="Tray"), candidates, same_chooser)
            )
            for _ in range(12)
        ]
        await asyncio.sleep(0)
        other = await pool.resolve(
            _Identity(part_name="螺栓"),
            candidates,
            other_chooser,
        )
        release.set()
        same = await asyncio.gather(*tasks)
        return same, other

    same, other = asyncio.run(scenario())

    assert same_calls == 1
    assert other_calls == 1
    assert all(result[0] is candidates[1] for result in same)
    assert other[0] is candidates[0]


def test_exception_and_cancellation_are_not_cached():
    candidates = [_option(0.23), _option(0.45, value_number=2)]

    async def scenario():
        pool = ModelWeightPool()
        exception_calls = 0

        async def exception_chooser():
            nonlocal exception_calls
            exception_calls += 1
            if exception_calls == 1:
                raise RuntimeError("temporary")
            return candidates[1], 0.7, "retried"

        with pytest.raises(RuntimeError, match="temporary"):
            await pool.resolve(
                _Identity(part_name="Tray"), candidates, exception_chooser
            )
        retried_exception = await pool.resolve(
            _Identity(part_name="Tray"), candidates, exception_chooser
        )

        cancel_started = asyncio.Event()
        cancellation_calls = 0

        async def cancellation_chooser():
            nonlocal cancellation_calls
            cancellation_calls += 1
            if cancellation_calls == 1:
                cancel_started.set()
                await asyncio.Event().wait()
            return candidates[0], 0.7, "retried cancellation"

        task = asyncio.create_task(
            pool.resolve(
                _Identity(part_name="螺栓"), candidates, cancellation_chooser
            )
        )
        await cancel_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        retried_cancellation = await pool.resolve(
            _Identity(part_name="螺栓"), candidates, cancellation_chooser
        )
        return (
            exception_calls,
            retried_exception,
            cancellation_calls,
            retried_cancellation,
        )

    (
        exception_calls,
        retried_exception,
        cancellation_calls,
        retried_cancellation,
    ) = asyncio.run(scenario())

    assert exception_calls == 2
    assert retried_exception[0] is candidates[1]
    assert cancellation_calls == 2
    assert retried_cancellation[0] is candidates[0]


def test_incomplete_weight_candidates_and_missing_identity_do_not_cache():
    pool = ModelWeightPool()
    incomplete = [
        _option(0.23),
        ValueOption(1, 1, 2, "heavy", "HEAVY,", 2.0, 0, 0),
    ]
    calls = 0

    async def chooser():
        nonlocal calls
        calls += 1
        return incomplete[0], 0.7, "ordinary llm"

    async def scenario():
        await pool.resolve(_Identity(part_name="Tray"), incomplete, chooser)
        await pool.resolve(_Identity(part_name="Tray"), incomplete, chooser)
        await pool.resolve(_Identity(), [_option(0.23), _option(0.45)], chooser)

    asyncio.run(scenario())

    assert calls == 3


def test_pick_value_pool_runs_only_after_deterministic_weight_lookup(monkeypatch):
    pool = ModelWeightPool()
    candidates = [_option(0.23), _option(0.45, value_number=2)]
    llm_calls = 0

    async def fake_structured(prompt, schema):
        nonlocal llm_calls
        llm_calls += 1
        return schema(index=1, reason="model inferred heavy")

    monkeypatch.setattr("stds.llm.pick_value.structured", fake_structured)
    table_context = NumericContext(
        weight_kg=0.1,
        query_name="Tray",
        matched_name="Tray",
        similarity=1.0,
        match_type="exact",
        source="Sheet1!E2",
    )
    identity = _Identity(part_name="Tray", source="model/V2")

    async def scenario():
        deterministic = await pick_value(
            "拿取Tray",
            candidates,
            numeric_context=table_context,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        first_model = await pick_value(
            "拿取Tray",
            candidates,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        reused = await pick_value(
            "移动Tray",
            candidates,
            part_identity_context=identity,
            model_weight_pool=pool,
        )
        return deterministic, first_model, reused

    deterministic, first_model, reused = asyncio.run(scenario())

    assert deterministic[0] is candidates[0]
    assert first_model[0] is candidates[1]
    assert reused[0] is candidates[1]
    assert llm_calls == 1
    assert "model-weight-pool:selected" in first_model[2]
    assert "model-weight-pool:mapped-exact" in reused[2]
