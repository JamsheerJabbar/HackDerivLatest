from typing import Dict, Any, Tuple
from typing import Optional

from pydantic import BaseModel

from app.services.llm import LLMCallError, UnparsableLLMResponse, is_llm_failure, llm_service, parse_llm_json
from app.core.logger import logger

class IntentReply(BaseModel):
    intent: str  # required: a JSON object without an intent is treated as unparsable
    confidence: float = 0.9
    complexity: str = "Simple"
    needs_clarification: bool = False
    clarity_score: float = 0.9
    off_topic_reason: Optional[str] = None


class IntentClassificationModule:
    """
    HLD 3.3: Intent Classification Module
    Determines query type, complexity, confidence level, and if clarification is needed.
    """
    
    async def classify(self, query: str, conversation_history: list = None, domain: str = "general") -> Tuple[str, float, str, bool]:
        """
        Returns (intent, confidence, complexity, needs_clarification)
        """
        logger.info(f"Classifying intent for domain: {domain} | Query: {query}")
        from app.modules.learning import learning_service
        
        # Load domain config
        domain_config = learning_service.get_domain_config(domain)
        schema_context = domain_config.get("schema_context", "")
        # We treat the stored prompt as "Domain Specific Instructions" 
        # (even if it currently contains 'You are an AI...', we'll just append it contextually)
        domain_instructions = domain_config.get("prompts", {}).get("intent", "")
        
        history_context = ""
        if conversation_history:
            history_context = "\n\nConversation History:\n"
            for msg in conversation_history[-3:]:  # Last 3 messages for context
                # Handle both dict and object messages
                role = msg.get('role', 'user') if isinstance(msg, dict) else getattr(msg, 'role', 'user')
                content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                history_context += f"- {role}: {content}\n"
        
        # Base Prompt Template
        prompt = f"""
You are an AI assistant for a Database system.
Your job is to classify the user's intent.

DATABASE SCHEMA CONTEXT (STRICT TRUTH):
{schema_context}

{history_context}

User Query: "{query}"

DOMAIN CONTEXT & INSTRUCTIONS:
{domain_instructions}

INSTRUCTIONS:
Analyze the query and return a valid JSON object with:
- "intent": "SELECT", "UPDATE", "DELETE", "INSERT", "SCHEMA_QUERY", "OFF_TOPIC", or "UNKNOWN"
- "confidence": float between 0.0 and 1.0
- "complexity": "Simple", "Medium", "Complex"
- "needs_clarification": boolean
- "off_topic_reason": string (if intent is OFF_TOPIC)

INTENT GUIDELINES:
1. **SCHEMA_QUERY**: User asks about the database structure, tables available, column names, or "what can you do?".
   - Example: "list columns", "what tables are there?", "show schema".
2. **OFF_TOPIC**: Query is about weather, sports... or greetings.
3. **SELECT**: Query asks for SPECIFIC DATA that exists in our 3 tables (users, transactions, login_events).
4. **UNKNOWN**: Query is gibberish.

CLARIFICATION GUIDELINES:
- Set needs_clarification=true if:
    a) The query mentions tables or concepts NOT in our schema (e.g., "rules", "policies", "compliance_alerts", "payments_table").
    b) The user wants to "discuss" or "explain" something rather than retrieve data.
    c) The query is vague (e.g., "show data", "check UAE").
- If the user asks for "compliance rules", set needs_clarification=true because we have DATA, not RULES.
- Your goal is to catch queries that would lead to SQL hallucinations and force a clarification instead.
- If you can reasonably map the request to one of the 3 tables without guessing, set needs_clarification=false.

Output JSON only.
"""
        
        try:
            from app.core.config import settings
            response_text = await llm_service.generate_response(prompt, model_name=settings.INTENT_MODEL)
            if is_llm_failure(response_text):
                raise LLMCallError(response_text)
            data = parse_llm_json(response_text, IntentReply, stage="intent_classification")  # one shot, alerts on failure

            intent, confidence, complexity = data.intent, data.confidence, data.complexity
            needs_clarification = data.needs_clarification

            # Override needs_clarification if clarity is too low
            if data.clarity_score < 0.6:
                needs_clarification = True

            logger.info(f"Classification Result: {intent} (conf: {confidence}) | Needs Clarification: {needs_clarification}")
            return intent, confidence, complexity, needs_clarification

        except UnparsableLLMResponse:
            # Backend alert already emitted; do not guess confidence. No second LLM call.
            return "SELECT", 0.5, "Simple", False
        except Exception as e:
            logger.error(f"Intent classification error: {str(e)}")
            return "SELECT", 0.9, "Simple", False

intent_module = IntentClassificationModule()
