"""决策树遍历(正向):用一个循环取代 Dify 的 V1..V12 十二段节点链。

range 双键收窄候选,LLM 只在 pick_value 内部被调用(且仅在候选>1且非数值时)。
"""
from __future__ import annotations

import inspect
from typing import Optional

from stds.cascade.numeric import NumericContext, PartIdentityContext
from stds.domain.models import MostChart
from stds.engine.formula import EngineError


def _supported_picker_kwargs(pick_value, kwargs: dict) -> dict:
    """只传 picker 明确支持的关键字，兼容既有两参数测试替身。"""
    try:
        parameters = inspect.signature(pick_value).parameters
    except (TypeError, ValueError):
        return {}
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return kwargs
    return {name: value for name, value in kwargs.items() if name in parameters}


def _variable_experience_hint(experience_context, variable_number: int):
    """只读取当前动作身份、当前 Vn 的提示，禁止按 Chartcode 横向找经验。"""
    if experience_context is None:
        return None
    hints = getattr(experience_context, "variable_hints", None)
    if not isinstance(hints, dict):
        return None
    for key in (variable_number, str(variable_number), f"V{variable_number}"):
        hint = hints.get(key)
        if hint is not None and str(hint).strip():
            return str(hint).strip()
    return None


def _experience_provenance(
    experience_context,
    experience_source: str,
) -> str:
    return (
        f"experience_id={getattr(experience_context, 'experience_id', '')};"
        f"operation={getattr(experience_context, 'operation_label', '')};"
        f"source={experience_source or 'uploaded-experience'};"
        f"parameter_row={getattr(experience_context, 'parameter_row', '')}"
    )


async def traverse(
    chart: MostChart,
    operation_des: str,
    pick_value,
    *,
    numeric_context: Optional[NumericContext] = None,
    part_identity_context: Optional[PartIdentityContext] = None,
    model_weight_pool=None,
    experience_context=None,
    experience_source: str = "",
):
    """
    pick_value: async (op_des, candidates) -> (ValueOption, conf, reason)
    返回 (values: dict[int,float], abbrevs: list[str], trace: list[tuple])
    """
    values: dict = {}
    abbrevs: list = []
    trace: list = []
    var, rng, visited = 1, 1, set()
    while var != 0:
        if (var, rng) in visited:
            raise EngineError(f"cycle {chart.chartcode} V{var}R{rng}")
        visited.add((var, rng))
        cands = chart.candidates(var, rng)        # ★ range 双键收窄
        if not cands:
            raise EngineError(f"no cands {chart.chartcode} V{var}R{rng}")
        experience_hint = _variable_experience_hint(experience_context, var)
        picker_kwargs = {}
        if numeric_context is not None:
            picker_kwargs["numeric_context"] = numeric_context
        if part_identity_context is not None and model_weight_pool is not None:
            picker_kwargs.update(
                part_identity_context=part_identity_context,
                model_weight_pool=model_weight_pool,
            )
        if experience_hint is not None:
            picker_kwargs.update(
                experience_hint=experience_hint,
                experience_context=experience_context,
                experience_source=experience_source,
            )
        supported_kwargs = _supported_picker_kwargs(pick_value, picker_kwargs)
        if supported_kwargs:
            choice, conf, reason = await pick_value(
                operation_des,
                cands,
                **supported_kwargs,
            )
        else:
            choice, conf, reason = await pick_value(operation_des, cands)
        if not any(choice is candidate for candidate in cands):
            raise EngineError(
                f"picker returned external candidate "
                f"{chart.chartcode} V{var}R{rng}"
            )
        reason = str(reason)
        if experience_hint is not None and "experience_id=" not in reason:
            reason = (
                f"{reason};experience-hint-available:"
                f"{_experience_provenance(experience_context, experience_source)}"
            )
        values[var] = choice.formula_value
        abbrevs.append(choice.metric_abbrev or "")
        trace.append((f"V{var}", choice.description, reason))
        var, rng = choice.next_variable, choice.next_range or 1
    return values, abbrevs, trace
