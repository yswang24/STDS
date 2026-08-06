"""零件名称提取、重量检索与公斤档选值测试。"""
from __future__ import annotations

import asyncio

from stds.cascade.numeric import (
    NumericContext,
    candidate_weight_kg,
    select_weight_range,
)
from stds.cascade.resolver import (
    Deps,
    _part_weight_context,
    resolve,
    resolve_part_weight_groups,
)
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.domain.models import (
    MostChart,
    Source,
    StdsElement,
    StdsResult,
    ValueOption,
)
from stds.llm.extract_part_name import (
    PartNameExtraction,
    PartOperationGroup,
    extract_part_groups,
    extract_part_name,
)
from stds.llm.pick_value import pick_value
from stds.retrieval.part_weight_index import (
    PartWeightIndex,
    PartWeightMatch,
    PartWeightRecord,
    PartWeightSource,
    load_part_weight_records,
)


class _Cell:
    def __init__(self, value):
        self.value = value


class _Worksheet:
    def __init__(self, title, headers, rows):
        self.title = title
        self._headers = headers
        self._rows = rows

    def iter_rows(self, *, min_row, max_row=None, values_only=False):
        if min_row == 1 and max_row == 1:
            return iter([tuple(_Cell(value) for value in self._headers)])
        assert min_row == 2 and values_only
        return iter(self._rows)


class _Workbook:
    def __init__(self, worksheets):
        self.worksheets = worksheets
        self.closed = False

    def close(self):
        self.closed = True


def _option(description, abbrev, formula_value):
    return ValueOption(
        variable_number=1,
        range_number=1,
        value_number=1,
        description=description,
        metric_abbrev=abbrev,
        formula_value=formula_value,
        next_variable=0,
        next_range=0,
    )


def test_loader_only_deduplicates_when_all_four_fields_match(
    monkeypatch,
    tmp_path,
):
    headers = [
        "index",
        "Part No.",
        "English Description",
        "Chinese Description",
        "零件单重(KG)",
    ]
    rows = [
        (1, "P1", "PART", "零件", 1.0),
        (2, "P1", "PART", "零件", 1.0),  # 四字段完全相同
        (3, "P1", "PART", "零件", 2.0),  # 重量不同
        (4, "P1", "PART", "零件二", 1.0),  # 名称不同
        (5, "P2", "PART", "零件", 1.0),  # 零件号不同
    ]
    workbook = _Workbook([_Worksheet("Sheet1", headers, rows)])
    monkeypatch.setattr(
        "stds.retrieval.part_weight_index.load_workbook",
        lambda *args, **kwargs: workbook,
    )
    source = tmp_path / "source.xlsx"
    source.touch()

    records = load_part_weight_records(source)

    assert len(records) == 4
    exact = next(
        record
        for record in records
        if record.part_no == "P1" and record.weight_kg == 1.0
        and record.chinese_name == "零件"
    )
    assert len(exact.sources) == 2
    assert exact.sources[0].cell == "E2"
    assert exact.sources[1].cell == "E3"
    assert workbook.closed


def test_exact_name_uses_only_one_consistent_positive_weight():
    index = PartWeightIndex(
        [
            PartWeightRecord("P1", "BDU", "BDU", 6.5),
            PartWeightRecord("P2", "BDU", "BDU", 0),
        ]
    )

    match = index.exact_match("bdu")

    assert match is not None
    assert match.weight_kg == 6.5
    assert match.match_type == "exact"


def test_same_matched_name_with_conflicting_positive_weights_is_unsafe():
    index = PartWeightIndex(
        [
            PartWeightRecord("P1", "PART", "零件", 1.0),
            PartWeightRecord("P2", "PART", "零件", 2.0),
        ]
    )

    assert index.exact_match("零件") is None


class _SemanticEmbed:
    @staticmethod
    def _vector(text):
        normalized = text.casefold()
        if "低压" in text or "low voltage" in normalized:
            return [1.0, 0.0]
        if "水冷" in text or "water" in normalized:
            return [0.0, 1.0]
        return [0.5, 0.5]

    def embed(self, texts):
        return [self._vector(text) for text in texts]

    def embed_one(self, text):
        return self._vector(text)


