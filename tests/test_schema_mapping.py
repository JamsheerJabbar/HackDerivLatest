"""Column-mapping layer: logical (canonical) catalog SQL -> physical schema (legacy live shape or canonical)."""
from datetime import datetime

import pytest
import sqlglot
from sqlalchemy import create_engine, event, text

from app.core import schema_mapping as sm
from app.modules.catalog import schema_profiler as sp
from app.modules.catalog import templates as tpl
from app.modules.catalog.metric_generator import generate_catalog
from tests.legacy_db import make_legacy_db

NOW = datetime(2026, 11, 18, 12, 0, 0)


@pytest.fixture(scope="module")
def catalog():
    return generate_catalog()


@pytest.fixture()
def legacy_engine(tmp_path):
    eng = create_engine(make_legacy_db(tmp_path / "legacy.db"))
    event.listen(eng, "connect", sm.register_sqlite_functions)
    return eng


@pytest.fixture()
def canonical_engine(tmp_path):
    import sqlite3
    path = tmp_path / "canonical.db"
    dest = sqlite3.connect(path)
    sp.build_sandbox().backup(dest)
    dest.close()
    eng = create_engine(f"sqlite:///{path}")
    event.listen(eng, "connect", sm.register_sqlite_functions)
    return eng


# ── timestamp parsing ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("2023-01-15T09:30:00Z", "2023-01-15 09:30:00"),
    ("2024-04-18 21:53:35", "2024-04-18 21:53:35"),
    ("3/31/24 6:07", "2024-03-31 06:07:00"),
    ("12/1/25 9:05", "2025-12-01 09:05:00"),
    ("1/2/2024 10:00", "2024-01-02 10:00:00"),
    ("2024-02-29", "2024-02-29 00:00:00"),
    ("2024-04-18T21:53:35.123+02:00", "2024-04-18 21:53:35"),
    ("", None), (None, None), ("not a date", None), ("13/40/24 1:00", None), ("2023-02-30 10:00:00", None),
])
def test_norm_ts(raw, expected):
    assert sm.norm_ts(raw) == expected


def test_parse_timestamp_passthrough_datetime():
    d = datetime(2024, 1, 1, 5, 6, 7)
    assert sm.parse_timestamp(d) is d


# ── profile detection and resolution ────────────────────────────────────────────────
def test_detects_legacy_and_canonical(legacy_engine, canonical_engine):
    assert sm.mapping_for_engine(legacy_engine).profile_name == "legacy_live"
    assert sm.mapping_for_engine(canonical_engine).profile_name == "canonical"


