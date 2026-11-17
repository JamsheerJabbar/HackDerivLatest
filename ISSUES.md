<!-- Format: ### ISS-<n> [OPEN|CORRECTED|FIXED] [<task>] [CODE|TEST|CONSTRAINT|OPTIMIZATION|TEST_GAP|CONVENTION] [BLOCKER|MAJOR|MINOR] <title>; then Found by / Detail / Spec ref / Resolution -->

### ISS-1 [FIXED] [T1] [CODE] [BLOCKER] Migration double-encodes JSON columns
Found by: reviewer
Detail: SQLite string values for JSON columns were re-encoded by SQLAlchemy JSON on Postgres.
Spec ref: T1 Acceptance (identical results)
Resolution: json.loads before insert in copy_data; verified live on Postgres 14 (alert_rules.condition is a JSON object).

### ISS-2 [FIXED] [T1] [CODE] [BLOCKER] Sequences not reset after explicit-id copy
Found by: reviewer
Detail: Next INSERT into serial tables collided with copied or schema-seeded ids.
Spec ref: T1 Acceptance (existing queries run on Postgres)
Resolution: _reset_sequence for all target tables after copy; verified live (next metric_specs id = 6).

### ISS-3 [FIXED] [T1] [CONSTRAINT] [BLOCKER] Live SQLite DB schema differs from derivinsight_schema.sql
Found by: task run (migration against derivinsightnew.db)
Detail: derivinsightnew.db has users(username, age, ...) without email/full_name/country/phone/dob, login_events.country VARCHAR(100), account_status 'INACTIVE', user_id values like '1'; spec schema file requires NOT NULL email/full_name/country. Data copy fails with NotNullViolation. Only users, transactions, login_events exist in the SQLite file (no alerts, alert_rules, etc.). actual_schema*.json also reflects the live shape. "Existing schema" is ambiguous. Options: (a) treat the live DB shape as the schema of record and migrate that, (b) treat the .sql file as the schema of record and regenerate data with app/files/generate_mock_data.py, (c) support both with a column-mapping layer. Also affects T2 metric catalog.
Spec ref: Constraints Must (existing business schema); T1 Acceptance
Resolution: option (c) implemented: column-mapping layer (app/core/schema_mapping.py + app/data/schema_mappings.json), mirror migration of legacy shapes, canonical target refused for a legacy source. Confirmed by review 2026-11-18. Caveats: ISS-95 (forced canonical copy) and ISS-106 (enum vocabulary).

### ISS-4 [FIXED] [T1] [CONSTRAINT] [BLOCKER] pgvector not verified live
Found by: task run
Detail: pgvector extension is not installed for local Postgres 14 and building it from GitHub was denied by the permission classifier. PgVectorBackend SQL (hnsw index, <=>, CAST AS vector) is untested; only InMemoryBackend is tested. Options: user installs pgvector (e.g. brew install pgvector for a supported Postgres, or build v0.8.0), or approves the build; then run a live check.
Spec ref: T1 Must (pgvector), Acceptance
Resolution: verified live on PostgreSQL 17 + pgvector 0.8.7: HNSW index used, retrieval parity with the in-memory backend on the golden set (recall@5 0.993, MRR 0.954), 7 live tests in tests/integration pass with NL2SQL_TEST_PG set. Review 2026-11-18 confirmed the SQL by reading; it could not re-run the live tests.

### ISS-5 [FIXED] [T1] [CONSTRAINT] [MAJOR] cache_hit=true flag and request wiring cannot be met in T1 files
Found by: reviewer
Detail: single_flight returns a bare value with no hit indicator and the cache is not wired into the workflow/API (outside T1 Files). Plan: add (value, hit) API and wire in T5/T6.
Spec ref: T1 Acceptance; test case cache-hit
Resolution: get_or_compute (value, hit) added and wired into answer_query in T5 (parameterized path); verified by cache-hit rows.

### ISS-6 [FIXED] [T1] [CODE] [MAJOR] Vector metadata not validated on add_documents (isolation fails open)
Found by: reviewer
Detail: doc without user_id key is stored as global and visible to all users; required metadata keys never checked (vector_store.py add_documents).
Spec ref: Must (metadata keys), Must Not (cross-user data)
Resolution: fixed in T6 (see ISS-71): add_documents requires a 'user_id' metadata key.

### ISS-7 [OPEN] [T1] [CODE] [MAJOR] Migration copy loads whole tables; silent column drops; weak MISMATCH check
Found by: reviewer
Detail: rows list unbounded for transactions; source columns missing in target are dropped silently; check is tgt>=src.
Spec ref: Performance; I/O contract
Resolution: open.
Review 2026-11-18: PARTIAL. Streaming batches and a count/sum verify exist; silent column drops remain in canonical mode (ISS-95) and verify() skips columns on error (ISS-97).

### ISS-8 [OPEN] [T1] [OPTIMIZATION] [MAJOR] Global cache_service retries Redis on every call when down
Found by: reviewer
Detail: backoff_seconds=0.0 default, up to ~1s timeout per get/set; threatens p95 and "Redis down does not break requests".
Spec ref: Performance; Edge: Redis unavailable
Resolution: open (set nonzero backoff default).
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-9 [FIXED] [T1] [OPTIMIZATION] [MAJOR] HNSW filtered search can under-return
Found by: reviewer
Detail: filter applied after index scan (ef_search 40); no hnsw.iterative_scan, ef_search tuning or partial index.
Spec ref: Performance; retrieval recall
Resolution: hnsw.iterative_scan=relaxed_order and ef_search per query (vector_store.py), live selective-user test passes. Residual caveats in ISS-101. Confirmed by review 2026-11-18.

### ISS-10 [OPEN] [T1] [CODE] [MAJOR] Silent fallback to hash embedder on model load failure
Found by: reviewer
Detail: mixes embedding spaces in the same table; should be explicit (EMBEDDING_BACKEND=hash) or fail in production.
Spec ref: Must (sentence-transformers 384 dims)
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-11 [OPEN] [T1] [TEST_GAP] [MAJOR] No offline tests for PgVectorBackend, migration, DatabaseService on Postgres
Found by: reviewer
Detail: translate_to_postgres is pure and testable; a SQLite-to-SQLite/mocked copy test would have caught ISS-1/ISS-2. Live migration was run manually against local Postgres 14 using a source built from the spec schema (not committed as a test).
Spec ref: T1 Acceptance
Resolution: open.
Review 2026-11-18: PARTIAL. Offline tests now cover translate/analyze/convert/mapping and live tests cover Postgres paths; still none offline for PgVectorBackend, verify(), copy (ISS-105).

