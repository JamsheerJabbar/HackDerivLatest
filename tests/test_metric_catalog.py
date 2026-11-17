"""T2 metric-catalog tests, driven by tests/fixtures/metric-catalog.json.

Static schema profile + in-memory sandbox only; no live DB, no real embeddings
(EMBEDDING_BACKEND=hash is set in conftest).
ASSUMPTIONS (planned interfaces): the sandbox connection is sqlite3 or SQLAlchemy;
templates.render_template(metric, params) -> (sql, binds); table weights live in
app/data/table_weights.json; app.services.vector_store exports InMemoryBackend and
VectorStoreService(name, backend=...) with async add_documents/search.
"""
import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.modules.catalog import metric_generator as mg
from app.modules.catalog import schema_profiler as sp
from app.modules.catalog.templates import render_template
from app.services import vector_store as vs

FIXTURE = Path(__file__).parent / "fixtures" / "metric-catalog.json"
CASES = json.loads(FIXTURE.read_text())

TEMPLATE_METRIC = {
    "id": "test_metric",
    "sql": "SELECT COUNT(*) AS n FROM transactions WHERE created_at >= :since",
    "parameters": [{"name": "days", "type": "int", "default": 7}],
}


def _execute(conn, sql, binds):
    if hasattr(conn, "exec_driver_sql"):
        from sqlalchemy import text
        return conn.execute(text(sql), binds).fetchall()
    return conn.execute(sql, binds).fetchall()


def _col(profile, dotted):
    t, c = dotted.split(".")
    return profile["tables"][t]["columns"][c]


def _all_binds(metric):
    sql, binds = render_template(metric, {})
    return sql, binds


def _meta(r):
    return r["metadata"] if isinstance(r, dict) else r.metadata


def _text(r):
    if isinstance(r, dict):
        return r.get("text", r.get("document", r.get("content")))
    return getattr(r, "text", getattr(r, "document", getattr(r, "content", None)))


def run_catalog_build(i, e):
    cat = mg.generate_catalog()
    for k in e["catalog_keys"]:
        assert k in cat
    assert len(cat["metrics"]) >= e["min_metrics"]
    for m in cat["metrics"]:
        for k in e["required_keys"]:
            assert k in m, f"{m.get('id')} missing {k}"
        assert m["tables"] and m["formula"] and m["interpretation"] and m["sql"]
        assert isinstance(m["indexed"], bool) and isinstance(m["fast"], bool)
        assert m["direction"] in e["directions"]
        assert len(m["example_queries"]) >= e["min_example_queries"]
        for j in m["joins"]:
            for k in ("left", "right", "on"):
                assert k in j


def run_fast_flag(i, e):
    conn = sp.build_sandbox()
    p1 = sp.explain_plan(conn, i["indexed_sql"], i["indexed_params"])
    p2 = sp.explain_plan(conn, i["unindexed_sql"], i["unindexed_params"])
    assert sp.plan_uses_index(p1) is e["indexed_uses_index"]
    assert sp.plan_uses_index(p2) is e["unindexed_uses_index"]
    cat = mg.generate_catalog()
    for m in cat["metrics"]:
        sql, binds = render_template(m, {})
        plan = sp.explain_plan(conn, sql, binds)
        assert m["fast"] is sp.plan_uses_index(plan), m["id"]
        if any("idx_txn_user_created" in line for line in plan):
            assert m["fast"] is True, m["id"]


def run_profile_presence(i, e):
    prof = sp.profile_schema()
    for t in e["tables"]:
        assert prof["tables"][t]["kind"] == "table"
    for v in e["views"]:
        assert prof["tables"][v]["kind"] == "view"


def run_profile_indexes(i, e):
    prof = sp.profile_schema()
    ix = e["index"]
    found = [x for x in prof["tables"][ix["table"]]["indexes"] if x["name"] == ix["name"]]
    assert found and list(found[0]["columns"]) == ix["columns"]
    for c in e["fast_filter_true"]:
        assert _col(prof, c)["fast_filter"] is True
    for c in e["indexed_true"]:
        assert _col(prof, c)["indexed"] is True
    for c in e["fast_filter_false"]:
        assert _col(prof, c)["fast_filter"] is False


def run_profile_fk(i, e):
    prof = sp.profile_schema()
    assert e["fk"] in prof["tables"][e["table"]]["fks"]


def run_profile_enum(i, e):
    prof = sp.profile_schema()
    vals = _col(prof, e["column"])["enum_values"]
    for v in e["contains"]:
        assert v in vals


def run_profile_no_index(i, e):
    prof = sp.profile_schema()
    assert prof["tables"][e["table"]]["indexes"] == e["indexes"]


def run_profile_nullable(i, e):
    prof = sp.profile_schema()
    for c in e["columns"]:
        assert _col(prof, c)["nullable"] is e["nullable"]


def run_profile_dashboards_once(i, e):
    prof = sp.profile_schema()
    assert prof["tables"][e["table"]]["kind"] == e["kind"]
    assert list(prof["tables"]).count(e["table"]) == 1


