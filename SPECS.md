# SPECS: pgvector NL2SQL, Metric Catalog, Eval Framework, Reliable Retrieval

## Why
The NL2SQL pipeline (`app/orchestration/workflow.py`) generates SQL with an LLM on every request, with a mock vector store (`app/services/vector_store.py`), no cache, no user isolation, no audit trail and no way to measure SQL quality. This spec moves the system to a parameterized-query-first design backed by pgvector and Redis, adds a computed-metric catalog as the semantic base, and adds an eval framework with a dashboard so retrieval and generation quality are measurable and regressions are caught. Failures, user ratings and Sentinel insights get bounded retries, deterministic fallbacks, human escalation and backend alerts.

## What
- Migrate business data (existing schema only) to PostgreSQL; add pgvector for schema, metric and value-profile embeddings (sentence-transformers, 384 dims).
- Redis cache layer for repeated prompts and SQL results, plus embedding cache.
- Metric catalog derived from the existing tables, indexes, joins and views, with interpretation text and linked parameterized SQL templates, stored as vector metadata at table, column and value-profile ("row") level.
- Golden dataset generated from the catalog, regression suite, eval metrics (retrieval precision/recall, generation faithfulness/relevancy, SQL execution accuracy) and an eval dashboard.
- Retrieval upgrades: small-model intent classification, multi-query and hybrid (vector + BM25) search, contextual compression for join tables, answer-aware reranking, feature-boosted reranking by table weight.
- Parameterized-first answering; free-form SQL generation only after user dissatisfaction AND role `super`.
- JWT identity, per-user session isolation, stored chat context, timestamped insights, separate audit-log DB.
- Retry policy: max 3 attempts, stronger model on attempt 2+, then deterministic fallback at >= 0.80 semantic similarity to an existing metric, else error message.
- Strict one-shot structured parsing of LLM output; unparsable output raises a backend alert.
- Same reliability stack for Sentinel ("SentinelIQ"); false ratings trigger human chat; more than 3 false ratings flag an anomaly and light up an "Insight Anomaly Detection" button.

## Existing schema inventory (no new business tables)
Source: `app/files/derivinsight_schema.sql`, `app/files/alerts_schema.sql`.

| Table / View | Indexed columns | Joins (FK) | Notes |
|---|---|---|---|
| users | country, kyc_status, risk_level, account_status, created_at, PK user_id, UNIQUE email | root | dimension |
| transactions | user_id, status, txn_type, amount_usd, created_at, (user_id, created_at), instrument | user_id -> users | largest fact table |
| login_events | user_id, status, ip_address, country, created_at | user_id -> users (nullable) | fact |
| alert_rules | PK rule_id, UNIQUE rule_name | none | config, small |
| alerts | user_id, severity, status, rule_name, created_at | user_id -> users, rule_id -> alert_rules, txn_id -> transactions, assigned_to -> users | fact |
| audit_logs | actor_id, action, created_at | actor_id -> users | business audit, distinct from new audit DB |
| dashboards | owner_id, is_deployed | owner_id -> users | note duplicate definition in alerts_schema.sql |
| query_history | PK query_id (no secondary index) | user_id -> users | |
| events | table_name, created_at, (table_name, created_at), processed | none | alerts engine |
| metric_specs | table_name, is_active | none | alerts engine |
| alert_history | metric_id, created_at | metric_id -> metric_specs | |
| anomaly_history | current_status, updated_at | metric_id -> metric_specs | |
| metric_windows | metric_id, event_timestamp, (metric_id, event_timestamp) | metric_id -> metric_specs | |
| v_high_risk_users, v_flagged_transactions, v_open_alerts, v_daily_summary | views over above | | fastest pre-joined paths |

Fast-query rule used by the catalog: a query is "fast" if its filters hit an indexed column (or the composite `(user_id, created_at)` / `(table_name, created_at)` / `(metric_id, event_timestamp)`) and joins follow an indexed FK. `EXPLAIN` output is recorded per template.

### Proposed table weights for feature-boosted reranking (please adjust)
| Table | Proposed weight | Reason |
|---|---|---|
| transactions | 1.0 | most queried fact, most metrics |
| alerts | 0.95 | core Sentinel/compliance output |
| users | 0.9 | join hub for nearly every metric |
| login_events | 0.8 | security domain |
| v_flagged_transactions / v_open_alerts / v_daily_summary / v_high_risk_users | 0.75 | fast pre-joined views |
| alert_rules | 0.5 | small config table |
| anomaly_history / alert_history | 0.5 | alerts engine reporting |
| metric_specs / events / metric_windows | 0.35 | engine internals |
| audit_logs | 0.4 | compliance lookups |
| dashboards / query_history | 0.25 | app metadata |

