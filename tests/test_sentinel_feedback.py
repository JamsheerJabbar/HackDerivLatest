"""T8 sentinel-feedback tests, driven by tests/fixtures/sentinel-feedback.json.

All offline: SQLite ("sqlite://" static in-memory), real AuditDB on SQLite or a fake audit,
backend alert Slack disabled (SLACK_BOT_TOKEN empty), FastAPI TestClient with dependency overrides.
ASSUMPTIONS (planned interfaces, see SPECS.md T8 and the task brief):
- InsightStore.save_insight(user, session, source, question, insight, sql, metadata) -> id
  (same call shape as tests/test_session_audit_store.py).
- InsightStore.rate_insight / get_rating / false_rating_count / anomaly_status /
  record_anomaly_flag / list_anomaly_flags / acknowledge_anomaly, InsightNotFound.
- HandoffStore (open_thread, post_message, get_thread, list_threads, resolve_thread),
  HandoffForbidden / HandoffNotFound in app.services.human_handoff.
- app.api.sentinel.rate_insight_flow(identity, insight_id, rating, insights=, handoff=, audit=),
  get_feedback_deps() overridable via app.dependency_overrides, identity = {"user_id", "role"}.
- SentinelBrainstormer.parse_missions(text) callable on an instance built with __new__.
- Fixture tokens in expected dicts: "@notnull", "@null", "@label:<name>".
- Window semantics beyond "recent ratings are inside a 30 day window" are not exercised
  (no way to backdate ratings through the public interface).
"""
import asyncio
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("EMBEDDING_BACKEND", "hash")

from fastapi import HTTPException  # noqa: E402

from app.core.config import settings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sentinel-feedback.json"
CASES = json.loads(FIXTURE.read_text())


class _Done:
    """Return value usable both sync and with `await`."""

    def __await__(self):
        return iter(())


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event=None, **kw):
        ev = dict(event or {})
        ev.update(kw)
        self.events.append(ev)
        return _Done()

    def count(self, action):
        return sum(1 for e in self.events if e.get("action") == action)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from app.services import backend_alerts as ba
    monkeypatch.setattr(settings, "SLACK_BOT_TOKEN", "", raising=False)
    ba.reset_alerts()
    yield
    ba.reset_alerts()


def _run(coro):
    return asyncio.run(coro)


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def _exc(name):
    from app.services import human_handoff as hh
    from app.services import insight_store as ins
    return {"InsightNotFound": ins.InsightNotFound, "HandoffForbidden": hh.HandoffForbidden,
            "HandoffNotFound": hh.HandoffNotFound, "ValueError": ValueError}[name]


def _check(got, want, labels, path="result"):
    if isinstance(want, str) and want.startswith("@"):
        if want == "@notnull":
            assert got is not None and got != "", path
        elif want == "@null":
            assert got is None, path
        elif want.startswith("@label:"):
            assert got == labels[want[len("@label:"):]], path
        else:
            pytest.fail(f"unknown token {want}")
    elif isinstance(want, dict):
        assert isinstance(got, dict), path
        for k, w in want.items():
            assert k in got, f"{path}.{k} missing"
            _check(got[k], w, labels, f"{path}.{k}")
    elif isinstance(want, bool) or want is None:
        assert got is want, path
    else:
        assert got == want, path


def _msg_subset(msg, want, labels):
    _check(msg, want, labels, "message")


# --------------------------------------------------------------------------- scenario

