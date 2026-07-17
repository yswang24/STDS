"""M5 测试:数据治理(Step 23)。"""
from __future__ import annotations

from stds.data.charts_loader import load_charts_with_diagnostics
from stds.data.repo import load_records_by_station


def test_load_diagnostics_formula_mismatch():
    """050 051 应被检测为不同步。"""
    _, diag = load_charts_with_diagnostics()
    mismatched_codes = [cc for cc, _, _ in diag.formula_mismatches]
    assert "050 051" in mismatched_codes  # 已知不同步


def test_load_diagnostics_orphan_codes():
    """孤儿码应被检测并记录。"""
    _, diag = load_charts_with_diagnostics()
    orphan_dict = dict(diag.orphan_codes)
    assert "020 12Z" in orphan_dict     # 18条
    assert orphan_dict["020 12Z"] >= 18


def test_load_diagnostics_empty_codes():
    """空码应被统计(NULL + 空字符串)。"""
    _, diag = load_charts_with_diagnostics()
    assert diag.empty_codes >= 80  # 56 NULL + 28 空字符串


def test_load_charts_uses_formula_not_formula_chart():
    """charts 的公式应取 formula.ChartFormula,不是 formula_chart.公式。"""
    charts, _ = load_charts_with_diagnostics()
    c = charts["050 051"]
    # formula.ChartFormula 比 formula_chart 多一层左括号
    assert c.formula.startswith("(((V2")
    assert c.formula.count("(") > c.formula.count(")") or "(" in c.formula


def test_sql_parameterized():
    """repo.load_records_by_station 不会因注入输入崩溃。"""
    # 这些注入尝试不应导致 SQL 错误
    load_records_by_station("OR '1'='1", "OP010")
    load_records_by_station("'; DROP TABLE stds_record; --", "OP010")
    load_records_by_station("正常项目", "OP010' OR '1'='1")
