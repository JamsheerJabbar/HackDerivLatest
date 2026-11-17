import asyncio
from langgraph.graph import StateGraph, END
from typing import Any, Dict, Optional
from app.models.state import GraphState
from app.modules.intent_classification import intent_module
from app.modules.clarification import clarification_module
from app.modules.preprocessing import preprocessing_service
from app.modules.sql_generation import sql_generation_module
from app.modules.validation import validation_module
from app.modules.visualization import visualization_module
from app.modules.insight_generation import insight_module
from app.services.database import db_service
from app.core.logger import logger
from app.services.deterministic_fallback import DeterministicFallback
from app.services.llm import LLMCallError, is_llm_failure
from app.services.retry_policy import NonRetryable, RetriesExhausted, RetryPolicy

# Node Definitions

async def classify_intent_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Node: [classify_intent]")
    query = state["user_question"]
    domain = state.get("domain", "general")
    conversation_history = state.get("conversation_history", [])
    
    # OPTIMIZATION: Run Intent Classification and Preprocessing in parallel
    # Preprocessing does NOT require intent as a pre-output, only the domain.
    logger.info(f"Starting Intent Classification and Preprocessing in parallel for domain: {domain}")
    
    task_intent = intent_module.classify(query, conversation_history, domain=domain)
    task_preproc = preprocessing_service.process(query, domain=domain)
    
    results = await asyncio.gather(task_intent, task_preproc)
    
    intent, confidence, complexity, needs_clarification = results[0]
    preproc_data = results[1]
    
    logger.info(f"Intent classified: {intent} (conf: {confidence}) | Needs Clarification: {needs_clarification}")
    
    return {
        "intent": intent,
        "confidence": confidence,
        "needs_clarification": needs_clarification,
        "relevant_columns": preproc_data["relevant_columns"],
        "few_shot_examples": preproc_data["few_shot_examples"],
        "entities": preproc_data["entities"], # Ensure entities are passed forward
        "metric_candidates": preproc_data.get("metric_candidates"),
        "domain": domain
    }

async def generate_clarification_node(state: GraphState) -> Dict[str, Any]:
    """Ask user for more information."""
    query = state["user_question"]
    domain = state.get("domain", "general")
    conversation_history = state.get("conversation_history", [])
    
    clarification_question = await clarification_module.generate_clarification(
        query, domain, conversation_history
    )
    
    return {
        "clarification_question": clarification_question,
        "status": "needs_clarification"
    }

class _Guidance(NonRetryable):
    """The generator answered with guidance (not SQL, not a failure): never retried."""


_fallback: Optional[DeterministicFallback] = None


def get_fallback() -> DeterministicFallback:
    """Lazily built deterministic fallback (catalog metric at >= FALLBACK_MIN_SIMILARITY, else error message)."""
    global _fallback
    if _fallback is None:
        from app.modules.param_resolver import get_shared_resolver
        _fallback = DeterministicFallback(
            get_shared_resolver(),
            lambda sql, bind: asyncio.to_thread(db_service.execute_logical, sql, bind))
    return _fallback


async def generate_sql_node(state: GraphState) -> Dict[str, Any]:
    """Generate -> validate -> execute as ONE attempt; retry (max 3) with stronger models; then deterministic fallback."""
    logger.info("Node: [generate_sql]")
    query = state["user_question"]
    domain = state.get("domain", "general")
    context = {
        "relevant_columns": state.get("relevant_columns", []),
        "few_shot_examples": state.get("few_shot_examples", []),
        "entities": state.get("entities", {}), # Fixed: Pass resolved entities forward
        "domain": domain,
        "conversation_history": state.get("conversation_history", []),
        "reference_templates": state.get("reference_templates") or [],
    }

    async def attempt(model: str, n: int):
        sql = await sql_generation_module.generate(query, {**context, "model_name": model})
        if is_llm_failure(sql):
            raise LLMCallError(sql)
        if sql.startswith("Error"):
            raise _Guidance(sql)
        ok, err = validation_module.validate(sql)
        if not ok:
            raise ValueError(f"validation: {err}")
        rows = await asyncio.to_thread(db_service.execute_params, sql, {})
        return sql, rows

    try:
        res = await RetryPolicy().run(attempt, stage="sql_generation")
        sql, rows = res.value
        logger.info(f"SQL generated and executed on attempt {res.attempts} ({res.models_used[-1]}): {sql[:50]}...")
        return {"generated_sql": sql, "query_result": rows, "status": "success"}
    except _Guidance as g:
        guidance = str(g)
        logger.info(f"SQL Generator returned guidance instead of SQL: {guidance[:50]}...")
        return {"generated_sql": None, "clarification_question": guidance.replace("Error:", "").strip(),
                "status": "needs_clarification"}
    except RetriesExhausted as e:
        logger.warning(f"SQL generation exhausted {e.attempts} attempts; trying deterministic fallback")
        fb = await get_fallback().handle(query)
        if fb["status"] == "success":
            return {"generated_sql": fb["sql"], "query_result": fb["results"], "status": "success",
                    "fallback": "deterministic"}
        return {"generated_sql": None, "query_result": None, "status": "failed",
                "validation_error": fb["error"]}

