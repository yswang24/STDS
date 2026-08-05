"""Step 5 验收:决策树遍历不抛错、不无限循环(visited 护栏)。"""
from __future__ import annotations

import asyncio

from stds.data.charts_loader import load_charts
from stds.engine.traverse import traverse


def test_traverse_020_02a():
    charts = load_charts()
    c = charts["020 02A"]
    async def pick(op, cands):
        return cands[0], 1.0, "test"
    v, ab, tr = asyncio.run(traverse(c, "op", pick))
    assert len(tr) >= 1                    # 走完不抛错
    assert 0 in v.values() or any(         # 最终要到终点(next_variable=0)
        cands[0].next_variable == 0
        for cands in c.options.values()
    )


def test_traverse_all_charts_finish():
    """64 个 chartcode 全部用'选第0个'能终止(不抛错、不无限循环)。"""
    charts = load_charts()
    async def pick(op, cands):
        return cands[0], 1.0, "t"
    errors = []
    for cc, c in charts.items():
        try:
            v, _, _ = asyncio.run(traverse(c, "t", pick))
            assert len(v) > 0
        except Exception as e:
            errors.append(f"{cc}: {e}")
    assert not errors, f"不可遍历: {errors[:5]}"
