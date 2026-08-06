"""上传经验文件中 Common_Chart 的确定性关键词匹配。"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from stds.experience.index import normalize_operation
from stds.experience.models import CommonChartEntry, CommonChartMatch

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_common_keyword(value: object) -> str:
    """返回可用关键词键；纯中文至少 2 字，其他形式至少 3 字符。"""
    normalized = normalize_operation(value)
    if not normalized:
        return ""
    cjk_count = len(_CJK_RE.findall(normalized))
    if cjk_count == len(normalized):
        return normalized if cjk_count >= 2 else ""
    return normalized if len(normalized) >= 3 else ""


def match_common_chart(
    operation_des: str,
    entries: Sequence[CommonChartEntry],
) -> Optional[CommonChartMatch]:
    """命中 Common_Chart。

    先在全部候选中找输入与关键词完全相等的记录；没有精确项时，才找
    ``keyword in input`` 的包含项。包含匹配优先最长关键词。同一优先级下
    若结果不等价则视为歧义并回退（返回 ``None``）；等价结果固定选择
    Excel 行号最小、关键词位置最靠前的一项。
    """
    match, _ = _match_common_chart_keywords(operation_des, entries)
    return match


def _match_common_chart_keywords(
    operation_des: str,
    entries: Sequence[CommonChartEntry],
) -> tuple[Optional[CommonChartMatch], bool]:
    """返回关键词结果和“是否出现过词法候选”。

    第二个返回值用于区分“没有候选”和“候选输出冲突”；整体索引只有在
    语义 Top1 未达阈值时才调用本关键词回退器。
    """
    normalized_input = normalize_operation(operation_des)
    if not normalized_input:
        return None, False

    candidates: list[tuple[CommonChartEntry, str, str, int]] = []
    for entry in entries:
        for keyword_index, (keyword, normalized_keyword) in enumerate(zip(
            entry.keywords,
            entry.normalized_keywords,
        )):
            if not normalized_keyword:
                continue
            candidates.append((
                entry,
                keyword,
                normalized_keyword,
                keyword_index,
            ))

    exact = [
        candidate
        for candidate in candidates
        if candidate[2] == normalized_input
    ]
    if exact:
        eligible = exact
        match_type = "exact"
    else:
        contained = [
            candidate
            for candidate in candidates
            if candidate[2] in normalized_input
        ]
        if not contained:
            return None, False
        longest = max(len(candidate[2]) for candidate in contained)
        eligible = [
            candidate
            for candidate in contained
            if len(candidate[2]) == longest
        ]
        match_type = "contains"

    signatures = {
        candidate[0].output_signature
        for candidate in eligible
    }
    if len(signatures) != 1:
        return None, True

    entry, keyword, _, _ = min(
        eligible,
        key=lambda candidate: (candidate[0].row, candidate[3]),
    )
    return (
        CommonChartMatch(
            entry=entry,
            keyword=keyword,
            match_type=match_type,
            similarity=1.0,
        ),
        True,
    )


__all__ = ["match_common_chart", "normalize_common_keyword"]