async def validate_sql_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Node: [validate_sql]")
    sql = state["generated_sql"]
    is_valid, error = validation_module.validate(sql)
    if not is_valid:
        logger.warning(f"SQL Validation Error: {error}")
    return {"validation_error": error}

async def repair_sql_node(state: GraphState) -> Dict[str, Any]:
    query = state["user_question"]
    invalid_sql = state["generated_sql"]
    error = state["validation_error"]
    
    repaired_sql = await sql_generation_module.repair(query, invalid_sql, error)
    
    return {
        "generated_sql": repaired_sql,
        "validation_error": None,
        "retry_count": state.get("retry_count", 0) + 1
    }

async def execute_query_node(state: GraphState) -> Dict[str, Any]:
    logger.info("Node: [execute_query]")
    sql = state["generated_sql"]
    try:
        from app.modules.catalog.templates import assert_select_only
        assert_select_only(sql)  # defense in depth: db_service.execute commits non-row statements
        results = db_service.execute(sql)
        logger.info(f"Query execution successful. Rows: {len(results)}")
        return {"query_result": results, "status": "success"}
    except Exception as e:
        logger.error(f"SQL EXECUTION ERROR: {str(e)}")
        return {"query_result": None, "status": "failed", "validation_error": str(e)}

async def recommend_visualization_node(state: GraphState) -> Dict[str, Any]:
    """Analyze results and recommend visualization."""
    logger.info("Node: [recommend_visualization]")
    query = state["user_question"]
    sql = state.get("generated_sql", "")
    results = state.get("query_result", [])
    
    if not results:
        logger.info("No results for visualization recommendation.")
        return {"visualization_config": None}

    vis_config = await visualization_module.recommend(query, sql, results)
    logger.info(f"Visualization recommended: {vis_config.get('chart_type') if vis_config else 'None'}")
    
    # If LLM generated mock data because real results were empty
    if vis_config and vis_config.get("is_mock") and "mock_data" in vis_config:
        logger.info("Injecting mock data for demonstration.")
        return {
            "visualization_config": vis_config,
            "query_result": vis_config["mock_data"] # Update results with mock data
        }
    
    return {"visualization_config": vis_config}

async def insight_recommendation_node(state: GraphState) -> Dict[str, Any]:
    """Provide executive insights and recommendations based on the results."""
    logger.info("Node: [insight_recommendation]")
    query = state["user_question"]
    results = state.get("query_result", [])
    domain = state.get("domain", "general")
    status = state.get("status", "unknown")
    
    if status != "success":
        logger.info(f"Skipping insights because status is {status}")
        return {}

    insights = await insight_module.generate(query, results, domain)
    logger.info("Insights generated successfully.")
    
    return {
        "insight": insights.get("insight"),
        "recommendation": insights.get("recommendation")
    }

