"""T5 param-first-query tests, driven by tests/fixtures/param-first-query.json.

All offline: fake resolver/execute/generate, fakeredis cache, EMBEDDING_BACKEND=hash.
ASSUMPTIONS (planned interfaces, see SPECS.md T5):
- extract_params(question, metric) where metric = {"params": {name: {"type","min","max","default","choices"?}}}
- QueryRequest(query=..., dissatisfied=...); QueryResponse has status, results, source,
  metric_id, cache_hit, metadata (dict with can_regenerate).
- QueryDeps(resolver, execute, generate, cache, catalog_version="v1").
- Fixture rows after the six SPECS.md rows (param-first .. cache-cross-user) are edge-case additions.
"""
import asyncio
import inspect
import json
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("EMBEDDING_BACKEND", "hash")

fakeredis = pytest.importorskip("fakeredis")
from fakeredis import aioredis as fake_aioredis  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.core.config import settings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "param-first-query.json"
CASES = json.loads(FIXTURE.read_text())


def _run(coro):
    return asyncio.run(coro)


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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


def _make_cache():
    from app.services.cache import CacheService
    return CacheService(redis_client=fake_aioredis.FakeRedis())


def _pipeline(inp):
    from app.api.endpoints import QueryDeps, answer_query
    from app.models.state import QueryRequest

    resolver = FakeResolver(inp["resolver"])
    counts = {"execute": 0, "generate": 0, "binds": []}
    rows_by_user = inp.get("rows_by_user", {})

    async def execute(sql, bind):
        counts["execute"] += 1
        counts["binds"].append(bind)
        if inp.get("execute_raises"):
            raise RuntimeError("boom")
        return rows_by_user.get((bind or {}).get("user_id"), [])

    async def generate(request, identity):
        counts["generate"] += 1
        return dict(inp["generate"])

    cache = _make_cache()
    deps = QueryDeps(resolver=resolver, execute=execute, generate=generate,
                     cache=cache, catalog_version="v1")

    async def go():
        out = []
        for st in inp["steps"]:
            req = QueryRequest(query=st["query"], dissatisfied=st["dissatisfied"])
            ident = {"user_id": st["user"], "role": st["role"]}
            try:
                out.append(await answer_query(req, ident, deps))
            except HTTPException as e:
                out.append(e)
        return out

    return _run(go()), resolver, counts


def _check_pipeline(case):
    inp, exp = case["input"], case["expected"]
    results, resolver, counts = _pipeline(inp)
    assert len(results) == len(exp["steps"])
    for got, want in zip(results, exp["steps"]):
        if "http" in want:
            assert isinstance(got, HTTPException)
            assert got.status_code == want["http"]
            assert got.detail == want["detail"]
            continue
        assert not isinstance(got, HTTPException), got
        if "status" in want:
            assert _field(got, "status") == want["status"]
        if "source" in want:
            assert _field(got, "source") == want["source"]
        if "metric_id" in want:
            assert _field(got, "metric_id") == want["metric_id"]
        if "cache_hit" in want:
            assert bool(_field(got, "cache_hit")) is want["cache_hit"]
        if "rows" in want:
            assert (_field(got, "results") or []) == want["rows"]
        if want.get("sql_has_named_bind"):
            assert re.search(r":[a-z_]+", _field(got, "sql") or "")
        if "can_regenerate" in want:
            assert (_field(got, "metadata") or {}).get("can_regenerate") is want["can_regenerate"]
    if "generate_calls" in exp:
        assert counts["generate"] == exp["generate_calls"]
    if "execute_calls" in exp:
        assert counts["execute"] == exp["execute_calls"]
    if "resolve_calls" in exp:
        assert resolver.calls == exp["resolve_calls"]
    if "execute_bind_contains" in exp:
        for k, v in exp["execute_bind_contains"].items():
            assert counts["binds"][0][k] == v


def _check_extract(case):
    from app.modules.param_resolver import extract_params
    inp = case["input"]
    got = extract_params(inp["question"], {"params": inp["params"]})
    exp = case["expected"]["params"]
    assert set(got) == set(exp)
    for k, v in exp.items():
        assert got[k] == v


