"""T3:chartcode 向量召回。62 个 chartcode 的标题 embed, top-k + 历史命中投票。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from stds.retrieval.embed import EmbedBackend, MockEmbed


@dataclass
class ChartcodeCandidate:
    code: Optional[str]          # 最终选中的 chartcode(或 None 表示不自信)
    topk: list                   # [(code, score), ...]
    confident: bool


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


class ChartcodeIndex:
    def __init__(self, embed_backend: EmbedBackend):
        self._embed = embed_backend
        self._chartcodes: list = []   # [(code, title, vec), ...]

    def build(self, charts: dict):
        """从 charts dict 构建索引。"""
        codes = list(charts.keys())
        texts = [charts[cc].title or cc for cc in codes]
        vecs = self._embed.embed(texts)
        self._chartcodes = list(zip(codes, texts, vecs))

    def retrieve(self, text: str, k: int = 5, threshold: float = 0.85) -> ChartcodeCandidate:
        """返回 top-k 召回 + 置信度判断。"""
        qvec = self._embed.embed_one(text)
        scored = []
        for code, title, vec in self._chartcodes:
            score = _cosine(qvec, vec)
            scored.append((code, score))
        scored.sort(key=lambda x: -x[1])
        topk = scored[:k]
        confident = topk[0][1] >= threshold if topk else False
        code = topk[0][0] if confident else None
        return ChartcodeCandidate(code=code, topk=topk, confident=confident)
