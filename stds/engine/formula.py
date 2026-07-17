"""安全公式求值:用 ast 白名单求值,不是 eval,不是 LLM。

只允许算术(+ - * / ** 和一元负)。未选到的 Vn 当 0。
公式串本身已含 *60(分->秒换算),直接求值即可。
"""
from __future__ import annotations

import ast
import operator

from stds.domain.models import MostChart


class EngineError(Exception):
    pass


_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.Pow: operator.pow,
}


def _ev(node, vars):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return vars.get(node.id, 0.0)          # 未选到的 Vn 当 0
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_ev(node.left, vars), _ev(node.right, vars))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_ev(node.operand, vars))
    raise EngineError(f"非法表达式节点: {type(node).__name__}")


def evaluate(chart: MostChart, values: dict) -> float:
    """values: {var_no: formula_value} -> 代入公式求时间(秒)。"""
    expr = chart.formula.lstrip("=").strip()
    vs = {f"V{i}": x for i, x in values.items()}
    return round(_ev(ast.parse(expr, mode="eval").body, vs), 2)