def _scenario(case):
    from app.api import sentinel
    from app.core import auth
    from app.main import app
    from app.services import backend_alerts as ba
    from app.services.audit_db import AuditDB
    from app.services.human_handoff import HandoffStore
    from app.services.insight_store import InsightStore
    from fastapi.testclient import TestClient

    inp = case["input"]

    async def go():
        insights = InsightStore("sqlite://")
        handoff = HandoffStore("sqlite://")
        mode = inp.get("audit", "none")
        audit = None
        if mode == "real":
            audit = AuditDB("sqlite://")
            await audit.start()
        elif mode == "fake":
            audit = FakeAudit()
        labels = {}
        stopped = False
        deps = SimpleNamespace(insights=insights, handoff=handoff, audit=audit)
        rev = lambda v: labels.get(v, v)  # noqa: E731
        ident = lambda o: {"user_id": o["user"], "role": o.get("role", "user")}  # noqa: E731

        def audit_count(action):
            if mode == "real":
                return len(audit.query(action=action))
            if mode == "fake":
                return audit.count(action)
            return 0

        try:
            for op in inp["ops"]:
                kind = op["op"]
                want_exc = op.get("raises")
                try:
                    out = None
                    if kind == "save":
                        out = insights.save_insight(op["user"], None, op.get("source", "sentinel"),
                                                    f"q-{op['as']}", f"insight text {op['as']}", None, None)
                        labels[op["as"]] = out
                        continue
                    if kind == "save_many":
                        for n in range(1, op["n"] + 1):
                            name = f"{op['prefix']}{n}"
                            labels[name] = insights.save_insight(
                                op["user"], None, op.get("source", "sentinel"), f"q-{name}",
                                f"insight text {name}", None, None)
                        continue
                    if kind == "flow":
                        try:
                            out = await sentinel.rate_insight_flow(
                                ident(op), rev(op["id"]), op["rating"],
                                insights=insights, handoff=handoff, audit=audit)
                        except HTTPException as e:
                            assert op.get("raises_http") == e.status_code, f"unexpected HTTP {e.status_code}"
                            continue
                        assert "raises_http" not in op, "expected HTTPException"
                        if op.get("as_thread"):
                            labels[op["as_thread"]] = out["thread_id"]
                    elif kind == "rate":
                        out = insights.rate_insight(rev(op["id"]), op["user"], op["rating"])
                    elif kind == "get_rating":
                        out = insights.get_rating(rev(op["id"]), op["user"])
                    elif kind == "count":
                        kw = {k: op[k] for k in ("source", "window_days") if k in op}
                        out = insights.false_rating_count(op["user"], **kw)
                    elif kind == "status":
                        kw = {k: op[k] for k in ("source", "window_days", "threshold") if k in op}
                        out = insights.anomaly_status(op["user"], **kw)
                        if "ids_len" in op:
                            assert len(out["insight_ids"]) == op["ids_len"]
                    elif kind == "record_flag":
                        out = insights.record_anomaly_flag(op["user"], op["source"], op["count"])
                        labels[op["as"]] = out
                        if "same_as" in op:
                            assert out == labels[op["same_as"]]
                        if "differs_from" in op:
                            assert out != labels[op["differs_from"]]
                        continue
                    elif kind == "list_flags":
                        kw = {"status": op["status"]} if "status" in op else {}
                        out = insights.list_anomaly_flags(op["user"], **kw)
                        assert len(out) == op["expect_len"]
                        if "expect_status" in op:
                            assert [f["status"] for f in out] == op["expect_status"]
                        continue
                    elif kind == "ack":
                        insights.acknowledge_anomaly(rev(op["flag"]), op["user"])
                    elif kind == "alerts":
                        assert ba.alert_counts().get(op["kind"], 0) == op["expect"]
                        continue
                    elif kind == "audit_count":
                        if mode == "real" and not stopped:
                            # flush the async writer; scenarios do no flow ops after an audit check
                            await audit.stop()
                            stopped = True
                        assert audit_count(op["action"]) == op["expect"]
                        continue
                    elif kind == "same":
                        assert labels[op["a"]] == labels[op["b"]]
                        continue
                    elif kind == "h_open":
                        out = handoff.open_thread(op["id"] if op["id"] not in labels else labels[op["id"]],
                                                  op["user"], op.get("reason"))
                        labels[op["as"]] = out
                        if "same_as" in op:
                            assert out == labels[op["same_as"]]
                        if "differs_from" in op:
                            assert out != labels[op["differs_from"]]
                        continue
                    elif kind == "h_post":
                        out = handoff.post_message(rev(op["thread"]), op["author"], op["role"], op["content"])
                        if op.get("as"):
                            labels[op["as"]] = out
                        assert out
                    elif kind == "h_get":
                        out = handoff.get_thread(rev(op["thread"]), op["user"], op["role"])
                        if "messages_len" in op:
                            assert len(out["messages"]) == op["messages_len"]
                        if "first_message" in op:
                            _msg_subset(out["messages"][0], op["first_message"], labels)
                        if "last_message" in op:
                            _msg_subset(out["messages"][-1], op["last_message"], labels)
                        if "message_roles" in op:
                            assert [m["author_role"] for m in out["messages"]] == op["message_roles"]
                    elif kind == "h_list":
                        out = handoff.list_threads(op["user"], op["role"])
                        assert len(out) == op["expect_len"]
                        if "expect_ids" in op:
                            got = [t["thread_id"] if isinstance(t, dict) else t for t in out]
                            assert got == [labels[x] for x in op["expect_ids"]]
                        continue
                    elif kind == "h_resolve":
                        handoff.resolve_thread(rev(op["thread"]), op["user"], op["role"])
                    elif kind == "http":
                        app.dependency_overrides[auth.get_identity] = lambda o=op: ident(o)
                        app.dependency_overrides[sentinel.get_feedback_deps] = lambda: deps
                        try:
                            client = TestClient(app)
                            path = op["path"].format(**labels)
                            kw = {"json": op["json"]} if "json" in op else {}
                            r = client.request(op["method"], path, **kw)
                        finally:
                            app.dependency_overrides.pop(auth.get_identity, None)
                            app.dependency_overrides.pop(sentinel.get_feedback_deps, None)
                        if "expect_status" in op:
                            assert r.status_code == op["expect_status"], r.text
                        if "expect_status_in" in op:
                            assert r.status_code in op["expect_status_in"], r.text
                        if r.status_code == 200:
                            body = r.json()
                            if "expect" in op:
                                _check(body, op["expect"], labels, "body")
                            if "expect_threads_len" in op:
                                assert len(body["threads"]) == op["expect_threads_len"]
                        continue
                    else:
                        pytest.fail(f"unknown op {kind}")
                except Exception as e:  # noqa: BLE001
                    if want_exc and isinstance(e, _exc(want_exc)):
                        continue
                    raise
                if want_exc:
                    pytest.fail(f"expected {want_exc} for op {op}")
                if "expect" in op and kind not in ("h_get",):
                    _check(out, op["expect"], labels, kind)
                elif kind == "h_get" and "expect" in op:
                    _check(out, op["expect"], labels, kind)
        finally:
            if mode == "real" and not stopped:
                try:
                    await audit.stop()
                except Exception:  # noqa: BLE001
                    pass

    _run(go())


