"""廉价优先级联主入口:串联 T0->T5，可配置 T0.5 common_chart。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from stds.cascade import rules

logger = logging.getLogger("stds.resolver")
from stds.data.common_chart import load_common_chart, match_common_chart
from stds.domain.models import MostChart, Source, StdsElement, StdsResult
from stds.engine.decision_codec import encode
from stds.engine.formula import EngineError, evaluate
from stds.engine.traverse import traverse
from stds.llm.classify import classify_machine
from stds.llm.pick_value import pick_value as _default_pick_value
from stds.llm.select_chartcode import select_chartcode as _default_select_chartcode


@dataclass
class Deps:
    charts: dict                           # {chartcode: MostChart}
    cache: object                          # AutoCache
    common_rows: list = None               # T0.5 common_chart 33 条
    use_common_chart: bool = False          # 是否启用 T0.5 快速路径（默认关闭）
    llm_classify: Callable = None          # async (text) -> bool(设备=True)
    llm_select_chartcode: Callable = None  # async (op_des, charts) -> chartcode or None
    llm_pick_value: Callable = None        # async (op_des, cands) -> (VOption, conf, reason)
    history_index: object = None           # T1 kNN(可选)

    def __post_init__(self):
        if self.llm_pick_value is None:
            self.llm_pick_value = _default_pick_value
        if self.llm_classify is None:
            self.llm_classify = classify_machine
        if self.llm_select_chartcode is None:
            self.llm_select_chartcode = _default_select_chartcode
        if self.common_rows is None:
            self.common_rows = load_common_chart() if self.use_common_chart else []


def _put_cache_template(el: StdsElement, result: StdsResult, deps: Deps, unit_time: float) -> None:
    """缓存频率为 1 的模板，避免跨输入频率复用已乘频率/已舍入的总时间。"""
    deps.cache.put(
        el.norm_key,
        replace(result, time_s=unit_time, freq=1.0),
    )


def _is_common_chart_result(result: StdsResult) -> bool:
    """识别由 T0.5 写入的缓存，保证关闭开关后不会从 T0 间接命中。"""
    for step in result.trace or []:
        if isinstance(step, dict):
            variable = step.get("变量", step.get("variable", step.get("step", "")))
        elif isinstance(step, (list, tuple)) and step:
            variable = step[0]
        else:
            variable = ""
        if str(variable).startswith("T0.5_common"):
            return True
    return False


async def resolve(
    el: StdsElement,
    deps: Deps,
    *,
    machine_hint: Optional[bool] = None,
) -> StdsResult:
    """单条记录:从输入到产出。"""
    op = el.operation_des
    logger.info(f"===== 开始分析: '{op}' (freq={el.freq}) =====")

    # Excel 两阶段管线已对父动作判定过主体。设备动作直接返回，避免缓存或
    # 第二次 LLM 分类改变同一批次的判定；人工动作仍可复用缓存和 kNN。
    if machine_hint is True:
        logger.info("  [T2] 沿用拆解阶段判定: 设备")
        return StdsResult.machine_placeholder(el)

    # T0 精确缓存
    cached = deps.cache.get(el.norm_key)
    common_cache_disabled = (
        cached is not None
        and not deps.use_common_chart
        and _is_common_chart_result(cached)
    )
    if cached is not None and not common_cache_disabled:
        logger.info(f"  [T0] 缓存命中: {cached.chartcode} / {cached.time_s}s")
        # 新缓存恒为 freq=1；兼容当前进程里热更新前留下的旧缓存对象。
        time_single = cached.time_s if cached.freq == 1.0 else cached.time_s / (cached.freq or 1.0)
        return replace(
            cached,
            element=el,
            time_s=round(time_single * el.freq, 2),
            freq=el.freq,
        )
    if common_cache_disabled:
        logger.info("  [T0] 跳过由 T0.5 产生的缓存（Common Chart 已关闭）")
    else:
        logger.debug(f"  [T0] 缓存未命中")

    # T0.5 common_chart 关键词匹配(33 条高频动作,零 LLM)
    cc_hit = (
        match_common_chart(op, deps.common_rows)
        if deps.use_common_chart
        else None
    )
    if not deps.use_common_chart:
        logger.info("  [T0.5] Common Chart 已关闭，跳过")
    if cc_hit and cc_hit.chartcode in deps.charts:
        chart = deps.charts[cc_hit.chartcode]
        try:
            from stds.engine.decision_codec import decode_with_trace
            values, lc, decision_trace = decode_with_trace(chart, cc_hit.decision)
            time_single = evaluate(chart, values)
            logger.info(f"  [T0.5] common_chart 命中: {cc_hit.chartcode} / '{cc_hit.decision}' / {time_single}s (low_conf={lc})")
            res = StdsResult(
                element=el, chartcode=cc_hit.chartcode, decision=cc_hit.decision,
                time_s=round(time_single * el.freq, 2),
                cv="C" if chart.value_added else "V", freq=el.freq,
                source=Source.CACHE, confidence=0.6 if lc else 0.95,
                needs_review=lc,
                trace=[
                    ("T0.5_common", cc_hit.operation_des, "keyword_match"),
                    *decision_trace,
                ],
            )
            _put_cache_template(el, res, deps, time_single)
            return res
        except Exception as e:
            logger.warning(f"  [T0.5] decode 失败: {e},继续 T1")

    # T1 历史 kNN(只复用 chartcode+决策描述,时间重新公式算)
    if deps.history_index is not None:
        hits = await deps.history_index.knn(op, k=5)
        if hits:
            logger.debug(f"  [T1] kNN top3: {[(h.chartcode, round(h.score,3)) for h in hits[:3]]}")
        if hits and hits[0].score >= 0.92:
            top_cc = hits[0].chartcode
            consistent = all(h.chartcode == top_cc for h in hits[:3])
            if consistent and top_cc in deps.charts:
                chart = deps.charts[top_cc]
                try:
                    from stds.engine.decision_codec import decode_with_trace
                    values, lc, decision_trace = decode_with_trace(chart, hits[0].decision)
                    time_single = evaluate(chart, values)
                    logger.info(f"  [T1] kNN 命中: {top_cc} / '{hits[0].decision}' / {time_single}s (score={hits[0].score:.3f}, low_conf={lc})")
                    res = StdsResult(
                        element=el, chartcode=top_cc, decision=hits[0].decision,
                        time_s=round(time_single * el.freq, 2),
                        cv="C" if chart.value_added else "V", freq=el.freq,
                        source=Source.KNN, confidence=min(hits[0].score, 0.6) if lc else hits[0].score,
                        needs_review=lc, trace=[
                            ("T1_kNN", hits[0].text, f"score={hits[0].score:.3f}"),
                            *decision_trace,
                        ],
                    )
                    _put_cache_template(el, res, deps, time_single)
                    return res
                except Exception as e:
                    logger.warning(f"  [T1] decode 失败: {e},继续 T2")
            else:
                logger.debug(f"  [T1] 邻居不一致或 chartcode 不在 charts,跳过")

    # T2 判人/设备(规则快速路径 + LLM 兜底)
    m = machine_hint
    if m is not None:
        logger.info(f"  [T2] 沿用拆解阶段判定: {'设备' if m else '人工'}")
    else:
        m = rules.rule_machine(op)
        if m is not None:
            logger.info(f"  [T2] 规则判定: {'设备' if m else '人工'}")
        else:
            m = await deps.llm_classify(op)
            logger.info(f"  [T2] LLM 判定: {'设备' if m else '人工'}")
    if m:
        logger.info(f"  → 结果: 设备动作,跳过计算")
        return StdsResult.machine_placeholder(el)

    # T3 chartcode 选择(LLM 从 62 个里选)
    cc = await deps.llm_select_chartcode(op, deps.charts)
    chart: Optional[MostChart] = deps.charts.get(cc) if cc else None
    if chart is None:
        logger.warning(f"  [T3] LLM 选码失败(cc={cc}),进 unresolved")
        return StdsResult.unresolved(el, cc).mark_review()
    logger.info(f"  [T3] LLM 选码: {cc} / '{chart.title}'")

    # T4 决策树(traverse -> pick_value -> evaluate)
    try:
        values, abbrevs, trace = await traverse(
            chart, op, deps.llm_pick_value
        )
    except EngineError as e:
        logger.error(f"  [T4] 决策树遍历失败: {e},进 unresolved")
        return StdsResult.unresolved(el, cc).mark_review()

    time_single = evaluate(chart, values)
    decision = encode(abbrevs)
    conf = min(1.0, 0.9)

    logger.info(f"  [T4] 决策树完成:")
    logger.info(f"       决策串: {decision}")
    logger.info(f"       公式时间: {time_single}s × freq={el.freq} = {round(time_single * el.freq, 2)}s")
    logger.info(f"       置信度: {conf}")
    for step in trace:
        logger.debug(f"       {step[0]}: {step[1]} ← {step[2]}")

    res = StdsResult(
        element=el, chartcode=cc, decision=decision,
        time_s=round(time_single * el.freq, 2),
        cv="C" if chart.value_added else "V", freq=el.freq,
        source=Source.FORMULA, confidence=conf,
        needs_review=conf < 0.75, trace=trace,
    )
    _put_cache_template(el, res, deps, time_single)
    logger.info(f"  → 结果: {res.time_s}s (source={res.source.value}, needs_review={res.needs_review})")
    return res
