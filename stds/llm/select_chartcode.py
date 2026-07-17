"""LLM chartcode 选择:参考 Dify 的 LLM chartcode选择节点。

62 个 chartcode(约 3200 字符)全给 LLM 选。
common_chart 的 T0.5 已覆盖 33 条高频(中文关键词精确匹配),这里处理剩余的。
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel

from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.select_chartcode")


class ChartcodePick(BaseModel):
    Chartcode: str


async def select_chartcode(operation_des: str, charts: dict) -> Optional[str]:
    """从 62 个 chartcode 里选最匹配的一个。返回 chartcode 或 None。"""
    lines = []
    for cc in sorted(charts.keys()):
        title = charts[cc].title or cc
        lines.append(f"- {cc}: {title}")
    chartcode_list = "\n".join(lines)

    logger.debug(f"  [select_cc] LLM 选码: '{operation_des}' (62 个候选)")
    prompt = render_prompt("select_chartcode",
                           operation_des=operation_des,
                           chartcode_list=chartcode_list)
    try:
        out: ChartcodePick = await structured(prompt, ChartcodePick)
        cc = out.Chartcode.strip()
        logger.debug(f"  [select_cc] LLM 返回: '{cc}'")
        if cc in charts:
            return cc
        for valid_cc in charts:
            if cc in valid_cc or valid_cc in cc:
                logger.debug(f"  [select_cc] 模糊匹配: '{cc}' -> '{valid_cc}'")
                return valid_cc
        logger.warning(f"  [select_cc] LLM 返回的 chartcode 无效: '{cc}'")
        return None
    except Exception as e:
        logger.error(f"  [select_cc] LLM 调用失败: {e}")
        return None
