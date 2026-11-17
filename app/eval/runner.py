"""Regression/eval runner, persistent run store, and an offline baseline pipeline."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, insert, select
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.logger import logger
from app.eval import judge, metrics as M
from app.modules.catalog import schema_profiler as sp
from app.modules.catalog import templates as tpl
from app.modules.catalog.metric_generator import generate_catalog, index_catalog

GATED = ["precision", "recall", "faithfulness", "relevancy"]
SERIES = GATED


def check_regression(current: Dict[str, float], baseline: Dict[str, float], max_drop: float = 0.05) -> List[str]:
    """Names of gated metrics that dropped by more than max_drop (exactly max_drop is allowed)."""
    out = []
    for k in GATED:
        if k in current and k in baseline and round(baseline[k] - current[k], 9) > max_drop:
            out.append(k)
    return out


class EvalStore:
    def __init__(self, url: Optional[str] = None):
        url = url or settings.EVAL_DATABASE_URL
        kw: Dict[str, Any] = {}
        if url.startswith("sqlite") and (url in ("sqlite://", "sqlite:///:memory:")):
            kw = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        elif url.startswith("sqlite"):
            kw = {"connect_args": {"check_same_thread": False}}
        self.engine = create_engine(url, **kw)
        md = MetaData()
        self.runs = Table("eval_runs", md,
                          Column("seq", Integer, primary_key=True, autoincrement=True),
                          Column("run_id", String(36), unique=True, nullable=False),
                          Column("started_at", String(40), nullable=False), Column("label", String(100)),
                          Column("k", Integer), Column("n_items", Integer), Column("metrics", Text),
                          Column("regressed", Integer), Column("regressions", Text),
                          Column("baseline_run_id", String(36)))
        self.items = Table("eval_items", md, Column("id", Integer, primary_key=True, autoincrement=True),
                           Column("run_id", String(36), index=True), Column("item_id", String(40)),
                           Column("result", Text))
        md.create_all(self.engine)

    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return {"run_id": r.run_id, "started_at": r.started_at, "label": r.label, "k": r.k, "n_items": r.n_items,
                "metrics": json.loads(r.metrics), "regressed": bool(r.regressed),
                "regressions": json.loads(r.regressions or "[]"), "baseline_run_id": r.baseline_run_id}

    def save_run(self, run: Dict[str, Any], items: Optional[List[Dict[str, Any]]] = None) -> str:
        run_id = run.get("run_id") or str(uuid.uuid4())
        with self.engine.begin() as c:
            c.execute(insert(self.runs).values(
                run_id=run_id, started_at=run["started_at"], label=run.get("label"), k=run.get("k"),
                n_items=run.get("n_items"), metrics=json.dumps(run["metrics"]),
                regressed=int(bool(run.get("regressed"))), regressions=json.dumps(run.get("regressions", [])),
                baseline_run_id=run.get("baseline_run_id")))
            if items:
                c.execute(insert(self.items), [{"run_id": run_id, "item_id": str(i.get("id")),
                                                "result": json.dumps(i, default=str)} for i in items])
        return run_id

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as c:
            r = c.execute(select(self.runs).where(self.runs.c.run_id == run_id)).first()
            if not r:
                return None
            out = self._row(r)
            out["items"] = [json.loads(x.result) for x in
                            c.execute(select(self.items).where(self.items.c.run_id == run_id).order_by(self.items.c.id))]
            return out

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(self.runs).order_by(self.runs.c.seq.desc()).limit(limit)).all()
        return [self._row(r) for r in rows]

    def latest_baseline(self) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as c:
            r = c.execute(select(self.runs).where(self.runs.c.regressed == 0)
                          .order_by(self.runs.c.seq.desc()).limit(1)).first()
        return self._row(r) if r else None

    def metric_series(self) -> Dict[str, List[Dict[str, Any]]]:
        with self.engine.connect() as c:
            rows = c.execute(select(self.runs).order_by(self.runs.c.seq.asc())).all()
        out: Dict[str, List[Dict[str, Any]]] = {k: [] for k in SERIES}
        for r in rows:
            m = json.loads(r.metrics)
            for k in SERIES:
                out[k].append({"run_id": r.run_id, "ts": r.started_at, "value": m.get(k, 0.0)})
        return out


Retriever = Callable[[str], Awaitable[List[str]]]
Generator = Callable[[str], Awaitable[Dict[str, Any]]]


class EvalRunner:
    def __init__(self, retriever: Retriever, generator: Generator, store: EvalStore, concurrency: int = 8):
        self.retriever, self.generator, self.store = retriever, generator, store
        self._sem = asyncio.Semaphore(concurrency)

    async def _eval_item(self, item: Dict[str, Any], k: int) -> Dict[str, Any]:
        res: Dict[str, Any] = {"id": item["id"], "question": item["question"], "precision": 0.0, "recall": 0.0,
                               "mrr": 0.0, "faithfulness": 0.0, "relevancy": 0.0, "execution_accuracy": 0.0}
        async with self._sem:
            relevant = item["expected_metric_ids"]
            try:
                retrieved = list(await self.retriever(item["question"]))
                res.update(retrieved=retrieved[:k], precision=M.precision_at_k(retrieved, relevant, k),
                           recall=M.recall_at_k(retrieved, relevant, k), mrr=M.mrr(retrieved, relevant))
            except Exception as e:
                res["error"] = f"retrieval: {e}"
            try:
                gen = await self.generator(item["question"])
                rows = gen.get("rows") or []
                res.update(sql=gen.get("sql"),
                           execution_accuracy=float(M.execution_accuracy(rows, item.get("expected_rows"))),
                           faithfulness=judge.faithfulness(gen.get("answer", ""), rows),
                           relevancy=await judge.relevancy(item["question"], gen.get("answer", "")))
            except Exception as e:
                res["error"] = (res.get("error", "") + f" generation: {e}").strip()
        return res

    async def run(self, golden: Dict[str, Any], k: int = 5, label: Optional[str] = None) -> Dict[str, Any]:
        started = datetime.now(timezone.utc).isoformat()
        items = golden["items"]
        results = await asyncio.gather(*[self._eval_item(i, k) for i in items])
        agg = M.aggregate(results)
        agg["failures"] = sum(1 for r in results if "error" in r)
        base = self.store.latest_baseline()
        regressions = check_regression(agg, base["metrics"], settings.EVAL_MAX_DROP) if base else []
        run = {"run_id": str(uuid.uuid4()), "started_at": started, "label": label, "k": k, "n_items": len(items),
               "metrics": agg, "regressed": bool(regressions), "regressions": regressions,
               "baseline_run_id": base["run_id"] if base else None}
        self.store.save_run(run, results)
        logger.info(f"Eval run {run['run_id']}: {agg} regressed={run['regressed']}")
        return run


def build_baseline_pipeline(catalog: Optional[Dict[str, Any]] = None, seed: int = 0) -> Tuple[Retriever, Generator]:
    """Offline pipeline: vector retrieval over the catalog + top-1 template executed on the seeded sandbox."""
    from app.eval.golden_builder import FIXED_NOW, seed_sandbox
    from app.services.vector_store import InMemoryBackend, VectorStoreService
    profile = sp.profile_schema()
    catalog = catalog or generate_catalog(profile)
    by_id = {m["id"]: m for m in catalog["metrics"]}
    store = VectorStoreService("eval_catalog", backend=InMemoryBackend())
    conn = sp.build_sandbox()
    seed_sandbox(conn, seed)
    lock = asyncio.Lock()
    state = {"indexed": False}

    async def retriever(question: str) -> List[str]:
        async with lock:
            if not state["indexed"]:
                await index_catalog(store, catalog, profile)
                state["indexed"] = True
        hits = await store.search(question, 60, None)
        ids: List[str] = []
        for h in hits:
            if h["metadata"].get("level") == "metric" and h["metadata"]["metric_id"] not in ids:
                ids.append(h["metadata"]["metric_id"])
        return ids

    async def generator(question: str) -> Dict[str, Any]:
        ids = await retriever(question)
        if not ids:
            return {"sql": None, "rows": [], "answer": "No matching metric."}
        m = by_id[ids[0]]
        days = re.search(r"last (\d+) days", question)
        params = {"days": int(days.group(1))} if days and any(p["name"] == "days" for p in m["parameters"]) else {}
        sql, bind = tpl.render_template(m, params, now=FIXED_NOW)
        cur = conn.execute(sql, bind)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchmany(50)]
        preview = "; ".join(", ".join(f"{k}={v}" for k, v in r.items()) for r in rows[:3])
        return {"sql": sql, "rows": rows, "answer": f"{m['name']}: {preview} ({len(rows)} rows)"}

    return retriever, generator
