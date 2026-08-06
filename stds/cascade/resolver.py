"""廉价优先级联主入口:串联 T0->T5，可配置 T0.5 common_chart。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

from stds.cascade import rules
from stds.cascade.numeric import NumericContext, PartIdentityContext

logger = logging.getLogger("stds.resolver")
from stds.data.cache import decision_cache_scope
from stds.domain.chartcode_policy import (
    general_chart_candidates,
    is_experience_only_chartcode,
)
from stds.domain.models import MostChart, Source, StdsElement, StdsResult
from stds.engine.decision_codec import decode_strict_with_trace, encode
from stds.engine.formula import EngineError, evaluate
from stds.engine.traverse import traverse
from stds.llm.classify import classify_machine
from stds.llm.extract_part_name import (
    extract_part_groups as _default_extract_part_groups,
    extract_part_name as _default_extract_part_name,
)
from stds.llm.pick_value import pick_value as _default_pick_value
from stds.llm.select_chartcode import select_chartcode as _default_select_chartcode
from stds.llm.select_parameter_experience import (
    select_parameter_experience as _default_select_parameter_experience,
)
from stds.experience.common_index import CommonChartSemanticIndex
from stds.experience.index import normalize_operation
from stds.retrieval.part_weight_index import normalize_part_name
from stds.retrieval.model_weight_pool import (
    ModelWeightPool,
    normalize_model_weight_identity,
)
from stds.retrieval.part_weight_pool import PartWeightPool


@dataclass
class Deps:
    charts: dict                           # {chartcode: MostChart}
    cache: object                          # AutoCache
    common_entries: Sequence = ()          # 上传经验文件中的 Common_Chart
    common_index: object = None             # Common 语义优先、关键词回退索引
    common_rows: Optional[Sequence] = None # 兼容显式注入的旧参数名；不再读数据库
    use_common_chart: bool = False          # 是否启用 T0.5 快速路径（默认关闭）
    use_semantic_experience: bool = True    # Chartcode/Common 语义检索（默认开启）
    llm_classify: Callable = None          # async (text) -> bool(设备=True)
    llm_select_chartcode: Callable = None  # async (op_des, charts) -> chartcode or None
    llm_select_parameter_experience: Callable = None # async (op, cc, contexts) -> (index, reason)
    llm_pick_value: Callable = None        # async (op_des, cands) -> (VOption, conf, reason)
    history_index: object = None           # T1 kNN(可选)
    part_weight_index: object = None       # 零件名称 -> 单重(精确/语义)
    llm_extract_part_name: Callable = None # async (op_des) -> part_name or None
    llm_extract_part_groups: Callable = None # async (parent, children) -> groups
    part_name_cache: dict = field(default_factory=dict)
    part_group_cache: dict = field(default_factory=dict)
    part_weight_pool: PartWeightPool = field(default_factory=PartWeightPool)
    model_weight_pool: ModelWeightPool = field(default_factory=ModelWeightPool)
    experience_index: object = None          # 操作身份 -> Chartcode + 参数经验
    experience_scope: str = ""               # 经验文件摘要/缓存命名空间

    def __post_init__(self):
        if self.llm_pick_value is None:
            self.llm_pick_value = _default_pick_value
        if self.llm_classify is None:
            self.llm_classify = classify_machine
        if self.llm_select_chartcode is None:
            self.llm_select_chartcode = _default_select_chartcode
        if self.llm_select_parameter_experience is None:
            self.llm_select_parameter_experience = (
                _default_select_parameter_experience
            )
        if self.llm_extract_part_name is None:
            self.llm_extract_part_name = _default_extract_part_name
        if self.llm_extract_part_groups is None:
            self.llm_extract_part_groups = _default_extract_part_groups
        if self.part_weight_pool is None:
            self.part_weight_pool = PartWeightPool()
        if self.model_weight_pool is None:
            self.model_weight_pool = ModelWeightPool()
        if not self.common_entries and self.common_rows:
            self.common_entries = tuple(self.common_rows)
        else:
            self.common_entries = tuple(self.common_entries or ())
        # 保留只读兼容别名，避免调用方在迁移期读取到 None。
        self.common_rows = self.common_entries
        if self.common_index is None and self.common_entries:
            self.common_index = CommonChartSemanticIndex(
                self.common_entries,
                similarity_threshold=0.70,
            )


@dataclass
class PartWeightGroupResolution:
    """父工序拆解组的共享零件身份/重量；key 为 1-based 子工序序号。"""

    contexts: dict[int, NumericContext] = field(default_factory=dict)
    identity_contexts: dict[int, PartIdentityContext] = field(default_factory=dict)
    attempted: bool = False


def _put_cache_template(el: StdsElement, result: StdsResult, deps: Deps, unit_time: float) -> None:
    """缓存频率为 1 的模板，避免跨输入频率复用已乘频率/已舍入的总时间。"""
    scope = decision_cache_scope(deps)
    template = replace(result, time_s=unit_time, freq=1.0)
    try:
        deps.cache.put(el.norm_key, template, scope=scope)
    except TypeError:
        # 有作用域时不允许降级写入全局键，否则上传文件之间会串缓存。
        if not scope:
            deps.cache.put(el.norm_key, template)


def _get_cache_template(el: StdsElement, deps: Deps):
    """按当前上传摘要和 Common 开关读取 T0；旧缓存仅在无 scope 时兼容。"""
    scope = decision_cache_scope(deps)
    try:
        return deps.cache.get(el.norm_key, scope=scope)
    except TypeError:
        return deps.cache.get(el.norm_key) if not scope else None


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
    """以语义 Top1 匹配一个属于当前图表库的 Chartcode 经验。"""
    if (
        not _experience_enabled(deps)
        or not bool(getattr(deps, "use_semantic_experience", True))
    ):
        return None, None
    index = deps.experience_index
    matcher = getattr(index, "match_chartcode_semantic", None)
    if not callable(matcher):
        logger.warning(
            "  [Experience] 当前经验索引不支持纯语义 Chartcode API，已跳过"
        )
        return None, None
    try:
        context = await matcher(
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


async def _match_parameter_experience(
    operation_des: str,
    deps: Deps,
    *,
    chartcode: str,
    chart_experience_context=None,
):
    """为最终 Chartcode 选择一整条参数经验，并贯穿所有 Vn。

    Chartcode 若由经验语义 Top1 选出，参数必须绑定到同一经验身份；
    Chartcode 若由 LLM 选出，则把该码下全部有效参数经验一次性交给 LLM
    选择，禁止在后续遍历中混用多条记录。
    """
    if not _experience_enabled(deps):
        return None, ""

    def has_variable_hints(context) -> bool:
        hints = getattr(context, "variable_hints", None)
        if not hasattr(hints, "values"):
            return False
        return any(str(value or "").strip() for value in hints.values())

    def valid_contexts(contexts) -> list:
        valid = []
        for context in tuple(contexts or ()):
            matched_chartcode = _chart_key(
                getattr(context, "chartcode", None),
                deps.charts,
            )
            if matched_chartcode == chartcode and has_variable_hints(context):
                valid.append(context)
        return valid

    index = deps.experience_index
    getter = getattr(index, "parameter_contexts_for_chartcode", None)

    # 语义经验已经选中具体 Chartcode 行：参数只能来自同一经验身份。
    if chart_experience_context is not None:
        matched_chartcode = _chart_key(
            getattr(chart_experience_context, "chartcode", None),
            deps.charts,
        )
        if matched_chartcode != chartcode:
            logger.warning(
                "  [ExperienceParameter] Chartcode 经验上下文越界: "
                "expected=%s actual=%s",
                chartcode,
                matched_chartcode,
            )
            return None, ""
        if has_variable_hints(chart_experience_context):
            return chart_experience_context, "bound-chartcode-experience"
        if callable(getter):
            try:
                bound = valid_contexts(getter(
                    chartcode,
                    experience_id=getattr(
                        chart_experience_context,
                        "experience_id",
                        "",
                    ),
                    operation_key=normalize_operation(getattr(
                        chart_experience_context,
                        "operation_label",
                        "",
                    )),
                ))
            except Exception:
                logger.exception(
                    "  [ExperienceParameter] 同身份参数经验读取失败: %s",
                    chartcode,
                )
                return None, ""
            if len(bound) == 1:
                return bound[0], "bound-chartcode-experience"
            if len(bound) > 1:
                logger.warning(
                    "  [ExperienceParameter] 同一经验身份存在多条参数规则，"
                    "为避免混用已全部忽略: chartcode=%s experience_id=%s",
                    chartcode,
                    getattr(chart_experience_context, "experience_id", ""),
                )
        return None, ""

    # Chartcode 来自 LLM：内置索引返回该码下的全部参数经验。
    if callable(getter):
        try:
            contexts = valid_contexts(getter(chartcode))
        except Exception:
            logger.exception(
                "  [ExperienceParameter] 参数经验候选读取失败(chartcode=%r)",
                chartcode,
            )
            return None, ""
        if len(contexts) == 1:
            return contexts[0], "llm-chart-single-parameter-experience"
        if len(contexts) > 1:
            try:
                selected_index, reason = (
                    await deps.llm_select_parameter_experience(
                        operation_des,
                        chartcode,
                        contexts,
                    )
                )
            except Exception:
                logger.exception(
                    "  [ExperienceParameter] LLM 参数经验选择失败: %s",
                    chartcode,
                )
                return None, ""
            if type(selected_index) is not int or not (
                0 <= selected_index < len(contexts)
            ):
                logger.warning(
                    "  [ExperienceParameter] LLM 未返回有效参数经验索引: "
                    "chartcode=%s index=%r count=%s",
                    chartcode,
                    selected_index,
                    len(contexts),
                )
                return None, ""
            safe_reason = str(reason or "").replace(";", "，").strip()
            return (
                contexts[selected_index],
                (
                    "llm-chart-parameter-experience;"
                    f"candidate_count={len(contexts)};"
                    f"selected_index={selected_index};"
                    f"reason={safe_reason}"
                ),
            )
        return None, ""

    # 兼容仍未迁移到新候选 API 的测试替身/扩展索引。
    matcher = getattr(index, "match_parameters", None)
    if callable(matcher):
        try:
            context = await matcher(
                operation_des,
                expected_chartcode=chartcode,
            )
        except Exception:
            logger.exception(
                "  [ExperienceParameter] 兼容参数经验匹配失败: %s",
                chartcode,
            )
            return None, ""
        if valid_contexts([context] if context is not None else []):
            return context, "legacy-parameter-matcher"
    return None, ""


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


def _parameter_experience_trace(
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
        "ExperienceParameter",
        f"{getattr(context, 'operation_label', '')} @ {chartcode}",
        (
            f"experience_id={getattr(context, 'experience_id', '')};"
            f"match_type={getattr(context, 'match_type', '')};"
            f"similarity={similarity_text};"
            f"source={source_name};"
            f"parameter_row={getattr(context, 'parameter_row', '')};"
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


def _part_identity_context(
    part_name: str,
    *,
    group_id: str = "",
    source: str,
    identity_kind: str = "extracted_name",
    identity_value: Optional[str] = None,
) -> Optional[PartIdentityContext]:
    normalized_identity = normalize_model_weight_identity(
        part_name if identity_value is None else identity_value
    )
    if not normalized_identity:
        return None
    return PartIdentityContext(
        part_name=str(part_name).strip(),
        identity_key=f"{identity_kind}:{normalized_identity}",
        source=source,
        group_id=group_id,
    )


def _matched_part_identity_context(
    part_name: str,
    match,
    *,
    group_id: str = "",
    source: str,
) -> Optional[PartIdentityContext]:
    """表重命中后用 canonical 零件号/名称建立模型重量身份。"""

    part_no = str(getattr(match, "part_no", "") or "").strip()
    if part_no:
        return _part_identity_context(
            part_name,
            group_id=group_id,
            source=source,
            identity_kind="part_no",
            identity_value=part_no,
        )
    matched_name = str(getattr(match, "matched_name", "") or "").strip()
    return _part_identity_context(
        part_name,
        group_id=group_id,
        source=source,
        identity_kind="matched_name" if matched_name else "extracted_name",
        identity_value=matched_name or part_name,
    )


async def _part_lookup_context(
    operation_des: str,
    deps: Deps,
) -> tuple[Optional[NumericContext], Optional[PartIdentityContext]]:
    """提取零件身份并查询单重；表格未命中时仍保留模型判断作用域。"""
    index = deps.part_weight_index
    if index is None or not getattr(index, "available", False):
        return None, None

    cache_key = rules.normalize(operation_des)
    try:
        if cache_key in deps.part_name_cache:
            part_name = deps.part_name_cache[cache_key]
        else:
            part_name = await deps.llm_extract_part_name(operation_des)
            deps.part_name_cache[cache_key] = part_name
        if not part_name:
            logger.info("  [PartWeight] LLM 未提取到零件，沿用原分析")
            return None, None

        identity_context = _part_identity_context(
            part_name,
            source="single-operation-extraction",
        )
        try:
            match = await deps.part_weight_pool.match(part_name, index.match)
        except Exception:
            logger.exception(
                "  [PartWeight] 重量检索失败，保留零件身份供模型重量池使用"
            )
            return None, identity_context
        if match is None:
            logger.info(
                "  [PartWeight] 未取得可靠单重: extracted=%r，"
                "保留零件身份供模型重量池使用",
                part_name,
            )
            return None, identity_context
        logger.info(
            "  [PartWeight] 命中: %r -> %r / %s kg / %s=%.4f",
            part_name,
            match.matched_name,
            match.weight_kg,
            match.match_type,
            match.similarity,
        )
        canonical_identity = _matched_part_identity_context(
            part_name,
            match,
            source="single-operation-weight-match",
        )
        return (
            _numeric_context_from_match(part_name, match),
            canonical_identity or identity_context,
        )
    except Exception:
        logger.exception("  [PartWeight] 零件提取/重量检索失败，沿用原分析")
        return None, None


async def _part_weight_context(
    operation_des: str,
    deps: Deps,
) -> Optional[NumericContext]:
    """兼容旧调用方：只返回可靠的表格单重上下文。"""
    numeric_context, _ = await _part_lookup_context(operation_des, deps)
    return numeric_context


async def resolve_part_weight_groups(
    parent_operation: str,
    child_operations: Sequence[str],
    deps: Deps,
) -> PartWeightGroupResolution:
    """父工序级统一识别零件；单重未命中时仍把身份传给模型重量池。"""
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
    cacheable = True
    try:
        extracted = await deps.llm_extract_part_groups(
            parent_operation,
            children,
        )
        groups = getattr(extracted, "groups", extracted) or ()
        contexts_by_name: dict[str, NumericContext] = {}
        identities_by_name: dict[str, PartIdentityContext] = {}
        identities_by_key: dict[str, PartIdentityContext] = {}
        ambiguous_indexes = set()
        for group_number, group in enumerate(groups, start=1):
            part_name = str(getattr(group, "part_name", "") or "").strip()
            normalized_name = normalize_part_name(part_name)
            if not normalized_name:
                continue
            identity_name_key = normalize_model_weight_identity(part_name)
            group_id = f"G{group_number}"
            identity_context = identities_by_name.get(identity_name_key)
            if identity_context is None:
                identity_context = _part_identity_context(
                    part_name,
                    group_id=group_id,
                    source="parent-operation-group",
                )
                if identity_context is None:
                    continue
                identity_context = identities_by_key.setdefault(
                    identity_context.identity_key,
                    identity_context,
                )
                identities_by_name[identity_name_key] = identity_context

            child_indexes: list[int] = []
            for raw_index in getattr(group, "child_indexes", ()):
                try:
                    child_index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if child_index < 1 or child_index > len(children):
                    continue
                if child_index in ambiguous_indexes:
                    continue
                if child_index in resolution.identity_contexts:
                    resolution.identity_contexts.pop(child_index, None)
                    resolution.contexts.pop(child_index, None)
                    ambiguous_indexes.add(child_index)
                    logger.warning(
                        "  [PartWeightGroup] 子工序 %s 被分配到多个零件，"
                        "已取消零件身份和自动重量",
                        child_index,
                    )
                    continue
                resolution.identity_contexts[child_index] = identity_context
                child_indexes.append(child_index)

            match = await deps.part_weight_pool.match(part_name, index.match)
            if match is None:
                logger.info(
                    "  [PartWeightGroup] 未取得可靠单重: part=%r，"
                    "保留身份给模型重量池 / children=%s",
                    part_name,
                    sorted(child_indexes),
                )
                continue
            canonical_identity = _matched_part_identity_context(
                part_name,
                match,
                group_id=group_id,
                source="parent-operation-weight-match",
            )
            if canonical_identity is not None:
                identity_context = identities_by_key.setdefault(
                    canonical_identity.identity_key,
                    canonical_identity,
                )
                identities_by_name[identity_name_key] = identity_context
                for child_index in child_indexes:
                    if child_index not in ambiguous_indexes:
                        resolution.identity_contexts[
                            child_index
                        ] = identity_context
            context = contexts_by_name.get(identity_name_key)
            if context is None:
                context = _numeric_context_from_match(
                    part_name,
                    match,
                    group_id=group_id,
                )
                contexts_by_name[identity_name_key] = context
            for child_index in child_indexes:
                if child_index in ambiguous_indexes:
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
        cacheable = False
        logger.exception(
            "  [PartWeightGroup] 父工序零件分组失败，整组不自动施加重量"
        )

    if isinstance(cache, dict) and cacheable:
        cache[cache_key] = resolution
    return resolution


async def resolve(
    el: StdsElement,
    deps: Deps,
    *,
    machine_hint: Optional[bool] = None,
    numeric_context: Optional[NumericContext] = None,
    part_identity_context: Optional[PartIdentityContext] = None,
    part_context_resolved: bool = False,
) -> StdsResult:
    """单条记录:从输入到产出。"""
    op = el.operation_des
    logger.info(f"===== 开始分析: '{op}' (freq={el.freq}) =====")

    # 明示设备动作必须先于缓存、经验、重量和公式链路返回。即使上游留下了
    # 错误的 False hint，也不能让设备工序泄漏成人工公式结果。
    explicit_machine = rules.is_explicit_machine_action(op)
    if machine_hint is True or explicit_machine:
        reason = "拆解阶段判定" if machine_hint is True else "明示设备主体/自主动作"
        logger.info("  [T2] %s: 设备", reason)
        return StdsResult.machine_placeholder(el)

    # 重量必须在任何完整决策复用之前解析，否则同一动作文本可能先命中没有
    # 重量上下文的旧缓存。part_context_resolved 只代表上游已尝试，并不等于
    # 确实存在重量。
    if (
        numeric_context is None
        and part_identity_context is None
        and not part_context_resolved
    ):
        numeric_context, part_identity_context = await _part_lookup_context(
            op,
            deps,
        )
    part_scoped = (
        numeric_context is not None
        or part_identity_context is not None
    )
    experience_scoped = _experience_enabled(deps)

    # T0 精确缓存。同一上传摘要和 Common 开关共享；可靠单重或仅有零件
    # 身份时都绕过，防止完整旧决策在到达模型重量池前提前返回。
    cached = None if part_scoped else _get_cache_template(el, deps)
    if part_scoped:
        logger.info("  [T0] 零件重量作用域已确定，跳过动作缓存")
    elif cached is not None:
        logger.info(f"  [T0] 缓存命中: {cached.chartcode} / {cached.time_s}s")
        # 新缓存恒为 freq=1；兼容当前进程里热更新前留下的旧缓存对象。
        time_single = cached.time_s if cached.freq == 1.0 else cached.time_s / (cached.freq or 1.0)
        return replace(
            cached,
            element=el,
            time_s=round(time_single * el.freq, 2),
            freq=el.freq,
        )
    else:
        logger.debug(f"  [T0] 缓存未命中")

    # T0.5 只使用随当前经验文件上传的 Common_Chart。经验索引是否可用不
    # 影响 Common；它本身就是同一上传上下文内更高优先级的完整决策。
    cc_hit = None
    if deps.use_common_chart and not part_scoped:
        semantic_enabled = bool(
            getattr(deps, "use_semantic_experience", True)
        )
        semantic_index_failed = False
        if semantic_enabled and deps.common_index is not None:
            try:
                cc_hit = await deps.common_index.match(op)
            except Exception:
                semantic_index_failed = True
                logger.exception(
                    "  [T0.5] Common 语义索引失败，回退纯关键词匹配"
                )
        if cc_hit is None and (
            not semantic_enabled
            or deps.common_index is None
            or semantic_index_failed
        ):
            from stds.experience.common_chart import match_common_chart

            cc_hit = match_common_chart(op, deps.common_entries)
    if part_scoped:
        logger.info("  [T0.5] 零件重量作用域已确定，跳过 Common Chart")
    elif not deps.use_common_chart:
        logger.info("  [T0.5] Common Chart 已关闭，跳过")
    elif not deps.common_entries:
        logger.info("  [T0.5] 当前上传上下文没有有效 Common_Chart，跳过")
    if cc_hit is not None:
        entry = cc_hit.entry
        chart = deps.charts.get(entry.chartcode)
        if chart is None:
            logger.warning(
                "  [T0.5] 上传 Common Chartcode=%r 不在当前图表库，继续 T1",
                entry.chartcode,
            )
            entry = None
        if entry is not None:
            try:
                kind = getattr(entry.kind, "value", str(entry.kind))
                common_source = (
                    getattr(deps.experience_index, "source_name", "")
                    or deps.experience_scope
                )
                common_trace = (
                    "T0.5_common",
                    entry.operation_label,
                    (
                        f"match={cc_hit.match_type};keyword={cc_hit.keyword};"
                        f"similarity={float(cc_hit.similarity):.4f};"
                        f"sheet=Common_Chart;row={entry.row};"
                        f"source={common_source}"
                    ),
                )
                if kind == "fixed_time":
                    time_single = float(entry.time_s)
                    decision_trace = [(
                        "EST_FIXED_TIME",
                        entry.decision,
                        (
                            f"source_time={entry.source_time_s:g};"
                            f"source_frequency={entry.frequency:g};"
                            f"unit_time={time_single:g}"
                        ),
                    )]
                else:
                    values, decision_trace = decode_strict_with_trace(
                        chart,
                        entry.decision,
                    )
                    time_single = evaluate(chart, values)
                logger.info(
                    "  [T0.5] Common 命中: %s / %r / %ss / %s",
                    entry.chartcode,
                    entry.decision,
                    time_single,
                    kind,
                )
                common_confidence = (
                    max(0.0, min(1.0, float(cc_hit.similarity)))
                    if cc_hit.match_type == "semantic"
                    else 0.99
                )
                res = StdsResult(
                    element=el,
                    chartcode=entry.chartcode,
                    decision=entry.decision,
                    time_s=round(time_single * el.freq, 2),
                    cv=entry.cv,
                    freq=el.freq,
                    source=Source.CACHE,
                    confidence=common_confidence,
                    needs_review=common_confidence < 0.75,
                    trace=[common_trace, *decision_trace],
                )
                _put_cache_template(el, res, deps, time_single)
                return res
            except Exception as exc:
                logger.warning("  [T0.5] Common 求值失败: %s，继续 T1", exc)

    # T1 历史 kNN 只在上传经验不会影响选码/参数时运行。否则它会提前复用
    # 完整决策，绕开应全局生效的参数选择经验。
    if deps.history_index is not None and not part_scoped and not experience_scoped:
        hits = await deps.history_index.knn(op, k=5)
        if hits:
            logger.debug(f"  [T1] kNN top3: {[(h.chartcode, round(h.score,3)) for h in hits[:3]]}")
        if hits and hits[0].score >= 0.92:
            top_cc = hits[0].chartcode
            consistent = all(h.chartcode == top_cc for h in hits[:3])
            if (
                consistent
                and top_cc in deps.charts
                and not is_experience_only_chartcode(top_cc)
            ):
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
        reason = (
            "上传的选码/参数经验已启用"
            if experience_scoped
            else "零件重量作用域已确定"
        )
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

    # T3E 只用语义向量 Top1 匹配 Chartcode 经验；低于 0.70 才交给 LLM。
    chart_experience_context, experience_cc = await _match_experience(op, deps)
    chart_experience_mode = ""
    if chart_experience_context is not None:
        cc = experience_cc
        chart_experience_mode = "semantic-top1;threshold=0.70"
        logger.info(
            "  [T3E] 经验选码: %s / operation=%r / experience_id=%s",
            cc,
            getattr(chart_experience_context, "operation_label", ""),
            getattr(chart_experience_context, "experience_id", ""),
        )
    else:
        # 没有唯一可靠动作经验时沿用原 LLM 选择。
        llm_charts = general_chart_candidates(deps.charts)
        cc = await deps.llm_select_chartcode(op, llm_charts)
        if is_experience_only_chartcode(cc):
            logger.warning(
                "  [T3] 普通 LLM 选择器返回经验专用 Chartcode=%r，已拒绝",
                cc,
            )
            cc = None
    chart: Optional[MostChart] = deps.charts.get(cc) if cc else None
    if chart is None:
        logger.warning(f"  [T3] LLM 选码失败(cc={cc}),进 unresolved")
        return StdsResult.unresolved(el, cc).mark_review()
    if chart_experience_context is None:
        logger.info(f"  [T3] LLM 选码: {cc} / '{chart.title}'")

    # 经验选码时严格沿用同一经验的参数；LLM 选码时把该码下所有参数经验
    # 一次性交给模型选择一整条，再贯穿整个 Vn 遍历。
    parameter_experience_context, parameter_experience_mode = (
        await _match_parameter_experience(
            op,
            deps,
            chartcode=cc,
            chart_experience_context=chart_experience_context,
        )
    )
    if parameter_experience_context is not None:
        logger.info(
            "  [T3P] 参数经验: %s / operation=%r / experience_id=%s / mode=%s",
            cc,
            getattr(parameter_experience_context, "operation_label", ""),
            getattr(parameter_experience_context, "experience_id", ""),
            parameter_experience_mode,
        )

    has_experience_context = (
        chart_experience_context is not None
        or parameter_experience_context is not None
    )
    experience_source = (
        str(getattr(deps.experience_index, "source_name", "") or "")
        if has_experience_context
        else ""
    )
    if has_experience_context and not experience_source:
        experience_source = "uploaded-experience"

    # T4 决策树(traverse -> pick_value -> evaluate)
    try:
        values, abbrevs, trace = await traverse(
            chart,
            op,
            deps.llm_pick_value,
            numeric_context=numeric_context,
            part_identity_context=part_identity_context,
            model_weight_pool=deps.model_weight_pool,
            experience_context=parameter_experience_context,
            experience_source=experience_source,
        )
    except EngineError as e:
        logger.error(f"  [T4] 决策树遍历失败: {e},进 unresolved")
        unresolved = StdsResult.unresolved(el, cc).mark_review()
        experience_trace = []
        if chart_experience_context is not None:
            experience_trace.append(
                _experience_trace(
                    chart_experience_context,
                    chartcode=cc,
                    source_name=experience_source,
                    selection_mode=(
                        f"{chart_experience_mode};traverse-error"
                    ),
                )
            )
        if parameter_experience_context is not None:
            experience_trace.append(
                _parameter_experience_trace(
                    parameter_experience_context,
                    chartcode=cc,
                    source_name=experience_source,
                    selection_mode=(
                        f"{parameter_experience_mode};traverse-error"
                    ),
                )
            )
        if experience_trace:
            unresolved.trace = [
                *experience_trace,
                ("UNRESOLVED", cc, str(e)),
            ]
        return unresolved

    time_single = evaluate(chart, values)
    decision = encode(abbrevs)
    model_weight_out_of_range = any(
        "model-weight-pool:clamped-high-review" in str(step[2])
        for step in trace
        if isinstance(step, (tuple, list)) and len(step) >= 3
    )
    conf = 0.65 if model_weight_out_of_range else 0.9
    if model_weight_out_of_range:
        trace.append(
            (
                "ModelWeightReview",
                "缓存重量超过当前 Chartcode 最大重量档",
                "已临时选择最大档，必须人工复核以避免低估",
            )
        )

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
    elif part_identity_context is not None:
        trace.insert(
            0,
            (
                "PartIdentity",
                part_identity_context.part_name,
                (
                    f"identity={part_identity_context.identity_key};"
                    f"source={part_identity_context.source};"
                    f"group={part_identity_context.group_id or 'single'};"
                    "weight_source=model-global-pool"
                ),
            ),
        )
    experience_trace = []
    if chart_experience_context is not None:
        experience_trace.append(
            _experience_trace(
                chart_experience_context,
                chartcode=cc,
                source_name=experience_source,
                selection_mode=chart_experience_mode,
            )
        )
    if parameter_experience_context is not None:
        experience_trace.append(
            _parameter_experience_trace(
                parameter_experience_context,
                chartcode=cc,
                source_name=experience_source,
                selection_mode=parameter_experience_mode,
            )
        )
    if experience_trace:
        trace[0:0] = experience_trace

    res = StdsResult(
        element=el, chartcode=cc, decision=decision,
        time_s=round(time_single * el.freq, 2),
        cv="C" if chart.value_added else "V", freq=el.freq,
        source=Source.FORMULA, confidence=conf,
        needs_review=model_weight_out_of_range or conf < 0.75, trace=trace,
    )
    if not part_scoped:
        _put_cache_template(el, res, deps, time_single)
    logger.info(f"  → 结果: {res.time_s}s (source={res.source.value}, needs_review={res.needs_review})")
    return res