Weights live in a config file (`app/data/table_weights.json`) and multiply the rerank score; final values come from the user.

## Constraints

### Must
- Use only the existing business schema; derive all metrics from it.
- Business data lives in PostgreSQL (migrated); vectors in pgvector; `nl2sql_meta` DB holds chats, insights, ratings, eval results; `nl2sql_audit` is a separate DB.
- Every vector row carries metadata: `user_id` (owner or `NULL` for global catalog), `session_id`, `level` (table|column|value|metric|template), `table`, `indexed`, `joins`, `fast`, `interpretation`, `created_at`. Every retrieval filters by `user_id`.
- Parameterized templates are tried first; only on explicit dissatisfaction plus `role == super` may the LLM generate free-form SQL, which still passes `validation_module` (SELECT only) and sqlglot checks.
- Cache key = hash(normalized query + user_id + role + catalog version); cache hit returns SQL result without LLM calls; invalidated on catalog version change and by TTL.
- Retry policy: max 3 attempts; attempt 1 small model, attempts 2-3 stronger model (configurable); after 3 failures, deterministic path: metric with cosine similarity >= 0.80 to the query runs its template; otherwise return error message.
- LLM output parsed once against a pydantic schema; parse failure raises `UnparsableLLMResponse` and emits a backend alert (log + Slack via `slack_notifier`), no silent repair loops.
- Insights stored with timestamp, user_id, session_id, source (chat|sentinel), rating.
- Sentinel gets the same pipeline: retry policy, parsing, caching, audit.
- Eval metrics: retrieval precision@k, recall@k, MRR; generation faithfulness, answer relevancy, execution accuracy; shown on a dashboard with run history.
- Small models for intent classification (`INTENT_MODEL`), stronger model names configurable in `config.py`.

### Must Not
- Create new business tables, columns, or indexes on existing tables.
- Return another user's data, vectors, chats or insights under any path (including cache).
- Run LLM-generated SQL for non-super roles, or before a parameterized attempt failed and the user marked dissatisfaction.
- Log secrets, JWTs or full result sets in the audit DB.
- Retry more than 3 times per request.

### Out of Scope
- Changing the business schema, adding new alert rules.
- Fine-tuning models or training embeddings.
- Full RBAC admin UI; user signup/login UI (JWT issued externally or by a stub issuer for tests).
- Real-time streaming of results.
- Human-chat product (agent console); only the handoff state, endpoint and notification are built.

## Test cases
| slug | name | input | expected | kind |
|---|---|---|---|---|
| vector-upsert | Upsert schema embeddings | catalog build run | rows exist for every table, column, view with metadata keys | happy |
| vector-isolation | User A cannot see User B vectors | search as user A for B-owned doc | zero results from B | error |
| cache-hit | Repeated prompt served from cache | same query twice, same user | second call has no LLM call, `cache_hit=true`, identical rows | happy |
| cache-cross-user | Same prompt, different user | query as user A then B | B misses cache, gets own scoped data | edge |
| cache-invalidate | Catalog version bump | bump version, repeat query | cache miss | edge |
| metric-catalog | Metric generation | catalog build | each metric has formula, tables, joins, indexed flag, interpretation, template SQL | happy |
| metric-fast-flag | Fast query tagging | template using idx_txn_user_created | `fast=true`; template with unindexed filter on `users.email LIKE '%x'` `fast=false` | happy |
| param-first | Parameterized answer | "flagged transactions last 7 days" | template SQL with bound params, no LLM SQL generation | happy |
| gen-blocked-user | Non-super asks regenerate | role=user, dissatisfied | 403 `generation_requires_super` | error |
| gen-allowed-super | Super asks regenerate | role=super, dissatisfied after param miss | LLM SQL generated, validated, executed | happy |
| gen-before-miss | Super skips parameterized | role=super, no prior miss/dissatisfaction | parameterized attempted first | edge |
| intent-small-model | Intent classification | any query | uses `INTENT_MODEL`, latency recorded | happy |
| hybrid-search | Exact identifier query | "RULE-007 structuring" | BM25 contributes, rule ranked top 3 | happy |
| multi-query | Complex query | 3-part question | >=2 sub-queries retrieved, results merged and deduped | happy |
| join-compress | Join context compression | query needing users+transactions+alerts | prompt contains compressed join context under token budget | happy |
| answer-rerank | Answer-aware rerank | top-k candidates | each scored against pre-generated answer, order changes vs raw vector rank | happy |
| feature-boost | Table weights | equal-similarity candidates | higher-weight table ranks first | happy |
| retry-model-escalation | Retry uses stronger model | attempt 1 fails | attempt 2 uses `RETRY_MODEL`, attempts <= 3 | happy |
| retry-exhausted-det | Deterministic fallback | 3 failures, metric sim 0.86 | metric template result returned, `fallback=deterministic` | edge |
| retry-exhausted-err | No metric match | 3 failures, best sim 0.72 | error message, no result | error |
| parse-fail-alert | Unparsable LLM output | LLM returns prose | `UnparsableLLMResponse`, backend alert emitted, no second parse attempt | error |
| audit-write | Audit trail | any query | row in `nl2sql_audit` with user_id, session_id, sql hash, outcome, timestamp | happy |
| chat-context | Chat persistence | 2 turns in a session | turn 2 receives turn 1 context; stored with timestamps | happy |
| session-isolation | Session id reuse | user B sends user A's session_id | 403 | error |
| insight-store | Insight saved | successful insight | row with timestamp, user_id, source | happy |
| sentinel-rating-false | False rating | rate insight false | status `human_review`, chat thread opened | happy |
| sentinel-anomaly-flag | More than 3 false ratings | 4 false ratings | anomaly flag set, button state `flagged` | edge |
| sentinel-three-false | Exactly 3 false ratings | 3 false | not flagged | edge |
| eval-run | Regression suite | run on golden set | precision, recall, faithfulness, relevancy persisted per run | happy |
| eval-regression-gate | Metric drop | run with precision drop > threshold | suite fails, run marked regressed | error |
| eval-dashboard | Dashboard data | GET metrics | time series for 4 metrics with run ids | happy |
| golden-provenance | Golden items traceable | build golden set | every item links to metric id and expected SQL | happy |

