"""决策串编/解码。

decode: 把历史决策描述("T,90,NB"/"LS")反解为 {var_no: formula_value}。
        沿决策树走,按 (var, range) 双键取候选,token 多层匹配。
encode: abbrevs -> 决策串(运行时产出用)。

匹配规则(实测修正,严禁用 `in` 子串 -- 会把 T 误配到 NTT):
  L1 精确: token == metric_abbrev.rstrip(",")
  L2 数值: token 中数字 == formula_value(精确数值匹配,不用 description 避免误匹配)
未匹配变量默认取 fv==0 的 "No XXX" 选项(060 010 V2 必须如此才得 1.2)。
"""
from __future__ import annotations

import re

from stds.domain.models import MostChart, ValueOption
from stds.engine.formula import EngineError


def encode(abbrevs: list) -> str:
    return ",".join((a or "").rstrip(",") for a in abbrevs) + ","


def _match(token: str, opt: ValueOption) -> bool:
    """L1 精确 + L2 数值精确。严禁 `in` 子串(会误配 T->NTT, 10->'4 in / 10 cm')。"""
    if not token:
        return False
    ab = (opt.metric_abbrev or "").rstrip(",")
    if token == ab:                                   # L1 精确(T->T, LS->LS, NB->NB)
        return True
    # L2:token 里的数字 == formula_value(精确数值,不用 description 避免误匹配)
    nums = re.findall(r"[\d.]+", token)
    for n in nums:
        fv = float(n)
        if opt.formula_value > 0 and abs(fv - opt.formula_value) < 0.001:
            return True
    return False


def _default_option(cands: list) -> tuple:
    """默认值策略(优先级递减):
    1. fv==0 的 'No XXX'(加法类变量,如 060 010 V2 No Place Scanner)
    2. description 精确 'No' 或 '0'(乘法因子类变量,如 050 05D V2 No/fv=1.0)
    3. 最小 fv(最后手段,标 low_conf)
    """
    zeros = [o for o in cands if o.formula_value == 0.0]
    if zeros:
        return zeros[0], False
    no_opts = [o for o in cands if o.description in ("No", "0")]
    if no_opts:
        return no_opts[0], False
    return min(cands, key=lambda o: o.formula_value), True


def _decode(chart: MostChart, decision: str) -> tuple:
    """解码并保留每个变量的选择过程。"""
    tokens = [t.strip() for t in decision.split(",") if t.strip()]
    values: dict = {}
    trace: list = []
    low_conf = False
    var, rng, visited, ti = 1, 1, set(), 0
    while var != 0:
        if (var, rng) in visited:
            raise EngineError(f"cycle {chart.chartcode} V{var}R{rng}")
        visited.add((var, rng))
        cands = chart.candidates(var, rng)
        if not cands:
            raise EngineError(f"no cands {chart.chartcode} V{var}R{rng}")
        choice = None
        matched_token = None
        if ti < len(tokens):
            for opt in cands:
                if _match(tokens[ti], opt):
                    choice = opt
                    matched_token = tokens[ti]
                    break
        if choice is None:
            choice, lc = _default_option(cands)        # ★ 修正:fv=0 优先
            low_conf = low_conf or lc
            waiting_token = tokens[ti] if ti < len(tokens) else None
            if waiting_token:
                low_conf = True
            if lc:
                reason = "decision-default:min-formula-value(low-confidence)"
            elif choice.formula_value == 0.0:
                reason = "decision-default:formula-value-zero"
            else:
                reason = "decision-default:no-option"
            if waiting_token:
                reason += f";unmatched-token={waiting_token}"
        else:
            ti += 1
            reason = f"decision-token={matched_token}"
        values[var] = choice.formula_value
        trace.append((f"V{var}", choice.description, reason))
        var, rng = choice.next_variable, choice.next_range or 1
    if ti < len(tokens):
        unused = tokens[ti:]
        low_conf = True
        trace.append(("UNUSED_TOKEN", ",".join(unused), "decision contains unused token(s)"))
    return values, low_conf, trace


def decode(chart: MostChart, decision: str) -> tuple:
    """返回 (values: dict[int,float], low_conf: bool)。"""
    values, low_conf, _ = _decode(chart, decision)
    return values, low_conf


def decode_with_trace(chart: MostChart, decision: str) -> tuple:
    """返回 (values, low_conf, trace)，供快速路径输出逐变量审计轨迹。"""
    return _decode(chart, decision)
