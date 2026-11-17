"""Last resort after retries are exhausted: run an existing catalog metric if the question matches it
with >= FALLBACK_MIN_SIMILARITY (0.80) semantic similarity; otherwise return an error message."""
import inspect
from typing import Any, Callable, Dict, Optional

from app.core.config import settings
from app.core.logger import logger


class DeterministicFallback:
    def __init__(self, resolver, execute: Callable[[str, Dict[str, Any]], Any], threshold: Optional[float] = None):
        self.resolver, self.execute = resolver, execute
        self.threshold = settings.FALLBACK_MIN_SIMILARITY if threshold is None else threshold

    @staticmethod
    def _fail(message: str, similarity: float = 0.0) -> Dict[str, Any]:
        return {"status": "failed", "source": "deterministic_fallback", "error": message, "similarity": similarity}

    async def handle(self, question: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            if not (question or "").strip():
                return self._fail("I could not understand the question. Please rephrase it.")
            res = await self.resolver.resolve(question, user_id)
            sim = float(res.get("confidence") or 0.0)
            if res.get("status") != "matched" or sim < self.threshold:
                return self._fail("I couldn't answer that reliably. Please rephrase, or ask about one of the "
                                  "standard metrics (e.g. flagged transactions, failed logins, open alerts).", sim)
            out = self.execute(res["sql"], res.get("bind") or {})
            rows = await out if inspect.isawaitable(out) else out
            return {"status": "success", "source": "deterministic_fallback", "metric_id": res.get("metric_id"),
                    "sql": res["sql"], "results": rows, "similarity": sim}
        except Exception as e:  # the fallback itself must never raise
            logger.error(f"Deterministic fallback failed: {e}")
            return self._fail("The fallback answer could not be produced. Please try again later.")
