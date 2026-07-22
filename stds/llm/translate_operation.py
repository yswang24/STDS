"""最终展示阶段的操作内容中文化，不改变参与工时分析的原始动作。"""
from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

from stds.llm.client import structured
from stds.llm.prompts import load_prompt

TRANSLATE_INPUT_PLACEHOLDER = "{{#operation#}}"
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


class TranslateOperationOut(BaseModel):
    translated_operation: str

    @field_validator("translated_operation")
    @classmethod
    def validate_translated_operation(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            raise ValueError("翻译后的操作内容不能为空")
        return cleaned


def contains_latin_letters(operation: str) -> bool:
    """仅含中文、数字或符号的内容无需调用 LLM。"""
    return _LATIN_LETTER_RE.search(str(operation)) is not None


def build_translate_operation_prompt(operation: str) -> str:
    template = load_prompt("translate_operation")
    if TRANSLATE_INPUT_PLACEHOLDER not in template:
        raise ValueError("操作内容翻译 Prompt 缺少 operation 占位符")
    return template.replace(TRANSLATE_INPUT_PLACEHOLDER, str(operation).strip())


async def translate_operation_for_output(operation: str) -> str:
    """将普通英文描述转为中文，专业零件名等由 Prompt 要求原样保留。"""
    cleaned = str(operation).strip()
    if not cleaned or not contains_latin_letters(cleaned):
        return cleaned
    out: TranslateOperationOut = await structured(
        build_translate_operation_prompt(cleaned),
        TranslateOperationOut,
    )
    return out.translated_operation
