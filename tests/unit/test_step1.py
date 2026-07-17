"""Step 1 验收:数据模型实例化与基本行为。"""
from __future__ import annotations

import pytest

from stds.domain.models import (
    MostChart,
    Source,
    StdsElement,
    StdsResult,
    ValueOption,
)


def test_value_option_frozen():
    o = ValueOption(1, 1, 1, "desc", "AB,", 1.0, 2, 1)
    assert o.formula_value == 1.0
    assert o.metric_abbrev == "AB,"
    with pytest.raises(AttributeError):  # frozen 不可改
        o.formula_value = 2.0


def test_most_chart_candidates_empty():
    c = MostChart("cc", "t", "V1", False, False, {})
    assert c.candidates(1, 1) == []


def test_most_chart_candidates_keyed():
    o = ValueOption(1, 1, 1, "d", "A,", 1.0, 0, 0)
    c = MostChart("cc", "t", "V1", False, False, {(1, 1): [o]})
    assert c.candidates(1, 1) == [o]
    assert c.candidates(2, 1) == []  # ★ 双键:不同 range 不串


def test_stds_result_defaults():
    el = StdsElement(1, "op", "L", "S")
    r = StdsResult(el, "020 02A", "D", 1.5, "C", 1.0, Source.FORMULA, 0.9, False)
    assert r.trace == [] and r.edited is False


def test_stds_result_helpers():
    el = StdsElement(1, "op", "L", "S")
    assert StdsResult.machine_placeholder(el).source == Source.MACHINE
    assert StdsResult.unresolved(el, "EST C00").needs_review is True