## Edge cases
- Empty or whitespace-only query.
- Query matching two metrics at nearly equal similarity (tie-break by table weight then fast flag).
- Redis unavailable: fall through to pipeline, no error to user.
- pgvector unavailable: return error; no unfiltered fallback that skips user filter.
- Cached result stale after underlying data change (TTL and `no_cache` flag).
- Very large result sets (row cap, truncated flag).
- Query referencing nullable FK data (`login_events.user_id` NULL, `alerts.user_id` NULL system alerts).
- Duplicate `dashboards` table definition across the two schema files.
- User dissatisfaction signaled but cached answer would be reserved; cache bypassed on regenerate.
- Similarity exactly 0.80 (counts as match).
- LLM returns valid JSON with wrong schema (treated as unparsable).
- Concurrent identical prompts (single-flight to avoid cache stampede).
- Rating submitted twice by same user for same insight (idempotent, counted once).
- Anomaly counting window and per-user vs global scope (counted per insight source, configurable window).
- Human chat requested while no human online (queued, user notified).
- Super role downgrade mid-session (role re-read from JWT each request).

## Performance
- Cache hit: p95 < 50 ms.
- Parameterized path, cache miss: p95 < 800 ms (embed query ~20 ms on CPU, pgvector HNSW top-k ~10 ms at <= 100k vectors, SQL execution on indexed path).
- LLM path (rerank, answer-aware, generation): p95 < 6 s; max 3 attempts bounds worst case to ~3 LLM calls.
- Vector search: HNSW index, O(log n) per query; catalog size expected < 50k vectors; value-profile rows capped at 200 distinct values per column.
- Hybrid search: BM25 via Postgres `tsvector` + GIN, merged with reciprocal rank fusion, k <= 50 candidates before rerank.
- Answer-aware rerank: one answer generated per top-k (k <= 10), batched in parallel.
- Eval full regression run: golden set <= 500 items, completes < 10 min with parallelism 8.
- Audit writes async, non-blocking to the request path.

## Tasks

### T1 - Postgres, pgvector and Redis foundation
Slug: vector-cache-foundation
Files: app/services/database.py, app/services/vector_store.py, app/services/embeddings.py, app/services/cache.py, app/core/config.py, app/files/migrate_to_postgres.py, requirements.txt
Description: Migrate existing business schema and data to PostgreSQL, implement a real pgvector store with user-scoped metadata filtering, a sentence-transformers embedder, and a Redis cache (prompt to result, single-flight, TTL, version key).
Acceptance: Existing queries run on Postgres with identical results; vector upsert/search filters by user_id; repeating a prompt returns `cache_hit=true` with no LLM call; Redis down does not break requests.

