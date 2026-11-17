"""T6 session-audit-store tests, driven by tests/fixtures/session-audit-store.json.

All offline: SQLite ("sqlite://" static in-memory or tmp file URLs), PyJWT, fakeredis
(only for the query cache in the pipeline cases), EMBEDDING_BACKEND=hash.
ASSUMPTIONS (planned interfaces, see SPECS.md T6 and the task brief):
- app.core.auth: create_token, decode_token, AuthError(.status_code), get_identity(authorization,
  x_user_id, x_user_role) reading settings.ALLOW_HEADER_IDENTITY / AUTH_REQUIRED / JWT_SECRET.
- SessionStore, InsightStore, AuditDB as described in the brief (exception classes
  SessionForbidden / SessionNotFound live in app.services.session_store).
- handle_query(request, identity, deps) with QueryDeps(..., sessions=, insights=, audit=).
- list_sessions items may be ids or dicts with "session_id"/"id"; AuditDB.detail may be dict or JSON str.
- Response insight (if any) is read from QueryResponse.insight; chat insight rows must match it.
- Fixture rows after the five SPECS.md rows (audit-write .. vector-isolation) are edge-case additions.
- Fixture string values "@repeat:<ch>:<n>" expand to ch * n.
"""
import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("EMBEDDING_BACKEND", "hash")

fakeredis = pytest.importorskip("fakeredis")
jwt = pytest.importorskip("jwt")
from fakeredis import aioredis as fake_aioredis  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.core.config import settings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "session-audit-store.json"
CASES = json.loads(FIXTURE.read_text())

SECRET = "unit-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"


def _run(coro):
    return asyncio.run(coro)


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _expand(v):
    if isinstance(v, str):
        m = re.fullmatch(r"@repeat:(.):(\d+)", v)
        return m.group(1) * int(m.group(2)) if m else v
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _assert_iso_utc(s):
    assert isinstance(s, str) and s
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        assert dt.utcoffset() == timedelta(0)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 3600


def _detail(row):
    d = row.get("detail")
    if isinstance(d, str):
        d = json.loads(d)
    return d or {}


def _audit_rows(audit, **kw):
    rows = audit.query(**kw)
    return sorted((dict(r) for r in rows), key=lambda r: r["id"])


def _match_audit_row(row, want, labels, responses):
    for k, w in want.items():
        if k == "outcome_not":
            assert row["outcome"] != w
            continue
        got = row.get(k)
        if isinstance(w, str) and w.startswith("@"):
            if w == "@notnull":
                assert got is not None and got != ""
            elif w.startswith("@label:"):
                assert got == labels[w[len("@label:"):]]
            elif w.startswith("@sha:"):
                assert got == _sha(w[len("@sha:"):])
            elif w.startswith("@sha_resp_sql:"):
                assert got == _sha(_field(responses[int(w.split(":")[1])], "sql"))
            elif w.startswith("@resp_session:"):
                assert got == _field(responses[int(w.split(":")[1])], "session_id")
            else:
                pytest.fail(f"unknown token {w}")
        elif isinstance(w, bool):
            assert bool(got) is w, k
        else:
            assert got == w, k


# --------------------------------------------------------------------------- pipeline

class FakeResolver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = 0

    async def resolve(self, question, user_id=None):
        self.calls += 1
        c = self.cfg
        if not c.get("match"):
            return {"status": "no_match", "metric_id": None, "sql": None, "bind": {},
                    "params": {}, "confidence": 0.1, "candidates": []}
        bind = dict(c.get("bind") or {})
        bind["user_id"] = user_id
        return {"status": "matched", "metric_id": c["metric_id"], "sql": c["sql"], "bind": bind,
                "params": {}, "confidence": 0.95, "candidates": [c["metric_id"]]}


