"""决策树遍历(正向):用一个循环取代 Dify 的 V1..V12 十二段节点链。

range 双键收窄候选,LLM 只在 pick_value 内部被调用(且仅在候选>1且非数值时)。
"""
from __future__ import annotations

from stds.domain.models import MostChart
from stds.engine.formula import EngineError


async def traverse(chart: MostChart, operation_des: str, pick_value):
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
        choice, conf, reason = await pick_value(operation_des, cands)
        values[var] = choice.formula_value
        abbrevs.append(choice.metric_abbrev or "")
        trace.append((f"V{var}", choice.description, reason))
        var, rng = choice.next_variable, choice.next_range or 1
    return values, abbrevs, trace