def run_ids_unique(i, e):
    ids = [m["id"] for m in mg.generate_catalog()["metrics"]]
    assert bool(ids) is e["non_empty"]
    assert (len(ids) == len(set(ids))) is e["unique"]


def run_refs_exist(i, e):
    prof = sp.profile_schema()
    cat = mg.generate_catalog(prof)
    for m in cat["metrics"]:
        for t in m["tables"]:
            assert t in prof["tables"], (m["id"], t)
        for c in m["columns"]:
            if "." in c:
                t, col = c.split(".", 1)
                assert col in prof["tables"][t]["columns"], (m["id"], c)
            else:
                assert any(c in prof["tables"][t]["columns"] for t in m["tables"]), (m["id"], c)


def run_sql_executes(i, e):
    conn = sp.build_sandbox()
    for m in mg.generate_catalog()["metrics"]:
        sql, binds = render_template(m, {})
        assert sql.lstrip().upper().startswith(("SELECT", "WITH"))
        assert re.search(r":[a-zA-Z_]", sql) or not m["parameters"]
        _execute(conn, sql, binds)  # must not raise


def run_no_ddl(i, e):
    bad = []
    for m in mg.generate_catalog()["metrics"]:
        words = set(re.findall(r"[A-Za-z_]+", m["sql"].upper()))
        bad += [(m["id"], w) for w in i["forbidden"] if w in words]
    assert bad == e["violations"]


def run_table_weights(i, e):
    w = mg.load_table_weights()
    for t in e["includes"]:
        assert t in w
    for t, v in w.items():
        assert e["min"] <= v <= e["max"], t
    raw = json.loads((Path(__file__).parent.parent / "app" / "data" / "table_weights.json").read_text())
    assert raw == w


def _docs():
    prof = sp.profile_schema()
    cat = mg.generate_catalog(prof)
    return prof, cat, *mg.build_vector_documents(cat, prof)


def run_vector_docs(i, e):
    _, _, texts, metas = _docs()
    assert len(texts) == len(metas) and texts
    assert {m["level"] for m in metas} >= set(e["levels"])
    for m in metas:
        for k in e["meta_keys"]:
            assert k in m
        assert m["user_id"] is e["user_id"]


def run_value_cap(i, e):
    prof = {"tables": {"zz_t": {"kind": "table", "indexes": [], "fks": [],
                                "columns": {"c": {"type": "TEXT", "nullable": True, "pk": False,
                                                  "indexed": False, "fast_filter": False,
                                                  "enum_values": [f"V{n}" for n in range(i["distinct"])]}}}}}
    cat = {"version": "1", "generated_at": "2026-01-01T00:00:00", "metrics": []}
    texts, metas = mg.build_vector_documents(cat, prof)
    n = sum(1 for m in metas if m["level"] == "value" and m["table"] == "zz_t")
    assert n <= e["max_value_docs_for_column"]
    if "exact" in e:
        assert n == e["exact"]


async def run_index_catalog(i, e):
    prof, cat, texts, metas = _docs()
    store = vs.VectorStoreService("t2_test", backend=vs.InMemoryBackend())
    count = await mg.index_catalog(store, cat, prof)
    assert (count == len(texts)) is e["count_equals_texts"]
    res = await store.search("transactions", 5, "any_user")
    assert bool(res) is e["stored_global_searchable"]
    for r in res:
        assert _meta(r)["user_id"] is None


def run_render(i, e):
    sql, binds = render_template(TEMPLATE_METRIC, i["params"])
    for k in e["binds_contain"]:
        assert k in binds
    if e["since_is_iso_str"]:
        from datetime import datetime
        assert isinstance(binds["since"], str)
        datetime.fromisoformat(binds["since"])
    assert sql.lstrip().upper().startswith("SELECT")


def run_render_error(i, e):
    with pytest.raises(ValueError):
        render_template(TEMPLATE_METRIC, i["params"])


def run_render_non_select(i, e):
    bad = {"id": "bad", "sql": i["sql"], "parameters": []}
    with pytest.raises(ValueError):
        render_template(bad, {})


def run_concurrent_generate(i, e):
    with ThreadPoolExecutor(i["workers"]) as ex:
        results = list(ex.map(lambda _: mg.generate_catalog(), range(i["workers"])))
    ids = [sorted(m["id"] for m in r["metrics"]) for r in results]
    assert all(x == ids[0] for x in ids) is e["same_ids"]


def run_write_catalog(i, e, tmp_path):
    out = tmp_path / "metric_catalog.json"
    mg.write_catalog(path=str(out))
    data = json.loads(out.read_text())
    expected = sorted(m["id"] for m in mg.generate_catalog()["metrics"])
    assert (sorted(m["id"] for m in data["metrics"]) == expected) is e["roundtrip_ids_equal"]


RUNNERS = {c["name"]: globals()["run_" + c["input"]["action"]] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_metric_catalog(case, tmp_path):
    fn = RUNNERS[case["name"]]
    if fn is run_write_catalog:
        out = fn(case["input"], case["expected"], tmp_path)
    else:
        out = fn(case["input"], case["expected"])
    if asyncio.iscoroutine(out):
        await out
