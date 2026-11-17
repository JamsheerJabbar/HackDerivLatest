"""Pure scoring functions for retrieval and generation evaluation."""
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence

TRACKED = ["precision", "recall", "mrr", "faithfulness", "relevancy", "execution_accuracy"]


def precision_at_k(retrieved: Sequence, relevant: Iterable, k: int) -> float:
    rel = set(relevant)
    if k <= 0 or not retrieved:
        return 0.0
    return sum(1 for r in list(retrieved)[:k] if r in rel) / k


def recall_at_k(retrieved: Sequence, relevant: Iterable, k: int) -> float:
    rel = set(relevant)
    if k <= 0 or not rel or not retrieved:
        return 0.0
    return len(rel & set(list(retrieved)[:k])) / len(rel)


def mrr(retrieved: Sequence, relevant: Iterable) -> float:
    rel = set(relevant)
    for i, r in enumerate(retrieved, 1):
        if r in rel:
            return 1.0 / i
    return 0.0


def hit_rate(retrieved: Sequence, relevant: Iterable, k: int) -> float:
    rel = set(relevant)
    return 1.0 if k > 0 and any(r in rel for r in list(retrieved)[:k]) else 0.0


def _norm(v: Any) -> Any:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return v


def _row_key(row: Dict[str, Any]) -> tuple:
    return tuple(_norm(v) for v in row.values())


def execution_accuracy(pred_rows: List[Dict[str, Any]], gold_rows: List[Dict[str, Any]]) -> bool:
    """Order-insensitive multiset comparison of row values (column aliases are ignored)."""
    if pred_rows is None or gold_rows is None:
        return False
    return Counter(map(_row_key, pred_rows)) == Counter(map(_row_key, gold_rows))


def aggregate(item_results: List[Dict[str, Any]]) -> Dict[str, float]:
    out = {k: 0.0 for k in TRACKED}
    if not item_results:
        return out
    for k in TRACKED:
        vals = [float(r.get(k, 0.0) or 0.0) for r in item_results]
        out[k] = sum(vals) / len(vals)
    return out
