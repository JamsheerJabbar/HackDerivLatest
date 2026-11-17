from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from app.core.auth import get_identity
from app.orchestration.workflow import app_graph
from app.orchestration.sentinel_agent import SentinelBrainstormer
from app.orchestration.risk_scorer import risk_scorer
from app.orchestration.deep_dive import deep_dive_generator
from app.orchestration.correlation_engine import correlation_engine
from app.orchestration.narrative_generator import narrative_generator
from app.orchestration.scan_memory import scan_memory
from app.services.slack_notifier import notify_slack, notify_slack_narrative
from app.core.config import settings
from app.core.logger import logger, _mission_log_buffer, _sse_event_queue, _mission_meta
from datetime import datetime
import asyncio
import json

router = APIRouter()
brainstormer = SentinelBrainstormer()


async def _start_audit(audit) -> None:
    start = getattr(audit, "start", None)
    if audit is not None and callable(start):
        await start()


async def persist_scan(identity: Dict[str, str], scan_id: str, detections: List[Dict[str, Any]], *,
                       insights, audit) -> List[str]:
    """Store each successful Sentinel insight (source 'sentinel', owned by the caller) and audit the scan."""
    import asyncio
    ids: List[str] = []
    await asyncio.to_thread(insights.register_scan, scan_id, identity["user_id"])  # history reads are owner-only
    for d in detections:
        if d.get("error") or not d.get("insight"):
            continue
        iid = await asyncio.to_thread(
            insights.save_insight, identity["user_id"], scan_id, "sentinel", d.get("mission_name") or d.get("mission_id"),
            d["insight"], d.get("sql"),
            {"mission_id": d.get("mission_id"), "domain": d.get("domain"), "severity": d.get("severity"),
             "risk_score": d.get("risk_score"), "recommendation": d.get("recommendation")})
        d["insight_id"] = iid  # lets the UI rate this insight
        ids.append(iid)
    if audit is not None:
        await _start_audit(audit)
        audit.record({"user_id": identity["user_id"], "session_id": scan_id, "action": "SENTINEL_SCAN",
                      "outcome": "success", "role": identity.get("role"), "source": "sentinel",
                      "row_count": len(detections), "detail": {"insights_saved": len(ids)}})
    return ids


def _persist_deps():
    from app.api.endpoints import get_query_deps
    d = get_query_deps()
    return d.insights, d.audit



# ─── Insight ratings, human handoff and insight anomaly detection ───────────

class _FeedbackDeps:
    def __init__(self, insights, handoff, audit):
        self.insights, self.handoff, self.audit = insights, handoff, audit


_feedback: Dict[str, Any] = {}


def get_feedback_deps() -> "_FeedbackDeps":
    if not _feedback:
        from app.services.human_handoff import HandoffStore
        insights, audit = _persist_deps()
        _feedback["deps"] = _FeedbackDeps(insights, HandoffStore(), audit)
    return _feedback["deps"]


def _audit_event(audit, identity: Dict[str, str], action: str, outcome: str, **detail) -> None:
    if audit is not None:
        audit.record({"user_id": identity["user_id"], "action": action, "outcome": outcome,
                      "role": identity.get("role"), "source": "sentinel", "detail": detail})


async def _notify_reviewers(insight_id: str, thread_id: str) -> None:
    try:
        from app.services.slack_notifier import _post_to_slack
        await _post_to_slack(settings.SLACK_CHANNEL,
                             f":mag: Insight {insight_id} was rated incorrect; review thread {thread_id} is open.")
    except Exception as e:  # notification is best effort
        logger.warning(f"Reviewer notification failed: {e}")


