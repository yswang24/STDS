"""M3 测试:Step 18-21 内聚 RAG 各层验证。"""
from __future__ import annotations

import asyncio

import pytest

from stds.cascade.resolver import Deps, resolve
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.repo import load_edited_history
from stds.domain.models import Source, StdsElement, StdsResult
from stds.retrieval.chartcode_index import ChartcodeIndex
from stds.retrieval.embed import MockEmbed
from stds.retrieval.history_index import HistoryIndex


# ---------- Step 18: embed + chartcode_index ----------


def test_mock_embed_deterministic():
    e = MockEmbed()
    v1 = e.embed_one("拿取物体")
    v2 = e.embed_one("拿取物体")
    assert v1 == v2  # 同文本同向量
    v3 = e.embed_one("扫码")
    assert v1 != v3  # 不同文本不同向量


def test_chartcode_index_retrieve():
    charts = load_charts()
    embed = MockEmbed()
    idx = ChartcodeIndex(embed)
    idx.build(charts)
    cand = idx.retrieve("Place or Reposition Nut Runner", k=3)
    assert len(cand.topk) == 3
    # MockEmbed 非语义,只验证逻辑:返回有效 chartcode + score
    for code, score in cand.topk:
        assert code in charts
        assert -1.01 <= score <= 1.01  # 浮点精度容差


def test_chartcode_index_confident():
    charts = load_charts()
    embed = MockEmbed()
    idx = ChartcodeIndex(embed)
    idx.build(charts)
    # 自身标题应该高分(因为 MockEmbed 确定性)
    title = charts["060 010"].title
    cand = idx.retrieve(title, k=3)
    assert cand.topk[0][1] > 0.9  # 自身相似度极高


# ---------- Step 19: history_index ----------


def test_history_index_build_and_knn():
    embed = MockEmbed()
    idx = HistoryIndex(embed)
    edited = load_edited_history()
    if not edited:
        pytest.skip("无已编辑历史记录")
    idx.build_from_edited(edited)
    hits = idx.knn(edited[0]["操作内容"], k=3)
    assert len(hits) > 0
    assert hits[0].score >= 0.99  # 自身相似度


def test_history_index_add():
    embed = MockEmbed()
    idx = HistoryIndex(embed)
    el = StdsElement(1, "拿取泡棉", "L", "S")
    result = StdsResult(el, "050 221", "SIM,18IN,0.5LBS,18IN,NAR,", 6.48, "V", 6.0, Source.KNN, 1.0, False)
    idx.add("拿取泡棉", result)
    hits = idx.knn("拿取泡棉", k=1)
    assert len(hits) == 1
    assert hits[0].chartcode == "050 221"


# ---------- Step 21: resolver T1/T3 接入 ----------


def test_resolver_t1_history_hit():
    """T1 历史命中:高分一致邻居 -> 复用 chartcode+decision,时间公式算。"""
    charts = load_charts()
    cache = AutoCache()
    embed = MockEmbed()
    hist_idx = HistoryIndex(embed)
    # 预置一条历史
    hist_idx.add("拿取泡棉", StdsResult(
        StdsElement(1, "", "", ""), "050 221",
        "SIM,18IN,0.5LBS,18IN,NAR,", 0.0, "V", 1.0, Source.KNN, 1.0, False,
    ))
    # 多次 add 相同内容,让邻居一致
    for _ in range(4):
        hist_idx.add("拿取泡棉", StdsResult(
            StdsElement(1, "", "", ""), "050 221",
            "SIM,18IN,0.5LBS,18IN,NAR,", 0.0, "V", 1.0, Source.KNN, 1.0, False,
        ))

    async def no_pick(op, cands):
        return cands[0], 1.0, "mock"

    deps = Deps(charts=charts, cache=cache, history_index=hist_idx, llm_pick_value=no_pick)
    el = StdsElement(1, "拿取泡棉", "L", "S", freq=1.0, norm_key="拿取泡棉")
    res = asyncio.run(resolve(el, deps))
    assert res.source == Source.KNN
    assert res.chartcode == "050 221"
    assert res.time_s > 0  # 时间来自公式(不是历史)


def test_resolver_t3_llm_select_chartcode():
    """T3 LLM 选码:llm_select_chartcode 从 62 个里选。"""
    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc(op_des, charts):
        return "020 02A"

    async def mock_classify(text):
        return False

    async def no_pick(op, cands):
        return cands[0], 1.0, "mock"

    deps = Deps(
        charts=charts, cache=cache,
        llm_classify=mock_classify,
        llm_select_chartcode=mock_select_cc,
        llm_pick_value=no_pick,
    )
    el = StdsElement(1, "Place or Reposition Nut Runner", "L", "S", freq=1.0, norm_key="Place Nut Runner")
    res = asyncio.run(resolve(el, deps))
    assert res.chartcode == "020 02A"
    assert res.time_s > 0


def test_resolver_all_time_from_formula():
    """关键:所有路径的时间都来自公式,不读历史。"""
    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc(op_des, charts):
        return "060 010"

    async def mock_classify(text):
        return False

    async def pick_0(op, cands):
        return cands[0], 1.0, "mock"

    deps = Deps(charts=charts, cache=cache, llm_classify=mock_classify, llm_select_chartcode=mock_select_cc, llm_pick_value=pick_0)
    el = StdsElement(1, "Scan with Laser", "L", "S", freq=1.0, norm_key="ls")
    res = asyncio.run(resolve(el, deps))
    assert res.time_s > 0
    assert res.source == Source.FORMULA
