"""Small-model intent and complexity classification with a deterministic heuristic fallback.

The LLM is called once with settings.INTENT_MODEL (a small model). The reply is parsed once;
on any failure the heuristic result is returned (source="heuristic", parse_error=True). No retry here:
retry policy belongs to the failure-handling layer.
"""
import json
import re
import time
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logger import logger

INTENTS = {"SELECT", "SCHEMA_QUERY", "OFF_TOPIC", "UNKNOWN"}
_COMPLEX = re.compile(r"\b(and also|as well as|compare|versus|vs\.?|correlat\w*|breakdown by .* and)\b|;", re.I)
_OFF_TOPIC = re.compile(r"\b(weather|football|cricket|recipe|joke|hello|hi there)\b", re.I)
_SCHEMA = re.compile(r"\b(what tables|list (the )?(tables|columns)|show schema|which columns)\b", re.I)


def heuristic_complexity(query: str) -> str:
    q = query or ""
    if _COMPLEX.search(q) or len([p for p in q.split(",") if p.strip()]) >= 3:
        return "complex"
    return "simple"


def heuristic_intent(query: str) -> str:
    q = query or ""
    if _SCHEMA.search(q):
        return "SCHEMA_QUERY"
    if _OFF_TOPIC.search(q):
        return "OFF_TOPIC"
    return "SELECT" if q.strip() else "UNKNOWN"


def _parse(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    data = json.loads(fence.group(1) if fence else t)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    intent = str(data["intent"]).upper()
    complexity = str(data["complexity"]).lower()
    if intent not in INTENTS or complexity not in ("simple", "complex"):
        raise ValueError("invalid values")
    return {"intent": intent, "complexity": complexity, "confidence": float(data.get("confidence", 0.8))}


class IntentClassifier:
    def __init__(self, llm=None, model: Optional[str] = None):
        self._llm = llm
        self.model = model or settings.INTENT_MODEL

    def _get_llm(self):
        if self._llm is None:
            from app.services.llm import llm_service
            self._llm = llm_service
        return self._llm

    async def classify(self, query: str) -> Dict[str, Any]:
        start = time.perf_counter()
        base = {"intent": heuristic_intent(query), "complexity": heuristic_complexity(query),
                "confidence": 0.5, "model": self.model, "source": "heuristic"}
        prompt = ('Classify the analytics question. Reply ONLY with JSON '
                  '{"intent": "SELECT|SCHEMA_QUERY|OFF_TOPIC|UNKNOWN", "complexity": "simple|complex", '
                  '"confidence": 0..1}. "complex" means several metrics or comparisons in one question.\n'
                  f"Question: {query}")
        try:
            reply = await self._get_llm().generate_response(prompt, model_name=self.model)
            base.update(_parse(reply), source="llm")
        except Exception as e:  # unparsable or LLM failure: heuristic result, flagged
            logger.warning(f"Intent LLM unusable ({type(e).__name__}); using heuristic")
            base["parse_error"] = True
        base["latency_ms"] = (time.perf_counter() - start) * 1000
        return base
