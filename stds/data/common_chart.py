"""common_chart 常用动作缓存(33 行):关键词匹配 -> 动作代码+决策描述+时间。

用于 T0.5 快速路径:冷启动时高频操作零 LLM 直接命中。
同时可作为 T1 history_index 的初始数据源(解决已编辑记录仅 30 条的问题)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from stds.data.db import get_conn


@dataclass
class CommonChartHit:
    operation_des: str
    chartcode: str
    decision: str
    freq: float
    time: float


def load_common_chart() -> list:
    """加载全部 33 条 common_chart 记录。"""
    con = get_conn()
    rows = con.execute("""
        SELECT 操作内容, 决策描述, 动作代码, 增值非增值, 频率, 时间,
               关键词1, 关键词2, 关键词3, 关键词4,
               关键词5, 关键词6, 关键词7, 关键词8
        FROM common_chart
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


def match_common_chart(text: str, common_rows: list) -> Optional[CommonChartHit]:
    """关键词匹配:输入文本是否命中 common_chart 的任一关键词列。
    关键词长度 >= 3 避免宽泛匹配(如'拿取'2 字符不匹配,但'转身90'3 字符匹配)。
    """
    text_norm = text.strip().lower()
    best = None
    best_kw_len = 0
    for row in common_rows:
        for i in range(1, 9):
            kw = (row.get(f"关键词{i}") or "").strip().lower()
            if len(kw) < 3:
                continue
            if kw in text_norm or text_norm in kw:
                if len(kw) > best_kw_len:  # 优先最长关键词(更精确)
                    best_kw_len = len(kw)
                    best = CommonChartHit(
                        operation_des=row["操作内容"],
                        chartcode=row["动作代码"],
                        decision=row["决策描述"] or "",
                        freq=float(row["频率"] or 1.0),
                        time=float(row["时间"] or 0.0),
                    )
    return best
