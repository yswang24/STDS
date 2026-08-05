"""Request-scoped consistency pool for LLM-selected weight bands.

The part-weight workbook remains the authoritative source whenever it yields a
``NumericContext``.  This pool covers the narrower fallback case where a
decision variable is clearly a set of physical kilogram bands, but the final
choice still has to be made by the LLM.  It remembers the *physical band* (not
the database option object), so the same part can be mapped consistently even
when another Chartcode exposes a different set of weight options.
"""
from __future__ import annotations

import asyncio
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from stds.cascade.numeric import candidate_weight_kg, candidate_weight_lbs


ModelWeightChoice = tuple[object, float, str]
ModelWeightChooser = Callable[[], Awaitable[ModelWeightChoice]]


@dataclass(frozen=True)
class ModelWeightDecision:
    """Canonical first decision stored for one normalized part identity."""

    band_kg: float
    source: str
    part_name: str = ""
    group_id: str = ""
    band_lbs: Optional[float] = None


def _context_text(context: object, name: str) -> str:
    value = getattr(context, name, "")
    if callable(value):
        value = value()
    return str(value or "").strip()


def normalize_model_weight_identity(value: object) -> str:
    """保守归一化物理件身份，保留有区分力的标点。

    名称检索可以忽略标点以提高召回，但全局重量缓存不能把 ``P-1`` 与
    ``P1``、``A/B`` 与 ``AB`` 合并。这里只统一全半角、大小写和空白。
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", "", text)


def model_weight_identity_key(context: object) -> str:
    """Return the stable normalized identity exposed by a context-like object.

    ``PartIdentityContext`` intentionally is not imported here: keeping this
    helper duck-typed avoids a dependency cycle between the cascade and
    retrieval packages.  ``identity_key`` is authoritative when supplied;
    ``part_name`` and finally ``group_id`` are safe compatibility fallbacks.
    """

    if context is None:
        return ""
    raw = (
        _context_text(context, "identity_key")
        or _context_text(context, "part_name")
        or _context_text(context, "group_id")
    )
    return normalize_model_weight_identity(raw)


def _parsed_candidates(candidates: list) -> Optional[list[tuple[float, int, object]]]:
    parsed: list[tuple[float, int, object]] = []
    for index, candidate in enumerate(candidates):
        weight = candidate_weight_kg(candidate)
        if weight is None or not math.isfinite(weight) or weight <= 0:
            return None
        parsed.append((float(weight), index, candidate))
    return parsed or None


def _same_weight(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


class ModelWeightPool:
    """Unify final LLM weight-band choices within one request.

    A lock per normalized identity provides singleflight behavior.  A failed or
    cancelled chooser call is deliberately not cached.  Different identities
    never share a lock or decision.
    """

    def __init__(self) -> None:
        self._decisions: dict[str, ModelWeightDecision] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def supports(part_identity_context: object, candidates: list) -> bool:
        """Whether this fallback is safe for the supplied variable."""

        return bool(
            model_weight_identity_key(part_identity_context)
            and _parsed_candidates(candidates) is not None
        )

    @staticmethod
    def _mapped_choice(
        decision: ModelWeightDecision,
        parsed: list[tuple[float, int, object]],
    ) -> ModelWeightChoice:
        exact = [item for item in parsed if _same_weight(item[0], decision.band_kg)]
        if exact:
            band_kg, _, candidate = min(exact, key=lambda item: item[1])
            mode = "mapped-exact"
        else:
            # 不同数据库图表对同一磅档可能使用不同公斤换算值，例如
            # 1 lb 同时写成 0.45 kg 和 0.4357 kg。共同的磅档比固定百分比
            # 容差更可靠，必须先于公斤向上覆盖规则。
            equivalent_lbs = []
            if decision.band_lbs is not None:
                equivalent_lbs = [
                    item
                    for item in parsed
                    if (
                        (candidate_lbs := candidate_weight_lbs(item[2]))
                        is not None
                        and _same_weight(candidate_lbs, decision.band_lbs)
                    )
                ]
            if equivalent_lbs:
                band_kg, _, candidate = min(
                    equivalent_lbs,
                    key=lambda item: item[1],
                )
                mode = "mapped-equivalent-lbs"
                reason = (
                    "model-weight-pool:"
                    f"{mode};cached_band_kg={decision.band_kg:g};"
                    f"cached_band_lbs={decision.band_lbs:g};"
                    f"band_kg={band_kg:g};source={decision.source};"
                    f"part={decision.part_name};group_id={decision.group_id}"
                )
                return candidate, 0.7, reason

            # 先吸收不同图表中 3.2kg/3.15kg 一类小幅标注差异；明显不同
            # 时按重量档上限语义向上覆盖，避免把 0.45kg 判断映成 0.2kg。
            minimum_distance = min(
                abs(item[0] - decision.band_kg) for item in parsed
            )
            nearest = [
                item
                for item in parsed
                if math.isclose(
                    abs(item[0] - decision.band_kg),
                    minimum_distance,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            ]
            nearest_band, _, nearest_candidate = min(
                nearest,
                key=lambda item: (-item[0], item[1]),
            )
            weights = [item[0] for item in parsed]
            near_tolerance = max(0.01, decision.band_kg * 0.02)
            if minimum_distance <= near_tolerance:
                band_kg, candidate = nearest_band, nearest_candidate
                mode = "mapped-near"
            elif decision.band_kg < min(weights):
                band_kg, _, candidate = min(
                    parsed,
                    key=lambda item: (item[0], item[1]),
                )
                mode = "clamped-low"
            elif decision.band_kg > max(weights):
                band_kg, _, candidate = max(
                    parsed,
                    key=lambda item: (item[0], -item[1]),
                )
                mode = "clamped-high-review"
            else:
                band_kg, _, candidate = min(
                    (
                        item for item in parsed
                        if item[0] >= decision.band_kg
                    ),
                    key=lambda item: (item[0], item[1]),
                )
                mode = "mapped-ceiling"
        reason = (
            "model-weight-pool:"
            f"{mode};cached_band_kg={decision.band_kg:g};"
            f"band_kg={band_kg:g};source={decision.source};"
            f"part={decision.part_name};group_id={decision.group_id}"
        )
        confidence = 0.55 if mode == "clamped-high-review" else 0.7
        return candidate, confidence, reason

    async def resolve(
        self,
        part_identity_context: object,
        candidates: list,
        chooser: ModelWeightChooser,
    ) -> ModelWeightChoice:
        """Choose once per part and map later variables to that physical band.

        Callers should invoke this only for the final LLM fallback.  For safety,
        this method itself also falls straight through when identity or complete
        kilogram-band parsing is unavailable.
        """

        key = model_weight_identity_key(part_identity_context)
        parsed = _parsed_candidates(candidates)
        if not key or parsed is None:
            return await chooser()

        cached = self._decisions.get(key)
        if cached is not None:
            return self._mapped_choice(cached, parsed)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._decisions.get(key)
            if cached is not None:
                return self._mapped_choice(cached, parsed)

            # If chooser raises (including CancelledError), control exits before
            # the assignment below and a later call can retry.
            chosen, confidence, reason = await chooser()
            selected = next(
                (item for item in parsed if item[2] is chosen),
                None,
            )
            if selected is None:
                # Preserve the existing chooser contract without caching an
                # external/invalid option.
                return chosen, confidence, reason

            band_kg = selected[0]
            band_lbs = candidate_weight_lbs(selected[2])
            source = _context_text(part_identity_context, "source")
            decision = ModelWeightDecision(
                band_kg=band_kg,
                source=source or "llm",
                part_name=_context_text(part_identity_context, "part_name"),
                group_id=_context_text(part_identity_context, "group_id"),
                band_lbs=band_lbs,
            )
            self._decisions[key] = decision
            first_reason = (
                "model-weight-pool:selected;"
                f"band_kg={band_kg:g};source={decision.source};"
                f"part={decision.part_name};group_id={decision.group_id};"
                f"llm_reason={reason}"
            )
            if band_lbs is not None:
                first_reason = (
                    "model-weight-pool:selected;"
                    f"band_kg={band_kg:g};band_lbs={band_lbs:g};"
                    f"source={decision.source};part={decision.part_name};"
                    f"group_id={decision.group_id};llm_reason={reason}"
                )
            return chosen, confidence, first_reason

    async def choose(
        self,
        part_identity_context: object,
        candidates: list,
        chooser: ModelWeightChooser,
    ) -> ModelWeightChoice:
        """Compatibility alias with a verb matching ``pick_value``."""

        return await self.resolve(part_identity_context, candidates, chooser)
