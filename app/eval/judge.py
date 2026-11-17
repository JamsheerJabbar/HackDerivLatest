"""Generation judges: deterministic by default, optional LLM judge with strict one-shot parsing."""
import json
import re
from typing import Any, Dict, List, Optional

from app.services.embeddings import embedding_service

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> List[float]:
    out = []
    for tok in _NUM.findall(text):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def _row_numbers(rows: List[Dict[str, Any]]) -> List[float]:
    vals: List[float] = []
    for r in rows:
        for v in r.values():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                vals.append(float(v))
            elif isinstance(v, str):
                vals.extend(_numbers(v))
    return vals


def faithfulness(answer: str, rows: List[Dict[str, Any]]) -> float:
    """Fraction of numbers quoted in the answer that are grounded in the result rows (or the row count)."""
    claims = _numbers(answer or "")
    if not claims:
        return 1.0
    grounded_pool = _row_numbers(rows or []) + [float(len(rows or []))]
    if not rows:
        return 0.0

    def ok(c: float) -> bool:
        return any(abs(c - g) <= max(1e-6, 0.005 * abs(g)) or abs(round(g, 2) - c) < 1e-9 for g in grounded_pool)

    return sum(ok(c) for c in claims) / len(claims)


async def relevancy(question: str, answer: str) -> float:
    q, a = await embedding_service.embed([question or "", answer or ""])
    dot = sum(x * y for x, y in zip(q, a))
    na = sum(x * x for x in q) ** 0.5
    nb = sum(x * x for x in a) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def parse_judge_json(text: str) -> Dict[str, Any]:
    """Parse an LLM judge reply once. Raises ValueError if it is not a JSON object (no repair/retry)."""
    t = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        data = json.loads(t)
    except ValueError as e:
        raise ValueError(f"unparsable judge response: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("judge response must be a JSON object")
    return data


async def llm_judge(question: str, answer: str, rows: List[Dict[str, Any]], llm=None) -> Optional[Dict[str, float]]:
    """Optional LLM-as-judge. Returns None when unparsable so the caller keeps the deterministic score."""
    from app.services.llm import llm_service
    llm = llm or llm_service
    prompt = ('Rate the answer to the question given the SQL result rows. Reply ONLY with JSON '
              '{"faithfulness": 0..1, "relevancy": 0..1}.\n'
              f"Question: {question}\nAnswer: {answer}\nRows: {json.dumps(rows[:20], default=str)}")
    try:
        data = parse_judge_json(await llm.generate_response(prompt))
        return {"faithfulness": float(data["faithfulness"]), "relevancy": float(data["relevancy"])}
    except (ValueError, KeyError, TypeError):
        return None
