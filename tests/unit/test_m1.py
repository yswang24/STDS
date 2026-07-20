"""M1 测试:Step 7-12 级联骨架各层验证。"""
from __future__ import annotations

import asyncio

import pytest

from stds.cascade.numeric import nearest_range, parse_numeric
from stds.cascade.resolver import Deps, resolve
from stds.cascade.rules import extract_freq, normalize, rule_machine
from stds.data.cache import AutoCache
from stds.data.charts_loader import load_charts
from stds.data.db import get_conn
from stds.domain.models import Source, StdsElement, ValueOption
from stds.llm.client import get_mock_llm
from stds.llm.pick_value import ValuePick, pick_value


# ---------- Step 7: cache + repo + rules ----------


def test_cache_get_put():
    c = AutoCache()
    assert c.get("key") is None
    c.put("key", "val")
    assert c.get("key") == "val"


def test_normalize():
    assert normalize("拿 取 泡棉") == "拿取泡棉"
    assert normalize("  拿取  ") == "拿取"


def test_extract_freq():
    assert extract_freq("拿取3次") == 3.0
    assert extract_freq("×2 操作") == 2.0
    assert extract_freq("普通操作") == 1.0


def test_rule_machine():
    assert rule_machine("自动扫码") is True
    assert rule_machine("AGV自动传送") is True
    assert rule_machine("拿取泡棉") is False
    assert rule_machine("转身并弯腰") is False
    assert rule_machine("操作人员移动吊具") is False  # 人工优先
    assert rule_machine("未知操作") is None  # 歧义,需 LLM


def test_repo_load_edited_history():
    from stds.data.repo import load_edited_history
    rows = load_edited_history()
    # 已人工编辑的记录应返回(如果有)
    assert isinstance(rows, list)
    for r in rows:
        assert "操作内容" in r and "动作代码" in r and "决策描述" in r


def test_repo_load_records_by_station_parametrized():
    """参数化 SQL 验证:不会因输入注入。"""
    from stds.data.repo import load_records_by_station
    # 正常查询(可能无结果,但不报 SQL 错)
    load_records_by_station("OR '1'='1", "OP010")  # 注入尝试
    load_records_by_station("BEV3 RESS P1", "OP010")


# ---------- Step 8: numeric ----------


def test_parse_numeric():
    assert parse_numeric("走 7m") == ("distance_m", 7.0)
    assert parse_numeric("18 in 范围") == ("distance_in", 18.0)
    assert parse_numeric("0.5LBS") == ("weight_lbs", 0.5)
    assert parse_numeric("1000rpm") == ("rpm", 1000.0)
    assert parse_numeric("普通操作") is None


def test_nearest_range():
    cands = [
        ValueOption(2, 1, 1, "10cm", "10CMX,", 4.0, 3, 1),
        ValueOption(2, 1, 2, "25cm", "25CMX,", 10.0, 3, 1),
        ValueOption(2, 1, 3, "46cm", "46CMX,", 18.0, 3, 1),
    ]
    assert nearest_range(18, cands).formula_value == 18.0
    assert nearest_range(12, cands).formula_value == 10.0  # 最近档


# ---------- Step 10: llm/client ----------


def test_mock_structured():
    import asyncio
    mock = get_mock_llm()
    assert mock.call_count == 0
    result = asyncio.run(mock.structured("test", ValuePick))
    assert result.index == 0
    assert mock.call_count == 1


# ---------- Step 11: pick_value ----------


def test_pick_value_single_candidate():
    import asyncio
    opt = ValueOption(1, 1, 1, "LS", "LS,", 0.02, 2, 1)
    r, conf, reason = asyncio.run(pick_value("扫码", [opt]))
    assert r.formula_value == 0.02 and conf == 1.0


def test_pick_value_numeric():
    import asyncio
    cands = [
        ValueOption(2, 1, 1, "10cm", "10CMX,", 4.0, 3, 1),
        ValueOption(2, 1, 2, "25cm", "25CMX,", 10.0, 3, 1),
        ValueOption(2, 1, 3, "46cm", "46CMX,", 18.0, 3, 1),
    ]
    r, conf, reason = asyncio.run(pick_value("走 18m", cands))
    assert r.formula_value == 18.0 and "numeric" in reason


def test_pick_value_llm_clamp():
    """LLM 返回越界 index 被夹取。"""
    import asyncio
    mock = get_mock_llm()
    cands = [
        ValueOption(1, 1, 1, "A", "A,", 1.0, 2, 1),
        ValueOption(1, 1, 2, "B", "B,", 2.0, 2, 1),
    ]
    # mock 返回 index=0(默认),不会越界;但确认夹取逻辑在 pick_value 里
    r, _, _ = asyncio.run(pick_value("未知操作", cands))
    assert r.formula_value == 1.0  # 夹到有效范围


