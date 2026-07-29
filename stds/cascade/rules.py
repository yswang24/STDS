"""规则层:文本归一化 / 频率抽取 / 判人-设备(不依赖 LLM)。"""
from __future__ import annotations

import re


def normalize(text: str) -> str:
    """归一化:去空格转小写,作为缓存键。"""
    return re.sub(r"\s+", "", text).lower()


def extract_freq(text: str) -> float:
    """从操作描述中提取频率(如'3次'、'×2')。默认 1.0。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*次|×\s*(\d+)", text)
    if m:
        return float(m.group(1) or m.group(2))
    return 1.0


# 设备关键词(设备自主完成)
_MACHINE_KEYWORDS = [
    "自动", "机器人自动", "AGV自动", "自动焊接", "自动传送", "自动升降", "自动扫码", "自动拧紧",
    "扫码枪", "拧紧枪", "升降台", "移载机", "焊机", "压机",
]
# 人工动作关键词
_HUMAN_KEYWORDS = [
    "拿取", "转身", "弯腰", "行走", "目视", "安装", "撕", "贴", "对准", "放入",
    "取出", "放置", "组装", "拧紧", "检查", "擦拭", "扣合", "卡入", "插接",
    "移动", "搬运", "使用", "手持",
]
_COUNTED_SUBJECT_PREFIX = (
    r"(?:"
    r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿俩]+)"
    r"\s*(?:个|台|套|组|辆|部|只|名|位)?\s*"
    r")?"
)
_HUMAN_SUBJECT_RE = re.compile(
    rf"^\s*{_COUNTED_SUBJECT_PREFIX}"
    r"(?:操作人员|人工|工人|操作工|操作员|作业员|员工|人员)",
    re.IGNORECASE,
)
_ENGLISH_HUMAN_RE = re.compile(r"^\s*manual(?:ly)?\b", re.IGNORECASE)
_ENGLISH_MACHINE_RE = re.compile(r"\bauto(?:matic(?:ally)?)?\b", re.IGNORECASE)
_MACHINE_SUBJECT_RE = re.compile(
    rf"^\s*{_COUNTED_SUBJECT_PREFIX}"
    r"(?:机器人|机械手|AGV|设备)",
    re.IGNORECASE,
)


def is_explicit_machine_action(text: str) -> bool:
    """判断文本是否明确描述设备自主动作。

    句首明示人工主体的优先级最高；否则 ``自动``/``Auto``，或位于句首的
    机器人、机械手、AGV、设备主体（可带数量和量词）都视为设备动作。
    工具名称本身不构成设备主体。
    """
    value = str(text or "")
    if _HUMAN_SUBJECT_RE.match(value) or _ENGLISH_HUMAN_RE.match(value):
        return False
    return bool(
        "自动" in value
        or _ENGLISH_MACHINE_RE.search(value)
        or _MACHINE_SUBJECT_RE.match(value)
    )


def rule_machine(text: str):
    """True=设备, False=人, None=歧义(需 LLM 兜底)。

    优先级:句首人工主体 > 设备主体/关键词 > 人工动作关键词 > None
    """
    # 1. 句首人工主体优先("操作人员移动吊具" / "Manual install" → 人工)
    if _HUMAN_SUBJECT_RE.match(text) or _ENGLISH_HUMAN_RE.match(text):
        return False
    # 2. 明示设备主体/自主动作。必须先于"拧紧"等普通人工动作判断。
    if is_explicit_machine_action(text):
        return True
    # 3. 其他既有设备关键词
    if any(k in text for k in _MACHINE_KEYWORDS):
        return True
    # 4. 人工动作("拿取"、"转身" → 人工)
    if any(k in text for k in _HUMAN_KEYWORDS):
        return False
    # 5. 歧义,交给 LLM
    return None
