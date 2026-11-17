"""T4 retrieval-pipeline tests, driven by tests/fixtures/retrieval-pipeline.json.

All offline (EMBEDDING_BACKEND=hash set in conftest); the LLM is a fake object.
ASSUMPTIONS (planned interfaces, see SPECS.md T4):
- app.modules.retrieval.{intent_small,multi_query,hybrid,compression,rerank}
- app.modules.preprocessing.service.{AdvancedRetrievalService,build_advanced_service}
  (factory may be sync or async)
- compression "joins" entries are strings (or dicts) containing 'table.col = table.col'
- AnswerBank is inspected only through rerank() behaviour.
"""
import asyncio
import inspect
import json
import math
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.modules.catalog import schema_profiler as sp
from app.modules.retrieval import compression as comp
from app.modules.retrieval import hybrid as hy
from app.modules.retrieval import intent_small as it
from app.modules.retrieval import multi_query as mq
from app.modules.retrieval import rerank as rr

FIXTURE = Path(__file__).parent / "fixtures" / "retrieval-pipeline.json"
CASES = json.loads(FIXTURE.read_text())
CATALOG_PATH = Path(__file__).parents[1] / "app" / "data" / "metric_catalog.json"

DOCS = {
    "rules": {
        "r7": "Rule RULE-007 structuring: multiple deposits just under reporting threshold",
        "r70": "Rule RULE-070 velocity: many logins in short window",
        "m1": "average transaction amount per user",
        "m2": "count of structuring alerts by severity",
        "m3": "failed login attempts by country",
        "m4": "kyc status distribution of users",
    }
}

INTENTS = {"SELECT", "SCHEMA_QUERY", "OFF_TOPIC", "UNKNOWN"}


def _run(coro):
    return asyncio.run(coro)


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply, self.raises = reply, raises
        self.calls = []

    async def generate_response(self, prompt, model_name=None):
        self.calls.append({"prompt": prompt, "model_name": model_name})
        if self.raises:
            raise RuntimeError("llm boom")
        return self.reply


def _docs(spec):
    return DOCS[spec] if isinstance(spec, str) else spec


def _vector_fn(ranking):
    async def fn(q, *args, **kwargs):
        return list(ranking)
    return fn


def _pair(text):
    toks = re.findall(r"[A-Za-z_]+\.[A-Za-z_]+", str(text))
    return frozenset(toks[:2])


# ---------- intent ----------

def do_intent(i, e):
    llm = FakeLLM(i["reply"], i["raises"])
    clf = it.IntentClassifier(llm=llm, model=i["model"]) if i["model"] else it.IntentClassifier(llm=llm)
    out = _run(clf.classify(i["query"]))
    if e.get("valid_shape"):
        assert out["intent"] in INTENTS
        assert out["complexity"] in {"simple", "complex"}
        assert isinstance(out["confidence"], (int, float))
        assert out["source"] in {"llm", "heuristic"}
    assert len(llm.calls) == e.get("calls", len(llm.calls))
    assert out["latency_ms"] >= 0
    if "intent" in e:
        assert out["intent"] == e["intent"]
    if "complexity" in e:
        assert out["complexity"] == e["complexity"]
    if "source" in e:
        assert out["source"] == e["source"]
    if "model" in e:
        want = settings.INTENT_MODEL if e["model"] == "__INTENT_MODEL__" else e["model"]
        assert llm.calls[0]["model_name"] == want
        assert out["model"] == want
    if "confidence" in e:
        assert out["confidence"] == pytest.approx(e["confidence"])
    if "parse_error" in e:
        assert bool(out.get("parse_error", False)) is e["parse_error"]
    if e.get("heuristic_complexity_matches"):
        assert out["complexity"] == it.heuristic_complexity(i["query"])
        assert out["intent"] in INTENTS


def do_heuristic(i, e):
    assert it.heuristic_complexity(i["query"]) == e


# ---------- bm25 / rrf / hybrid ----------

def do_bm25(i, e):
    idx = hy.BM25Index(_docs(i["docs"]))
    res = idx.search(i["query"], i["k"])
    ids = [r[0] for r in res]
    scores = [r[1] for r in res]
    assert len(res) <= i["k"]
    if "len" in e:
        assert len(res) == e["len"]
    if "first" in e:
        assert ids[0] == e["first"]
    if "ids" in e:
        assert ids == e["ids"]
    if e.get("descending"):
        assert scores == sorted(scores, reverse=True)
    if e.get("no_zero"):
        assert all(s > 0 for s in scores)


