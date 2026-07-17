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


# 人工优先关键词(明确表示人执行,即使涉及设备也是人工)
_HUMAN_PRIORITY = [
    "操作人员", "人工", "工人", "操作工", "作业员", "员工",
]
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


def rule_machine(text: str):
    """True=设备, False=人, None=歧义(需 LLM 兜底)。

    优先级:人工优先关键词 > 设备关键词 > 人工动作关键词 > None
    """
    # 1. 人工优先("操作人员移动吊具" → 人工,即使含"吊具")
    if any(k in text for k in _HUMAN_PRIORITY):
        return False
    # 2. 设备("自动扫码"、"AGV自动传送" → 设备)
    if any(k in text for k in _MACHINE_KEYWORDS):
        return True
    # 3. 人工动作("拿取"、"转身" → 人工)
    if any(k in text for k in _HUMAN_KEYWORDS):
        return False
    # 4. 歧义,交给 LLM
    return None
