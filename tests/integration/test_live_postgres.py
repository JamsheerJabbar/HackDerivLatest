"""Live PostgreSQL + pgvector tests. Skipped unless NL2SQL_TEST_PG is set, e.g.

    NL2SQL_TEST_PG=postgresql+psycopg2://postgres@localhost:5433 pytest tests/integration

Each test class gets its own throwaway database, dropped afterwards.
"""
import os
import random
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, inspect, text

BASE = os.environ.get("NL2SQL_TEST_PG")
pytestmark = pytest.mark.skipif(not BASE, reason="NL2SQL_TEST_PG not set (live PostgreSQL + pgvector required)")


def _admin():
    return create_engine(f"{BASE}/postgres", isolation_level="AUTOCOMMIT")


@pytest.fixture()
def pgdb():
    name = f"t_{uuid.uuid4().hex[:10]}"
    with _admin().connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))
    url = f"{BASE}/{name}"
    yield url
    with _admin().connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def _meta(user, **kw):
    return {"user_id": user, "session_id": None, "level": "table", "table": "t", **kw}


# ─── pgvector ────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pgvector_upsert_search_isolation(pgdb):
    from app.services.vector_store import PgVectorBackend, VectorStoreService
    store = VectorStoreService("live", backend=PgVectorBackend(pgdb))
    await store.add_documents(["flagged transactions", "global catalog doc", "secret doc of B"],
                              [_meta("A"), _meta(None), _meta("B")])
    await store.add_documents(["flagged transactions"], [_meta("A")])          # idempotent upsert
    a = await store.search("flagged transactions", 10, "A")
    assert sorted(r["text"] for r in a) == ["flagged transactions", "global catalog doc"]
    assert all(r["metadata"]["user_id"] in ("A", None) for r in a)
    assert [r["text"] for r in await store.search("secret", 10, None)] == ["global catalog doc"]
    assert "secret doc of B" in [r["text"] for r in await store.search("secret doc", 10, "B")]
    with create_engine(pgdb).connect() as c:
        assert c.execute(text("select count(*) from vector_documents")).scalar() == 3


@pytest.mark.asyncio
async def test_pgvector_hnsw_index_and_extension(pgdb):
    from app.services.vector_store import PgVectorBackend, VectorStoreService
    backend = PgVectorBackend(pgdb)
    await VectorStoreService("live", backend=backend).add_documents(["x"], [_meta(None)])
    with create_engine(pgdb).connect() as c:
        idx = c.execute(text("select indexdef from pg_indexes where indexname='idx_vecdoc_embedding'")).scalar()
        assert "hnsw" in idx and "vector_cosine_ops" in idx
        assert c.execute(text("select extversion from pg_extension where extname='vector'")).scalar()
    assert backend._iterative in (True, False)


@pytest.mark.asyncio
async def test_pgvector_ranking_matches_in_memory_backend(pgdb):
    from app.services.vector_store import InMemoryBackend, PgVectorBackend, VectorStoreService
    rng = random.Random(3)
    words = [f"w{i}" for i in range(60)]
    docs = [" ".join(rng.sample(words, 5)) for _ in range(80)]
    pg = VectorStoreService("c", backend=PgVectorBackend(pgdb))
    mem = VectorStoreService("c", backend=InMemoryBackend())
    metas = [_meta("A")] * len(docs)
    await pg.add_documents(docs, metas)
    await mem.add_documents(docs, metas)
    for q in [" ".join(rng.sample(words, 5)) for _ in range(10)]:
        ra, rb = await pg.search(q, 3, "A"), await mem.search(q, 3, "A")
        # the best score must agree (texts can differ only when several documents tie on score exactly)
        assert abs(ra[0]["score"] - rb[0]["score"]) < 1e-5
        assert len({r["text"] for r in ra} & {r["text"] for r in rb}) >= 2   # HNSW is approximate; overlap stays high


@pytest.mark.asyncio
async def test_selective_user_still_gets_top_k_among_many_rows(pgdb):
    """A user with few rows among thousands must still get a full top-k (iterative HNSW scan / planner)."""
    from app.services.vector_store import PgVectorBackend, VectorStoreService
    rng = random.Random(1)
    words = [f"w{i}" for i in range(400)]
    store = VectorStoreService("many", backend=PgVectorBackend(pgdb))
    big = [" ".join(rng.sample(words, 6)) for _ in range(6000)]
    await store.add_documents(big, [_meta("X")] * len(big))
    mine = [" ".join(rng.sample(words, 6)) for _ in range(8)]
    await store.add_documents(mine, [_meta("Y")] * len(mine))
    with create_engine(pgdb).begin() as c:
        c.execute(text("ANALYZE vector_documents"))
    short = 0
    for q in [" ".join(rng.sample(words, 6)) for _ in range(25)]:
        got = await store.search(q, 5, "Y")
        assert all(r["metadata"]["user_id"] == "Y" for r in got)       # never another user's rows
        short += len(got) < 5
    assert short == 0