def test_resolve_renames_wraps_and_rejects(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    assert m.resolve("users", "full_name").physical == "username"
    assert m.resolve("users", "is_pep").wrapper == "({c} <> 0)"          # INTEGER flag -> boolean
    assert m.resolve("transactions", "created_at").wrapper == "norm_ts({c})"
    assert m.resolve("transactions", "status").is_identity
    for table, col in [("users", "email"), ("users", "country"), ("alerts", "status"), ("v_open_alerts", "alert_id")]:
        with pytest.raises(sm.UnavailableColumn):
            m.resolve(table, col)


def test_canonical_is_identity_and_keeps_indexable_columns(canonical_engine, catalog):
    m = sm.mapping_for_engine(canonical_engine)
    assert m.resolve("transactions", "created_at").is_identity   # no function wrapper: indexes stay usable
    assert m.resolve("users", "is_pep").is_identity
    report = m.available_metrics(catalog)
    assert all(v["available"] for v in report.values()), [k for k, v in report.items() if not v["available"]]
    for metric in catalog["metrics"]:
        assert m.rewrite_sql(metric["sql"]).sql == sqlglot.parse_one(metric["sql"]).sql()


def test_profile_override_marks_column_unavailable(legacy_engine):
    profiles = sm.load_profiles()
    profiles["legacy_live"]["columns"]["transactions.status"] = {"unavailable": True}
    m = sm.SchemaMapping(sm.PhysicalSchema.introspect(legacy_engine), profiles=profiles)
    with pytest.raises(sm.UnavailableColumn):
        m.rewrite_sql("SELECT COUNT(*) FROM transactions WHERE status = 'FLAGGED'")


# ── rewriting ───────────────────────────────────────────────────────────────────────
def test_rewrite_renamed_column_keeps_logical_output_name(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    assert m.rewrite_sql("SELECT users.full_name FROM users").sql == "SELECT users.username AS full_name FROM users"
    assert m.rewrite_sql("SELECT full_name FROM users ORDER BY full_name").sql == \
        "SELECT username AS full_name FROM users ORDER BY username"
    out = m.rewrite_sql("SELECT u.full_name AS who FROM transactions t JOIN users u ON t.user_id = u.user_id").sql
    assert "u.username AS who" in out and "t.user_id = u.user_id" in out


def test_rewrite_boolean_and_timestamp(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    out = m.rewrite_sql("SELECT COUNT(*) FROM users WHERE is_pep = TRUE").sql
    assert "(is_pep <> 0) = TRUE" in out
    out = m.rewrite_sql("SELECT COUNT(*) FROM transactions WHERE created_at >= :since").sql
    assert "NORM_TS(created_at) >= :since" in out


def test_rewrite_translates_enum_values_in_literals_and_binds(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    assert "'Desktop'" in m.rewrite_sql("SELECT COUNT(*) FROM login_events WHERE device_type = 'DESKTOP'").sql
    assert "'Mobile'" in m.rewrite_sql("SELECT COUNT(*) FROM login_events WHERE device_type IN ('MOBILE', 'x')").sql
    rw = m.rewrite_sql("SELECT COUNT(*) FROM login_events WHERE device_type = :value")
    assert rw.apply_bind({"value": "TABLET"}) == {"value": "Tablet"}
    assert rw.apply_bind({"value": "other"}) == {"value": "other"}       # unknown values pass through
    assert rw.apply_bind({"value": 5}) == {"value": 5}                    # non-strings untouched


def test_rewrite_unavailable_raises_with_reason(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    with pytest.raises(sm.UnavailableColumn, match="alerts"):
        m.rewrite_sql("SELECT COUNT(*) FROM alerts")
    with pytest.raises(sm.UnavailableColumn, match="email"):
        m.rewrite_sql("SELECT users.email FROM users")


def test_select_alias_references_are_left_alone(legacy_engine):
    m = sm.mapping_for_engine(legacy_engine)
    out = m.rewrite_sql("SELECT status, COUNT(*) AS n FROM transactions GROUP BY status ORDER BY n DESC").sql
    assert "ORDER BY n DESC" in out


# ── availability + real execution ───────────────────────────────────────────────────
def test_availability_report_on_legacy(legacy_engine, catalog):
    m = sm.mapping_for_engine(legacy_engine)
    report = m.available_metrics(catalog)
    assert report["flagged_txn_count"]["available"] and report["pep_user_count"]["available"]
    assert not report["flagged_txn_by_country"]["available"] and "country" in report["flagged_txn_by_country"]["reason"]
    assert not report["open_alerts_by_severity"]["available"]
    assert all(v["reason"] for v in report.values() if not v["available"])
    assert 0 < len(m.available_ids(catalog)) < len(catalog["metrics"])


def test_every_available_metric_executes_on_legacy(legacy_engine, catalog):
    m = sm.mapping_for_engine(legacy_engine)
    by_id = {x["id"]: x for x in catalog["metrics"]}
    with legacy_engine.connect() as c:
        for mid in m.available_ids(catalog):
            sql, bind = tpl.render_template(by_id[mid], {}, now=NOW)
            rw = m.rewrite_sql(sql)
            c.execute(text(rw.sql), rw.apply_bind(bind)).fetchall()   # raises on any bad rewrite


def test_results_are_correct_on_legacy_values(legacy_engine, catalog):
    m = sm.mapping_for_engine(legacy_engine)
    by_id = {x["id"]: x for x in catalog["metrics"]}

    def run(mid, params=None):
        sql, bind = tpl.render_template(by_id[mid], params or {}, now=NOW)
        rw = m.rewrite_sql(sql)
        with legacy_engine.connect() as c:
            return [dict(r) for r in c.execute(text(rw.sql), rw.apply_bind(bind)).mappings()]

    assert run("flagged_txn_count", {"days": 3650})[0]["flagged_count"] == 2            # US-format timestamps
    # NOW is 2026-11-18: a 365-day window starts 2025-11-18, so only the 12/1/25 flagged txn qualifies
    assert run("flagged_txn_count", {"days": 365})[0]["flagged_count"] == 1
    assert run("pep_user_count")[0]["pep_users"] == 2                                   # INTEGER flag -> boolean
    assert run("login_events_device_type_count", {"value": "DESKTOP", "days": 3650})[0]["row_count"] == 2
    trend = run("daily_txn_trend", {"days": 3650})
    assert sum(r["txn_count"] for r in trend) == 4
    window = run("failed_login_count", {"days": 400})[0]["failed_logins"]                # only the 2025-11-01 failure
    assert window == 1


def test_database_service_execute_logical_and_availability(tmp_path, monkeypatch, catalog):
    from app.core.config import settings
    url = make_legacy_db(tmp_path / "svc.db")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    from app.services.database import DatabaseService
    db = DatabaseService()
    assert db.mapping.profile_name == "legacy_live"
    sql, bind = tpl.render_template({m["id"]: m for m in catalog["metrics"]}["flagged_txn_count"], {"days": 3650}, now=NOW)
    assert db.execute_logical(sql, bind)[0]["flagged_count"] == 2
    ids = db.available_metric_ids(catalog)
    assert "flagged_txn_count" in ids and "open_alerts_by_severity" not in ids
    with pytest.raises(sm.UnavailableColumn):
        db.execute_logical("SELECT users.email FROM users", {})
    with pytest.raises(ValueError):
        db.execute_logical("DELETE FROM users", {})   # still SELECT-only after rewriting


@pytest.mark.asyncio
async def test_resolver_only_offers_metrics_the_database_can_run(legacy_engine, catalog):
    from app.modules.param_resolver import ParamResolver
    from app.modules.preprocessing.service import build_advanced_service
    mapping = sm.mapping_for_engine(legacy_engine)
    svc = build_advanced_service(available_fn=mapping.available_ids)
    unavailable = {k for k, v in mapping.available_metrics(catalog).items() if not v["available"]}
    resolver = ParamResolver(svc, threshold=0.0)
    for q in ["show open alerts by severity", "flagged transactions by country", "daily transactions last 30 days",
              "export actions audit log", "pep users"]:
        res = await svc.retrieve(q, k=5)
        assert not set(res["metric_ids"]) & unavailable, (q, res["metric_ids"])
        out = await resolver.resolve(q)
        assert out["metric_id"] is None or out["metric_id"] not in unavailable


@pytest.mark.asyncio
async def test_resolver_reports_missing_data_instead_of_answering_a_weaker_question(legacy_engine, catalog):
    from app.modules.param_resolver import ParamResolver
    from app.modules.preprocessing.service import build_advanced_service
    mapping = sm.mapping_for_engine(legacy_engine)
    svc = build_advanced_service(available_fn=mapping.available_ids, reasons_fn=mapping.unavailable_reasons)
    resolver = ParamResolver(svc)
    out = await resolver.resolve("show open alerts by severity")
    assert out["status"] == "no_match" and out["sql"] is None
    assert out["unavailable"]["metric_id"] == "open_alerts_by_severity" and "alerts" in out["unavailable"]["reason"]
    out = await resolver.resolve("flagged transactions by country")
    assert out["status"] == "no_match" and out["unavailable"]["metric_id"] == "flagged_txn_by_country"
    assert "country" in out["unavailable"]["reason"]
    ok = await resolver.resolve("pep users")                                  # fully answerable: no false alarm
    assert ok["status"] == "matched" and ok["metric_id"] == "pep_user_count" and "unavailable" not in ok
    assert (await resolver.resolve("what is the weather in paris")).get("unavailable") is None


@pytest.mark.asyncio
async def test_no_match_message_names_the_missing_data(legacy_engine, catalog):
    from app.api.endpoints import QueryDeps, answer_query
    from app.models.state import QueryRequest
    from app.modules.param_resolver import ParamResolver
    from app.modules.preprocessing.service import build_advanced_service
    from app.services.cache import CacheService
    from fakeredis import aioredis as fake_aioredis
    mapping = sm.mapping_for_engine(legacy_engine)
    resolver = ParamResolver(build_advanced_service(available_fn=mapping.available_ids,
                                                    reasons_fn=mapping.unavailable_reasons))
    deps = QueryDeps(resolver=resolver, execute=lambda s, b: [], generate=None,
                     cache=CacheService(redis_client=fake_aioredis.FakeRedis()))
    resp = await answer_query(QueryRequest(query="flagged transactions by country"),
                              {"user_id": "u", "role": "user"}, deps)
    assert resp.status == "no_match" and "users.country" in resp.error
    assert resp.metadata["unavailable_metric"] == "flagged_txn_by_country" and resp.metadata["can_regenerate"] is True