def _check_authorize(case):
    from app.modules.param_resolver import authorize_generation
    i = case["input"]
    allowed, reason = authorize_generation(i["role"], i["dissatisfied"], i["param_attempted"])
    assert allowed is case["expected"]["allowed"]
    assert reason == case["expected"]["reason"]


def _check_route(case):
    from fastapi.testclient import TestClient
    from app import main as app_main
    from app.api import endpoints

    inp, exp = case["input"], case["expected"]
    app = app_main.app
    if inp["mode"] == "override":
        app.dependency_overrides[endpoints.get_identity] = lambda: inp["identity"]
        try:
            client = TestClient(app)
            r = client.post("/api/v1/query", json=inp["body"])
        finally:
            app.dependency_overrides.pop(endpoints.get_identity, None)
        assert r.status_code == exp["http"]
        assert exp["detail"] in json.dumps(r.json())
        return

    from starlette.requests import Request
    old = settings.ALLOW_HEADER_IDENTITY
    settings.ALLOW_HEADER_IDENTITY = inp["allow_header_identity"]
    try:
        scope = {"type": "http", "method": "POST", "path": "/api/v1/query",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in inp["headers"].items()]}
        request = Request(scope)
        sig = inspect.signature(endpoints.get_identity)
        kwargs = {}
        for name in sig.parameters:
            if name == "request":
                kwargs[name] = request
            elif name.lower() == "x_user_id":
                kwargs[name] = inp["headers"].get("X-User-Id")
            elif name.lower() == "x_user_role":
                kwargs[name] = inp["headers"].get("X-User-Role")
        ident = _run(_maybe_await(endpoints.get_identity(**kwargs)))
    finally:
        settings.ALLOW_HEADER_IDENTITY = old
    assert ident == exp["identity"]


def _check_goc(case):
    inp, exp = case["input"], case["expected"]
    cache = _make_cache()
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return inp["value"]

    async def go():
        if inp["action"] == "goc_sequence":
            return [await cache.get_or_compute(inp["key"], compute) for _ in range(2)]
        return await asyncio.gather(*[cache.get_or_compute(inp["key"], compute)
                                      for _ in range(inp["concurrency"])])

    res = _run(go())
    assert calls["n"] == exp["compute_calls"]
    if inp["action"] == "goc_sequence":
        assert [h for _, h in res] == exp["hits"]
        assert [v for v, _ in res] == exp["values"]
    else:
        assert sum(1 for _, h in res if h is False) == exp["hit_false_count"]
        vals = [v for v, _ in res]
        assert all(v == vals[0] for v in vals)


def _check_resolve(case):
    from app.modules.preprocessing.service import build_advanced_service
    from app.modules.param_resolver import ParamResolver
    inp, exp = case["input"], case["expected"]

    async def go():
        svc = await _maybe_await(build_advanced_service())
        r = ParamResolver(svc, threshold=inp.get("threshold"))
        return await r.resolve(inp["question"], user_id="u1")

    res = _run(go())
    assert res["status"] == exp["status"]
    if exp["status"] == "no_match":
        assert res["sql"] is None
    if exp.get("sql_has_named_bind"):
        assert re.search(r":[a-z_]+", res["sql"])
        assert isinstance(res["bind"], dict)
    if exp.get("confidence_in_unit_range"):
        assert 0.0 <= res["confidence"] <= 1.0
        assert res["confidence"] >= settings.PARAM_MATCH_THRESHOLD


def _check_db(case, test_db):
    inp, exp = case["input"], case["expected"]
    if "raises" in exp:
        cls = {"ValueError": ValueError, "Exception": Exception}[exp["raises"]]
        with pytest.raises(cls):
            test_db.execute_params(inp["sql"], inp["bind"])
        return
    rows = test_db.execute_params(inp["sql"], inp["bind"])
    assert [dict(r) for r in rows] == exp["rows"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_param_first_query(case, test_db):
    action = case["input"]["action"]
    if action == "pipeline":
        _check_pipeline(case)
    elif action == "extract":
        _check_extract(case)
    elif action == "authorize":
        _check_authorize(case)
    elif action == "route":
        _check_route(case)
    elif action in ("goc_sequence", "goc_concurrent"):
        _check_goc(case)
    elif action == "resolve":
        _check_resolve(case)
    elif action == "db":
        _check_db(case, test_db)
    else:
        pytest.fail(f"unknown action {action}")
