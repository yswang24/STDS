"""LLM 分类型变量选值:只返回下标(index),数值/跳转指针全来自 DB。"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from stds.cascade import numeric
from stds.domain.models import ValueOption
from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.pick_value")


class ValuePick(BaseModel):
    index: int
    reason: str


async def pick_value(op_des: str, candidates: list) -> tuple:
    """(ValueOption, confidence, reason)。

    三分支:单候选免 LLM / 数值精确匹配 / LLM 选(含数值型)。
    """
    if len(candidates) == 1:
        logger.debug(f"  [pick] 单候选免LLM: {candidates[0].description}")
        return candidates[0], 1.0, "single-candidate"

    # 数值精确匹配(操作描述有明确数值,如"7m"、"18in")
    n = numeric.parse_numeric(op_des)
    if n:
        kind, val = n
        hit = numeric.nearest_range(val, candidates)
        logger.debug(f"  [pick] 数值确定性: {kind}={val} -> {hit.description} (fv={hit.formula_value})")
        return hit, 0.95, f"numeric:{kind}={val}"

    # LLM 选(所有类型,包括数值型)
    menu = "\n".join(f"[{i}] {c.description}" for i, c in enumerate(candidates))
    logger.debug(f"  [pick] LLM 选值, {len(candidates)} 个候选")
    prompt = render_prompt("pick_value", op=op_des, menu=menu)
    out: ValuePick = await structured(prompt, ValuePick)
    idx = min(max(out.index, 0), len(candidates) - 1)
    chosen = candidates[idx]
    logger.debug(f"  [pick] LLM 选择: [{idx}] {chosen.description} (fv={chosen.formula_value}, reason={out.reason})")
    return chosen, 0.7, out.reason