def _pipeline_case(case):
    from app.api.endpoints import QueryDeps, handle_query
    from app.core.config import settings as _s  # noqa: F401
    from app.models.state import QueryRequest
    from app.services.audit_db import AuditDB
    from app.services.cache import CacheService
    from app.services.insight_store import InsightStore
    from app.services.session_store import SessionStore

    inp, exp = case["input"], case["expected"]
    setup = inp.get("setup", {})
    resolver = FakeResolver(inp["resolver"])
    counts = {"execute": 0, "generate": 0}
    gen_histories = []
    rows_by_user = inp.get("rows_by_user", {})

    async def execute(sql, bind):
        counts["execute"] += 1
        if inp.get("execute_raises"):
            raise RuntimeError("boom")
        return rows_by_user.get((bind or {}).get("user_id"), [])

    async def generate(request, identity):
        counts["generate"] += 1
        gen_histories.append(list(_field(request, "conversation_history") or []))
        return dict(inp["generate"])

    async def go():
        sessions = SessionStore("sqlite://") if setup.get("sessions", True) else None
        insights = InsightStore("sqlite://") if setup.get("insights", True) else None
        audit = None
        if setup.get("audit", True):
            audit = AuditDB("sqlite://")
            await audit.start()
        cache = CacheService(redis_client=fake_aioredis.FakeRedis())
        deps = QueryDeps(resolver=resolver, execute=execute, generate=generate, cache=cache,
                         catalog_version="v1", sessions=sessions, insights=insights, audit=audit)
        labels = {}
        for seed in setup.get("seed", []):
            sid = sessions.create_session(seed["user"])
            labels[seed["label"]] = sid
            for i in range(seed["count"]):
                sessions.append_message(sid, seed["user"], "user" if i % 2 == 0 else "assistant", f"seed-{i}")
        results = []
        for st in inp["steps"]:
            kwargs = {"query": st["query"], "dissatisfied": st["dissatisfied"]}
            sess = st.get("session")
            if sess == "bogus":
                kwargs["session_id"] = "no-such-session-id"
            elif sess and sess != "new":
                kwargs["session_id"] = labels[sess]
            if "client_history" in st:
                kwargs["conversation_history"] = st["client_history"]
            req = QueryRequest(**kwargs)
            ident = {"user_id": st["user"], "role": st["role"]}
            try:
                res = await handle_query(req, ident, deps)
                if st.get("label") and _field(res, "session_id"):
                    labels[st["label"]] = _field(res, "session_id")
                results.append(res)
            except Exception as e:  # noqa: BLE001
                results.append(e)
        audit_rows = None
        if audit is not None:
            await audit.stop()
            audit_rows = _audit_rows(audit, limit=1000)
        return results, labels, sessions, insights, audit_rows

    results, labels, sessions, insights, audit_rows = _run(go())

    assert len(results) == len(exp["steps"])
    for step, got, want in zip(inp["steps"], results, exp["steps"]):
        if want.get("any"):
            continue
        if "http" in want:
            assert isinstance(got, HTTPException), got
            assert got.status_code == want["http"]
            if "detail" in want:
                assert got.detail == want["detail"]
            continue
        assert not isinstance(got, Exception), got
        if "status" in want:
            assert _field(got, "status") == want["status"]
        if "source" in want:
            assert _field(got, "source") == want["source"]
        if "cache_hit" in want:
            assert bool(_field(got, "cache_hit")) is want["cache_hit"]
        if "rows" in want:
            assert (_field(got, "results") or []) == want["rows"]
        if "session_id_label" in want:
            assert _field(got, "session_id") == labels[want["session_id_label"]]

    if "generate_calls" in exp:
        assert counts["generate"] == exp["generate_calls"]
    if "execute_calls" in exp:
        assert counts["execute"] == exp["execute_calls"]
    if "resolve_calls" in exp:
        assert resolver.calls == exp["resolve_calls"]

    for g in exp.get("generate_history", []):
        hist = gen_histories[g["call"]]
        blob = json.dumps(hist, default=str)
        for s in g.get("contains", []):
            assert s in blob
        for s in g.get("not_contains", []):
            assert s not in blob
        if "max_len" in g:
            assert len(hist) <= g["max_len"]

    for s in exp.get("stored", []):
        msgs = sessions.history(labels[s["label"]], s["user"], limit=100)
        assert [m["role"] for m in msgs] == s["roles"]
        if s.get("all_have_created_at"):
            for m in msgs:
                _assert_iso_utc(m["created_at"])

    if "audit_count" in exp:
        assert len(audit_rows) == exp["audit_count"]
    for row, want in zip(audit_rows or [], exp.get("audit", [])):
        _match_audit_row(row, want, labels, results)

    if exp.get("chat_insights_match_responses"):
        with_insight = [r for r in results if not isinstance(r, Exception) and _field(r, "insight")]
        for step, res in zip(inp["steps"], results):
            saved = insights.list_insights(step["user"], source="chat")
            assert len(saved) == len(with_insight)
            for it in saved:
                assert it["source"] == "chat"
                assert it["user_id"] == step["user"]
                _assert_iso_utc(it["created_at"])
                assert it["session_id"] == _field(res, "session_id")
            break


