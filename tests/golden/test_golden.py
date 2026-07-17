"""Step 6 验收:公式计算正确性验证(以 formula 为金标准,不比对历史时间)。

6a - ast 求值正确性(手算对照)
6b - 全 62 chartcode 可遍历性
6c - 手工 golden 集(decode + 求值精确,>=5 条)
6d - Dify 偏差差异报告(产出物,非验收卡点)
"""
from __future__ import annotations

import asyncio
import sqlite3

from stds.config.settings import settings
from stds.data.charts_loader import load_charts
from stds.domain.models import MostChart
from stds.engine.decision_codec import decode
from stds.engine.formula import EngineError, evaluate
from stds.engine.traverse import traverse

charts = load_charts()


def chart(cc: str) -> MostChart:
    return charts[cc]


# ---------- 6a: ast 求值正确性(手算对照) ----------


def test_6a_eval_basic():
    c = MostChart("T", "t", "V1+V2", False, False, {})
    assert evaluate(c, {1: 2.0, 2: 3.0}) == 5.0


def test_6a_eval_unselected_zero():
    c = MostChart("T", "t", "V1+V2", False, False, {})
    assert evaluate(c, {1: 2.0}) == 2.0   # V2 缺失当 0


def test_6a_eval_202_010():
    assert evaluate(chart("202 010"), {2: 0.012, 3: 0.0}) == 0.72


def test_6a_eval_060_010():
    assert evaluate(chart("060 010"), {1: 0.02, 2: 0.0}) == 1.2


def test_6a_eval_180_061():
    # (V1+V2+V3+V4-.01)*60 = (0+0.06+0+0-0.01)*60 = 3.0
    assert evaluate(chart("180 061"), {1: 0.0, 2: 0.06, 3: 0.0, 4: 0.0}) == 3.0


def test_6a_eval_060_050():
    # V1*60 = 0.06*60 = 3.6
    assert evaluate(chart("060 050"), {1: 0.06}) == 3.6


def test_6a_eval_050_05d():
    # (0.0067+(V1*0.0067))*V2*60 = (0.0067+0.0067)*1*60 = 0.804 -> 0.80
    assert evaluate(chart("050 05D"), {1: 1.0, 2: 1.0}) == 0.8


def test_6a_rejects_non_arith():
    c = MostChart("T", "t", "__import__('os').system('rm -rf /')", False, False, {})
    try:
        evaluate(c, {})
        assert False, "应抛 EngineError"
    except EngineError:
        pass


# ---------- 6b: 全 62 chartcode 可遍历性 ----------


def test_6b_all_charts_traversable():
    async def pick(op, cands):
        return cands[0], 1.0, "t"
    errors = []
    for cc, c in charts.items():
        try:
            asyncio.run(traverse(c, "t", pick))
        except Exception as e:
            errors.append(f"{cc}: {e}")
    assert not errors, f"不可遍历的 chart: {errors[:5]}"


# ---------- 6c: 手工 golden 集(decode + 求值精确) ----------

GOLDEN = [
    # (chartcode, 决策描述, 期望V值 dict, 期望单次时间秒)
    ("202 010", "T,90,NB",            {1: 0.0, 2: 0.012, 3: 0.0},                    0.72),
    ("060 010", "LS",                  {1: 0.02, 2: 0.0},                             1.2),
    ("180 061", "PO,3IP,0HT,0B",      {1: 0.0, 2: 0.06, 3: 0.0, 4: 0.0},            3.0),
    ("060 050", "PL",                  {1: 0.06},                                     3.6),
    ("050 05D", "1PM,",               {1: 1.0, 2: 1.0},                              0.8),
    # agent 补充更多样例(可选):050 221, 050 222, 052 040 等
]


def test_6c_hand_golden():
    for cc, dec, expect_v, expect_t in GOLDEN:
        c = chart(cc)
        v, lc = decode(c, dec)
        assert v == expect_v, f"{cc} decode: {v} != {expect_v}"
        got = evaluate(c, v)
        assert abs(got - expect_t) < 0.01, f"{cc} time: {got} != {expect_t}"


# ---------- 6d: Dify 偏差差异报告(产出物,非卡点) ----------


def test_6d_dify_bias_report(capsys):
    """量化 Dify LLM 口算偏差。仅统计写文件,不 assert 偏差率。"""
    con = sqlite3.connect(settings.DB_PATH)
    con.row_factory = sqlite3.Row
    recs = con.execute(
        """
        SELECT 操作内容,决策描述,动作代码,时间,频率 FROM stds_record
        WHERE 决策描述 IS NOT NULL AND 决策描述 != ''
          AND 动作代码 IS NOT NULL AND 动作代码 != ''
          AND 时间 IS NOT NULL
        """
    ).fetchall()
    con.close()
    rows = []
    for r in recs:
        cc = r["动作代码"]
        if cc not in charts:
            continue
        try:
            v, _ = decode(charts[cc], r["决策描述"])
        except EngineError:
            continue
        formula_time = evaluate(charts[cc], v) * (r["频率"] or 1.0)
        dify_time = r["时间"]
        denom = max(abs(formula_time), 0.01)
        ratio = abs(formula_time - dify_time) / denom
        rows.append((cc, (r["操作内容"] or "")[:20], formula_time, dify_time, ratio))
    rows.sort(key=lambda x: -x[4])
    n = len(rows)
    gt10 = sum(1 for r in rows if r[4] > 0.10)
    gt50 = sum(1 for r in rows if r[4] > 0.50)
    gt100 = sum(1 for r in rows if r[4] > 1.00)
    report = (
        f"# Dify LLM 口算偏差报告\n"
        f"样本数: {n}\n"
        f"偏差>10%: {gt10} ({gt10/max(n,1):.0%})\n"
        f"偏差>50%: {gt50} ({gt50/max(n,1):.0%})\n"
        f"偏差>100%: {gt100} ({gt100/max(n,1):.0%})\n"
        f"\ntop-10 偏差最大:\n"
        + "\n".join(
            f"  {r[0]} | {r[1]} | 公式{r[2]:.2f} vs Dify{r[3]:.2f} | {r[4]:.0%}"
            for r in rows[:10]
        )
    )
    from pathlib import Path
    Path("tests/golden/dify_bias_report.md").write_text(report, encoding="utf-8")
    with capsys.disabled():
        print(report)
    assert n > 100, "样本太少,检查 decode 覆盖率"
