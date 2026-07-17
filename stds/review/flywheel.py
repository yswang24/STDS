"""Step 15b:数据飞轮 -- 每次人工复核确认,系统立刻变便宜/变准。"""
from __future__ import annotations


def on_review_confirmed(el, result, deps):
    """复核结果回灌 T0 缓存 + T1 检索索引 + golden 池。"""
    result.edited = True
    deps.cache.put(el.norm_key, result)                  # 回灌 T0(未来同文本零 LLM)
    if deps.history_index is not None:
        deps.history_index.add(el.operation_des, result) # 回灌 T1(M3 接入后生效)
    if hasattr(deps, "goldens"):
        deps.goldens.append((el, result))                # 回灌 golden 池
