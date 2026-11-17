"""Bounded retry with model escalation: attempt 1 small model, attempts 2-3 stronger models, max 3 total."""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from app.core.config import settings
from app.core.logger import logger
from app.services.backend_alerts import emit_backend_alert

HARD_MAX_ATTEMPTS = 3


class NonRetryable(Exception):
    """Raise from an operation to stop retrying immediately."""


class RetriesExhausted(Exception):
    def __init__(self, stage: str, attempts: int, errors: List[str], models_used: List[str]):
        super().__init__(f"[{stage}] failed after {attempts} attempts: {errors[-1] if errors else 'unknown'}")
        self.stage, self.attempts, self.errors, self.models_used = stage, attempts, errors, models_used


@dataclass
class RetryResult:
    value: Any
    attempts: int
    models_used: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class RetryPolicy:
    def __init__(self, base_model: Optional[str] = None, models: Optional[List[str]] = None,
                 max_attempts: Optional[int] = None, base_delay: float = 0.0):
        self.models = list(models) if models else [base_model or settings.SQL_MODEL, settings.RETRY_MODEL,
                                                   settings.FINAL_RETRY_MODEL]
        wanted = settings.MAX_LLM_ATTEMPTS if max_attempts is None else max_attempts
        self.max_attempts = max(1, min(int(wanted), HARD_MAX_ATTEMPTS))
        self.base_delay = base_delay

    def model_for(self, attempt: int) -> str:
        return self.models[min(attempt - 1, len(self.models) - 1)]

    async def run(self, operation: Callable[[str, int], Awaitable[Any]], *, stage: str) -> RetryResult:
        errors: List[str] = []
        used: List[str] = []
        for attempt in range(1, self.max_attempts + 1):
            model = self.model_for(attempt)
            used.append(model)
            try:
                value = await operation(model, attempt)
                return RetryResult(value, attempt, used, errors)
            except NonRetryable:
                raise
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}"[:300])
                logger.warning(f"[{stage}] attempt {attempt}/{self.max_attempts} on {model} failed: {errors[-1]}")
                if attempt < self.max_attempts and self.base_delay > 0:
                    await asyncio.sleep(self.base_delay * attempt)
        emit_backend_alert("llm_retries_exhausted", f"{stage}: {len(errors)} attempts failed", stage=stage,
                           models=",".join(used), last_error=errors[-1] if errors else "")
        raise RetriesExhausted(stage, self.max_attempts, errors, used)