async def _detect_anomaly(identity: Dict[str, str], insights, audit) -> Dict[str, Any]:
    """Compute the caller's false-rating anomaly status; flag it once and alert when the count exceeds the threshold."""
    import asyncio
    from app.services.backend_alerts import emit_backend_alert
    status = await asyncio.to_thread(insights.anomaly_status, identity["user_id"])
    status["flag_id"] = None
    if status["flagged"]:
        had_open = bool(await asyncio.to_thread(insights.list_anomaly_flags, identity["user_id"], "flagged"))
        status["flag_id"] = await asyncio.to_thread(insights.record_anomaly_flag, identity["user_id"], "sentinel",
                                                    status["false_count"])
        if not had_open:  # first crossing only: no repeated alerts while a flag is open
            emit_backend_alert("insight_anomaly",
                               f"user exceeded {status['threshold']} false Sentinel insight ratings",
                               user_id=identity["user_id"], false_count=status["false_count"], flag_id=status["flag_id"])
            _audit_event(audit, identity, "INSIGHT_ANOMALY", "flagged", false_count=status["false_count"],
                         flag_id=status["flag_id"])
    return status


async def rate_insight_flow(identity: Dict[str, str], insight_id: str, rating: bool, *, insights, handoff, audit) -> Dict[str, Any]:
    import asyncio
    from fastapi import HTTPException
    from app.services.insight_store import InsightNotFound
    try:
        res = await asyncio.to_thread(insights.rate_insight, insight_id, identity["user_id"], rating)
    except InsightNotFound:
        _audit_event(audit, identity, "INSIGHT_RATING", "not_found", insight_id=insight_id)
        raise HTTPException(status_code=404, detail="Insight not found")
    thread_id = None
    if not rating:
        thread_id = await asyncio.to_thread(handoff.open_thread, insight_id, identity["user_id"], "rated incorrect")
        if res["changed"]:
            asyncio.create_task(_notify_reviewers(insight_id, thread_id))
    anomaly = await _detect_anomaly(identity, insights, audit)
    await _start_audit(audit)
    _audit_event(audit, identity, "INSIGHT_RATING", "human_review" if not rating else "rated",
                 insight_id=insight_id, rating=rating, thread_id=thread_id)
    return {"insight_id": insight_id, "rating": bool(rating), "status": "human_review" if not rating else "rated",
            "thread_id": thread_id, "anomaly": anomaly}


class RatingBody(BaseModel):
    rating: bool


class MessageBody(BaseModel):
    content: str


@router.post("/insights/{insight_id}/rating")
async def rate_insight(insight_id: str, body: RatingBody, identity: Dict[str, str] = Depends(get_identity),
                       d: _FeedbackDeps = Depends(get_feedback_deps)):
    return await rate_insight_flow(identity, insight_id, body.rating, insights=d.insights, handoff=d.handoff, audit=d.audit)


@router.get("/insights/anomaly")
async def insight_anomaly_status(identity: Dict[str, str] = Depends(get_identity),
                                 d: _FeedbackDeps = Depends(get_feedback_deps)):
    import asyncio
    return await asyncio.to_thread(d.insights.anomaly_status, identity["user_id"])


@router.post("/insights/anomaly/detect")
async def detect_insight_anomaly(identity: Dict[str, str] = Depends(get_identity),
                                 d: _FeedbackDeps = Depends(get_feedback_deps)):
    """The 'Insight Anomaly Detection' button: recompute, flag (once) and alert when false ratings exceed the threshold."""
    return await _detect_anomaly(identity, d.insights, d.audit)


@router.get("/handoff/threads")
async def list_handoff_threads(identity: Dict[str, str] = Depends(get_identity),
                               d: _FeedbackDeps = Depends(get_feedback_deps)):
    import asyncio
    return {"threads": await asyncio.to_thread(d.handoff.list_threads, identity["user_id"], identity["role"])}


@router.get("/handoff/threads/{thread_id}")
async def get_handoff_thread(thread_id: str, identity: Dict[str, str] = Depends(get_identity),
                             d: _FeedbackDeps = Depends(get_feedback_deps)):
    import asyncio
    from fastapi import HTTPException
    from app.services.human_handoff import HandoffForbidden, HandoffNotFound
    try:
        return await asyncio.to_thread(d.handoff.get_thread, thread_id, identity["user_id"], identity["role"])
    except HandoffNotFound:
        raise HTTPException(status_code=404, detail="Thread not found")
    except HandoffForbidden:
        raise HTTPException(status_code=403, detail="thread_forbidden")


