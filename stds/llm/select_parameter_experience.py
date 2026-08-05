"""Select one parameter-experience record for an LLM-selected Chartcode.

The returned index always refers to the original ``candidates`` sequence.  The
selector deliberately chooses a whole experience record once; downstream
variable traversal must reuse that record instead of mixing hints from several
records that happen to share a Chartcode.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, StrictInt

from stds.experience.models import ExperienceContext
from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.select_parameter_experience")


class ParameterExperiencePick(BaseModel):
    """Strict structured output for selecting one complete experience row."""

    model_config = ConfigDict(extra="forbid")

    index: StrictInt
    reason: str


def _normalize_chartcode(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return re.sub(r"\s+", "", text)


def _matching_candidates(
    chartcode: str,
    candidates: Sequence[ExperienceContext],
) -> list[tuple[int, ExperienceContext]]:
    """Keep the original indexes while enforcing the final Chartcode boundary."""
    selected_key = _normalize_chartcode(chartcode)
    if not selected_key:
        return []
    return [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if _normalize_chartcode(getattr(candidate, "chartcode", "")) == selected_key
    ]


def build_parameter_experience_prompt(
    operation_des: str,
    chartcode: str,
    indexed_candidates: Sequence[tuple[int, ExperienceContext]],
) -> str:
    """Render every field needed to compare complete parameter experiences."""
    payload = []
    for original_index, candidate in indexed_candidates:
        hints = getattr(candidate, "variable_hints", {}) or {}
        payload.append({
            "index": original_index,
            "experience_id": str(getattr(candidate, "experience_id", "") or ""),
            "operation_label": str(
                getattr(candidate, "operation_label", "") or ""
            ),
            "chartcode": str(getattr(candidate, "chartcode", "") or ""),
            "parameter_row": getattr(candidate, "parameter_row", None),
            "parameter_text": str(
                getattr(candidate, "parameter_text", "") or ""
            ),
            "variable_hints": {
                str(variable): str(hint)
                for variable, hint in sorted(
                    hints.items(), key=lambda item: str(item[0])
                )
            },
        })

    return render_prompt(
        "select_parameter_experience",
        operation_des=str(operation_des or "").strip(),
        chartcode=str(chartcode or "").strip(),
        candidate_list=json.dumps(payload, ensure_ascii=False, indent=2),
    )


async def select_parameter_experience(
    operation_des: str,
    chartcode: str,
    candidates: Sequence[ExperienceContext],
) -> tuple[Optional[int], str]:
    """Choose one complete parameter-experience record for ``chartcode``.

    No candidate is guessed on malformed output or an LLM failure.  In
    particular, an out-of-range index is never clamped and the first candidate
    is never used as an error fallback.
    """
    try:
        indexed_candidates = _matching_candidates(chartcode, tuple(candidates or ()))
    except Exception:
        logger.warning(
            "Parameter-experience candidates could not be inspected: chartcode=%r",
            chartcode,
            exc_info=True,
        )
        return None, "参数经验候选无效"

    if not indexed_candidates:
        return None, "没有该 Chartcode 的参数经验候选"

    if len(indexed_candidates) == 1:
        return indexed_candidates[0][0], "唯一参数经验候选，无需调用 LLM"

    valid_indexes = {index for index, _candidate in indexed_candidates}
    try:
        prompt = build_parameter_experience_prompt(
            operation_des,
            chartcode,
            indexed_candidates,
        )
        out: ParameterExperiencePick = await structured(
            prompt,
            ParameterExperiencePick,
        )
    except Exception:
        logger.warning(
            "LLM parameter-experience selection failed: chartcode=%r operation=%r",
            chartcode,
            operation_des,
            exc_info=True,
        )
        return None, "LLM 参数经验选择失败"

    selected_index = getattr(out, "index", None)
    # ``bool`` is an ``int`` subclass; reject it explicitly as invalid output.
    if type(selected_index) is not int or selected_index not in valid_indexes:
        logger.warning(
            "LLM returned invalid parameter-experience index: index=%r valid=%s",
            selected_index,
            sorted(valid_indexes),
        )
        return None, "LLM 返回的参数经验索引无效"

    reason = str(getattr(out, "reason", "") or "").strip()
    return selected_index, reason or "LLM 选择了最匹配的参数经验"
