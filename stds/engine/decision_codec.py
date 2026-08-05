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
import unicodedata

from stds.domain.models import MostChart, ValueOption
from stds.engine.formula import EngineError


def encode(abbrevs: list) -> str:
    """按决策顺序编码，尾逗号只用于表示最后一个决策为空。

    历史实现会无条件追加逗号，导致最后一个决策非空时也产生尾逗号；
    若末项本身为空还会得到两个尾逗号。这里保留中间空决策，但把连续的
    末尾空决策收敛为一个，保证结果最多只有一个尾逗号。
    """
    cleaned = [(abbrev or "").rstrip(",") for abbrev in abbrevs]
    while len(cleaned) >= 2 and cleaned[-1] == "" and cleaned[-2] == "":
        cleaned.pop()
    return ",".join(cleaned)


_TOKEN_ALIASES = {
    # 旧经验文件中的 No Additional Reach 缩写；当前数据库使用 NARX。
    "NAR": "NARX",
}
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def _normalized_token(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return text.rstrip(",").strip()


def _canonical_token(value: object) -> str:
    token = _normalized_token(value)
    return _TOKEN_ALIASES.get(token, token)


def canonicalize_decision(decision: str) -> str:
    """规范化已知旧缩写，同时保留决策串中的空槽位和尾逗号。"""
    return ",".join(
        _canonical_token(token) if str(token).strip() else ""
        for token in str(decision or "").split(",")
    )


def _exact_matches(token: str, cands: list[ValueOption]) -> list[ValueOption]:
    """L1：必须先在全部候选中完成缩写精确匹配。"""
    canonical = _canonical_token(token)
    if not canonical:
        return []
    return [
        option
        for option in cands
        if _canonical_token(option.metric_abbrev) == canonical
    ]


def _numeric_matches(token: str, cands: list[ValueOption]) -> list[ValueOption]:
    """L2：只有 L1 全部未命中后，才按公式值做数字退化匹配。"""
    numbers = [float(value) for value in _NUMBER_RE.findall(token or "")]
    if not numbers:
        return []
    return [
        option
        for option in cands
        if option.formula_value > 0
        and any(
            abs(number - option.formula_value) < 0.001
            for number in numbers
        )
    ]


def _token_choice(token: str, cands: list[ValueOption]) -> tuple:
    """返回(choice, match_level, ambiguous)，严格执行全候选 L1→L2。"""
    exact = _exact_matches(token, cands)
    if exact:
        return exact[0], "abbrev-exact", len(exact) > 1
    numeric = _numeric_matches(token, cands)
    if numeric:
        return numeric[0], "formula-value-numeric", len(numeric) > 1
    return None, "", False


def _default_option(cands: list) -> tuple:
    """默认值策略(优先级递减):
    1. fv==0 的 'No XXX'(加法类变量,如 060 010 V2 No Place Scanner)
    2. description 精确 'No' 或 '0'(乘法因子类变量,如 050 05D V2 No/fv=1.0)
    3. 最小 fv(最后手段,标 low_conf)
    """
    if len(cands) == 1:
        return cands[0], False
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
        match_level = ""
        ambiguous_match = False
        if ti < len(tokens):
            choice, match_level, ambiguous_match = _token_choice(
                tokens[ti],
                cands,
            )
            if choice is not None:
                matched_token = tokens[ti]
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
            low_conf = low_conf or ambiguous_match
            reason = (
                f"decision-token={matched_token};match={match_level}"
            )
            if ambiguous_match:
                reason += ";ambiguous-candidates(low-confidence)"
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


def decode_strict_with_trace(chart: MostChart, decision: str) -> tuple:
    """严格解码；任何默认歧义或未消费 token 都视为无效决策。"""
    values, low_confidence, trace = _decode(
        chart,
        canonicalize_decision(decision),
    )
    if low_confidence:
        raise EngineError(
            f"low-confidence decision {chart.chartcode}: {decision!r}"
        )
    return values, trace
