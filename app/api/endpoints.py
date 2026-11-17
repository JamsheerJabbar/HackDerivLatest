import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from app.models.state import QueryRequest, QueryResponse
from app.alert_system.metric_models import MetricRequest, MetricResponse
from app.alert_system.metric_workflow import metric_app_graph
from app.orchestration.workflow import app_graph
from app.core.auth import get_identity  # noqa: F401  (re-exported; overridable via dependency_overrides)
from app.core.config import settings
from app.core.logger import logger
from app.modules.catalog.templates import assert_select_only
from app.modules.param_resolver import ParamResolver, authorize_generation, get_shared_resolver
from app.services.cache import cache_service
from app.services.audit_db import AuditDB
from app.services.database import db_service
from app.services.insight_store import InsightStore
from app.services.session_store import SessionForbidden, SessionNotFound, SessionStore

router = APIRouter()

@dataclass
class QueryDeps:
    resolver: Any   # async resolve(question, user_id) -> dict (see ParamResolver)
    execute: Callable[[str, Dict[str, Any]], Any]   # fn(sql, bind) -> rows (sync or async)
    generate: Callable[..., Awaitable[Dict[str, Any]]]  # async fn(request, identity) -> {"sql", "rows", ...}
    cache: Any      # CacheService
    catalog_version: str = "v1"
    sessions: Any = None   # SessionStore (optional)
    insights: Any = None   # InsightStore (optional)
    audit: Any = None      # AuditDB (optional)