def do_rrf(i, e):
    kw = {"top": i["top"]} if "top" in i else {}
    assert hy.rrf_fuse(i["rankings"], **kw) == e


def _check_ids(out, e, k):
    assert isinstance(out, list)
    if "ids" in e:
        assert out == e["ids"]
    if e.get("unique"):
        assert len(out) == len(set(out))
    if "max_len" in e:
        assert len(out) <= e["max_len"]
    if "top_contains" in e:
        assert e["top_contains"] in out[: e["top_n"]]
    if "top_not_contains" in e:
        assert e["top_not_contains"] not in out[:k]


def do_hybrid(i, e):
    h = hy.HybridRetriever(_vector_fn(i["vector_ranking"]), hy.BM25Index(_docs(i["docs"])))
    out = _run(h.search(i["query"], i["k"]))
    _check_ids(out, e, i["k"])
    if "bm25_top1" in e:
        assert hy.BM25Index(_docs(i["docs"])).search(i["query"], 3)[0][0] == e["bm25_top1"]
        assert i["vector_ranking"].index(e["bm25_top1"]) >= i["k"]  # vector alone would miss it


def do_hybrid_empty_bm25(i, e):
    h = hy.HybridRetriever(_vector_fn(i["vector_ranking"]), hy.BM25Index({}))
    _check_ids(_run(h.search(i["query"], i["k"])), e, i["k"])


def do_hybrid_novec_bm25(i, e):
    # control: with an empty BM25 side the identifier rule is not promoted
    h = hy.HybridRetriever(_vector_fn(i["vector_ranking"]), hy.BM25Index({}))
    _check_ids(_run(h.search(i["query"], i["k"])), e, i["k"])


# ---------- multi-query ----------

def do_decompose(i, e):
    out = mq.decompose(i["query"])
    assert isinstance(out, list) and out
    if e == "__non_empty_list__":
        return
    assert out == e


def do_decompose_many(i, e):
    q = "; ".join(f"metric number {n}" for n in range(i["n"]))
    out = mq.decompose(q)
    assert len(out) >= e["min_len"]
    assert len(out) == len(set(out))
    if e["all_two_words"]:
        assert all(len(p.split()) >= 2 for p in out)


def do_mq_search(i, e):
    seen = []

    async def search_fn(q):
        seen.append(q)
        if q in i["raise_for"]:
            raise RuntimeError("search boom")
        return list(i["rankings"].get(q, []))

    out = _run(mq.multi_query_search(search_fn, i["query"], i["sub_queries"], i["k"]))
    assert isinstance(out, list)
    assert len(out) <= i["k"]
    if "set" in e:
        assert set(out) == set(e["set"]) if i["k"] >= len(e["set"]) else True
    if "len" in e:
        assert len(out) == e["len"]
    if "first" in e:
        assert out[0] == e["first"]
    if e.get("unique"):
        assert len(out) == len(set(out))
    if e.get("a_before_c"):
        assert out.index("a") < out.index("c")
    if e.get("tops_in_first_n_subqueries"):
        n = len(i["sub_queries"])
        tops = {i["rankings"][q][0] for q in i["sub_queries"] if i["rankings"].get(q)}
        assert tops <= set(out[:n])
    if e.get("first_two_are_tops_of_first_two_subqueries"):
        tops = {i["rankings"][q][0] for q in i["sub_queries"][:2] if i["rankings"].get(q)}
        assert set(out[:2]) == tops
    if "calls_for" in e:
        # every sub-query is searched; the original query may also be searched
        assert set(e["calls_for"]) <= set(seen)


# ---------- compression ----------

_PROFILE = None


def _profile():
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = sp.profile_schema()
    return _PROFILE


