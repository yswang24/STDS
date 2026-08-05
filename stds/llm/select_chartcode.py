"""LLM chartcode 选择:参考 Dify 的 LLM chartcode选择节点。

64 个 chartcode 中排除 2 个 EST 经验专用码，其余 62 个给 LLM 选。
common_chart 的 T0.5 已覆盖 33 条高频(中文关键词精确匹配),这里处理剩余的。
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel

from stds.domain.chartcode_policy import general_chart_candidates
from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.select_chartcode")


class ChartcodePick(BaseModel):
    Chartcode: str


async def select_chartcode(operation_des: str, charts: dict) -> Optional[str]:
    """从普通 Chartcode 中选最匹配的一个；保留码必须由明确经验命中。"""
    eligible_charts = general_chart_candidates(charts)
    lines = []
    for cc in sorted(eligible_charts.keys()):
        title = eligible_charts[cc].title or cc
        lines.append(f"- {cc}: {title}")
    chartcode_list = "\n".join(lines)

    logger.debug(
        "  [select_cc] LLM 选码: %r (%d 个候选)",
        operation_des,
        len(eligible_charts),
    )
    prompt = render_prompt("select_chartcode",
                           operation_des=operation_des,
                           chartcode_list=chartcode_list)
    try:
        out: ChartcodePick = await structured(prompt, ChartcodePick)
        cc = out.Chartcode.strip()
        logger.debug(f"  [select_cc] LLM 返回: '{cc}'")
        if cc in eligible_charts:
            return cc
        for valid_cc in eligible_charts:
            if cc in valid_cc or valid_cc in cc:
                logger.debug(f"  [select_cc] 模糊匹配: '{cc}' -> '{valid_cc}'")
                return valid_cc
        logger.warning(f"  [select_cc] LLM 返回的 chartcode 无效: '{cc}'")
        return None
    except Exception as e:
        logger.error(f"  [select_cc] LLM 调用失败: {e}")
        return None
