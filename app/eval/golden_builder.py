"""Golden dataset generation from the metric catalog.

Questions are paraphrased, typo'd, abbreviated and time-windowed to resemble production traffic.
Expected SQL is the catalog template; expected rows come from executing it on a seeded in-memory
SQLite sandbox with production-like synthetic data (never the business DB). Deterministic per seed.
"""
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.modules.catalog import schema_profiler as sp
from app.modules.catalog import templates as tpl
from app.modules.catalog.metric_generator import generate_catalog

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0)
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "data" / "golden_dataset.json"
MAX_ITEMS = 500
MAX_ROWS = 50

_PREFIXES = ["", "can you show", "i need", "give me", "pls show", "hey, what's", "quick question:", "tell me"]
_ABBREV = {"transactions": "txns", "transaction": "txn", "users": "usrs", "average": "avg", "number of": "no. of",
           "usd": "usd", "volume": "vol"}


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def seed_sandbox(conn: sqlite3.Connection, seed: int = 0) -> None:
    """Fill the sandbox with production-like rows: skewed statuses, lognormal amounts, ~90 days."""
    rng = random.Random(seed)
    users = []
    countries = [("US", 30), ("GB", 15), ("DE", 10), ("IN", 12), ("BR", 8), ("NG", 5), ("AE", 6), ("IR", 1), ("SY", 1), ("ZA", 6)]
    for i in range(1, 201):
        risk = _weighted(rng, [("LOW", 70), ("MEDIUM", 22), ("HIGH", 8)])
        users.append((f"USR-{i:04d}", f"user{i}@example.com", f"User {i}", _weighted(rng, countries),
                      _weighted(rng, [("VERIFIED", 75), ("PENDING", 15), ("REJECTED", 5), ("EXPIRED", 5)]),
                      risk, {"LOW": 10, "MEDIUM": 45, "HIGH": 75}[risk] + rng.randint(-8, 8),
                      1 if rng.random() < 0.04 else 0,
                      _weighted(rng, [("ACTIVE", 90), ("SUSPENDED", 5), ("FROZEN", 2), ("CLOSED", 3)]),
                      _ts(FIXED_NOW - timedelta(days=rng.randint(30, 900)))))
    conn.executemany("INSERT INTO users(user_id,email,full_name,country,kyc_status,risk_level,risk_score,is_pep,"
                     "account_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", users)
    uids = [u[0] for u in users]
    # heavy-tailed activity: a few power users dominate
    user_weights = [1 / (i + 1) ** 0.8 for i in range(len(uids))]

    def when(days=90):
        return _ts(FIXED_NOW - timedelta(days=rng.random() * days, hours=rng.random() * 24))

    txns = []
    instruments = ["EUR/USD", "BTC/USD", "GOLD", "GBP/USD", "ETH/USD", "USD/JPY", None]
    for i in range(1, 3001):
        amt = round(min(math.exp(rng.gauss(5.5, 1.5)), 250000), 2)
        ttype = _weighted(rng, [("TRADE", 50), ("DEPOSIT", 20), ("WITHDRAWAL", 12), ("FEE", 8), ("BONUS", 4), ("TRANSFER", 6)])
        if ttype == "DEPOSIT" and rng.random() < 0.03:
            amt = round(rng.uniform(9000, 10000), 2)  # structuring-like deposits
        status = _weighted(rng, [("COMPLETED", 86), ("PENDING", 5), ("FAILED", 5), ("FLAGGED", 3), ("REVERSED", 1)])
        txns.append((f"TXN-{i:06d}", rng.choices(uids, user_weights)[0], ttype, rng.choice(instruments), amt, "USD",
                     amt, status, "velocity" if status == "FLAGGED" else None,
                     _weighted(rng, [("CARD", 40), ("BANK_TRANSFER", 30), ("CRYPTO", 15), ("EWALLET", 15)]),
                     f"10.{rng.randint(0, 20)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}", when()))
    conn.executemany("INSERT INTO transactions(txn_id,user_id,txn_type,instrument,amount,currency,amount_usd,status,"
                     "flag_reason,payment_method,ip_address,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", txns)
    logins = []
    bad_ips = [f"203.0.113.{n}" for n in range(1, 6)]
    for i in range(1, 2001):
        status = _weighted(rng, [("SUCCESS", 80), ("FAILED", 13), ("BLOCKED", 4), ("MFA_REQUIRED", 3)])
        uid = rng.choice(uids) if rng.random() > 0.05 else None
        ip = rng.choice(bad_ips) if status in ("FAILED", "BLOCKED") and rng.random() < 0.5 else f"198.51.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        logins.append((f"EVT-{i:05d}", uid, f"user{rng.randint(1, 250)}@example.com", ip,
                       _weighted(rng, countries), rng.choice(["London", "Austin", "Mumbai", "Berlin"]),
                       _weighted(rng, [("MOBILE", 50), ("DESKTOP", 40), ("TABLET", 7), ("UNKNOWN", 3)]), status,
                       "WRONG_PASSWORD" if status == "FAILED" else ("IP_BLOCKED" if status == "BLOCKED" else None), when()))
    conn.executemany("INSERT INTO login_events(event_id,user_id,email_attempted,ip_address,country,city,device_type,"
                     "status,failure_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", logins)
    rules = [(r[0], r[1]) for r in conn.execute("SELECT rule_id, rule_name FROM alert_rules").fetchall()]
    alerts = []
    for i in range(1, 301):
        rid, rname = rng.choice(rules)
        alerts.append((f"ALR-{i:04d}", rname, rid, rng.choice(uids) if rng.random() > 0.1 else None,
                       _weighted(rng, [("LOW", 25), ("MEDIUM", 35), ("HIGH", 30), ("CRITICAL", 10)]),
                       _weighted(rng, [("OPEN", 35), ("INVESTIGATING", 15), ("RESOLVED", 35), ("DISMISSED", 10), ("ESCALATED", 5)]), when()))
    conn.executemany("INSERT INTO alerts(alert_id,rule_name,rule_id,user_id,severity,status,created_at) "
                     "VALUES (?,?,?,?,?,?,?)", alerts)
    conn.executemany("INSERT INTO audit_logs(log_id,actor_id,action,resource_type,created_at) VALUES (?,?,?,?,?)",
                     [(f"LOG-{i:05d}", rng.choice(uids[:20]), _weighted(rng, [("QUERY", 50), ("VIEW", 30), ("EXPORT", 8), ("UPDATE", 8), ("DELETE", 2), ("LOGIN", 2)]),
                       _weighted(rng, [("USER", 30), ("TRANSACTION", 40), ("ALERT", 20), ("DASHBOARD", 5), ("REPORT", 5)]), when()) for i in range(1, 501)])
    conn.executemany("INSERT INTO dashboards(dashboard_id,name,owner_id,widgets,layout,is_deployed,created_at) VALUES (?,?,?,?,?,?,?)",
                     [(f"DSH-{i}", f"Dash {i}", rng.choice(uids), "[]", "{}", 1 if rng.random() < 0.4 else 0, when()) for i in range(1, 21)])
    conn.executemany("INSERT INTO query_history(query_id,user_id,nl_query,generated_sql,result_count,is_favorite,created_at) VALUES (?,?,?,?,?,?,?)",
                     [(f"QRY-{i}", rng.choice(uids), "q", "SELECT 1", rng.randint(0, 100), 1 if rng.random() < 0.15 else 0, when()) for i in range(1, 101)])
    conn.executemany("INSERT INTO events(table_name,payload_json,created_at,processed) VALUES (?,?,?,?)",
                     [(rng.choice(["login", "transaction", "kyc", "user"]), "{}", when(), 1 if rng.random() < 0.8 else 0) for _ in range(200)])
    conn.executemany("INSERT INTO alert_history(metric_id,action,event_count,threshold,created_at) VALUES (?,?,?,?,?)",
                     [(rng.randint(1, 5), rng.choice(["triggered", "resolved"]), rng.randint(1, 60), 10, when()) for _ in range(100)])
    conn.executemany("INSERT INTO anomaly_history(metric_id,metric_name,severity,alert_count,current_status) VALUES (?,?,?,?,?)",
                     [(i, f"metric {i}", rng.choice(["low", "medium", "high", "critical"]), rng.randint(0, 20),
                       rng.choice(["active", "resolved"])) for i in range(1, 6)])
    conn.commit()