async def _run(execute, sql: str, bind: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = execute(sql, bind)
    return await out if inspect.isawaitable(out) else out


async def answer_query(request: QueryRequest, identity: Dict[str, str], deps: QueryDeps) -> QueryResponse:
    """Parameterized first; free-form generation only when dissatisfied AND role == 'super'."""
    user_id, role = identity["user_id"], identity["role"]
    key = deps.cache.make_key(request.query, user_id, role, deps.catalog_version)
    if not request.dissatisfied:
        cached = await deps.cache.get(key)
        if cached is not None:
            return QueryResponse(sql=cached.get("sql"), results=cached.get("results"), status="success",
                                 source=cached.get("source"), metric_id=cached.get("metric_id"),
                                 cache_hit=True, is_final=True)

    param_rows, resolved = None, None
    try:
        resolved = await deps.resolver.resolve(request.query, user_id)
        if resolved.get("status") == "matched":
            param_rows = await _run(deps.execute, resolved["sql"], resolved["bind"])
    except Exception as e:
        logger.warning(f"Parameterized path failed: {e}")
        param_rows = None

    if not request.dissatisfied:
        if param_rows is None:
            missing = (resolved or {}).get("unavailable")
            if missing:
                return QueryResponse(sql=None, results=None, status="no_match", is_final=False,
                                     error=f"That metric needs data this database does not have ({missing['reason']}).",
                                     metadata={"can_regenerate": True, "unavailable_metric": missing["metric_id"],
                                               "reason": missing["reason"]})
            return QueryResponse(sql=None, results=None, status="no_match", is_final=False,
                                 error="No catalog metric matches this question.",
                                 metadata={"can_regenerate": True})
        if len(param_rows) <= 500:
            await deps.cache.set(key, {"sql": resolved["sql"], "results": param_rows, "source": "parameterized",
                                       "metric_id": resolved["metric_id"]})
        return QueryResponse(sql=resolved["sql"], results=param_rows, status="success", source="parameterized",
                             metric_id=resolved["metric_id"], cache_hit=False, is_final=True)

    allowed, reason = authorize_generation(role, True, True)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    gen = await deps.generate(request, identity)
    sql = gen.get("sql")
    if gen.get("status") == "needs_clarification":
        return QueryResponse(sql=None, results=None, status="needs_clarification", is_final=False,
                             clarification_question=gen.get("clarification_question"), source="generated")
    if gen.get("status") == "failed":
        return QueryResponse(sql=None, results=None, status="failed", error=gen.get("error"), source="generated",
                             is_final=True)
    try:
        assert_select_only(sql or "")
    except ValueError as e:
        return QueryResponse(sql=sql, results=None, status="failed", error=str(e), source="generated", is_final=True)
    rows = gen.get("rows")
    return QueryResponse(sql=sql, results=rows, status="success", source="generated",
                         visualization_config=gen.get("visualization_config"), insight=gen.get("insight"),
                         recommendation=gen.get("recommendation"), cache_hit=False, is_final=True,
                         metadata={"fallback": gen["fallback"]} if gen.get("fallback") else {})


def _audit(deps: QueryDeps, identity: Dict[str, str], session_id: Optional[str], request: QueryRequest,
           outcome: str, resp: Optional[QueryResponse], t0: float) -> None:
    if deps.audit is None:
        return
    rows = (resp.results or []) if resp is not None else []
    deps.audit.record({
        "user_id": identity["user_id"], "session_id": session_id, "action": "QUERY", "outcome": outcome,
        "role": identity["role"], "source": resp.source if resp else None, "sql": resp.sql if resp else None,
        "row_count": len(rows), "cache_hit": bool(resp.cache_hit) if resp else None,
        "latency_ms": (time.perf_counter() - t0) * 1000,
        "detail": {"status": resp.status if resp else None, "dissatisfied": request.dissatisfied,
                   "metric_id": resp.metric_id if resp else None, "query": request.query}})


async def handle_query(request: QueryRequest, identity: Dict[str, str], deps: QueryDeps) -> QueryResponse:
    """answer_query + session ownership, server-side chat context, insight storage and audit trail."""
    t0 = time.perf_counter()
    user_id = identity["user_id"]
    session_id = request.session_id
    if deps.audit is not None:
        await deps.audit.start()
    if deps.sessions is not None:
        try:
            if session_id:
                await asyncio.to_thread(deps.sessions.assert_owner, session_id, user_id)
            else:
                session_id = await asyncio.to_thread(deps.sessions.create_session, user_id)
        except SessionForbidden:
            _audit(deps, identity, session_id, request, "denied", None, t0)
            raise HTTPException(status_code=403, detail="session_forbidden")
        except SessionNotFound:
            _audit(deps, identity, session_id, request, "not_found", None, t0)
            raise HTTPException(status_code=404, detail="session_not_found")
        hist = await asyncio.to_thread(deps.sessions.history, session_id, user_id, 10)
        # server-side history is authoritative; client-supplied history is ignored
        request = request.model_copy(update={"session_id": session_id, "conversation_history":
                                             [{"role": m["role"], "content": m["content"]} for m in hist]})
        await asyncio.to_thread(deps.sessions.append_message, session_id, user_id, "user", request.query,
                                {"dissatisfied": request.dissatisfied})
    try:
        resp = await answer_query(request, identity, deps)
    except HTTPException as e:
        _audit(deps, identity, session_id, request, "denied" if e.status_code in (401, 403) else "error", None, t0)
        raise
    except Exception:
        _audit(deps, identity, session_id, request, "failed", None, t0)
        raise
    resp.session_id = session_id
    if deps.sessions is not None:
        summary = (resp.insight if isinstance(resp.insight, str) else None) or resp.error or \
            f"{resp.status}: {len(resp.results or [])} rows"
        meta = {"status": resp.status, "source": resp.source, "metric_id": resp.metric_id,
                "sql_hash": hashlib.sha256(resp.sql.encode()).hexdigest() if resp.sql else None}
        await asyncio.to_thread(deps.sessions.append_message, session_id, user_id, "assistant", summary, meta)
    if deps.insights is not None and resp.insight:
        await asyncio.to_thread(deps.insights.save_insight, user_id, session_id, "chat", request.query,
                                resp.insight, resp.sql, {"metric_id": resp.metric_id, "source": resp.source})
    _audit(deps, identity, session_id, request, "success" if resp.status == "success" else resp.status, resp, t0)
    return resp


_default_resolver: Optional[ParamResolver] = None


def _get_resolver() -> ParamResolver:
    return get_shared_resolver()


class _DefaultResolver:
    async def resolve(self, question: str, user_id: Optional[str] = None):
        return await _get_resolver().resolve(question, user_id)


async def _default_generate(request: QueryRequest, identity: Dict[str, str]) -> Dict[str, Any]:
    refs: List[str] = []
    try:
        res = await _get_resolver().resolve(request.query, identity["user_id"])
        svc = _get_resolver().service
        refs = [svc.by_id[c["id"]]["sql"] for c in res.get("candidates", [])[:3] if c["id"] in svc.by_id]
    except Exception:
        pass
    result = await app_graph.ainvoke({
        "user_question": request.query, "domain": request.domain,
        "conversation_history": request.conversation_history, "retry_count": 0, "reference_templates": refs})
    if result.get("status") == "needs_clarification":
        return {"status": "needs_clarification", "clarification_question": result.get("clarification_question")}
    if result.get("status") == "failed" and not result.get("generated_sql"):
        return {"status": "failed", "error": result.get("validation_error") or "Generation failed."}
    return {"sql": result.get("generated_sql"), "rows": result.get("query_result"), "fallback": result.get("fallback"),
            "visualization_config": result.get("visualization_config"), "insight": result.get("insight"),
            "recommendation": result.get("recommendation")}


async def _default_execute(sql: str, bind: Dict[str, Any]):
    # catalog templates are written against the canonical schema: map them onto the physical one
    return await asyncio.to_thread(db_service.execute_logical, sql, bind)


_stores: Dict[str, Any] = {}


def get_query_deps() -> QueryDeps:
    if not _stores:
        _stores.update(sessions=SessionStore(), insights=InsightStore(), audit=AuditDB())
    return QueryDeps(resolver=_DefaultResolver(), execute=_default_execute, generate=_default_generate,
                     cache=cache_service, catalog_version=settings.CATALOG_VERSION, **_stores)


@router.post("/query", response_model=QueryResponse)
async def query_database(request: QueryRequest, identity: Dict[str, str] = Depends(get_identity),
                         deps: QueryDeps = Depends(get_query_deps)):
    """
    Answer a natural language question. Parameterized catalog templates first (cached);
    free-form SQL generation only after user dissatisfaction and only for role 'super'.
    """
    logger.info(f"Incoming query: {request.query} | Domain: {request.domain} | dissatisfied={request.dissatisfied}")
    return await handle_query(request, identity, deps)

@router.post("/alert", response_model=MetricResponse)
async def create_alert(request: MetricRequest):
    """
    Process a request to create a data metric/alert.
    """
    logger.info(f"Relaying alert request to Metric Workflow: {request.query}")
    
    initial_state = {
        "user_query": request.query,
        "domain": request.domain,
        "conversation_history": request.conversation_history,
        "metric": {},
        "status": "pending",
        "explanation": ""
    }
    
    result = await metric_app_graph.ainvoke(initial_state)
    
    return MetricResponse(
        status=result.get("status", "failed"),
        metric=result.get("metric"),
        explanation=result.get("explanation", ""),
        clarification_question=result.get("clarification_question")
    )