### ISS-12 [OPEN] [T1] [CONSTRAINT] [MINOR] LLM prompts and dashboard endpoints assume SQLite
Found by: reviewer
Detail: app/alert_system/service.py:40 says "Use SQLite syntax"; app/api/dashboard_endpoints.py uses sqlite3 directly. Outside T1 files.
Spec ref: T1 Acceptance (existing queries identical on Postgres)
Resolution: assign to a later task.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-13 [OPEN] [T1] [CODE] [MINOR] Misc robustness (DDL split on ';', _ensure race, per-row upsert, embedding load race, zero-vector on empty query, cached None is a miss, execute() swallows errors)
Found by: reviewer
Detail: see REVIEW.md for locations.
Spec ref: Edge cases (empty query, Redis unavailable, large results)
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-14 [OPEN] [T1] [CONVENTION] [MINOR] Test deps in requirements.txt; unused pgvector package; redundant os.getenv; VECTOR_BACKEND defaults to memory
Found by: reviewer
Detail: see REVIEW.md.
Spec ref: conventions
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-15 [OPEN] [T1] [TEST_GAP] [MINOR] Test file quality
Found by: reviewer
Detail: bare globals() runner lookup, real-time sleep in TTL test, isolation test does not assert B's doc was inserted.
Spec ref: Test cases
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-16 [FIXED] [T1] [TEST] [MINOR] Pre-existing failures in tests/test_services.py
Found by: task run
Detail: test_llm_service_retry_logic and test_llm_service_safety_block fail with AttributeError; identical failures on the baseline without T1 changes.
Spec ref: n/a (pre-existing)
Resolution: tests/conftest.py now sets a dummy GEMINI_API_KEY (T5); both tests pass (environment fix, see ISS-59).

### ISS-17 [FIXED] [T1] [CODE] [MINOR] Unguarded json.loads aborts migration on non-JSON legacy strings
Found by: reviewer (re-review)
Detail: migrate_to_postgres.py copy_data conv(): JSONDecodeError aborts the table copy; fall back to raw value or log and skip.
Spec ref: Edge cases
Resolution: migrate_to_postgres.convert keeps the raw value for non-JSON text (tests/test_migration.py). Confirmed by review 2026-11-18.

### ISS-18 [OPEN] [T1] [TEST_GAP] [MINOR] No regression tests for JSON decode and sequence reset fixes (ISS-1, ISS-2)
Found by: reviewer (re-review)
Detail: only manual live Postgres checks exist.
Spec ref: Tests
Resolution: open.
Review 2026-11-18: PARTIAL. JSON decode covered (tests/test_migration.py); sequence reset still has no test.

### ISS-19 [OPEN] [T2] [CONSTRAINT] [BLOCKER] Catalog built from .sql schema files only; live DB shape differs (ISS-3)
Found by: reviewer
Detail: Assumed derivinsight_schema.sql and alerts_schema.sql are the schema of record (option b of ISS-3). Live SQLite lacks users.email/full_name/country and the alerts, alert_rules, audit_logs tables, so metrics such as flagged_txn_by_country and the alerts_* metrics would fail on the live data. Resolves with the ISS-3 decision.
Spec ref: Constraints Must (existing business schema)
Resolution: pending user decision on ISS-3.
Review 2026-11-18: PARTIAL. Availability is now computed per physical database (43/83 on the live file, all execute, 40 carry reasons) but it is NOT safe to close: several 'available' metrics return silently wrong results on the live vocabulary (ISS-106).

### ISS-20 [OPEN] [T2] [CONSTRAINT] [MAJOR] `fast` counts full index scans as fast
Found by: reviewer
Detail: Spec-literal rule (plan uses any index) marks 66/83 metrics fast, including 15 index_scan metrics (unbounded GROUP BY). `access` field (seek/index_scan/full_scan) exists but nothing downstream is required to read it. Options: tie-break on access == seek in T4/T7, or redefine fast as seek only.
Spec ref: T2 fast tagging; Edge cases tie-break
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-21 [OPEN] [T2] [CONSTRAINT] [MAJOR] fast flag derived from SQLite planner on an empty sandbox, not PostgreSQL
Found by: reviewer
Detail: Postgres may seq-scan where SQLite picks an index; SQL also uses DATE(created_at) and ISO string :since. Options: re-EXPLAIN against a seeded Postgres in CI (needs ISS-3/ISS-4 resolved).
Spec ref: T2 description; Must (Postgres)
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-22 [OPEN] [T2] [CODE] [MAJOR] View metrics have indexed=False but fast=True
Found by: reviewer
Detail: view columns are marked indexed=False; flagged_transactions_view, open_alerts_view, high_risk_users_view contradict themselves, also in vector metadata.
Spec ref: T2 Acceptance
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-23 [OPEN] [T2] [CODE] [MAJOR] build_sandbox swallows all sqlite3.Error
Found by: reviewer
Detail: failed translations silently drop tables/indexes; ';' split breaks on semicolons in strings; no object-count assertions. Swallow should be limited to the duplicate dashboards case.
Spec ref: Edge cases
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-24 [OPEN] [T2] [CODE] [MAJOR] render_template raises KeyError when :since used without a declared `days` param
Found by: reviewer
Detail: contract says ValueError; T5 will call it with resolved metrics.
Spec ref: I/O contract
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-25 [OPEN] [T2] [CODE] [MINOR] Misc T2 code findings
Found by: reviewer
Detail: assert_select_only uses default dialect; index_catalog never deletes stale rows of older catalog versions; value rows come only from schema-file enum comments (not data) and truncate silently at 200; metric rows use tables[0] and joins lack ON conditions; plans executed twice and two sandboxes built; write_catalog not atomic.
Spec ref: Performance; T2 description
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-26 [OPEN] [T2] [TEST_GAP] [MINOR] T2 test gaps
Found by: reviewer
Detail: fast-flag test compares against the implementation's own function (tautological); no pinned fast/access expectations; no committed-JSON freshness test; KeyError path, choices/max bounds, idempotent re-index, view indexed consistency, nullable FK metrics untested; test docstring still says planned interfaces.
Spec ref: Test cases
Resolution: open.
Review 2026-11-18: STILL OPEN (verified against current code).

### ISS-27 [OPEN] [T2] [CONVENTION] [MINOR] Weights/regeneration conventions
Found by: reviewer
Detail: no test that every profiled table has a weight (silent 0.5 default); generated_at makes committed metric_catalog.json differ on every regeneration.
Spec ref: conventions
Resolution: open.
Review 2026-11-18: PARTIAL. Weights coverage is complete today; the silent 0.5 fallback and generated_at churn remain (ISS-112).

### ISS-28 [CORRECTED] [T2] [TEST] [MINOR] profile-indexes-fast-filter expected created_at to be non-fast
Found by: task run
Detail: transactions.created_at has its own index idx_txn_created_at, so fast_filter=True is correct.
Spec ref: SPECS.md schema inventory (transactions indexes)
Resolution: test-designer CORRECT mode changed fast_filter_false to query_history.user_id.

### ISS-29 [FIXED] [T3] [CODE] [BLOCKER] Regression suite did not persist a dashboard-visible run or gate on the drop threshold
Found by: reviewer
Detail: tests/eval persisted only to a tmp store, never asserted run["regressed"], silently rewrote a missing baseline file.
Spec ref: T3 Acceptance; test case eval-regression-gate
Resolution: suite uses EvalStore() (configured store), asserts not regressed, fails when floors file is missing unless UPDATE_EVAL_BASELINE=1; verified runs persisted.

