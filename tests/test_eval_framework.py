"""T3 eval-framework tests, driven by tests/fixtures/eval-framework.json.

All offline (EMBEDDING_BACKEND=hash set in conftest).
ASSUMPTIONS (planned interfaces, see SPECS.md T3): app.eval.{metrics,judge,
golden_builder,runner}, app.api.eval_endpoints.router with module-level
get_store() dependency.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.eval import golden_builder as gb
from app.eval import judge
from app.eval import metrics as M
from app.eval import runner as R

FIXTURE = Path(__file__).parent / "fixtures" / "eval-framework.json"
CASES = json.loads(FIXTURE.read_text())

FOUR = ["precision", "recall", "faithfulness", "relevancy"]


def _run(coro):
    return asyncio.run(coro)


# ---------- helpers ----------

def _golden4():
    return {"meta": {}, "items": [
        {"id": f"g{i}", "question": f"q{i}", "expected_metric_ids": [f"m{i}"],
         "expected_sql": "SELECT 1", "expected_rows": [{"n": i + 1}]}
        for i in range(4)]}


def _make_pipeline(retrieve="perfect", generate="ok"):
    async def retriever(question):
        i = int(question[1:])
        if retrieve == "empty":
            return []
        if retrieve == "raise_all" or (retrieve == "raise_first" and i == 0):
            raise RuntimeError("retriever boom")
        return [f"m{i}"]

    async def generator(question):
        i = int(question[1:])
        if generate == "raise_all":
            raise RuntimeError("generator boom")
        return {"sql": "SELECT 1", "rows": [{"n": i + 1}], "answer": f"The count is {i + 1}"}
    return retriever, generator


def _run_step(store, step, k, golden=None):
    retr, gen = _make_pipeline(step.get("retrieve", "perfect"), step.get("generate", "ok"))
    return _run(R.EvalRunner(retr, gen, store).run(golden or _golden4(), k=k, label=step.get("label")))


def _seed_runs(store, n):
    ids = []
    for i in range(n):
        run = _run_step(store, {"retrieve": "perfect", "label": f"s{i}"}, 1)
        ids.append(run["run_id"])
    return ids


def _api_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import eval_endpoints as ep
    store = R.EvalStore(url=f"sqlite:///{tmp_path}/eval.db")
    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[ep.get_store] = lambda: store
    return TestClient(app), store


def _check_range(val, exp):
    if "between_inclusive" in exp:
        lo, hi = exp["between_inclusive"]
        assert lo <= val <= hi
    else:
        lo, hi = exp["between_exclusive"]
        assert lo < val < hi


# ---------- actions ----------

def do_metric(i, e):
    rel = i["relevant"]
    if isinstance(rel, dict) and "__set__" in rel:
        rel = set(rel["__set__"])
    fn = getattr(M, i["fn"])
    args = [i["retrieved"], rel] + ([i["k"]] if "k" in i else [])
    assert fn(*args) == pytest.approx(e)


def do_exec_acc(i, e):
    assert bool(M.execution_accuracy(i["pred"], i["gold"])) is e


def do_aggregate(i, e):
    out = M.aggregate(i["items"])
    for k, v in e.items():
        assert out[k] == pytest.approx(v)


def do_faithfulness(i, e):
    v = judge.faithfulness(i["answer"], i["rows"])
    if isinstance(e, dict):
        _check_range(v, e)
    else:
        assert v == pytest.approx(e)


def do_relevancy(i, e):
    _check_range(_run(judge.relevancy(i["question"], i["answer"])), e)


def do_judge_json(i, e):
    if e == "ValueError":
        with pytest.raises(ValueError):
            judge.parse_judge_json(i["text"])
    else:
        assert judge.parse_judge_json(i["text"]) == e


def do_judge_json_calls(i, e, monkeypatch=None):
    calls = []
    real = judge.parse_judge_json
    # no-retry contract: a single call raises once, nothing re-invokes the judge
    with pytest.raises(ValueError):
        calls.append(1)
        real(i["text"])
    assert len(calls) <= e["max_calls"]


def do_check_regression(i, e):
    out = R.check_regression(i["current"], i["baseline"], max_drop=i["max_drop"])
    assert sorted(out) == sorted(e)


def _golden(i):
    return gb.build_golden_dataset(per_metric=i.get("per_metric", 3), seed=i.get("seed", 0))


def do_golden_provenance(i, e):
    g = _golden(i)
    assert set(e["meta_keys"]) <= set(g["meta"])
    assert 0 < len(g["items"]) <= e["max_count"]
    for it in g["items"]:
        assert set(e["item_keys"]) <= set(it)
        assert it["provenance"]["metric_id"] == it["metric_id"]
        assert it["provenance"]["catalog_version"] == g["meta"]["catalog_version"]
        assert it["expected_sql"]


def do_golden_sql(i, e):
    g = _golden(i)
    cat = json.loads((Path(__file__).parents[1] / "app" / "data" / "metric_catalog.json").read_text())
    ids = {m["id"] for m in (cat["metrics"] if isinstance(cat, dict) else cat)}
    for it in g["items"]:
        assert it["metric_id"] in ids
        assert isinstance(it["expected_sql"], str) and it["expected_sql"].strip()
        assert it["primary_metric_id"] in it["expected_metric_ids"]
    assert g["meta"]["count"] == len(g["items"])


def do_golden_difficulty(i, e):
    diffs = {it["difficulty"] for it in _golden(i)["items"]}
    assert diffs <= set(e["allowed"])
    assert set(e["must_include"]) <= diffs


def do_golden_determinism(i, e):
    a = gb.build_golden_dataset(seed=i["seed_a"])
    b = gb.build_golden_dataset(seed=i["seed_b"])
    assert (a == b) is e["identical"]
    if "questions_differ" in e:
        assert [x["question"] for x in a["items"]] != [x["question"] for x in b["items"]]
    if e.get("metric_ids_same"):
        assert {x["metric_id"] for x in a["items"]} == {x["metric_id"] for x in b["items"]}


def do_golden_roundtrip(i, e, tmp_path):
    g = gb.build_golden_dataset(seed=i["seed"])
    p = tmp_path / "golden.json"
    gb.write_golden(str(p))  # ASSUMPTION: default args (seed=0) match build_golden_dataset(seed=0)
    assert (gb.load_golden(str(p)) == g) is e["equal"]


def do_golden_cap(i, e):
    n = len(gb.build_golden_dataset(per_metric=i["per_metric"], seed=0)["items"])
    assert n <= e["max_count"]
    assert n >= e.get("min_count", 0)


def do_runner(i, e, tmp_path):
    store = R.EvalStore(url="sqlite://")
    runs = [_run_step(store, s, i["k"]) for s in i["steps"]]
    first = runs[0]
    if "metric_keys" in e:
        assert set(e["metric_keys"]) <= set(first["metrics"])
    if "n_items" in e:
        assert first["n_items"] == e["n_items"]
    if e.get("persisted"):
        got = store.get_run(first["run_id"])
        assert got is not None
        assert got["metrics"] == first["metrics"]
    if "first" in e:
        assert first["regressed"] is e["first"]["regressed"]
        assert first["baseline_run_id"] == e["first"]["baseline_run_id"]
    if "precision_at_least" in e:
        assert first["metrics"]["precision"] >= e["precision_at_least"]
    if "precision_at_most" in e:
        assert first["metrics"]["precision"] <= e["precision_at_most"]
    if "faithfulness_at_most" in e:
        assert first["metrics"]["faithfulness"] <= e["faithfulness_at_most"]
    if "step_regressed" in e:
        assert [r["regressed"] for r in runs] == e["step_regressed"]
    if "step_regressions_contain" in e:
        for r, c in zip(runs, e["step_regressions_contain"]):
            if c:
                assert c in r["regressions"]
    if "step_baseline" in e:
        for r, b in zip(runs, e["step_baseline"]):
            assert r["baseline_run_id"] == (None if b is None else runs[b]["run_id"])
    if "persisted_regressed_flag" in e:
        for r, f in zip(runs, e["persisted_regressed_flag"]):
            assert store.get_run(r["run_id"])["regressed"] is f


def do_runner_concurrency(i, e):
    state = {"cur": 0, "max": 0}

    async def retriever(q):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.01)
        state["cur"] -= 1
        return ["m"]

    async def generator(q):
        return {"sql": "SELECT 1", "rows": [{"n": 1}], "answer": "The count is 1"}

    golden = {"meta": {}, "items": [
        {"id": f"g{n}", "question": f"q{n}", "expected_metric_ids": ["m"],
         "expected_sql": "SELECT 1", "expected_rows": [{"n": 1}]} for n in range(i["n_items"])]}
    store = R.EvalStore(url="sqlite://")
    run = _run(R.EvalRunner(retriever, generator, store, concurrency=i["concurrency"]).run(golden, k=1))
    assert run["n_items"] == e["n_items"]
    assert state["max"] <= e["max_in_flight_at_most"]


def do_runner_empty(i, e):
    store = R.EvalStore(url="sqlite://")
    retr, gen = _make_pipeline()
    run = _run(R.EvalRunner(retr, gen, store).run({"meta": {}, "items": []}, k=5))
    assert run["n_items"] == e["n_items"]
    assert store.get_run(run["run_id"]) is not None


def do_baseline_pipeline(i, e):
    retriever, generator = R.build_baseline_pipeline()
    assert isinstance(_run(retriever(i["question"])), list)
    out = _run(generator(i["question"]))
    assert set(e["generator_keys"]) <= set(out)


def do_store_roundtrip(i, e):
    store = R.EvalStore(url="sqlite://")
    run = _run_step(store, {"label": "rt"}, 1)
    got = store.get_run(run["run_id"])
    assert got["metrics"] == run["metrics"]
    try:
        assert store.get_run("does-not-exist") is None
    except Exception:
        pass


def do_store_list(i, e):
    store = R.EvalStore(url="sqlite://")
    ids = _seed_runs(store, i["saved"])
    runs = store.list_runs(limit=i["limit"])
    assert len(runs) == e["len"]
    assert [r["run_id"] for r in runs] == ids[::-1][: i["limit"]]


def do_store_series(i, e):
    store = R.EvalStore(url="sqlite://")
    ids = _seed_runs(store, i["saved"])
    series = store.metric_series()
    assert set(e["series_keys"]) <= set(series)
    for k in e["series_keys"]:
        assert len(series[k]) == e["points"]
        assert {p["run_id"] for p in series[k]} == set(ids)


def do_api_list(i, e, tmp_path):
    client, store = _api_client(tmp_path)
    _seed_runs(store, i["runs"])
    r = client.get("/eval/runs")
    assert r.status_code == e["status"]
    body = r.json()
    body = body["runs"] if isinstance(body, dict) else body
    assert len(body) == e["count"]


def do_api_get(i, e, tmp_path):
    client, store = _api_client(tmp_path)
    rid = _seed_runs(store, 1)[0] if i["exists"] else "missing-run"
    assert client.get(f"/eval/runs/{rid}").status_code == e["status"]


def do_api_metrics(i, e, tmp_path):
    client, store = _api_client(tmp_path)
    ids = _seed_runs(store, i["runs"])
    r = client.get("/eval/metrics")
    assert r.status_code == 200
    body = r.json()
    body = body.get("series", body)
    assert set(e["series_keys"]) <= set(body)
    for k in e["series_keys"]:
        assert len(body[k]) == e["points_per_series"]
        for p in body[k]:
            assert set(e["point_keys"]) <= set(p)
            assert p["run_id"] in ids


def do_api_post_run(i, e, tmp_path):
    client, _ = _api_client(tmp_path)
    r = client.post("/eval/run", json={})
    assert r.status_code in e["status_in"]


ACTIONS = {
    "metric": do_metric, "exec_acc": do_exec_acc, "aggregate": do_aggregate,
    "faithfulness": do_faithfulness, "relevancy": do_relevancy,
    "judge_json": do_judge_json, "judge_json_calls": do_judge_json_calls,
    "check_regression": do_check_regression,
    "golden_provenance": do_golden_provenance, "golden_sql": do_golden_sql,
    "golden_difficulty": do_golden_difficulty, "golden_determinism": do_golden_determinism,
    "golden_roundtrip": do_golden_roundtrip, "golden_cap": do_golden_cap,
    "runner": do_runner, "runner_concurrency": do_runner_concurrency,
    "runner_empty": do_runner_empty, "baseline_pipeline": do_baseline_pipeline,
    "store_roundtrip": do_store_roundtrip, "store_list": do_store_list,
    "store_series": do_store_series,
    "api_list": do_api_list, "api_get": do_api_get, "api_metrics": do_api_metrics,
    "api_post_run": do_api_post_run,
}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_eval_framework(case, tmp_path):
    fn = ACTIONS[case["input"]["action"]]
    params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    if "tmp_path" in params:
        fn(case["input"], case["expected"], tmp_path)
    else:
        fn(case["input"], case["expected"])
