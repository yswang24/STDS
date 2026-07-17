"""Step 2 验收:加载 MOST 内存图,核对跳转/range/fv=0 默认选项。"""
from __future__ import annotations

from stds.data.charts_loader import load_charts


def test_load_charts_count():
    charts = load_charts()
    assert len(charts) == 62


def test_020_02a_jump_and_terminator():
    charts = load_charts()
    c = charts["020 02A"]
    assert c.value_added is True                          # C
    assert c.formula.startswith("(0.000333333")
    # V1 选 PSF -> next_variable=3(跳过 V2)
    v1 = c.candidates(1, 1)
    psf = next(o for o in v1 if o.metric_abbrev and "PSF" in o.metric_abbrev)
    assert psf.next_variable == 3
    # V5 选 NIBU -> next_variable=0(终点), fv=0
    v5 = c.candidates(5, 1)
    nibu = next(o for o in v5 if o.metric_abbrev and "NIBU" in o.metric_abbrev)
    assert nibu.next_variable == 0
    assert nibu.formula_value == 0.0


def test_050_221_range_dual_key():
    charts = load_charts()
    c2 = charts["050 221"]
    # 变量2 有 RangeNumber=1 和 =2 两组(range 收窄 bug 的证据)
    assert (2, 1) in c2.options and (2, 2) in c2.options


def test_060_010_has_zero_default():
    charts = load_charts()
    c3 = charts["060 010"]
    v2 = c3.candidates(2, 1)
    # V2 存在 fv=0 的 "No Place Scanner" 选项(未匹配变量的默认值)
    assert any(o.formula_value == 0.0 for o in v2)
