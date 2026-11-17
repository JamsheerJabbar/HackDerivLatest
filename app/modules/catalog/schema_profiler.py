"""Static profiler for the existing schema files (no live business DB access).

Parses app/files/derivinsight_schema.sql and app/files/alerts_schema.sql to produce
tables, columns, indexes, foreign keys, views and enum value profiles. A throwaway in-memory
SQLite sandbox (never the business DB) is used to EXPLAIN catalog queries.
"""
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_FILES = [ROOT / "app/files/derivinsight_schema.sql", ROOT / "app/files/alerts_schema.sql"]
_CONSTRAINT_WORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT", "KEY"}
_ENUM = re.compile(r"--\s*(?:Format:[^\n]*|)([A-Z][A-Z0-9_]*(?:\|[A-Z][A-Z0-9_]*)+)")


def _split_top(body: str) -> List[str]:
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_create_table(name: str, body: str) -> Dict[str, Any]:
    cols: Dict[str, Dict[str, Any]] = {}
    fks: List[Dict[str, str]] = []
    uniques: List[str] = []
    pk_cols: List[str] = []
    # keep per-line comments to extract enum values, then strip them for structure parsing
    enums: Dict[str, List[str]] = {}
    for line in body.splitlines():
        m = _ENUM.search(line)
        cm = re.match(r"\s*(\w+)\s", line)
        if m and cm:
            enums[cm.group(1)] = m.group(1).split("|")
    clean = re.sub(r"--[^\n]*", "", body)
    for item in _split_top(clean):
        item = " ".join(item.split())
        if not item:
            continue
        first = item.split(" ", 1)[0].upper()
        if first in _CONSTRAINT_WORDS:
            fk = re.match(r"FOREIGN KEY \((\w+)\) REFERENCES (\w+)\s*\((\w+)\)", item, re.I)
            if fk:
                fks.append({"column": fk.group(1), "ref_table": fk.group(2), "ref_column": fk.group(3)})
            pk = re.match(r"PRIMARY KEY \(([^)]+)\)", item, re.I)
            if pk:
                pk_cols += [c.strip() for c in pk.group(1).split(",")]
            continue
        cname, rest = item.split(" ", 1)
        up = rest.upper()
        col = {
            "type": re.match(r"[A-Za-z]+(?:\(\d+(?:,\d+)?\))?", rest).group(0),
            "nullable": not ("NOT NULL" in up or "PRIMARY KEY" in up),
            "pk": "PRIMARY KEY" in up,
            "unique": "UNIQUE" in up,
            "indexed": False,
            "fast_filter": False,
            "enum_values": enums.get(cname, []),
        }
        cols[cname] = col
        ref = re.search(r"REFERENCES (\w+)\s*\((\w+)\)", rest, re.I)
        if ref:
            fks.append({"column": cname, "ref_table": ref.group(1), "ref_column": ref.group(2)})
        if col["pk"]:
            pk_cols.append(cname)
    for c in pk_cols:
        if c in cols:
            cols[c]["pk"], cols[c]["nullable"] = True, False
    return {"kind": "table", "columns": cols, "indexes": [], "fks": fks}


def profile_schema(paths: Optional[List[Path]] = None) -> Dict[str, Any]:
    tables: Dict[str, Dict[str, Any]] = {}
    for path in paths or SCHEMA_FILES:
        text = Path(path).read_text(encoding="utf-8")
        for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\)\s*;", text, re.S | re.I):
            name = m.group(1)
            if name in tables:  # duplicate `dashboards` definition across files: first one wins
                continue
            tables[name] = _parse_create_table(name, m.group(2))
        for m in re.finditer(r"--\s*TABLE:\s*(\w+)\s*\n--\s*Description:\s*([^\n]*)", text):
            if m.group(1) in tables:
                tables[m.group(1)]["description"] = m.group(2).strip()
        for m in re.finditer(r"CREATE (?:UNIQUE )?INDEX\s+(\w+)\s+ON\s+(\w+)\s*\(([^)]+)\)", text, re.I):
            if m.group(2) in tables:
                tables[m.group(2)]["indexes"].append(
                    {"name": m.group(1), "columns": [c.strip() for c in m.group(3).split(",")]})
        for m in re.finditer(r"CREATE VIEW(?: IF NOT EXISTS)?\s+(\w+)\s+AS(.*?);", text, re.S | re.I):
            body = re.sub(r"--[^\n]*", "", m.group(2))
            deps = sorted(set(re.findall(r"(?:FROM|JOIN)\s+(\w+)", body, re.I)))
            tables[m.group(1)] = {"kind": "view", "columns": {}, "indexes": [], "fks": [], "depends_on": deps}
    sandbox = build_sandbox()
    try:
        for name, t in tables.items():
            if t["kind"] == "view":  # view columns come from the sandbox, not the business DB
                for _, cname, ctype, *_ in sandbox.execute(f"PRAGMA table_info({name})").fetchall():
                    t["columns"][cname] = {"type": ctype or "", "nullable": True, "pk": False, "unique": False,
                                           "indexed": False, "fast_filter": False, "enum_values": []}
    finally:
        sandbox.close()
    for t in tables.values():
        for col, c in t["columns"].items():
            if c["pk"] or c["unique"]:
                c["indexed"] = c["fast_filter"] = True
        for ix in t["indexes"]:
            for i, col in enumerate(ix["columns"]):
                if col in t["columns"]:
                    t["columns"][col]["indexed"] = True
                    if i == 0:
                        t["columns"][col]["fast_filter"] = True
    return {"tables": tables}


def _to_sqlite(stmt: str) -> str:
    s = re.sub(r"--[^\n]*", "", stmt)
    s = re.sub(r"\bINT\s+PRIMARY KEY\s+AUTO_INCREMENT\b", "INTEGER PRIMARY KEY AUTOINCREMENT", s, flags=re.I)
    s = re.sub(r"\bTINYINT\(1\)", "INTEGER", s, flags=re.I)
    s = re.sub(r"\s+ON UPDATE CURRENT_TIMESTAMP", "", s, flags=re.I)
    s = re.sub(r"INSERT IGNORE", "INSERT OR IGNORE", s, flags=re.I)
    return s


def build_sandbox() -> sqlite3.Connection:
    """In-memory SQLite with every schema object. Never connects to the business database."""
    conn = sqlite3.connect(":memory:")
    for path in SCHEMA_FILES:
        for raw in Path(path).read_text(encoding="utf-8").split(";"):
            stmt = _to_sqlite(raw).strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.Error:
                pass  # duplicates (dashboards, repeated index names) are expected
    conn.commit()
    return conn


def explain_plan(conn: sqlite3.Connection, sql: str, params: Dict[str, Any]) -> List[str]:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return [str(r[-1]) for r in rows]


def plan_uses_index(plan: List[str]) -> bool:
    return any(re.search(r"\bUSING (COVERING )?INDEX\b|PRIMARY KEY", p) for p in plan)


def plan_access(plan: List[str]) -> str:
    """seek = index SEARCH, index_scan = full scan over an index, full_scan = no index used."""
    if any(re.search(r"SEARCH .*(USING (COVERING )?INDEX|PRIMARY KEY)", p) for p in plan):
        return "seek"
    return "index_scan" if plan_uses_index(plan) else "full_scan"
