"""数值确定性抽取:从操作描述中提取数值,取决策树最近档。不应问 LLM。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_PATTERNS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(cm|厘米)(?![a-z])", re.I), "distance_cm"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(m|米)(?![a-z])", re.I), "distance_m"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(in|英寸)", re.I),       "distance_in"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(kg|公斤|千克)", re.I),  "weight_kg"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(lbs|磅)", re.I),        "weight_lbs"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(rpm|转)", re.I),        "rpm"),
]
_WEIGHT_DESCRIPTION_PATTERN = re.compile(
    r"(?<![\d.])((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*"
    r"(?:kg|kgs|公斤|千克)(?![a-z])",
    re.I,
)
_WEIGHT_ABBREV_PATTERN = re.compile(
    r"(?<![\d.])((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*"
    r"KGS?X?(?![A-Z])",
    re.I,
)
_WEIGHT_LBS_PATTERN = re.compile(
    r"(?<![\d.])((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*"
    r"(?:lbs?|磅)(?![a-z])",
    re.I,
)
_DISTANCE_PATTERN = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(cm|厘米|m|米|in|英寸|ft|英尺)(?![a-z])",
    re.I,
)
_RPM_PATTERN = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:rpm|转/分|转每分钟)",
    re.I,
)


@dataclass(frozen=True)
class NumericContext:
    """由零件重量检索提供给决策树选值的上下文。"""

    weight_kg: float
    query_name: str
    matched_name: str
    similarity: float
    match_type: str
    source: str = ""
    group_id: str = ""


@dataclass(frozen=True)
class PartIdentityContext:
    """已识别的物理零件身份；不要求重量表已经提供单重。"""

    part_name: str
    identity_key: str
    source: str = ""
    group_id: str = ""


def parse_numeric(text: str):
    """返回 (kind, value) 或 None。"""
    values = parse_numerics(text)
    return values[0] if values else None


def parse_numerics(text: str) -> list[tuple[str, float]]:
    """返回描述中的全部工程数值，保持既有维度优先顺序。

    一个动作可能同时出现移动距离、物体重量和转速。选值节点应逐一尝试，
    直到找到与当前候选维度相符的事实，而不能因首个事实属于其他维度就
    错误回退到 LLM。
    """
    values: list[tuple[str, float]] = []
    for pat, kind in _PATTERNS:
        values.extend(
            (kind, float(match.group(1)))
            for match in pat.finditer(text)
        )
    return values


def nearest_range(value: float, candidates: list) -> object:
    """数值型变量:取 formula_value 最接近的档。"""
    return min(candidates, key=lambda c: abs(c.formula_value - value))


def _distance_m(value: float, unit: str) -> float:
    normalized = unit.casefold()
    if normalized in {"cm", "厘米"}:
        return value / 100.0
    if normalized in {"in", "英寸"}:
        return value * 0.0254
    if normalized in {"ft", "英尺"}:
        return value * 0.3048
    return value


def _candidate_distances_m(candidate: object) -> list[float]:
    text = (
        f"{getattr(candidate, 'description', '') or ''} "
        f"{getattr(candidate, 'metric_abbrev', '') or ''}"
    )
    return [
        _distance_m(float(match.group(1)), match.group(2))
        for match in _DISTANCE_PATTERN.finditer(text)
    ]


def select_numeric_range(
    kind: str,
    value: float,
    candidates: list,
) -> Optional[object]:
    """仅当整组候选确实属于该数值维度时，才应用明示数值。

    不能拿距离“5m”去比较分类变量的 formula_value；formula_value 可能是
    时间系数或动作指数，并不是候选显示的物理量。
    """
    if not candidates:
        return None
    if kind in {"distance_cm", "distance_m", "distance_in"}:
        unit = {
            "distance_cm": "cm",
            "distance_m": "m",
            "distance_in": "in",
        }[kind]
        target = _distance_m(value, unit)
        parsed = [
            _candidate_distances_m(candidate)
            for candidate in candidates
        ]
        if any(not values for values in parsed):
            return None
        return min(
            zip(candidates, parsed),
            key=lambda item: min(
                abs(candidate_value - target)
                for candidate_value in item[1]
            ),
        )[0]

    if kind in {"weight_kg", "weight_lbs"}:
        target_kg = value if kind == "weight_kg" else value * 0.45359237
        hit = select_weight_range(target_kg, candidates)
        return hit[0] if hit is not None else None

    if kind == "rpm":
        parsed_rpm = []
        for candidate in candidates:
            text = (
                f"{getattr(candidate, 'description', '') or ''} "
                f"{getattr(candidate, 'metric_abbrev', '') or ''}"
            )
            values = [
                float(match.group(1))
                for match in _RPM_PATTERN.finditer(text)
            ]
            parsed_rpm.append(values)
        if any(not values for values in parsed_rpm):
            return None
        return min(
            zip(candidates, parsed_rpm),
            key=lambda item: min(
                abs(candidate_value - value)
                for candidate_value in item[1]
            ),
        )[0]
    return None


def candidate_weight_kg(candidate: object) -> Optional[float]:
    """从候选的显示描述/度量缩写中解析真实公斤档位。"""
    description = str(getattr(candidate, "description", "") or "")
    metric_abbrev = str(getattr(candidate, "metric_abbrev", "") or "")
    for pattern, text in (
        (_WEIGHT_DESCRIPTION_PATTERN, description),
        (_WEIGHT_ABBREV_PATTERN, metric_abbrev),
    ):
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    return None


def candidate_weight_lbs(candidate: object) -> Optional[float]:
    """从候选描述中解析磅档，用于跨图表识别同一物理重量档。"""
    description = str(getattr(candidate, "description", "") or "")
    match = _WEIGHT_LBS_PATTERN.search(description)
    return float(match.group(1)) if match else None


def select_weight_range(
    weight_kg: float,
    candidates: list,
) -> Optional[tuple[object, float]]:
    """按单重选择第一个不小于它的公斤档；超过最大档时不自动选择。"""
    if weight_kg <= 0 or not candidates:
        return None
    parsed = [
        (candidate_weight_kg(candidate), candidate)
        for candidate in candidates
    ]
    # 只有整组候选都明确是公斤档时才应用重量增强，避免误选其他变量。
    if any(weight is None for weight, _ in parsed):
        return None
    for band_weight, candidate in sorted(parsed, key=lambda item: item[0]):
        if band_weight >= weight_kg:
            return candidate, float(band_weight)
    return None