async def guidance_node(state: GraphState) -> Dict[str, Any]:
    """Generate a helpful response for off-topic or schema queries."""
    from app.services.llm import llm_service
    from app.modules.schema_understanding import schema_module
    
    query = state["user_question"]
    domain = state.get("domain", "general")
    intent = state.get("intent", "OFF_TOPIC")
    
    # Get schema context to help guide the user
    schema_context = schema_module.get_schema_string(domain=domain)
    
    if intent == "SCHEMA_QUERY":
        prompt = f"""
        You are a Database Expert. The user wants to know about the system structure.
        User asked: "{query}"
        
        Database Schema Context:
        {schema_context}
        
        Task:
        Provide a very clear and helpful list of the tables and columns available. 
        Structure it so it's easy to read (use bullet points).
        Mention what kind of questions you can answer with these tables.
        """
    else:
        prompt = f"""
        You are a helpful database assistant.
        The user asked: "{query}"
        This is off-topic or cannot be answered by the database.

        Database Knowledge:
        {schema_context}

        Task:
        Write a polite, helpful response that:
        1. Validates that you cannot answer the specific question.
        2. Suggests 3 relevant things they CAN ask about instead.
        3. Be concise and friendly.
        """
    
    guidance = await llm_service.generate_response(prompt)
    
    # We use 'clarification_question' field to show this as a standard chat message
    return {
        "clarification_question": guidance,
        "status": "needs_clarification" 
    }

async def format_response_node(state: GraphState) -> Dict[str, Any]:
    # Format results if needed
    return {}

# Conditional Edges

def route_after_intent(state: GraphState):
    """Route based on whether clarification is needed."""
    if state["intent"] in ["OFF_TOPIC", "SCHEMA_QUERY"]:
        return "guidance"
    if state.get("needs_clarification", False):
        return "clarification"
    if state["confidence"] < 0.5:
        return "clarification"
    return "generate_sql"

def route_after_validation(state: GraphState):
    if state["validation_error"]:
        if state.get("retry_count", 0) < 3:
            return "repair_sql"
        else:
            return "format_response"  # Or failure node
    return "execute_query"

# Graph Construction

workflow = StateGraph(GraphState)

workflow.add_node("classify_intent", classify_intent_node)
workflow.add_node("clarification", generate_clarification_node)
workflow.add_node("guidance", guidance_node)
workflow.add_node("generate_sql", generate_sql_node)
workflow.add_node("validate_sql", validate_sql_node)
workflow.add_node("repair_sql", repair_sql_node)
workflow.add_node("execute_query", execute_query_node)
workflow.add_node("recommend_visualization", recommend_visualization_node)
workflow.add_node("insight_recommendation", insight_recommendation_node)
workflow.add_node("format_response", format_response_node)

workflow.set_entry_point("classify_intent")

workflow.add_conditional_edges(
    "classify_intent",
    route_after_intent,
    {
        "generate_sql": "generate_sql",
        "clarification": "clarification",
        "guidance": "guidance"
    }
)

# Clarification ends the conversation (user must respond)
workflow.add_edge("clarification", END)
workflow.add_edge("guidance", END)

def route_after_sql_generation(state: GraphState):
    if state.get("status") == "needs_clarification" or state.get("needs_clarification"):
        return "end"
    if state.get("status") == "success" and state.get("query_result") is not None:
        return "present"  # already generated, validated and executed inside generate_sql
    if state.get("status") == "failed":
        return "format_response"
    return "validate_sql"  # legacy path


async def present_node(state: GraphState) -> Dict[str, Any]:
    return {}

workflow.add_conditional_edges(
    "generate_sql",
    route_after_sql_generation,
    {
        "validate_sql": "validate_sql",
        "present": "present",
        "format_response": "format_response",
        "end": END
    }
)

workflow.add_conditional_edges(
    "validate_sql",
    route_after_validation,
    {
        "execute_query": "execute_query",
        "repair_sql": "repair_sql",
        "format_response": "format_response"
    }
)

workflow.add_edge("repair_sql", "validate_sql")
workflow.add_node("present", present_node)
workflow.add_edge("present", "recommend_visualization")
workflow.add_edge("present", "insight_recommendation")
workflow.add_edge("execute_query", "recommend_visualization")
workflow.add_edge("execute_query", "insight_recommendation")

# Join parallel branches
workflow.add_edge(["recommend_visualization", "insight_recommendation"], "format_response")
workflow.add_edge("format_response", END)

app_graph = workflow.compile()
