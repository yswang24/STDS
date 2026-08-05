"""LLM 分类型变量选值:只返回下标(index),数值/跳转指针全来自 DB。"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from stds.cascade import numeric
from stds.domain.models import ValueOption
from stds.llm.client import structured
from stds.llm.prompts import render_prompt

if TYPE_CHECKING:
    from stds.retrieval.model_weight_pool import ModelWeightPool

logger = logging.getLogger("stds.llm.pick_value")


class ValuePick(BaseModel):
    index: int
    reason: str


_DEFAULT_PATTERNS = (
    re.compile(
        r"(?:默认(?:选择|为|取|使用)?|缺省(?:选择|为|取|使用)?)"
        r"\s*[：:=为是]?\s*([^；;。\n，,]+)",
        re.I,
    ),
    re.compile(r"\bdefault\s*[:=]\s*([^；;。\n，,]+)", re.I),
    re.compile(
        r"(?:选择|使用|取)\s*([^；;。\n，,()（）]+)"
        r"\s*[（(]\s*默认(?:值)?\s*[）)]",
        re.I,
    ),
)
_CHOICE_FRAGMENT_PATTERN = re.compile(
    r"(?:默认)?(?:选择|采用|使用|取)\s*([^；;。\n，,]+)",
    re.I,
)


def _normalize_option_text(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"(?:degrees?|度|°)", "", text)
    return re.sub(r"[\s,，。.;；:：()（）/\\_-]+", "", text)


def _candidate_for_default(fragment: str, candidates: list):
    """把一个明确默认值解析回当前候选；不唯一时绝不自动选择。"""
    target = _normalize_option_text(fragment)
    if not target:
        return None

    def candidate_keys(candidate) -> tuple[str, ...]:
        return tuple(
            key
            for key in (
                _normalize_option_text(getattr(candidate, "description", "")),
                _normalize_option_text(getattr(candidate, "metric_abbrev", "")),
            )
            if key
        )

    exact = [
        candidate
        for candidate in candidates
        if target in candidate_keys(candidate)
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    contained = [
        candidate
        for candidate in candidates
        if any(
            len(target) >= 3 and (target in key or key in target)
            for key in candidate_keys(candidate)
        )
    ]
    if len(contained) == 1:
        return contained[0]

    # “180°”之类经验默认值与候选“180”可以稳定对应；多个候选含同值时
    # 仍视为歧义，不做确定性选择。
    target_numbers = re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", fragment)
    if len(target_numbers) == 1:
        numeric_hits = []
        for candidate in candidates:
            candidate_numbers = re.findall(
                r"(?<!\d)\d+(?:\.\d+)?(?!\d)",
                str(getattr(candidate, "description", "") or ""),
            )
            if target_numbers[0] in candidate_numbers:
                numeric_hits.append(candidate)
        if len(numeric_hits) == 1:
            return numeric_hits[0]
    return None


def _experience_default_choice(experience_hint: object, candidates: list):
    """仅对没有条件分支歧义的唯一默认值做确定性候选匹配。"""
    hint = str(experience_hint or "").strip()
    if not hint:
        return None

    # Loader 允许参数单元格直接写候选值（例如 ``Turn``、``180``、
    # ``No Bend``）。这类提示本身就是完整选择，不应再次交给 LLM；否则
    # 同一份参数经验会因模型波动偶尔选到其他档。含条件、选择语句或多个
    # 分句的自然语言仍按下面的保守规则处理。
    if not re.search(
        r"(?:如果|若|否则|当|默认|缺省|选择|采用|使用|取|[；;。\n，,])",
        hint,
        re.I,
    ):
        direct = _candidate_for_default(hint, candidates)
        if direct is not None:
            return direct

    # 同一提示若还明确选择了其他候选，说明它包含条件分支。此时不能
    # 无条件套用默认值，交给 LLM 结合操作描述判断分支。
    mentioned = []
    for fragment_match in _CHOICE_FRAGMENT_PATTERN.finditer(hint):
        fragment = re.sub(
            r"[（(]\s*默认(?:值)?\s*[）)]",
            "",
            fragment_match.group(1),
            flags=re.I,
        )
        target = _normalize_option_text(fragment)
        exact = [
            candidate
            for candidate in candidates
            if target
            and target
            in {
                _normalize_option_text(candidate.description),
                _normalize_option_text(candidate.metric_abbrev),
            }
        ]
        fragment_candidates = exact
        if not fragment_candidates:
            fragment_candidates = [
                candidate
                for candidate in candidates
                if any(
                    len(key) >= 2 and key in target
                    for key in (
                        _normalize_option_text(candidate.description),
                        _normalize_option_text(candidate.metric_abbrev),
                    )
                    if key
                )
            ]
        for candidate in fragment_candidates:
            if not any(candidate is existing for existing in mentioned):
                mentioned.append(candidate)
    if len(mentioned) > 1:
        return None

    matched = []
    for pattern in _DEFAULT_PATTERNS:
        for match in pattern.finditer(hint):
            candidate = _candidate_for_default(match.group(1), candidates)
            if candidate is not None and not any(
                candidate is existing for existing in matched
            ):
                matched.append(candidate)
    return matched[0] if len(matched) == 1 else None


def _experience_explicit_angle_choice(
    op_des: str,
    experience_hint: object,
    experience_context,
    candidates: list,
):
    """按本动作、本 Vn 的角度规则提取明示角度，避免默认值覆盖事实。"""
    hint = str(experience_hint or "")
    if "角度" not in hint:
        return None
    angle_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|度)", op_des)
    if angle_match is None:
        return None

    # 同一 202 010 中，转身 V2 与弯腰 V3 都是角度。提示若明确说的是
    # 另一动作的角度，只有描述真的包含该动作时才允许使用这个数。
    hinted_actions = [
        action for action in ("转身", "弯腰")
        if f"{action}角度" in hint
    ]
    operation_label = str(
        getattr(experience_context, "operation_label", "") or ""
    )
    if hinted_actions and not any(
        action == operation_label or action in op_des
        for action in hinted_actions
    ):
        return None

    candidate_values = []
    for candidate in candidates:
        numbers = re.findall(
            r"(?<!\d)\d+(?:\.\d+)?(?!\d)",
            str(getattr(candidate, "description", "") or ""),
        )
        if len(numbers) == 1:
            candidate_values.append((float(numbers[0]), candidate))
    if not candidate_values:
        return None
    requested = float(angle_match.group(1))
    return min(candidate_values, key=lambda item: abs(item[0] - requested))[1]


def _experience_reason(
    experience_context,
    experience_source: str,
    experience_hint: object,
) -> str:
    rule = " ".join(str(experience_hint or "").split())
    return (
        f"experience_id={getattr(experience_context, 'experience_id', '')};"
        f"operation={getattr(experience_context, 'operation_label', '')};"
        f"source={experience_source or 'uploaded-experience'};"
        f"parameter_row={getattr(experience_context, 'parameter_row', '')};"
        f"rule={rule[:240]}"
    )


async def pick_value(
    op_des: str,
    candidates: list,
    *,
    numeric_context: Optional[numeric.NumericContext] = None,
    experience_hint: Optional[str] = None,
    experience_context=None,
    experience_source: str = "",
    part_identity_context=None,
    model_weight_pool: Optional["ModelWeightPool"] = None,
) -> tuple:
    """(ValueOption, confidence, reason)。

    分支:单候选 / 零件单重 / 明示数值 / 经验辅助 / 普通 LLM。
    """
    if len(candidates) == 1:
        logger.debug(f"  [pick] 单候选免LLM: {candidates[0].description}")
        return candidates[0], 1.0, "single-candidate"

    if numeric_context is not None:
        weight_hit = numeric.select_weight_range(
            numeric_context.weight_kg,
            candidates,
        )
        if weight_hit is not None:
            hit, band_kg = weight_hit
            reason = (
                "part-weight:"
                f"query={numeric_context.query_name};"
                f"matched={numeric_context.matched_name};"
                f"match_type={numeric_context.match_type};"
                f"similarity={numeric_context.similarity:.4f};"
                f"weight_kg={numeric_context.weight_kg:g};"
                f"band_kg={band_kg:g};"
                f"source={numeric_context.source}"
            )
            logger.debug(
                "  [pick] 零件单重确定性: %s kg -> %s (%s kg)",
                numeric_context.weight_kg,
                hit.description,
                band_kg,
            )
            return hit, 0.98, reason

    # 数值精确匹配(操作描述有明确数值,如"7m"、"18in")
    for kind, val in numeric.parse_numerics(op_des):
        hit = numeric.select_numeric_range(kind, val, candidates)
        if hit is not None:
            logger.debug(
                "  [pick] 数值确定性: %s=%s -> %s (fv=%s)",
                kind,
                val,
                hit.description,
                hit.formula_value,
            )
            return hit, 0.95, f"numeric:{kind}={val}"

    # 经验只能辅助当前 ExperienceContext 的当前 Vn。明确且唯一的默认值
    # 可确定性选择；其余自然语言经验交给 LLM，并且始终限制在当前候选内。
    if experience_hint and experience_context is not None:
        explicit_angle_hit = _experience_explicit_angle_choice(
            op_des,
            experience_hint,
            experience_context,
            candidates,
        )
        experience_hit = _experience_default_choice(experience_hint, candidates)
        experience_reason = _experience_reason(
            experience_context,
            experience_source,
            experience_hint,
        )
        if explicit_angle_hit is not None:
            logger.debug(
                "  [pick] 经验规则提取明示角度: %s (%s)",
                explicit_angle_hit.description,
                experience_reason,
            )
            return (
                explicit_angle_hit,
                0.96,
                f"experience-explicit-angle:{experience_reason}",
            )
        if experience_hit is not None:
            logger.debug(
                "  [pick] 经验默认值确定性选择: %s (%s)",
                experience_hit.description,
                experience_reason,
            )
            return (
                experience_hit,
                0.9,
                f"experience-default:{experience_reason}",
            )
        experience_block = (
            "以下经验只属于本次命中的动作身份，且只适用于当前变量；"
            "不得借用同一 Chartcode 下其他动作的经验。\n"
            f"动作身份：{getattr(experience_context, 'operation_label', '')}\n"
            f"经验ID：{getattr(experience_context, 'experience_id', '')}\n"
            f"当前变量经验：{experience_hint}\n"
            "若经验与操作描述中的明确事实冲突，以操作描述为准。"
        )
    else:
        experience_reason = ""
        experience_block = "无可用的当前动作、当前变量经验。"

    # LLM 选(所有类型,包括数值型)
    menu = "\n".join(f"[{i}] {c.description}" for i, c in enumerate(candidates))
    logger.debug(f"  [pick] LLM 选值, {len(candidates)} 个候选")
    prompt = render_prompt(
        "pick_value",
        op=op_des,
        menu=menu,
        experience=experience_block,
    )
    async def choose_with_llm() -> tuple:
        out: ValuePick = await structured(prompt, ValuePick)
        idx = min(max(out.index, 0), len(candidates) - 1)
        chosen = candidates[idx]
        logger.debug(
            "  [pick] LLM 选择: [%s] %s (fv=%s, reason=%s)",
            idx,
            chosen.description,
            chosen.formula_value,
            out.reason,
        )
        reason = out.reason
        if experience_reason:
            reason = (
                f"experience-assisted:{experience_reason};"
                f"llm_reason={out.reason}"
            )
        return chosen, 0.7, reason

    # Only the final LLM fallback participates.  The pool performs its own
    # identity and all-candidates-are-kilogram-bands checks, so existing callers
    # and non-weight variables retain their exact behavior.
    if (
        model_weight_pool is not None
        and part_identity_context is not None
        and model_weight_pool.supports(part_identity_context, candidates)
    ):
        return await model_weight_pool.resolve(
            part_identity_context,
            candidates,
            choose_with_llm,
        )
    return await choose_with_llm()
