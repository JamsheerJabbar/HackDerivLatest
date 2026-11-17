"""Multi-query retrieval for complex questions: decompose, search each part, interleave round-robin.

Round-robin (each sub-query contributes its best unseen result in turn) keeps every sub-question's top hit
near the top. RRF was measured to favour consensus items over each part's own best hit (complex MRR 0.95 -> 0.43).
"""
import asyncio
import re
from typing import Awaitable, Callable, List


_SPLIT = re.compile(r"\s+and also\s+|\s+as well as\s+|\s*;\s*|\s+vs\.?\s+|\s+versus\s+", re.I)


def decompose(query: str) -> List[str]:
    q = " ".join((query or "").split())
    if not q:
        return [q]
    parts, seen = [], set()
    for p in _SPLIT.split(q):
        p = p.strip(" ,.")
        if len(p.split()) >= 2 and p.lower() not in seen:
            seen.add(p.lower())
            parts.append(p)
    return parts if len(parts) >= 2 else [q]


async def multi_query_search(search_fn: Callable[[str], Awaitable[List[str]]], query: str,
                             sub_queries: List[str], k: int) -> List[str]:
    queries = list(dict.fromkeys([*sub_queries, query]))  # the original query is always searched too

    async def safe(q: str) -> List[str]:
        try:
            return list(await search_fn(q))
        except Exception:
            return []

    rankings = await asyncio.gather(*[safe(q) for q in queries])
    lists = [r for r in rankings if r]
    out: List[str] = []
    depth = 0
    while len(out) < k and any(depth < len(r) for r in lists):
        for r in lists:
            if depth < len(r) and r[depth] not in out:
                out.append(r[depth])
        depth += 1
    return out[:k]