def test_semantic_name_match_is_in_memory_and_short_abbreviation_is_exact_only():
    index = PartWeightIndex(
        [
            PartWeightRecord("P1", "LOW VOLTAGE CABLE", "低压线束", 0.095),
            PartWeightRecord("P2", "WATER COOLING PLATE", "水冷板", 6.5),
        ],
        embed_backend=_SemanticEmbed(),
        similarity_threshold=0.85,
        similarity_margin=0.05,
    )

    match = asyncio.run(index.match("主低压线束"))

    assert match is not None
    assert match.matched_name == "低压线束"
    assert match.weight_kg == 0.095
    assert match.match_type == "semantic"
    assert index._semantic_vectors is not None
    assert asyncio.run(index.match("BDU")) is None


def test_failed_remote_embedding_cannot_create_weight_semantic_match():
    class _FailedRemote:
        def __init__(self):
            self._api_available = None

        def embed(self, texts):
            self._api_available = False
            return [[1.0, 0.0] for _ in texts]

        def embed_one(self, _text):
            return [1.0, 0.0]

    index = PartWeightIndex(
        [PartWeightRecord("P1", "LOW VOLTAGE CABLE", "低压线束", 0.095)],
        embed_backend=_FailedRemote(),
        similarity_threshold=-1.0,
        similarity_margin=0.0,
    )

    assert asyncio.run(index.semantic_match("主低压线束")) is None
    assert index._semantic_unavailable is True


def test_llm_extraction_ignores_quantity_and_rejects_hallucination(monkeypatch):
    calls = []

    async def fake_structured(prompt, schema):
        calls.append(prompt)
        return schema(part_name="螺栓", reason="被拿取的零件")

    monkeypatch.setattr(
        "stds.llm.extract_part_name.structured",
        fake_structured,
    )
    assert asyncio.run(extract_part_name("人工A拿取4个螺栓")) == "螺栓"
    assert "重量始终按一个零件的单重处理" in calls[0]

    async def hallucinated(prompt, schema):
        return PartNameExtraction(part_name="水冷板", reason="猜测")

    monkeypatch.setattr(
        "stds.llm.extract_part_name.structured",
        hallucinated,
    )
    assert asyncio.run(extract_part_name("人工A移动吊具")) is None


def test_llm_parent_group_assigns_tool_continuation_to_same_part(monkeypatch):
    async def fake_structured(prompt, schema):
        assert "[2] 人工A移动吊具" in prompt
        return schema(
            groups=[
                {
                    "part_name": "Tray",
                    "child_indexes": [1, 2, 3],
                    "reason": "同一吊运链",
                }
            ]
        )

    monkeypatch.setattr(
        "stds.llm.extract_part_name.structured",
        fake_structured,
    )
    groups = asyncio.run(
        extract_part_groups(
            "人工A用吊具将Tray落位到小车",
            (
                "人工A用吊具夹持Tray",
                "人工A移动吊具",
                "人工A落位Tray",
            ),
        )
    )

    assert len(groups) == 1
    assert groups[0].part_name == "Tray"
    assert groups[0].child_indexes == [1, 2, 3]


def test_llm_parent_group_drops_ambiguous_child_assignment(monkeypatch):
    async def fake_structured(prompt, schema):
        return schema(
            groups=[
                {
                    "part_name": "零件甲",
                    "child_indexes": [1],
                    "reason": "候选甲",
                },
                {
                    "part_name": "零件乙",
                    "child_indexes": [1, 2],
                    "reason": "候选乙",
                },
            ]
        )

    monkeypatch.setattr(
        "stds.llm.extract_part_name.structured",
        fake_structured,
    )
    groups = asyncio.run(
        extract_part_groups(
            "依次处理零件甲和零件乙",
            ("移动零件甲或零件乙", "安装零件乙"),
        )
    )

    assert len(groups) == 1
    assert groups[0].part_name == "零件乙"
    assert groups[0].child_indexes == [2]


