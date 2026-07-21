"""将 Dify 的动作拆解节点原样接入本地 LLM 客户端。"""
from __future__ import annotations

import logging

from pydantic import BaseModel, field_validator

from stds.llm.client import structured_system
from stds.llm.prompts import load_prompt

logger = logging.getLogger("stds.llm.decompose")

DIFY_INPUT_PLACEHOLDER = "{{#1768378483078.operation_des#}}"


class DecomposeOut(BaseModel):
    operation: list[str]

    @field_validator("operation")
    @classmethod
    def validate_operations(cls, values: list[str]) -> list[str]:
        cleaned = [str(value).strip() for value in values if str(value).strip()]
        if not cleaned:
            raise ValueError("拆解结果 operation 不能为空")
        return cleaned


def build_decompose_prompt(operation_des: str) -> str:
    """只替换 Dify 变量，其余字符保持导出工作流原样。"""
    template = load_prompt("decompose_operation")
    if DIFY_INPUT_PLACEHOLDER not in template:
        raise ValueError("Dify 拆解 Prompt 缺少 operation_des 占位符")
    return template.replace(DIFY_INPUT_PLACEHOLDER, operation_des)


async def decompose_operation(operation_des: str) -> list[str]:
    logger.debug("[decompose] 开始拆解: %r", operation_des)
    out: DecomposeOut = await structured_system(
        build_decompose_prompt(operation_des),
        DecomposeOut,
    )
    logger.debug("[decompose] 拆解完成: %r", out.operation)
    return out.operation