def do_compress(i, e):
    out = comp.compress_join_context(i["query"], i["tables"], _profile(), token_budget=i["budget"])
    text = out["text"]
    for k in ("text", "tokens", "tables", "joins", "dropped_columns"):
        assert k in out
    if e.get("tokens_formula"):
        assert out["tokens"] == math.ceil(len(text.split()) * 1.3)
    if e.get("tokens_le_budget"):
        assert out["tokens"] <= i["budget"]
    if "tokens" in e:
        assert out["tokens"] == e["tokens"]
    for s in e.get("text_contains", []):
        assert s in text
    for s in e.get("text_not_contains", []):
        assert s not in text
    if e.get("text_empty"):
        assert text.strip() == ""
    if "tables_set" in e:
        assert set(out["tables"]) == set(e["tables_set"])
    if "joins_len" in e:
        assert len(out["joins"]) == e["joins_len"]
    if "joins" in e:
        got = {_pair(j) for j in out["joins"]}
        for a, b in e["joins"]:
            assert frozenset([a, b]) in got, (a, b, out["joins"])
        for a, b in e["joins"]:
            assert frozenset([a, b]) and any(a in str(j) and b in str(j) for j in out["joins"])
    if e.get("dropped_nonempty"):
        assert out["dropped_columns"]


def do_compress_budget_monotonic(i, e):
    small = comp.compress_join_context(i["query"], i["tables"], _profile(), token_budget=i["small"])
    big = comp.compress_join_context(i["query"], i["tables"], _profile(), token_budget=i["big"])
    assert small["tokens"] <= i["small"]
    assert (small["tokens"] <= big["tokens"]) is e["small_le_big"]


def do_compress_twice(i, e):
    a = comp.compress_join_context(i["query"], i["tables"], _profile(), token_budget=i["budget"])
    b = comp.compress_join_context(i["query"], i["tables"], _profile(), token_budget=i["budget"])
    assert (a == b) is e["identical"]


# ---------- rerank ----------

def do_boost(i, e):
    if i["boost"] is None:
        out = rr.feature_boost(i["score"], i["weight"])
    else:
        out = rr.feature_boost(i["score"], i["weight"], i["boost"])
    assert out == pytest.approx(e)


def _rerank(i):
    bank = rr.AnswerBank(i["answers"])
    kw = {}
    if "alpha" in i:
        kw["alpha"] = i["alpha"]
    if "boost" in i:
        kw["boost"] = i["boost"]
    return _run(rr.rerank(i["query"], [dict(c) for c in i["candidates"]], bank, i["weights"], i["k"], **kw)), kw


def do_rerank(i, e):
    out, kw = _rerank(i)
    assert isinstance(out, list)
    assert len(out) <= i["k"]
    assert [c["id"] for c in out] == e["order"]
    assert len(out) == e["len"]
    alpha = kw.get("alpha", 0.5)
    boost = kw.get("boost", 0.25)
    by_id = {c["id"]: c for c in i["candidates"]}
    for c in out:
        for k in ("id", "score", "table", "fast", "access", "answer_sim", "weight", "final_score"):
            assert k in c
        assert c["score"] == by_id[c["id"]]["score"]
        assert c["answer_sim"] <= 1.0 + 1e-6
        want_w = i["weights"].get(c["table"], 0.5)
        assert c["weight"] == pytest.approx(want_w)
        base = alpha * c["score"] + (1 - alpha) * c["answer_sim"]
        assert c["final_score"] == pytest.approx(rr.feature_boost(base, want_w, boost))
    finals = [c["final_score"] for c in out]
    assert finals == sorted(finals, reverse=True)
    for cid, w in e.get("weights", {}).items():
        assert next(c for c in out if c["id"] == cid)["weight"] == pytest.approx(w)
    for cid in e.get("answer_sim_zero", []):
        assert next(c for c in out if c["id"] == cid)["answer_sim"] == 0.0
    for cid in e.get("answer_sim_positive", []):
        assert next(c for c in out if c["id"] == cid)["answer_sim"] > 0.0
    if i.get("twice"):
        again, _ = _rerank(i)
        assert again == out


def do_bank_build(i, e):
    calls = []

    def answer_fn(metric):
        calls.append(metric["id"])
        return i["answers"][metric["id"]]

    bank = rr.AnswerBank.build({"metrics": i["metrics"]}, answer_fn)
    assert len(calls) == e["calls"]
    assert sorted(calls) == sorted(m["id"] for m in i["metrics"])
    cands = [{"id": m, "score": s, "table": "t1", "fast": True, "access": "global"}
             for m, s in i["scores"].items()]
    out = _run(rr.rerank(i["query"], cands, bank, {"t1": 1.0}, 5))
    assert [c["id"] for c in out] == e["order"]


# ---------- service ----------

_SERVICE = None


def _service():
    global _SERVICE
    if _SERVICE is None:
        from app.modules.preprocessing.service import build_advanced_service
        _SERVICE = _run(_maybe_await(build_advanced_service(seed=0)))
    return _SERVICE