### T2 - Schema metadata and metric catalog
Slug: metric-catalog
Files: app/modules/catalog/schema_profiler.py, app/modules/catalog/metric_generator.py, app/modules/catalog/templates.py, app/data/table_weights.json, app/data/metric_catalog.json
Description: Profile existing tables, indexes, FKs and views (including EXPLAIN-based fast-query tagging and value profiles), generate computable metrics with formulas and interpretations, link each to parameterized SQL templates, and index them in pgvector at table, column and value level.
Acceptance: Catalog JSON lists every metric with tables, joins, indexed/fast flags, interpretation and template; pgvector contains table, column, value and metric rows with the full metadata keys; no DDL runs against business tables.

### T3 - Golden dataset and eval framework with dashboard
Slug: eval-framework
Files: app/eval/golden_builder.py, app/eval/metrics.py, app/eval/runner.py, app/eval/judge.py, app/api/eval_endpoints.py, tests/eval/test_regression.py, frontend-react/src/components/EvalDashboard.tsx
Description: Generate a production-like golden dataset from the catalog (NL variants, expected SQL, expected tables/metrics), build a regression suite scoring retrieval (precision@k, recall@k, MRR) and generation (faithfulness, answer relevancy, execution accuracy), persist runs, and expose them on a dashboard with regression gating.
Acceptance: `pytest tests/eval` runs the suite and persists a run; dashboard shows precision, recall, faithfulness and relevancy over runs; a metric drop beyond threshold fails the suite.

### T4 - Advanced retrieval and reranking
Slug: retrieval-pipeline
Files: app/modules/retrieval/intent_small.py, app/modules/retrieval/multi_query.py, app/modules/retrieval/hybrid.py, app/modules/retrieval/compression.py, app/modules/retrieval/rerank.py, app/modules/preprocessing/service.py, app/orchestration/workflow.py
Description: Small-model intent and complexity classification routing complex queries to multi-query and hybrid search, contextual compression of join-table context, answer-aware reranking (pre-generated answers per metric, rerank top-k against them), and table-weight feature boosting.
Acceptance: Retrieval precision/recall on the golden set (T3) improves versus the T1 baseline; complex queries produce multiple sub-queries; reranked order reflects answer-aware and table-weight scores.

### T5 - Parameterized-first answering with gated generation
Slug: param-first-query
Files: app/modules/sql_generation.py, app/modules/param_resolver.py, app/orchestration/workflow.py, app/api/endpoints.py, app/models/state.py
Description: Resolve queries to catalog templates with bound parameters first; add a dissatisfaction signal in the API and allow free-form LLM SQL generation only after a parameterized miss plus dissatisfaction and role `super`.
Acceptance: Default path never calls LLM SQL generation; non-super receives 403 on regenerate; super regenerate produces validated SQL; cache is bypassed on regenerate.

### T6 - Identity, sessions, chat context, insights and audit DB
Slug: session-audit-store
Files: app/core/auth.py, app/services/session_store.py, app/services/insight_store.py, app/services/audit_db.py, app/api/endpoints.py, app/api/sentinel.py, app/core/config.py
Description: JWT identity with roles, isolated sessions keyed by user_id, persisted chat context, timestamped insight storage, and an async writer to the separate `nl2sql_audit` database.
Acceptance: A session id belonging to another user returns 403; chat turns and insights are persisted with timestamps; each request yields an audit row in `nl2sql_audit`; vector queries always carry the user filter.

### T7 - Retry policy, deterministic fallback and strict parsing
Slug: failure-handling
Files: app/services/llm.py, app/services/retry_policy.py, app/services/deterministic_fallback.py, app/services/backend_alerts.py, app/orchestration/workflow.py, app/modules/intent_classification.py
Description: Bounded retry (max 3) with stronger model from attempt 2, deterministic metric fallback at >= 0.80 similarity else error message, and single-shot pydantic parsing that raises and alerts on unparsable LLM output.
Acceptance: Failures stop at 3 attempts; model escalates on attempt 2; fallback returns template result at similarity 0.80+ and error below; unparsable output emits a backend alert once and is not re-parsed.

### T8 - SentinelIQ ratings, human handoff and insight anomaly detection
Slug: sentinel-feedback
Files: app/orchestration/sentinel_agent.py, app/api/sentinel.py, app/services/insight_store.py, app/services/human_handoff.py, frontend-react/src/components/SentinelDashboard.tsx, frontend-react/src/components/ChatInterface.tsx
Description: Route Sentinel through the T5-T7 stack, add true/false insight ratings, open a human chat on false rating, flag when more than 3 false ratings occur, and add an "Insight Anomaly Detection" button reflecting the flag.
Acceptance: A false rating opens a human review thread; the 4th false rating sets the anomaly flag and the button shows flagged state; exactly 3 does not flag; duplicate ratings by one user count once.
