"""Metric catalog generation, validation against the schema profile, and vector indexing.

Metrics = curated cross-table templates (templates.CURATED) + auto-generated per-column metrics
derived from the profile (enum distributions, filtered counts, daily trends). Every metric is
validated: tables/columns exist, SQL is SELECT-only, SQL executes in the sandbox with defaults, and
`fast` is derived from the EXPLAIN plan (index seek, no bare full-table scan).
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.modules.catalog import schema_profiler as sp
from app.modules.catalog import templates as tpl

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
WEIGHTS_PATH = DATA_DIR / "table_weights.json"
CATALOG_PATH = DATA_DIR / "metric_catalog.json"
VALUE_CAP = 200

COLUMN_INTERP = {
    "transactions.status": "Distribution of transaction outcomes; growth in FAILED or FLAGGED is a risk or reliability signal.",
    "transactions.txn_type": "Mix of financial activity; shifts show changing customer behaviour.",
    "transactions.payment_method": "Channel mix; concentration raises provider dependency risk.",
    "users.kyc_status": "Compliance coverage of the customer base.",
    "users.risk_level": "Risk profile of customers; HIGH share is exposure.",
    "users.account_status": "Account health; SUSPENDED or FROZEN reflect enforcement.",
    "login_events.status": "Authentication outcomes; FAILED or BLOCKED growth signals attacks.",
    "login_events.device_type": "Device mix; UNKNOWN devices are less trustworthy.",
    "login_events.failure_reason": "Why logins fail; ACCOUNT_LOCKED or IP_BLOCKED point to defences working.",
    "alerts.severity": "Alert criticality mix.",
    "alerts.status": "Alert handling backlog; OPEN and ESCALATED need attention.",
    "alert_rules.severity": "Distribution of rule severities configured.",
    "alert_rules.rule_type": "Detection coverage by rule family.",
    "audit_logs.action": "Mix of audited actions; EXPORT and DELETE deserve scrutiny.",
    "audit_logs.resource_type": "Which resources are touched most.",
}
DOMAIN_BY_TABLE = {"login_events": "security", "alerts": "security", "users": "compliance",
                   "transactions": "risk", "audit_logs": "compliance"}


def load_table_weights(path: Optional[str] = None) -> Dict[str, float]:
    return json.loads(Path(path or WEIGHTS_PATH).read_text())


def _auto_metrics(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for tname, t in profile["tables"].items():
        if t["kind"] != "table":
            continue
        cols = t["columns"]
        has_ts = "created_at" in cols
        dom = DOMAIN_BY_TABLE.get(tname, "operations")
        for cname, c in cols.items():
            if not c["enum_values"]:
                continue
            interp = COLUMN_INTERP.get(f"{tname}.{cname}", f"Distribution of {tname} rows by {cname}.")
            out.append(tpl._t(
                f"{tname}_by_{cname}", f"{tname.replace('_', ' ').title()} by {cname}", dom,
                f"Row count of {tname} grouped by {cname}.", [tname], [f"{tname}.{cname}"],
                f"COUNT(*) GROUP BY {cname}", [],
                f"SELECT {cname}, COUNT(*) AS row_count FROM {tname} GROUP BY {cname}",
                interp, "neutral", [f"{tname} by {cname}", f"{cname} breakdown of {tname}"]))
            if has_ts:
                out.append(tpl._t(
                    f"{tname}_{cname}_count", f"{tname.replace('_', ' ').title()} count for a {cname}", dom,
                    f"Count of {tname} rows with a given {cname} in the window.", [tname],
                    [f"{tname}.{cname}", f"{tname}.created_at"], f"COUNT(*) WHERE {cname} = :value",
                    [tpl.DAYS, tpl._p("value", "str", c["enum_values"][0], choices=list(c["enum_values"]))],
                    f"SELECT COUNT(*) AS row_count FROM {tname} WHERE {cname} = :value AND created_at >= :since",
                    interp, "neutral", [f"number of {tname} with {cname}", f"{cname} count in {tname}"]))
        if has_ts:
            out.append(tpl._t(
                f"{tname}_daily_count", f"{tname.replace('_', ' ').title()} created per day", dom,
                f"Rows created per day in {tname}.", [tname], [f"{tname}.created_at"],
                "COUNT(*) GROUP BY DATE(created_at)", [tpl.DAYS],
                f"SELECT DATE(created_at) AS day, COUNT(*) AS row_count FROM {tname} "
                f"WHERE created_at >= :since GROUP BY DATE(created_at) ORDER BY day",
                "Volume trend; breaks in the pattern indicate incidents or campaigns.", "neutral",
                [f"{tname} per day", f"daily {tname} trend"]))
    return out


def _check_refs(m: Dict[str, Any], profile: Dict[str, Any]) -> None:
    for t in m["tables"]:
        if t not in profile["tables"]:
            raise ValueError(f"{m['id']}: unknown table {t}")
    for ref in m["columns"]:
        t, _, c = ref.partition(".")
        cols = profile["tables"][t]["columns"]
        if c not in cols and profile["tables"][t]["kind"] == "table":
            raise ValueError(f"{m['id']}: unknown column {ref}")


def _finalize(m: Dict[str, Any], profile: Dict[str, Any], conn) -> Dict[str, Any]:
    _check_refs(m, profile)
    sql, bind = tpl.render_template(m, {})
    plan = sp.explain_plan(conn, sql, bind)  # also proves the SQL executes in the sandbox
    conn.execute(sql, bind).fetchall()
    indexed = any(
        profile["tables"][r.split(".")[0]]["columns"].get(r.split(".")[1], {}).get("indexed", False)
        for r in m["columns"] if r.split(".")[0] in profile["tables"]
        and profile["tables"][r.split(".")[0]]["kind"] == "table")
    m = dict(m)
    m.update({"explain": plan, "indexed": bool(indexed), "fast": sp.plan_uses_index(plan),
              "access": sp.plan_access(plan),
              "keywords": sorted(set(re.findall(r"[a-z_]{3,}", " ".join([m["name"], *m["synonyms"]]).lower()))),
              "example_queries": tpl.nl_variants(m)})
    return m


def generate_catalog(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = profile or sp.profile_schema()
    conn = sp.build_sandbox()
    try:
        metrics, seen = [], set()
        for raw in tpl.CURATED + _auto_metrics(profile):
            if raw["id"] in seen:
                continue
            seen.add(raw["id"])
            metrics.append(_finalize(raw, profile, conn))
    finally:
        conn.close()
    body = json.dumps([{k: v for k, v in m.items() if k != "explain"} for m in metrics], sort_keys=True)
    digest = hashlib.sha256(body.encode()).hexdigest()[:8]
    return {"version": f"{settings.CATALOG_VERSION}-{digest}",
            "generated_at": datetime.now(timezone.utc).isoformat(), "metrics": metrics}


def write_catalog(path: Optional[str] = None, catalog: Optional[Dict[str, Any]] = None) -> str:
    catalog = catalog or generate_catalog()
    p = Path(path or CATALOG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(catalog, indent=2, default=str))
    return str(p)


def _meta(level: str, table: str, now: str, weights: Dict[str, float], **kw) -> Dict[str, Any]:
    base = {"user_id": None, "session_id": None, "level": level, "table": table, "indexed": False,
            "joins": [], "fast": False, "interpretation": "", "created_at": now,
            "weight": weights.get(table, 0.5)}
    base.update(kw)
    return base


def build_vector_documents(catalog: Dict[str, Any], profile: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
    weights = load_table_weights()
    now = datetime.now(timezone.utc).isoformat()
    texts: List[str] = []
    metas: List[Dict[str, Any]] = []

    def add(t, m):
        texts.append(t)
        metas.append(m)

    for tname, t in profile["tables"].items():
        joins = sorted({f["ref_table"] for f in t["fks"]} | set(t.get("depends_on", [])) | {
            o for o, ot in profile["tables"].items() for f in ot["fks"] if f["ref_table"] == tname})
        any_index = bool(t["indexes"]) or any(c["indexed"] for c in t["columns"].values())
        idx_txt = "; ".join(f"{i['name']}({', '.join(i['columns'])})" for i in t["indexes"]) or "none"
        add(f"{t['kind']} {tname}: {t.get('description', tname)}. Columns: {', '.join(t['columns'])}. "
            f"Indexes: {idx_txt}. Joins: {', '.join(joins) or 'none'}.",
            _meta("table", tname, now, weights, indexed=any_index, joins=joins, fast=any_index,
                  interpretation=t.get("description", f"{t['kind']} {tname}")))
        for cname, c in t["columns"].items():
            enum = f" Values: {', '.join(c['enum_values'])}." if c["enum_values"] else ""
            interp = COLUMN_INTERP.get(f"{tname}.{cname}", f"{cname} of {tname} ({c['type']})")
            add(f"column {tname}.{cname} {c['type']}{' primary key' if c['pk'] else ''}"
                f"{' indexed' if c['indexed'] else ' not indexed'}.{enum}",
                _meta("column", tname, now, weights, column=cname, indexed=c["indexed"], joins=joins,
                      fast=c["fast_filter"], interpretation=interp))
            for v in c["enum_values"][:VALUE_CAP]:
                add(f"value {tname}.{cname} = {v}",
                    _meta("value", tname, now, weights, column=cname, value=v, indexed=c["indexed"],
                          joins=joins, fast=c["fast_filter"], interpretation=f"Rows of {tname} where {cname} is {v}"))
    for m in catalog["metrics"]:
        table = m["tables"][0]
        joins = sorted(set(m["tables"]) - {table})
        common = dict(metric_id=m["id"], indexed=m["indexed"], joins=joins, fast=m["fast"],
                      interpretation=m["interpretation"], domain=m["domain"], direction=m["direction"],
                      catalog_version=catalog["version"])
        add(f"metric {m['name']}: {m['description']} Keywords: {', '.join(m['keywords'])}. "
            f"Examples: {'; '.join(m['example_queries'][:3])}. Interpretation: {m['interpretation']}",
            _meta("metric", table, now, weights, **common))
        add(f"template {m['id']}: {m['sql']} Params: {', '.join(p['name'] for p in m['parameters']) or 'none'}",
            _meta("template", table, now, weights, **common))
    return texts, metas


async def index_catalog(store, catalog: Dict[str, Any], profile: Dict[str, Any]) -> int:
    texts, metas = build_vector_documents(catalog, profile)
    await store.add_documents(texts, metas)
    return len(texts)


if __name__ == "__main__":
    print(f"wrote {write_catalog()}")
