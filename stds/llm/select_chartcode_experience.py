"""Select one Chartcode-experience row from the complete valid experience pool.

The returned index always refers to the original ``candidates`` sequence.
Rows sharing a Chartcode deliberately remain separate because their operation
labels can express different actions.  Parameter-selection content is kept out
of this prompt; it belongs to the later parameter-experience selection stage.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Optional

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from stds.llm.client import structured
from stds.llm.prompts import render_prompt

logger = logging.getLogger("stds.llm.select_chartcode_experience")


class ChartcodeExperiencePick(BaseModel):
    """Strict structured output for selecting or rejecting an experience row."""

    model_config = ConfigDict(extra="forbid", strict=True)

    index: StrictInt
    reason: StrictStr


def _normalize_chartcode(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    return re.sub(r"\s+", "", text)


def _available_charts(charts: Mapping[object, object]) -> dict[str, object]:
    """Index available charts by normalized code without excluding EST codes."""
    available: dict[str, object] = {}
    for chartcode, chart in charts.items():
        normalized = _normalize_chartcode(chartcode)
        if normalized and normalized not in available:
            available[normalized] = chart
    return available


def _valid_candidates(
    candidates: Sequence[object],
    charts: Mapping[object, object],
) -> list[tuple[int, object, object]]:
    """Keep valid rows and their original indexes; never deduplicate Chartcodes."""
    available = _available_charts(charts)
    valid: list[tuple[int, object, object]] = []
    for index, candidate in enumerate(candidates):
        normalized = _normalize_chartcode(getattr(candidate, "chartcode", ""))
        chart = available.get(normalized)
        if chart is not None:
            valid.append((index, candidate, chart))
    return valid


def build_chartcode_experience_prompt(
    operation_des: str,
    indexed_candidates: Sequence[tuple[int, object, object]],
) -> str:
    """Render every valid experience row while omitting parameter-only fields."""
    payload = []
    for original_index, candidate, chart in indexed_candidates:
        payload.append({
            "index": original_index,
            "experience_id": str(
                getattr(candidate, "experience_id", "") or ""
            ),
            "operation_label": str(
                getattr(candidate, "operation_label", "") or ""
            ),
            "chartcode": str(getattr(candidate, "chartcode", "") or ""),
            "chart_title": str(getattr(chart, "title", "") or ""),
            "chart_row": getattr(candidate, "chart_row", None),
        })

    return render_prompt(
        "select_chartcode_experience",
        operation_des=str(operation_des or "").strip(),
        candidate_list=json.dumps(payload, ensure_ascii=False, indent=2),
    )


async def select_chartcode_experience(
    operation_des: str,
    candidates: Sequence[object],
    charts: Mapping[object, object],
) -> tuple[Optional[int], str]:
    """Ask the LLM to select one complete Chartcode-experience row.

    ``-1`` is the model's explicit no-match answer.  Any other invalid value,
    malformed response, or LLM failure also produces ``None``; this function
    never guesses the first candidate, even when only one valid row remains.
    """
    try:
        candidate_sequence = tuple(() if candidates is None else candidates)
        chart_mapping = {} if charts is None else charts
        indexed_candidates = _valid_candidates(candidate_sequence, chart_mapping)
    except Exception:
        logger.warning(
            "Chartcode-experience candidates could not be inspected",
            exc_info=True,
        )
        return None, "Chartcode 经验候选无效"

    if not indexed_candidates:
        return None, "没有可用的 Chartcode 经验候选"

    valid_indexes = {index for index, _candidate, _chart in indexed_candidates}
    try:
        prompt = build_chartcode_experience_prompt(
            operation_des,
            indexed_candidates,
        )
        out: ChartcodeExperiencePick = await structured(
            prompt,
            ChartcodeExperiencePick,
        )
    except Exception:
        logger.warning(
            "LLM Chartcode-experience selection failed: operation=%r",
            operation_des,
            exc_info=True,
        )
        return None, "LLM Chartcode 经验选择失败"

    selected_index = getattr(out, "index", None)
    reason = getattr(out, "reason", None)
    # ``bool`` is an ``int`` subclass; exact type checks preserve strictness
    # even when a test double bypasses Pydantic validation.
    if type(selected_index) is not int or type(reason) is not str:
        logger.warning(
            "LLM returned invalid Chartcode-experience types: index=%r reason=%r",
            selected_index,
            reason,
        )
        return None, "LLM 返回的 Chartcode 经验结果类型无效"

    clean_reason = reason.strip()
    if selected_index == -1:
        return None, clean_reason or "LLM 判定没有匹配的 Chartcode 经验"

    if selected_index not in valid_indexes:
        logger.warning(
            "LLM returned invalid Chartcode-experience index: index=%r valid=%s",
            selected_index,
            sorted(valid_indexes),
        )
        return None, "LLM 返回的 Chartcode 经验索引无效"

    return selected_index, clean_reason or "LLM 选择了最匹配的 Chartcode 经验"