# --------------------------------------------------------------------------- ops

def _sid_item(item):
    if isinstance(item, str):
        return item
    return item.get("session_id") or item.get("id")


def _ops_case(case):
    from app.services import session_store as ss_mod
    from app.services.insight_store import InsightStore
    from app.services.session_store import SessionStore

    ops, want_results = case["input"]["ops"], case["expected"]["results"]
    assert len(ops) == len(want_results)
    sessions = SessionStore("sqlite://")
    insights = InsightStore("sqlite://")
    labels = {}
    rev = lambda v: labels.get(v, v)  # noqa: E731

    def resolve_want(v):
        if isinstance(v, str) and v.startswith("@label:"):
            return labels[v[len("@label:"):]]
        return _expand(v)

    for op, want in zip(ops, want_results):
        kind = op["op"]
        err = None
        out = None
        try:
            if kind == "sleep":
                time.sleep(op["seconds"])
            elif kind == "labels_distinct":
                vals = [labels[x] for x in op["labels"]]
                assert len(set(vals)) == len(vals)
            elif kind == "create":
                out = sessions.create_session(op["user"])
                assert isinstance(out, str) and out
                labels[op["as"]] = out
            elif kind == "assert_owner":
                sessions.assert_owner(rev(op["session"]), op["user"])
            elif kind == "append":
                out = sessions.append_message(rev(op["session"]), op["user"], op["role"],
                                              _expand(op["content"]), op.get("metadata"))
            elif kind == "history":
                kw = {"limit": op["limit"]} if "limit" in op else {}
                out = sessions.history(rev(op["session"]), op["user"], **kw)
            elif kind == "list_sessions":
                out = sessions.list_sessions(op["user"])
            elif kind == "save":
                out = insights.save_insight(op["user"], rev(op["session"]) if op["session"] else None,
                                            op["source"], op["question"], _expand(op["insight"]),
                                            op.get("sql"), op.get("metadata"))
                if op.get("as"):
                    labels[op["as"]] = out
            elif kind == "get":
                out = insights.get_insight(rev(op["id"]), op["user"])
            elif kind == "list":
                kw = {}
                if "source" in op:
                    kw["source"] = op["source"]
                if "limit" in op:
                    kw["limit"] = op["limit"]
                out = insights.list_insights(op["user"], **kw)
            else:
                pytest.fail(f"unknown op {kind}")
        except Exception as e:  # noqa: BLE001
            err = e

        if "error" in want:
            assert err is not None, f"{kind}: expected {want['error']}"
            assert type(err).__name__ == want["error"], repr(err)
            continue
        if want.get("hidden"):
            if err is not None:
                assert isinstance(err, (ss_mod.SessionForbidden, ss_mod.SessionNotFound)), repr(err)
            else:
                assert list(out) == []
            continue
        assert err is None, repr(err)
        if want.get("is_int"):
            assert isinstance(out, int)
        if "found" in want:
            if want["found"]:
                assert out is not None
                for k, v in (want.get("fields") or {}).items():
                    assert out[k] == resolve_want(v), k
            else:
                assert out is None
        if "contents" in want:
            assert [m["content"] for m in out] == _expand(want["contents"])
        if "roles" in want:
            assert [m["role"] for m in out] == want["roles"]
        if "metadata_last" in want:
            assert out[-1]["metadata"] == want["metadata_last"]
        if "keys" in want:
            for m in out:
                for k in want["keys"]:
                    assert k in m
        if "session_ids" in want:
            assert [_sid_item(x) for x in out] == [labels[x] for x in want["session_ids"]]
        if "questions" in want:
            assert [x["question"] for x in out] == want["questions"]
        if "count" in want:
            assert len(out) == want["count"]
        if want.get("created_at_iso_utc"):
            items = out if isinstance(out, list) else [out]
            assert items
            for it in items:
                _assert_iso_utc(it["created_at"])