@router.post("/handoff/threads/{thread_id}/messages")
async def post_handoff_message(thread_id: str, body: MessageBody, identity: Dict[str, str] = Depends(get_identity),
                               d: _FeedbackDeps = Depends(get_feedback_deps)):
    import asyncio
    from fastapi import HTTPException
    from app.services.human_handoff import STAFF_ROLES, HandoffForbidden, HandoffNotFound
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be empty")
    try:
        await asyncio.to_thread(d.handoff.get_thread, thread_id, identity["user_id"], identity["role"])  # authorize
        author_role = "human" if identity["role"] in STAFF_ROLES else "user"
        await asyncio.to_thread(d.handoff.post_message, thread_id, identity["user_id"], author_role, body.content)
        _audit_event(d.audit, identity, "HANDOFF_MESSAGE", "success", thread_id=thread_id, author_role=author_role)
        return await asyncio.to_thread(d.handoff.get_thread, thread_id, identity["user_id"], identity["role"])
    except HandoffNotFound:
        raise HTTPException(status_code=404, detail="Thread not found")
    except HandoffForbidden:
        raise HTTPException(status_code=403, detail="thread_forbidden")

# ─── Helper: execute a single mission through the NL2SQL graph ──────────────

async def run_mission(mission: dict) -> dict:
    # Set up per-mission log capture
    log_buf: list = []
    _mission_log_buffer.set(log_buf)
    # Set mission metadata so the log handler can tag SSE entries
    _mission_meta.set({
        "mission_id": mission["id"],
        "mission_name": mission["name"],
        "domain": mission["domain"],
        "severity": mission.get("severity", "MEDIUM"),
    })

    initial_state = {
        "user_question": mission["query"],
        "domain": mission["domain"],
        "conversation_history": [],
        "retry_count": 0,
    }

    try:
        result = await app_graph.ainvoke(initial_state)
        query_results = result.get("query_result") or []

        # Dynamic risk scoring from actual data
        scoring = risk_scorer.score(mission, query_results)

        return {
            "mission_id": mission["id"],
            "mission_name": mission["name"],
            "domain": mission["domain"],
            "severity": scoring["severity"],
            "risk_score": scoring["risk_score"],
            "risk_factors": scoring["factors"],
            "sql": result.get("generated_sql"),
            "data_count": len(query_results),
            "results": query_results,
            "visualization_config": result.get("visualization_config"),
            "insight": result.get("insight"),
            "recommendation": result.get("recommendation"),
            "original_query": mission["query"],
            "depth": mission.get("depth", 0),
            "parent_mission_id": mission.get("parent_mission_id"),
            "rationale": mission.get("rationale", ""),
            "timestamp": "LIVE AUDIT",
            "logs": log_buf,
        }
    except Exception as e:
        logger.error(f"Sentinel Mission {mission['id']} Failed: {str(e)}")
        return {
            "mission_id": mission["id"],
            "mission_name": mission["name"],
            "domain": mission["domain"],
            "severity": mission.get("severity", "MEDIUM"),
            "risk_score": 0,
            "risk_factors": {},
            "error": str(e),
            "depth": mission.get("depth", 0),
            "parent_mission_id": mission.get("parent_mission_id"),
            "data_count": 0,
            "results": [],
            "logs": log_buf,
        }
    finally:
        _mission_log_buffer.set(None)


# ─── SSE Streaming Endpoint ─────────────────────────────────────────────────