# ─── migration of a legacy-shaped database ───────────────────────────────────────────
def test_legacy_sqlite_migrates_to_postgres_mirror(pgdb, tmp_path):
    from app.core import schema_mapping as sm
    from app.files import migrate_to_postgres as mig
    from tests.legacy_db import make_legacy_db
    src = create_engine(make_legacy_db(tmp_path / "legacy.db"))
    tgt = create_engine(pgdb)
    assert mig.main(["--source", str(src.url), "--target", pgdb]) == 0

    insp = inspect(tgt)
    assert set(insp.get_table_names()) == {"users", "transactions", "login_events"}      # nothing invented
    assert "username" in {c["name"] for c in insp.get_columns("users")}
    assert "email" not in {c["name"] for c in insp.get_columns("users")}
    types = {c["name"]: str(c["type"]) for c in insp.get_columns("transactions")}
    assert types["created_at"].startswith("TIMESTAMP")                                    # text -> real timestamps
    with tgt.connect() as c:
        got = [r[0] for r in c.execute(text("select created_at from transactions order by txn_id"))]
        assert got[0] == datetime(2024, 3, 31, 6, 7) and got[3] == datetime(2024, 1, 2, 10, 0)
        assert c.execute(text("select count(*) from users where is_pep = 1")).scalar() == 2
    assert {"idx_txn_status", "idx_txn_created_at", "idx_users_risk_level"} <= {i["name"] for i in insp.get_indexes("transactions") + insp.get_indexes("users")}
    assert mig.main(["--source", str(src.url), "--target", pgdb]) == 0                  # idempotent re-run
    with tgt.connect() as c:
        assert c.execute(text("select count(*) from transactions")).scalar() == 4


def test_canonical_target_refuses_to_invent_missing_columns(pgdb, tmp_path):
    from app.files import migrate_to_postgres as mig
    from tests.legacy_db import make_legacy_db
    url = make_legacy_db(tmp_path / "legacy.db")
    assert mig.main(["--source", url, "--target", pgdb, "--target-schema", "canonical"]) == 3
    assert not inspect(create_engine(pgdb)).get_table_names()                              # nothing created


def test_catalog_metrics_agree_between_sqlite_and_postgres(pgdb, tmp_path):
    from app.core import schema_mapping as sm
    from app.files import migrate_to_postgres as mig
    from app.modules.catalog import templates as tpl
    from app.modules.catalog.metric_generator import generate_catalog
    from tests.legacy_db import make_legacy_db
    url = make_legacy_db(tmp_path / "legacy.db")
    assert mig.main(["--source", url, "--target", pgdb]) == 0
    lite = create_engine(url)
    event.listen(lite, "connect", sm.register_sqlite_functions)
    pg = create_engine(pgdb)
    ml, mp = sm.mapping_for_engine(lite), sm.mapping_for_engine(pg)
    assert ml.profile_name == mp.profile_name == "legacy_live"
    catalog = generate_catalog()
    assert ml.available_ids(catalog) == mp.available_ids(catalog)
    now = datetime(2026, 11, 18, 12, 0, 0)

    def norm(v):
        return round(float(v), 4) if hasattr(v, "quantize") or isinstance(v, float) else (v.isoformat()[:10] if hasattr(v, "isoformat") else v)

    def run(eng, m, sql, bind):
        rw = m.rewrite_sql(sql)
        with eng.connect() as c:
            return sorted((tuple(norm(x) for x in r) for r in c.execute(text(rw.sql), rw.apply_bind(bind)).fetchall()), key=repr)

    checked = 0
    for metric in catalog["metrics"]:
        if metric["id"] not in ml.available_ids(catalog):
            continue
        sql, bind = tpl.render_template(metric, {"days": 3650} if any(p["name"] == "days" for p in metric["parameters"]) else {}, now=now)
        if "LIMIT" in sql.upper():
            bind = {**bind, **({"limit": 1000} if "limit" in bind else {})}   # avoid ORDER BY ... LIMIT tie ambiguity
        assert run(lite, ml, sql, bind) == run(pg, mp, sql, bind), metric["id"]
        checked += 1
    assert checked >= 40
