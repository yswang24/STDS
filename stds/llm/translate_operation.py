"""最终展示阶段的操作内容中文化，不改变参与工时分析的原始动作。"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, field_validator

from stds.llm.client import structured
from stds.llm.prompts import load_prompt

TRANSLATE_INPUT_PLACEHOLDER = "{{#operation#}}"
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_AUTO_SOURCE_PREFIX_RE = re.compile(r"^\s*auto\b[\s:：_-]*", re.IGNORECASE)
_AUTO_TRANSLATED_PREFIX_RE = re.compile(r"^\s*(?:设备\s*)?自动[\s:：_-]*")

OutputTranslator = Callable[[str], Awaitable[str]]
logger = logging.getLogger("stds.llm.translate_operation")


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


def normalize_auto_output_prefix(source: str, translated: str) -> str:
    """Auto 开头的设备动作在最终展示中强制以“自动”开头。"""
    cleaned = str(translated).strip()
    if not _AUTO_SOURCE_PREFIX_RE.search(str(source)):
        return cleaned

    translated_body = _AUTO_TRANSLATED_PREFIX_RE.sub("", cleaned, count=1)
    if translated_body == cleaned:
        translated_body = _AUTO_SOURCE_PREFIX_RE.sub("", cleaned, count=1)
    translated_body = translated_body.lstrip()
    separator = " " if translated_body[:1].isascii() and translated_body[:1].isalpha() else ""
    return f"自动{separator}{translated_body}"


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
    return normalize_auto_output_prefix(cleaned, out.translated_operation)


async def translate_operation_for_display(
    operation: str,
    *,
    translator: Optional[OutputTranslator] = None,
) -> str:
    """安全生成最终展示文本；失败保留原文，并继续规范 Auto 设备前缀。"""
    cleaned = str(operation).strip()
    fallback = normalize_auto_output_prefix(cleaned, cleaned)
    if not cleaned or not contains_latin_letters(cleaned):
        return fallback

    selected_translator = translator or translate_operation_for_output
    try:
        translated = str(await selected_translator(cleaned)).strip()
        if not translated:
            raise ValueError("翻译后的操作内容为空")
        return normalize_auto_output_prefix(cleaned, translated)
    except Exception:
        logger.warning(
            "Operation display translation failed; keeping original: operation=%r",
            cleaned,
            exc_info=True,
        )
        return fallback
