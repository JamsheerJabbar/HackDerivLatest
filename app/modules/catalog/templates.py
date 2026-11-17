"""Curated parameterized metric templates + safe rendering.

All SQL uses :name bind parameters and is portable across SQLite and PostgreSQL.
A time window is expressed as `days` and exposed to SQL as :since (ISO timestamp).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import sqlglot
from sqlglot import exp

DAYS = {"name": "days", "type": "int", "default": 7, "min": 1, "max": 3650}
LIMIT = {"name": "limit", "type": "int", "default": 10, "min": 1, "max": 1000}


def _p(name, typ, default, **kw):
    return {"name": name, "type": typ, "default": default, **kw}


# id, name, domain, description, tables, joins, columns, formula, params, sql, interpretation, direction, synonyms
def _t(id, name, domain, description, tables, columns, formula, params, sql, interpretation,
       direction="neutral", synonyms=(), joins=()):
    return {"id": id, "name": name, "domain": domain, "description": description, "tables": list(tables),
            "joins": [{"left": a, "right": b, "on": on} for a, b, on in joins], "columns": list(columns),
            "formula": formula, "parameters": params, "sql": " ".join(sql.split()),
            "interpretation": interpretation, "direction": direction, "synonyms": list(synonyms)}


TXN_USER = ("transactions", "users", "transactions.user_id = users.user_id")
ALR_USER = ("alerts", "users", "alerts.user_id = users.user_id")
LOGIN_USER = ("login_events", "users", "login_events.user_id = users.user_id")

CURATED: List[Dict[str, Any]] = [
    _t("flagged_txn_count", "Flagged transaction count", "risk",
       "Number of transactions flagged as suspicious in the window.", ["transactions"],
       ["transactions.status", "transactions.created_at"], "COUNT(*) WHERE status='FLAGGED'", [DAYS],
       "SELECT COUNT(*) AS flagged_count FROM transactions WHERE status = 'FLAGGED' AND created_at >= :since",
       "A rising count signals fraud or rule tuning issues; compare with total volume for a rate.",
       "higher_is_worse", ["suspicious transactions", "flagged payments"]),
    _t("txn_volume_usd", "Completed transaction volume (USD)", "operations",
       "Total USD value of completed transactions in the window.", ["transactions"],
       ["transactions.amount_usd", "transactions.status", "transactions.created_at"],
       "SUM(amount_usd) WHERE status='COMPLETED'", [DAYS],
       "SELECT SUM(amount_usd) AS volume_usd FROM transactions WHERE status = 'COMPLETED' AND created_at >= :since",
       "Business throughput; sharp drops hint at outages, spikes at campaigns or abuse.",
       "higher_is_better", ["total volume", "transaction value", "money moved"]),
    _t("failed_txn_rate", "Failed transaction rate", "operations",
       "Share of transactions that failed in the window.", ["transactions"],
       ["transactions.status", "transactions.created_at"], "failed / total",
       [DAYS],
       "SELECT SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS failed_rate "
       "FROM transactions WHERE created_at >= :since",
       "Above a few percent suggests payment provider or validation problems.",
       "higher_is_worse", ["transaction failure rate", "payment failures"]),
    _t("avg_txn_amount_by_type", "Average transaction amount by type", "operations",
       "Mean USD amount per transaction type.", ["transactions"],
       ["transactions.txn_type", "transactions.amount_usd", "transactions.created_at"], "AVG(amount_usd) GROUP BY txn_type",
       [DAYS],
       "SELECT txn_type, AVG(amount_usd) AS avg_amount_usd FROM transactions WHERE created_at >= :since GROUP BY txn_type",
       "Shows typical ticket size per activity; outlying types deserve review.",
       "neutral", ["average deposit", "average withdrawal", "ticket size"]),
    _t("large_txn_count", "Large transaction count", "risk",
       "Transactions above a USD threshold (default matches RULE-001: 50000).", ["transactions"],
       ["transactions.amount_usd", "transactions.created_at"], "COUNT(*) WHERE amount_usd > :min_amount",
       [DAYS, _p("min_amount", "float", 50000.0, min=0, max=1e9)],
       "SELECT COUNT(*) AS large_txn_count FROM transactions WHERE amount_usd > :min_amount AND created_at >= :since",
       "Large movements need AML review; trend matters more than single counts.",
       "higher_is_worse", ["big transactions", "high value transactions"]),
    _t("top_users_by_txn_count", "Most active users by transactions", "risk",
       "Users with the highest transaction counts in the window.", ["transactions"],
       ["transactions.user_id", "transactions.created_at"], "COUNT(*) GROUP BY user_id ORDER BY 1 DESC",
       [DAYS, LIMIT],
       "SELECT user_id, COUNT(*) AS txn_count FROM transactions WHERE created_at >= :since "
       "GROUP BY user_id ORDER BY txn_count DESC LIMIT :limit",
       "Very high counts can indicate bots, velocity abuse or power users.",
       "neutral", ["busiest users", "velocity", "most active traders"]),
    _t("structuring_candidates", "Structuring candidates", "compliance",
       "Users with repeated deposits just below the reporting threshold (RULE-007).", ["transactions"],
       ["transactions.user_id", "transactions.txn_type", "transactions.amount_usd", "transactions.created_at"],
       "COUNT(*) WHERE DEPOSIT AND amount BETWEEN 9000 AND 10000 HAVING >= 3",
       [DAYS, _p("min_count", "int", 3, min=1, max=1000)],
       "SELECT user_id, COUNT(*) AS deposit_count FROM transactions WHERE txn_type = 'DEPOSIT' "
       "AND amount_usd >= 9000 AND amount_usd <= 10000 AND created_at >= :since "
       "GROUP BY user_id HAVING COUNT(*) >= :min_count",
       "Classic structuring pattern; each listed user should be escalated to compliance.",
       "higher_is_worse", ["structuring", "smurfing", "deposits below threshold"]),
    _t("flagged_txn_by_country", "Flagged transactions by user country", "risk",
       "Flagged transactions grouped by the user's country.", ["transactions", "users"],
       ["transactions.status", "transactions.created_at", "transactions.user_id", "users.user_id", "users.country"],
       "COUNT(*) JOIN users GROUP BY country", [DAYS],
       "SELECT users.country, COUNT(*) AS flagged_count FROM transactions JOIN users "
       "ON transactions.user_id = users.user_id WHERE transactions.status = 'FLAGGED' "
       "AND transactions.created_at >= :since GROUP BY users.country ORDER BY flagged_count DESC",
       "Concentration in a country points to regional fraud or sanctions exposure.",
       "higher_is_worse", ["flagged by country", "suspicious activity by region"], [TXN_USER]),
    _t("high_risk_user_volume", "Transaction volume of high-risk users", "risk",
       "USD volume transacted by users with a HIGH risk level.", ["transactions", "users"],
       ["users.risk_level", "users.user_id", "transactions.user_id", "transactions.amount_usd", "transactions.created_at"],
       "SUM(amount_usd) JOIN users WHERE risk_level='HIGH'", [DAYS],
       "SELECT SUM(transactions.amount_usd) AS high_risk_volume_usd FROM users JOIN transactions "
       "ON transactions.user_id = users.user_id WHERE users.risk_level = 'HIGH' AND transactions.created_at >= :since",
       "Share of volume from high-risk customers; growth increases exposure.",
       "higher_is_worse", ["high risk volume", "risky customer volume"], [TXN_USER]),
    _t("pep_transaction_volume", "PEP transaction volume", "compliance",
       "USD volume of politically exposed persons.", ["transactions", "users"],
       ["users.is_pep", "users.user_id", "transactions.user_id", "transactions.amount_usd", "transactions.created_at"],
       "SUM(amount_usd) JOIN users WHERE is_pep", [DAYS],
       "SELECT SUM(transactions.amount_usd) AS pep_volume_usd FROM users JOIN transactions "
       "ON transactions.user_id = users.user_id WHERE users.is_pep = TRUE AND transactions.created_at >= :since",
       "PEP activity requires enhanced due diligence.",
       "higher_is_worse", ["politically exposed persons volume", "pep activity"], [TXN_USER]),
    _t("unverified_deposit_users", "Unverified users with deposits", "compliance",
       "Distinct users who deposited without verified KYC.", ["transactions", "users"],
       ["users.kyc_status", "users.user_id", "transactions.user_id", "transactions.txn_type", "transactions.created_at"],
       "COUNT(DISTINCT user_id) WHERE kyc_status != 'VERIFIED' AND DEPOSIT", [DAYS],
       "SELECT COUNT(DISTINCT users.user_id) AS unverified_depositors FROM users JOIN transactions "
       "ON transactions.user_id = users.user_id WHERE users.kyc_status <> 'VERIFIED' "
       "AND transactions.txn_type = 'DEPOSIT' AND transactions.created_at >= :since",
       "Any non-zero value is a KYC control gap.",
       "higher_is_worse", ["deposits without kyc", "unverified depositors"], [TXN_USER]),
    _t("txn_by_instrument_volume", "Volume by instrument", "operations",
       "USD volume per traded instrument.", ["transactions"],
       ["transactions.instrument", "transactions.amount_usd", "transactions.created_at"],
       "SUM(amount_usd) GROUP BY instrument", [DAYS, LIMIT],
       "SELECT instrument, SUM(amount_usd) AS volume_usd FROM transactions WHERE created_at >= :since "
       "AND instrument IS NOT NULL GROUP BY instrument ORDER BY volume_usd DESC LIMIT :limit",
       "Shows where trading demand and concentration risk sit.",
       "neutral", ["top instruments", "popular markets"]),
    _t("payment_method_failure_rate", "Failure rate by payment method", "operations",
       "Share of failed transactions per payment method.", ["transactions"],
       ["transactions.payment_method", "transactions.status", "transactions.created_at"],
       "failed / total GROUP BY payment_method", [DAYS],
       "SELECT payment_method, SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS failed_rate "
       "FROM transactions WHERE created_at >= :since AND payment_method IS NOT NULL GROUP BY payment_method",
       "A method with a high failure rate needs provider investigation.",
       "higher_is_worse", ["payment method failures", "failed payments by method"]),
    _t("daily_txn_trend", "Daily transaction trend", "operations",
       "Transactions and USD volume per day.", ["transactions"],
       ["transactions.created_at", "transactions.amount_usd"], "COUNT(*), SUM(amount_usd) GROUP BY DATE(created_at)",
       [DAYS],
       "SELECT DATE(created_at) AS day, COUNT(*) AS txn_count, SUM(amount_usd) AS volume_usd FROM transactions "
       "WHERE created_at >= :since GROUP BY DATE(created_at) ORDER BY day",
       "Use for trend and seasonality; sudden breaks suggest incidents.",
       "neutral", ["transactions per day", "daily volume trend"]),
    _t("high_risk_user_count", "High-risk user count", "risk",
       "Users currently rated HIGH risk.", ["users"], ["users.risk_level"], "COUNT(*) WHERE risk_level='HIGH'", [],
       "SELECT COUNT(*) AS high_risk_users FROM users WHERE risk_level = 'HIGH'",
       "Rising count means growing risk exposure in the customer base.",
       "higher_is_worse", ["risky users", "high risk customers"]),
    _t("kyc_status_distribution", "KYC status distribution", "compliance",
       "Users per KYC status.", ["users"], ["users.kyc_status"], "COUNT(*) GROUP BY kyc_status", [],
       "SELECT kyc_status, COUNT(*) AS user_count FROM users GROUP BY kyc_status",
       "A large PENDING or EXPIRED share is a compliance backlog.",
       "neutral", ["kyc breakdown", "verification status"]),
    _t("pep_user_count", "PEP user count", "compliance",
       "Number of users flagged as politically exposed.", ["users"], ["users.is_pep"], "COUNT(*) WHERE is_pep", [],
       "SELECT COUNT(*) AS pep_users FROM users WHERE is_pep = TRUE",
       "PEPs require enhanced monitoring. Note: is_pep is not indexed, so this scans users.",
       "neutral", ["pep users", "politically exposed users"]),
    _t("restricted_account_count", "Suspended or frozen accounts", "compliance",
       "Accounts in SUSPENDED or FROZEN state.", ["users"], ["users.account_status"],
       "COUNT(*) WHERE account_status IN (...)", [],
       "SELECT account_status, COUNT(*) AS account_count FROM users "
       "WHERE account_status = 'SUSPENDED' OR account_status = 'FROZEN' GROUP BY account_status",
       "Reflects enforcement actions; spikes indicate incidents or policy changes.",
       "neutral", ["frozen accounts", "suspended accounts"]),
    _t("new_users_by_country", "New users by country", "operations",
       "Registrations per country in the window.", ["users"], ["users.country", "users.created_at"],
       "COUNT(*) GROUP BY country", [DAYS],
       "SELECT country, COUNT(*) AS new_users FROM users WHERE created_at >= :since GROUP BY country "
       "ORDER BY new_users DESC",
       "Growth hotspots; sudden single-country surges can be abuse.",
       "higher_is_better", ["signups by country", "new registrations"]),
    _t("failed_login_count", "Failed login count", "security",
       "Failed login attempts in the window.", ["login_events"], ["login_events.status", "login_events.created_at"],
       "COUNT(*) WHERE status='FAILED'", [DAYS],
       "SELECT COUNT(*) AS failed_logins FROM login_events WHERE status = 'FAILED' AND created_at >= :since",
       "Spikes indicate credential stuffing or brute force.",
       "higher_is_worse", ["failed logins", "login failures"]),
    _t("failed_logins_by_ip", "Failed logins by IP", "security",
       "IP addresses with the most failed logins.", ["login_events"],
       ["login_events.ip_address", "login_events.status", "login_events.created_at"],
       "COUNT(*) GROUP BY ip_address WHERE status='FAILED'", [DAYS, LIMIT],
       "SELECT ip_address, COUNT(*) AS failures FROM login_events WHERE status = 'FAILED' "
       "AND created_at >= :since GROUP BY ip_address ORDER BY failures DESC LIMIT :limit",
       "Top IPs are candidates for blocking.",
       "higher_is_worse", ["suspicious ips", "brute force sources"]),
    _t("multi_country_login_users", "Users logging in from multiple countries", "security",
       "Users with logins from more than one country in the window.", ["login_events"],
       ["login_events.user_id", "login_events.country", "login_events.created_at"],
       "COUNT(DISTINCT country) > 1 GROUP BY user_id", [DAYS],
       "SELECT user_id, COUNT(DISTINCT country) AS countries FROM login_events WHERE created_at >= :since "
       "AND user_id IS NOT NULL GROUP BY user_id HAVING COUNT(DISTINCT country) > 1",
       "Impossible-travel signal for account takeover.",
       "higher_is_worse", ["impossible travel", "geo anomalies"]),
    _t("login_status_breakdown", "Login outcomes", "security",
       "Logins by status (SUCCESS, FAILED, BLOCKED, MFA_REQUIRED).", ["login_events"],
       ["login_events.status", "login_events.created_at"], "COUNT(*) GROUP BY status", [DAYS],
       "SELECT status, COUNT(*) AS events FROM login_events WHERE created_at >= :since GROUP BY status",
       "A rising BLOCKED or FAILED share points to attacks.",
       "neutral", ["login success rate", "authentication outcomes"]),
    _t("high_risk_user_failed_logins", "Failed logins of high-risk users", "security",
       "Failed logins by users rated HIGH risk.", ["login_events", "users"],
       ["login_events.status", "login_events.user_id", "login_events.created_at", "users.user_id", "users.risk_level"],
       "COUNT(*) JOIN users WHERE HIGH and FAILED", [DAYS],
       "SELECT COUNT(*) AS failures FROM login_events JOIN users ON login_events.user_id = users.user_id "
       "WHERE login_events.status = 'FAILED' AND users.risk_level = 'HIGH' AND login_events.created_at >= :since",
       "Combines identity risk with authentication failures.",
       "higher_is_worse", ["risky users failed logins"], [LOGIN_USER]),
    _t("open_alerts_by_severity", "Open alerts by severity", "security",
       "Open alerts grouped by severity.", ["alerts"], ["alerts.status", "alerts.severity"],
       "COUNT(*) WHERE status='OPEN' GROUP BY severity", [],
       "SELECT severity, COUNT(*) AS open_alerts FROM alerts WHERE status = 'OPEN' GROUP BY severity",
       "Backlog of unhandled alerts; CRITICAL and HIGH should be near zero.",
       "higher_is_worse", ["open alerts", "alert backlog"]),
    _t("alerts_by_rule", "Alerts by rule", "security",
       "Alert counts per detection rule with rule type.", ["alerts", "alert_rules"],
       ["alerts.rule_id", "alerts.created_at", "alert_rules.rule_id", "alert_rules.rule_name", "alert_rules.rule_type"],
       "COUNT(*) GROUP BY rule", [DAYS],
       "SELECT alert_rules.rule_name, alert_rules.rule_type, COUNT(*) AS alert_count FROM alerts JOIN alert_rules "
       "ON alerts.rule_id = alert_rules.rule_id WHERE alerts.created_at >= :since "
       "GROUP BY alert_rules.rule_name, alert_rules.rule_type ORDER BY alert_count DESC",
       "Noisy rules need tuning; silent rules may be misconfigured.",
       "neutral", ["rule hit counts", "noisiest rules"], [("alerts", "alert_rules", "alerts.rule_id = alert_rules.rule_id")]),
    _t("top_users_by_alerts", "Users with most alerts", "risk",
       "Users ranked by number of alerts raised.", ["alerts", "users"],
       ["alerts.user_id", "alerts.created_at", "users.user_id", "users.full_name", "users.risk_level"],
       "COUNT(*) GROUP BY user", [DAYS, LIMIT],
       "SELECT users.user_id, users.full_name, users.risk_level, COUNT(*) AS alert_count FROM alerts JOIN users "
       "ON alerts.user_id = users.user_id WHERE alerts.created_at >= :since "
       "GROUP BY users.user_id, users.full_name, users.risk_level ORDER BY alert_count DESC LIMIT :limit",
       "Repeat offenders for investigation priority.",
       "higher_is_worse", ["repeat alert users", "most flagged customers"], [ALR_USER]),
    _t("critical_open_alert_count", "Critical open alerts", "security",
       "Open CRITICAL alerts needing immediate action.", ["alerts"], ["alerts.status", "alerts.severity"],
       "COUNT(*) WHERE OPEN AND CRITICAL", [],
       "SELECT COUNT(*) AS critical_open FROM alerts WHERE status = 'OPEN' AND severity = 'CRITICAL'",
       "Should be zero; any value requires immediate response.",
       "higher_is_worse", ["critical alerts", "urgent alerts"]),
    _t("export_actions_count", "Export actions", "compliance",
       "Audit log EXPORT actions in the window.", ["audit_logs"], ["audit_logs.action", "audit_logs.created_at"],
       "COUNT(*) WHERE action='EXPORT'", [DAYS],
       "SELECT COUNT(*) AS exports FROM audit_logs WHERE action = 'EXPORT' AND created_at >= :since",
       "Data exfiltration indicator; review unusual spikes by actor.",
       "higher_is_worse", ["data exports", "export activity"]),
    _t("audit_actions_by_actor", "Audit actions by actor", "compliance",
       "Most active actors in the audit trail.", ["audit_logs"],
       ["audit_logs.actor_id", "audit_logs.created_at"], "COUNT(*) GROUP BY actor_id", [DAYS, LIMIT],
       "SELECT actor_id, COUNT(*) AS actions FROM audit_logs WHERE created_at >= :since "
       "GROUP BY actor_id ORDER BY actions DESC LIMIT :limit",
       "Highlights privileged users and unusual activity levels.",
       "neutral", ["who did most actions", "audit trail activity"]),
    _t("deployed_dashboard_count", "Deployed dashboards", "operations",
       "Dashboards currently deployed.", ["dashboards"], ["dashboards.is_deployed"],
       "COUNT(*) WHERE is_deployed", [],
       "SELECT COUNT(*) AS deployed FROM dashboards WHERE is_deployed = TRUE",
       "Adoption of saved dashboards.", "higher_is_better", ["live dashboards", "deployed dashboards"]),
    _t("favorite_query_count_by_user", "Favorite queries by user", "operations",
       "Favorited saved queries per user.", ["query_history"], ["query_history.user_id", "query_history.is_favorite"],
       "COUNT(*) WHERE is_favorite GROUP BY user_id", [LIMIT],
       "SELECT user_id, COUNT(*) AS favorites FROM query_history WHERE is_favorite = TRUE "
       "GROUP BY user_id ORDER BY favorites DESC LIMIT :limit",
       "Shows repeat-use queries worth turning into dashboards.", "neutral", ["favorite queries"]),
    _t("active_metric_specs_count", "Active alert metrics", "operations",
       "Alert-engine metrics currently triggered.", ["metric_specs"], ["metric_specs.is_active", "metric_specs.severity"],
       "COUNT(*) WHERE is_active=1 GROUP BY severity", [],
       "SELECT severity, COUNT(*) AS active_metrics FROM metric_specs WHERE is_active = 1 GROUP BY severity",
       "Currently firing engine alerts by severity.", "higher_is_worse", ["triggered alert metrics"]),
    _t("active_anomalies", "Active anomalies", "operations",
       "Anomalies with current status active.", ["anomaly_history"],
       ["anomaly_history.current_status", "anomaly_history.severity", "anomaly_history.alert_count"],
       "COUNT(*), SUM(alert_count) WHERE current_status='active'", [],
       "SELECT severity, COUNT(*) AS anomalies, SUM(alert_count) AS total_alerts FROM anomaly_history "
       "WHERE current_status = 'active' GROUP BY severity",
       "Ongoing anomaly episodes; longer-lived ones need escalation.", "higher_is_worse", ["ongoing anomalies"]),
    _t("alert_triggers_by_metric", "Alert triggers per metric", "operations",
       "Trigger counts per alert-engine metric.", ["alert_history", "metric_specs"],
       ["alert_history.metric_id", "alert_history.action", "alert_history.created_at", "metric_specs.metric_id", "metric_specs.name"],
       "COUNT(*) WHERE action='triggered' GROUP BY metric", [DAYS],
       "SELECT metric_specs.name, COUNT(*) AS triggers FROM alert_history JOIN metric_specs "
       "ON alert_history.metric_id = metric_specs.metric_id WHERE alert_history.action = 'triggered' "
       "AND alert_history.created_at >= :since GROUP BY metric_specs.name ORDER BY triggers DESC",
       "Frequent triggers imply noisy thresholds or persistent issues.", "higher_is_worse",
       ["alert engine triggers"], [("alert_history", "metric_specs", "alert_history.metric_id = metric_specs.metric_id")]),
    _t("unprocessed_events_count", "Unprocessed events", "operations",
       "Events not yet processed by the alert engine, by source.", ["events"], ["events.processed", "events.table_name"],
       "COUNT(*) WHERE processed=0 GROUP BY table_name", [],
       "SELECT table_name, COUNT(*) AS pending FROM events WHERE processed = 0 GROUP BY table_name",
       "Backlog means the engine is lagging.", "higher_is_worse", ["event backlog"]),
    _t("daily_summary_view", "Daily summary (view)", "operations",
       "Pre-aggregated daily transaction summary.", ["v_daily_summary"],
       ["v_daily_summary.date", "v_daily_summary.total_txns"], "SELECT FROM v_daily_summary", [LIMIT],
       "SELECT date, total_txns, total_volume, avg_amount, flagged_count, failed_count FROM v_daily_summary "
       "ORDER BY date DESC LIMIT :limit",
       "Fast pre-aggregated trend view.", "neutral", ["daily summary"]),
    _t("flagged_transactions_view", "Flagged transactions with user context (view)", "risk",
       "Flagged transactions joined to user details.", ["v_flagged_transactions"],
       ["v_flagged_transactions.txn_id", "v_flagged_transactions.amount_usd"], "SELECT FROM v_flagged_transactions", [LIMIT],
       "SELECT txn_id, user_id, amount_usd, full_name, user_country, risk_level FROM v_flagged_transactions "
       "ORDER BY amount_usd DESC LIMIT :limit",
       "Triage list of flagged payments with user risk context.", "higher_is_worse", ["flagged transactions list"]),
    _t("open_alerts_view", "Open alerts with context (view)", "security",
       "Open alerts with user and transaction context.", ["v_open_alerts"],
       ["v_open_alerts.alert_id", "v_open_alerts.severity"], "SELECT FROM v_open_alerts", [LIMIT],
       "SELECT alert_id, rule_name, severity, user_id, full_name, txn_amount FROM v_open_alerts LIMIT :limit",
       "Prioritized work queue for analysts.", "higher_is_worse", ["alert work queue"]),
    _t("high_risk_users_view", "High-risk users with exposure (view)", "risk",
       "High-risk users with transaction volume and open alerts.", ["v_high_risk_users"],
       ["v_high_risk_users.user_id", "v_high_risk_users.total_volume"], "SELECT FROM v_high_risk_users", [LIMIT],
       "SELECT user_id, full_name, txn_count, total_volume, alert_count FROM v_high_risk_users "
       "ORDER BY total_volume DESC LIMIT :limit",
       "Highest-exposure risky customers first.", "higher_is_worse", ["risky users exposure"]),
]


def derive_since(days: int, now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge", "Command", "Copy", "Lock", "Into",
    "TruncateTable", "Set", "Use", "Grant", "Pragma", "Transaction", "Commit", "Rollback", "Attach", "Detach",
    "AlterTable") if hasattr(exp, n))
_FORBIDDEN_FUNCS = {"pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
                    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "lo_import", "lo_export",
                    "set_config", "nextval", "setval", "dblink", "dblink_exec", "load_extension", "readfile",
                    "writefile", "sleep", "benchmark", "randomblob", "zeroblob"}


def assert_select_only(sql: str) -> None:
    """Allow exactly one read-only SELECT/UNION. Walks the whole tree, so writable CTEs, SELECT INTO,
    locking clauses and side-effecting functions are rejected, not just non-SELECT roots."""
    try:
        trees = sqlglot.parse(sql)
    except Exception as e:
        raise ValueError(f"unparsable SQL: {e}") from e
    if len(trees) != 1 or trees[0] is None or not isinstance(trees[0], (exp.Select, exp.Union)):
        raise ValueError("only a single SELECT statement is allowed")
    root = trees[0]
    for node in root.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, _FORBIDDEN_NODES) or (isinstance(node, exp.Select) and node.args.get("into")):
            raise ValueError("only a read-only SELECT is allowed")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _FORBIDDEN_FUNCS or name.startswith("dblink"):
                raise ValueError(f"function not allowed: {name}")


def _coerce(spec: Dict[str, Any], value: Any) -> Any:
    typ = spec["type"]
    if spec["name"] == "days":  # default window bounds even if the spec omits them
        spec = {"min": 1, "max": 3650, **spec}
    try:
        if typ == "int":
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            value = int(value)
        elif typ == "float":
            value = float(value)
        elif typ == "str":
            value = str(value)
        else:
            raise ValueError(f"unknown type {typ}")
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"parameter '{spec['name']}' must be {typ}")
    if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
        raise ValueError(f"parameter '{spec['name']}' out of range [{spec.get('min')}, {spec.get('max')}]")
    if "choices" in spec and value not in spec["choices"]:
        raise ValueError(f"parameter '{spec['name']}' must be one of {spec['choices']}")
    return value


def render_template(metric: Dict[str, Any], params: Dict[str, Any] = None, now: datetime = None) -> Tuple[str, Dict[str, Any]]:
    """Validate params against the metric spec; return (sql, bind). Raises ValueError on bad input."""
    params = dict(params or {})
    specs = {p["name"]: p for p in metric["parameters"]}
    unknown = set(params) - set(specs)
    if unknown:
        raise ValueError(f"unknown parameter(s): {sorted(unknown)}")
    bind: Dict[str, Any] = {}
    for name, spec in specs.items():
        bind[name] = _coerce(spec, params.get(name, spec["default"]))
    sql = metric["sql"]
    assert_select_only(sql)
    if ":since" in sql:
        bind["since"] = derive_since(bind["days"], now)
        bind.pop("days", None)
    else:
        bind.pop("days", None)
    return sql, bind


def nl_variants(metric: Dict[str, Any]) -> List[str]:
    name = metric["name"].lower()
    out = [f"show {name}", f"what is the {name}", f"{name} please"]
    for syn in metric.get("synonyms", []):
        out.append(f"show me {syn}")
    if any(p["name"] == "days" for p in metric["parameters"]):
        out += [f"{name} in the last 7 days", f"{name} last 30 days"]
        out += [f"{s} in the last 24 hours" for s in metric.get("synonyms", [])[:1]]
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq
