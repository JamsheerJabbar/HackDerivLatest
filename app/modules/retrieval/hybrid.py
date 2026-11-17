"""Hybrid retrieval: in-process BM25 + vector search fused by reciprocal rank fusion (RRF)."""
import math
import re
from collections import Counter
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+|[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    """Lowercase; hyphenated identifiers (rule-007) are kept joined and also split."""
    out: List[str] = []
    for tok in _TOKEN.findall((text or "").lower().replace("_", " ")):
        out.append(tok)
        if "-" in tok:
            out.extend(tok.split("-"))
    return out


class BM25Index:
    def __init__(self, docs: Dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf: Dict[str, Counter] = {i: Counter(tokenize(t)) for i, t in docs.items()}
        self.len = {i: sum(c.values()) for i, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 0.0
        df: Counter = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        n = len(self.tf)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        q = tokenize(query)
        if not q or not self.tf or k <= 0:
            return []
        scores: Dict[str, float] = {}
        for i, tf in self.tf.items():
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1)))
            if s > 0:
                scores[i] = s
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]


def rrf_fuse(rankings: List[List[str]], k: int = 60, top: Optional[int] = None) -> List[str]:
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    fused = [i for i, _ in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]
    return fused[:top] if top is not None else fused


class HybridRetriever:
    def __init__(self, vector_search_fn: Callable[[str], Awaitable[List[str]]], bm25: BM25Index,
                 top_candidates: int = 50):
        self.vector_search_fn, self.bm25, self.top = vector_search_fn, bm25, top_candidates

    async def search(self, query: str, k: int = 5) -> List[str]:
        if not (query or "").strip():
            return []
        try:
            vec = list(await self.vector_search_fn(query))[: self.top]
        except Exception:
            vec = []
        lex = [i for i, _ in self.bm25.search(query, self.top)]
        return rrf_fuse([r for r in (vec, lex) if r], top=k)
