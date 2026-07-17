"""数值确定性抽取:从操作描述中提取数值,取决策树最近档。不应问 LLM。"""
from __future__ import annotations

import re


_PATTERNS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(m|米)(?![a-z])", re.I), "distance_m"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(in|英寸)", re.I),       "distance_in"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(kg|公斤|千克)", re.I),  "weight_kg"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(lbs|磅)", re.I),        "weight_lbs"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(rpm|转)", re.I),        "rpm"),
]


def parse_numeric(text: str):
    """返回 (kind, value) 或 None。"""
    for pat, kind in _PATTERNS:
        m = pat.search(text)
        if m:
            return (kind, float(m.group(1)))
    return None


def nearest_range(value: float, candidates: list) -> object:
    """数值型变量:取 formula_value 最接近的档。"""
    return min(candidates, key=lambda c: abs(c.formula_value - value))
