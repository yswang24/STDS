"""LLM 判人/设备:参考 Dify 的 LLM-自动/手动节点。"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.classify")


class ClassifyOut(BaseModel):
    auto: int  # 1=设备, 0=人工


async def classify_machine(operation_des: str) -> bool:
    """True=设备, False=人。"""
    logger.debug(f"  [classify] LLM 判人/设备: '{operation_des}'")
    prompt = render_prompt("classify_machine", operation_des=operation_des)
    out: ClassifyOut = await structured(prompt, ClassifyOut)
    result = out.auto == 1
    logger.debug(f"  [classify] 结果: {'设备' if result else '人工'} (auto={out.auto})")
    return result
