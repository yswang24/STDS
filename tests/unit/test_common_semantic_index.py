import asyncio
import math
import threading
import time

from stds.experience.common_chart import normalize_common_keyword
from stds.experience.common_index import (
    CommonChartSemanticIndex,
    common_semantic_cells,
    common_semantic_document,
)
from stds.experience.models import CommonChartEntry, CommonChartKind
from stds.retrieval.embed import MockEmbed


def _entry(
    row: int,
    operation: str,
    *,
    keywords: tuple[str, ...] | None = None,
    time_s: float = 1.0,
) -> CommonChartEntry:
    keywords = keywords or ()
    return CommonChartEntry(
        operation_label=operation,
        normalized_operation=normalize_common_keyword(operation),
        chartcode="EST V00",
        decision=f"{time_s:g}S",
        cv="V",
        frequency=1.0,
        source_time_s=time_s,
        time_s=time_s,
        keywords=keywords,
        normalized_keywords=tuple(
            normalize_common_keyword(keyword)
            for keyword in keywords
        ),
        row=row,
        kind=CommonChartKind.FIXED_TIME,
        values={},
    )


class _SemanticEmbed:
    def __init__(self, document_vectors, query_vector):
        self.document_vectors = document_vectors
        self.query_vector = query_vector
        self.embed_calls = 0
        self.embed_one_calls = 0
        self.documents = []

    def embed(self, texts):
        self.embed_calls += 1
        self.documents = list(texts)
        return [list(vector) for vector in self.document_vectors]

    def embed_one(self, _text):
        self.embed_one_calls += 1
        return list(self.query_vector)


def test_semantic_cells_keep_operation_and_each_valid_keyword_separate():
    entry = _entry(
        2,
        "安装托盘",
        keywords=("安装", "托盘落位", "拿"),
    )

    cells = common_semantic_cells(entry)

    assert cells == ("安装托盘", "安装", "托盘落位")
    assert common_semantic_cells(_entry(3, "转身")) == ("转身",)

    # 旧行级展示 helper 仍可供外部诊断使用，但不再作为向量输入。
    document = common_semantic_document(entry)
    assert "操作内容：安装托盘" in document
    assert "安装" in document
    assert "托盘落位" in document
    assert "拿" not in document.split("关键词：", 1)[1]


def test_keyword_match_falls_back_when_embedding_is_unavailable():
    factory_calls = 0

    def unavailable_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError("embedding unavailable")

    index = CommonChartSemanticIndex(
        [_entry(2, "转身", keywords=("转身",))],
        embed_backend_factory=unavailable_factory,
    )

    match = asyncio.run(index.match("人工A转身"))

    assert match is not None
    assert match.match_type == "contains"
    assert match.entry.row == 2
    assert factory_calls == 1


def test_semantic_hit_overrides_keyword_ambiguity():
    backend = _SemanticEmbed(
        [
            [0.0, 1.0],  # row 2 operation
            [0.0, 1.0],  # row 2 keyword
            [0.0, 1.0],  # row 3 operation
            [1.0, 0.0],  # row 3 keyword
        ],
        [1.0, 0.0],
    )
    index = CommonChartSemanticIndex(
        [
            _entry(2, "扫描一", keywords=("扫描",), time_s=1.0),
            _entry(3, "扫描二", keywords=("扫描",), time_s=2.0),
        ],
        embed_backend=backend,
    )

    match = asyncio.run(index.match("人工A扫描"))

    assert match is not None
    assert match.match_type == "semantic"
    assert match.entry.row == 3
    assert match.keyword == "扫描"
    assert match.matched_field == "关键词描述1"
    assert backend.embed_calls == 1
    assert backend.embed_one_calls == 1
    assert backend.documents == ["扫描一", "扫描", "扫描二", "扫描"]


