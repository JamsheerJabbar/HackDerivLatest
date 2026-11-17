"""Answer-aware reranking + feature boosting by table weight.

An AnswerBank holds one pre-generated initial answer per metric. For the top-k candidates the query is
compared with each candidate's answer (embedding cosine), blended with the retrieval score, then
multiplied by (1 + boost * table_weight) so vital tables win ties.
NOTE: answers must be generated from non-user-specific aggregates (see ISSUES) to preserve isolation.
"""
from typing import Any, Callable, Dict, List

from app.services.embeddings import embedding_service


def feature_boost(score: float, weight: float, boost: float = 0.25) -> float:
    return score * (1 + boost * weight)


class AnswerBank:
    def __init__(self, answers: Dict[str, str]):
        self.answers = dict(answers)

    @classmethod
    def build(cls, catalog: Dict[str, Any], answer_fn: Callable[[Dict[str, Any]], str]) -> "AnswerBank":
        out = {}
        for m in catalog["metrics"]:
            try:
                out[m["id"]] = answer_fn(m)
            except Exception:
                continue  # a metric whose answer cannot be generated simply gets answer_sim 0.0
        return cls(out)


def _cos(a: List[float], b: List[float]) -> float:
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else max(0.0, sum(x * y for x, y in zip(a, b)) / (na * nb))


async def rerank(query: str, candidates: List[Dict[str, Any]], answer_bank: AnswerBank,
                 weights: Dict[str, float], k: int, alpha: float = 0.5, boost: float = 0.25) -> List[Dict[str, Any]]:
    if not candidates or k <= 0:
        return []
    texts = [query] + [answer_bank.answers.get(c["id"], "") for c in candidates]
    vecs = await embedding_service.embed(texts)
    qv = vecs[0]
    out = []
    for c, v in zip(candidates, vecs[1:]):
        has = bool(answer_bank.answers.get(c["id"]))
        sim = _cos(qv, v) if has else 0.0
        w = weights.get(c.get("table"), 0.5)
        base = alpha * c["score"] + (1 - alpha) * sim
        out.append({**c, "answer_sim": sim, "weight": w, "final_score": feature_boost(base, w, boost)})
    out.sort(key=lambda r: (-round(r["final_score"], 12), -r["weight"], r["id"]))
    return out[:k]