### ISS-30 [OPEN] [T3] [CODE] [MAJOR] Baseline selection ratchets and ignores k / dataset version
Found by: reviewer
Detail: latest_baseline is the latest non-regressed run regardless of k, n_items, label or golden catalog_version; repeated 0.04 drops each pass and lower the baseline.
Spec ref: T3 regression gating
Resolution: open.

### ISS-31 [OPEN] [T3] [CODE] [MAJOR] POST /eval/run unauthenticated, blocking, unguarded
Found by: reviewer
Detail: builds the pipeline per request, runs sync sqlite/embedding work on the event loop, holds the HTTP request for the whole run, no concurrency guard. Auth arrives with T6; needs background job + lock.
Spec ref: Performance; T6 auth
Resolution: open.

### ISS-32 [OPEN] [T3] [CODE] [MAJOR] Retrieval runs twice per item; shared sqlite connection gives no real parallelism
Found by: reviewer
Detail: _eval_item and baseline generator each search; Semaphore(8) is ineffective for sandbox work.
Spec ref: Performance (parallelism 8)
Resolution: open.

### ISS-33 [OPEN] [T3] [CODE] [MAJOR] Faithfulness judge weak: no-number answers score 1.0, "(0 rows)" scores 0.0, LLM judge unwired
Found by: reviewer
Detail: score is near tautological for the baseline; llm_judge never used by the runner.
Spec ref: faithfulness metric validity
Resolution: open.

### ISS-34 [OPEN] [T3] [CODE] [MAJOR] Item errors only counted as `failures`, not gated
Found by: reviewer
Detail: a run with all items failing is saved as normal; check_regression ignores failures.
Spec ref: regression gating
Resolution: open.

### ISS-35 [OPEN] [T3] [TEST_GAP] [MAJOR] Floors are loose and tied to the hash embedder
Found by: reviewer
Detail: precision 0.149, relevancy 0.229 (measured minus 0.05); relevancy via hash embeddings is not a meaningful quality signal.
Spec ref: dashboard shows meaningful precision/relevancy
Resolution: open; re-baseline with real model/pipeline after T4/T5.

### ISS-36 [OPEN] [T3] [CODE] [MAJOR] Golden set leaks catalog wording and execution accuracy is circular
Found by: reviewer
Detail: questions derive from the same names/synonyms the retriever indexes; expected rows and generated rows both come from the same seeded sandbox; 15/290 items have empty expected rows; multi-metric items scored on primary metric only. Needs held-out phrasing from real query_history/prod logs and Postgres-backed expected results.
Spec ref: golden dataset resembles production; golden-provenance
Resolution: open.

### ISS-37 [OPEN] [T3] [CONSTRAINT] [MAJOR] Eval store defaults to local sqlite file, not nl2sql_meta Postgres
Found by: reviewer
Detail: EVAL_DATABASE_URL defaults to sqlite:///./eval_runs.db; tables created via create_all, no migration. Needs the Postgres meta DB (see ISS-4).
Spec ref: Must (nl2sql_meta holds eval results)
Resolution: open; set EVAL_DATABASE_URL to the meta Postgres in deployment.

### ISS-38 [OPEN] [T3] [CODE] [MINOR] Misc T3 code findings
Found by: reviewer
Detail: check_regression skips mrr/execution_accuracy and missing keys, absolute drops only; golden fixture overwrites tracked app/data/golden_dataset.json on catalog drift; dashboard hardcodes localhost:8080, no error handling or polling; App 'Back' always goes to sentinel and the mode comment is stale; list_runs/metric_series unbounded, eval_items stored as JSON blobs, no retention.
Spec ref: conventions; Performance
Resolution: open.

### ISS-39 [OPEN] [T3] [TEST_GAP] [MINOR] No router-level /eval endpoint test with gate trip; execution_accuracy edge cases unpinned
Found by: reviewer
Detail: concurrent runs, baseline ratchet, column order/None vs 0/bool vs int normalization untested.
Spec ref: Test cases eval-run, eval-dashboard
Resolution: open.

### ISS-40 [OPEN] [T3] [CONVENTION] [MINOR] frontend `tsc` fails project-wide; ChatInterface toast.info type error pre-exists
Found by: task run
Detail: `npx tsc --noEmit` uses a tsconfig without include/jsx per project config; checked EvalDashboard/App with explicit flags: clean apart from pre-existing ChatInterface.tsx:66.
Spec ref: n/a
Resolution: open.

### ISS-41 [OPEN] [T3] [CODE] [MAJOR] Regression assert runs before UPDATE_EVAL_BASELINE branch
Found by: reviewer (re-review)
Detail: after an intentional quality change the suite fails before floors can be rewritten, and latest_baseline stays the old high run, so UPDATE_EVAL_BASELINE=1 cannot recover without editing the eval DB. Move the update branch above the assert or skip the drop gate when updating.
Spec ref: T3 Acceptance (usable regression gate)
Resolution: open.