def test_semantic_hit_overrides_unambiguous_keyword_match():
    backend = _SemanticEmbed(
        [
            [0.0, 1.0],
            [0.0, 1.0],
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        [1.0, 0.0],
    )
    index = CommonChartSemanticIndex(
        [
            _entry(2, "扫描", keywords=("扫描",)),
            _entry(3, "检查", keywords=("检查",)),
        ],
        embed_backend=backend,
    )

    match = asyncio.run(index.match("人工A扫描"))

    assert match is not None
    assert match.match_type == "semantic"
    assert match.entry.row == 3
    assert match.keyword == "检查"
    assert match.matched_field == "操作内容"


def test_semantic_low_score_falls_back_to_keyword_and_keeps_ambiguity():
    keyword_backend = _SemanticEmbed(
        [[0.1, 0.995], [0.1, 0.995]],
        [1.0, 0.0],
    )
    keyword_index = CommonChartSemanticIndex(
        [_entry(2, "转身", keywords=("转身",))],
        embed_backend=keyword_backend,
    )

    keyword_match = asyncio.run(keyword_index.match("人工A转身"))

    assert keyword_match is not None
    assert keyword_match.match_type == "contains"
    assert keyword_match.entry.row == 2

    ambiguous_backend = _SemanticEmbed(
        [
            [0.1, 0.995],
            [0.1, 0.995],
            [0.2, 0.98],
            [0.2, 0.98],
        ],
        [1.0, 0.0],
    )
    ambiguous_index = CommonChartSemanticIndex(
        [
            _entry(2, "扫描一", keywords=("扫描",), time_s=1.0),
            _entry(3, "扫描二", keywords=("扫描",), time_s=2.0),
        ],
        embed_backend=ambiguous_backend,
    )

    assert asyncio.run(ambiguous_index.match("人工A扫描")) is None


def test_invalid_or_failed_semantic_query_falls_back_to_keyword():
    class _InvalidDocuments(_SemanticEmbed):
        def embed(self, texts):
            self.embed_calls += 1
            return [[float("nan"), 0.0] for _ in texts]

    class _InvalidQuery(_SemanticEmbed):
        def embed_one(self, _text):
            self.embed_one_calls += 1
            return [float("nan"), 0.0]

    class _FailedQuery(_SemanticEmbed):
        def embed_one(self, _text):
            self.embed_one_calls += 1
            raise RuntimeError("query failed")

    backends = (
        _InvalidDocuments([[1.0, 0.0]], [1.0, 0.0]),
        _InvalidQuery([[1.0, 0.0]], [1.0, 0.0]),
        _FailedQuery([[1.0, 0.0]], [1.0, 0.0]),
    )
    for backend in backends:
        index = CommonChartSemanticIndex(
            [_entry(2, "转身", keywords=("转身",))],
            embed_backend=backend,
        )

        match = asyncio.run(index.match("人工A转身"))

        assert match is not None
        assert match.match_type == "contains"
        assert match.entry.row == 2


def test_semantic_priority_uses_threshold_070_and_top1_without_margin():
    entries = [_entry(2, "旋转身体"), _entry(3, "弯曲身体")]
    backend = _SemanticEmbed(
        [[1.0, 0.0], [0.99, math.sqrt(1.0 - 0.99**2)]],
        [1.0, 0.0],
    )
    index = CommonChartSemanticIndex(entries, embed_backend=backend)

    match = asyncio.run(index.match("改变人物朝向"))

    assert match is not None
    assert match.entry.row == 2
    assert match.match_type == "semantic"
    assert match.keyword == "旋转身体"
    assert match.matched_field == "操作内容"
    assert match.similarity == 1.0

    threshold_backend = _SemanticEmbed(
        [[0.70, math.sqrt(1.0 - 0.70**2)]],
        [1.0, 0.0],
    )
    threshold_index = CommonChartSemanticIndex(
        [_entry(4, "托盘落位")],
        embed_backend=threshold_backend,
    )
    assert asyncio.run(threshold_index.match("放好承载物")) is not None

    low_backend = _SemanticEmbed(
        [[0.699, math.sqrt(1.0 - 0.699**2)]],
        [1.0, 0.0],
    )
    low_index = CommonChartSemanticIndex(
        [_entry(4, "托盘落位")],
        embed_backend=low_backend,
    )
    assert asyncio.run(low_index.match("放好承载物")) is None


def test_semantic_equal_score_uses_smallest_excel_row():
    backend = _SemanticEmbed([[1.0, 0.0], [1.0, 0.0]], [1.0, 0.0])
    index = CommonChartSemanticIndex(
        [_entry(8, "动作甲"), _entry(3, "动作乙")],
        embed_backend=backend,
    )

    match = asyncio.run(index.match("完全不同的描述"))

    assert match is not None
    assert match.entry.row == 3


def test_semantic_equal_score_within_row_prefers_operation_cell():
    backend = _SemanticEmbed(
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
        [1.0, 0.0],
    )
    index = CommonChartSemanticIndex(
        [_entry(3, "托盘落位", keywords=("放置托盘", "落位"))],
        embed_backend=backend,
    )

    match = asyncio.run(index.match("将承载物放到小车上"))

    assert match is not None
    assert match.entry.row == 3
    assert match.keyword == "托盘落位"
    assert match.matched_field == "操作内容"
    assert backend.documents == ["托盘落位", "放置托盘", "落位"]


def test_semantic_top1_can_hit_later_keyword_cell_and_return_its_row():
    backend = _SemanticEmbed(
        [
            [0.0, 1.0],  # row 2 operation
            [0.2, 0.98],  # row 2 keyword
            [0.0, 1.0],  # row 7 operation
            [0.1, 0.995],  # row 7 keyword 1
            [1.0, 0.0],  # row 7 keyword 2, global Top1
        ],
        [1.0, 0.0],
    )
    index = CommonChartSemanticIndex(
        [
            _entry(2, "动作甲", keywords=("描述甲",)),
            _entry(7, "动作乙", keywords=("描述乙", "命中单元格")),
        ],
        embed_backend=backend,
    )

    match = asyncio.run(index.match("输入动作"))

    assert match is not None
    assert match.entry.row == 7
    assert match.keyword == "命中单元格"
    assert match.matched_field == "关键词描述2"
    assert match.match_type == "semantic"
    assert match.similarity == 1.0
    assert len(backend.documents) == 5


def test_mock_and_failed_remote_fallback_never_produce_semantic_hit():
    mock_index = CommonChartSemanticIndex(
        [_entry(2, "旋转身体")],
        embed_backend=MockEmbed(),
        similarity_threshold=-1.0,
    )
    assert asyncio.run(mock_index.match("改变人物朝向")) is None

    class _FailedRemote:
        def __init__(self):
            self._api_available = None

        def embed(self, _texts):
            self._api_available = False
            return [[1.0, 0.0]]

        def embed_one(self, _text):
            return [1.0, 0.0]

    failed_index = CommonChartSemanticIndex(
        [_entry(2, "旋转身体")],
        embed_backend=_FailedRemote(),
        similarity_threshold=-1.0,
    )
    assert asyncio.run(failed_index.match("改变人物朝向")) is None


def test_concurrent_first_queries_build_cell_vectors_once():
    class _SlowEmbed(_SemanticEmbed):
        def embed(self, texts):
            time.sleep(0.03)
            return super().embed(texts)

    backend = _SlowEmbed([[1.0, 0.0]], [1.0, 0.0])
    index = CommonChartSemanticIndex(
        [_entry(2, "旋转身体")],
        embed_backend=backend,
    )

    async def run_queries():
        return await asyncio.gather(*(
            index.match(f"改变人物朝向{i}")
            for i in range(20)
        ))

    matches = asyncio.run(run_queries())

    assert all(match is not None for match in matches)
    assert backend.embed_calls == 1
    assert backend.embed_one_calls == 20


def test_concurrent_query_failure_cannot_promote_mock_vector_to_semantic():
    class _RacyAvailabilityEmbed:
        def __init__(self):
            self._api_available = None
            self.embed_one_calls = 0
            self._counter_lock = threading.Lock()
            self._barrier = threading.Barrier(2)
            self._failure_set = threading.Event()
            self._success_set = threading.Event()

        def embed(self, texts):
            self._api_available = True
            return [[1.0, 0.0] for _ in texts]

        def embed_one(self, _text):
            with self._counter_lock:
                self.embed_one_calls += 1
                call_number = self.embed_one_calls
            try:
                self._barrier.wait(timeout=0.05)
                concurrent = True
            except threading.BrokenBarrierError:
                concurrent = False

            if call_number == 1:
                # 模拟远端失败后返回恰好会命中文档的 Mock 占位向量。
                self._api_available = False
                self._failure_set.set()
                if concurrent:
                    self._success_set.wait(timeout=0.2)
                return [1.0, 0.0]

            # 未串行时，另一条成功请求会把共享状态重新写成 True；旧实现
            # 因而可能把第一条 Mock 向量误判为有效语义结果。
            self._failure_set.wait(timeout=0.2)
            self._api_available = True
            self._success_set.set()
            return [0.0, 1.0]

    backend = _RacyAvailabilityEmbed()
    index = CommonChartSemanticIndex(
        [_entry(2, "旋转动作", keywords=("旋转身体",))],
        embed_backend=backend,
    )

    async def run_queries():
        return await asyncio.gather(
            index.match("人工A旋转身体"),
            index.match("人工A旋转身体"),
        )

    matches = asyncio.run(run_queries())

    assert all(match is not None for match in matches)
    assert [match.match_type for match in matches] == ["contains", "contains"]
    assert backend.embed_one_calls == 1