def _typo(text: str, rng: random.Random) -> str:
    words = text.split()
    cands = [i for i, w in enumerate(words) if len(w) > 4 and w.isalpha()]
    if not cands:
        return text
    i = rng.choice(cands)
    w = words[i]
    j = rng.randint(1, len(w) - 3)
    words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]
    return " ".join(words)


def _abbrev(text: str) -> str:
    for k, v in _ABBREV.items():
        text = text.replace(k, v)
    return text


def _question(metric: Dict[str, Any], variant: str, rng: random.Random, days: Optional[int]) -> str:
    syns = metric.get("synonyms") or []
    base = rng.choice([metric["name"].lower()] + syns)
    window = f" in the last {days} days" if days else ""
    if variant == "paraphrase":
        q = f"{rng.choice(_PREFIXES)} {base}{window}".strip()
    elif variant == "typo":
        q = _typo(f"show {base}{window}", rng)
    elif variant == "noisy_prod":
        q = _abbrev(f"{base}{window}").replace(" the ", " ")
    else:  # time_window / synonym
        q = f"show me {base}{window}"
    return " ".join(q.split())


def _rows(conn: sqlite3.Connection, sql: str, bind: Dict[str, Any]) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, bind)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchmany(MAX_ROWS)]


def build_golden_dataset(catalog: Optional[Dict[str, Any]] = None, per_metric: int = 3, seed: int = 0) -> Dict[str, Any]:
    catalog = catalog or generate_catalog()
    rng = random.Random(seed)
    conn = sp.build_sandbox()
    seed_sandbox(conn, seed)
    metrics = catalog["metrics"]
    by_id = {m["id"]: m for m in metrics}
    variants = ["time_window", "paraphrase", "typo", "noisy_prod", "synonym"]
    items: List[Dict[str, Any]] = []
    seen_q: set = set()

    def make_item(idx: int, ids: List[str], variant: str, question: str, params: Dict[str, Any], difficulty: str):
        m = by_id[ids[0]]
        sql, bind = tpl.render_template(m, params, now=FIXED_NOW)
        return {"id": f"G-{idx:04d}", "question": question, "metric_id": m["id"], "primary_metric_id": m["id"],
                "expected_metric_ids": ids, "expected_tables": sorted({t for i in ids for t in by_id[i]["tables"]}),
                "expected_sql": sql, "expected_bind": bind, "expected_params": params,
                "expected_rows": _rows(conn, sql, bind), "domain": m["domain"], "difficulty": difficulty,
                "variant": variant, "provenance": {"metric_id": m["id"], "catalog_version": catalog["version"]}}

    for m in metrics:
        has_days = any(p["name"] == "days" for p in m["parameters"])
        for n in range(per_metric):
            variant = variants[(n + rng.randrange(len(variants))) % len(variants)]
            days = rng.choice([7, 14, 30, 90]) if has_days and (variant == "time_window" or rng.random() < 0.5) else None
            params = {"days": days} if days else {}
            q = _question(m, variant, rng, days)
            for _ in range(5):  # avoid duplicate questions across items
                if q not in seen_q:
                    break
                q = _question(m, variant, rng, days)
            if q in seen_q:
                continue
            seen_q.add(q)
            items.append(make_item(len(items) + 1, [m["id"]], variant, q, params, "join" if m["joins"] else "simple"))
    # complex: two metrics in one question, evaluated on the primary metric
    pool = [m for m in metrics if m["synonyms"]]
    rng.shuffle(pool)
    for a, b in zip(pool[0::2], pool[1::2]):
        if len(items) >= MAX_ITEMS:
            break
        q = f"{rng.choice(a['synonyms'])} and also {rng.choice(b['synonyms'])}"
        if q in seen_q:
            continue
        seen_q.add(q)
        items.append(make_item(len(items) + 1, [a["id"], b["id"]], "multi_metric", q, {}, "complex"))
        if sum(i["difficulty"] == "complex" for i in items) >= 60:
            break
    conn.close()
    items = items[:MAX_ITEMS]
    return {"meta": {"catalog_version": catalog["version"], "seed": seed, "now": _ts(FIXED_NOW), "count": len(items)},
            "items": items}


def write_golden(path: Optional[str] = None, dataset: Optional[Dict[str, Any]] = None, **kw) -> str:
    dataset = dataset or build_golden_dataset(**kw)
    p = Path(path or GOLDEN_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(dataset, indent=1, default=str))
    tmp.replace(p)
    return str(p)


def load_golden(path: Optional[str] = None) -> Dict[str, Any]:
    return json.loads(Path(path or GOLDEN_PATH).read_text())


if __name__ == "__main__":
    print(write_golden())
