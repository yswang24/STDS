"""把 formula + formula_chart 表加载成内存决策图(dict[chartcode -> MostChart])。

连接键用 formula.Chartcode(完整码 "020 02A"),不用 formula.动作代码(后缀)。
公式统一取 formula.ChartFormula(同一图表所有行一致),lstrip '=' 清 Excel 残留。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from stds.data.db import get_conn
from stds.domain.models import MostChart, ValueOption

logger = logging.getLogger(__name__)


@dataclass
class LoadDiagnostics:
    """加载诊断:公式不同步 / 孤儿码 / 空码。"""
    formula_mismatches: list     # [(chartcode, fc_formula, f_formula), ...]
    orphan_codes: list           # [(动作代码, 条数), ...]
    empty_codes: int             # 空码(NULL+空字符串)条数


def load_charts() -> dict:
    """加载 62 个 MostChart。以 formula.ChartFormula 为准。"""
    charts, _ = load_charts_with_diagnostics()
    return charts


def load_charts_with_diagnostics() -> tuple:
    """返回 (charts, diagnostics)。"""
    con = get_conn()

    # 1. 加载 formula 行
    rows = con.execute(
        """
        SELECT Chartcode, ChartTitle, ChartFormula, ChartValueAdded,
               ChartDevelopedInSeconds, VariableNumber, RangeNumber, ValueNumber,
               ValueDescription, ValueMetricAbbrev, ValueFormulaValue,
               ValueNextVariable, ValueNextRange
        FROM formula
        WHERE Chartcode IS NOT NULL AND Chartcode != ''
        """
    ).fetchall()

    # 2. 检测公式不同步(formula_chart vs formula)
    mismatches = []
    fc_rows = con.execute("SELECT 动作代码, 公式 FROM formula_chart").fetchall()
    fc_formulas = {r["动作代码"]: (r["公式"] or "").strip().lstrip("=") for r in fc_rows}
    formula_formulas = {}
    for r in rows:
        cc = r["Chartcode"]
        if cc not in formula_formulas:
            formula_formulas[cc] = (r["ChartFormula"] or "").strip().lstrip("=")
    for cc in fc_formulas:
        if cc in formula_formulas and fc_formulas[cc] != formula_formulas[cc]:
            mismatches.append((cc, fc_formulas[cc][:80], formula_formulas[cc][:80]))
            logger.warning(f"公式不同步 {cc}: formula_chart vs formula,以 formula 为准")

    # 3. 孤儿码 + 空码审计
    orphan_rows = con.execute("""
        SELECT 动作代码, COUNT(*) c FROM stds_record
        WHERE 动作代码 IS NOT NULL AND 动作代码 != ''
          AND 动作代码 NOT IN (SELECT 动作代码 FROM formula_chart)
        GROUP BY 动作代码 ORDER BY c DESC
    """).fetchall()
    orphans = [(r["动作代码"], r["c"]) for r in orphan_rows]

    empty_count = con.execute("""
        SELECT COUNT(*) FROM stds_record
        WHERE 动作代码 IS NULL OR 动作代码 = ''
    """).fetchone()[0]

    con.close()

    # 4. 构建 charts(以 formula.ChartFormula 为准)
    grouped: dict = {}
    for r in rows:
        cc = r["Chartcode"]
        grouped.setdefault(cc, {"meta": r, "rows": []})["rows"].append(r)

    charts: dict = {}
    for cc, g in grouped.items():
        m = g["meta"]
        options: dict = {}
        for r in g["rows"]:
            key = (r["VariableNumber"], r["RangeNumber"])
            options.setdefault(key, []).append(
                ValueOption(
                    variable_number=r["VariableNumber"],
                    range_number=r["RangeNumber"],
                    value_number=r["ValueNumber"],
                    description=r["ValueDescription"] or "",
                    metric_abbrev=r["ValueMetricAbbrev"],
                    formula_value=float(r["ValueFormulaValue"] or 0),
                    next_variable=int(r["ValueNextVariable"] or 0),
                    next_range=int(r["ValueNextRange"] or 0),
                )
            )
        charts[cc] = MostChart(
            chartcode=cc,
            title=m["ChartTitle"] or "",
            formula=(m["ChartFormula"] or "").lstrip("=").strip(),
            value_added=bool(m["ChartValueAdded"]),
            developed_in_seconds=bool(m["ChartDevelopedInSeconds"]),
            options=options,
        )

    diag = LoadDiagnostics(
        formula_mismatches=mismatches,
        orphan_codes=orphans,
        empty_codes=empty_count,
    )
    return charts, diag
