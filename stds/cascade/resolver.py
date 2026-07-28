"""廉价优先级联主入口:串联 T0->T5，可配置 T0.5 common_chart。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

from stds.cascade import rules
from stds.cascade.numeric import NumericContext

logger = logging.getLogger("stds.resolver")
from stds.data.common_chart import load_common_chart, match_common_chart
from stds.domain.models import MostChart, Source, StdsElement, StdsResult
from stds.engine.decision_codec import encode
from stds.engine.formula import EngineError, evaluate
from stds.engine.traverse import traverse
from stds.llm.classify import classify_machine
from stds.llm.extract_part_name import (
    extract_part_groups as _default_extract_part_groups,
    extract_part_name as _default_extract_part_name,
)
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
    part_weight_index: object = None       # 零件名称 -> 单重(精确/语义)
    llm_extract_part_name: Callable = None # async (op_des) -> part_name or None
    llm_extract_part_groups: Callable = None # async (parent, children) -> groups
    part_name_cache: dict = field(default_factory=dict)
    part_group_cache: dict = field(default_factory=dict)
    experience_index: object = None          # 操作身份 -> Chartcode + 参数经验
    experience_scope: str = ""               # 经验文件摘要/缓存命名空间

    def __post_init__(self):
        if self.llm_pick_value is None:
            self.llm_pick_value = _default_pick_value
        if self.llm_classify is None:
            self.llm_classify = classify_machine
        if self.llm_select_chartcode is None:
            self.llm_select_chartcode = _default_select_chartcode
        if self.llm_extract_part_name is None:
            self.llm_extract_part_name = _default_extract_part_name
        if self.llm_extract_part_groups is None:
            self.llm_extract_part_groups = _default_extract_part_groups
        if self.common_rows is None:
            self.common_rows = load_common_chart() if self.use_common_chart else []


@dataclass
class PartWeightGroupResolution:
    """父工序拆解组的共享重量上下文；key 为 1-based 子工序序号。"""

    contexts: dict[int, NumericContext] = field(default_factory=dict)
    attempted: bool = False


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


def _experience_enabled(deps: Deps) -> bool:
    index = getattr(deps, "experience_index", None)
    return index is not None and bool(getattr(index, "available", False))


def _chart_key(chartcode: object, charts: dict) -> Optional[str]:
    """将经验表中的码映射到当前图表库的真实 key。"""
    raw = str(chartcode or "").strip().upper()
    if raw in charts:
        return raw
    compact = "".join(raw.split())
    matches = [
        key
        for key in charts
        if "".join(str(key).upper().split()) == compact
    ]
    return matches[0] if len(matches) == 1 else None


async def _match_experience(
    operation_des: str,
    deps: Deps,
    *,
    expected_chartcode: Optional[str] = None,
):
    """匹配一个不歧义且属于当前图表库的完整经验上下文。"""
    if not _experience_enabled(deps):
        return None, None
    try:
        context = await deps.experience_index.match(
            operation_des,
            expected_chartcode=expected_chartcode,
        )
    except Exception:
        logger.exception(
            "  [Experience] 经验匹配失败(expected_chartcode=%r)，沿用原逻辑",
            expected_chartcode,
        )
        return None, None
    if context is None:
        return None, None
    chartcode = _chart_key(getattr(context, "chartcode", None), deps.charts)
    if chartcode is None:
        logger.warning(
            "  [Experience] 经验 %r 的 Chartcode=%r 不在当前图表库，已忽略",
            getattr(context, "experience_id", ""),
            getattr(context, "chartcode", None),
        )
        return None, None
    if expected_chartcode is not None and chartcode != expected_chartcode:
        logger.warning(
            "  [Experience] 约束匹配返回了其他 Chartcode: expected=%s actual=%s",
            expected_chartcode,
            chartcode,
        )
        return None, None
    return context, chartcode


def _experience_trace(
    context,
    *,
    chartcode: str,
    source_name: str,
    selection_mode: str,
) -> tuple:
    similarity = getattr(context, "similarity", 0.0)
    try:
        similarity_text = f"{float(similarity):.4f}"
    except (TypeError, ValueError):
        similarity_text = str(similarity)
    return (
        "ExperienceChartcode",
        f"{getattr(context, 'operation_label', '')} -> {chartcode}",
        (
            f"experience_id={getattr(context, 'experience_id', '')};"
            f"match_type={getattr(context, 'match_type', '')};"
            f"similarity={similarity_text};"
            f"source={source_name};"
            f"chart_row={getattr(context, 'chart_row', '')};"
            f"mode={selection_mode}"
        ),
    )


def _numeric_context_from_match(
    part_name: str,
    match,
    *,
    group_id: str = "",
) -> NumericContext:
    return NumericContext(
        weight_kg=match.weight_kg,
        query_name=part_name,
        matched_name=match.matched_name,
        similarity=match.similarity,
        match_type=match.match_type,
        source=match.source_label,
        group_id=group_id,
    )


async def _part_weight_context(
    operation_des: str,
    deps: Deps,
) -> Optional[NumericContext]:
    """提取零件名并查询单重；任何失败都退回原分析链路。"""
    index = deps.part_weight_index
    if index is None or not getattr(index, "available", False):
        return None

    cache_key = rules.normalize(operation_des)
    try:
        if cache_key in deps.part_name_cache:
            part_name = deps.part_name_cache[cache_key]
        else:
            part_name = await deps.llm_extract_part_name(operation_des)
            deps.part_name_cache[cache_key] = part_name
        if not part_name:
            logger.info("  [PartWeight] LLM 未提取到零件，沿用原分析")
            return None

        match = await index.match(part_name)
        if match is None:
            logger.info(
                "  [PartWeight] 未取得可靠单重: extracted=%r，沿用原分析",
                part_name,
            )
            return None
        logger.info(
            "  [PartWeight] 命中: %r -> %r / %s kg / %s=%.4f",
            part_name,
            match.matched_name,
            match.weight_kg,
            match.match_type,
            match.similarity,
        )
        return _numeric_context_from_match(part_name, match)
    except Exception:
        logger.exception("  [PartWeight] 零件提取/重量检索失败，沿用原分析")
        return None


async def resolve_part_weight_groups(
    parent_operation: str,
    child_operations: Sequence[str],
    deps: Deps,
) -> PartWeightGroupResolution:
    """父工序级统一提取/匹配，保证同组子工序共享同一 NumericContext。"""
    index = getattr(deps, "part_weight_index", None)
    if index is None or not getattr(index, "available", False):
        return PartWeightGroupResolution()

    children = tuple(str(child or "").strip() for child in child_operations)
    cache_key = (
        rules.normalize(parent_operation),
        tuple(rules.normalize(child) for child in children),
    )
    cache = getattr(deps, "part_group_cache", None)
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    resolution = PartWeightGroupResolution(attempted=True)
    try:
        extracted = await deps.llm_extract_part_groups(
            parent_operation,
            children,
        )
        groups = getattr(extracted, "groups", extracted) or ()
        matches = {}
        contexts_by_name = {}
        ambiguous_indexes = set()
        for group_number, group in enumerate(groups, start=1):
            part_name = str(getattr(group, "part_name", "") or "").strip()
            normalized_name = rules.normalize(part_name)
            if not normalized_name:
                continue
            if normalized_name not in matches:
                matches[normalized_name] = await index.match(part_name)
            match = matches[normalized_name]
            if match is None:
                logger.info(
                    "  [PartWeightGroup] 未取得可靠单重: part=%r",
                    part_name,
                )
                continue
            context = contexts_by_name.get(normalized_name)
            if context is None:
                context = _numeric_context_from_match(
                    part_name,
                    match,
                    group_id=f"G{group_number}",
                )
                contexts_by_name[normalized_name] = context
            for raw_index in getattr(group, "child_indexes", ()):
                try:
                    child_index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if (
                    child_index < 1
                    or child_index > len(children)
                    or child_index in ambiguous_indexes
                ):
                    continue
                if child_index in resolution.contexts:
                    resolution.contexts.pop(child_index, None)
                    ambiguous_indexes.add(child_index)
                    logger.warning(
                        "  [PartWeightGroup] 子工序 %s 被分配到多个零件，"
                        "已取消自动重量",
                        child_index,
                    )
                    continue
                resolution.contexts[child_index] = context
            logger.info(
                "  [PartWeightGroup] %s: %r -> %r / %s kg / children=%s",
                context.group_id,
                part_name,
                match.matched_name,
                match.weight_kg,
                sorted(
                    child_index
                    for child_index, candidate in resolution.contexts.items()
                    if candidate is context
                ),
            )
    except Exception:
        logger.exception(
            "  [PartWeightGroup] 父工序零件分组失败，整组不自动施加重量"
        )

    if isinstance(cache, dict):
        cache[cache_key] = resolution
    return resolution


async def resolve(
    el: StdsElement,
    deps: Deps,
    *,
    machine_hint: Optional[bool] = None,
    numeric_context: Optional[NumericContext] = None,
    part_context_resolved: bool = False,
) -> StdsResult:
    """单条记录:从输入到产出。"""
    op = el.operation_des
    logger.info(f"===== 开始分析: '{op}' (freq={el.freq}) =====")

    # Excel 两阶段管线已对父动作判定过主体。设备动作直接返回，避免缓存或
    # 第二次 LLM 分类改变同一批次的判定；人工动作仍可复用缓存和 kNN。
    if machine_hint is True:
        logger.info("  [T2] 沿用拆解阶段判定: 设备")
        return StdsResult.machine_placeholder(el)

    weight_scoped = part_context_resolved or numeric_context is not None
    experience_scoped = _experience_enabled(deps)
    decision_scoped = weight_scoped or experience_scoped

    # T0 精确缓存
    cached = None if decision_scoped else deps.cache.get(el.norm_key)
    common_cache_disabled = (
        cached is not None
        and not deps.use_common_chart
        and _is_common_chart_result(cached)
    )
    if decision_scoped:
        reason = "经验已启用" if experience_scoped else "父工序重量上下文已确定"
        logger.info("  [T0] %s，跳过通用动作缓存", reason)
    elif cached is not None and not common_cache_disabled:
        logger.info(f"  [T0] 缓存命中: {cached.chartcode} / {cached.time_s}s")
        # 新缓存恒为 freq=1；兼容当前进程里热更新前留下的旧缓存对象。
        time_single = cached.time_s if cached.freq == 1.0 else cached.time_s / (cached.freq or 1.0)
        return replace(
            cached,
            element=el,
            time_s=round(time_single * el.freq, 2),
            freq=el.freq,
        )
    if decision_scoped:
        pass
    elif common_cache_disabled:
        logger.info("  [T0] 跳过由 T0.5 产生的缓存（Common Chart 已关闭）")
    else:
        logger.debug(f"  [T0] 缓存未命中")

    if numeric_context is None and not part_context_resolved:
        numeric_context = await _part_weight_context(op, deps)
        weight_scoped = numeric_context is not None
        decision_scoped = weight_scoped or experience_scoped

    # T0.5 common_chart 关键词匹配(33 条高频动作,零 LLM)
    cc_hit = (
        match_common_chart(op, deps.common_rows)
        if deps.use_common_chart and not decision_scoped
        else None
    )
    if decision_scoped:
        reason = "经验已启用" if experience_scoped else "重量上下文已确定"
        logger.info("  [T0.5] %s，跳过完整决策复用", reason)
    elif not deps.use_common_chart:
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
    if deps.history_index is not None and not decision_scoped:
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
    elif deps.history_index is not None:
        reason = "经验已启用" if experience_scoped else "重量上下文已确定"
        logger.info("  [T1] %s，跳过历史完整决策复用", reason)

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

    # T3E 先匹配完整动作经验。必须保留 ExperienceContext，不能只留下码，
    # 否则“转身/弯腰”等共用 Chartcode 的动作会串用参数规则。
    experience_context, experience_cc = await _match_experience(op, deps)
    experience_mode = ""
    if experience_context is not None:
        cc = experience_cc
        experience_mode = "experience-selected"
        logger.info(
            "  [T3E] 经验选码: %s / operation=%r / experience_id=%s",
            cc,
            getattr(experience_context, "operation_label", ""),
            getattr(experience_context, "experience_id", ""),
        )
    else:
        # 没有唯一可靠动作经验时沿用原 LLM 选择。
        cc = await deps.llm_select_chartcode(op, deps.charts)
    chart: Optional[MostChart] = deps.charts.get(cc) if cc else None
    if chart is None:
        logger.warning(f"  [T3] LLM 选码失败(cc={cc}),进 unresolved")
        return StdsResult.unresolved(el, cc).mark_review()
    if experience_context is None:
        logger.info(f"  [T3] LLM 选码: {cc} / '{chart.title}'")
        # LLM 已确定图表后，可在该图表内重新约束匹配参数经验。仍然要求
        # 唯一动作身份；匹配不到就完全回退原参数选择逻辑。
        experience_context, constrained_cc = await _match_experience(
            op,
            deps,
            expected_chartcode=cc,
        )
        if experience_context is not None and constrained_cc == cc:
            experience_mode = "llm-chart-constrained-context"
            logger.info(
                "  [T3E] 已绑定同 Chartcode 参数经验: operation=%r / "
                "experience_id=%s",
                getattr(experience_context, "operation_label", ""),
                getattr(experience_context, "experience_id", ""),
            )

    experience_source = (
        str(getattr(deps.experience_index, "source_name", "") or "")
        if experience_context is not None
        else ""
    )
    if experience_context is not None and not experience_source:
        experience_source = "uploaded-experience"

    # T4 决策树(traverse -> pick_value -> evaluate)
    try:
        values, abbrevs, trace = await traverse(
            chart,
            op,
            deps.llm_pick_value,
            numeric_context=numeric_context,
            experience_context=experience_context,
            experience_source=experience_source,
        )
    except EngineError as e:
        logger.error(f"  [T4] 决策树遍历失败: {e},进 unresolved")
        unresolved = StdsResult.unresolved(el, cc).mark_review()
        if experience_context is not None:
            unresolved.trace = [
                _experience_trace(
                    experience_context,
                    chartcode=cc,
                    source_name=experience_source,
                    selection_mode=f"{experience_mode};traverse-error",
                ),
                ("UNRESOLVED", cc, str(e)),
            ]
        return unresolved

    time_single = evaluate(chart, values)
    decision = encode(abbrevs)
    conf = min(1.0, 0.9)

    logger.info(f"  [T4] 决策树完成:")
    logger.info(f"       决策串: {decision}")
    logger.info(f"       公式时间: {time_single}s × freq={el.freq} = {round(time_single * el.freq, 2)}s")
    logger.info(f"       置信度: {conf}")
    for step in trace:
        logger.debug(f"       {step[0]}: {step[1]} ← {step[2]}")

    if numeric_context is not None:
        trace.insert(
            0,
            (
                "PartWeightLookup",
                (
                    f"{numeric_context.query_name} -> "
                    f"{numeric_context.matched_name} / "
                    f"{numeric_context.weight_kg:g} kg"
                ),
                (
                    f"{numeric_context.match_type};"
                    f"similarity={numeric_context.similarity:.4f};"
                    f"source={numeric_context.source};"
                    f"group={numeric_context.group_id or 'single'}"
                ),
            ),
        )
    if experience_context is not None:
        trace.insert(
            0,
            _experience_trace(
                experience_context,
                chartcode=cc,
                source_name=experience_source,
                selection_mode=experience_mode,
            ),
        )

    res = StdsResult(
        element=el, chartcode=cc, decision=decision,
        time_s=round(time_single * el.freq, 2),
        cv="C" if chart.value_added else "V", freq=el.freq,
        source=Source.FORMULA, confidence=conf,
        needs_review=conf < 0.75, trace=trace,
    )
    if not decision_scoped:
        _put_cache_template(el, res, deps, time_single)
    logger.info(f"  → 结果: {res.time_s}s (source={res.source.value}, needs_review={res.needs_review})")
    return res