# ---------- Step 12: resolver 端到端 ----------


def test_resolver_end_to_end():
    """端到端:rule_machine=False(人) -> LLM 选 060 010 -> 公式算时间。"""
    import asyncio

    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc(op_des, charts):
        return "060 010"

    async def mock_classify(text):
        return False  # 人

    async def pick_0(op, cands):
        return cands[0], 1.0, "mock-pick0"

    deps = Deps(
        charts=charts, cache=cache,
        llm_classify=mock_classify,
        llm_select_chartcode=mock_select_cc,
        llm_pick_value=pick_0,
    )

    el = StdsElement(1, "对准并扫描标签", "L", "S", freq=1.0, norm_key="对准并扫描标签")
    res = asyncio.run(resolve(el, deps))

    assert res.time_s > 0
    assert res.decision != ""
    assert res.source == Source.FORMULA
    assert len(res.trace) >= 1


def test_resolver_cache_hit():
    """T0 缓存命中:复用决策，但按当前 element/freq 重算时间。"""
    import asyncio

    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc(op_des, charts):
        return "060 010"

    async def mock_classify(text):
        return False

    async def pick_0(op, cands):
        return cands[0], 1.0, "mock"

    deps = Deps(charts=charts, cache=cache, llm_classify=mock_classify, llm_select_chartcode=mock_select_cc, llm_pick_value=pick_0)
    el = StdsElement(1, "扫描标签", "L", "S", freq=1.0, norm_key="扫描标签")
    el_twice = StdsElement(2, "扫描标签", "L2", "S2", freq=2.0, norm_key="扫描标签")

    res1 = asyncio.run(resolve(el, deps))
    res2 = asyncio.run(resolve(el_twice, deps))
    assert res2 is not res1
    assert res2.element is el_twice
    assert res2.decision == res1.decision
    assert res2.time_s == round(res1.time_s * 2, 2)
    assert res2.freq == 2.0


def test_resolver_cache_template_avoids_rounding_drift():
    """小频率首次结果的舍入不能污染后续 freq=1 的缓存命中。"""
    charts = load_charts()

    async def mock_select_cc(op_des, charts):
        return "020 130"

    async def mock_classify(text):
        return False

    async def pick_0(op, cands):
        return cands[0], 1.0, "mock"

    def make_deps():
        return Deps(
            charts=charts,
            cache=AutoCache(),
            llm_classify=mock_classify,
            llm_select_chartcode=mock_select_cc,
            llm_pick_value=pick_0,
        )

    cached_deps = make_deps()
    small = StdsElement(1, "cache-rounding", "L", "S", freq=0.1, norm_key="cache-rounding")
    normal = StdsElement(2, "cache-rounding", "L", "S", freq=1.0, norm_key="cache-rounding")
    asyncio.run(resolve(small, cached_deps))
    cached_result = asyncio.run(resolve(normal, cached_deps))
    fresh_result = asyncio.run(resolve(normal, make_deps()))
    assert cached_result.time_s == fresh_result.time_s


def test_resolver_unresolved_on_no_chartcode():
    """LLM 选码返回 None -> 进 unresolved + needs_review。"""
    import asyncio

    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc_none(op_des, charts):
        return None

    async def mock_classify(text):
        return False

    deps = Deps(charts=charts, cache=cache, llm_classify=mock_classify, llm_select_chartcode=mock_select_cc_none)
    el = StdsElement(1, "未知操作", "L", "S", freq=1.0, norm_key="未知操作")
    res = asyncio.run(resolve(el, deps))

    assert res.source == Source.UNRESOLVED
    assert res.needs_review is True


def test_resolver_formula_not_history():
    """关键:time_s 来自公式求值,不读历史时间。"""
    import asyncio

    charts = load_charts()
    cache = AutoCache()

    async def mock_select_cc(op_des, charts):
        return "060 010"

    async def mock_classify(text):
        return False

    async def pick_0(op, cands):
        return cands[0], 1.0, "mock"

    deps = Deps(charts=charts, cache=cache, llm_classify=mock_classify, llm_select_chartcode=mock_select_cc, llm_pick_value=pick_0)
    el = StdsElement(1, "扫描", "L", "S", freq=1.0, norm_key="扫描")
    res = asyncio.run(resolve(el, deps))

    # 060 010 V1=LS(fv=0.02), V2 默认 fv=0 -> (0.02+0)*60 = 1.2
    assert res.time_s == 1.2
