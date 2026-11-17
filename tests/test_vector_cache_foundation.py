"""T1 vector-cache-foundation tests, driven by tests/fixtures/vector-cache-foundation.json.

Uses fakeredis and an in-memory vector backend only; no live Redis or Postgres.
ASSUMPTION: app.services.vector_store exports `InMemoryBackend` (zero-arg ctor)
and `VectorStoreUnavailable`. The backend-down case wraps any backend in a
protocol-agnostic proxy so it does not depend on backend method names.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services.cache import CacheService
from app.services import vector_store as vs
from app.services.vector_store import VectorStoreService, VectorStoreUnavailable

fakeredis = pytest.importorskip("fakeredis")
from fakeredis import aioredis as fake_aioredis  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "vector-cache-foundation.json"
CASES = json.loads(FIXTURE.read_text())


class DownBackend:
    """Every attribute is an async callable that raises, and records the call."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _fail(*a, **kw):
            self.calls.append((name, a, kw))
            raise ConnectionError("pgvector down")

        return _fail


class BrokenRedis:
    """Stand-in Redis client where every operation raises."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _fail(*a, **kw):
            raise ConnectionError("redis down")

        return _fail


def _meta(r):
    return r["metadata"] if isinstance(r, dict) else r.metadata


def _text(r):
    if isinstance(r, dict):
        return r.get("text", r.get("document", r.get("content")))
    return getattr(r, "text", getattr(r, "document", getattr(r, "content", None)))


async def _store(docs):
    store = VectorStoreService("t1_test", backend=vs.InMemoryBackend())
    if docs:
        await store.add_documents([d["text"] for d in docs], [d["metadata"] for d in docs])
    return store


def _cache():
    return CacheService(redis_client=fake_aioredis.FakeRedis())


async def run_vector_upsert(i, e):
    store = await _store(i["docs"])
    res = await store.search(i["query"], i["top_k"], i["search_as"])
    assert len(res) == e["count"]
    for r in res:
        for k in e["metadata_keys"]:
            assert k in _meta(r)


async def run_vector_isolation(i, e):
    store = await _store(i["docs"])
    res = await store.search(i["query"], i["top_k"], i["search_as"])
    for r in res:
        uid = _meta(r).get("user_id")
        assert uid not in e["forbidden_user_ids"]
        assert uid in e["allowed_user_ids"]


async def run_vector_global_catalog_visible(i, e):
    store = await _store(i["docs"])
    res = await store.search(i["query"], i["top_k"], i["search_as"])
    assert [_text(r) for r in res] == e["texts"]


async def run_cache_hit(i, e):
    c = _cache()
    k1 = c.make_key(i["query"], i["user_id"], i["role"], i["catalog_version"])
    k2 = c.make_key(i["query"], i["user_id"], i["role"], i["catalog_version"])
    assert (k1 == k2) is e["same_key"]
    assert await c.get(k1) is e["first_get"]
    await c.set(k1, i["value"], i["ttl"])
    assert (await c.get(k2) == i["value"]) is e["second_get_equals_value"]


async def run_cache_cross_user(i, e):
    c = _cache()
    ka = c.make_key(i["query"], i["user_a"], i["role"], i["catalog_version"])
    kb = c.make_key(i["query"], i["user_b"], i["role"], i["catalog_version"])
    assert (ka != kb) is e["keys_differ"]
    await c.set(ka, i["value_a"], 60)
    assert await c.get(kb) is e["b_get"]
    assert (await c.get(ka) == i["value_a"]) is e["a_get_equals_value"]


async def run_cache_invalidate(i, e):
    c = _cache()
    ko = c.make_key(i["query"], i["user_id"], i["role"], i["version_old"])
    kn = c.make_key(i["query"], i["user_id"], i["role"], i["version_new"])
    assert (ko != kn) is e["keys_differ"]
    await c.set(ko, i["value"], 60)
    assert await c.get(kn) is e["get_after_bump"]
    assert (await c.get(ko) == i["value"]) is e["old_still_readable"]


async def run_cache_role_in_key(i, e):
    c = _cache()
    ka, kb = (c.make_key(i["query"], i["user_id"], r, i["catalog_version"]) for r in i["roles"])
    assert (ka != kb) is e["keys_differ"]


async def run_cache_key_normalization(i, e):
    c = _cache()
    ka = c.make_key(i["query_a"], i["user_id"], i["role"], i["catalog_version"])
    kb = c.make_key(i["query_b"], i["user_id"], i["role"], i["catalog_version"])
    assert (ka == kb) is e["keys_equal"]


async def run_cache_ttl_expiry(i, e):
    c = _cache()
    await c.set(i["key"], i["value"], i["ttl"])
    assert await c.get(i["key"]) == i["value"]
    await asyncio.sleep(i["ttl"] + 0.2)
    assert await c.get(i["key"]) is e["get_after_expiry"]


async def run_cache_redis_unavailable(i, e):
    c = CacheService(redis_client=BrokenRedis())
    assert await c.get(i["key"]) is e["get"]
    await c.set(i["key"], i["value"], i["ttl"])  # must not raise

    async def compute():
        return "computed"

    assert await c.single_flight(i["key"], compute) == e["single_flight_result"]


async def run_cache_single_flight_concurrent(i, e):
    c = _cache()
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"rows": [1, 2, 3]}

    results = await asyncio.gather(*[c.single_flight(i["key"], compute) for _ in range(i["concurrency"])])
    assert calls == e["coro_calls"]
    assert all(r == results[0] for r in results) is e["all_results_equal"]


async def run_cache_empty_query_key(i, e):
    c = _cache()
    k1 = c.make_key(i["query"], i["user_id"], i["role"], i["catalog_version"])
    k2 = c.make_key(i["query"], i["user_id"], i["role"], i["catalog_version"])
    assert isinstance(k1, str) is e["key_is_str"]
    assert (k1 == k2) is e["deterministic"]


async def run_vector_pgvector_unavailable(i, e):
    backend = DownBackend()
    store = VectorStoreService("t1_test", backend=backend)
    with pytest.raises(VectorStoreUnavailable):
        await store.search(i["query"], i["top_k"], i["search_as"])
    # the proxy never returns data, so no unfiltered fallback could have succeeded


async def run_vector_empty_store(i, e):
    store = await _store([])
    assert list(await store.search(i["query"], i["top_k"], i["search_as"])) == e["results"]


async def run_vector_top_k_boundary(i, e):
    docs = [{"text": f"doc {n}", "metadata": {"user_id": i["search_as"], "level": "table", "table": "t"}}
            for n in range(i["n_docs"])]
    store = await _store(docs)
    res = await store.search(i["query"], i["top_k"], i["search_as"])
    assert len(res) <= e["max_results"]


async def run_vector_duplicate_upsert(i, e):
    store = await _store([i["doc"]] * i["times"])
    res = await store.search(i["query"], i["top_k"], i["search_as"])
    assert res
    for r in res:
        assert _meta(r)["user_id"] in e["allowed_user_ids"]


RUNNERS = {c["name"]: globals()["run_" + c["name"].replace("-", "_")] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_vector_cache_foundation(case):
    await RUNNERS[case["name"]](case["input"], case["expected"])