def test_parent_group_matches_weight_once_and_shares_same_context_object():
    match_calls = []

    class WeightIndex:
        available = True

        async def match(self, query):
            match_calls.append(query)
            return PartWeightMatch(
                query=query,
                matched_name="低压线束",
                part_no="P1",
                weight_kg=0.095,
                similarity=0.96,
                match_type="semantic",
                sources=(PartWeightSource("DU", 40, "E40"),),
            )

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(
                part_name="主低压线束",
                child_indexes=[1, 2, 3],
                reason="同一零件操作链",
            ),
        )

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=WeightIndex(),
        llm_extract_part_groups=extract_groups,
    )
    resolution = asyncio.run(
        resolve_part_weight_groups(
            "人工A安装主低压线束",
            ("拿取主低压线束", "移动吊具", "安装主低压线束"),
            deps,
        )
    )

    assert resolution.attempted
    assert match_calls == ["主低压线束"]
    assert sorted(resolution.contexts) == [1, 2, 3]
    assert len({id(context) for context in resolution.contexts.values()}) == 1
    assert {context.weight_kg for context in resolution.contexts.values()} == {
        0.095
    }
    assert {context.group_id for context in resolution.contexts.values()} == {
        "G1"
    }


def test_two_parents_share_one_request_weight_match_but_keep_local_contexts():
    match_calls = 0

    class WeightIndex:
        available = True

        async def match(self, query):
            nonlocal match_calls
            match_calls += 1
            await asyncio.sleep(0)
            return PartWeightMatch(
                query=query,
                matched_name="低压线束",
                part_no="P1",
                weight_kg=0.095,
                similarity=0.96,
                match_type="semantic",
                sources=(PartWeightSource("DU", 40, "E40"),),
            )

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(
                part_name="主低压线束",
                child_indexes=[1],
                reason=parent,
            ),
        )

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=WeightIndex(),
        llm_extract_part_groups=extract_groups,
    )

    async def scenario():
        return await asyncio.gather(
            resolve_part_weight_groups(
                "人工A拿取主低压线束",
                ("拿取主低压线束",),
                deps,
            ),
            resolve_part_weight_groups(
                "人工B安装主低压线束",
                ("安装主低压线束",),
                deps,
            ),
        )

    first, second = asyncio.run(scenario())

    assert match_calls == 1
    assert first.contexts[1].weight_kg == second.contexts[1].weight_kg == 0.095
    assert first.contexts[1] is not second.contexts[1]
    assert first.contexts[1].query_name == "主低压线束"
    assert second.contexts[1].query_name == "主低压线束"


def test_direct_and_parent_group_weight_paths_share_request_pool():
    match_calls = 0

    class WeightIndex:
        available = True

        async def match(self, query):
            nonlocal match_calls
            match_calls += 1
            return PartWeightMatch(
                query=query,
                matched_name="低压线束",
                part_no="P1",
                weight_kg=0.095,
                similarity=0.96,
                match_type="semantic",
                sources=(),
            )

    async def extract_name(_):
        return "主低压线束"

    async def extract_groups(parent, children):
        return (
            PartOperationGroup(
                part_name="主 低压线束",
                child_indexes=[1],
                reason="同一零件",
            ),
        )

    deps = Deps(
        charts={},
        cache=AutoCache(),
        part_weight_index=WeightIndex(),
        llm_extract_part_name=extract_name,
        llm_extract_part_groups=extract_groups,
    )

    async def scenario():
        direct = await _part_weight_context("人工A拿取主低压线束", deps)
        grouped = await resolve_part_weight_groups(
            "人工A安装主低压线束",
            ("安装主低压线束",),
            deps,
        )
        return direct, grouped.contexts[1]

    direct, grouped = asyncio.run(scenario())

    assert match_calls == 1
    assert direct is not None
    assert direct is not grouped
    assert direct.query_name == "主低压线束"
    assert grouped.query_name == "主 低压线束"
    assert direct.weight_kg == grouped.weight_kg == 0.095


def test_request_weight_pool_single_flights_and_caches_clean_none():
    match_calls = 0
    deps = Deps(charts={}, cache=AutoCache())

    async def matcher(query):
        nonlocal match_calls
        match_calls += 1
        await asyncio.sleep(0.01)
        return None

    async def scenario():
        results = await asyncio.gather(
            deps.part_weight_pool.match("主低压线束", matcher),
            deps.part_weight_pool.match("主 低压线束", matcher),
            deps.part_weight_pool.match("主低压线束", matcher),
        )
        cached = await deps.part_weight_pool.match("主低压线束", matcher)
        return results, cached

    results, cached = asyncio.run(scenario())

    assert results == [None, None, None]
    assert cached is None
    assert match_calls == 1


