"""Step 3 验收:ast 安全求值,含手工 golden(202 010=0.72, 060 010=1.2)。"""
from __future__ import annotations

import pytest

from stds.data.charts_loader import load_charts
from stds.domain.models import MostChart
from stds.engine.formula import EngineError, evaluate


def test_eval_basic():
    c = MostChart("T", "t", "V1+V2", False, False, {})
    assert evaluate(c, {1: 2.0, 2: 3.0}) == 5.0


def test_eval_unselected_var_is_zero():
    c = MostChart("T", "t", "V1+V2", False, False, {})
    assert evaluate(c, {1: 2.0}) == 2.0          # V2 缺失当 0


def test_eval_202_010():                          # 手工 golden 样例 C
    charts = load_charts()
    c = charts["202 010"]                          # (V2+V3)*60
    assert evaluate(c, {2: 0.012, 3: 0.0}) == 0.72


def test_eval_060_010():                          # 手工 golden 样例 D
    charts = load_charts()
    c = charts["060 010"]                          # (V1+V2)*60
    assert evaluate(c, {1: 0.02, 2: 0.0}) == 1.2


def test_eval_rejects_non_arith():
    c = MostChart("T", "t", "__import__('os').system('rm -rf /')", False, False, {})
    with pytest.raises(EngineError):
        evaluate(c, {})