@router.get("/scan/stream")
async def stream_sentinel_scan(identity: Dict[str, str] = Depends(get_identity)):
    """
    Server-Sent Events endpoint that streams the full Sentinel v2 pipeline:
    1. Brainstorm missions (adaptive)
    2. Stream each mission result as it completes
    3. Deep-dive follow-ups on high-risk findings
    4. Cross-domain correlation
    5. Executive intelligence brief
    6. Record scan to memory
    """

    async def event_generator():
        all_results = []

        # Unified event queue for real-time log streaming + mission results
        event_queue = asyncio.Queue()
        _sse_event_queue.set(event_queue)

        # ── Phase 1: Brainstorm ──────────────────────────────────────────
        logger.info("SENTINEL v2: Brainstorming adaptive missions...")
        missions = await brainstormer.brainstorm_missions(count_per_domain=2)
        if not missions:
            logger.warning("Brainstorm failed, using fallback.")
            missions = [
                {"id": "fall-001", "name": "Basic Security Audit",
                 "query": "Show users with risk_level HIGH and account_status ACTIVE",
                 "domain": "security", "severity": "HIGH"},
            ]

        adaptive_ctx = scan_memory.get_adaptive_context()
        yield _sse("scan_started", {
            "total_missions": len(missions),
            "missions": [{"id": m["id"], "name": m["name"], "domain": m["domain"]} for m in missions],
            "adaptive_context": {
                "scan_count": adaptive_ctx["scan_count"],
                "domain_weights": adaptive_ctx["domain_weights"],
            },
        })

        # ── Phase 2: Execute missions in parallel, stream as they complete ─
        logger.info(f"SENTINEL v2: Executing {len(missions)} missions...")

        async def run_and_queue(m):
            result = await run_mission(m)
            await event_queue.put(("mission", result))

        tasks = [asyncio.create_task(run_and_queue(m)) for m in missions]

        completed = 0
        total = len(missions)
        while completed < total:
            event_type, data = await event_queue.get()
            if event_type == "log":
                yield _sse("mission_log", data)
            elif event_type == "mission":
                all_results.append(data)
                completed += 1
                yield _sse("mission_complete", data)
                asyncio.create_task(notify_slack(data))

        await asyncio.gather(*tasks, return_exceptions=True)

        # Drain any remaining log events in the queue
        while not event_queue.empty():
            event_type, data = event_queue.get_nowait()
            if event_type == "log":
                yield _sse("mission_log", data)

        yield _sse("scan_complete", {"total": len(all_results)})

        # ── Phase 3: Deep Dive follow-ups ────────────────────────────────
        if settings.DEEP_DIVE_ENABLED:
            deep_dive_missions = []
            for res in list(all_results):
                if await deep_dive_generator.should_deep_dive(res):
                    followups = await deep_dive_generator.generate_followups(res, depth=0)
                    if followups:
                        deep_dive_missions.extend(followups)
                        yield _sse("deep_dive_started", {
                            "parent_mission_id": res["mission_id"],
                            "followup_count": len(followups),
                        })

            if deep_dive_missions:
                logger.info(f"SENTINEL v2: Running {len(deep_dive_missions)} deep-dive follow-ups...")

                async def run_dd(m):
                    result = await run_mission(m)
                    await event_queue.put(("mission", result))

                dd_tasks = [asyncio.create_task(run_dd(m)) for m in deep_dive_missions]

                dd_completed = 0
                dd_total = len(deep_dive_missions)
                while dd_completed < dd_total:
                    event_type, data = await event_queue.get()
                    if event_type == "log":
                        yield _sse("mission_log", data)
                    elif event_type == "mission":
                        all_results.append(data)
                        dd_completed += 1
                        yield _sse("mission_complete", data)
                        asyncio.create_task(notify_slack(data))

                await asyncio.gather(*dd_tasks, return_exceptions=True)

                # Drain remaining logs
                while not event_queue.empty():
                    event_type, data = event_queue.get_nowait()
                    if event_type == "log":
                        yield _sse("mission_log", data)

        # ── Phase 4: Cross-Domain Correlation ────────────────────────────
        clusters = []
        if settings.CORRELATION_ENABLED:
            logger.info("SENTINEL v2: Running cross-domain correlation...")
            yield _sse("correlation_started", {})
            clusters = await correlation_engine.correlate(all_results)
            yield _sse("correlation_complete", {"clusters": clusters})

        # ── Phase 5: Executive Intelligence Brief ────────────────────────
        logger.info("SENTINEL v2: Generating intelligence brief...")
        yield _sse("narrative_started", {})
        narrative = await narrative_generator.generate(
            detections=all_results,
            clusters=clusters,
            scan_metadata={"total_missions": len(all_results), "timestamp": "LIVE"},
        )
        yield _sse("narrative_complete", narrative)
        asyncio.create_task(notify_slack_narrative(narrative))

        # ── Phase 6: Record to scan memory ───────────────────────────────
        scan_memory.record_scan(all_results)
        scan_id = f"scan-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
        scan_memory.save_full_scan(scan_id, all_results, clusters, narrative)
        try:
            _ins, _aud = _persist_deps()
            await persist_scan(identity, scan_id, all_results, insights=_ins, audit=_aud)
            yield _sse("insights_saved", {"scan_id": scan_id, "insight_ids": {
                r["mission_id"]: r["insight_id"] for r in all_results if r.get("insight_id")}})
        except Exception as e:  # persistence must never break the live scan
            logger.warning(f"SENTINEL: could not persist insights/audit: {e}")
        logger.info(f"SENTINEL v2: Scan complete. {len(all_results)} total detections recorded. ID: {scan_id}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Original non-streaming endpoint (backward compatible) ──────────────────

@router.get("/scan")
async def run_sentinel_scan(identity: Dict[str, str] = Depends(get_identity)):
    """
    Original Sentinel scan — returns all results at once.
    Kept for backward compatibility. Now includes risk scoring + narrative.
    """
    logger.info("SENTINEL: Running non-streaming scan...")

    missions = await brainstormer.brainstorm_missions(count_per_domain=2)
    if not missions:
        missions = [
            {"id": "fall-001", "name": "Basic Security Audit",
             "query": "Show users with risk_level HIGH and account_status ACTIVE",
             "domain": "security", "severity": "HIGH"},
        ]

    tasks = [run_mission(m) for m in missions]
    results = await asyncio.gather(*tasks)
    results_list = list(results)

    # Send Slack alerts for HIGH/CRITICAL findings
    for r in results_list:
        asyncio.create_task(notify_slack(r))

    # Generate narrative and send to Slack
    narrative = await narrative_generator.generate(
        detections=results_list,
        clusters=[],
        scan_metadata={"total_missions": len(results_list), "timestamp": "LIVE"},
    )
    asyncio.create_task(notify_slack_narrative(narrative))

    # Record to scan memory
    scan_memory.record_scan(results_list)
    scan_id = f"scan-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    scan_memory.save_full_scan(scan_id, results_list, narrative=narrative)
    try:
        _ins, _aud = _persist_deps()
        await persist_scan(identity, scan_id, results_list, insights=_ins, audit=_aud)
    except Exception as e:  # persistence must never break the scan
        logger.warning(f"SENTINEL: could not persist insights/audit: {e}")

    return {"status": "success", "detections": results_list, "narrative": narrative}


# ─── Scan History Endpoints ─────────────────────────────────────────────────

@router.get("/history")
async def list_scan_history(identity: Dict[str, str] = Depends(get_identity)):
    """Metadata for the caller's own persisted scans, newest first."""
    import asyncio
    insights, _ = _persist_deps()
    owned = set(await asyncio.to_thread(insights.owned_scan_ids, identity["user_id"]))
    return {"scans": [s for s in scan_memory.list_scans() if s.get("scan_id") in owned]}


@router.get("/history/{scan_id}")
async def get_scan_history(scan_id: str, identity: Dict[str, str] = Depends(get_identity)):
    """Full data for one of the caller's scans. Unowned and unknown scans both return 404 (no existence leak)."""
    import asyncio
    from fastapi import HTTPException
    insights, _ = _persist_deps()
    if not await asyncio.to_thread(insights.owns_scan, scan_id, identity["user_id"]):
        raise HTTPException(status_code=404, detail="Scan not found")
    data = scan_memory.get_scan(scan_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return data


# ─── Helpers ────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