# --------------------------------------------------------------------------- persist_scan

def _persist_scan(case):
    from app.api.sentinel import persist_scan
    from app.services.audit_db import AuditDB
    from app.services.insight_store import InsightStore
    inp = case["input"]

    async def go():
        insights = InsightStore("sqlite://")
        audit = AuditDB("sqlite://")
        await audit.start()
        dets = json.loads(json.dumps(inp["detections"]))
        try:
            ids = await _maybe_await(persist_scan(inp["identity"], inp["scan_id"], dets,
                                                  insights=insights, audit=audit))
        finally:
            await audit.stop()
        return ids, dets, insights

    ids, dets, insights = _run(go())
    assert len(ids) == case["expected"]["saved"]
    assert [d["insight_id"] for d in dets] == list(ids)
    for d in dets:
        assert insights.get_insight(d["insight_id"], inp["identity"]["user_id"]) is not None
        assert insights.get_insight(d["insight_id"], "someone-else") is None


# --------------------------------------------------------------------------- parse_missions

def _mission(i):
    return {"id": f"m{i}", "name": f"Mission {i}", "query": f"query {i}",
            "domain": ["security", "compliance", "operations", "risk"][i % 4],
            "severity": ["CRITICAL", "HIGH", "MEDIUM", "LOW"][i % 4]}


def _parse(case):
    from app.orchestration.sentinel_agent import SentinelBrainstormer
    from app.services import backend_alerts as ba
    from app.services.llm import UnparsableLLMResponse

    inp, exp = case["input"], case["expected"]
    text = inp["text"] if inp["action"] == "parse" else json.dumps([_mission(i) for i in range(inp["n"])])
    parser = SentinelBrainstormer.__new__(SentinelBrainstormer)
    if exp.get("raises"):
        with pytest.raises(UnparsableLLMResponse):
            parser.parse_missions(text)
    else:
        out = parser.parse_missions(text)
        assert isinstance(out, list)
        if "ids" in exp:
            assert [m["id"] for m in out] == exp["ids"]
        if "count" in exp:
            assert len(out) == exp["count"]
        for m in out:
            for f in ("id", "name", "query", "domain", "severity"):
                assert isinstance(m[f], str)
    assert ba.alert_counts().get("unparsable_llm_response", 0) == exp["alerts"]


# --------------------------------------------------------------------------- driver

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_sentinel_feedback(case):
    action = case["input"]["action"]
    if action == "scenario":
        _scenario(case)
    elif action == "persist_scan":
        _persist_scan(case)
    elif action in ("parse", "parse_many"):
        _parse(case)
    else:
        pytest.fail(f"unknown action {action}")