def _catalog_ids():
    cat = json.loads(CATALOG_PATH.read_text())
    return {m["id"] for m in (cat["metrics"] if isinstance(cat, dict) else cat)}


def _query(q):
    if q.startswith("__REPEAT__"):
        return "failed login count by country " * 500
    return q


def do_service(i, e):
    q = _query(i["query"])
    out = _run(_service().retrieve(q, user_id=i["user_id"], k=i["k"]))
    for k in e.get("keys", ["intent", "sub_queries", "metric_ids", "candidates", "join_context"]):
        assert k in out
    assert isinstance(out["intent"], dict)
    ids = out["metric_ids"]
    assert len(ids) <= e["max_ids"]
    assert len(out["candidates"]) <= i["k"]
    if e.get("unique_ids"):
        assert len(ids) == len(set(ids))
    if e.get("ids_in_catalog"):
        assert set(ids) <= _catalog_ids()  # user_id None sees only global catalog docs
    if "min_sub_queries" in e:
        assert len(out["sub_queries"]) >= e["min_sub_queries"]
    if "sub_queries_equal" in e:
        assert out["sub_queries"] == e["sub_queries_equal"]
    if e.get("join_context_none"):
        assert out["join_context"] is None
    if e.get("join_context_invariant"):
        tables = {c["table"] for c in out["candidates"]}
        if len(tables) <= 1:
            assert out["join_context"] is None
        if out["join_context"] is not None:
            assert isinstance(out["join_context"], str) and out["join_context"]
            assert len(tables) > 1


def do_service_twice(i, e):
    a = _run(_service().retrieve(i["query"], user_id=None, k=i["k"]))
    b = _run(_service().retrieve(i["query"], user_id=None, k=i["k"]))
    assert (a["metric_ids"] == b["metric_ids"]) is e["identical_ids"]


def do_service_concurrent(i, e):
    async def go():
        svc = _service()
        return await asyncio.gather(*[svc.retrieve(i["query"], user_id=None, k=i["k"]) for _ in range(i["n"])])

    outs = _run(go())
    ids = [o["metric_ids"] for o in outs]
    assert all(x == ids[0] for x in ids) is e["identical_ids"]


def do_acceptance(i, e):
    from app.eval import golden_builder as gb
    from app.eval import metrics as M
    from app.eval import runner as R

    golden = gb.build_golden_dataset(per_metric=i["per_metric"], seed=i["seed"])
    items = golden["items"]
    assert items
    base_retriever, _ = R.build_baseline_pipeline()
    svc = _service()
    k = i["k"]

    async def go():
        base, adv = [], []
        for it_ in items:
            rel = set(it_["expected_metric_ids"])
            b = await base_retriever(it_["question"])
            a = (await svc.retrieve(it_["question"], user_id=None, k=k))["metric_ids"]
            base.append((M.recall_at_k(b, rel, k), M.mrr(b, rel)))
            adv.append((M.recall_at_k(a, rel, k), M.mrr(a, rel)))
        return base, adv

    base, adv = _run(go())
    n = len(items)
    b_rec, b_mrr = sum(x[0] for x in base) / n, sum(x[1] for x in base) / n
    a_rec, a_mrr = sum(x[0] for x in adv) / n, sum(x[1] for x in adv) / n
    if e["recall_ge_baseline"]:
        assert a_rec >= b_rec - 1e-9, (a_rec, b_rec)
    if e["mrr_ge_baseline"]:
        assert a_mrr >= b_mrr - 1e-9, (a_mrr, b_mrr)


RUNNERS = {
    "intent": do_intent, "heuristic": do_heuristic, "bm25": do_bm25, "rrf": do_rrf,
    "hybrid": do_hybrid, "hybrid_empty_bm25": do_hybrid_empty_bm25,
    "hybrid_novec_bm25": do_hybrid_novec_bm25, "decompose": do_decompose,
    "decompose_many": do_decompose_many, "mq_search": do_mq_search, "compress": do_compress,
    "compress_budget_monotonic": do_compress_budget_monotonic, "compress_twice": do_compress_twice,
    "boost": do_boost, "rerank": do_rerank, "bank_build": do_bank_build, "service": do_service,
    "service_twice": do_service_twice, "service_concurrent": do_service_concurrent,
    "acceptance": do_acceptance,
}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_retrieval_pipeline(case):
    RUNNERS[case["input"]["action"]](case["input"], case["expected"])
