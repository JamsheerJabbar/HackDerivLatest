"""Migrate a DerivInsight business database to PostgreSQL, whatever its physical shape.

Usage:
    python -m app.files.migrate_to_postgres --source sqlite:///./derivinsightnew.db \
        --target postgresql+psycopg2://localhost:5432/derivinsight [--target-schema auto|mirror|canonical]

Target schema modes
  canonical  tables from app/files/derivinsight_schema.sql + alerts_schema.sql (requires a canonical source)
  mirror     the source's own tables/columns, recreated 1:1 in PostgreSQL (nothing invented or dropped), plus
             the canonical schema's indexes where the table/columns exist. Text timestamps (ISO and US
             'M/D/YY H:MM') become real TIMESTAMPs; the runtime column mapping (app.core.schema_mapping)
             lets catalog queries run against either shape.
  auto       mirror when the source is not canonical, else canonical (default)

Idempotent (ON CONFLICT DO NOTHING). Rows are streamed in batches. The run is verified (row counts, numeric
sums, unparsable timestamps) and exits non-zero on any mismatch.
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.types import JSON, Boolean, Date, DateTime, Numeric

from app.core.schema_mapping import PhysicalSchema, SchemaMapping, detect_profile, load_profiles, parse_timestamp

SCHEMA_FILES = ["app/files/derivinsight_schema.sql", "app/files/alerts_schema.sql"]
# FK-safe copy order for the canonical schema
TABLE_ORDER = ["users", "alert_rules", "transactions", "login_events", "alerts", "audit_logs",
               "dashboards", "query_history", "metric_specs", "events", "alert_history",
               "anomaly_history", "metric_windows"]
BATCH = 1000


def translate_to_postgres(sql: str) -> List[str]:
    """Translate SQLite/MySQL flavoured DDL from the repo schema files into PostgreSQL statements."""
    sql = re.sub(r"--[^\n]*", "", sql)
    stmts = []
    for raw in sql.split(";"):
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"CREATE VIEW IF NOT EXISTS", "CREATE OR REPLACE VIEW", s, flags=re.I)
        s = re.sub(r"CREATE INDEX (?!IF NOT EXISTS)", "CREATE INDEX IF NOT EXISTS ", s, flags=re.I)
        s = re.sub(r"\bINT\s+PRIMARY KEY\s+AUTO_INCREMENT\b", "SERIAL PRIMARY KEY", s, flags=re.I)
        s = re.sub(r"\bTINYINT\(1\)", "SMALLINT", s, flags=re.I)
        s = re.sub(r"\s+ON UPDATE CURRENT_TIMESTAMP", "", s, flags=re.I)
        if re.match(r"INSERT IGNORE INTO", s, flags=re.I):
            s = re.sub(r"INSERT IGNORE INTO", "INSERT INTO", s, count=1, flags=re.I) + " ON CONFLICT DO NOTHING"
        elif re.match(r"INSERT INTO", s, flags=re.I) and "ON CONFLICT" not in s.upper():
            s += " ON CONFLICT DO NOTHING"
        stmts.append(s)
    return stmts


def create_schema(target_engine) -> int:
    """Create the canonical schema (tables, indexes, views, seed rows) on the target."""
    errors = 0
    for path in SCHEMA_FILES:
        with open(path, encoding="utf-8") as f:
            stmts = translate_to_postgres(f.read())
        for s in stmts:
            try:
                with target_engine.begin() as conn:
                    conn.execute(text(s))
            except Exception as e:
                if "already exists" in str(e).lower():
                    continue  # e.g. duplicate `dashboards` definition across the two schema files
                errors += 1
                print(f"[schema] FAILED: {s[:70]!r}... -> {str(e).splitlines()[0]}", file=sys.stderr)
    return errors


# ─── source analysis ─────────────────────────────────────────────────────────────────

def analyze_source(source_engine) -> Tuple[PhysicalSchema, str]:
    physical = PhysicalSchema.introspect(source_engine)
    return physical, detect_profile(physical, load_profiles())


def missing_required_columns(physical: PhysicalSchema, profile: str) -> List[str]:
    """NOT NULL canonical columns that the source cannot provide (blocks a canonical-target migration)."""
    from app.modules.catalog.schema_profiler import profile_schema
    mapping = SchemaMapping(physical, profile_schema()["tables"], load_profiles(), profile)
    missing = []
    for tname, t in mapping.logical.items():
        if t["kind"] != "table" or tname not in physical.tables:
            continue
        for cname, c in t["columns"].items():
            if c["nullable"] or c["pk"]:
                continue
            try:
                mapping.resolve(tname, cname)
            except Exception:
                missing.append(f"{tname}.{cname}")
    return missing


# ─── value conversion ────────────────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.unparsable: Dict[str, int] = {}

    def bad(self, key: str) -> None:
        self.unparsable[key] = self.unparsable.get(key, 0) + 1


def convert(table: str, col, value: Any, stats: Stats) -> Any:
    if value is None:
        return None
    t = col.type
    if isinstance(t, Boolean):
        return bool(value)
    if isinstance(t, (DateTime, Date)):
        if value == "":
            return None
        dt = parse_timestamp(value)
        if dt is None:
            stats.bad(f"{table}.{col.name}")
            return None
        return dt.date() if isinstance(t, Date) and not isinstance(t, DateTime) else dt
    if isinstance(t, JSON) and isinstance(value, str):
        try:
            return json.loads(value)  # SQLAlchemy JSON re-encodes once; avoid double-encoding
        except ValueError:
            return value  # legacy plain text: keep the raw value rather than abort the table
    return value


def _reset_sequence(engine, table) -> None:
    """Advance serial/identity sequences past copied explicit ids so later INSERTs do not collide."""
    for col in table.primary_key.columns:
        with engine.begin() as c:
            seq = c.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                            {"t": table.name, "c": col.name}).scalar()
            if seq:
                c.execute(text(f"SELECT setval(:s, COALESCE((SELECT MAX({col.name}) FROM {table.name}), 0) + 1, false)"),
                          {"s": seq})


def _copy_table(source_engine, target_engine, src_name: str, tgt) -> Tuple[int, int]:
    tgt_cols = {c.name: c for c in tgt.columns}
    stats = Stats()
    n = 0
    with source_engine.connect() as sc:
        res = sc.execution_options(stream_results=True).execute(text(f"SELECT * FROM {src_name}"))
        keys = list(res.keys())
        while True:
            chunk = res.fetchmany(BATCH)
            if not chunk:
                break
            rows = [{k: convert(src_name, tgt_cols[k], v, stats) for k, v in zip(keys, r) if k in tgt_cols}
                    for r in chunk]
            with target_engine.begin() as tc:
                tc.execute(pg_insert(tgt).on_conflict_do_nothing(), rows)
            n += len(rows)
    copy_table.unparsable.update(stats.unparsable)
    return n, 0


class _Report:
    unparsable: Dict[str, int] = {}


copy_table = _Report()


def copy_data(source_engine, target_engine, tables: Optional[List[str]] = None) -> Dict[str, Tuple[int, int]]:
    """Copy rows table by table (streamed). Returns {table: (source_rows, target_rows)}."""
    meta = MetaData()
    meta.reflect(bind=target_engine)
    src_tables = set(inspect(source_engine).get_table_names())
    names = tables or [t for t in TABLE_ORDER if t in src_tables] + sorted(t for t in src_tables if t not in TABLE_ORDER)
    copy_table.unparsable = {}
    counts: Dict[str, Tuple[int, int]] = {}
    for name in names:
        if name not in src_tables or name not in meta.tables or name.startswith("sqlite_"):
            continue
        _copy_table(source_engine, target_engine, name, meta.tables[name])
        with source_engine.connect() as sc, target_engine.connect() as tc:
            counts[name] = (sc.execute(text(f"SELECT count(*) FROM {name}")).scalar(),
                            tc.execute(text(f"SELECT count(*) FROM {name}")).scalar())
    for t in meta.tables.values():  # includes tables seeded by the schema files with explicit ids
        _reset_sequence(target_engine, t)
    return counts


# ─── mirror mode ─────────────────────────────────────────────────────────────────────

def create_mirror_schema(source_engine, target_engine) -> List[str]:
    """Recreate the source's tables 1:1 on the target, then add the canonical schema's indexes that fit."""
    meta = MetaData()
    meta.reflect(bind=source_engine, views=False)
    for name in [n for n in meta.tables if n.startswith("sqlite_")]:
        meta.remove(meta.tables[name])
    meta.create_all(target_engine)
    created = []
    from app.modules.catalog.schema_profiler import profile_schema
    for tname, t in profile_schema()["tables"].items():
        if tname not in meta.tables:
            continue
        have = {c.name for c in meta.tables[tname].columns}
        for ix in t["indexes"]:
            if all(c in have for c in ix["columns"]):
                with target_engine.begin() as conn:
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {ix['name']} ON {tname}({', '.join(ix['columns'])})"))
                created.append(ix["name"])
    return created


def verify(source_engine, target_engine, counts: Dict[str, Tuple[int, int]]) -> List[str]:
    """Problems found after the copy (empty list = verified): row counts, numeric sums, unparsable timestamps."""
    problems = []
    for t, (s, d) in counts.items():
        if s != d:
            problems.append(f"{t}: source rows {s} != target rows {d}")
    insp = inspect(target_engine)
    for t in counts:
        for col in insp.get_columns(t):
            if isinstance(col["type"], Numeric) or str(col["type"]).upper().startswith(("INTEGER", "BIGINT", "SMALLINT")):
                q = text(f"SELECT COALESCE(SUM(CAST({col['name']} AS NUMERIC)), 0) FROM {t}")
                with source_engine.connect() as sc, target_engine.connect() as tc:
                    try:
                        a, b = Decimal(str(sc.execute(q).scalar())), Decimal(str(tc.execute(q).scalar()))
                    except Exception:
                        continue
                if abs(a - b) > Decimal("0.01"):
                    problems.append(f"{t}.{col['name']}: sum source {a} != target {b}")
    for key, n in copy_table.unparsable.items():
        problems.append(f"{key}: {n} unparsable timestamp value(s) stored as NULL")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--target-schema", choices=["auto", "mirror", "canonical"], default="auto")
    a = ap.parse_args(argv)
    target = create_engine(a.target)
    if target.dialect.name != "postgresql":
        print("target must be PostgreSQL", file=sys.stderr)
        return 2
    source = create_engine(a.source)
    physical, profile = analyze_source(source)
    mode = a.target_schema if a.target_schema != "auto" else ("canonical" if profile == "canonical" else "mirror")
    print(f"source profile={profile} tables={sorted(physical.tables)} -> target schema mode={mode}")

    if mode == "canonical":
        missing = missing_required_columns(physical, profile)
        if missing:
            print("cannot migrate into the canonical schema; the source lacks NOT NULL columns:\n  "
                  + "\n  ".join(missing) + "\nuse --target-schema mirror (no data is invented).", file=sys.stderr)
            return 3
        errors = create_schema(target)
    else:
        created = create_mirror_schema(source, target)
        print(f"mirror schema created; canonical indexes applied: {created}")
        errors = 0
    if a.schema_only:
        return 1 if errors else 0
    counts = copy_data(source, target)
    for t, (s, d) in counts.items():
        print(f"{t}: source={s} target={d}")
    problems = verify(source, target, counts)
    for p in problems:
        print(f"VERIFY: {p}", file=sys.stderr)
    print("verified" if not problems and not errors else "NOT verified")
    return 1 if (errors or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