# --------------------------------------------------------------------------- audit

def _audit_case(case):
    from app.services.audit_db import AuditDB
    inp, exp = case["input"], case["expected"]

    async def go():
        audit = AuditDB("sqlite://")
        await audit.start()
        for ev in inp["events"]:
            audit.record(_expand(ev))
        await audit.stop()
        if "queries" in inp:
            return [len(audit.query(**q)) for q in inp["queries"]], None
        return _audit_rows(audit, **inp["query"]), None

    out, _ = _run(go())
    if "queries" in inp:
        assert out == exp["query_counts"]
        return
    rows = out
    if "count" in exp:
        assert len(rows) == exp["count"]
    for row, want in zip(rows, exp.get("rows", [])):
        _match_audit_row(row, want, {}, [])
    blob = json.dumps(rows, default=str)
    for s in exp.get("forbidden_substrings", []):
        assert s not in blob
    if exp.get("distinct_ids"):
        assert len({r["id"] for r in rows}) == len(rows)
    if rows:
        d = _detail(rows[0])
        for k in exp.get("detail_absent_keys", []):
            assert k not in d
        for k, v in exp.get("detail_equals", {}).items():
            assert d[k] == v
        for k, n in exp.get("detail_max_len", {}).items():
            assert len(d[k]) <= n
        for k, n in exp.get("detail_min_len", {}).items():
            assert len(d[k]) >= n


def _audit_junk_case(case):
    from app.services.audit_db import AuditDB
    inp, exp = case["input"], case["expected"]

    async def go():
        audit = AuditDB("sqlite://")
        await audit.start()
        for j in inp["junk"]:
            audit.record(j)  # must never raise
        audit.record(inp["valid"])
        await audit.stop()
        return _audit_rows(audit, user_id="ok", limit=1000)

    assert len(_run(go())) == exp["valid_rows"]


def _audit_overflow_case(case):
    from app.services.audit_db import AuditDB
    n = case["input"]["n"]

    async def go():
        audit = AuditDB("sqlite://")
        await audit.start()
        for i in range(n):  # no await: the writer cannot drain, so a bounded queue must overflow
            audit.record({"user_id": "u1", "action": "QUERY", "outcome": "success", "row_count": i})
        await audit.stop()
        return audit.dropped, len(audit.query(limit=n * 2))

    dropped, stored = _run(go())
    assert isinstance(dropped, int) and dropped >= 0
    assert stored + dropped == n


def _audit_separate_case(case, tmp_path):
    from app.services.audit_db import AuditDB
    from app.services.session_store import SessionStore
    s_file, a_file = tmp_path / "sessions.db", tmp_path / "audit.db"
    s_url, a_url = f"sqlite:///{s_file}", f"sqlite:///{a_file}"
    assert s_url != a_url

    async def go():
        sessions = SessionStore(s_url)
        audit = AuditDB(a_url)
        await audit.start()
        sid = sessions.create_session("u1")
        sessions.append_message(sid, "u1", "user", "UNIQUE-MESSAGE-TEXT")
        audit.record({"user_id": "u1", "session_id": sid, "action": "QUERY", "outcome": "success",
                      "detail": {"note": "AUDIT-ONLY-MARKER"}})
        await audit.stop()
        return _audit_rows(audit)

    rows = _run(go())
    assert len(rows) == 1
    assert "UNIQUE-MESSAGE-TEXT" not in json.dumps(rows, default=str)
    assert s_file.exists() and a_file.exists()
    assert b"UNIQUE-MESSAGE-TEXT" not in a_file.read_bytes()
    assert b"AUDIT-ONLY-MARKER" not in s_file.read_bytes()


# --------------------------------------------------------------------------- sentinel

