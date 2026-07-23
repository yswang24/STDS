"""T1:kNN 历史检索。只索引 已人工编辑='是' 的记录(chartcode+决策描述可信)。

复用邻居时只复用 chartcode + decision,时间重新用公式算(不读历史时间)。
飞轮回灌:add() 让索引随复核累积越来越准。
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import List, Optional

from stds.retrieval.embed import EmbedBackend


@dataclass
class Hit:
    text: str
    chartcode: str
    decision: str
    score: float


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


class HistoryIndex:
    def __init__(self, embed_backend: EmbedBackend):
        self._embed = embed_backend
        self._texts: list = []
        self._metas: list = []   # [(chartcode, decision), ...]
        self._vecs: list = []

    def build_from_edited(self, edited_rows: list):
        """从 已人工编辑='是' 的记录构建索引。只取 chartcode + 决策描述,不取时间。"""
        texts = [r["操作内容"] for r in edited_rows]
        vecs = self._embed.embed(texts) if texts else []
        for r, v in zip(edited_rows, vecs):
            self._texts.append(r["操作内容"])
            self._metas.append((r["动作代码"], r["决策描述"]))
            self._vecs.append(v)

    async def knn(self, text: str, k: int = 5) -> List[Hit]:
        """返回 top-k 相似历史(余弦)。"""
        if not self._vecs:
            return []
        qvec = await asyncio.to_thread(self._embed.embed_one, text)
        scored = []
        for i, v in enumerate(self._vecs):
            score = _cosine(qvec, v)
            scored.append(Hit(
                text=self._texts[i],
                chartcode=self._metas[i][0],
                decision=self._metas[i][1],
                score=score,
            ))
        scored.sort(key=lambda h: -h.score)
        return scored[:k]

    def add(self, text: str, result) -> None:
        """飞轮回灌:复核确认后立即加入索引(复核为单次操作,同步可接受)。"""
        vec = self._embed.embed_one(text)
        self._texts.append(text)
        self._metas.append((result.chartcode, result.decision))
        self._vecs.append(vec)
