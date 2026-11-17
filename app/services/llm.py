import asyncio
import time
import google.generativeai as genai
from app.core.config import settings
from app.core.logger import logger

# Optional import of OpenAI – will be available if the package is installed
try:
    import openai
except ImportError:
    openai = None

class LLMService:
    def __init__(self):
        # Prefer OpenAI if an API key is provided
        if getattr(settings, "OPENAI_API_KEY", None):
            if openai is None:
                raise ImportError("openai package is required for OPENAI_API_KEY usage.")
            openai.api_key = settings.OPENAI_API_KEY
            self.provider = "openai"
            self.model_name = getattr(settings, "OPENAI_MODEL_NAME", "gpt-3.5-turbo")
        elif settings.GEMINI_API_KEY:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            self.provider = "gemini"
            self.model_name = getattr(settings, "GEMINI_MODEL_NAME", "gemini-2.5-flash-lite")
            # Set lowered safety settings to avoid blocking technical database queries
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
            self.model = genai.GenerativeModel(
                model_name=self.model_name,
                safety_settings=safety_settings
            )
        else:
            print("Warning: No LLM API key found in settings.")
            self.provider = None
            self.model = None

    async def _call_openai(self, prompt: str, model_override: str = None) -> str:
        # Simple wrapper for OpenAI ChatCompletion (sync client: run in a worker thread, not on the event loop)
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model=model_override or self.model_name,
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
        )
        return response.choices[0].message.content

    async def generate_response(self, prompt: str, model_name: str = None, internal_retries: int = 3) -> str:
        """internal_retries: provider rate-limit attempts inside this call. Callers that run under RetryPolicy pass 1
        so the policy (max 3 attempts, model escalation) is the only retry layer."""
        if not self.provider:
            logger.error("LLM Service not configured (no API key found).")
            return "LLM Service not configured."

        selected_model = model_name or self.model_name
        logger.info(f"LLM [{self.provider}] using model [{selected_model}] generating response...")
        
        # Retry logic for rate‑limit (429) – up to 3 attempts
        attempts = 0
        while attempts < internal_retries:
            try:
                if self.provider == "openai":
                    res = await self._call_openai(prompt, model_override=selected_model)
                else:  # gemini
                    # If model_name is provided, we use a new model instance for that call
                    model_to_use = self.model
                    if model_name:
                        model_to_use = genai.GenerativeModel(
                            model_name=model_name,
                            safety_settings=[
                                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                            ]
                        )
                    
                    response = await asyncio.to_thread(model_to_use.generate_content, prompt)  # keep the event loop free
                    
                    # Handle safety blocks
                    if not response.candidates or response.candidates[0].finish_reason != 1: # 1 = STOP (Success)
                        if response.candidates and response.candidates[0].finish_reason == 3: # 3 = SAFETY
                            logger.warning(f"LLM response blocked by safety filters for model {selected_model}.")
                            return "Error: Response blocked by safety filters."
                        
                    res = response.text
                
                logger.info(f"LLM response received from [{selected_model}]. Snippet: {res[:50]}...")
                return res
            except Exception as e:
                # Detect rate‑limit / quota errors
                err_msg = str(e).lower()
                if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg:
                    attempts += 1
                    if attempts >= internal_retries:
                        break
                    wait = 2 ** attempts
                    logger.warning(f"LLM rate limit encountered, retrying in {wait}s (attempt {attempts})")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM Error: {str(e)}")
                return f"Error generating response: {str(e)}"
        logger.error("LLM service rate limit exceeded after multiple retries.")
        return "Error: LLM service rate limit exceeded after multiple retries."



# ─── Strict, one-shot structured output ─────────────────────────────────────

class LLMCallError(Exception):
    """The LLM call itself failed (provider error, rate limit, safety block, not configured)."""


class UnparsableLLMResponse(Exception):
    def __init__(self, stage: str, raw: str, error: str):
        super().__init__(f"[{stage}] unparsable LLM response: {error}")
        self.stage, self.raw, self.error = stage, (raw or "")[:500], error


_LLM_FAILURE_PREFIXES = ("Error generating response", "Error: LLM service rate limit", "Error: Response blocked")


def is_llm_failure(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith(_LLM_FAILURE_PREFIXES) or t == "LLM Service not configured."


def parse_llm_json(text: str, schema=None, *, stage: str = "llm"):
    """Parse an LLM reply exactly once: a single JSON object, optionally in one ```json fence.

    Failure raises UnparsableLLMResponse and emits a backend alert; there is no repair or retry here.
    """
    import json
    import re
    from app.services.backend_alerts import emit_backend_alert
    try:
        t = (text or "").strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
        data = json.loads(fence.group(1) if fence else t)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return schema.model_validate(data) if schema is not None else data
    except Exception as e:
        emit_backend_alert("unparsable_llm_response", f"{stage}: {e}", stage=stage, raw=(text or "")[:500])
        raise UnparsableLLMResponse(stage, text, str(e)) from e


async def generate_text(prompt: str, model_name: str = None, llm=None) -> str:
    llm = llm or llm_service
    try:
        out = await llm.generate_response(prompt, model_name=model_name)
    except Exception as e:
        raise LLMCallError(str(e)) from e
    if is_llm_failure(out):
        raise LLMCallError(out)
    return out


async def generate_structured(prompt: str, schema, *, stage: str, model_name: str = None, llm=None):
    """One LLM call, one parse."""
    return parse_llm_json(await generate_text(prompt, model_name, llm), schema, stage=stage)


llm_service = LLMService()