def _sentinel_case(case):
    from app.api.sentinel import persist_scan
    from app.services.audit_db import AuditDB
    from app.services.insight_store import InsightStore
    inp, exp = case["input"], case["expected"]
    ident = inp["identity"]

    async def go():
        insights = InsightStore("sqlite://")
        audit = AuditDB("sqlite://")
        await audit.start()
        ids = await _maybe_await(persist_scan(ident, inp["scan_id"], inp["detections"],
                                              insights=insights, audit=audit))
        await audit.stop()
        return ids, insights, _audit_rows(audit, action="SENTINEL_SCAN")

    ids, insights, rows = _run(go())
    assert len(ids) == exp["saved_count"]
    got = [insights.get_insight(i, ident["user_id"]) for i in ids]
    assert all(g is not None for g in got)
    assert [g["question"] for g in got] == exp["questions_in_order"]
    assert [g["insight"] for g in got] == exp["insights_in_order"]
    for g in got:
        assert g["session_id"] == exp["session_id"]
        assert g["source"] == exp["source"]
        assert g["user_id"] == exp["user_id"]
        _assert_iso_utc(g["created_at"])
    assert len(insights.list_insights(ident["user_id"], source="sentinel")) == exp["saved_count"]
    assert len(insights.list_insights("someone-else")) == exp["other_user_insights"]
    assert len(rows) == exp["audit_rows"]
    assert rows[0]["row_count"] == exp["audit_row_count"]
    assert rows[0]["user_id"] == ident["user_id"]


# --------------------------------------------------------------------------- vector

def _meta(r):
    return r["metadata"] if isinstance(r, dict) else r.metadata


async def _vstore(docs):
    from app.services import vector_store as vs
    store = vs.VectorStoreService("t6_test", backend=vs.InMemoryBackend())
    if docs:
        await store.add_documents([d["text"] for d in docs], [d["metadata"] for d in docs])
    return store


def _vector_case(case):
    inp, exp = case["input"], case["expected"]

    async def go():
        store = await _vstore(inp["docs"])
        return await store.search(inp["query"], inp["top_k"], inp["search_as"])

    res = _run(go())
    assert len(res) >= exp["min_results"]
    for r in res:
        uid = _meta(r).get("user_id")
        assert uid not in exp["forbidden_user_ids"]
        assert uid in exp["allowed_user_ids"]


def _vector_add_raises_case(case):
    inp, exp = case["input"], case["expected"]

    async def go():
        store = await _vstore([])
        with pytest.raises(ValueError):
            await store.add_documents([d["text"] for d in inp["docs"]], [d["metadata"] for d in inp["docs"]])
        return await store.search(inp["query"], 10, None)

    res = _run(go())
    assert len(res) == exp["global_results_after"]


# --------------------------------------------------------------------------- auth

def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def _forge(kind):
    from app.core import auth
    exp = int(time.time()) + 3600
    if kind == "alg_none":
        return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'u1', 'role': 'admin', 'exp': exp})}."
    if kind == "no_sub":
        return jwt.encode({"role": "user", "exp": exp}, SECRET, algorithm="HS256")
    if kind == "no_exp":
        return jwt.encode({"sub": "u1", "role": "user"}, SECRET, algorithm="HS256")
    if kind == "bad_role":
        return jwt.encode({"sub": "u1", "role": "root", "exp": exp}, SECRET, algorithm="HS256")
    good = auth.create_token("u1", "user")
    h, p, s = good.split(".")
    if kind == "tampered_sig":
        return f"{h}.{p}.{'A' * len(s)}"
    if kind == "tampered_payload":
        return f"{h}.{_b64({'sub': 'u1', 'role': 'admin', 'exp': exp})}.{s}"
    raise AssertionError(kind)


