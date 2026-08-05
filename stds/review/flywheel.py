"""Step 15b:数据飞轮 -- 每次人工复核确认,系统立刻变便宜/变准。"""
from __future__ import annotations

from dataclasses import replace

from stds.data.cache import decision_cache_scope


def on_review_confirmed(el, result, deps):
    """复核结果回灌 T0 缓存 + T1 检索索引 + golden 池。"""
    result.edited = True
    unit_time = result.time_s / (result.freq or 1.0)
    cache_template = replace(result, time_s=unit_time, freq=1.0)
    cache_scope = decision_cache_scope(deps)
    if cache_scope:
        try:
            deps.cache.put(
                el.norm_key,
                cache_template,
                scope=cache_scope,
            )                                            # 同一决策环境内复用
        except TypeError:
            # 旧自定义缓存不支持 namespace 时宁可不回灌，也不能跨经验串用。
            pass
    else:
        deps.cache.put(el.norm_key, cache_template)
    if deps.history_index is not None:
        deps.history_index.add(el.operation_des, result) # 回灌 T1(M3 接入后生效)
    if hasattr(deps, "goldens"):
        deps.goldens.append((el, result))                # 回灌 golden 池