### ISS-42 [FIXED] [T4] [CODE] [BLOCKER] Shared mutable user_id on AdvancedRetrievalService
Found by: reviewer
Detail: self._user_id was set in retrieve and read after awaits; concurrent calls for different users could search with each other's user_id.
Spec ref: Must Not (another user's vectors); Must (every retrieval filters by user_id)
Resolution: user_id passed explicitly via per-call HybridRetriever closure; verified with a concurrent A/B/A/B run (no leak) and 98 tests.

### ISS-43 [OPEN] [T4] [CODE] [MAJOR] user_id never reaches the advanced retrieval hook
Found by: reviewer
Detail: PreprocessingService.process and classify_intent_node carry no user_id, so the hook always searches as anonymous (global docs only). Thread user_id from GraphState (T6) before enabling the flag.
Spec ref: Must (every retrieval filters by user_id)
Resolution: open; ADVANCED_RETRIEVAL_ENABLED stays False until T5/T6.

### ISS-44 [OPEN] [T4] [OPTIMIZATION] [MAJOR] Advanced service built synchronously on first request, no timeout, runs for every intent
Found by: reviewer
Detail: profiling, sandbox seeding, ~83 query executions and catalog embedding happen on the event loop; hook runs after the legacy gather with no asyncio.wait_for and also for OFF_TOPIC/SCHEMA_QUERY. Needs startup warm-up, to_thread, and a timeout.
Spec ref: Performance
Resolution: open.

### ISS-45 [OPEN] [T4] [CODE] [MAJOR] Production hook never uses the small-model intent classifier or intent routing
Found by: reviewer
Detail: build_advanced_service(llm=None) means heuristic intent only; OFF_TOPIC and SCHEMA_QUERY are not routed away from retrieval; LLM call has no timeout. Wire llm in T7 (retry policy) / T5.
Spec ref: Must (small models for intent classification)
Resolution: open.

### ISS-46 [OPEN] [T4] [CODE] [MAJOR] AnswerBank built from synthetic seeded sandbox; answer-aware signal is weak
Found by: reviewer
Detail: answers are fabricated numbers that never refresh; with alpha 0.9 the answer term is nearly inert, so gains are not evidence that real answers help. Production needs answers from non-user-specific aggregates of real data (preserving isolation) and periodic refresh.
Spec ref: T4 answer-aware reranking
Resolution: open.

### ISS-47 [OPEN] [T4] [CODE] [MINOR] Misc T4 code findings
Found by: reviewer
Detail: unknown metric ids from a stale vector store raise KeyError and disable advanced retrieval for the request; compression returns an over-budget skeleton when joins alone exceed the budget and multi-word enum values never match; _COMPLEX 'vs.' never matches and heuristic results report the INTENT_MODEL name; unused `re` import; metric_candidates not initialised in GraphState.
Spec ref: Edge cases; test case join-compress
Resolution: open.

### ISS-48 [OPEN] [T4] [OPTIMIZATION] [MINOR] Linear scans and unbounded sub-query fan-out
Found by: reviewer
Detail: BM25 and the in-memory cosine scan are O(N) per query; each sub-query runs a 60-hit search; no cap on sub-query count; Postgres tsvector/GIN path from the spec not implemented (BM25 is in-process).
Spec ref: Performance (hybrid via tsvector + GIN)
Resolution: open.

### ISS-49 [OPEN] [T4] [TEST_GAP] [MINOR] No tests for the flag-guarded hook, concurrent multi-user isolation, or sub-query limits
Found by: reviewer
Detail: the isolation fix was only verified manually.
Spec ref: Acceptance
Resolution: open.

### ISS-50 [OPEN] [T4] [TEST_GAP] [MINOR] Reported gains are on the leaky golden set (ISS-36)
Found by: reviewer
Detail: recall@5 0.931 -> 0.993, MRR 0.875 -> 0.954, precision@5 0.208 -> 0.227 (real model) are not independently demonstrated; ablation showed RRF multi-query (complex MRR 0.95 -> 0.43) and boost 0.25 hurt, so defaults are round-robin, alpha 0.9, boost 0.05.
Spec ref: T4 Acceptance
Resolution: open.

### ISS-51 [CORRECTED] [T4] [TEST] [MINOR] multi-query cases over-specified RRF consensus ordering
Found by: task run
Detail: expected first item 'b' assumed RRF; SPECS.md requires only merged and deduped sub-query results.
Spec ref: Test cases multi-query
Resolution: test-designer CORRECT mode replaced it with rank-robust invariants (tops of sub-queries in first positions).

### ISS-52 [FIXED] [T5] [CODE] [BLOCKER] Non-SELECT LLM-generated SQL could execute and commit inside the graph
Found by: reviewer
Detail: validation_module only parsed and blocked the substring DROP; execute_query_node ran db_service.execute (which commits row-less statements) before answer_query's assert_select_only ran.
Spec ref: T5 Acceptance (validated SQL); Constraints Must (SELECT only)
Resolution: validate() rejects non-SELECT via assert_select_only; execute_query_node re-checks before executing. Verified on UPDATE/DELETE/INSERT/TRUNCATE/ALTER/multi-statement; full suite 312 passed.

### ISS-53 [OPEN] [T5] [CODE] [MAJOR] Parameterized resolve/execute runs before authorization on the dissatisfied path
Found by: reviewer
Detail: a non-super user asking to regenerate triggers an embedding call and a DB query, then gets 403; the result is discarded in every dissatisfied case. authorize_generation is also passed param_attempted=True unconditionally. Move the check first.
Spec ref: gen-blocked-user; Performance
Resolution: open.

### ISS-54 [OPEN] [T5] [OPTIMIZATION] [MAJOR] execute_params has no row cap, statement timeout or streaming
Found by: reviewer
Detail: whole result set loaded into memory and returned; the 500-row limit only affects caching.
Spec ref: Edge cases (very large result sets: row cap, truncated flag)
Resolution: open.

### ISS-55 [OPEN] [T5] [CODE] [MINOR] Backend failures reported as no_match; empty query reaches generation for super
Found by: reviewer
Detail: except Exception around resolve/execute turns DB/embedding errors into HTTP 200 no_match inviting regenerate; super with dissatisfied=True and an empty query goes straight to the LLM graph.
Spec ref: Edge cases
Resolution: open.

### ISS-56 [OPEN] [T5] [TEST_GAP] [MINOR] Tests use fake resolver/generate/execute
Found by: reviewer
Detail: real graph path never exercised; no test that non-SELECT SQL is blocked before execution (the ISS-52 blocker was invisible to the suite); no test that execute_params rejects multi-statement/DML; FakeResolver injects bind['user_id'] which real render_template does not.
Spec ref: Test cases
Resolution: open.

### ISS-57 [OPEN] [T5] [CONSTRAINT] [MINOR] Catalog templates are global; results are not user-scoped
Found by: reviewer
Detail: cache-cross-user expects B to get own scoped data, but templates take no user_id bind, so isolation is only at cache-key level. Needs per-user scoping (row filter binds) in T6.
Spec ref: cache-cross-user; Must (user data isolation)
Resolution: open; assign to T6.

### ISS-58 [OPEN] [T5] [OPTIMIZATION] [MINOR] _default_generate repeats resolve
Found by: reviewer
Detail: a second embedding/retrieval pass per regenerate on top of answer_query's own.
Spec ref: Performance
Resolution: open.

### ISS-59 [OPEN] [T5] [CONVENTION] [MINOR] Test env side effects
Found by: task run
Detail: tests/conftest.py now sets a dummy GEMINI_API_KEY so app.main imports; 2 formerly failing tests in test_services.py (ISS-16) now pass as a side effect, so ISS-16 is resolved by environment, not by a code fix. ParamResolver threshold 0.4 is calibrated on the leaky golden set (ISS-36). ISS-5 (cache_hit wiring) satisfied for the parameterized path.
Spec ref: n/a
Resolution: open (informational).

### ISS-60 [FIXED] [T5] [CODE] [BLOCKER] SELECT-only guard bypassable via writable CTE, SELECT INTO and side-effect functions
Found by: reviewer (re-review)
Detail: assert_select_only checked only the root node; `WITH d AS (DELETE ... RETURNING *) SELECT ...`, `SELECT ... INTO t` and `SELECT pg_sleep(1)` passed.
Spec ref: Constraints Must Not (generated SQL must never modify data)
Resolution: assert_select_only now walks the whole tree rejecting Insert/Update/Delete/Create/Drop/Alter/Merge/Command/Lock/Into/etc., SELECT INTO, and a denylist of side-effecting functions (pg_sleep, lo_import, set_config, nextval, dblink*, load_extension, ...). Verified on 18 adversarial and 7 valid inputs; full suite 312 passed; all 83 catalog templates still validate. No third reviewer pass was run (re-review limit reached), so this fix is only adversarially checked by the implementer.

### ISS-61 [OPEN] [T5] [TEST_GAP] [MINOR] No regression tests for writable-CTE / SELECT INTO / function denylist
Found by: reviewer (re-review)
Detail: tests cover only top-level non-SELECT and stacked statements; the ISS-60 cases are checked manually only. A denylist is also inherently incomplete; a DB role with SELECT-only privileges is the durable control.
Spec ref: Edge cases
Resolution: open.

### ISS-62 [FIXED] [T6] [CODE] [BLOCKER] Sentinel /history endpoints exposed every user's scans
Found by: reviewer
Detail: GET /sentinel/history and /history/{scan_id} took no identity and read global scan files.
Spec ref: T6 isolation by user_id on every read path
Resolution: endpoints require identity and return only scans owned by the caller (InsightStore.scan_owners, registered in persist_scan); unowned/unknown scans 404; legacy scans without an owner become invisible. Verified with TestClient (owner 200; other user and anonymous empty list/404); full suite 387 passed.

### ISS-63 [OPEN] [T6] [CODE] [MAJOR] /eval and /alert endpoints unauthenticated
Found by: reviewer
Detail: /eval/runs, /eval/metrics, POST /eval/run and /alert have no get_identity, so they stay open even with AUTH_REQUIRED=True; POST /eval/run triggers expensive work (see ISS-31). Should require role admin/super.
Spec ref: T6 identity on endpoints
Resolution: open.

### ISS-64 [OPEN] [T6] [CODE] [MAJOR] Anonymous users share one bucket; no fail-closed startup
Found by: reviewer
Detail: with AUTH_REQUIRED=False every anonymous request is user_id 'anonymous' and shares sessions, insights, cache and scan history; startup does not fail when AUTH_REQUIRED=True and JWT_SECRET is unset; with ALLOW_HEADER_IDENTITY X-User-Role is self-asserted (dev only).
Spec ref: Must (user isolation)
Resolution: open; deployment must set JWT_SECRET and AUTH_REQUIRED=true.

### ISS-65 [OPEN] [T6] [CODE] [MAJOR] Raw query text in audit detail, insight question, chat content and logs
Found by: reviewer
Detail: _DROP_KEYS strips keys only; PII/secrets typed into questions land in nl2sql_audit (500 chars), insights, chat and the application log. SQL is hashed correctly.
Spec ref: Must Not (log secrets); audit design
Resolution: open; consider storing a query hash plus redaction.

### ISS-66 [OPEN] [T6] [CODE] [MAJOR] Audit events can be lost
Found by: reviewer
Detail: no shutdown hook calls AuditDB.stop()/flush() (main.py has no lifespan); crash loses the queue; a failed batch is logged and dropped; one bad row (e.g. user_id > 64 chars, never validated for JWT sub / X-User-Id) poisons a batch of up to 100; drops are counted but not surfaced. Oversized user_id also 500s create_session.
Spec ref: T6 each request yields an audit row
Resolution: open.

### ISS-67 [OPEN] [T6] [CONSTRAINT] [MAJOR] ISS-57 deferred: catalog templates are global, results not user-scoped
Found by: reviewer
Detail: isolation exists in cache keys and in session/insight/audit/scan-owner rows, but template SQL takes no user_id bind. Needs a business-data ownership model (which tables map to a user, row-filter binds) that the existing schema does not define; user decision needed. ISS-57 stays open and is superseded by this entry.
Spec ref: Must (user data isolation); cache-cross-user
Resolution: open; needs user verdict.

### ISS-68 [OPEN] [T6] [CODE] [MAJOR] Audit of denied/failed paths is partial
Found by: reviewer
Detail: Sentinel scans audited only on success; 401s from get_identity and failed scans produce no audit row.
Spec ref: Audit of denied/failed paths
Resolution: open.

### ISS-69 [OPEN] [T6] [CODE] [MINOR] Misc T6 findings
Found by: reviewer
Detail: 403 for regenerate-by-non-super raised after the user message is appended and after resolve/execute (orphan user message; see ISS-53); 404 vs 403 leaks session id existence; JWT has no iss/aud/leeway/min secret length; 3-5 sequential SQLite round trips per request and unlocked get_query_deps singletons; column_retriever/few_shot_retriever call search() without user_id (global only, safe); legacy scans without owner are now invisible in history.
Spec ref: Performance; Isolation
Resolution: open.

### ISS-70 [OPEN] [T6] [TEST_GAP] [MINOR] T6 test gaps
Found by: reviewer
Detail: no tests for unauthenticated sentinel/eval/alert endpoints, scan history ownership (ISS-62 verified manually), audit loss on shutdown or poisoned batch, oversized user_id, query text in audit, 401 audit, per-user result scoping; REVIEW.md has no T6 section (reviewer cannot append).
Spec ref: Test cases
Resolution: open.

### ISS-71 [FIXED] [T1] [CODE] [MAJOR] Vector metadata not validated on add_documents
Found by: reviewer (T1), fixed during T6
Detail: ISS-6.
Spec ref: Must (metadata keys), Must Not (cross-user data)
Resolution: add_documents raises ValueError when metadata lacks the 'user_id' key (None allowed for global docs); covered by T6 vector rows.

### ISS-72 [OPEN] [T6] [CODE] [MAJOR] Sentinel scan_id has 1-second resolution and is not unique
Found by: reviewer (re-review)
Detail: two users scanning in the same second share a scan_id; the file is overwritten and register_scan is first-writer-wins, so the second user gets 404 on their own scan and the first can read the second's data under their id. Add a uuid suffix and make register_scan fail on conflict.
Spec ref: Per-user isolation
Resolution: open.

### ISS-73 [OPEN] [T6] [OPTIMIZATION] [MINOR] History list filters in Python over all scans
Found by: reviewer (re-review)
Detail: scan_memory.list_scans() reads every scan file before filtering to the caller's.
Spec ref: Performance
Resolution: open.

### ISS-74 [FIXED] [T7] [CODE] [BLOCKER] Retry layer multiplied the existing rate-limit loop and blocked the event loop
Found by: reviewer
Detail: each RetryPolicy attempt called generate_response, which had its own 3x 429 loop with blocking time.sleep (2/4/8s): up to 9 provider calls and ~42s of event-loop freeze per request.
Spec ref: T7 Acceptance (failures stop at 3 attempts); no blocking calls on the event loop
Resolution: generate_response gained internal_retries (legacy default 3; sql generation passes 1 under RetryPolicy), no sleep after the last attempt, asyncio.sleep instead of time.sleep, provider calls via asyncio.to_thread. Verified: 1 provider call and 0s per rate-limited attempt, loop responsive during a 0.5s provider call, 461 tests pass.

### ISS-75 [OPEN] [T7] [CODE] [MAJOR] Failure detection relies on error strings; provider finish reasons other than 1/3 fall through
Found by: reviewer
Detail: generate_response converts errors to strings; is_llm_failure matches three prefixes; an unseen error string or finish_reason other than 1/3 can be treated as SQL or success. generate_text/generate_structured should use a raising primitive.
Spec ref: Failures must raise LLMCallError structurally
Resolution: open.

### ISS-76 [OPEN] [T7] [CODE] [MAJOR] One-shot parsing and backend alerts cover only intent classification
Found by: reviewer
Detail: insight_generation, visualization, clarification, entity_extractor, table_retriever, alert_generation, alert_system/*, retrieval/intent_small, learning/service and sentinel_agent still parse JSON ad hoc (fence stripping, swallowed errors) with no backend alert. Spec says LLM output must be parsed in one shot with an alert on failure; migrate each caller to parse_llm_json.
Spec ref: Must (unparsable LLM output raises backend alert)
Resolution: open.

### ISS-77 [OPEN] [T7] [CODE] [MAJOR] Intent classification still guesses on failure
Found by: reviewer
Detail: unparsable reply returns ("SELECT", 0.5, ...) which route_after_intent lets through (0.5 < 0.5 is false); LLMCallError/provider failures return ("SELECT", 0.9, ...) with no alert; IntentReply.intent is an unconstrained str and confidence/clarity_score unbounded.
Spec ref: Must (strict parsing; no guessing)
Resolution: open; fail to clarification and alert instead.

### ISS-78 [OPEN] [T7] [CODE] [MAJOR] Deterministic fallback runs without user scoping and outside the generated-SQL walk
Found by: reviewer
Detail: get_fallback().handle(query) has no user_id (graph state carries none); catalog SQL runs via execute_params with root-type check only. Data scoping is ISS-67; risk is data exposure, not injection (templates are trusted).
Spec ref: Must (user data isolation)
Resolution: open; tied to ISS-67.

### ISS-79 [OPEN] [T7] [CODE] [MAJOR] Backend alerts may carry LLM output / PII; Slack throttle hides distinct failures
Found by: reviewer
Detail: raw[:500] and pydantic ValidationError text (input_value=...) reach logs, alert history and Slack; throttle is per kind (60s), so a different failing stage in the window is not posted; per process; task handle not retained.
Spec ref: Must Not (secrets in logs); alerting design
Resolution: open; redact LLM output, key throttle by kind+stage.

### ISS-80 [OPEN] [T7] [OPTIMIZATION] [MAJOR] Generated SQL executes inside the retry loop without row cap/timeout
Found by: reviewer
Detail: a heavy query can run up to 3 times plus the fallback; execute_params loads all rows; execution errors regenerate with the same prompt (no error feedback to the model).
Spec ref: Performance; edge cases (large results)
Resolution: open (see ISS-54).

### ISS-81 [OPEN] [T7] [CODE] [MINOR] Misc T7 findings
Found by: reviewer
Detail: guidance detection uses startswith("Error") and .replace("Error:", "") strips every occurrence; legacy validate_sql/repair_sql/execute_query nodes are dead but would stack a retry_count<3 loop on the 3 attempts if reached; RETRY_MODEL/FINAL_RETRY_MODEL default to Gemini names (OpenAI provider would always fail attempts 2-3); StaticPool shares one SQLite connection across threads (not thread-safe under concurrency) and pool_pre_ping now applies everywhere; base_delay is 0 (no backoff) and non-LLM errors consume LLM attempts.
Spec ref: Max 3 attempts across the request; conventions
Resolution: open.

### ISS-82 [OPEN] [T7] [TEST_GAP] [MINOR] T7 test gaps
Found by: reviewer
Detail: model_name reaching generate_response via sql_generation_module; 429 loop + RetryPolicy within 3 provider calls (ISS-74 verified manually); compiled-graph run without the legacy loop; LLMCallError path in intent; SELECT-only before execute_params on the generated path; redaction of LLM output in alerts; fallback user scoping.
Spec ref: Test cases
Resolution: open.

### ISS-83 [OPEN] [T7] [CONVENTION] [MINOR] Side effects of existing-test fixes
Found by: task run
Detail: test_workflow previously passed only because DatabaseService.execute swallowed "no such table"; in-memory SQLite now uses a shared StaticPool connection (see ISS-81). test_llm_service_retry_logic now really sleeps 2s (asyncio.sleep, patch of time.sleep no longer applies), so the full suite is slower.
Spec ref: n/a
Resolution: open (informational).

### ISS-84 [OPEN] [T7] [CODE] [MINOR] internal_retries <= 0 skips the provider call
Found by: reviewer (re-review)
Detail: generate_response with internal_retries<=0 returns the rate-limit error string without calling the provider; unreachable from current callers. Clamp with max(1, internal_retries).
Spec ref: robustness
Resolution: open.

### ISS-85 [FIXED] [T8] [CODE] [BLOCKER] Streaming scan could not carry the JWT; authenticated users' scans were owned by 'anonymous'
Found by: reviewer
Detail: the dashboard used the browser EventSource, which cannot set Authorization, so with a token the stream ran as anonymous (or 401 when AUTH_REQUIRED=true) while rating/anomaly/thread calls used the token's user: rate_insight then 404'd and the whole rating, handoff and anomaly flow failed.
Spec ref: T8 Acceptance (false rating opens a human review thread); ISS-62 isolation
Resolution: new authEventSource.ts (fetch-based SSE with Authorization header) replaces EventSource in SentinelDashboard.tsx; verified with tsc (strict) and a node run against a fake chunked stream (header sent, 3 events parsed across a chunk split). Not exercised in a real browser.

### ISS-86 [OPEN] [T8] [CODE] [MAJOR] Reviewer notification spam and no way to clear a flag
Found by: reviewer
Detail: `changed` flips on every true/false toggle, so each false toggle posts to Slack (no cooldown; create_task handle not kept); a true rating leaves the thread open. There is no acknowledge route (store method only), and since the flag is derived from ratings alone, acknowledging re-flags on the next detect/rating with a fresh alert (no baseline).
Spec ref: Edge cases (duplicate rating counts once); anomaly flag lifecycle
Resolution: open.

### ISS-87 [OPEN] [T8] [CONSTRAINT] [MAJOR] Reliability stack only partly applied to Sentinel
Found by: reviewer
Detail: Sentinel missions still run through app_graph free-form generation: they get retry/model escalation/deterministic fallback/strict parsing (brainstorm too) and audit, but not param-first resolution, the Redis cache, or the super-only generation gate; run_mission passes no user_id/role, so free-form SQL runs for every scan caller. Mission queries are free text with no metric id, so param-first does not map directly. Options: resolve mission text through ParamResolver first, cache brainstorm and mission results per user, and require a role for scans. Needs your decision.
Spec ref: "The above should be utilized for SentinelIQ too"
Resolution: open; needs user verdict.

### ISS-88 [OPEN] [T8] [CODE] [MAJOR] Anonymous shared bucket makes threads and flags crowd-shared
Found by: reviewer
Detail: with anonymous access every caller is user_id 'anonymous': any anonymous caller can rate or flip others' insights, share one false-rating count/flag, and read/post in each other's threads. Same root as ISS-64.
Spec ref: Isolation; flag integrity
Resolution: open; AUTH_REQUIRED=true in deployment.

### ISS-89 [OPEN] [T8] [CODE] [MINOR] Concurrency, scope and perf findings
Found by: reviewer
Detail: no unique open-flag constraint (concurrent detect can duplicate flags/alerts; open_thread has the same check-then-insert race; get_feedback_deps unlocked); false_rating_count fetches all ids instead of COUNT and has no (user_id, rating) index; staff list_threads unbounded without insight context; a false rating on a chat-source insight opens a thread but never counts toward the sentinel-only anomaly flag (intent unconfirmed).
Spec ref: Concurrency; performance; anomaly scope
Resolution: open.

### ISS-90 [OPEN] [T8] [CODE] [MINOR] Frontend findings
Found by: reviewer
Detail: HandoffChat polls every 5s without backoff, stop on resolved, or tab-hidden skip, and send() has no try/catch; in streaming mode insight_id arrives only at the end-of-scan insights_saved event so ratings are impossible mid-scan; rateInsight ignores non-ok responses silently; rating/thread state is lost on reload and the thread list endpoint is unused (no staff UI to list open threads); historical scans cannot be rated.
Spec ref: Frontend correctness
Resolution: open.

### ISS-91 [OPEN] [T8] [TEST_GAP] [MINOR] T8 test gaps and conventions
Found by: reviewer
Detail: no tests for the EventSource/auth mismatch (verified manually), Slack spam on toggling, re-flag after acknowledge, concurrent detect, anonymous cross-user thread access, chat-source rating, staff list filter; handlers repeat function-level imports and post_handoff_message audits without starting the writer; REVIEW.md has no T8 section (reviewer cannot append).
Spec ref: Test cases; conventions
Resolution: open.

### ISS-92 [OPEN] [T8] [CODE] [MINOR] authEventSource edge cases
Found by: reviewer (re-review)
Detail: parser splits only on '\n\n' (CRLF servers such as sse-starlette would dispatch nothing; this backend uses a plain StreamingResponse that emits '\n\n', so not affected today); a clean stream end calls onerror (native EventSource would reconnect; the dashboard closes the client on the final event first); no committed test for the client (verified ad hoc with node).
Spec ref: parity with EventSource
Resolution: open; normalize '\r\n?' to '\n' and add a test.

### ISS-93 [OPEN] [T1] [CONVENTION] [BLOCKER] Real database credential committed in tracked .env.example
Found by: reviewer (T1 re-review 2026-11-18); verified by the implementer
Detail: .env.example line 3 holds DATABASE_URL=mysql+pymysql://admin:<password redacted here>@database-1.cxu2seuselq9.ap-south-1.rds.amazonaws.com:3306/derivinsight. It is the only tracked file with a user:password URL and is in git history (at least commit b2f14f0). Do not copy the value into issues, logs or chat. Action: rotate that password now, replace the line with a placeholder, and treat the repository history as exposed (history rewrite or private repo).
Spec ref: Constraints Must Not (secrets); conventions
Resolution: open; needs your action (rotation) before any code change matters.

### ISS-94 [OPEN] [T1] [CODE] [MAJOR] DatabaseService logs the full DATABASE_URL including the password for non-SQLite URLs
Found by: reviewer (T1 re-review 2026-11-18); verified by the implementer
Detail: app/services/database.py:21 logs settings.DATABASE_URL.split('///')[-1], which only trims SQLite URLs; a PostgreSQL/MySQL URL is logged whole on every start-up. Use engine.url.render_as_string(hide_password=True).
Spec ref: Must Not (secrets in logs); T1 Acceptance on PostgreSQL
Resolution: open.

### ISS-95 [OPEN] [T1] [CODE] [MAJOR] Forced canonical migration of a legacy source passes the pre-check then fails mid-copy
Found by: reviewer (T1 re-review 2026-11-18)
Detail: missing_required_columns treats users.full_name as satisfiable via username, but _copy_table never applies the mapping and silently drops source columns absent from the target (if k in tgt_cols), so --target-schema canonical on a legacy source fails with NotNullViolation after earlier batches are committed. The check and the copy use different rules. Refuse canonical for a non-canonical profile, or apply the mapping and report every dropped column.
Spec ref: T1 I/O contract; edge cases; ISS-7
Resolution: open.

### ISS-96 [OPEN] [T1] [CODE] [MAJOR] A cancelled get_or_compute leader fails every waiting caller
Found by: reviewer (T1 re-review 2026-11-18)
Detail: cache.py: if the leader is cancelled (client disconnect), fut.set_exception(CancelledError) makes all waiters that shielded the future raise CancelledError although they were not cancelled. Waiters should recompute or fall through.
Spec ref: Edge cases (concurrent identical queries)
Resolution: open.

### ISS-97 [OPEN] [T1] [CODE] [MAJOR] migration verify() can claim "verified" while skipping columns
Found by: reviewer (T1 re-review 2026-11-18)
Detail: verify() runs CAST(col AS NUMERIC) on the target; boolean columns fail on PostgreSQL and the broad except continues without reporting; a Decimal NaN raises outside the try; the tolerance is an absolute 0.01 regardless of magnitude. Report skipped columns and use a relative tolerance.
Spec ref: T1 Acceptance (identical results); ISS-7
Resolution: open.

### ISS-98 [OPEN] [T1] [CODE] [MINOR] Timestamp parsing is lossy
Found by: reviewer (T1 re-review 2026-11-18)
Detail: schema_mapping.parse_timestamp assumes US M/D/Y (only impossible dates are rejected, so 3/4/24 silently becomes March 4), discards ISO offsets (+05:30, Z) without conversion, and truncates fractional seconds. Correct for the bundled data today (all 500 transaction timestamps are M/D/YY with day > 12 in 283 rows); document the assumptions and count offset-bearing values.
Spec ref: T1 Acceptance; edge cases
Resolution: open.

### ISS-99 [OPEN] [T1] [CODE] [MINOR] Mapping-rewrite failure modes
Found by: reviewer (T1 re-review 2026-11-18)
Detail: alias_map is global so one alias reused for different tables in separate subqueries maps to the wrong table; unqualified-column candidates are collected across the whole statement (false 'ambiguous' errors, safe); bind_maps[name] is overwritten when one bind is compared with two columns having different value maps; value translation covers only =, <>, IN (not LIKE, CASE, column-to-column); res.wrapper.format treats the profile JSON as trusted. None is tested.
Spec ref: T1 Acceptance; ISS-3 option (c)
Resolution: open.

### ISS-100 [OPEN] [T1] [OPTIMIZATION] [MINOR] norm_ts() in WHERE defeats timestamp indexes on SQLite
Found by: reviewer (T1 re-review 2026-11-18)
Detail: acceptable at 100/500/200 rows; PostgreSQL mirrors use real TIMESTAMPs and are unaffected.
Spec ref: Performance
Resolution: open.

### ISS-101 [OPEN] [T1] [CODE] [MINOR] vector_store residual edge cases
Found by: reviewer (T1 re-review 2026-11-18)
Detail: ef_search is capped at 1000 by pgvector (a top_k above 1000 makes SET LOCAL fail and surfaces as VectorStoreUnavailable; clamp it); relaxed_order iterative scan returns only approximately ordered results with no strict re-sort and max_scan_tuples (20000) can still under-return for a very selective user on a huge table; a non-string user_id is stored through a text cast but then fails on search (cast with str() in both paths); _ensure has no cross-process lock (two workers can race CREATE TABLE) and _ready stays False after any DDL failure so every call retries the DDL.
Spec ref: Edge cases; performance; isolation parity
Resolution: open.

### ISS-102 [OPEN] [T1] [CODE] [MINOR] Migration robustness
Found by: reviewer (T1 re-review 2026-11-18)
Detail: table/column names are interpolated unquoted into SQL; _reset_sequence runs setval on every reflected table (including untouched ones) and can move a sequence backwards; idempotency holds only for tables with a primary key (others duplicate on re-run); source tables without a target counterpart are skipped silently; each batch commits on its own (partial data after a failure); results pass through a module-global mutable report object (not re-entrant) and _copy_table returns an unused tuple.
Spec ref: T1 Acceptance; edge cases; conventions
Resolution: open.

### ISS-103 [OPEN] [T1] [CONSTRAINT] [MINOR] Default REDIS_HOST is a concrete cloud hostname with SSL on
Found by: reviewer (T1 re-review 2026-11-18)
Detail: config.py defaults REDIS_HOST to a specific AWS ElastiCache host, so an unconfigured environment dials it on every cache call (compounds ISS-8). Default to None/localhost. Local override that works today: REDIS_HOST= (empty) and REDIS_URL=redis://localhost:6379/0 (REDIS_USE_SSL is ignored).
Spec ref: Must Not; Edge: Redis unavailable
Resolution: open.

### ISS-104 [OPEN] [T1] [OPTIMIZATION] [MINOR] StaticPool shares one SQLite connection across worker threads
Found by: reviewer (T1 re-review 2026-11-18)
Detail: in-memory/test only; concurrent transactions can interleave. File databases use a normal pool; the norm_ts connect listener is registered before the first connect.
Spec ref: Performance; thread safety
Resolution: open.

### ISS-105 [OPEN] [T1] [TEST_GAP] [MINOR] Mapping/migration/vector edge cases untested
Found by: reviewer (T1 re-review 2026-11-18)
Detail: no tests for D/M ambiguity, ISO offsets, alias collisions across subqueries, bind-map collisions, LIKE/CASE on mapped enum columns; no offline tests for verify(), _reset_sequence, the canonical-refusal path (live test only) or PgVectorBackend with a mocked engine.
Spec ref: Test cases; edge cases
Resolution: open.

### ISS-106 [OPEN] [T2] [CODE] [BLOCKER] Enum-parameter metrics report "available" on the live DB but silently return wrong results
Found by: reviewer (T2 re-review 2026-11-18); verified by the implementer against derivinsightnew.db
Detail: only login_events.device_type has a value_map. Live vocabularies differ from the catalog's choices: payment_method is Credit Card/Debit Card/Bank Transfer/Wire Transfer/Crypto Wallet/E-Wallet/ACH/SEPA (catalog: CARD, BANK_TRANSFER, CRYPTO, EWALLET, so transactions_payment_method_count returns 0 for every choice and for the default 'CARD' despite 58 Credit Card rows); transactions.status has CANCELLED (63) not REVERSED; txn_type has REFUND (73) not BONUS; login failure_reason uses 'Account locked'/'Invalid password'/... not WRONG_PASSWORD/ACCOUNT_LOCKED/IP_BLOCKED; users.risk_level has CRITICAL (5 users) which the catalog does not know, so users_risk_level_count, high_risk_user_count, high_risk_user_volume, high_risk_user_failed_logins and the 'HIGH share is exposure' interpretation silently exclude CRITICAL; users.account_status has INACTIVE (5) and PENDING (3), not valid choices. This contradicts the implementer's earlier claim that the 43 available metrics are correct (only that they execute). Fix direction (not applied): extend value_maps and choices from the live distinct values, and mark a metric unavailable or partial when live distinct values do not cover its declared choices.
Spec ref: Must (no wrong answers on the existing schema); edge cases
Resolution: open.

### ISS-107 [OPEN] [T2] [CODE] [MAJOR] Empty strings are treated as real values
Found by: reviewer (T2 re-review 2026-11-18)
Detail: 55 of 500 live transactions have instrument = '' and 125 of 200 login_events have failure_reason = ''. txn_by_instrument_volume filters only IS NOT NULL (an '' instrument appears in the top-N); login_events_by_failure_reason and its count show a blank group of 125. Normalise '' to NULL in the profile or add <> '' to the SQL.
Spec ref: Edge cases (NULL/empty)
Resolution: open.

### ISS-108 [OPEN] [T2] [OPTIMIZATION] [MAJOR] fast/access flags are wrong on the live database
Found by: reviewer (T2 re-review 2026-11-18)
Detail: the live derivinsightnew.db has no secondary indexes (primary-key autoindexes only) and every windowed query wraps created_at in NORM_TS, so fast=True (66 of 83 metrics) is false there; anything preferring fast metrics is misled. Compute fast per physical database next to availability, or expose indexes-present in the availability report. Extends ISS-21; small impact at 500 rows.
Spec ref: T2 fast tagging
Resolution: open.

### ISS-109 [OPEN] [T2] [CODE] [MINOR] Availability reports only the first missing item and passes unknown unqualified columns through
Found by: reviewer (T2 re-review 2026-11-18)
Detail: available_metrics raises on the first UnavailableColumn, so a metric missing several things names one; unqualified columns unknown to the logical schema are not checked (a typo or physical-only column fails at execution, not at availability time).
Spec ref: Edge cases
Resolution: open.

### ISS-110 [OPEN] [T2] [CODE] [MINOR] Timestamp assumptions affect catalog windows
Found by: reviewer (T2 re-review 2026-11-18)
Detail: same root as ISS-98: parse_timestamp discards offsets and assumes M/D/YY, so mixed-offset or D/M data would be mis-windowed. Observation: the bundled data spans 2024-01 to 2025-12 and today is 2026-11-18, so default 7-day windows return empty there (correct, but surprising in demos).
Spec ref: Edge cases
Resolution: open.

### ISS-111 [OPEN] [T2] [TEST_GAP] [MINOR] No test checks enum-parameter metrics against the legacy vocabulary
Found by: reviewer (T2 re-review 2026-11-18)
Detail: tests/legacy_db.py contains 'Credit Card' and REFUND but nothing asserts correct non-zero counts for payment_method/status/txn_type/failure_reason/risk_level metrics, CRITICAL, empty-string instrument/failure_reason, or that declared choices cover the live distinct values. test_every_available_metric_executes_on_legacy proves only that the SQL runs, which is the gap ISS-106 exposes.
Spec ref: Test cases metric-catalog
Resolution: open.

### ISS-112 [OPEN] [T2] [CONVENTION] [MINOR] metric_catalog.json churns on generated_at and has no freshness test
Found by: reviewer (T2 re-review 2026-11-18)
Detail: the committed metrics array is byte-identical to a fresh generate_catalog() and the version matches (v1-6f34a2dd), but generated_at changes on every run and nothing tests freshness. Open part of ISS-26/27.
Spec ref: conventions
Resolution: open.