def _auth_case(case, monkeypatch):
    from app.core import auth
    inp = case["input"]
    want_results = case["expected"]["results"]
    monkeypatch.setattr(settings, "JWT_SECRET", inp.get("settings_secret", SECRET) if "settings_secret" in inp else SECRET, raising=False)
    labels = {}

    def sub(s):
        if s is None:
            return None
        for k, v in labels.items():
            s = s.replace("@" + k, v)
        return s

    assert len(inp["steps"]) == len(want_results)
    for op, want in zip(inp["steps"], want_results):
        kind = op["op"]
        out, err = None, None
        try:
            if kind == "create":
                kw = {}
                if "role" in op:
                    kw["role"] = op["role"]
                if "ttl" in op:
                    kw["ttl_seconds"] = op["ttl"]
                if "secret" in op:
                    kw["secret"] = op["secret"]
                out = auth.create_token(op["user"], **kw)
                if op.get("as"):
                    labels[op["as"]] = out
            elif kind == "forge":
                out = _forge(op["kind"])
                labels[op["as"]] = out
            elif kind == "decode":
                kw = {"secret": op["secret"]} if "secret" in op else {}
                out = auth.decode_token(sub(op["token"]), **kw)
            elif kind == "identity":
                monkeypatch.setattr(settings, "ALLOW_HEADER_IDENTITY", op.get("allow_header", False), raising=False)
                monkeypatch.setattr(settings, "AUTH_REQUIRED", op.get("auth_required", False), raising=False)
                out = _run(_maybe_await(auth.get_identity(
                    authorization=sub(op.get("authorization")),
                    x_user_id=op.get("x_user_id"), x_user_role=op.get("x_user_role"))))
            else:
                pytest.fail(f"unknown op {kind}")
        except Exception as e:  # noqa: BLE001
            err = e

        if "http" in want:
            assert isinstance(err, HTTPException), repr(err)
            assert err.status_code == want["http"]
        elif "error" in want:
            assert err is not None, f"{kind}: expected AuthError"
            assert type(err).__name__ == want["error"], repr(err)
            if "status_code" in want:
                assert err.status_code == want["status_code"]
            elif want["error"] == "AuthError":
                assert getattr(err, "status_code", 401) == 401
        elif want.get("ok"):
            assert err is None, repr(err)
        else:
            assert err is None, repr(err)
            assert out == want


# --------------------------------------------------------------------------- concurrent

def _concurrent_append_case(case, tmp_path):
    from app.services.session_store import SessionStore
    n = case["input"]["threads"]
    store = SessionStore(f"sqlite:///{tmp_path / 'sessions.db'}")
    sid = store.create_session("u1")

    def work(i):
        return store.append_message(sid, "u1", "user", f"msg-{i}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(work, range(n)))
    msgs = store.history(sid, "u1", limit=1000)
    assert len(msgs) == case["expected"]["count"]
    assert {m["content"] for m in msgs} == {f"msg-{i}" for i in range(n)}
    assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------------- driver

@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_session_audit_store(case, tmp_path, monkeypatch):
    action = case["input"]["action"]
    if action == "pipeline":
        _pipeline_case(case)
    elif action == "ops":
        _ops_case(case)
    elif action == "audit":
        _audit_case(case)
    elif action == "audit_junk":
        _audit_junk_case(case)
    elif action == "audit_overflow":
        _audit_overflow_case(case)
    elif action == "audit_separate":
        _audit_separate_case(case, tmp_path)
    elif action == "sentinel":
        _sentinel_case(case)
    elif action == "vector":
        _vector_case(case)
    elif action == "vector_add_raises":
        _vector_add_raises_case(case)
    elif action == "auth":
        _auth_case(case, monkeypatch)
    elif action == "concurrent_append":
        _concurrent_append_case(case, tmp_path)
    else:
        pytest.fail(f"unknown action {action}")


def test_audit_writer_survives_a_changed_event_loop():
    """The queue/task are bound to the loop that created them; a new loop (tests, reloads) must not lose or break events."""
    import asyncio
    from app.services.audit_db import AuditDB
    audit = AuditDB("sqlite://")

    async def burst(tag):
        await audit.start()
        for i in range(3):
            audit.record({"user_id": f"{tag}{i}", "action": "QUERY", "outcome": "success"})
        await audit.flush()

    asyncio.run(burst("a"))
    asyncio.run(burst("b"))          # second loop: previously "Queue is bound to a different event loop"
    users = sorted(r["user_id"] for r in audit.query(limit=100))
    assert users == ["a0", "a1", "a2", "b0", "b1", "b2"] and audit.dropped == 0