def test_request_weight_pool_does_not_cache_exception_or_cancellation():
    deps = Deps(charts={}, cache=AutoCache())
    exception_calls = 0
    cancellation_calls = 0

    async def exception_matcher(query):
        nonlocal exception_calls
        exception_calls += 1
        if exception_calls == 1:
            raise RuntimeError("temporary")
        return PartWeightMatch(
            query=query,
            matched_name="低压线束",
            part_no="P1",
            weight_kg=0.095,
            similarity=1.0,
            match_type="exact",
            sources=(),
        )

    async def cancellation_matcher(query):
        nonlocal cancellation_calls
        cancellation_calls += 1
        if cancellation_calls == 1:
            raise asyncio.CancelledError
        return PartWeightMatch(
            query=query,
            matched_name="水冷板",
            part_no="P2",
            weight_kg=6.5,
            similarity=1.0,
            match_type="exact",
            sources=(),
        )

    async def scenario():
        try:
            await deps.part_weight_pool.match("低压线束别名", exception_matcher)
        except RuntimeError:
            pass
        retried_exception = await deps.part_weight_pool.match(
            "低压线束别名",
            exception_matcher,
        )
        try:
            await deps.part_weight_pool.match("主水冷板", cancellation_matcher)
        except asyncio.CancelledError:
            pass
        retried_cancellation = await deps.part_weight_pool.match(
            "主水冷板",
            cancellation_matcher,
        )
        return retried_exception, retried_cancellation

    retried_exception, retried_cancellation = asyncio.run(scenario())

    assert exception_calls == 2
    assert cancellation_calls == 2
    assert retried_exception.weight_kg == 0.095
    assert retried_cancellation.weight_kg == 6.5


def test_request_weight_pool_unifies_aliases_by_part_number_or_matched_name():
    async def run_case(
        *,
        first_part_no,
        second_part_no,
        first_matched_name,
        second_matched_name,
    ):
        calls = []
        deps = Deps(charts={}, cache=AutoCache())

        async def matcher(query):
            calls.append(query)
            if len(calls) == 1:
                return PartWeightMatch(
                    query=query,
                    matched_name=first_matched_name,
                    part_no=first_part_no,
                    weight_kg=0.095,
                    similarity=0.96,
                    match_type="semantic",
                    sources=(),
                )
            return PartWeightMatch(
                query=query,
                matched_name=second_matched_name,
                part_no=second_part_no,
                weight_kg=9.9,
                similarity=0.95,
                match_type="semantic",
                sources=(),
            )

        first = await deps.part_weight_pool.match("别名甲", matcher)
        second = await deps.part_weight_pool.match("别名乙", matcher)
        standard_name = await deps.part_weight_pool.match(
            second_matched_name,
            matcher,
        )
        return first, second, standard_name, calls

    by_part_no = asyncio.run(
        run_case(
            first_part_no="P1",
            second_part_no="P1",
            first_matched_name="低压线束",
            second_matched_name="LOW VOLTAGE HARNESS",
        )
    )
    by_matched_name = asyncio.run(
        run_case(
            first_part_no="",
            second_part_no="",
            first_matched_name="低压线束",
            second_matched_name="低压线束",
        )
    )

    for first, second, standard_name, calls in (by_part_no, by_matched_name):
        assert calls == ["别名甲", "别名乙"]
        assert second is first
        assert standard_name is first
        assert second.weight_kg == 0.095


def test_cached_none_is_not_overwritten_by_later_matched_name_alias():
    deps = Deps(charts={}, cache=AutoCache())
    calls = []

    async def matcher(query):
        calls.append(query)
        if query == "模块":
            return None
        return PartWeightMatch(
            query=query,
            matched_name="模块",
            part_no="P1",
            weight_kg=1.5,
            similarity=0.96,
            match_type="semantic",
            sources=(),
        )

    async def scenario():
        before = await deps.part_weight_pool.match("模块", matcher)
        alias = await deps.part_weight_pool.match("模块别名", matcher)
        after = await deps.part_weight_pool.match("模块", matcher)
        return before, alias, after

    before, alias, after = asyncio.run(scenario())

    assert calls == ["模块", "模块别名"]
    assert before is None
    assert alias.weight_kg == 1.5
    assert after is None


