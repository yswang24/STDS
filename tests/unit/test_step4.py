"""Step 4 验收:决策串解码,含手工 golden(202 010, 060 010)。"""
from __future__ import annotations

from stds.data.charts_loader import load_charts
from stds.engine.decision_codec import decode, encode


def test_decode_202_010():
    charts = load_charts()
    v, lc = decode(charts["202 010"], "T,90,NB")
    assert v == {1: 0.0, 2: 0.012, 3: 0.0}            # T->0, 90(R2)->0.012, NB->0
    assert lc is False


def test_decode_060_010():
    charts = load_charts()
    v, lc = decode(charts["060 010"], "LS")
    assert v == {1: 0.02, 2: 0.0}                      # V2 默认 No Place Scanner (fv=0)
    assert lc is False


def test_decode_020_02a_traversal():
    charts = load_charts()
    v, lc = decode(charts["020 02A"], "WFB,WPF,D,10DM,NIBU")
    assert v[1] == 2.0 and v[2] == 1.0 and v[3] == 1.0 and v[5] == 0.0
    # V4 的 10DM 走 L2 数值匹配(允许具体值取决于 description 命中)


def test_decode_t_not_misplaced_to_ntt():
    """回归保护:T 不能误配到 'No Twist or Turn'(NTT)。"""
    charts = load_charts()
    v, _ = decode(charts["202 010"], "T,90,NB")
    # 若 T 误配 NTT(nextV=3 跳过 V2),则 v 不会有 V2=0.012
    assert v.get(2) == 0.012


def test_encode_roundtrip():
    assert encode(["T,", "90,", "NB"]) == "T,90,NB"


def test_encode_keeps_one_trailing_comma_only_for_empty_last_decision():
    assert encode(["LS,", ""]) == "LS,"
    assert encode(["LS,", "", ""]) == "LS,"
    assert encode(["PSF,", "", "E,"]) == "PSF,,E"
