"""Column-mapping layer: logical (canonical) schema -> the physical database actually in use.

The metric catalog, templates and retrieval are written against the canonical schema
(app/files/derivinsight_schema.sql). A physical database may differ (e.g. the live SQLite file has
users.username instead of full_name, no email/country, text timestamps in three formats). This module:

  * introspects the physical schema and picks a mapping profile (app/data/schema_mappings.json),
  * resolves every logical table.column to a physical column / SQL expression, or reports it unavailable,
  * rewrites catalog SQL (sqlglot) from logical to physical, translating enum values on comparisons,
  * reports which catalog metrics are available on a given database (never fabricating missing data).

CLI: python -m app.core.schema_mapping --db sqlite:///derivinsightnew.db [--execute]
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlglot
from sqlalchemy import create_engine, inspect, text
from sqlglot import exp

MAPPINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "schema_mappings.json"
_INT_TYPES = ("INT", "SMALLINT", "BIGINT", "TINYINT")


class UnavailableColumn(Exception):
    """A logical table/column has no physical counterpart on this database."""


# ─── timestamp normalisation (used by SQLite queries and by the migration) ──────────

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO ('2023-01-15T09:30:00Z', '2024-04-18 21:53:35') and US ('3/31/24 6:07') text. None if unparsable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        m = _ISO.match(s)
        if m:
            y, mo, d, h, mi, sec = m.groups()
            return datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
        m = _US.match(s)
        if m:
            mo, d, y, h, mi, sec = m.groups()
            year = int(y) + (2000 if len(y) <= 2 else 0)
            return datetime(year, int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0))
    except ValueError:
        return None
    return None


def norm_ts(value: Any) -> Optional[str]:
    """SQLite scalar function: any supported text timestamp -> 'YYYY-MM-DD HH:MM:SS' (NULL when unparsable)."""
    dt = parse_timestamp(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def register_sqlite_functions(dbapi_conn, _record=None) -> None:
    dbapi_conn.create_function("norm_ts", 1, norm_ts, deterministic=True)


# ─── physical schema ────────────────────────────────────────────────────────────────

@dataclass
class PhysicalSchema:
    dialect: str
    tables: Dict[str, Dict[str, str]]  # table/view -> column -> declared type

    @classmethod
    def introspect(cls, engine) -> "PhysicalSchema":
        insp = inspect(engine)
        tables: Dict[str, Dict[str, str]] = {}
        for name in list(insp.get_table_names()) + list(insp.get_view_names()):
            tables[name] = {c["name"]: str(c["type"]).upper() for c in insp.get_columns(name)}
        return cls(engine.dialect.name, tables)


def load_profiles(path: Optional[Path] = None) -> Dict[str, Any]:
    return json.loads(Path(path or MAPPINGS_PATH).read_text()).get("profiles", {})


def detect_profile(physical: PhysicalSchema, profiles: Optional[Dict[str, Any]] = None) -> str:
    for name, cfg in (profiles if profiles is not None else load_profiles()).items():
        ok = True
        for table, rule in cfg.get("match", {}).items():
            cols = physical.tables.get(table)
            if cols is None or any(c not in cols for c in rule.get("present", [])) \
                    or any(c in cols for c in rule.get("absent", [])):
                ok = False
                break
        if ok:
            return name
    return "canonical"


@dataclass(frozen=True)
class Resolved:
    physical: str
    wrapper: str = "{c}"                                   # SQL template over the qualified physical column
    value_map: Optional[Tuple[Tuple[str, str], ...]] = None  # logical enum value (UPPER) -> physical value

    @property
    def is_identity(self) -> bool:
        return self.wrapper == "{c}" and self.value_map is None


@dataclass
class Rewrite:
    sql: str
    bind_maps: Dict[str, Dict[str, str]] = field(default_factory=dict)  # bind name -> {LOGICAL_UPPER: physical}

    def apply_bind(self, bind: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(bind or {})
        for name, vmap in self.bind_maps.items():
            if isinstance(out.get(name), str):
                out[name] = vmap.get(out[name].upper(), out[name])
        return out


class SchemaMapping:
    def __init__(self, physical: PhysicalSchema, logical: Optional[Dict[str, Any]] = None,
                 profiles: Optional[Dict[str, Any]] = None, profile: Optional[str] = None):
        if logical is None:
            from app.modules.catalog.schema_profiler import profile_schema
            logical = profile_schema()["tables"]
        self.physical, self.logical = physical, logical
        profiles = load_profiles() if profiles is None else profiles
        self.profile_name = profile or detect_profile(physical, profiles)
        self.cfg: Dict[str, Any] = profiles.get(self.profile_name, {})
        self._cache: Dict[Tuple[str, str], Resolved] = {}

    # ── resolution ────────────────────────────────────────────────────────────
    def table_available(self, table: str) -> bool:
        return table in self.physical.tables

    def resolve(self, table: str, column: str) -> Resolved:
        key = (table, column)
        if key in self._cache:
            return self._cache[key]
        if not self.table_available(table):
            raise UnavailableColumn(f"table '{table}' does not exist")
        pcols = self.physical.tables[table]
        override = self.cfg.get("columns", {}).get(f"{table}.{column}", {})
        if override.get("unavailable"):
            raise UnavailableColumn(f"{table}.{column} is marked unavailable")
        phys = override.get("physical", column)
        if phys not in pcols:
            raise UnavailableColumn(f"{table}.{column} has no physical column")
        wrapper = override.get("expr", "{c}")
        ltype = str(self.logical.get(table, {}).get("columns", {}).get(column, {}).get("type", "")).upper()
        ptype = pcols[phys]
        if wrapper == "{c}":
            if ltype.startswith("BOOLEAN") and ptype.startswith(_INT_TYPES):
                wrapper = "({c} <> 0)"  # integer 0/1 flag exposed as a boolean
            elif (self.cfg.get("normalize_timestamps") and self.physical.dialect == "sqlite"
                  and ltype.startswith(("TIMESTAMP", "DATE", "DATETIME"))):
                wrapper = "norm_ts({c})"
        vmap = self.cfg.get("value_maps", {}).get(f"{table}.{column}")
        res = Resolved(phys, wrapper, tuple((k.upper(), v) for k, v in vmap.items()) if vmap else None)
        self._cache[key] = res
        return res

    # ── SQL rewriting ─────────────────────────────────────────────────────────
    def rewrite_sql(self, sql: str) -> Rewrite:
        tree = sqlglot.parse_one(sql)
        alias_map: Dict[str, str] = {}
        for t in tree.find_all(exp.Table):
            if not self.table_available(t.name):
                raise UnavailableColumn(f"table '{t.name}' does not exist")
            alias_map[t.alias or t.name] = t.name
        scope_tables = sorted(set(alias_map.values()))
        select_aliases = {a.alias for a in tree.find_all(exp.Alias)}
        bind_maps: Dict[str, Dict[str, str]] = {}

        for col in list(tree.find_all(exp.Column)):
            qual, name = col.table, col.name
            if qual:
                table = alias_map.get(qual)
                if table is None:
                    continue
            else:
                cands = [t for t in scope_tables if name in self.logical.get(t, {}).get("columns", {})]
                if not cands:
                    if name in select_aliases:
                        continue  # reference to a SELECT alias, not a table column
                    # unknown to the logical schema: leave as-is (physical-only column)
                    continue
                if len(cands) > 1:
                    raise UnavailableColumn(f"ambiguous unqualified column '{name}'")
                table = cands[0]
            res = self.resolve(table, name)
            if res.is_identity and res.physical == name:
                continue  # same name, no transformation
            if res.value_map:
                self._translate_values(col, dict(res.value_map), bind_maps)
            ref = f"{qual}.{res.physical}" if qual else res.physical
            new = sqlglot.parse_one(res.wrapper.format(c=ref))
            parent = col.parent
            if isinstance(parent, exp.Select) and col.arg_key == "expressions" and name != res.physical:
                new = exp.alias_(new, name)  # keep the logical output column name
            elif isinstance(parent, exp.Select) and col.arg_key == "expressions" and res.wrapper != "{c}":
                new = exp.alias_(new, name)
            col.replace(new)
        return Rewrite(tree.sql(), bind_maps)

    @staticmethod
    def _translate_values(col: exp.Column, vmap: Dict[str, str], bind_maps: Dict[str, Dict[str, str]]) -> None:
        parent = col.parent
        if isinstance(parent, (exp.EQ, exp.NEQ)):
            other = parent.right if parent.left is col else parent.left
            others = [other]
        elif isinstance(parent, exp.In):
            others = list(parent.expressions)
        else:
            return
        for o in others:
            if isinstance(o, exp.Literal) and o.is_string and o.this.upper() in vmap:
                o.replace(exp.Literal.string(vmap[o.this.upper()]))
            elif isinstance(o, exp.Placeholder):
                bind_maps[o.name] = vmap

    # ── catalog availability ──────────────────────────────────────────────────
    def available_metrics(self, catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """metric_id -> {'available': bool, 'reason': str|None, 'sql': physical SQL when available}."""
        out: Dict[str, Dict[str, Any]] = {}
        for m in catalog["metrics"]:
            try:
                rw = self.rewrite_sql(m["sql"])
                out[m["id"]] = {"available": True, "reason": None, "sql": rw.sql}
            except UnavailableColumn as e:
                out[m["id"]] = {"available": False, "reason": str(e), "sql": None}
        return out

    def available_ids(self, catalog: Dict[str, Any]) -> Set[str]:
        return {k for k, v in self.available_metrics(catalog).items() if v["available"]}

    def unavailable_reasons(self, catalog: Dict[str, Any]) -> Dict[str, str]:
        """metric_id -> why it cannot run on this database (e.g. "users.country has no physical column")."""
        return {k: v["reason"] for k, v in self.available_metrics(catalog).items() if not v["available"]}


def mapping_for_engine(engine, **kw) -> SchemaMapping:
    return SchemaMapping(PhysicalSchema.introspect(engine), **kw)


# ─── CLI: availability report (optionally executes every available metric, read-only) ──

def _main(argv: Optional[List[str]] = None) -> int:
    from sqlalchemy import event
    from app.modules.catalog import templates as tpl
    from app.modules.catalog.metric_generator import generate_catalog
    ap = argparse.ArgumentParser(description="Report which catalog metrics run on a physical database")
    ap.add_argument("--db", required=True, help="SQLAlchemy URL (use sqlite:///file:path?mode=ro&uri=true for read-only)")
    ap.add_argument("--execute", action="store_true", help="also execute each available metric with defaults")
    a = ap.parse_args(argv)
    engine = create_engine(a.db)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", register_sqlite_functions)
    mapping = mapping_for_engine(engine)
    catalog = generate_catalog()
    report = mapping.available_metrics(catalog)
    avail = [k for k, v in report.items() if v["available"]]
    print(f"profile={mapping.profile_name} dialect={engine.dialect.name} tables={sorted(mapping.physical.tables)}")
    print(f"available: {len(avail)}/{len(report)} metrics")
    reasons: Dict[str, int] = {}
    for v in report.values():
        if not v["available"]:
            reasons[v["reason"]] = reasons.get(v["reason"], 0) + 1
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  unavailable x{n}: {r}")
    failures = 0
    if a.execute:
        by_id = {m["id"]: m for m in catalog["metrics"]}
        with engine.connect() as c:
            for mid in avail:
                sql, bind = tpl.render_template(by_id[mid], {})
                rw = mapping.rewrite_sql(sql)
                try:
                    rows = c.execute(text(rw.sql), rw.apply_bind(bind)).fetchall()
                except Exception as e:
                    failures += 1
                    print(f"  EXECUTION FAILED {mid}: {str(e).splitlines()[0]}")
                    if engine.dialect.name == "postgresql":
                        c.rollback()
                else:
                    report[mid]["rows"] = len(rows)
        print(f"executed {len(avail) - failures}/{len(avail)} available metrics without error")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