def test_same_matched_name_with_different_part_numbers_does_not_merge():
    deps = Deps(charts={}, cache=AutoCache())

    async def matcher(query):
        if query == "模块甲别名":
            return PartWeightMatch(
                query=query,
                matched_name="模块",
                part_no="P1",
                weight_kg=1.0,
                similarity=0.96,
                match_type="semantic",
                sources=(),
            )
        return PartWeightMatch(
            query=query,
            matched_name="模块",
            part_no="P2",
            weight_kg=2.0,
            similarity=0.96,
            match_type="semantic",
            sources=(),
        )

    async def scenario():
        first = await deps.part_weight_pool.match("模块甲别名", matcher)
        second = await deps.part_weight_pool.match("模块乙别名", matcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.weight_kg == 1.0
    assert second.weight_kg == 2.0
    assert first is not second


def test_part_number_identity_preserves_punctuation():
    deps = Deps(charts={}, cache=AutoCache())

    async def matcher(query):
        if query == "连字符零件":
            return PartWeightMatch(
                query=query,
                matched_name="模块甲",
                part_no="P-1",
                weight_kg=1.0,
                similarity=1.0,
                match_type="exact",
                sources=(),
            )
        return PartWeightMatch(
            query=query,
            matched_name="模块乙",
            part_no="P1",
            weight_kg=2.0,
            similarity=1.0,
            match_type="exact",
            sources=(),
        )

    async def scenario():
        first = await deps.part_weight_pool.match("连字符零件", matcher)
        second = await deps.part_weight_pool.match("无连字符零件", matcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.part_no == "P-1"
    assert second.part_no == "P1"
    assert first.weight_kg == 1.0
    assert second.weight_kg == 2.0


def test_published_query_result_never_changes_after_later_alias_match():
    deps = Deps(charts={}, cache=AutoCache())
    calls = []

    async def matcher(query):
        calls.append(query)
        if query == "模块":
            return PartWeightMatch(
                query=query,
                matched_name="模块",
                part_no="P1",
                weight_kg=1.0,
                similarity=1.0,
                match_type="exact",
                sources=(),
            )
        return PartWeightMatch(
            query=query,
            matched_name="模块",
            part_no="P2",
            weight_kg=9.0,
            similarity=0.96,
            match_type="semantic",
            sources=(),
        )

    async def scenario():
        before = await deps.part_weight_pool.match("模块", matcher)
        later = await deps.part_weight_pool.match("另一模块别名", matcher)
        after = await deps.part_weight_pool.match("模块", matcher)
        return before, later, after

    before, later, after = asyncio.run(scenario())

    assert calls == ["模块", "另一模块别名"]
    assert before.weight_kg == after.weight_kg == 1.0
    assert after is before
    assert later.weight_kg == 9.0


def test_same_part_number_with_different_names_keeps_first_reliable_weight():
    deps = Deps(charts={}, cache=AutoCache())

    async def matcher(query):
        if query == "中文别名":
            return PartWeightMatch(
                query=query,
                matched_name="低压线束",
                part_no="LV-001",
                weight_kg=0.095,
                similarity=0.96,
                match_type="semantic",
                sources=(),
            )
        return PartWeightMatch(
            query=query,
            matched_name="LOW VOLTAGE HARNESS",
            part_no="LV-001",
            weight_kg=9.9,
            similarity=0.95,
            match_type="semantic",
            sources=(),
        )

    async def scenario():
        first = await deps.part_weight_pool.match("中文别名", matcher)
        second = await deps.part_weight_pool.match("English alias", matcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert second is first
    assert second.weight_kg == 0.095


def test_weight_pools_are_isolated_between_deps_instances():
    match_calls = 0
    first_deps = Deps(charts={}, cache=AutoCache())
    second_deps = Deps(charts={}, cache=AutoCache())

    async def matcher(query):
        nonlocal match_calls
        match_calls += 1
        return PartWeightMatch(
            query=query,
            matched_name="低压线束",
            part_no="P1",
            weight_kg=float(match_calls),
            similarity=1.0,
            match_type="exact",
            sources=(),
        )

    async def scenario():
        first = await first_deps.part_weight_pool.match("低压线束", matcher)
        second = await second_deps.part_weight_pool.match("低压线束", matcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first_deps.part_weight_pool is not second_deps.part_weight_pool
    assert match_calls == 2
    assert first.weight_kg == 1.0
    assert second.weight_kg == 2.0


def test_weight_band_uses_real_kg_text_and_ceiling_not_formula_value():
    candidates = [
        _option("0.50 lbs / 0.23 kg", "0.23KGX,", 99.0),
        _option("1 lb / .45 kg", ".45KGX,", 1.0),
        _option("15.56 lbs / 7.0 kg", "7.0KGX,", 2.0),
    ]

    assert candidate_weight_kg(candidates[1]) == 0.45
    selected, band = select_weight_range(0.095, candidates)
    assert selected is candidates[0]
    assert band == 0.23
    selected, band = select_weight_range(6.5, candidates)
    assert selected is candidates[2]
    assert band == 7.0
    assert select_weight_range(7.1, candidates) is None


def test_weight_band_matches_real_most_chart_candidates():
    chart = load_charts()["050 224"]
    candidates = chart.candidates(2, 1)

    low_choice, low_band = select_weight_range(0.095, candidates)
    heavy_choice, heavy_band = select_weight_range(6.5, candidates)

    assert low_choice.metric_abbrev == "0.23KGX,"
    assert low_band == 0.23
    assert heavy_choice.metric_abbrev == "7.0KGX,"
    assert heavy_band == 7.0


def test_pick_value_does_not_multiply_unit_weight_by_quantity(monkeypatch):
    candidates = [
        _option("0.50 lbs / 0.23 kg", "0.23KGX,", 0.5),
        _option("1 lb / 0.45 kg", "0.45KGX,", 1.0),
    ]
    context = NumericContext(
        weight_kg=0.095,
        query_name="螺栓",
        matched_name="螺栓",
        similarity=1.0,
        match_type="exact",
        source="DU!E10",
    )

    chosen, confidence, reason = asyncio.run(
        pick_value(
            "人工A拿取4个螺栓",
            candidates,
            numeric_context=context,
        )
    )

    assert chosen is candidates[0]
    assert confidence == 0.98
    assert "weight_kg=0.095" in reason
    assert "band_kg=0.23" in reason


def test_resolver_skips_history_decision_when_weight_is_available():
    candidates = [
        _option("0.50 lbs / 0.23 kg", "0.23KGX,", 0.23),
        _option("1 lb / 0.45 kg", "0.45KGX,", 0.45),
    ]
    chart = MostChart(
        chartcode="TEST WEIGHT",
        title="Get Object",
        formula="V1",
        value_added=False,
        developed_in_seconds=True,
        options={(1, 1): candidates},
    )

    class WeightIndex:
        available = True

        async def match(self, query):
            return PartWeightMatch(
                query=query,
                matched_name="螺栓",
                part_no="P1",
                weight_kg=0.095,
                similarity=1.0,
                match_type="exact",
                sources=(PartWeightSource("DU", 10, "E10"),),
            )

    class HistoryIndex:
        async def knn(self, text, k):
            raise AssertionError("重量命中后不应复用历史完整决策")

    async def extract(_):
        return "螺栓"

    async def select_chartcode(_, __):
        return "TEST WEIGHT"

    deps = Deps(
        charts={"TEST WEIGHT": chart},
        cache=AutoCache(),
        history_index=HistoryIndex(),
        part_weight_index=WeightIndex(),
        llm_extract_part_name=extract,
        llm_select_chartcode=select_chartcode,
    )
    element = StdsElement(
        1,
        "人工A拿取4个螺栓",
        "Line",
        "Station",
        freq=1.0,
        norm_key="人工A拿取4个螺栓",
    )
    deps.cache.put(
        element.norm_key,
        StdsResult(
            element=element,
            chartcode="STALE",
            decision="STALE",
            time_s=99.0,
            cv="V",
            freq=1.0,
            source=Source.CACHE,
            confidence=1.0,
            needs_review=False,
        ),
    )

    context = NumericContext(
        weight_kg=0.095,
        query_name="螺栓",
        matched_name="螺栓",
        similarity=1.0,
        match_type="exact",
        source="DU!E10",
        group_id="G1",
    )
    result = asyncio.run(
        resolve(
            element,
            deps,
            numeric_context=context,
            part_context_resolved=True,
        )
    )

    assert result.source == Source.FORMULA
    assert result.decision == "0.23KGX"
    assert result.time_s == 0.23
    assert result.trace[0][0] == "PartWeightLookup"
    assert any("part-weight:" in step[2] for step in result.trace)
